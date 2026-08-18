from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import nonebot
import pytest
from nonebot.adapters.onebot.v11 import Message
from nonebot.exception import RejectedException
from typing_extensions import Self

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_RETRY_RESPONSES = 3
_OK_STATUS = 200
_EXPECTED_CALLS = 4
_TEST_BOOK_RECORD_ID = 7
_TEST_JOB_ID = 42
_DEFAULT_FANQIE_REQUEST_DELAY = 0.5
_DEFAULT_FANQIE_RANK_LIMIT = 10
_MIN_FANQIE_REQUEST_DELAY = 0.2
_THIRD_PARTY_REQUEST_COUNT = 2
_MIN_FULL_CONTENT_CHARS = 120
_SEARCH_READ_COUNT = 12
_SEARCH_WORD_COUNT = 345
_API_READ_COUNT = 1234
_API_WORD_COUNT = 5678
_RANK_READ_COUNT = 99
_FONT_SIGNATURE_COUNT = 362
_RAW_FULL_502_ERROR = "raw_full HTTP 502"


class _CommitExpiringSession:
    def __init__(self, book_type: type[object], job_type: type[object]) -> None:
        self.book_type = book_type
        self.job_type = job_type
        self.items: list[Any] = []
        self.job: Any = None
        self.scalar_calls = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def scalar(self, _statement: object) -> Any:
        self.scalar_calls += 1
        return 0 if self.scalar_calls == 1 else None

    def add(self, item: Any) -> None:
        self.items.append(item)
        if isinstance(item, self.job_type):
            self.job = item

    async def flush(self) -> None:
        for item in self.items:
            item_any = cast("Any", item)
            if isinstance(item, self.book_type) and item_any.id is None:
                item_any.id = _TEST_BOOK_RECORD_ID
            if isinstance(item, self.job_type) and item_any.id is None:
                item_any.id = _TEST_JOB_ID

    async def commit(self) -> None:
        assert self.job is not None
        cast("Any", self.job).__dict__.pop("id", None)


class _FakeResult:
    def __init__(self, values: list[Any]) -> None:
        self.values = values

    def scalars(self) -> Self:
        return self

    def all(self) -> list[Any]:
        return self.values


class _WorkerSession:
    def __init__(
        self,
        *,
        job: Any,
        book: Any = None,
        chapter_rows: list[Any] | None = None,
        chapter_row: Any = None,
        expire_on_commit: bool = False,
    ) -> None:
        self.job = job
        self.book = book
        self.chapter_rows = chapter_rows or []
        self.chapter_row = chapter_row
        self.expire_on_commit = expire_on_commit
        self.commits = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get(self, model: type[object], _key: int) -> Any:
        if model.__name__ == "FanqieBook":
            return self.book
        if model.__name__ == "FanqieJobChapter":
            return self.chapter_row
        return self.job

    async def execute(self, _statement: object) -> _FakeResult:
        return _FakeResult(self.chapter_rows)

    async def scalar(self, _statement: object) -> int:
        return 1

    async def commit(self) -> None:
        self.commits += 1
        if not self.expire_on_commit:
            return
        if self.book is not None:
            for field in ("title", "author", "book_id"):
                self.book.__dict__.pop(field, None)
        for item in [self.job, *self.chapter_rows, self.chapter_row]:
            if item is not None:
                for field in (
                    "id",
                    "chapter_index",
                    "completed_chapters",
                    "total_chapters",
                ):
                    item.__dict__.pop(field, None)


@pytest.fixture(scope="module")
def fanqie_modules() -> SimpleNamespace:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()
    if nonebot.get_plugin("yawn_core") is None:
        nonebot.load_from_toml("pyproject.toml")
    return SimpleNamespace(
        browser_search=importlib.import_module(
            "src.plugins.yawn_core.yawn_fanqie.browser_search"
        ),
        decoder=importlib.import_module("src.plugins.yawn_core.yawn_fanqie.decoder"),
        provider=importlib.import_module("src.plugins.yawn_core.yawn_fanqie.provider"),
        config=importlib.import_module("src.plugins.yawn_core.yawn_fanqie.config"),
        mobile_helper=importlib.import_module(
            "src.plugins.yawn_core.yawn_fanqie.mobile_helper"
        ),
        commands=importlib.import_module("src.plugins.yawn_core.yawn_fanqie.commands"),
        state=importlib.import_module("src.plugins.yawn_core.yawn_fanqie.state"),
        core=importlib.import_module("src.plugins.yawn_core"),
    )


def test_source_and_page_url_parsing(fanqie_modules: SimpleNamespace) -> None:
    parse_source = fanqie_modules.provider.parse_source

    assert parse_source("123456") == ("page", "123456")
    assert parse_source("https://fanqienovel.com/page/123456?from=test") == (
        "page",
        "123456",
    )
    assert parse_source("https://www.fanqienovel.com/reader/987654") == (
        "reader",
        "987654",
    )
    with pytest.raises(ValueError):
        parse_source("https://example.com/page/123456")


def test_fanqie_request_delay_default_is_short_but_bounded(
    fanqie_modules: SimpleNamespace,
) -> None:
    settings = fanqie_modules.config.Config()

    assert settings.fanqie_request_delay == _DEFAULT_FANQIE_REQUEST_DELAY
    assert settings.fanqie_rank_limit == _DEFAULT_FANQIE_RANK_LIMIT
    assert (
        fanqie_modules.config.Config(
            fanqie_request_delay=_MIN_FANQIE_REQUEST_DELAY
        ).fanqie_request_delay
        == _MIN_FANQIE_REQUEST_DELAY
    )


