# Agent 工作台新增的活动聚合、配额诊断与短会话结束接口。
# ruff: noqa: PLR2004, DTZ001, E501, PLR0913, ARG001, PYI034
from __future__ import annotations

import importlib
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

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

service = importlib.import_module("src.plugins.yawn_core.webui.service")
agent_routes = importlib.import_module("src.plugins.yawn_core.webui.agent")
message_models = importlib.import_module(
    "src.plugins.yawn_core.data_models.group_agent_message"
)

NOW = datetime(2026, 10, 22, 12, 30, 0)


@pytest.mark.asyncio
async def test_agent_activity_buckets_members_and_bot_by_hour() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: message_models.GroupAgentMessage.metadata.create_all(
                sync_connection,
                tables=[message_models.GroupAgentMessage.__table__],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    def message(
        message_id: int,
        user_id: int,
        received_at: datetime,
        *,
        role: str = "member",
        group_id: int = 100,
        name: str | None = None,
    ) -> object:
        return message_models.GroupAgentMessage(
            bot_id=9,
            message_id=message_id,
            group_id=group_id,
            user_id=user_id,
            sender_name=name,
            role=role,
            normalized_text="x",
            received_at=received_at,
        )

    async with factory() as session:
        session.add_all(
            [
                message(1, 1, NOW - timedelta(minutes=10), name="阿一"),
                message(2, 1, NOW - timedelta(minutes=5), name="阿一"),
                message(3, 2, NOW - timedelta(hours=3), name="二号"),
                message(4, 9, NOW - timedelta(minutes=4), role="bot"),
                # 窗口外与其它群的消息不计入。
                message(5, 1, NOW - timedelta(hours=30)),
                message(6, 3, NOW - timedelta(minutes=1), group_id=200),
            ]
        )
        await session.commit()

    async with factory() as session:
        result = await service.agent_activity(session, 100, now=NOW, hours=24)

    assert len(result["buckets"]) == 24
    assert result["buckets"][-1] == {
        "hour": service.iso(datetime(2026, 10, 22, 12, 0, 0)),
        "members": 2,
        "bot": 1,
    }
    assert result["buckets"][-4]["members"] == 1
    assert result["totals"] == {"members": 3, "bot": 1, "activeMembers": 2}
    assert result["topMembers"][0] == {"userId": "1", "name": "阿一", "count": 2}

    await engine.dispose()


@pytest.mark.asyncio
async def test_agent_diagnostics_reports_tool_quota_and_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    today = datetime.now(service.BEIJING_TZ).strftime("%Y-%m-%d")
    config = SimpleNamespace(
        enabled=True,
        reply_trigger_enabled=True,
        explicit_wakeup_enabled=True,
        proactive_enabled=False,
        proactive_active_enabled=False,
        short_conversation_enabled=True,
        daily_limit=30,
        cooldown_minutes=8,
        last_agent_at=None,
        last_proactive_at=None,
        active_topic=None,
        media_cache_enabled=False,
        proactive_count=0,
        proactive_day=None,
        tool_day=today,
        admin_tool_count=4,
        admin_tool_daily_limit=20,
        critical_tool_count=1,
        critical_tool_daily_limit=3,
        tool_allowlist=["mute_member"],
    )

    class FakeSession:
        async def get(self, _model: object, _group_id: int) -> object:
            return config

    async def fake_runtime_enabled(*_args: object, **_kwargs: object) -> bool:
        return True

    async def fake_memory_status(_session: object, group_id: int) -> dict[str, object]:
        return {"rebuildRequired": False, "consecutiveFailures": 0}

    routes = [
        {"task": task, "configured": True}
        for task in ("agent_dialogue", "agent_proactive", "agent_memory", "agent_image")
    ]
    monkeypatch.setattr(service, "agent_runtime_enabled", fake_runtime_enabled)
    monkeypatch.setattr(service, "agent_memory_status", fake_memory_status)
    monkeypatch.setattr(
        service,
        "_llm_runtime_status",
        lambda: {"routes": routes, "unconfiguredProviders": []},
    )
    monkeypatch.setattr(service, "get_bots", lambda: {"9": object()})
    monkeypatch.setattr(
        service, "current_conversation", lambda _bot_id, _group_id: None
    )

    result = await service.agent_diagnostics(FakeSession(), 100)

    assert result["tools"] == {
        "adminToday": 4,
        "adminLimit": 20,
        "criticalToday": 1,
        "criticalLimit": 3,
        "allowlist": ["mute_member"],
    }
    assert result["pipeline"]["callContinuationSeconds"] == 6.0
    assert result["pipeline"]["conversationDirectQuietSeconds"] == 8.0
    assert result["pipeline"]["conversationQuietSeconds"] == 20.0

    config.tool_day = "2000-01-01"
    stale = await service.agent_diagnostics(FakeSession(), 100)
    assert stale["tools"]["adminToday"] == 0
    assert stale["tools"]["criticalToday"] == 0


@pytest.mark.asyncio
async def test_close_agent_conversation_closes_runtime_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[tuple[int, str]] = []
    notified: list[tuple[str, object]] = []

    class FakeDb:
        async def __aenter__(self) -> FakeDb:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    async def fake_require_group(_db: object, _group_id: int) -> None:
        return None

    async def fake_notify(resource: str, _entity: object = None, **kwargs: object) -> None:
        notified.append((resource, kwargs.get("group_id")))

    def fake_close(group_id: int, *, reason: str) -> int:
        closed.append((group_id, reason))
        return 1

    monkeypatch.setattr(agent_routes, "get_session", FakeDb)
    monkeypatch.setattr(agent_routes, "require_group", fake_require_group)
    monkeypatch.setattr(agent_routes, "close_group_conversations", fake_close)
    monkeypatch.setattr(agent_routes.hub, "notify_change", fake_notify)

    response = await agent_routes.close_agent_conversation(100, object())

    assert response["data"] == {"groupId": "100", "closed": 1}
    assert closed and closed[0][0] == 100
    assert notified == [("agent_config", 100)]


@pytest.mark.asyncio
async def test_agent_tool_usage_groups_by_tool_and_day() -> None:
    audit_models = importlib.import_module(
        "src.plugins.yawn_core.data_models.agent_audit"
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: audit_models.AgentAudit.metadata.create_all(
                sync_connection, tables=[audit_models.AgentAudit.__table__]
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    def audit(tool: str, result: str, created_at: datetime, group_id: int = 100) -> object:
        return audit_models.AgentAudit(
            group_id=group_id,
            actor_user_id=1,
            tool_name=tool,
            arguments={},
            result=result,
            created_at=created_at,
        )

    async with factory() as session:
        session.add_all(
            [
                audit("mute_member", "success", NOW - timedelta(hours=1)),
                audit("mute_member", "error", NOW - timedelta(days=1)),
                audit("set_essence_message", "success", NOW - timedelta(days=2)),
                audit("mute_member", "success", NOW - timedelta(days=10)),
                audit("mute_member", "success", NOW, group_id=200),
                # 主动发言从不写字面量 success：speak 是成功，wait/close 是跳过。
                audit("proactive_reply", "speak", NOW - timedelta(hours=2)),
                audit("proactive_reply", "wait", NOW - timedelta(hours=3)),
                audit("proactive_reply", "close", NOW - timedelta(hours=3)),
                audit("proactive_reply", "delivery_unknown", NOW - timedelta(hours=4)),
            ]
        )
        await session.commit()

    async with factory() as session:
        result = await service.agent_tool_usage(session, 100, now=NOW, days=7)

    assert result["total"] == 7
    assert result["failed"] == 1
    assert result["skipped"] == 2
    assert result["unknown"] == 1
    assert result["tools"][0] == {
        "toolName": "proactive_reply",
        "success": 1,
        "skipped": 2,
        "failed": 0,
        "unknown": 1,
        "total": 4,
    }
    assert result["tools"][1] == {
        "toolName": "mute_member",
        "success": 1,
        "skipped": 0,
        "failed": 1,
        "unknown": 0,
        "total": 2,
    }
    assert [day["day"] for day in result["daily"]][-1] == "2026-10-22"
    assert result["daily"][-1] == {
        "day": "2026-10-22",
        "success": 2,
        "skipped": 2,
        "failed": 0,
        "unknown": 1,
    }
    assert result["daily"][-2]["failed"] == 1

    await engine.dispose()
