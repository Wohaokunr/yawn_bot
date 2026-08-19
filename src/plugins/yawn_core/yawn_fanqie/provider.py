"""番茄免费小说公开页面 provider。

provider 只负责 HTTP、页面解析和公开字体解码，不依赖 ORM、任务队列或
NoneBot 权限。页面结构变化时可独立替换本模块。
"""

# provider 的诊断错误需要携带页面/HTTP 上下文，且解析分支较多。
# ruff: noqa: N818, PLR2004, TRY003, TRY300, TRY301, C901, PLR0912, PLR0913, PLR0915, PERF203, PERF401, PYI034

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
from dataclasses import dataclass
from html import unescape
from time import perf_counter
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlparse

import httpx

from .app_protocol import (
    AppChapterUnavailable,
    AppProtocolTransientError,
    FanqieAppClient,
    FanqieAppError,
)
from .browser_search import (
    BrowserSearchError,
    BrowserSearchSnapshot,
    search_page_snapshot,
)
from .config import Config
from .decoder import (
    contains_pua,
    decrypt_pua,
    decrypt_reader_pua,
    extract_initial_state,
    extract_reader_content,
    font_glyph_signature_to_text,
    html_to_text,
)
from .mobile_helper import (
    MobileHelperError,
    fetch_mobile_chapter,
    mobile_helper_configured,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

BASE_URL = "https://fanqienovel.com"
_HOST = "fanqienovel.com"
_BOOK_ID_RE = re.compile(r"(?<!\d)(\d{6,32})(?!\d)")
_FONT_URL_RE = re.compile(
    r"url\(\s*['\"]?(https?://[^)'\"\s]+?\.woff2(?:\?[^)'\"\s]*)?)['\"]?\s*\)",
    re.IGNORECASE,
)
_SEARCH_QUERY_TYPES: dict[str, str] = {"related": "0", "new": "1", "hot": "2"}
_BOOK_TEXT_KEYS = (
    "bookName",
    "book_name",
    "bookTitle",
    "book_title",
    "title",
    "name",
    "author",
    "authorName",
    "author_name",
    "authorNameText",
    "abstract",
    "book_abstract",
    "description",
    "bookIntro",
    "book_intro",
)
_SEARCH_VISIBLE_TEXT_KEYS = (
    "bookName",
    "book_name",
    "bookTitle",
    "book_title",
    "title",
    "name",
    "author",
    "authorName",
    "author_name",
    "authorNameText",
)
_CHAPTER_HEADING_RE = re.compile(
    r"^第(?:\d+|[零〇一二两三四五六七八九十百千万]+)章[：:](.+)$"
)
_RANK_GENDER_CODES: dict[str, str] = {"male": "1", "female": "0"}
_RANK_TYPE_CODES: dict[str, str] = {"read": "2", "new": "1"}
logger = logging.getLogger(__name__)


class FanqieProviderError(RuntimeError):
    """公开页面不可用、结构改变或请求失败。"""


class ChapterUnavailable(FanqieProviderError):
    """章节锁定、正文为空或无法公开解码。"""


class ChapterFetchTransientError(FanqieProviderError):
    """全文后端暂时不可用，任务应保留进度并允许重试。"""


class FanqieServiceUnavailable(FanqieProviderError):
    """搜索或榜单接口被风控、返回异常或页面结构暂时不可用。"""


SearchOrder = Literal["related", "new", "hot"]
RankGender = Literal["male", "female"]
RankType = Literal["read", "new"]


@dataclass(frozen=True, slots=True)
class BookSummary:
    book_id: str
    title: str
    author: str = "未知作者"
    description: str = ""
    url: str = ""
    rank: int | None = None
    read_count: int | None = None
    word_count: int | None = None


@dataclass(frozen=True, slots=True)
class RankCategory:
    """番茄榜单页面提供的性别分类。"""

    category_id: str
    name: str


@dataclass(frozen=True, slots=True)
class ChapterRef:
    item_id: str
    title: str
    index: int
    is_locked: bool = False


@dataclass(frozen=True, slots=True)
class ChapterContent:
    item_id: str
    title: str
    content: str


def parse_source(value: str) -> tuple[str, str]:
    """解析书籍页、阅读页 URL 或 book ID，返回 ``(kind, id)``。"""

    text = value.strip()
    if text.isdigit() and 6 <= len(text) <= 32:
        return "page", text
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in {
        _HOST,
        f"www.{_HOST}",
    }:
        raise ValueError("请输入番茄小说书籍页/阅读页链接，或 6-32 位 book ID")
    match = re.fullmatch(r"/(page|reader)/(\d+)/?", parsed.path)
    if match is None:
        raise ValueError(
            "链接必须是 fanqienovel.com/page/<book_id> 或 /reader/<item_id>"
        )
    return match.group(1), match.group(2)


def sanitize_text(value: Any) -> str:
    """将页面字段规整为单行文本。"""

    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return ""
    return re.sub(r"\s+", " ", unescape(str(value))).strip()


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _first_value(value: Any, keys: tuple[str, ...]) -> Any:
    for item in _walk(value):
        for key in keys:
            if key in item and item[key] not in (None, "", [], {}):
                return item[key]
    return None


def _positive_int(value: Any) -> int | None:
    """把阅读页的章节序号/字数转换为正整数。"""

    if isinstance(value, bool):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _nonnegative_int(value: Any) -> int | None:
    """把榜单中的阅读量、字数等字段转换为非负整数。"""

    if isinstance(value, bool):
        return None
    try:
        parsed = int(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _chapter_is_explicitly_free(chapter_data: dict[str, Any] | None) -> bool:
    """仅接受阅读页同时明确标记为免费且非付费出版的章节。"""

    if not isinstance(chapter_data, dict):
        return False
    need_pay = chapter_data.get("needPay")
    paid_markers = (
        str(chapter_data.get("isPaidPublication")).strip().lower(),
        str(chapter_data.get("isPaidStory")).strip().lower(),
    )
    return (
        need_pay in (0, "0", False)
        and paid_markers[0] in {"0", "false", "no"}
        and paid_markers[1] in {"0", "false", "no"}
    )


def _chapter_looks_like_preview(
    content: str,
    chapter_data: dict[str, Any] | None,
) -> bool:
    """根据页面标明的字数识别明显短于正文的公开预览。"""

    if not content:
        return True
    if not isinstance(chapter_data, dict):
        return False
    expected_length = _positive_int(chapter_data.get("chapterWordNumber"))
    if expected_length is None:
        return False
    actual_length = len(re.sub(r"\s+", "", content))
    return actual_length < max(120, expected_length // 2)


def _mobile_helper_request(
    chapter_data: dict[str, Any] | None,
) -> tuple[str, int] | None:
    """从阅读页提取 helper 所需的书籍 ID 和章节顺序。"""

    if not isinstance(chapter_data, dict):
        return None
    book_id = str(chapter_data.get("bookId", "")).strip()
    chapter_order = _positive_int(
        chapter_data.get("realChapterOrder", chapter_data.get("order"))
    )
    if not _BOOK_ID_RE.fullmatch(book_id) or chapter_order is None:
        return None
    return book_id, chapter_order


def _third_party_api_base(value: str) -> str:
    """校验管理员配置的第三方 API 根地址。"""

    base = value.strip().rstrip("/")
    parsed = urlparse(base)
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise FanqieProviderError(
            "番茄第三方 API 地址必须是无账号密码的 HTTP(S) 地址"
        )
    if host in {"localhost", "localhost.localdomain"}:
        raise FanqieProviderError("番茄第三方 API 地址不能指向本机")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise FanqieProviderError("番茄第三方 API 地址不能指向内网地址")
    return base


def _third_party_chapter_response(
    payload: Any,
) -> tuple[str, str, dict[str, Any]]:
    """提取开源第三方 API ``/api/raw_full`` 的章节数据。"""

    if not isinstance(payload, dict):
        raise FanqieProviderError("第三方 API 返回的不是 JSON 对象")
    code = payload.get("code")
    if code not in (None, 200, "200"):
        message = sanitize_text(payload.get("message")) or f"code={code}"
        numeric_code = _positive_int(code)
        if numeric_code == 429 or (
            numeric_code is not None and numeric_code >= 500
        ):
            raise FanqieProviderError(f"第三方 API 服务错误：{message}")
        raise ChapterUnavailable(f"第三方 API 返回错误：{message}")
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        raise ChapterUnavailable("第三方 API 缺少章节数据")
    raw_content = data.get("content")
    if not isinstance(raw_content, str) or not raw_content.strip():
        raise ChapterUnavailable("第三方 API 未返回章节正文")
    title = sanitize_text(data.get("title", data.get("chapter_title", "")))
    return title, raw_content, data


def _validate_mirror_content(
    raw_content: str,
    *,
    expected_word_count: int | None,
    preview: str,
) -> str:
    """Validate content-only mirrors against the official free preview."""

    content = html_to_text(raw_content)
    if contains_pua(content):
        raise ChapterUnavailable("第三方 API 正文仍含未解码字体字符")
    if not content:
        raise ChapterUnavailable("第三方 API 清理后正文为空")
    quote_translation = str.maketrans(
        {
            "“": '"',
            "”": '"',
            "「": '"',
            "」": '"',
            "\u2018": "'",
            "\u2019": "'",
            "『": "'",
            "』": "'",
        }
    )
    normalized_content = re.sub(r"\s+", "", content).translate(quote_translation)
    if expected_word_count is not None and len(normalized_content) < max(
        120, expected_word_count * 4 // 5
    ):
        raise ChapterUnavailable("第三方 API 返回正文明显短于章节字数，拒绝保存为全文")
    normalized_preview = re.sub(r"\s+", "", preview).translate(quote_translation)
    if len(normalized_preview) >= 40 and not normalized_content.startswith(
        normalized_preview[:80]
    ):
        raise ChapterUnavailable("第三方 API 返回内容与阅读页预览不匹配")
    return content


def _strip_matching_leading_title(content: str, expected_title: str) -> str:
    """Remove a duplicated App title with the same explicit chapter subtitle."""

    lines = content.splitlines()
    if len(lines) < 2:
        return content
    actual = sanitize_text(lines[0])
    expected = sanitize_text(expected_title)
    if actual != expected:
        actual_heading = _CHAPTER_HEADING_RE.fullmatch(actual)
        expected_heading = _CHAPTER_HEADING_RE.fullmatch(expected)
        if (
            actual_heading is None
            or expected_heading is None
            or actual_heading.group(1) != expected_heading.group(1)
        ):
            return content
    return "\n".join(lines[1:]).lstrip()


def _item_value(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None


def _book_from_dict(item: dict[str, Any]) -> BookSummary | None:
    raw_id = _item_value(item, "bookId", "book_id", "bookID", "bookIdStr")
    if raw_id is None:
        url = str(_item_value(item, "url", "bookUrl") or "")
        match = re.search(r"/page/(\d+)", url)
        raw_id = match.group(1) if match else None
    if raw_id is None or not _BOOK_ID_RE.fullmatch(str(raw_id)):
        return None
    title = sanitize_text(
        _item_value(
            item,
            "bookName",
            "book_name",
            "bookTitle",
            "book_title",
            "title",
            "name",
        )
    )
    if not title:
        return None
    author = sanitize_text(
        _item_value(item, "author", "authorName", "author_name", "authorNameText")
    )
    description = sanitize_text(
        _item_value(
            item,
            "abstract",
            "book_abstract",
            "description",
            "bookIntro",
            "book_intro",
        )
    )
    return BookSummary(
        book_id=str(raw_id),
        title=title,
        author=author or "未知作者",
        description=description,
        url=f"{BASE_URL}/page/{raw_id}",
        rank=_nonnegative_int(
            _item_value(item, "rank", "currentPos", "rank_pos", "rankNum", "rank_num")
        ),
        read_count=_nonnegative_int(
            _item_value(
                item,
                "read_count",
                "readCount",
                "read_count_num",
                "readNum",
                "read_num",
            )
        ),
        word_count=_nonnegative_int(
            _item_value(
                item,
                "wordNumber",
                "word_number",
                "word_count",
                "wordCount",
            )
        ),
    )


def _dedupe_books(items: Any, limit: int) -> list[BookSummary]:
    if not isinstance(items, list):
        raise TypeError("书籍结果不是列表")
    results: list[BookSummary] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        book = _book_from_dict(item)
        if book is None or book.book_id in seen:
            continue
        results.append(book)
        seen.add(book.book_id)
        if len(results) >= limit:
            break
    return results


def parse_search_response(
    payload: Any,
    limit: int = 5,
    mapping: dict[str, str] | None = None,
) -> list[BookSummary]:
    """解析官方搜索接口返回的书籍列表。"""

    if not isinstance(payload, dict):
        raise TypeError("搜索接口返回的不是 JSON 对象")
    code = payload.get("code")
    if code not in (None, 0, "0", 200, "200"):
        raise ValueError(f"搜索接口返回错误 code={code}")
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        raise TypeError("搜索接口缺少 data 对象")
    key = next(
        (
            name
            for name in ("search_book_data_list", "book_list", "books")
            if name in data
        ),
        None,
    )
    if key is None:
        raise ValueError("搜索接口缺少书籍列表")
    items = data.get(key) or []
    active_mapping = mapping or {}
    if active_mapping and isinstance(items, list):
        items = [
            _decode_book_item(item, active_mapping)
            if isinstance(item, dict)
            else item
            for item in items
        ]
    return _dedupe_books(items, limit)


def _rank_state(state: dict[str, Any]) -> dict[str, Any]:
    rank = state.get("rank", state)
    if not isinstance(rank, dict):
        raise TypeError("榜单状态不是对象")
    return rank


def _rank_book_items(rank_state: dict[str, Any]) -> list[dict[str, Any]]:
    raw_books = rank_state.get("book_list", rank_state.get("bookList"))
    if not isinstance(raw_books, list):
        raise TypeError("榜单页面缺少书籍列表")
    return [item for item in raw_books if isinstance(item, dict)]


def _rank_needs_mapping(items: list[dict[str, Any]]) -> bool:
    return any(
        contains_pua(str(item.get(key, "")))
        for item in items
        for key in _BOOK_TEXT_KEYS
    )


def _require_rank_mapping(
    mapping: dict[str, str],
    *,
    required: bool,
) -> dict[str, str]:
    if required and not mapping:
        raise ValueError("榜单字体映射缺失")
    return mapping


def _validate_rank_text(books: list[BookSummary]) -> None:
    if any(
        contains_pua(value)
        for book in books
        for value in (book.title, book.author, book.description)
    ):
        raise ValueError("榜单文字仍含未解码字体字符")


def parse_rank_categories(page: str) -> dict[str, list[RankCategory]]:
    """解析榜单页面提供的男频/女频分类。"""

    state = extract_initial_state(page)
    raw_categories = _rank_state(state).get("rankCategoryTypeList")
    if not isinstance(raw_categories, dict):
        raise TypeError("榜单页面缺少分类列表")
    categories: dict[str, list[RankCategory]] = {}
    for gender in ("male", "female"):
        raw_items = raw_categories.get(gender)
        if not isinstance(raw_items, list):
            continue
        parsed: list[RankCategory] = []
        seen: set[str] = set()
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            category_id = str(
                _item_value(item, "id", "category_id", "categoryId") or ""
            ).strip()
            name = sanitize_text(_item_value(item, "name", "categoryName"))
            if not category_id or not name or category_id in seen:
                continue
            parsed.append(RankCategory(category_id=category_id, name=name))
            seen.add(category_id)
        if parsed:
            categories[gender] = parsed
    if not categories:
        raise ValueError("榜单页面未找到有效分类")
    return categories


def _decode_book_item(item: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    decoded = dict(item)
    for key in _BOOK_TEXT_KEYS:
        value = decoded.get(key)
        if isinstance(value, str):
            decoded[key] = decrypt_pua(value, mapping)
    return decoded


def _search_payload_needs_mapping(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return False
    for key in ("search_book_data_list", "book_list", "books"):
        items = data.get(key)
        if not isinstance(items, list):
            continue
        return any(
            isinstance(item, dict)
            and any(
                isinstance(item.get(text_key), str)
                and contains_pua(item[text_key])
                for text_key in _SEARCH_VISIBLE_TEXT_KEYS
            )
            for item in items
        )
    return False


def _validate_search_text(books: list[BookSummary]) -> None:
    if any(
        contains_pua(value)
        for book in books
        for value in (book.title, book.author)
    ):
        raise ValueError("搜索文字仍含未解码字体字符")


def parse_rank_results(
    page: str,
    limit: int = 10,
    mapping: dict[str, str] | None = None,
) -> list[BookSummary]:
    """解析榜单页面的书籍列表，保留排名顺序和榜单数据。"""

    state = extract_initial_state(page)
    raw_books = _rank_book_items(_rank_state(state))
    active_mapping = mapping or {}
    items = (
        [_decode_book_item(item, active_mapping) for item in raw_books]
        if active_mapping
        else raw_books
    )
    results = _dedupe_books(items, limit)
    return [
        BookSummary(
            book_id=book.book_id,
            title=book.title,
            author=book.author,
            description=book.description,
            url=book.url,
            rank=index,
            read_count=book.read_count,
            word_count=book.word_count,
        )
        for index, book in enumerate(results, 1)
    ]


def _looks_like_challenge(text: str) -> bool:
    return any(marker in text for marker in ("请完成下列验证", "人机验证", "验证码"))


def parse_book_page(page: str, book_id: str) -> BookSummary:
    """解析书籍页摘要。"""

    state = extract_initial_state(page)
    page_state = state.get("page", state)
    book = _book_from_dict(
        {
            "bookId": _first_value(page_state, ("bookId",)) or book_id,
            "bookName": _first_value(page_state, ("bookName", "title", "name")),
            "author": _first_value(page_state, ("author", "authorName")),
            "abstract": _first_value(
                page_state, ("abstract", "description", "bookIntro")
            ),
        }
    )
    if book is None:
        raise FanqieProviderError("书籍页结构发生变化，未找到书名或 book ID")
    return book


def parse_chapter_list(page: str) -> list[ChapterRef]:
    """解析书籍页目录，保留页面顺序并去重。"""

    state = extract_initial_state(page)
    page_state = state.get("page", state)
    if not isinstance(page_state, dict):
        raise FanqieProviderError("书籍页目录结构发生变化")
    raw_volumes = page_state.get("chapterListWithVolume")
    candidates: list[dict[str, Any]] = []
    if isinstance(raw_volumes, list):
        for volume in raw_volumes:
            if isinstance(volume, list):
                candidates.extend(item for item in volume if isinstance(item, dict))
            elif isinstance(volume, dict):
                nested = volume.get("chapterList", volume.get("chapters", []))
                if isinstance(nested, list):
                    candidates.extend(item for item in nested if isinstance(item, dict))
    if not candidates:
        for item in _walk(page_state):
            if isinstance(item, dict) and (item.get("itemId") or item.get("item_id")):
                candidates.append(item)
    chapters: list[ChapterRef] = []
    seen: set[str] = set()
    for item in candidates:
        item_id = item.get("itemId", item.get("item_id"))
        if item_id is None or str(item_id) in seen:
            continue
        title = sanitize_text(item.get("title", item.get("chapterName", "")))
        if not title:
            title = f"第{len(chapters) + 1}章"
        chapters.append(
            ChapterRef(
                item_id=str(item_id),
                title=title,
                index=len(chapters) + 1,
                is_locked=bool(item.get("isChapterLock", item.get("isLocked", False))),
            )
        )
        seen.add(str(item_id))
    if not chapters:
        raise FanqieProviderError("书籍页结构发生变化，未找到章节目录")
    return chapters


class FanqieProvider:
    """访问番茄公开页面的异步 provider。"""

    def __init__(
        self,
        settings: Config | None = None,
        client: httpx.AsyncClient | None = None,
        browser_search: Callable[..., Awaitable[BrowserSearchSnapshot]] | None = None,
        *,
        app_client: FanqieAppClient | None = None,
        app_client_factory: Callable[[], FanqieAppClient] | None = None,
    ) -> None:
        if app_client is not None and app_client_factory is not None:
            raise ValueError("app_client 与 app_client_factory 不能同时设置")
        self.settings = settings or Config()
        self._client = client
        self._owned_client = client is None
        self._browser_search = browser_search or search_page_snapshot
        self._app_client = app_client
        self._app_client_factory = app_client_factory or (
            lambda: FanqieAppClient(timeout=self.settings.fanqie_request_timeout)
        )
        self._font_cache: dict[str, dict[str, str]] = {}
        self._third_party_open_sources: set[str] = set()

    async def __aenter__(self) -> FanqieProvider:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": self.settings.fanqie_user_agent},
                follow_redirects=False,
            )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        try:
            if self._app_client is not None:
                await self._app_client.aclose()
                self._app_client = None
        finally:
            if self._owned_client and self._client is not None:
                await self._client.aclose()
                self._client = None

    def _page_url(self, path: str) -> str:
        if not path.startswith("/") or "//" in path:
            raise FanqieProviderError("非法页面路径")
        return f"{BASE_URL}{path}"

    @staticmethod
    def _validate_font_url(url: str) -> str:
        parsed = urlparse(url)
        host = parsed.hostname
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise FanqieProviderError("页面字体链接不是安全 HTTPS 地址")
        lowered_host = host.lower().rstrip(".")
        if lowered_host in {"localhost", "localhost.localdomain"}:
            raise FanqieProviderError("页面字体链接指向本地地址")
        try:
            address = ipaddress.ip_address(lowered_host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise FanqieProviderError("页面字体链接指向非公开地址")
        return url

    async def _request(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        allow_third_party: bool = False,
        timeout: float | None = None,
        retries: int | None = None,
    ) -> httpx.Response:
        if self._client is None:
            raise RuntimeError("FanqieProvider 必须在 async with 中使用")
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if host not in {_HOST, f"www.{_HOST}"}:
            if allow_third_party:
                _third_party_api_base(url.rsplit("/api/", 1)[0])
            else:
                self._validate_font_url(url)
                if not parsed.path.lower().endswith(".woff2"):
                    raise FanqieProviderError(
                        "provider 只允许请求页面返回的 woff2 字体"
                    )
        last_error: Exception | None = None
        total = (
            self.settings.fanqie_request_retries if retries is None else retries
        ) + 1
        request_timeout = (
            self.settings.fanqie_request_timeout
            if timeout is None
            else timeout
        )
        for attempt in range(total):
            started = perf_counter()
            logger.debug(
                "fanqie request start: url=%s params=%s attempt=%d/%d",
                url,
                params or {},
                attempt + 1,
                total,
            )
            try:
                response = await self._client.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=request_timeout,
                )
                logger.debug(
                    "fanqie request response: url=%s status=%d bytes=%d "
                    "elapsed_ms=%.1f attempt=%d/%d",
                    url,
                    response.status_code,
                    len(response.content),
                    (perf_counter() - started) * 1000,
                    attempt + 1,
                    total,
                )
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = FanqieProviderError(f"HTTP {response.status_code}")
                    if attempt + 1 < total:
                        retry_after = response.headers.get("Retry-After", "")
                        try:
                            delay = min(float(retry_after), 10.0)
                        except ValueError:
                            delay = min(2**attempt, 5.0)
                        logger.debug(
                            "fanqie request retry scheduled: url=%s status=%d "
                            "delay=%.2fs next_attempt=%d/%d",
                            url,
                            response.status_code,
                            delay,
                            attempt + 2,
                            total,
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise last_error
                if response.status_code >= 400:
                    logger.warning(
                        "fanqie request rejected: url=%s status=%d elapsed_ms=%.1f",
                        url,
                        response.status_code,
                        (perf_counter() - started) * 1000,
                    )
                    raise FanqieProviderError(f"HTTP {response.status_code}")
                return response
            except httpx.RequestError as exc:
                last_error = exc
                logger.debug(
                    "fanqie request network error: url=%s error_type=%s "
                    "elapsed_ms=%.1f attempt=%d/%d",
                    url,
                    type(exc).__name__,
                    (perf_counter() - started) * 1000,
                    attempt + 1,
                    total,
                    exc_info=True,
                )
                if attempt + 1 < total:
                    logger.debug(
                        "fanqie request retry scheduled after network error: "
                        "url=%s delay=%.2fs next_attempt=%d/%d",
                        url,
                        min(2**attempt, 5.0),
                        attempt + 2,
                        total,
                    )
                    await asyncio.sleep(min(2**attempt, 5.0))
                    continue
                raise FanqieProviderError("网络请求失败") from exc
        raise FanqieProviderError("网络请求失败") from last_error

    async def _third_party_chapter(
        self,
        *,
        item_id: str,
        expected_title: str,
        expected_word_count: int | None,
    ) -> ChapterContent:
        """通过开源项目采用的第三方 raw_full 接口读取免费章节全文。"""

        base = _third_party_api_base(self.settings.fanqie_third_party_api_base)
        response = await self._request(
            f"{base}/api/raw_full",
            params={"item_id": item_id},
            allow_third_party=True,
            timeout=self.settings.fanqie_third_party_api_timeout,
            retries=self.settings.fanqie_third_party_api_retries,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise FanqieProviderError("第三方 API 返回的不是有效 JSON") from exc
        title, raw_content, metadata = _third_party_chapter_response(payload)
        if not title:
            raise ChapterUnavailable("第三方 API 缺少章节标题")
        if sanitize_text(title) != sanitize_text(expected_title):
            raise ChapterUnavailable(
                f"第三方 API 章节标题不匹配：返回 {title!r}，期望 {expected_title!r}"
            )
        content = html_to_text(raw_content)
        if contains_pua(content):
            raise ChapterUnavailable("第三方 API 正文仍含未解码字体字符")
        if not content:
            raise ChapterUnavailable("第三方 API 清理后正文为空")
        paragraphs = _positive_int(metadata.get("paragraphs_num"))
        free_paragraphs = _positive_int(metadata.get("free_para_nums"))
        if (
            paragraphs is not None
            and free_paragraphs is not None
            and free_paragraphs < paragraphs
        ):
            raise ChapterUnavailable("第三方 API 返回的章节并非全部免费段落")
        api_word_count = _positive_int(
            metadata.get("chapter_word_number", metadata.get("word_number"))
        )
        expected = expected_word_count or api_word_count
        non_whitespace_chars = len(re.sub(r"\s+", "", content))
        if expected is not None and non_whitespace_chars < max(120, expected * 4 // 5):
            raise ChapterUnavailable(
                "第三方 API 返回正文明显短于章节字数，拒绝保存为全文"
            )
        logger.info(
            "fanqie third-party chapter complete: item_id=%s title=%r "
            "content_chars=%d non_whitespace_chars=%d paragraphs=%s "
            "free_paragraphs=%s",
            item_id,
            title[:80],
            len(content),
            non_whitespace_chars,
            paragraphs,
            free_paragraphs,
        )
        return ChapterContent(
            item_id=item_id,
            title=title,
            content=content,
        )

    async def _app_protocol_chapter(
        self,
        *,
        book_id: str,
        item_id: str,
        expected_title: str,
        expected_word_count: int | None,
        preview: str,
    ) -> ChapterContent:
        """Read a free chapter through the fixed anonymous App protocol."""

        if self._app_client is None:
            try:
                self._app_client = self._app_client_factory()
            except FanqieAppError:
                self._app_client = None
            if self._app_client is None:
                raise ChapterFetchTransientError(
                    "番茄 App 协议客户端初始化失败"
                ) from None
        raw_content = ""
        failure: Literal["unavailable", "transient"] | None = None
        try:
            raw_content = await self._app_client.fetch_chapter(
                item_id,
                book_id=book_id,
            )
        except AppChapterUnavailable:
            failure = "unavailable"
        except (AppProtocolTransientError, FanqieAppError):
            failure = "transient"
        if failure == "unavailable":
            raise ChapterUnavailable("番茄 App 协议未返回可用正文") from None
        if failure == "transient":
            raise ChapterFetchTransientError(
                "番茄 App 协议暂时不可用"
            ) from None
        raw_content = _strip_matching_leading_title(raw_content, expected_title)
        content = _validate_mirror_content(
            raw_content,
            expected_word_count=expected_word_count,
            preview=preview,
        )
        logger.info(
            "fanqie app protocol chapter complete: item_id=%s content_chars=%d",
            item_id,
            len(content),
        )
        return ChapterContent(
            item_id=item_id,
            title=expected_title,
            content=content,
        )

    async def _third_party_proxy_chapter(
        self,
        *,
        item_id: str,
        expected_title: str,
        expected_word_count: int | None,
        preview: str,
    ) -> ChapterContent:
        """Read App-decrypted free text from the public fanqietc proxy."""

        base = _third_party_api_base(self.settings.fanqie_third_party_fallback_base)
        token = self.settings.fanqie_third_party_fallback_token.strip()
        if not token:
            raise FanqieProviderError("番茄全文回退代理未配置 API token")
        response = await self._request(
            f"{base}/proxy",
            params={
                "api": "default",
                "action": "content",
                "item_id": item_id,
            },
            headers={
                "X-API-Token": token,
                "Origin": "https://fanqietc.com",
                "Referer": "https://fanqietc.com/",
            },
            allow_third_party=True,
            timeout=self.settings.fanqie_third_party_api_timeout,
            retries=self.settings.fanqie_third_party_api_retries,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise FanqieProviderError(
                "番茄全文回退代理返回的不是有效 JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise FanqieProviderError("番茄全文回退代理返回结构异常")
        code = payload.get("code")
        if code not in (None, 200, "200"):
            message = sanitize_text(
                payload.get("message", payload.get("msg"))
            ) or f"code={code}"
            numeric_code = _positive_int(code)
            if numeric_code == 429 or (
                numeric_code is not None and numeric_code >= 500
            ):
                raise FanqieProviderError(f"番茄全文回退代理服务错误：{message}")
            raise ChapterUnavailable(f"番茄全文回退代理返回错误：{message}")
        data = payload.get("data", payload)
        if not isinstance(data, dict):
            raise ChapterUnavailable("番茄全文回退代理缺少章节数据")
        raw_content = data.get("content")
        if not isinstance(raw_content, str) or not raw_content.strip():
            raise ChapterUnavailable("番茄全文回退代理未返回章节正文")
        content = _validate_mirror_content(
            raw_content,
            expected_word_count=expected_word_count,
            preview=preview,
        )
        returned_title = sanitize_text(
            data.get("title", data.get("chapter_title", expected_title))
        )
        if returned_title and returned_title != sanitize_text(expected_title):
            raise ChapterUnavailable(
                f"番茄全文回退代理章节标题不匹配：返回 {returned_title!r}，"
                f"期望 {expected_title!r}"
            )
        logger.info(
            "fanqie fallback chapter complete: item_id=%s content_chars=%d",
            item_id,
            len(content),
        )
        return ChapterContent(
            item_id=item_id,
            title=expected_title,
            content=content,
        )

    async def _page(self, path: str) -> str:
        response = await self._request(self._page_url(path))
        return response.text

    async def search(
        self,
        keyword: str,
        *,
        order: SearchOrder = "related",
    ) -> list[BookSummary]:
        """通过官方公开搜索接口模糊搜索书名或作者。"""

        keyword = keyword.strip()
        if not keyword:
            return []
        query_type = _SEARCH_QUERY_TYPES.get(order)
        if query_type is None:
            raise ValueError("不支持的番茄搜索排序")
        logger.debug(
            "fanqie search start: keyword=%r order=%s limit=%d",
            keyword[:80],
            order,
            self.settings.fanqie_search_limit,
        )
        try:
            snapshot = await self._browser_search(
                keyword,
                query_type=query_type,
                timeout=self.settings.fanqie_browser_timeout,
                headless=self.settings.fanqie_browser_headless,
                profile_dir=self.settings.fanqie_browser_profile_dir,
            )
        except BrowserSearchError as exc:
            raise FanqieServiceUnavailable(str(exc)) from exc
        try:
            page = snapshot.page
            payload = snapshot.payload
            needs_mapping = _search_payload_needs_mapping(payload)
            mapping: dict[str, str] = {}
            if needs_mapping:
                if not page:
                    raise ValueError("搜索页面字体映射缺失")
                state = extract_initial_state(page)
                mapping = await self._font_mapping(page, state)
                if not mapping:
                    raise ValueError("搜索页面字体映射缺失")
            results = parse_search_response(
                payload,
                self.settings.fanqie_search_limit,
                mapping=mapping,
            )
            if needs_mapping:
                _validate_search_text(results)
        except (FanqieProviderError, ValueError, TypeError) as exc:
            raise FanqieServiceUnavailable("番茄搜索接口返回结构异常") from exc
        logger.debug(
            "fanqie search complete: keyword=%r order=%s result_count=%d "
            "book_ids=%s",
            keyword[:80],
            order,
            len(results),
            [book.book_id for book in results],
        )
        return results

    async def list_rank_categories(self) -> dict[str, list[RankCategory]]:
        """取得官方榜单页面提供的男频/女频分类。"""

        try:
            response = await self._request(self._page_url("/rank"))
        except FanqieProviderError as exc:
            raise FanqieServiceUnavailable("番茄榜单暂时不可用") from exc
        if not response.content or _looks_like_challenge(response.text):
            raise FanqieServiceUnavailable("番茄榜单返回了空响应或验证页面")
        try:
            categories = parse_rank_categories(response.text)
        except (ValueError, TypeError) as exc:
            raise FanqieServiceUnavailable("番茄榜单分类结构异常") from exc
        logger.debug(
            "fanqie rank categories complete: genders=%s counts=%s",
            sorted(categories),
            {gender: len(items) for gender, items in categories.items()},
        )
        return categories

    async def list_rank_books(
        self,
        *,
        gender: RankGender,
        rank_type: RankType,
        category_id: str,
        limit: int | None = None,
    ) -> list[BookSummary]:
        """取得指定性别、榜单类型和分类的公开榜单。"""

        gender_code = _RANK_GENDER_CODES.get(gender)
        rank_type_code = _RANK_TYPE_CODES.get(rank_type)
        if gender_code is None or rank_type_code is None:
            raise ValueError("不支持的番茄榜单类型")
        if not re.fullmatch(r"\d{1,12}", category_id):
            raise ValueError("非法番茄榜单分类")
        rank_limit = self.settings.fanqie_rank_limit if limit is None else limit
        if not 1 <= rank_limit <= 10:
            raise ValueError("番茄榜单最多读取 10 本书")
        path = f"/rank/{gender_code}_{rank_type_code}_{category_id}"
        logger.debug(
            "fanqie rank start: gender=%s rank_type=%s category_id=%s limit=%d",
            gender,
            rank_type,
            category_id,
            rank_limit,
        )
        try:
            response = await self._request(self._page_url(path))
        except FanqieProviderError as exc:
            raise FanqieServiceUnavailable("番茄榜单暂时不可用") from exc
        page = response.text
        if not response.content or _looks_like_challenge(page):
            raise FanqieServiceUnavailable("番茄榜单返回了空响应或验证页面")
        try:
            state = extract_initial_state(page)
            rank_state = _rank_state(state)
            raw_books = _rank_book_items(rank_state)
            needs_mapping = _rank_needs_mapping(raw_books)
            mapping = await self._font_mapping(page, state) if needs_mapping else {}
            active_mapping = _require_rank_mapping(mapping, required=needs_mapping)
            books = parse_rank_results(page, rank_limit, mapping=active_mapping)
            _validate_rank_text(books)
        except (FanqieProviderError, ValueError, TypeError) as exc:
            raise FanqieServiceUnavailable("番茄榜单结构或字体解析失败") from exc
        logger.debug(
            "fanqie rank complete: gender=%s rank_type=%s category_id=%s "
            "result_count=%d book_ids=%s",
            gender,
            rank_type,
            category_id,
            len(books),
            [book.book_id for book in books],
        )
        return books

    async def resolve_book_reference(self, value: str) -> BookSummary:
        """解析书籍页、阅读页 URL 或 ID 并取得书籍摘要。"""

        kind, value_id = parse_source(value)
        logger.debug(
            "fanqie resolve book reference: kind=%s value_id=%s", kind, value_id
        )
        if kind == "page":
            return await self.get_book(value_id)
        page = await self._page(f"/reader/{value_id}")
        state = extract_initial_state(page)
        chapter = _first_value(state, ("chapterData",))
        if not isinstance(chapter, dict) or not chapter.get("bookId"):
            raise FanqieProviderError("阅读页缺少所属书籍信息")
        logger.debug(
            "fanqie reader reference resolved: item_id=%s book_id=%s",
            value_id,
            chapter["bookId"],
        )
        return await self.get_book(str(chapter["bookId"]))

    async def get_book(self, book_id: str) -> BookSummary:
        """取得书籍页摘要。"""

        if not _BOOK_ID_RE.fullmatch(book_id):
            raise ValueError("非法 book ID")
        book = parse_book_page(await self._page(f"/page/{book_id}"), book_id)
        logger.debug(
            "fanqie book parsed: book_id=%s title=%r author=%r",
            book.book_id,
            book.title[:80],
            book.author[:80],
        )
        return book

    async def list_chapters(self, book_id: str) -> list[ChapterRef]:
        """取得书籍目录。"""

        if not _BOOK_ID_RE.fullmatch(book_id):
            raise ValueError("非法 book ID")
        chapters = parse_chapter_list(await self._page(f"/page/{book_id}"))
        logger.debug(
            "fanqie chapter list parsed: book_id=%s chapter_count=%d locked_count=%d",
            book_id,
            len(chapters),
            sum(chapter.is_locked for chapter in chapters),
        )
        return chapters

    async def _font_mapping(self, page: str, state: dict[str, Any]) -> dict[str, str]:
        css_parts = [page]
        for item in _walk(state):
            if isinstance(item, str) and ".woff2" in item:
                css_parts.append(item)
        match = _FONT_URL_RE.search("\n".join(css_parts))
        if match is None:
            logger.debug("fanqie page font mapping absent")
            return {}
        url = self._validate_font_url(match.group(1))
        if url in self._font_cache:
            logger.debug(
                "fanqie page font mapping cache hit: url=%s entries=%d",
                url,
                len(self._font_cache[url]),
            )
            return self._font_cache[url]
        response = await self._request(url)
        try:
            from io import BytesIO

            from fontTools.ttLib import TTFont

            font = TTFont(BytesIO(response.content))
            cmap = font.getBestCmap() or {}
        except Exception as exc:
            raise ChapterUnavailable("章节字体格式异常，无法公开解码") from exc
        mapping: dict[str, str] = {}
        for codepoint, raw_glyph_name in cmap.items():
            glyph_name = str(raw_glyph_name)
            decoded = font_glyph_signature_to_text(font, glyph_name)
            if decoded is not None:
                mapping[chr(codepoint)] = decoded
        self._font_cache[url] = mapping
        logger.debug(
            "fanqie page font mapping parsed: url=%s entries=%d",
            url,
            len(mapping),
        )
        return mapping

    async def fetch_chapter(
        self,
        item_id: str,
        *,
        book_id: str = "",
    ) -> ChapterContent:
        """读取页面正文；免费预览按 App、镜像、helper 的顺序补全。"""

        if not re.fullmatch(r"\d{6,32}", item_id):
            raise ValueError("非法章节 ID")
        if book_id and not re.fullmatch(r"\d{6,32}", book_id):
            raise ValueError("非法书籍 ID")
        logger.debug("fanqie chapter fetch start: item_id=%s", item_id)
        page = await self._page(f"/reader/{item_id}")
        try:
            state = extract_initial_state(page)
        except ValueError:
            state = {}
        chapter_data = _first_value(state, ("chapterData",))
        if isinstance(chapter_data, dict):
            title = sanitize_text(chapter_data.get("title", "")) or f"章节 {item_id}"
            raw_content = chapter_data.get("content") or chapter_data.get(
                "chapterContent", ""
            )
            content = html_to_text(raw_content) if isinstance(raw_content, str) else ""
        else:
            title = f"章节 {item_id}"
            content = ""
        if not content:
            content = extract_reader_content(page)
        if not content:
            raise ChapterUnavailable("阅读页未返回公开正文")
        if contains_pua(content):
            content = decrypt_reader_pua(content)
            title = decrypt_reader_pua(title)
            if contains_pua(content):
                mapping = await self._font_mapping(page, state)
                content = decrypt_pua(content, mapping)
                title = decrypt_pua(title, mapping)
            if contains_pua(content):
                raise ChapterUnavailable("章节字体映射缺失，未尝试绕过访问控制")
        is_preview = _chapter_looks_like_preview(content, chapter_data)
        if is_preview:
            if not _chapter_is_explicitly_free(chapter_data):
                raise ChapterUnavailable(
                    "该章节未被阅读页明确标记为免费，未调用第三方全文接口"
                )
            expected_word_count = _positive_int(
                chapter_data.get("chapterWordNumber")
                if isinstance(chapter_data, dict)
                else None
            )
            primary_base = self.settings.fanqie_third_party_api_base.strip()
            fallback_base = self.settings.fanqie_third_party_fallback_base.strip()
            source_errors: list[FanqieProviderError] = []
            transient_error: ChapterFetchTransientError | None = None
            if self.settings.fanqie_app_protocol_enabled and book_id:
                try:
                    return await self._app_protocol_chapter(
                        book_id=book_id,
                        item_id=item_id,
                        expected_title=title,
                        expected_word_count=expected_word_count,
                        preview=content,
                    )
                except ChapterUnavailable as exc:
                    source_errors.append(exc)
                    logger.warning(
                        "fanqie app protocol chapter rejected: item_id=%s error=%s",
                        item_id,
                        exc,
                    )
                except ChapterFetchTransientError as exc:
                    transient_error = exc
                    logger.warning(
                        "fanqie app protocol chapter unavailable: item_id=%s "
                        "error=%s",
                        item_id,
                        exc,
                    )
            third_party_sources: list[tuple[str, Any]] = []
            configured_sources = {"raw_full"} if primary_base else set()
            if primary_base and "raw_full" not in self._third_party_open_sources:
                third_party_sources.append(
                    (
                        "raw_full",
                        lambda: self._third_party_chapter(
                            item_id=item_id,
                            expected_title=title,
                            expected_word_count=expected_word_count,
                        ),
                    )
                )
            # Clearing the primary base disables both remote mirrors. The App
            # protocol has its own fixed boolean switch above.
            fallback_configured = (
                primary_base
                and fallback_base
                and self.settings.fanqie_third_party_fallback_token.strip()
            )
            if fallback_configured:
                configured_sources.add("fanqietc")
            if (
                fallback_configured
                and "fanqietc" not in self._third_party_open_sources
            ):
                third_party_sources.append(
                    (
                        "fanqietc",
                        lambda: self._third_party_proxy_chapter(
                            item_id=item_id,
                            expected_title=title,
                            expected_word_count=expected_word_count,
                            preview=content,
                        ),
                    )
                )
            open_sources = configured_sources & self._third_party_open_sources
            if open_sources:
                transient_error = ChapterFetchTransientError(
                    "番茄全文服务暂时不可用，已熔断节点："
                    + ", ".join(sorted(open_sources))
                )
            if not third_party_sources and primary_base:
                transient_error = ChapterFetchTransientError(
                    "番茄全文服务暂时不可用，所有已配置节点均已熔断"
                )
            for source_name, source_call in third_party_sources:
                try:
                    return await source_call()
                except ChapterUnavailable as exc:
                    source_errors.append(exc)
                    logger.warning(
                        "fanqie third-party chapter rejected: item_id=%s "
                        "source=%s error=%s",
                        item_id,
                        source_name,
                        exc,
                    )
                except FanqieProviderError as exc:
                    source_errors.append(exc)
                    self._third_party_open_sources.add(source_name)
                    transient_error = ChapterFetchTransientError(
                        f"番茄全文源 {source_name} 暂时不可用：{exc}"
                    )
                    logger.warning(
                        "fanqie third-party chapter unavailable: item_id=%s "
                        "source=%s error=%s",
                        item_id,
                        source_name,
                        exc,
                    )
            third_party_error = source_errors[-1] if source_errors else None
            if mobile_helper_configured(self.settings):
                helper_request = _mobile_helper_request(chapter_data)
                if helper_request is None:
                    if transient_error is not None:
                        raise transient_error from third_party_error
                    raise ChapterUnavailable(
                        "第三方全文接口失败，且阅读页缺少本机 helper 所需的 "
                        "书籍或章节序号"
                    ) from third_party_error
                book_id, chapter_order = helper_request
                try:
                    mobile_chapter = await fetch_mobile_chapter(
                        self.settings,
                        book_id=book_id,
                        chapter_order=chapter_order,
                        expected_title=title,
                    )
                except MobileHelperError as exc:
                    if transient_error is not None:
                        raise ChapterFetchTransientError(
                            f"第三方全文服务暂时不可用，且本机 helper 失败：{exc}"
                        ) from transient_error
                    raise ChapterUnavailable(str(exc)) from exc
                logger.debug(
                    "fanqie chapter mobile helper complete: item_id=%s book_id=%s "
                    "chapter_order=%d title=%r content_chars=%d",
                    item_id,
                    book_id,
                    chapter_order,
                    mobile_chapter.title[:80],
                    len(mobile_chapter.content),
                )
                return ChapterContent(
                    item_id=item_id,
                    title=mobile_chapter.title,
                    content=mobile_chapter.content,
                )
            if transient_error is not None:
                raise transient_error from third_party_error
            raise ChapterUnavailable(
                "阅读页仅返回预览；第三方全文接口不可用"
            ) from third_party_error
        logger.debug(
            "fanqie chapter fetch complete: item_id=%s title=%r "
            "content_chars=%d pua=%s",
            item_id,
            title[:80],
            len(content),
            contains_pua(content),
        )
        return ChapterContent(item_id=item_id, title=title, content=content)


__all__ = [
    "BookSummary",
    "ChapterContent",
    "ChapterFetchTransientError",
    "ChapterRef",
    "ChapterUnavailable",
    "FanqieProvider",
    "FanqieProviderError",
    "FanqieServiceUnavailable",
    "RankCategory",
    "parse_book_page",
    "parse_chapter_list",
    "parse_rank_categories",
    "parse_rank_results",
    "parse_search_response",
    "parse_source",
]
