from __future__ import annotations

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
_MIN_FANQIE_REQUEST_DELAY = 0.2


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
            if isinstance(item, self.book_type) and item.id is None:
                item.id = _TEST_BOOK_RECORD_ID
            if isinstance(item, self.job_type) and item.id is None:
                item.id = _TEST_JOB_ID

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
        decoder=importlib.import_module("src.plugins.yawn_core.yawn_fanqie.decoder"),
        provider=importlib.import_module("src.plugins.yawn_core.yawn_fanqie.provider"),
        config=importlib.import_module("src.plugins.yawn_core.yawn_fanqie.config"),
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
    assert decoder.contains_pua("A\ue000B")
    assert decoder.font_glyph_to_text("gid58670") == "0"


def test_search_and_book_chapter_parsers(fanqie_modules: SimpleNamespace) -> None:
    provider = fanqie_modules.provider
    state = {
        "search": {
            "books": [
                {
                    "bookId": "123456",
                    "bookName": "公开的书",
                    "author": "作者",
                    "abstract": "简介",
                },
                {"bookId": "234567", "title": "第二本", "authorName": "乙"},
            ]
        }
    }
    page = (
        "<script>window.__INITIAL_STATE__="
        + json.dumps(state, ensure_ascii=False)
        + ";</script>"
    )
    books = provider.parse_search_results(page)
    assert [book.book_id for book in books] == ["123456", "234567"]
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


@pytest.mark.asyncio
async def test_search_uses_current_public_path(
    fanqie_modules: SimpleNamespace,
) -> None:
    provider_module = fanqie_modules.provider
    page = (
        "<script>window.__INITIAL_STATE__="
        + json.dumps(
            {
                "search": {
                    "books": [
                        {
                            "bookId": "123456",
                            "bookName": "Target book",
                            "author": "Author",
                        }
                    ]
                }
            },
            ensure_ascii=False,
        )
        + ";</script>"
    )
    seen_urls: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(request.url)
        return httpx.Response(200, text=page, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with provider_module.FanqieProvider(
        fanqie_modules.config.Config(), client
    ) as provider:
        books = await provider.search("十日终焉")
    await client.aclose()

    assert books[0].book_id == "123456"
    assert seen_urls[0].raw_path == (
        b"/search/%E5%8D%81%E6%97%A5%E7%BB%88%E7%84%89"
    )
    assert seen_urls[0].query == b""


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

        async def search(self, value: str) -> list[object]:
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
        chapter_index=3,
        item_id="654321",
        title="Chapter 3",
        is_locked=False,
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
