from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from typing import Any, ClassVar

import nonebot
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _SendFailedError(RuntimeError):
    pass


@pytest.fixture(scope="module")
def ai_chat_module() -> Any:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()
    if nonebot.get_plugin("yawn_core") is None:
        nonebot.load_from_toml("pyproject.toml")
    return importlib.import_module("src.plugins.yawn_core.ai_chat")


@pytest.mark.asyncio
async def test_worker_and_direct_path_share_user_lock(
    ai_chat_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = 91001
    started = asyncio.Event()
    release = asyncio.Event()
    order: list[str] = []

    class SessionContext:
        session = object()

        async def __aenter__(self) -> object:
            return self.session

        async def __aexit__(self, *_args: object) -> None:
            return None

    async def process(
        _bot: object,
        _event: object,
        _user_id: int,
        _session: object,
        text: str,
    ) -> None:
        order.append(f"start:{text}")
        if text == "worker":
            started.set()
            await release.wait()
        order.append(f"end:{text}")

    monkeypatch.setattr(ai_chat_module, "get_session", SessionContext)
    monkeypatch.setattr(ai_chat_module, "_process_chat", process)
    event = object()
    bot = object()

    worker = asyncio.create_task(
        ai_chat_module._worker_process_chat(bot, event, user_id, "worker")
    )
    await started.wait()

    direct_task = asyncio.create_task(
        ai_chat_module._run_user_chat(
            bot,
            event,
            user_id,
            SessionContext.session,
            "direct",
        )
    )
    await asyncio.sleep(0)
    assert order == ["start:worker"]
    release.set()
    await asyncio.gather(worker, direct_task)
    assert order == [
        "start:worker",
        "end:worker",
        "start:direct",
        "end:direct",
    ]


@pytest.mark.asyncio
async def test_reset_waits_for_same_user_lock(
    ai_chat_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = 91002
    calls: list[str] = []

    class Session:
        async def rollback(self) -> None:
            calls.append("rollback")

    async def stop(_user_id: int) -> None:
        calls.append("stop")

    new_session_id = 7

    async def reset(_session: object, _user_id: int) -> int:
        calls.append("reset")
        return new_session_id

    monkeypatch.setattr(ai_chat_module, "stop_worker", stop)
    monkeypatch.setattr(ai_chat_module, "_reset_chat_session", reset)
    lock = ai_chat_module._chat_lock(user_id)
    await lock.acquire()
    task = asyncio.create_task(ai_chat_module._reset_user_chat(Session(), user_id))
    await asyncio.sleep(0)
    assert calls == []
    lock.release()
    assert await task == new_session_id
    assert calls == ["stop", "rollback", "reset"]


@pytest.mark.asyncio
async def test_partial_delivery_only_persists_sent_segments(
    ai_chat_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = "a" * ai_chat_module._SEGMENT_CHAR_LIMIT
    second = "b"

    class Delta:
        content = first + second

    class Choice:
        delta = Delta()

    class Chunk:
        choices: ClassVar[list[Choice]] = [Choice()]

    class Stream:
        def __init__(self) -> None:
            self._chunks = iter([Chunk()])

        def __aiter__(self) -> Stream:
            return self

        async def __anext__(self) -> Chunk:
            try:
                return next(self._chunks)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

        async def close(self) -> None:
            return None

    class Completions:
        async def create(self, **_kwargs: object) -> Any:
            return Stream()

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    class Bot:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, _event: object, message: Any) -> None:
            text = message.data["text"]
            if self.sent:
                raise _SendFailedError
            self.sent.append(text)

    monkeypatch.setattr(ai_chat_module, "_client", Client())
    bot = Bot()
    metrics = importlib.import_module("src.plugins.yawn_core.metrics")
    metrics.reset_metrics_for_tests()
    try:
        result = await ai_chat_module._stream_and_send_impl(bot, object(), [])

        assert bot.sent == [first]
        assert result == first
        assert any(
            item["name"] == "yawnbot_ai_requests_total"
            and item["labels"]
            == {"operation": "chat_stream", "outcome": "delivery_failed_partial"}
            and item["value"] == 1
            for item in metrics.snapshot_metrics()["counters"]
        )
    finally:
        metrics.reset_metrics_for_tests()
