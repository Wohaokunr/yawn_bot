from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import nonebot
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_RETRY_RESPONSES = 3
_OK_STATUS = 200
_EXPECTED_CALLS = 4


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
