# ruff: noqa: E501, FBT001, PLR2004, UP031
"""模型调用可靠性：并发分道、失败原因分类、统一重试、群聊收尾轮。"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest
import test_agent_group_dialogue as base
from openai import OpenAIError

from src.plugins.yawn_core import llm
from src.plugins.yawn_core.yawn_agent import (
    dialogue,
    llm_retry,
    prompt_sections,
    turn_budget,
)

# ── 失败原因分类 ───────────────────────────────────────────


class _StatusError(OpenAIError):
    def __init__(self, status_code: int, text: str = "boom") -> None:
        super().__init__(text)
        self.status_code = status_code


@pytest.mark.parametrize(
    ("status", "text", "expected"),
    [
        (429, "rate limit reached", "rate_limited"),
        (429, "Insufficient Balance", "quota"),
        (402, "payment required", "quota"),
        (401, "invalid api key", "auth"),
        (403, "forbidden", "auth"),
        (400, "messages[3] is invalid", "bad_request"),
        (400, "This model's maximum context length is 65536 tokens", "context_length"),
        (413, "payload too large", "context_length"),
        (404, "model not found", "bad_request"),
        (500, "internal error", "server"),
        (503, "overloaded", "server"),
    ],
)
def test_classify_llm_error_by_status(status: int, text: str, expected: str) -> None:
    assert llm.classify_llm_error(_StatusError(status, text)) == expected


def test_classify_llm_error_without_status() -> None:
    assert llm.classify_llm_error(asyncio.TimeoutError()) == "timeout"
    assert llm.classify_llm_error(OpenAIError("weird")) == "unknown"


def _fake_client(create: Any) -> Any:
    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )


def _ok_response(content: str = "好") -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "kind", "retryable"),
    [(429, "rate_limited", True), (500, "server", True), (401, "auth", False)],
)
async def test_tool_completion_reports_error_kind(
    monkeypatch: pytest.MonkeyPatch, status: int, kind: str, retryable: bool
) -> None:
    async def create(**_kwargs: Any) -> Any:
        raise _StatusError(status)

    monkeypatch.setattr(llm, "get_client", lambda *_a, **_k: _fake_client(create))
    result = await llm.complete_with_tools_result(
        [{"role": "user", "content": "hi"}], [], timeout=1
    )
    assert result.message is None
    assert result.outcome == "error"
    assert result.error_kind == kind
    assert result.retryable is retryable


@pytest.mark.asyncio
async def test_empty_and_unconfigured_error_kinds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def create(**_kwargs: Any) -> Any:
        return _ok_response("")

    monkeypatch.setattr(llm, "get_client", lambda *_a, **_k: _fake_client(create))
    empty = await llm.complete_with_tools_result(
        [{"role": "user", "content": "hi"}], [], timeout=1
    )
    assert (empty.outcome, empty.error_kind, empty.retryable) == ("empty", "empty", True)

    monkeypatch.setattr(llm, "get_client", lambda *_a, **_k: None)
    missing = await llm.complete_with_tools_result(
        [{"role": "user", "content": "hi"}], [], timeout=1
    )
    assert missing.error_kind == "not_configured"
    assert missing.retryable is False


@pytest.mark.asyncio
async def test_complete_records_last_error_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def create(**_kwargs: Any) -> Any:
        raise _StatusError(429)

    monkeypatch.setattr(llm, "get_client", lambda *_a, **_k: _fake_client(create))
    assert await llm.complete([{"role": "user", "content": "hi"}], timeout=1) is None
    assert llm.last_completion_outcome() == "error"
    assert llm.last_completion_error_kind() == "rate_limited"


# ── 并发分道 ───────────────────────────────────────────────


def test_resolve_priority_order() -> None:
    assert llm.resolve_priority("agent_dialogue") == "interactive"
    assert llm.resolve_priority("agent_memory") == "background"
    assert llm.resolve_priority("agent_proactive") == "background"
    assert llm.resolve_priority("agent_memory", "interactive") == "interactive"
    with llm.background_priority():
        assert llm.resolve_priority("agent_dialogue") == "background"
        assert llm.resolve_priority("agent_dialogue", "interactive") == "interactive"
    assert llm.resolve_priority("agent_dialogue") == "interactive"


@pytest.mark.asyncio
async def test_background_calls_leave_room_for_interactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """后台调用再多也只占后台闸的名额；实时聊天不用排在它们后面。"""

    monkeypatch.setattr(llm, "_COMPLETION_CONCURRENCY", asyncio.Semaphore(3))
    monkeypatch.setattr(llm, "_BACKGROUND_CONCURRENCY", asyncio.Semaphore(2))
    release = asyncio.Event()
    running: list[str] = []
    peak = {"background": 0}
    active = {"background": 0}

    async def create(**kwargs: Any) -> Any:
        lane = "background" if kwargs["messages"][0]["content"] == "bg" else "chat"
        running.append(lane)
        if lane == "background":
            active["background"] += 1
            peak["background"] = max(peak["background"], active["background"])
            await release.wait()
            active["background"] -= 1
        return _ok_response()

    monkeypatch.setattr(llm, "get_client", lambda *_a, **_k: _fake_client(create))

    background = [
        asyncio.create_task(
            llm.complete_with_tools_result(
                [{"role": "user", "content": "bg"}],
                [],
                timeout=5,
                priority="background",
            )
        )
        for _ in range(6)
    ]
    await asyncio.sleep(0.05)
    assert running.count("background") == 2

    chat = await asyncio.wait_for(
        llm.complete_with_tools_result(
            [{"role": "user", "content": "chat"}], [], timeout=5
        ),
        timeout=1,
    )
    assert chat.outcome == "success"

    release.set()
    results = await asyncio.gather(*background)
    assert all(item.outcome == "success" for item in results)
    assert peak["background"] == 2
    assert not llm._COMPLETION_CONCURRENCY.locked()
    assert not llm._BACKGROUND_CONCURRENCY.locked()


@pytest.mark.asyncio
async def test_background_queue_timeout_releases_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = asyncio.Semaphore(1)
    monkeypatch.setattr(llm, "_BACKGROUND_CONCURRENCY", gate)

    async def create(**_kwargs: Any) -> Any:
        raise AssertionError("排队超时时不应发起请求")

    monkeypatch.setattr(llm, "get_client", lambda *_a, **_k: _fake_client(create))
    before = llm.concurrency_snapshot()["lanes"]["background"]["queue_timeouts"]
    await gate.acquire()
    try:
        result = await llm.complete_with_tools_result(
            [{"role": "user", "content": "hi"}], [], timeout=0.05, priority="background"
        )
    finally:
        gate.release()
    assert result.outcome == "timeout"
    assert result.retryable is True
    assert not gate.locked()
    assert not llm._COMPLETION_CONCURRENCY.locked()
    after = llm.concurrency_snapshot()["lanes"]["background"]["queue_timeouts"]
    assert after == before + 1


@pytest.mark.asyncio
async def test_request_observer_sees_lane_and_error_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def create(**_kwargs: Any) -> Any:
        raise _StatusError(503)

    monkeypatch.setattr(llm, "get_client", lambda *_a, **_k: _fake_client(create))
    seen: list[dict[str, Any]] = []
    llm.add_request_observer(seen.append)
    try:
        await llm.complete_with_tools_result(
            [{"role": "user", "content": "hi"}], [], timeout=1, task="agent_memory"
        )
    finally:
        llm.remove_request_observer(seen.append)
    assert seen[-1]["priority"] == "background"
    assert seen[-1]["error_kind"] == "server"


# ── 统一重试 ───────────────────────────────────────────────


def _failed(kind: str, outcome: str = "error") -> llm.LLMToolCompletionResult:
    return llm.LLMToolCompletionResult(
        message=None,
        finish_reason=None,
        prompt_tokens=None,
        completion_tokens=None,
        cached_tokens=None,
        cache_miss_tokens=None,
        outcome=outcome,
        duration_ms=1.0,
        error_kind=kind,
    )


def _succeeded(content: str = "好") -> Any:
    return base._completion(content)


def _calls(results: list[Any]) -> tuple[Any, list[int]]:
    count: list[int] = []

    async def call() -> Any:
        count.append(1)
        return results[min(len(count), len(results)) - 1]

    return call, count


@pytest.mark.asyncio
async def test_retry_once_on_transient_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(llm_retry.asyncio, "sleep", fake_sleep)
    notes: list[tuple[str, int]] = []
    call, count = _calls([_failed("rate_limited"), _succeeded()])
    result = await llm_retry.complete_with_retry(
        call, on_retry=lambda failed, n: notes.append((failed.error_kind, n))
    )
    assert result.message is not None
    assert len(count) == 2
    assert slept == [llm_retry.RETRY_PAUSE_SECONDS["rate_limited"]]
    assert notes == [("rate_limited", 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["auth", "quota", "bad_request", "context_length", "not_configured"]
)
async def test_no_retry_when_it_cannot_help(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    async def fake_sleep(_seconds: float) -> None:
        raise AssertionError("不该等待重试")

    monkeypatch.setattr(llm_retry.asyncio, "sleep", fake_sleep)
    call, count = _calls([_failed(kind), _succeeded()])
    result = await llm_retry.complete_with_retry(call)
    assert result.message is None
    assert len(count) == 1


@pytest.mark.asyncio
async def test_retry_gives_up_after_limit_and_respects_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(llm_retry.asyncio, "sleep", fake_sleep)
    call, count = _calls([_failed("timeout", "timeout")])
    result = await llm_retry.complete_with_retry(call, retries=2)
    assert result.message is None
    assert len(count) == 3

    # 回合期限快到了：再试也只会再超时一次，直接返回。
    call, count = _calls([_failed("timeout", "timeout"), _succeeded()])
    result = await llm_retry.complete_with_retry(call, deadline=time.monotonic() + 5)
    assert result.message is None
    assert len(count) == 1


def test_legacy_results_without_error_kind_still_retry() -> None:
    assert llm_retry.should_retry(SimpleNamespace(message=None, outcome="timeout"))
    assert not llm_retry.should_retry(SimpleNamespace(message=None, outcome="cancelled"))
    assert not llm_retry.should_retry(_succeeded())
    assert llm_retry.error_kind(SimpleNamespace(message=None, outcome="empty")) == "empty"


# ── 群聊：抖动重试 + 收尾轮 ────────────────────────────────


def _tool_call(index: int) -> Any:
    return SimpleNamespace(
        id=f"call_{index}",
        type="function",
        function=SimpleNamespace(
            name="discover_tools", arguments='{"query": "不存在的能力%d"}' % index
        ),
    )


def _tool_completion(index: int) -> Any:
    result = base._completion("")
    result.message = SimpleNamespace(content="", tool_calls=[_tool_call(index)])
    result.finish_reason = "tool_calls"
    return result


async def _run_turn(
    monkeypatch: pytest.MonkeyPatch, fake_complete: Any
) -> list[Any]:
    engine, maker = await base._dialogue_db((base.ALICE, 1, "帮我查一下"))
    sent: list[Any] = []

    async def fake_send(_bot: Any, _group_id: int, prepared: Any, **_kwargs: Any) -> Any:
        sent.append(prepared)
        return base.outbound.SendResult(
            sent=True,
            message_id=999,
            normalized_text=prepared.normalized_text,
            segment_types=tuple(item["type"] for item in prepared.segment_records),
        )

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(llm_retry.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(dialogue, "get_session", base._SessionFactory(maker))
    monkeypatch.setattr(dialogue, "complete_with_tools_result", fake_complete)
    monkeypatch.setattr(dialogue, "send_prepared_outbound", fake_send)
    monkeypatch.setenv("AGENT_AI_WAIT_NOTICE_DELAY", "0")
    monkeypatch.setenv("AGENT_ROUTER_ENABLED", "false")
    bot = SimpleNamespace(self_id=str(base.BOT_ID))
    try:
        await dialogue.process_group_message(
            bot, base._event(base.ALICE, 1), base._text("帮我查一下"), enqueued_at=None
        )
    finally:
        await engine.dispose()
    return sent


@pytest.mark.asyncio
async def test_group_turn_retries_once_when_model_hiccups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []

    async def fake_complete(_messages: Any, _tools: Any, **_kwargs: Any) -> Any:
        attempts.append(1)
        if len(attempts) == 1:
            return _failed("server")
        return base._completion("查到了，是这样的")

    sent = await _run_turn(monkeypatch, fake_complete)
    assert len(attempts) == 2
    assert len(sent) == 1
    assert "查到了" in sent[0].normalized_text


@pytest.mark.asyncio
async def test_group_turn_does_not_retry_auth_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []

    async def fake_complete(_messages: Any, _tools: Any, **_kwargs: Any) -> Any:
        attempts.append(1)
        return _failed("auth")

    sent = await _run_turn(monkeypatch, fake_complete)
    assert len(attempts) == 1
    # 仍然要给成员一个交代，不能静默。
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_group_turn_wraps_up_with_results_when_rounds_run_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """轮次全被工具调用耗尽：不再发固定的“稍后再聊”，而是禁用工具请模型据已有结果作答。"""

    calls: list[dict[str, Any]] = []

    async def fake_complete(messages: Any, _tools: Any, **kwargs: Any) -> Any:
        calls.append({"tool_choice": kwargs.get("tool_choice"), "messages": list(messages)})
        if kwargs.get("tool_choice") == "none":
            return base._completion("目前只查到一部分，还差最后一步")
        return _tool_completion(len(calls))

    sent = await _run_turn(monkeypatch, fake_complete)
    assert calls[-1]["tool_choice"] == "none"
    assert all(item["tool_choice"] is None for item in calls[:-1])
    assert any(
        item.get("role") == "system" and item.get("content") == turn_budget.WRAP_UP_NOTE
        for item in calls[-1]["messages"]
    )
    assert len(sent) == 1
    assert "只查到一部分" in sent[0].normalized_text
    assert dialogue._TURN_END_NOTICE not in sent[0].normalized_text


@pytest.mark.asyncio
async def test_group_turn_falls_back_to_notice_when_wrap_up_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []

    async def fake_complete(_messages: Any, _tools: Any, **kwargs: Any) -> Any:
        calls.append(kwargs.get("tool_choice"))
        if kwargs.get("tool_choice") == "none":
            return _failed("timeout", "timeout")
        return _tool_completion(len(calls))

    sent = await _run_turn(monkeypatch, fake_complete)
    # 收尾轮本身不重试。
    assert calls.count("none") == 1
    assert len(sent) == 1
    assert sent[0].normalized_text == dialogue._TURN_END_NOTICE


def test_wrap_up_note_is_registered_prompt_section() -> None:
    assert prompt_sections.get_section("tools.wrap_up") is not None
    assert turn_budget.wrap_up_messages() == [
        {"role": "system", "content": turn_budget.WRAP_UP_NOTE}
    ]
