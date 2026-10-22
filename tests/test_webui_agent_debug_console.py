# 调试台 2.0 新增的 Trace 统计、筛选、清空与关联审计接口。
# ruff: noqa: PLR2004, PYI034
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import nonebot
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if nonebot.get_plugin("yawn_core") is None:
    nonebot.load_from_toml("pyproject.toml")

agent_routes = importlib.import_module("src.plugins.yawn_core.webui.agent")
tracing = importlib.import_module("src.plugins.yawn_core.yawn_agent.execution_trace")
audit_models = importlib.import_module("src.plugins.yawn_core.data_models.agent_audit")


def _runtime_trace(
    group_id: int,
    *,
    mode: str = "dialogue",
    trigger: str = "mention",
    actor: int = 10,
    outcome: str = "completed",
) -> object:
    trace = tracing.begin_execution_trace(
        group_id,
        mode=mode,
        source="runtime",
        trigger_source=trigger,
        actor_user_id=actor,
        message_id=99,
    )
    tracing.trace_event(
        "intake",
        "消息归一化完成",
        output={"text_preview": "  帮我看看今天谁最活跃，顺便把群公告整理一下  " * 4},
        trace=trace,
    )
    tracing.trace_event("llm", "模型调用", duration_ms=420.0, trace=trace)
    tracing.trace_event("llm", "模型调用", duration_ms=180.0, trace=trace)
    tracing.trace_event(
        "tool",
        "工具 search_group_memory",
        status="failed" if outcome == "error" else "success",
        duration_ms=35.0,
        trace=trace,
    )
    tracing.trace_event("tool", "工具意图 mute_member", status="planned", trace=trace)
    tracing.finish_execution_trace(trace, outcome=outcome)
    return trace


def test_trace_summary_carries_preview_tools_rounds_and_slowest_phase() -> None:
    group_id = 880001
    tracing.clear_execution_traces(group_id)
    _runtime_trace(group_id)

    summary = tracing.recent_execution_trace_summaries(group_id)[0]

    assert summary["preview"].startswith("帮我看看今天谁最活跃")
    assert summary["preview"].endswith("…")
    assert len(summary["preview"]) == 81
    assert summary["toolNames"] == ["search_group_memory", "mute_member"]
    assert summary["llmRounds"] == 2
    assert summary["slowestPhase"] == {
        "phase": "llm",
        "label": "模型调用",
        "durationMs": 420.0,
    }
    assert summary["truncated"] is False
    assert "events" not in summary
    tracing.clear_execution_traces(group_id)


def test_trace_summaries_filter_by_mode_trigger_actor_and_source() -> None:
    group_id = 880002
    tracing.clear_execution_traces(group_id)
    _runtime_trace(group_id, mode="dialogue", trigger="mention", actor=1)
    _runtime_trace(group_id, mode="active", trigger="proactive_interject", actor=2)
    _runtime_trace(group_id, mode="dialogue", trigger="reply", actor=2, outcome="error")

    assert len(tracing.recent_execution_trace_summaries(group_id, mode="dialogue")) == 2
    assert [
        item["triggerSource"]
        for item in tracing.recent_execution_trace_summaries(
            group_id, trigger_source="proactive_interject"
        )
    ] == ["proactive_interject"]
    assert len(tracing.recent_execution_trace_summaries(group_id, actor_user_id=2)) == 2
    combined = tracing.recent_execution_trace_summaries(
        group_id, actor_user_id=2, status="failed"
    )
    assert [item["triggerSource"] for item in combined] == ["reply"]
    assert tracing.recent_execution_trace_summaries(group_id, source="debug") == []
    tracing.clear_execution_traces(group_id)


