"""P1-6 运行指标与运行时埋点回归测试。"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nonebot
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PLAY_PHASE_SECONDS = 3.0
EXPECTED_EVENT_LOG_REJECTIONS = 2
EXPECTED_COMPLETE_REQUESTS = 4
EXPECTED_SUCCESS_REQUESTS = 2
EXPECTED_INPUT_TOKENS = 240
EXPECTED_OUTPUT_TOKENS = 40
EXPECTED_CACHED_TOKENS = 160
EXPECTED_CACHE_MISS_TOKENS = 80
EXPECTED_AGENT_CONTEXT_QUERIES = 11
EXPECTED_AGENT_TOOL_ROUNDS = 2
EXPECTED_TOOL_SCHEMA_CHARS = 4200
EXPECTED_SELECTED_TOOLS = 7
EXPECTED_EXPOSED_TOOLS = 6
EXPECTED_PROVIDER_CACHED = 900
EXPECTED_PROVIDER_CACHE_MISS = 300


@pytest.fixture(scope="module")
def runtime_modules() -> dict[str, Any]:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()
    if nonebot.get_plugin("yawn_core") is None:
        nonebot.load_from_toml("pyproject.toml")
    return {
        "metrics": importlib.import_module("src.plugins.yawn_core.metrics"),
        "event_log": importlib.import_module("src.plugins.yawn_core.event_log"),
        "llm": importlib.import_module("src.plugins.yawn_core.llm"),
        "chat_state": importlib.import_module("src.plugins.yawn_core.chat_state"),
        "rpg_state": importlib.import_module(
            "src.plugins.yawn_core.yawn_rpg.state"
        ),
        "ww_state": importlib.import_module(
            "src.plugins.yawn_core.yawn_werewolf.state"
        ),
    }


@pytest.fixture(autouse=True)
def reset_metrics(runtime_modules: dict[str, Any]) -> Any:
    metrics = runtime_modules["metrics"]
    metrics.reset_metrics_for_tests()
    yield


def test_agent_turn_metrics_are_low_cardinality(
    runtime_modules: dict[str, Any],
) -> None:
    metrics = runtime_modules["metrics"]
    metrics.record_agent_turn(
        "followup", "wait", 0.25, queue_wait_seconds=1.5
    )
    snapshot = metrics.snapshot_metrics()

    assert _counter(
        snapshot,
        "yawnbot_agent_turns_total",
        {"operation": "followup", "outcome": "wait"},
    ) == 1
    assert _histogram(
        snapshot,
        "yawnbot_agent_turn_duration_seconds",
        {"operation": "followup"},
    )["count"] == 1
    assert _histogram(
        snapshot,
        "yawnbot_agent_queue_wait_seconds",
        {"operation": "followup"},
    )["count"] == 1
    metrics.reset_metrics_for_tests()


def test_agent_phase_and_runtime_cost_metrics_are_low_cardinality(
    runtime_modules: dict[str, Any],
) -> None:
    metrics = runtime_modules["metrics"]
    metrics.record_agent_phase("context", 0.12)
    metrics.record_agent_phase("llm", 0.8)
    metrics.record_agent_capability_probe("success")
    metrics.record_agent_context_db_queries(EXPECTED_AGENT_CONTEXT_QUERIES)
    metrics.record_agent_tool_rounds(EXPECTED_AGENT_TOOL_ROUNDS, "dialogue")
    metrics.record_agent_tool_selection(
        schema_chars=EXPECTED_TOOL_SCHEMA_CHARS,
        selected_count=EXPECTED_SELECTED_TOOLS,
        exposed_count=EXPECTED_EXPOSED_TOOLS,
    )
    metrics.record_agent_tool_discovery("called")
    metrics.record_agent_tool_discovery("returned")
    metrics.record_agent_tool_discovery("used")
    metrics.record_agent_provider_cache_tokens(
        cached=EXPECTED_PROVIDER_CACHED,
        cache_miss=EXPECTED_PROVIDER_CACHE_MISS,
    )

    snapshot = metrics.snapshot_metrics()
    assert _histogram(
        snapshot,
        "yawnbot_agent_phase_duration_seconds",
        {"phase": "context"},
    )["count"] == 1
    assert _histogram(
        snapshot,
        "yawnbot_agent_phase_duration_seconds",
        {"phase": "llm"},
    )["count"] == 1
    assert _counter(
        snapshot,
        "yawnbot_agent_capability_probes_total",
        {"outcome": "success"},
    ) == 1
    assert _histogram(
        snapshot,
        "yawnbot_agent_context_db_queries",
        {},
    )["sum"] == float(EXPECTED_AGENT_CONTEXT_QUERIES)
    assert _histogram(
        snapshot,
        "yawnbot_agent_tool_rounds",
        {"operation": "dialogue"},
    )["sum"] == float(EXPECTED_AGENT_TOOL_ROUNDS)
    assert _histogram(snapshot, "yawnbot_agent_tool_schema_chars", {})["sum"] == float(
        EXPECTED_TOOL_SCHEMA_CHARS
    )
    selected_tools = _histogram(snapshot, "yawnbot_agent_tool_selected_count", {})
    assert selected_tools["sum"] == float(EXPECTED_SELECTED_TOOLS)
    assert _histogram(snapshot, "yawnbot_agent_tool_exposed_count", {})["sum"] == float(
        EXPECTED_EXPOSED_TOOLS
    )
    assert _counter(
        snapshot,
        "yawnbot_agent_tool_discovery_total",
        {"outcome": "called"},
    ) == 1
    assert _counter(
        snapshot,
        "yawnbot_agent_tool_discovery_total",
        {"outcome": "returned"},
    ) == 1
    assert _counter(
        snapshot,
        "yawnbot_agent_tool_discovery_total",
        {"outcome": "used"},
    ) == 1
    assert _histogram(
        snapshot,
        "yawnbot_agent_provider_cache_tokens",
        {"source": "cached"},
    )["sum"] == float(EXPECTED_PROVIDER_CACHED)
    assert _histogram(
        snapshot,
        "yawnbot_agent_provider_cache_tokens",
        {"source": "cache_miss"},
    )["sum"] == float(EXPECTED_PROVIDER_CACHE_MISS)


def test_ai_health_tracks_only_unrecovered_consecutive_failures(
    runtime_modules: dict[str, Any],
) -> None:
    metrics = runtime_modules["metrics"]
    metrics.record_ai_request("agent_proactive", "error", 0.2)
    metrics.record_ai_request("agent_proactive", "timeout", 0.3)
    metrics.record_ai_request("agent_memory", "error", 0.1)

    assert metrics.ai_health_snapshot() == [
        {
            "operation": "agent_memory",
            "consecutiveFailures": 1,
            "lastFailureOutcome": "error",
        },
        {
            "operation": "agent_proactive",
            "consecutiveFailures": 2,
            "lastFailureOutcome": "timeout",
        },
    ]

    # 成功只关闭对应 operation 的当前故障；累计 counter 仍保留历史错误。
    metrics.record_ai_request("agent_proactive", "success", 0.1)
    assert metrics.ai_health_snapshot() == [
        {
            "operation": "agent_memory",
            "consecutiveFailures": 1,
            "lastFailureOutcome": "error",
        }
    ]
    snapshot = metrics.snapshot_metrics()
    assert _counter(
        snapshot,
        "yawnbot_ai_requests_total",
        {"operation": "agent_proactive", "outcome": "error"},
    ) == 1


def _counter(
    snapshot: dict[str, object],
    name: str,
    labels: dict[str, str],
) -> int:
    for item in snapshot["counters"]:  # type: ignore[union-attr]
        if item["name"] == name and item["labels"] == labels:  # type: ignore[index]
            return int(item["value"])  # type: ignore[index]
    return 0


def _histogram(
    snapshot: dict[str, object],
    name: str,
    labels: dict[str, str],
) -> dict[str, object]:
    for item in snapshot["histograms"]:  # type: ignore[union-attr]
        if item["name"] == name and item["labels"] == labels:  # type: ignore[index]
            return item  # type: ignore[return-value]
    return {}


def test_metric_aggregation_export_and_cardinality_guard(
    runtime_modules: dict[str, Any],
) -> None:
    metrics = runtime_modules["metrics"]
    metrics.record_queue_rejection("action_queue", "rpg", "queue_full")
    metrics.record_event_log_write_failure("werewolf")
    metrics.record_ai_request("complete", "timeout", 0.25)
    metrics.record_ai_degradation("rpg", "timeout")
    metrics.record_game_ending(
        "rpg",
        outcome="good",
        ending="ending_one",
    )
    metrics.start_game_phase("rpg", "private-game-id", "SIGNUP", now=10.0)
    metrics.record_phase_change(
        "rpg",
        "private-game-id",
        "SIGNUP",
        "PLAY",
        now=12.0,
    )
    metrics.record_phase_change(
        "rpg",
        "private-game-id",
        "PLAY",
        "ENDED",
        now=15.0,
    )

    metrics.increment_counter(
        "yawnbot_forbidden_total",
        labels={"game_id": "private-game-id"},
    )
    metrics.observe_histogram(
        "yawnbot_forbidden_duration_seconds",
        1.0,
        labels={"user_id": "123456"},
    )

    snapshot = metrics.snapshot_metrics()
    assert _counter(
        snapshot,
        "yawnbot_queue_rejections_total",
        {"component": "action_queue", "game_kind": "rpg", "reason": "queue_full"},
    ) == 1
    assert _counter(
        snapshot,
        "yawnbot_event_log_write_failures_total",
        {"game_kind": "werewolf"},
    ) == 1
    assert _counter(
        snapshot,
        "yawnbot_ai_requests_total",
        {"operation": "complete", "outcome": "timeout"},
    ) == 1
    assert _counter(
        snapshot,
        "yawnbot_ai_degradations_total",
        {"component": "rpg", "reason": "timeout"},
    ) == 1
    assert _counter(
        snapshot,
        "yawnbot_game_endings_total",
        {"ending": "ending_one", "game_kind": "rpg", "outcome": "good"},
    ) == 1

    phase_histogram = _histogram(
        snapshot,
        "yawnbot_game_phase_duration_seconds",
        {"game_kind": "rpg", "phase": "PLAY"},
    )
    assert phase_histogram["count"] == 1
    assert phase_histogram["sum"] == EXPECTED_PLAY_PHASE_SECONDS
    assert "yawnbot_forbidden_total" not in {
        item["name"] for item in snapshot["counters"]  # type: ignore[union-attr]
    }

    exposition = metrics.render_prometheus()
    assert "# TYPE yawnbot_ai_requests_total counter" in exposition
    assert "# TYPE yawnbot_game_phase_duration_seconds histogram" in exposition
    assert "private-game-id" not in exposition
    assert "user_id" not in exposition


def test_game_and_chat_queue_rejections_are_observed(
    runtime_modules: dict[str, Any],
) -> None:
    rpg_state = runtime_modules["rpg_state"]
    rpg_game = rpg_state.Game(
        group_id=8001,
        host_user_id=1,
        action_queue=asyncio.Queue(maxsize=1),
    )
    assert (
        rpg_state.submit_action(
            rpg_game,
            rpg_state.Action(rpg_state.ActionKind.MOVE, 1),
            queue_max=1,
            user_pending_max=2,
            user_say_pending_max=2,
        )
        is rpg_state.SubmitResult.ACCEPTED
    )
    assert (
        rpg_state.submit_action(
            rpg_game,
            rpg_state.Action(rpg_state.ActionKind.MOVE, 2),
            queue_max=1,
            user_pending_max=2,
            user_say_pending_max=2,
        )
        is rpg_state.SubmitResult.QUEUE_FULL
    )

    ww_state = runtime_modules["ww_state"]
    ww_game = ww_state.Game(
        group_id=8002,
        host_user_id=1,
        signup_user_ids=[1],
        action_queue=asyncio.Queue(maxsize=1),
    )
    assert ww_state.submit_action(
        ww_game,
        ww_state.Action(ww_state.ActionKind.ABSTAIN, 1),
        user_pending_max=2,
    )
    assert not ww_state.submit_action(
        ww_game,
        ww_state.Action(ww_state.ActionKind.SKIP, 1),
        user_pending_max=2,
    )

    chat_state = runtime_modules["chat_state"]
    chat = chat_state.UserChatState(queue=asyncio.Queue(maxsize=1))
    assert chat_state.enqueue(chat, (None, None, None))
    assert not chat_state.enqueue(chat, (None, None, None))

    snapshot = runtime_modules["metrics"].snapshot_metrics()
    assert _counter(
        snapshot,
        "yawnbot_queue_rejections_total",
        {"component": "action_queue", "game_kind": "rpg", "reason": "queue_full"},
    ) == 1
    assert _counter(
        snapshot,
        "yawnbot_queue_rejections_total",
        {
            "component": "action_queue",
            "game_kind": "werewolf",
            "reason": "queue_full",
        },
    ) == 1
    assert _counter(
        snapshot,
        "yawnbot_queue_rejections_total",
        {"component": "chat_queue", "game_kind": "chat", "reason": "queue_full"},
    ) == 1


@pytest.mark.asyncio
async def test_event_log_phase_and_writer_queue_metrics(
    runtime_modules: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_log = runtime_modules["event_log"]
    event_log.reset_event_log_state_for_tests()
    event_log._WRITERS.clear()
    monkeypatch.setattr(event_log, "_EVENT_QUEUE_MAX", 1)

    event_log.record_event(
        "rpg",
        "metrics-game",
        "game_created",
        phase="SIGNUP",
        root=tmp_path,
    )
    event_log.record_event(
        "rpg",
        "metrics-game",
        "phase_changed",
        phase="PLAY",
        root=tmp_path,
    )
    event_log.record_event(
        "rpg",
        "metrics-game",
        "phase_changed",
        phase="ENDED",
        root=tmp_path,
    )
    await asyncio.sleep(0)

    metrics = runtime_modules["metrics"]
    snapshot = metrics.snapshot_metrics()
    assert _counter(
        snapshot,
        "yawnbot_queue_rejections_total",
        {
            "component": "event_log_writer",
            "game_kind": "rpg",
            "reason": "queue_full",
        },
    ) == EXPECTED_EVENT_LOG_REJECTIONS
    phase_histogram = _histogram(
        snapshot,
        "yawnbot_game_phase_duration_seconds",
        {"game_kind": "rpg", "phase": "SIGNUP"},
    )
    assert phase_histogram["count"] == 1

    writer = event_log._WRITERS.get(asyncio.get_running_loop())
    if writer is not None:
        writer.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await writer.task
    event_log._WRITERS.clear()


@pytest.mark.asyncio
async def test_event_log_write_failure_metric(
    runtime_modules: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_log = runtime_modules["event_log"]
    event_log.reset_event_log_state_for_tests()
    event_log._WRITERS.clear()

    def fail_write(_path: Path, _line: str) -> None:
        raise OSError

    monkeypatch.setattr(event_log, "_append_line", fail_write)
    event_log.record_event(
        "werewolf",
        "failure-game",
        "game_created",
        root=tmp_path,
    )
    await event_log.flush_events()

    snapshot = runtime_modules["metrics"].snapshot_metrics()
    assert _counter(
        snapshot,
        "yawnbot_event_log_write_failures_total",
        {"game_kind": "werewolf"},
    ) == 1

    writer = event_log._WRITERS.get(asyncio.get_running_loop())
    if writer is not None:
        writer.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await writer.task
    event_log._WRITERS.clear()


@pytest.mark.asyncio
async def test_llm_latency_timeout_and_degradation_metrics(
    runtime_modules: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = runtime_modules["llm"]
    metrics = runtime_modules["metrics"]

    class Completions:
        mode = "success"

        async def create(self, **_kwargs: object) -> Any:
            if self.mode == "timeout":
                raise asyncio.TimeoutError
            message = SimpleNamespace(content="ok", tool_calls=[])
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message, finish_reason="stop")],
                usage=SimpleNamespace(
                    prompt_tokens=120,
                    completion_tokens=20,
                    prompt_tokens_details=SimpleNamespace(cached_tokens=80),
                ),
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    monkeypatch.setattr(llm, "get_client", lambda _provider="default": client)

    messages = [{"role": "user", "content": "hello"}]
    assert await llm.complete(messages) == "ok"
    assert await llm.complete_with_tools(messages, []) is not None
    client.chat.completions.mode = "timeout"
    assert await llm.complete(messages, timeout=0.01) is None

    monkeypatch.setattr(llm, "get_client", lambda _provider="default": None)
    assert await llm.complete(messages) is None

    snapshot = metrics.snapshot_metrics()
    assert _counter(
        snapshot,
        "yawnbot_ai_requests_total",
        {"operation": "core_chat", "outcome": "success"},
    ) == EXPECTED_SUCCESS_REQUESTS
    assert _counter(
        snapshot,
        "yawnbot_ai_requests_total",
        {"operation": "core_chat", "outcome": "timeout"},
    ) == 1
    assert _counter(
        snapshot,
        "yawnbot_ai_requests_total",
        {"operation": "core_chat", "outcome": "not_configured"},
    ) == 1
    assert _counter(
        snapshot,
        "yawnbot_ai_degradations_total",
        {"component": "llm", "reason": "timeout"},
    ) == 1
    assert _counter(
        snapshot,
        "yawnbot_ai_degradations_total",
        {"component": "llm", "reason": "not_configured"},
    ) == 1
    latency = _histogram(
        snapshot,
        "yawnbot_ai_request_duration_seconds",
        {"operation": "core_chat"},
    )
    assert latency["count"] == EXPECTED_COMPLETE_REQUESTS
    assert _counter(
        snapshot,
        "yawnbot_ai_tokens_total",
        {"operation": "core_chat", "source": "input"},
    ) == EXPECTED_INPUT_TOKENS
    assert _counter(
        snapshot,
        "yawnbot_ai_tokens_total",
        {"operation": "core_chat", "source": "output"},
    ) == EXPECTED_OUTPUT_TOKENS
    assert _counter(
        snapshot,
        "yawnbot_ai_tokens_total",
        {"operation": "core_chat", "source": "cached"},
    ) == EXPECTED_CACHED_TOKENS
    assert _counter(
        snapshot,
        "yawnbot_ai_tokens_total",
        {"operation": "core_chat", "source": "cache_miss"},
    ) == EXPECTED_CACHE_MISS_TOKENS
