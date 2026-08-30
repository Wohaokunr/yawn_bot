from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nonebot
import pytest

_REMOTE_TIMEOUT_MS = 12_000
_PRIVATE_FILE_MODE = 0o600


def _browser_module() -> Any:
    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()
    return importlib.import_module(
        "src.plugins.yawn_core.yawn_fanqie.browser_search"
    )


class _FakeContext:
    def __init__(self) -> None:
        self.closed = False
        self.saved_path: Path | None = None
        self.indexed_db = False

    async def storage_state(
        self,
        *,
        path: str,
        indexed_db: bool,
    ) -> dict[str, object]:
        target = Path(path)
        payload = json.dumps({"cookies": [], "origins": []})
        await asyncio.to_thread(target.write_text, payload, encoding="utf-8")
        self.saved_path = target
        self.indexed_db = indexed_db
        return {"cookies": [], "origins": []}

    async def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self, context: _FakeContext) -> None:
        self.context = context
        self.closed = False
        self.context_kwargs: dict[str, object] | None = None

    async def new_context(self, **kwargs: object) -> _FakeContext:
        self.context_kwargs = kwargs
        return self.context

    async def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self) -> None:
        self.remote_context = _FakeContext()
        self.remote_browser = _FakeBrowser(self.remote_context)
        self.connected_endpoint = ""
        self.connected_timeout = 0.0
        self.local_context = _FakeContext()
        self.local_kwargs: dict[str, object] | None = None

    async def connect(self, endpoint: str, *, timeout: float) -> _FakeBrowser:
        self.connected_endpoint = endpoint
        self.connected_timeout = timeout
        return self.remote_browser

    async def launch_persistent_context(
        self,
        profile: str,
        **kwargs: object,
    ) -> _FakeContext:
        self.local_kwargs = {"profile": profile, **kwargs}
        return self.local_context


def test_remote_endpoint_can_come_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = _browser_module()
    monkeypatch.setenv("FANQIE_BROWSER_WS_ENDPOINT", "ws://playwright:3000/")

    assert browser._normalize_ws_endpoint(None) == "ws://playwright:3000/"
    with pytest.raises(browser.BrowserSearchError):
        browser._normalize_ws_endpoint("https://playwright:3000/")
    with pytest.raises(browser.BrowserSearchError):
        browser._normalize_ws_endpoint("ws://user:secret@playwright:3000/")


@pytest.mark.asyncio
async def test_remote_browser_session_persists_storage_state(tmp_path: Path) -> None:
    browser = _browser_module()
    chromium = _FakeChromium()
    playwright = SimpleNamespace(chromium=chromium)

    session = await browser._open_browser_session(
        playwright,
        timeout_ms=_REMOTE_TIMEOUT_MS,
        headless=True,
        profile_path=tmp_path,
        ws_endpoint="ws://playwright:3000/",
    )
    assert chromium.connected_endpoint == "ws://playwright:3000/"
    assert chromium.connected_timeout == _REMOTE_TIMEOUT_MS
    assert session.browser is chromium.remote_browser
    assert chromium.remote_browser.context_kwargs is not None
    assert chromium.remote_browser.context_kwargs["storage_state"] is None

    await browser._close_browser_session(session)

    state = tmp_path / "storage-state.json"
    assert state.is_file()
    assert state.stat().st_mode & 0o777 == _PRIVATE_FILE_MODE
    assert chromium.remote_context.saved_path == state
    assert chromium.remote_context.indexed_db is True
    assert chromium.remote_context.closed is True
    assert chromium.remote_browser.closed is True


@pytest.mark.asyncio
async def test_local_browser_mode_keeps_persistent_context(tmp_path: Path) -> None:
    browser = _browser_module()
    chromium = _FakeChromium()
    playwright = SimpleNamespace(chromium=chromium)

    session = await browser._open_browser_session(
        playwright,
        timeout_ms=10_000,
        headless=False,
        profile_path=tmp_path,
        ws_endpoint="",
    )

    assert session.browser is None
    assert chromium.local_kwargs is not None
    assert chromium.local_kwargs["profile"] == str(tmp_path)
    assert chromium.local_kwargs["headless"] is False
    assert chromium.local_kwargs["locale"] == "zh-CN"
    await browser._close_browser_session(session)
    assert chromium.local_context.closed is True
