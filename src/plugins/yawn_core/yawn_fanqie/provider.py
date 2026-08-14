"""番茄免费小说公开页面 provider。

provider 只负责 HTTP、页面解析和公开字体解码，不依赖 ORM、任务队列或
NoneBot 权限。页面结构变化时可独立替换本模块。
"""

# provider 的诊断错误需要携带页面/HTTP 上下文，且解析分支较多。
# ruff: noqa: N818, PLR2004, TRY003, TRY300, C901, PLR0912, PLR0915, PERF401, PYI034

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
from dataclasses import dataclass
from html import unescape
from time import perf_counter
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from .config import Config
from .decoder import (
    contains_pua,
    decrypt_pua,
    extract_initial_state,
    extract_reader_content,
    font_glyph_to_text,
    html_to_text,
)
from .mobile_helper import (
    MobileHelperError,
    fetch_mobile_chapter,
    mobile_helper_configured,
)

BASE_URL = "https://fanqienovel.com"
_HOST = "fanqienovel.com"
_BOOK_ID_RE = re.compile(r"(?<!\d)(\d{6,32})(?!\d)")
_FONT_URL_RE = re.compile(
    r"url\(\s*['\"]?(https?://[^)'\"\s]+?\.woff2(?:\?[^)'\"\s]*)?)['\"]?\s*\)",
    re.IGNORECASE,
)
logger = logging.getLogger(__name__)


class FanqieProviderError(RuntimeError):
    """公开页面不可用、结构改变或请求失败。"""


class ChapterUnavailable(FanqieProviderError):
    """章节锁定、正文为空或无法公开解码。"""


@dataclass(frozen=True, slots=True)
class BookSummary:
    book_id: str
    title: str
    author: str = "未知作者"
    description: str = ""
    url: str = ""


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
        raise ChapterUnavailable("第三方 API 返回的不是 JSON 对象")
    code = payload.get("code")
    if code not in (None, 200, "200"):
        message = sanitize_text(payload.get("message")) or f"code={code}"
        raise ChapterUnavailable(f"第三方 API 返回错误：{message}")
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        raise ChapterUnavailable("第三方 API 缺少章节数据")
    raw_content = data.get("content")
    if not isinstance(raw_content, str) or not raw_content.strip():
        raise ChapterUnavailable("第三方 API 未返回章节正文")
    title = sanitize_text(data.get("title", data.get("chapter_title", "")))
    return title, raw_content, data


def _book_from_dict(item: dict[str, Any]) -> BookSummary | None:
    raw_id = item.get("bookId", item.get("book_id"))
    if raw_id is None:
        url = str(item.get("url", item.get("bookUrl", "")))
        match = re.search(r"/page/(\d+)", url)
        raw_id = match.group(1) if match else None
    if raw_id is None or not _BOOK_ID_RE.fullmatch(str(raw_id)):
        return None
    title = sanitize_text(item.get("bookName", item.get("title", item.get("name", ""))))
    if not title:
        return None
    author = sanitize_text(item.get("author", item.get("authorName", "未知作者")))
    description = sanitize_text(
        item.get("abstract", item.get("description", item.get("bookIntro", "")))
    )
    return BookSummary(
        book_id=str(raw_id),
        title=title,
        author=author or "未知作者",
        description=description,
        url=f"{BASE_URL}/page/{raw_id}",
    )