def test_initial_state_html_cleaning_and_pua(fanqie_modules: SimpleNamespace) -> None:
    decoder = fanqie_modules.decoder
    page = (
        "<script>window.__INITIAL_STATE__ = "
        '{"reader":{"chapterData":{"title":"第一章",'
        '"content":"<p>甲&nbsp;乙</p><p>丙<br>丁</p>"}}};</script>'
    )
    state = decoder.extract_initial_state(page)
    assert state["reader"]["chapterData"]["title"] == "第一章"
    assert decoder.html_to_text(state["reader"]["chapterData"]["content"]) == (
        "甲 乙\n\n丙\n丁"
    )
    assert decoder.decrypt_pua("A\ue000B", {"\ue000": "你"}) == "A你B"
    assert decoder.decrypt_reader_pua("\ue3e8\ue3e9\ue52e") == "D在0"
    assert decoder.contains_pua("A\ue000B")
    assert len(decoder.FONT_SIGNATURE_MAP) == _FONT_SIGNATURE_COUNT
    assert (
        decoder.FONT_SIGNATURE_MAP[
            "6a8f5c2b453ad5daa08f8d4b71ee82a1cdef9f3ce24490763618e4505b3010f1"
        ]
        == "女"
    )


def test_reader_dom_content_fallback(fanqie_modules: SimpleNamespace) -> None:
    decoder = fanqie_modules.decoder
    page = (
        '<div class="muye-reader-content noselect">'
        "<p>甲&nbsp;乙</p><p>丙<br/>丁</p>"
        "</div>"
    )

    assert decoder.extract_reader_content(page) == "甲 乙\n\n丙\n丁"


def test_book_and_chapter_parsers(fanqie_modules: SimpleNamespace) -> None:
    provider = fanqie_modules.provider
    book_page = {
        "page": {
            "bookId": "123456",
            "bookName": "公开的书",
            "author": "作者",
            "chapterListWithVolume": [
                [
                    {"itemId": "111111", "title": "第一章"},
                    {"itemId": "222222", "title": "第二章", "isChapterLock": True},
                ]
            ],
        }
    }
    page = (
        "<script>window.__INITIAL_STATE__="
        + json.dumps(book_page, ensure_ascii=False)
        + ";</script>"
    )
    book = provider.parse_book_page(page, "123456")
    chapters = provider.parse_chapter_list(page)
    assert book.title == "公开的书"
    assert [chapter.item_id for chapter in chapters] == ["111111", "222222"]
    assert chapters[1].is_locked


def test_search_response_and_rank_parsers(fanqie_modules: SimpleNamespace) -> None:
    provider = fanqie_modules.provider
    search_payload = {
        "data": {
            "search_book_data_list": [
                {
                    "book_id": "123456",
                    "bookName": "模糊结果",
                    "author": "作者甲",
                    "readCount": str(_SEARCH_READ_COUNT),
                    "wordNumber": str(_SEARCH_WORD_COUNT),
                },
                {
                    "book_id": "123456",
                    "bookName": "重复结果",
                },
                {"book_id": "invalid", "bookName": "应被丢弃"},
            ]
        }
    }
    books = provider.parse_search_response(search_payload, limit=5)
    assert [book.book_id for book in books] == ["123456"]
    assert books[0].read_count == _SEARCH_READ_COUNT
    assert books[0].word_count == _SEARCH_WORD_COUNT
    assert provider.parse_search_response(
        {"data": {"search_book_data_list": []}}
    ) == []

    rank_state = {
        "rank": {
            "rankCategoryTypeList": {
                "male": [{"id": "1141", "name": "西方奇幻"}],
                "female": [{"id": "1139", "name": "古风世情"}],
            },
            "book_list": [
                {
                    "bookId": "765432",
                    "bookName": "\ue000榜书",
                    "author": "作者乙",
                    "read_count": str(_RANK_READ_COUNT),
                    "wordNumber": "1000",
                }
            ],
        }
    }
    page = (
        "<script>window.__INITIAL_STATE__="
        + json.dumps(rank_state, ensure_ascii=False)
        + ";</script>"
    )
    categories = provider.parse_rank_categories(page)
    assert categories["male"][0].category_id == "1141"
    ranked = provider.parse_rank_results(
        page,
        mapping={"\ue000": "榜"},
    )
    assert ranked[0].title == "榜榜书"
    assert ranked[0].rank == 1
    assert ranked[0].read_count == _RANK_READ_COUNT


def test_search_input_orders(fanqie_modules: SimpleNamespace) -> None:
    commands = fanqie_modules.commands

    assert commands._parse_search_input("模糊关键词") == ("模糊关键词", "related")
    assert commands._parse_search_input("搜索最新 模糊关键词") == (
        "模糊关键词",
        "new",
    )
    assert commands._parse_search_input("搜索最热　模糊关键词") == (
        "模糊关键词",
        "hot",
    )


class _FakeSearchResponse:
    def __init__(self, url: str, status: int, body: bytes) -> None:
        self.url = url
        self.status = status
        self._body = body

    async def body(self) -> bytes:
        return self._body


def test_browser_search_matches_only_official_response(
    fanqie_modules: SimpleNamespace,
) -> None:
    matches = fanqie_modules.browser_search._matches_search_response
    assert matches(
        SimpleNamespace(
            url=(
                "https://fanqienovel.com/api/author/search/search_book/v1"
                "?query_type=2"
            )
        ),
        "2",
    )
    assert matches(
        SimpleNamespace(
            url=(
                "https://www.fanqienovel.com/api/author/search/search_book/v1"
                "?query_type=0"
            )
        ),
        "0",
    )
    rejected = (
        "http://fanqienovel.com/api/author/search/search_book/v1?query_type=0",
        "https://example.com/api/author/search/search_book/v1?query_type=0",
        "https://fanqienovel.com/api/author/search/search_book/v1?query_type=1",
    )
    assert not any(matches(SimpleNamespace(url=url), "0") for url in rejected)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body"),
    [
        (200, b""),
        (200, "<div>请完成下列验证</div>".encode()),
        (403, b"forbidden"),
        (200, b"not-json"),
    ],
)
async def test_browser_search_rejects_invalid_responses(
    fanqie_modules: SimpleNamespace,
    status: int,
    body: bytes,
) -> None:
    browser = fanqie_modules.browser_search
    response = _FakeSearchResponse("https://fanqienovel.com/search", status, body)
    with pytest.raises(browser.BrowserSearchError):
        await browser._read_response_payload(response)