def test_trace_stats_aggregate_buffer_without_events() -> None:
    group_id = 880003
    tracing.clear_execution_traces(group_id)
    _runtime_trace(group_id, trigger="mention")
    _runtime_trace(group_id, trigger="mention", outcome="error")
    _runtime_trace(group_id, mode="warmup", trigger="proactive_warmup")

    stats = tracing.execution_trace_stats(group_id)

    assert stats["total"] == 3
    assert stats["capacity"] == 12
    assert stats["failed"] == 1
    assert stats["withTools"] == 3
    assert stats["byMode"] == {"dialogue": 2, "warmup": 1}
    assert stats["byTrigger"] == {"mention": 2, "proactive_warmup": 1}
    assert stats["byOutcome"] == {"completed": 2, "error": 1}
    assert stats["durationMs"]["avg"] is not None
    assert stats["durationMs"]["p95"] >= stats["durationMs"]["p50"]
    llm = next(item for item in stats["phases"] if item["phase"] == "llm")
    assert llm == {
        "phase": "llm",
        "count": 6,
        "avgMs": 300.0,
        "maxMs": 420.0,
        "totalMs": 1800.0,
        "failures": 0,
    }
    tool = next(item for item in stats["phases"] if item["phase"] == "tool")
    assert tool["failures"] == 1
    assert stats["phases"][0]["phase"] == "llm"
    assert stats["tools"][0] == {"name": "mute_member", "count": 3}
    assert stats["oldestAt"] <= stats["newestAt"]

    empty = tracing.execution_trace_stats(880999)
    assert empty["total"] == 0
    assert empty["durationMs"] == {"avg": None, "p50": None, "p95": None, "max": None}
    assert empty["oldestAt"] is None
    tracing.clear_execution_traces(group_id)


class _FakeDb:
    async def __aenter__(self) -> _FakeDb:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


async def _no_group_check(_db: object, _group_id: int) -> None:
    return None


@pytest.mark.asyncio
async def test_trace_stats_and_clear_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    group_id = 880004
    tracing.clear_execution_traces(group_id)
    _runtime_trace(group_id)
    _runtime_trace(group_id, mode="active", trigger="proactive_interject")
    monkeypatch.setattr(agent_routes, "get_session", _FakeDb)
    monkeypatch.setattr(agent_routes, "require_group", _no_group_check)

    listed = await agent_routes.get_agent_execution_traces(
        group_id,
        object(),
        status=None,
        mode="active",
        trigger_source=None,
        actor_user_id=None,
        source="runtime",
    )
    assert [item["mode"] for item in listed["data"]] == ["active"]

    stats = await agent_routes.get_agent_execution_trace_stats(group_id, object())
    assert stats["data"]["total"] == 2

    cleared = await agent_routes.clear_agent_execution_traces(group_id, object())
    assert cleared["data"] == {"groupId": str(group_id), "cleared": 2}
    assert tracing.recent_execution_trace_summaries(group_id) == []
    again = await agent_routes.clear_agent_execution_traces(group_id, object())
    assert again["data"]["cleared"] == 0


@pytest.mark.asyncio
async def test_trace_audits_route_matches_hidden_trace_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: audit_models.AgentAudit.metadata.create_all(
                sync_connection, tables=[audit_models.AgentAudit.__table__]
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    def audit(tool: str, trace_id: str | None, group_id: int = 100) -> object:
        arguments: dict[str, object] = {"user_id": 5}
        if trace_id:
            arguments["_trace_id"] = trace_id
        return audit_models.AgentAudit(
            group_id=group_id,
            actor_user_id=1,
            tool_name=tool,
            arguments=arguments,
            result="success",
        )

    async with factory() as session:
        session.add_all(
            [
                audit("mute_member", "abc"),
                audit("set_essence_message", "abc"),
                audit("mute_member", "other"),
                audit("mute_member", None),
                audit("mute_member", "abc", group_id=200),
            ]
        )
        await session.commit()

    monkeypatch.setattr(agent_routes, "get_session", factory)
    monkeypatch.setattr(agent_routes, "require_group", _no_group_check)

    response = await agent_routes.get_agent_execution_trace_audits(100, "abc", object())
    rows = response["data"]
    assert [row["toolName"] for row in rows] == ["set_essence_message", "mute_member"]
    assert all(row["traceId"] == "abc" for row in rows)
    assert all("_trace_id" not in row["arguments"] for row in rows)

    with pytest.raises(agent_routes.HTTPException):
        await agent_routes.get_agent_execution_trace_audits(100, "x" * 65, object())

    await engine.dispose()