def parse_search_results(page: str, limit: int = 5) -> list[BookSummary]:
    """从搜索 HTML/初始状态提取去重后的书籍摘要。"""

    results: list[BookSummary] = []
    seen: set[str] = set()
    try:
        state: Any = extract_initial_state(page)
    except ValueError:
        state = None
    if state is not None:
        for item in _walk(state):
            book = _book_from_dict(item)
            if book and book.book_id not in seen:
                results.append(book)
                seen.add(book.book_id)
                if len(results) >= limit:
                    logger.debug(
                        "fanqie parse search results: result_count=%d limit=%d",
                        len(results),
                        limit,
                    )
                    return results

    pattern = re.compile(
        r'<a[^>]+href=["\']/page/(\d+)["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL
    )
    for match in pattern.finditer(page):
        book_id = match.group(1)
        title = html_to_text(match.group(2))
        if not title or book_id in seen:
            continue
        results.append(
            BookSummary(
                book_id=book_id,
                title=title,
                url=f"{BASE_URL}/page/{book_id}",
            )
        )
        seen.add(book_id)
        if len(results) >= limit:
            break
    logger.debug(
        "fanqie parse search results: result_count=%d limit=%d", len(results), limit
    )
    return results


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
    ) -> None:
        self.settings = settings or Config()
        self._client = client
        self._owned_client = client is None
        self._font_cache: dict[str, dict[str, str]] = {}

    async def __aenter__(self) -> FanqieProvider:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": self.settings.fanqie_user_agent},
                follow_redirects=False,
            )
        return self

    async def __aexit__(self, *_exc: object) -> None:
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
            raise ChapterUnavailable("第三方 API 返回的不是有效 JSON") from exc
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

    async def _page(self, path: str) -> str:
        response = await self._request(self._page_url(path))
        return response.text

    async def search(self, keyword: str) -> list[BookSummary]:
        """搜索最多五本公开书籍。"""

        keyword = keyword.strip()
        if not keyword:
            return []
        search_path = f"/search/{quote(keyword, safe='')}"
        logger.debug(
            "fanqie search start: keyword=%r path=%s limit=%d",
            keyword[:80],
            search_path,
            self.settings.fanqie_search_limit,
        )
        response = await self._request(
            self._page_url(search_path),
        )
        page = response.text
        results = parse_search_results(page, self.settings.fanqie_search_limit)
        logger.debug(
            "fanqie search complete: keyword=%r result_count=%d book_ids=%s "
            "page_bytes=%d",
            keyword[:80],
            len(results),
            [book.book_id for book in results],
            len(response.content),
        )
        return results

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
            logger.debug("fanqie chapter font mapping absent")
            return {}
        url = self._validate_font_url(match.group(1))
        if url in self._font_cache:
            logger.debug(
                "fanqie chapter font mapping cache hit: url=%s entries=%d",
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
        mapping = {
            chr(codepoint): decoded
            for codepoint, glyph_name in cmap.items()
            if (decoded := font_glyph_to_text(str(glyph_name))) is not None
        }
        self._font_cache[url] = mapping
        logger.debug(
            "fanqie chapter font mapping parsed: url=%s entries=%d",
            url,
            len(mapping),
        )
        return mapping

    async def fetch_chapter(self, item_id: str) -> ChapterContent:
        """读取页面正文；免费预览优先由第三方接口补全。"""

        if not re.fullmatch(r"\d{6,32}", item_id):
            raise ValueError("非法章节 ID")
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
            third_party_error: FanqieProviderError | None = None
            try:
                return await self._third_party_chapter(
                    item_id=item_id,
                    expected_title=title,
                    expected_word_count=expected_word_count,
                )
            except FanqieProviderError as exc:
                third_party_error = exc
                logger.warning(
                    "fanqie third-party chapter unavailable: item_id=%s error=%s",
                    item_id,
                    exc,
                )
            if mobile_helper_configured(self.settings):
                helper_request = _mobile_helper_request(chapter_data)
                if helper_request is None:
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
            raise ChapterUnavailable(
                "阅读页仅返回预览；第三方全文接口不可用"
            ) from third_party_error
        if not content:
            raise ChapterUnavailable("阅读页未返回公开正文")
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
    "ChapterRef",
    "ChapterUnavailable",
    "FanqieProvider",
    "FanqieProviderError",
    "parse_book_page",
    "parse_chapter_list",
    "parse_search_results",
    "parse_source",
]