@pytest.mark.asyncio
async def test_provider_translates_browser_failure(
    fanqie_modules: SimpleNamespace,
) -> None:
    provider_module = fanqie_modules.provider

    async def unavailable(*_args: object, **_kwargs: object) -> object:
        raise fanqie_modules.browser_search.BrowserSearchError("浏览器不可用")

    async with provider_module.FanqieProvider(
        fanqie_modules.config.Config(),
        browser_search=unavailable,
    ) as provider:
        with pytest.raises(
            provider_module.FanqieServiceUnavailable,
            match="浏览器不可用",
        ):
            await provider.search("模糊关键词")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("order", "query_type"),
    [("related", "0"), ("new", "1"), ("hot", "2")],
)
async def test_search_uses_browser_payload_and_order(
    fanqie_modules: SimpleNamespace,
    order: str,
    query_type: str,
) -> None:
    provider_module = fanqie_modules.provider
    browser_module = fanqie_modules.browser_search
    payload = {
        "data": {
            "search_book_data_list": [
                {
                    "book_id": "123456",
                    "bookName": "Target book",
                    "author": "Author",
                    "book_abstract": "模糊匹配简介",
                    "read_count": str(_API_READ_COUNT),
                    "word_count": str(_API_WORD_COUNT),
                }
            ],
            "total_count": 1,
        }
    }
    seen: list[tuple[str, str, float, bool, str]] = []

    async def browser_search(
        keyword: str,
        *,
        query_type: str,
        timeout: float,
        headless: bool,
        profile_dir: str,
    ) -> Any:
        seen.append((keyword, query_type, timeout, headless, profile_dir))
        return browser_module.BrowserSearchSnapshot(payload=payload, page="")

    async with provider_module.FanqieProvider(
        fanqie_modules.config.Config(),
        browser_search=browser_search,
    ) as provider:
        books = await provider.search("十日", order=order)  # type: ignore[arg-type]

    assert books[0].book_id == "123456"
    assert books[0].description == "模糊匹配简介"
    assert books[0].read_count == _API_READ_COUNT
    assert books[0].word_count == _API_WORD_COUNT
    assert seen == [("十日", query_type, 30.0, True, "")]


