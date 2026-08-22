from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from nonebot.adapters.onebot.v11 import Message

ROOT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "plugins"
    / "yawn_core"
    / "yawn_agent"
)


def _install_package_shim() -> None:
    package = sys.modules.setdefault("yawn_core", types.ModuleType("yawn_core"))
    package.__path__ = [str(ROOT.parent)]  # pyright: ignore[reportAttributeAccessIssue]
    agent_package = sys.modules.setdefault(
        "yawn_core.yawn_agent", types.ModuleType("yawn_core.yawn_agent")
    )
    agent_package.__path__ = [str(ROOT)]  # pyright: ignore[reportAttributeAccessIssue]


def _load(name: str):
    _install_package_shim()
    module_name = f"yawn_core.yawn_agent.{name}"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, ROOT / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_context = _load("context")
_parser = _load("message_parser")
_collector = _load("collector")
ActivitySnapshot = _context.ActivitySnapshot
coldness_score = _context.coldness_score
normalize_message = _parser.normalize_message


def test_normalize_media_at_and_forward_placeholders() -> None:
    message = Message(
        "hello[CQ:image,file=x.jpg,url=https://example.test/x.jpg]"
        "[CQ:file,file=x.pdf,name=x.pdf]"
    )
    normalized = normalize_message(message)

    assert "hello" in normalized.plain_text
    assert any(item["type"] in {"image", "file"} for item in normalized.media_refs)
    assert normalized.prompt_text()


def test_coldness_increases_after_idle_period() -> None:
    now = datetime(2026, 1, 1, 12, 0)  # noqa: DTZ001
    active = ActivitySnapshot(
        now - timedelta(minutes=1), messages_5m=5, messages_20m=10
    )
    idle = ActivitySnapshot(now - timedelta(minutes=60))
    assert coldness_score(idle, now) > coldness_score(active, now)


def test_proactive_policy_inputs_are_available() -> None:
    now = datetime(2026, 1, 1, 12, 0)  # noqa: DTZ001
    snapshot = ActivitySnapshot(now - timedelta(hours=1), proactive_today=0)
    assert coldness_score(snapshot, now) > 0.6  # noqa: PLR2004


@pytest.mark.asyncio
async def test_agent_queue_processes_valid_items_in_fifo_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_collector, "DEBOUNCE_SECONDS", 0.0)
    _collector.reset_for_tests()
    processed: list[str] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_done = asyncio.Event()

    async def process(
        _bot: object,
        _event: object,
        normalized: str,
        *,
        enqueued_at: float,
    ) -> None:
        assert enqueued_at > 0
        processed.append(normalized)
        if normalized == "first":
            first_started.set()
            await release_first.wait()
        else:
            second_done.set()

    bot = types.SimpleNamespace(self_id="100")
    first = (bot, types.SimpleNamespace(message_id=1), "first")
    second = (bot, types.SimpleNamespace(message_id=2), "second")
    try:
        assert _collector.enqueue(1, first, 100)
        _collector.ensure_worker(1, process, 100)
        await asyncio.wait_for(first_started.wait(), timeout=1)
        assert _collector.enqueue(1, second, 100)
        release_first.set()
        await asyncio.wait_for(second_done.wait(), timeout=1)
        assert processed == ["first", "second"]
    finally:
        _collector.reset_for_tests()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_agent_queue_drops_expired_items_after_slow_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_collector, "DEBOUNCE_SECONDS", 0.0)
    monkeypatch.setattr(_collector, "PENDING_TRIGGER_TTL_SECONDS", 0.05)
    _collector.reset_for_tests()
    processed: list[str] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def process(
        _bot: object,
        _event: object,
        normalized: str,
        *,
        enqueued_at: float,
    ) -> None:
        assert enqueued_at > 0
        processed.append(normalized)
        if normalized == "first":
            first_started.set()
            await release_first.wait()

    bot = types.SimpleNamespace(self_id="100")
    first = (bot, types.SimpleNamespace(message_id=1), "first")
    second = (bot, types.SimpleNamespace(message_id=2), "expired")
    try:
        assert _collector.enqueue(1, first, 100)
        _collector.ensure_worker(1, process, 100)
        await asyncio.wait_for(first_started.wait(), timeout=1)
        assert _collector.enqueue(1, second, 100)
        await asyncio.sleep(0.08)
        release_first.set()
        await asyncio.sleep(0.08)
        assert processed == ["first"]
    finally:
        _collector.reset_for_tests()
        await asyncio.sleep(0)