@pytest.mark.asyncio
async def test_search_decodes_pua_with_font_from_browser_page(
    fanqie_modules: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_module = fanqie_modules.provider
    browser_module = fanqie_modules.browser_search
    snapshot = browser_module.BrowserSearchSnapshot(
        payload={
            "data": {
                "search_book_data_list": [
                    {
                        "book_id": "123456",
                        "book_name": "\ue410\ue4c0终焉",
                        "author": "杀虫队队\ue4cc",
                    }
                ]
            }
        },
        page="<script>window.__INITIAL_STATE__={};</script>",
    )

    async def browser_search(*_args: object, **_kwargs: object) -> object:
        return snapshot

    async def font_mapping(
        _self: object,
        _page: str,
        _state: dict[str, Any],
    ) -> dict[str, str]:
        return {"\ue410": "文", "\ue4c0": "打", "\ue4cc": "走"}

    monkeypatch.setattr(provider_module.FanqieProvider, "_font_mapping", font_mapping)
    async with provider_module.FanqieProvider(
        fanqie_modules.config.Config(),
        browser_search=browser_search,
    ) as provider:
        books = await provider.search("终焉")

    assert books[0].title == "文打终焉"
    assert books[0].author == "杀虫队队走"


@pytest.mark.asyncio
async def test_browser_search_reports_missing_runtime(
    fanqie_modules: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = fanqie_modules.browser_search

    def missing_runtime() -> object:
        raise browser.BrowserSearchError("未安装 Playwright")  # noqa: TRY003

    monkeypatch.setattr(browser, "_load_async_playwright", missing_runtime)
    with pytest.raises(browser.BrowserSearchError, match="未安装 Playwright"):
        await browser.search_page_snapshot(
            "模糊关键词",
            query_type="0",
            timeout=1,
            headless=True,
        )


@pytest.mark.asyncio
async def test_browser_search_serializes_requests(
    fanqie_modules: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = fanqie_modules.browser_search
    active = 0
    max_active = 0

    async def fake_page_search(*_args: object, **_kwargs: object) -> object:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        active -= 1
        return browser.BrowserSearchSnapshot(
            payload={"data": {"search_book_data_list": []}},
            page="",
        )

    monkeypatch.setattr(browser, "_search_page_snapshot", fake_page_search)
    await asyncio.gather(
        browser.search_page_snapshot(
            "关键词一",
            query_type="0",
            timeout=1,
            headless=True,
        ),
        browser.search_page_snapshot(
            "关键词二",
            query_type="0",
            timeout=1,
            headless=True,
        ),
    )

    assert max_active == 1


@pytest.mark.asyncio
async def test_rank_provider_uses_official_routes(
    fanqie_modules: SimpleNamespace,
) -> None:
    provider_module = fanqie_modules.provider
    page = (
        "<script src='captcha/index.js'></script>"
        "<script>window.__INITIAL_STATE__="
        + json.dumps(
            {
                "rank": {
                    "rankCategoryTypeList": {
                        "male": [{"id": "1141", "name": "西方奇幻"}],
                        "female": [{"id": "1139", "name": "古风世情"}],
                    },
                    "book_list": [
                        {
                            "bookId": "765432",
                            "bookName": "榜单书",
                            "author": "作者乙",
                            "read_count": "99",
                            "wordNumber": "1000",
                        }
                    ],
                }
            },
            ensure_ascii=False,
        )
        + ";</script>"
    )
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.raw_path.decode())
        return httpx.Response(200, text=page, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with provider_module.FanqieProvider(
        fanqie_modules.config.Config(fanqie_rank_limit=1), client
    ) as provider:
        categories = await provider.list_rank_categories()
        books = await provider.list_rank_books(
            gender="male",
            rank_type="read",
            category_id=categories["male"][0].category_id,
        )
    await client.aclose()

    assert seen_paths == ["/rank", "/rank/1_2_1141"]
    assert books[0].title == "榜单书"
    assert books[0].rank == 1


@pytest.mark.asyncio
async def test_link_query_propagates_state_rejection(
    fanqie_modules: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = fanqie_modules.commands
    provider_module = fanqie_modules.provider
    book = provider_module.BookSummary("123456", "Target book", "Author")
    chapters = [provider_module.ChapterRef("654321", "Chapter 1", 1)]

    class FakeProvider:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def resolve_book_reference(self, _value: str) -> object:
            return book

        async def list_chapters(self, _book_id: str) -> list[object]:
            return chapters

    rejection_calls: list[tuple[object, ...]] = []

    async def fake_reject_arg(*args: object) -> None:
        rejection_calls.append(args)

    async def fake_set_book_and_ask_range(
        _matcher: object,
        _book: object,
        _chapters: object,
    ) -> None:
        raise RejectedException

    monkeypatch.setattr(commands, "FanqieProvider", FakeProvider)
    monkeypatch.setattr(
        commands,
        "_set_book_and_ask_range",
        fake_set_book_and_ask_range,
    )
    monkeypatch.setattr(
        commands.fanqie_cmd,
        "reject_arg",
        fake_reject_arg,
        raising=False,
    )

    with pytest.raises(RejectedException):
        await commands._begin_fanqie_input(
            SimpleNamespace(state={}),
            "https://fanqienovel.com/page/123456",
        )

    assert rejection_calls == []


@pytest.mark.asyncio
async def test_choice_state_machine_does_not_repeat_search(
    fanqie_modules: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = fanqie_modules.commands
    provider_module = fanqie_modules.provider
    book = provider_module.BookSummary("123456", "Target book", "Author")
    chapters = [
        provider_module.ChapterRef("654321", "Chapter 1", 1),
        provider_module.ChapterRef("654322", "Chapter 2", 2),
    ]
    search_values: list[str] = []
    chapter_book_ids: list[str] = []
    prompts: list[tuple[object, ...]] = []
    finishes: list[tuple[object, ...]] = []
    submissions: list[tuple[object, ...]] = []

    class FakeProvider:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def search(self, value: str, **_kwargs: object) -> list[object]:
            search_values.append(value)
            return [book]

        async def list_chapters(self, book_id: str) -> list[object]:
            chapter_book_ids.append(book_id)
            return chapters

    async def allow_feature(*_args: object, **_kwargs: object) -> bool:
        return True

    async def fake_reject_arg(*args: object, **_kwargs: object) -> None:
        prompts.append(args)

    async def fake_finish(*args: object, **_kwargs: object) -> None:
        finishes.append(args)

    async def fake_submit_job(*args: object, **_kwargs: object) -> tuple[int, None]:
        submissions.append(args)
        return 42, None

    monkeypatch.setattr(commands, "FanqieProvider", FakeProvider)
    monkeypatch.setattr(commands, "_feature_ok", allow_feature)
    monkeypatch.setattr(commands, "submit_job", fake_submit_job)
    monkeypatch.setattr(commands.fanqie_cmd, "reject_arg", fake_reject_arg)
    monkeypatch.setattr(commands.fanqie_cmd, "finish", fake_finish)

    event = SimpleNamespace(get_user_id=lambda: "10001", group_id=20002)
    matcher = commands.fanqie_cmd()

    await commands.handle_fanqie_entry(matcher, Message("Target book"), None)
    assert matcher.state["fanqie_step"] == "input"
    assert str(matcher.get_arg("fanqie_choice")) == "Target book"

    await commands.handle_fanqie_choice(
        event,
        matcher,
        object(),
        Message("Target book"),
    )
    assert search_values == ["Target book"]
    assert matcher.state["fanqie_step"] == "book"

    await commands.handle_fanqie_choice(event, matcher, object(), Message("1"))
    assert search_values == ["Target book"]
    assert chapter_book_ids == ["123456"]
    assert matcher.state["fanqie_step"] == "range"

    await commands.handle_fanqie_choice(event, matcher, object(), Message("1-2"))
    assert matcher.state["fanqie_step"] == "confirm"

    await commands.handle_fanqie_choice(event, matcher, object(), Message("确认"))
    assert submissions and submissions[0][0:2] == (10001, 20002)
    assert finishes and "#42" in str(finishes[-1][0])
    assert [args[0] for args in prompts] == ["fanqie_choice"] * 3


@pytest.mark.asyncio
async def test_rank_state_machine_reuses_book_download_flow(
    fanqie_modules: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = fanqie_modules.commands
    provider_module = fanqie_modules.provider
    book = provider_module.BookSummary(
        "765432",
        "榜单书",
        "作者乙",
        rank=1,
        read_count=99,
        word_count=1000,
    )
    categories = {
        "male": [provider_module.RankCategory("1141", "西方奇幻")],
        "female": [provider_module.RankCategory("1139", "古风世情")],
    }
    chapters = [provider_module.ChapterRef("654321", "Chapter 1", 1)]
    rank_calls: list[tuple[str, str, str]] = []
    chapter_book_ids: list[str] = []
    prompts: list[tuple[object, ...]] = []

    class FakeProvider:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def list_rank_categories(self) -> dict[str, list[object]]:
            return categories

        async def list_rank_books(
            self,
            *,
            gender: str,
            rank_type: str,
            category_id: str,
        ) -> list[object]:
            rank_calls.append((gender, rank_type, category_id))
            return [book]

        async def list_chapters(self, book_id: str) -> list[object]:
            chapter_book_ids.append(book_id)
            return chapters

    async def allow_feature(*_args: object, **_kwargs: object) -> bool:
        return True

    async def fake_reject_arg(*args: object, **_kwargs: object) -> None:
        prompts.append(args)

    monkeypatch.setattr(commands, "FanqieProvider", FakeProvider)
    monkeypatch.setattr(commands, "_feature_ok", allow_feature)
    monkeypatch.setattr(commands.fanqie_cmd, "reject_arg", fake_reject_arg)

    event = SimpleNamespace(get_user_id=lambda: "10001", group_id=20002)
    matcher = commands.fanqie_cmd()

    await commands.handle_fanqie_entry(matcher, Message("榜单"), None)
    await commands.handle_fanqie_choice(event, matcher, object(), Message("榜单"))
    assert matcher.state["fanqie_step"] == "rank_kind"
    await commands.handle_fanqie_choice(event, matcher, object(), Message("9"))
    assert matcher.state["fanqie_step"] == "rank_kind"
    await commands.handle_fanqie_choice(event, matcher, object(), Message("1"))
    assert matcher.state["fanqie_step"] == "rank_gender"
    await commands.handle_fanqie_choice(event, matcher, object(), Message("9"))
    assert matcher.state["fanqie_step"] == "rank_gender"
    await commands.handle_fanqie_choice(event, matcher, object(), Message("1"))
    assert matcher.state["fanqie_step"] == "rank_category"
    await commands.handle_fanqie_choice(event, matcher, object(), Message("1"))
    assert matcher.state["fanqie_step"] == "book"
    assert rank_calls == [("male", "read", "1141")]
    assert "#1" in str(prompts[-1][1])
    await commands.handle_fanqie_choice(event, matcher, object(), Message("1"))
    assert chapter_book_ids == ["765432"]
    assert matcher.state["fanqie_step"] == "range"


@pytest.mark.asyncio
async def test_submit_job_caches_ids_before_commit(
    fanqie_modules: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = fanqie_modules.state
    provider_module = fanqie_modules.provider
    session = _CommitExpiringSession(state.FanqieBook, state.FanqieJob)
    queued_ids: list[int] = []

    def fake_get_session() -> _CommitExpiringSession:
        return session

    async def fake_enqueue(job_id: int) -> bool:
        queued_ids.append(job_id)
        return True

    monkeypatch.setattr(state, "get_session", fake_get_session)
    monkeypatch.setattr(state, "_enqueue", fake_enqueue)

    book = provider_module.BookSummary("123456", "Target book", "Author")
    chapters = [provider_module.ChapterRef("654321", "Chapter 1", 1)]
    job_id, error = await state.submit_job(10001, None, book, chapters, 1, 1)

    assert (job_id, error) == (_TEST_JOB_ID, None)
    assert queued_ids == [_TEST_JOB_ID]
    created_chapters = [
        item
        for item in session.items
        if isinstance(item, state.FanqieJobChapter)
    ]
    assert created_chapters[0].job_id == _TEST_JOB_ID


@pytest.mark.asyncio
async def test_run_job_snapshots_values_before_commit(
    fanqie_modules: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = fanqie_modules.state
    provider_module = fanqie_modules.provider
    job = state.FanqieJob(
        book_record_id=_TEST_BOOK_RECORD_ID,
        requester_user_id=10001,
        group_id=None,
        start_chapter=1,
        end_chapter=1,
        total_chapters=1,
        status="queued",
        cancel_requested=False,
    )
    job.id = _TEST_JOB_ID
    book = state.FanqieBook(
        book_id="123456",
        title="Target book",
        author="Author",
        url="https://fanqienovel.com/page/123456",
    )
    book.id = _TEST_BOOK_RECORD_ID
    chapter = state.FanqieJobChapter(
        job_id=_TEST_JOB_ID,
        chapter_index=3,
        item_id="654321",
        title="Chapter 3",
        is_locked=True,
        status="pending",
    )
    chapter.id = 99
    current_job = state.FanqieJob(
        book_record_id=_TEST_BOOK_RECORD_ID,
        requester_user_id=10001,
        group_id=None,
        start_chapter=1,
        end_chapter=1,
        total_chapters=1,
        status="running",
        cancel_requested=False,
    )
    current_job.id = _TEST_JOB_ID
    current_chapter = state.FanqieJobChapter(
        job_id=_TEST_JOB_ID,
        chapter_index=3,
        item_id="654321",
        title="Chapter 3",
        is_locked=True,
        status="pending",
    )
    current_chapter.id = 99
    sessions = iter(
        [
            _WorkerSession(
                job=job,
                book=book,
                chapter_rows=[chapter],
                expire_on_commit=True,
            ),
            _WorkerSession(job=current_job, chapter_row=current_chapter),
        ]
    )
    assembled: list[tuple[int, str, str]] = []

    class FakeProvider:
        def __init__(self, _config: object) -> None:
            return None

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def fetch_chapter(self, item_id: str) -> object:
            assert item_id == "654321"
            return provider_module.ChapterContent("654321", "Chapter 3", "正文")

    async def allow_feature(*_args: object, **_kwargs: object) -> bool:
        return True

    async def not_cancelled(_job_id: int) -> bool:
        return False

    async def mark_chapter(*_args: object, **_kwargs: object) -> None:
        return None

    async def assemble(job_id: int, title: str, author: str) -> None:
        assembled.append((job_id, title, author))

    monkeypatch.setattr(state, "get_session", lambda: next(sessions))
    monkeypatch.setattr(state, "FanqieProvider", FakeProvider)
    monkeypatch.setattr(state, "check_feature_permission", allow_feature)
    monkeypatch.setattr(state, "_is_cancelled", not_cancelled)
    monkeypatch.setattr(state, "_mark_chapter", mark_chapter)
    monkeypatch.setattr(state, "_assemble_and_deliver", assemble)
    monkeypatch.setattr(
        state,
        "_chapter_temp_path",
        lambda _job_id, _chapter_index: tmp_path / "chapter.txt",
    )

    await state._run_job(_TEST_JOB_ID)

    assert assembled == [(_TEST_JOB_ID, "Target book", "Author")]
    assert (tmp_path / "chapter.txt").read_text(encoding="utf-8") == (
        "第3章 Chapter 3\n\n正文\n"
    )


@pytest.mark.asyncio
async def test_run_job_keeps_transient_chapter_failure_retryable(  # noqa: C901
    fanqie_modules: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = fanqie_modules.state
    provider_module = fanqie_modules.provider
    job = state.FanqieJob(
        book_record_id=_TEST_BOOK_RECORD_ID,
        requester_user_id=10001,
        group_id=None,
        start_chapter=1,
        end_chapter=1,
        total_chapters=1,
        status="queued",
        cancel_requested=False,
    )
    job.id = _TEST_JOB_ID
    book = state.FanqieBook(
        book_id="123456",
        title="Target book",
        author="Author",
        url="https://fanqienovel.com/page/123456",
    )
    book.id = _TEST_BOOK_RECORD_ID
    chapter = state.FanqieJobChapter(
        job_id=_TEST_JOB_ID,
        chapter_index=11,
        item_id="654321",
        title="Chapter 11",
        is_locked=False,
        status="pending",
    )
    chapter.id = 99
    current_job = state.FanqieJob(
        book_record_id=_TEST_BOOK_RECORD_ID,
        requester_user_id=10001,
        group_id=None,
        start_chapter=1,
        end_chapter=1,
        total_chapters=1,
        status="running",
        cancel_requested=False,
    )
    current_job.id = _TEST_JOB_ID
    current_chapter = state.FanqieJobChapter(
        job_id=_TEST_JOB_ID,
        chapter_index=11,
        item_id="654321",
        title="Chapter 11",
        is_locked=False,
        status="pending",
    )
    current_chapter.id = 99
    sessions = iter(
        [
            _WorkerSession(job=job, book=book, chapter_rows=[chapter]),
            _WorkerSession(job=current_job, chapter_row=current_chapter),
        ]
    )
    chapter_updates: list[tuple[int, int, str, str | None]] = []
    job_failures: list[tuple[int, str]] = []
    notifications: list[str] = []

    class FakeProvider:
        def __init__(self, _config: object) -> None:
            return None

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def fetch_chapter(self, _item_id: str) -> object:
            raise provider_module.ChapterFetchTransientError(_RAW_FULL_502_ERROR)

    async def allow_feature(*_args: object, **_kwargs: object) -> bool:
        return True

    async def not_cancelled(_job_id: int) -> bool:
        return False

    async def mark_chapter(
        job_id: int,
        chapter_id: int,
        status: str,
        *,
        error: str | None = None,
        **_kwargs: object,
    ) -> None:
        chapter_updates.append((job_id, chapter_id, status, error))

    async def mark_failed(job_id: int, error: str) -> None:
        job_failures.append((job_id, error))

    async def job_group_id(_job_id: int) -> None:
        return None

    async def notify(_bot: object, _group_id: int | None, message: str) -> None:
        notifications.append(message)

    async def should_not_assemble(*_args: object) -> None:
        pytest.fail("瞬时失败后不应合并缺章成品")

    monkeypatch.setattr(state, "get_session", lambda: next(sessions))
    monkeypatch.setattr(state, "FanqieProvider", FakeProvider)
    monkeypatch.setattr(state, "check_feature_permission", allow_feature)
    monkeypatch.setattr(state, "_is_cancelled", not_cancelled)
    monkeypatch.setattr(state, "_mark_chapter", mark_chapter)
    monkeypatch.setattr(state, "_mark_job_failed", mark_failed)
    monkeypatch.setattr(state, "_job_group_id", job_group_id)
    monkeypatch.setattr(state, "notify_group", notify)
    monkeypatch.setattr(state, "get_bot", object)
    monkeypatch.setattr(state, "_assemble_and_deliver", should_not_assemble)

    await state._run_job(_TEST_JOB_ID)

    expected_error = "第11章请求失败：raw_full HTTP 502"
    assert chapter_updates == [(_TEST_JOB_ID, 99, "failed", expected_error)]
    assert job_failures == [(_TEST_JOB_ID, expected_error)]
    assert notifications == [
        "番茄任务 #42 下载失败：第11章，请使用“番茄任务 42 重试”"
    ]


@pytest.mark.asyncio
async def test_mark_chapter_caches_values_before_commit(
    fanqie_modules: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = fanqie_modules.state
    job = state.FanqieJob(
        book_record_id=_TEST_BOOK_RECORD_ID,
        requester_user_id=10001,
        group_id=None,
        start_chapter=1,
        end_chapter=1,
        total_chapters=1,
        status="running",
        cancel_requested=False,
    )
    job.id = _TEST_JOB_ID
    chapter = state.FanqieJobChapter(
        job_id=_TEST_JOB_ID,
        chapter_index=3,
        item_id="654321",
        title="Chapter 3",
        is_locked=False,
        status="pending",
    )
    chapter.id = 99
    session = _WorkerSession(
        job=job,
        chapter_row=chapter,
        expire_on_commit=True,
    )
    monkeypatch.setattr(state, "get_session", lambda: session)

    await state._mark_chapter(
        _TEST_JOB_ID,
        99,
        "completed",
        temp_path="chapter.txt",
    )

    assert session.commits == 1
    assert chapter.status == "completed"
    assert chapter.temp_path == "chapter.txt"


@pytest.mark.asyncio
async def test_retry_completed_job_resets_only_old_remote_outages(
    fanqie_modules: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = fanqie_modules.state
    job = state.FanqieJob(
        book_record_id=_TEST_BOOK_RECORD_ID,
        requester_user_id=10001,
        group_id=None,
        start_chapter=1,
        end_chapter=3,
        total_chapters=3,
        completed_chapters=3,
        status="completed",
        cancel_requested=False,
        output_path="old.txt",
        output_name="old.txt",
        send_status="sent",
    )
    job.id = _TEST_JOB_ID
    completed = SimpleNamespace(
        status="completed",
        temp_path="good.txt",
        last_error=None,
    )
    old_outage = SimpleNamespace(
        status="unavailable",
        temp_path=None,
        last_error="阅读页仅返回预览；第三方全文接口不可用",
    )
    nonfree = SimpleNamespace(
        status="unavailable",
        temp_path=None,
        last_error="该章节未被阅读页明确标记为免费",
    )
    session = _WorkerSession(
        job=job,
        chapter_rows=[completed, old_outage, nonfree],
    )
    queued: list[int] = []

    async def enqueue(job_id: int) -> bool:
        queued.append(job_id)
        return True

    monkeypatch.setattr(state, "get_session", lambda: session)
    monkeypatch.setattr(state, "_has_owned_file", lambda path: path == "good.txt")
    monkeypatch.setattr(state, "_enqueue", enqueue)

    assert await state.retry_job(_TEST_JOB_ID) == (True, None)
    assert queued == [_TEST_JOB_ID]
    assert job.status == "queued"
    assert job.output_path is None
    assert completed.status == "completed"
    assert old_outage.status == "pending"
    assert old_outage.last_error is None
    assert nonfree.status == "unavailable"


@pytest.mark.asyncio
async def test_provider_retries_429_and_rejects_empty_chapter(
    fanqie_modules: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_module = fanqie_modules.provider
    calls = 0

    async def no_sleep(_seconds: float) -> None:
        return

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < _RETRY_RESPONSES:
            return httpx.Response(429, request=request)
        page = '<script>window.__INITIAL_STATE__={"reader":{}};</script>'
        return httpx.Response(_OK_STATUS, text=page, request=request)

    monkeypatch.setattr(provider_module.asyncio, "sleep", no_sleep)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = fanqie_modules.config.Config(fanqie_request_retries=2)
    async with provider_module.FanqieProvider(settings, client) as provider:
        response = await provider._request("https://fanqienovel.com/reader/111111")
        assert response.status_code == _OK_STATUS
        with pytest.raises(provider_module.ChapterUnavailable):
            await provider.fetch_chapter("111111")
    await client.aclose()
    assert calls == _EXPECTED_CALLS


@pytest.mark.asyncio
async def test_provider_reads_content_even_when_catalog_marks_chapter_locked(
    fanqie_modules: SimpleNamespace,
) -> None:
    provider_module = fanqie_modules.provider
    page = (
        "<script>window.__INITIAL_STATE__="
        + json.dumps(
            {
                "reader": {
                    "chapterData": {
                        "title": "第十一章",
                        "isChapterLock": True,
                        "content": "",
                    }
                }
            },
            ensure_ascii=False,
        )
        + ";</script>"
        '<div class="muye-reader-content noselect"><p>页面实际正文</p></div>'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(_OK_STATUS, text=page, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with provider_module.FanqieProvider(
        fanqie_modules.config.Config(), client
    ) as provider:
        chapter = await provider.fetch_chapter("111111")
    await client.aclose()

    assert chapter.title == "第十一章"
    assert chapter.content == "页面实际正文"


def test_mobile_helper_reads_only_single_exported_chapter(
    fanqie_modules: SimpleNamespace,
    tmp_path: Path,
) -> None:
    mobile_helper = fanqie_modules.mobile_helper
    output_dir = tmp_path / "output" / "book"
    output_dir.mkdir(parents=True)
    (output_dir / "0000_书籍信息.txt").write_text(
        "书名：测试书\n",
        encoding="utf-8",
    )
    (output_dir / "0001_第十一章.txt").write_text(
        "分卷：第一卷\n\n第十一章\n\n完整正文\n第二段\n",
        encoding="utf-8",
    )

    chapter = mobile_helper._read_exported_chapter(
        tmp_path / "output",
        "第十一章",
        4096,
    )

    assert chapter.title == "第十一章"
    assert chapter.content == "完整正文\n第二段"


@pytest.mark.asyncio
async def test_provider_uses_mobile_helper_for_explicitly_free_preview(
    fanqie_modules: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_module = fanqie_modules.provider
    page = (
        "<script>window.__INITIAL_STATE__="
        + json.dumps(
            {
                "reader": {
                    "chapterData": {
                        "bookId": "123456",
                        "order": "11",
                        "title": "第十一章",
                        "needPay": 0,
                        "isPaidPublication": False,
                        "isPaidStory": False,
                        "isChapterLock": True,
                        "chapterWordNumber": "2000",
                        "content": "网页预览",
                    }
                }
            },
            ensure_ascii=False,
        )
        + ";</script>"
    )
    helper_calls: list[tuple[str, int, str]] = []

    async def fake_fetch_mobile_chapter(
        _settings: object,
        *,
        book_id: str,
        chapter_order: int,
        expected_title: str,
    ) -> SimpleNamespace:
        helper_calls.append((book_id, chapter_order, expected_title))
        return SimpleNamespace(title="第十一章", content="完整移动端正文")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(_OK_STATUS, text=page, request=request)

    monkeypatch.setattr(
        provider_module,
        "fetch_mobile_chapter",
        fake_fetch_mobile_chapter,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = fanqie_modules.config.Config(
        fanqie_third_party_api_base="",
        fanqie_mobile_helper_path="helper.exe",
    )
    async with provider_module.FanqieProvider(settings, client) as provider:
        chapter = await provider.fetch_chapter("111111")
    await client.aclose()

    assert helper_calls == [("123456", 11, "第十一章")]
    assert chapter.content == "完整移动端正文"


@pytest.mark.asyncio
async def test_provider_uses_third_party_raw_full_for_free_preview(
    fanqie_modules: SimpleNamespace,
) -> None:
    provider_module = fanqie_modules.provider
    page = (
        "<script>window.__INITIAL_STATE__="
        + json.dumps(
            {
                "reader": {
                    "chapterData": {
                        "title": "第十一章",
                        "needPay": 0,
                        "isPaidPublication": False,
                        "isPaidStory": False,
                        "isChapterLock": True,
                        "chapterWordNumber": "10",
                        "content": "网页预览",
                    }
                }
            },
            ensure_ascii=False,
        )
        + ";</script>"
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "fanqienovel.com":
            return httpx.Response(_OK_STATUS, text=page, request=request)
        assert request.url.host == "api.example.test"
        assert request.url.path == "/api/raw_full"
        assert request.url.params["item_id"] == "111111"
        return httpx.Response(
            _OK_STATUS,
            json={
                "code": 200,
                "data": {
                    "title": "第十一章",
                    "content": "<p>完整第三方全文正文。</p>" * 20,
                    "paragraphs_num": 2,
                    "free_para_nums": 2,
                    "chapter_word_number": "10",
                },
            },
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = fanqie_modules.config.Config(
        fanqie_third_party_api_base="https://api.example.test"
    )
    async with provider_module.FanqieProvider(settings, client) as provider:
        chapter = await provider.fetch_chapter("111111")
    await client.aclose()

    assert len(requests) == _THIRD_PARTY_REQUEST_COUNT
    assert chapter.title == "第十一章"
    assert chapter.content.startswith("完整第三方全文正文。\n\n")
    assert len(chapter.content) > _MIN_FULL_CONTENT_CHARS


@pytest.mark.asyncio
async def test_provider_fails_over_after_raw_full_502(
    fanqie_modules: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_module = fanqie_modules.provider
    preview = "官网免费预览内容" * 12
    full_content = preview + "完整正文后续" * 40
    page = (
        "<script>window.__INITIAL_STATE__="
        + json.dumps(
            {
                "reader": {
                    "chapterData": {
                        "title": "第十一章",
                        "needPay": 0,
                        "isPaidPublication": False,
                        "isPaidStory": False,
                        "chapterWordNumber": "300",
                        "content": preview,
                    }
                }
            },
            ensure_ascii=False,
        )
        + ";</script>"
    )
    requests: list[httpx.Request] = []
    fallback_calls = 0

    async def no_sleep(_seconds: float) -> None:
        return

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal fallback_calls
        requests.append(request)
        if request.url.host == "fanqienovel.com":
            return httpx.Response(_OK_STATUS, text=page, request=request)
        if request.url.host == "raw.example.test":
            return httpx.Response(502, request=request)
        assert request.url.host == "fallback.example.test"
        assert request.url.path == "/proxy"
        assert request.url.params["action"] == "content"
        assert request.headers["X-API-Token"] == "public-test-token"
        fallback_calls += 1
        return httpx.Response(
            _OK_STATUS,
            json={
                "data": {
                    "content": full_content if fallback_calls == 1 else "短文"
                }
            },
            request=request,
        )

    monkeypatch.setattr(provider_module.asyncio, "sleep", no_sleep)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = fanqie_modules.config.Config(
        fanqie_third_party_api_base="https://raw.example.test",
        fanqie_third_party_api_retries=1,
        fanqie_third_party_fallback_base="https://fallback.example.test",
        fanqie_third_party_fallback_token="public-test-token",
    )
    async with provider_module.FanqieProvider(settings, client) as provider:
        chapter = await provider.fetch_chapter("111111")
        with pytest.raises(
            provider_module.ChapterFetchTransientError,
            match="已熔断节点：raw_full",
        ):
            await provider.fetch_chapter("111111")
    await client.aclose()

    assert [request.url.host for request in requests] == [
        "fanqienovel.com",
        "raw.example.test",
        "raw.example.test",
        "fallback.example.test",
        "fanqienovel.com",
        "fallback.example.test",
    ]
    assert chapter.title == "第十一章"
    assert chapter.content == full_content


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["http_502", "payload_500"])
async def test_provider_keeps_service_failures_retryable(
    fanqie_modules: SimpleNamespace,
    failure_mode: str,
) -> None:
    provider_module = fanqie_modules.provider
    preview = "官网免费预览内容" * 12
    page = (
        "<script>window.__INITIAL_STATE__="
        + json.dumps(
            {
                "reader": {
                    "chapterData": {
                        "title": "第十一章",
                        "needPay": 0,
                        "isPaidPublication": False,
                        "isPaidStory": False,
                        "chapterWordNumber": "2000",
                        "content": preview,
                    }
                }
            },
            ensure_ascii=False,
        )
        + ";</script>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "fanqienovel.com":
            return httpx.Response(_OK_STATUS, text=page, request=request)
        if failure_mode == "http_502":
            return httpx.Response(502, request=request)
        return httpx.Response(
            _OK_STATUS,
            json={"code": 500, "message": "upstream unavailable"},
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = fanqie_modules.config.Config(
        fanqie_third_party_api_base="https://raw.example.test",
        fanqie_third_party_api_retries=0,
        fanqie_third_party_fallback_base="",
    )
    async with provider_module.FanqieProvider(settings, client) as provider:
        with pytest.raises(
            provider_module.ChapterFetchTransientError,
            match="raw_full",
        ):
            await provider.fetch_chapter("111111")
    await client.aclose()


@pytest.mark.asyncio
async def test_provider_never_uses_mobile_helper_for_nonfree_preview(
    fanqie_modules: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_module = fanqie_modules.provider
    page = (
        "<script>window.__INITIAL_STATE__="
        + json.dumps(
            {
                "reader": {
                    "chapterData": {
                        "bookId": "123456",
                        "order": "11",
                        "title": "第十一章",
                        "needPay": 1,
                        "isPaidPublication": True,
                        "chapterWordNumber": "2000",
                        "content": "网页预览",
                    }
                }
            },
            ensure_ascii=False,
        )
        + ";</script>"
    )
    helper_called = False

    async def fake_fetch_mobile_chapter(*_args: object, **_kwargs: object) -> object:
        nonlocal helper_called
        helper_called = True
        raise AssertionError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(_OK_STATUS, text=page, request=request)

    monkeypatch.setattr(
        provider_module,
        "fetch_mobile_chapter",
        fake_fetch_mobile_chapter,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = fanqie_modules.config.Config(fanqie_mobile_helper_path="helper.exe")
    async with provider_module.FanqieProvider(settings, client) as provider:
        with pytest.raises(provider_module.ChapterUnavailable, match="明确标记为免费"):
            await provider.fetch_chapter("111111")
    await client.aclose()

    assert not helper_called


def test_range_filename_and_startup_report(fanqie_modules: SimpleNamespace) -> None:
    parse_range = fanqie_modules.commands._parse_range
    assert parse_range("全书", 20) == (1, 20)
    assert parse_range("第 2 章-5", 20) == (2, 5)
    with pytest.raises(ValueError, match="超过"):
        parse_range("全书", 501)
    with pytest.raises(ValueError):
        parse_range("4-2", 20)
    assert fanqie_modules.state.safe_filename("../a:b?.txt") == "_a_b_.txt"
    assert any(
        item.module_name.endswith("yawn_fanqie") and item.state == "loaded"
        for item in fanqie_modules.core.get_sub_plugin_load_report()
    )
    assert fanqie_modules.core.permission.FEATURE_REGISTRY["fanqie"] == "番茄小说"
