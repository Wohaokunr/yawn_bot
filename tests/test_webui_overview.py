# 测试断言大量使用魔法数字与 naive 北京时间（与库内约定一致）。
# ruff: noqa: PLR2004, DTZ001
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
metrics = importlib.import_module("src.plugins.yawn_core.metrics")

bot_group_models = importlib.import_module(
    "src.plugins.yawn_core.data_models.bot_group"
)
bot_user_models = importlib.import_module(
    "src.plugins.yawn_core.data_models.bot_user"
)
audit_models = importlib.import_module(
    "src.plugins.yawn_core.data_models.agent_audit"
)
config_models = importlib.import_module(
    "src.plugins.yawn_core.data_models.group_agent_config"
)
group_feature_models = importlib.import_module(
    "src.plugins.yawn_core.data_models.group_feature"
)
message_models = importlib.import_module(
    "src.plugins.yawn_core.data_models.group_agent_message"
)
reminder_models = importlib.import_module(
    "src.plugins.yawn_core.data_models.scheduled_reminder"
)

# naive 北京时间，与库内时间列的约定一致。
NOW = datetime(2026, 8, 23, 12, 0, 0)


@pytest.fixture(autouse=True)
def _reset_overview_caches() -> None:
    metrics.reset_metrics_for_tests()
    service.reset_stats_cache_for_tests()


def test_summarize_ai_metrics_derives_health_from_snapshot() -> None:
    metrics.record_ai_request("chat", "success", 0.3)
    metrics.record_ai_request("chat", "success", 0.8)
    metrics.record_ai_request("chat", "timeout", 5.0)
    metrics.record_ai_degradation("chat", "timeout")

    summary = metrics.summarize_ai_metrics(metrics.snapshot_metrics())

    assert summary["requestsTotal"] == 3
    assert summary["success"] == 2
    assert summary["failed"] == 1
    assert summary["successRate"] == pytest.approx(2 / 3)
    assert summary["degradations"] == 1
    assert summary["avgDurationMs"] == pytest.approx(2033.33, rel=1e-3)
    # 3 个观测的 p95 落在覆盖最大值 5s 的 bucket。
    assert summary["p95DurationMs"] == pytest.approx(5000.0)
    assert summary["byOutcome"] == [
        {"outcome": "success", "count": 2},
        {"outcome": "timeout", "count": 1},
    ]


def test_summarize_ai_metrics_empty_snapshot_returns_null_fields() -> None:
    summary = metrics.summarize_ai_metrics(metrics.snapshot_metrics())

    assert summary["requestsTotal"] == 0
    assert summary["success"] == 0
    assert summary["failed"] == 0
    assert summary["successRate"] is None
    assert summary["avgDurationMs"] is None
    assert summary["p95DurationMs"] is None
    assert summary["byOutcome"] == []
    assert summary["degradations"] == 0


@pytest.mark.asyncio
async def test_cached_db_stats_reuses_result_within_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[datetime] = []

    async def fake_db_stats(now: datetime) -> dict[str, object]:
        calls.append(now)
        return {"activity": {"messages24h": len(calls)}}

    monkeypatch.setattr(service, "_db_stats", fake_db_stats)

    first = await service._cached_db_stats(NOW)
    second = await service._cached_db_stats(NOW)
    assert first is second
    assert len(calls) == 1

    # 模拟 TTL 过期后应重新聚合。
    service._stats_state["expires_at"] = 0.0
    third = await service._cached_db_stats(NOW)
    assert len(calls) == 2
    assert third["activity"]["messages24h"] == 2


@pytest.mark.asyncio
async def test_db_stats_aggregates_activity_memory_and_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        bot_group_models.BotGroup.__table__,
        config_models.GroupAgentConfig.__table__,
        message_models.GroupAgentMessage.__table__,
        reminder_models.ScheduledReminder.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: config_models.GroupAgentConfig.metadata.create_all(
                sync_connection, tables=tables
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(service, "get_session", factory)

    today = NOW.strftime("%Y-%m-%d")
    async with factory() as session:
        session.add_all(
            [
                bot_group_models.BotGroup(group_id=100, group_name="群一"),
                bot_group_models.BotGroup(group_id=200, group_name="群二"),
                config_models.GroupAgentConfig(
                    group_id=100,
                    cross_group_visibility="isolated",
                    proactive_day=today,
                    proactive_count=3,
                    tool_day=today,
                    admin_tool_count=2,
                    last_response_at=NOW - timedelta(hours=1),
                    memory_rebuild_required=True,
                    memory_consecutive_failures=2,
                    memory_last_error="compact failed",
                    memory_last_attempt_at=NOW - timedelta(hours=2),
                ),
                config_models.GroupAgentConfig(
                    group_id=200,
                    cross_group_visibility="isolated",
                    proactive_day="2026-08-01",
                    proactive_count=9,
                ),
                message_models.GroupAgentMessage(
                    bot_id=9,
                    message_id=1,
                    group_id=100,
                    user_id=1,
                    normalized_text="a",
                    received_at=NOW - timedelta(hours=23),
                ),
                message_models.GroupAgentMessage(
                    bot_id=9,
                    message_id=2,
                    group_id=100,
                    user_id=1,
                    normalized_text="b",
                    received_at=NOW - timedelta(hours=25),
                ),
                message_models.GroupAgentMessage(
                    bot_id=9,
                    message_id=3,
                    group_id=200,
                    user_id=2,
                    normalized_text="c",
                    received_at=NOW - timedelta(minutes=5),
                ),
                reminder_models.ScheduledReminder(
                    group_id=100,
                    creator_user_id=1,
                    name="坏提醒",
                    target_type="group",
                    message_segments=[],
                    enabled=True,
                    last_error="boom",
                ),
                reminder_models.ScheduledReminder(
                    group_id=100,
                    creator_user_id=1,
                    name="好提醒",
                    target_type="group",
                    message_segments=[],
                    enabled=True,
                    last_error=None,
                ),
                reminder_models.ScheduledReminder(
                    group_id=200,
                    creator_user_id=2,
                    name="停用但有错",
                    target_type="group",
                    message_segments=[],
                    enabled=False,
                    last_error="ignored",
                ),
            ]
        )
        await session.commit()

    stats = await service._db_stats(NOW)

    activity = stats["activity"]
    assert activity["messages24h"] == 2
    assert activity["activeGroups24h"] == 2
    assert activity["agentResponseGroups24h"] == 1
    assert activity["proactiveToday"] == 3
    assert activity["adminToolToday"] == 2

    memory_stats = stats["memory"]
    assert memory_stats["rebuildRequired"] == 1
    assert memory_stats["failingGroups"] == 1
    assert memory_stats["recentError"] == {
        "groupId": "100",
        "error": "compact failed",
        "at": service.iso(NOW - timedelta(hours=2)),
    }

    assert stats["jobs"]["reminderErrors"] == 1

    # 跑团/狼人杀/番茄表未在该引擎建表：查询失败时应降级而不是抛错。
    assert stats["games"]["endedToday"] == {"rpg": None, "werewolf": None}
    assert stats["jobs"]["fanqie"] == {"available": False, "byStatus": {}}

    await engine.dispose()


@pytest.mark.asyncio
async def test_overview_payload_keeps_legacy_fields_and_adds_stats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        bot_group_models.BotGroup.__table__,
        bot_user_models.BotUser.__table__,
        audit_models.AgentAudit.__table__,
        config_models.GroupAgentConfig.__table__,
        group_feature_models.GroupFeature.__table__,
        message_models.GroupAgentMessage.__table__,
        reminder_models.ScheduledReminder.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: config_models.GroupAgentConfig.metadata.create_all(
                sync_connection, tables=tables
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(service, "get_session", factory)
    monkeypatch.setattr(service, "get_bots", lambda: {"10001": object()})

    metrics.record_ai_request("chat", "success", 0.4)

    payload = await service.overview()

    assert payload["bots"] == ["10001"]
    assert payload["counts"] == {"groups": 0, "users": 0, "enabledAgents": 0}
    assert payload["recentAgentActions"] == []
    assert "metrics" in payload
    assert "generatedAt" in payload
    stats = payload["stats"]
    assert stats["ai"]["requestsTotal"] == 1
    assert stats["ai"]["successRate"] == pytest.approx(1.0)
    assert {route["task"] for route in stats["llm"]["routes"]} == {
        "agent_dialogue",
        "agent_proactive",
        "agent_memory",
        "agent_image",
    }
    assert all("apiKey" not in route for route in stats["llm"]["routes"])
    assert stats["activity"]["messages24h"] == 0
    assert stats["memory"]["compactingGroups"] == 0
    # 测试进程已加载跑团子插件，live 统计可用且无对局。
    assert stats["games"]["live"]["rpg"] == {"available": True, "count": 0}
    assert stats["uptime"]["uptimeSeconds"] >= 0

    await engine.dispose()


@pytest.mark.asyncio
async def test_agent_diagnostics_uses_runtime_defaults_without_persisted_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSession:
        async def get(self, _model: object, _group_id: int) -> None:
            return None

    async def fake_memory_status(_session: object, group_id: int) -> dict[str, object]:
        return {
            "groupId": str(group_id),
            "pendingMessages": 0,
            "lastCompactedMessageId": None,
            "lastCompactedAt": None,
            "countsByType": {},
            "total": 0,
            "oldestUpdatedAt": None,
            "newestUpdatedAt": None,
            "rebuildRequired": False,
            "lastAttemptAt": None,
            "lastSuccessAt": None,
            "lastError": None,
            "consecutiveFailures": 0,
            "inFlight": False,
        }

    routes = [
        {
            "task": task,
            "profile": "default",
            "provider": "default",
            "model": "model",
            "thinking": "auto",
            "multimodal": "auto",
            "configured": True,
        }
        for task in ("agent_dialogue", "agent_proactive", "agent_memory", "agent_image")
    ]
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

    assert result["effective"]["enabled"] is True
    assert result["effective"]["replyTriggerEnabled"] is True
    assert result["effective"]["explicitWakeupEnabled"] is True
    assert result["effective"]["proactiveEnabled"] is True
    assert result["effective"]["dailyLimit"] == 30
    assert result["effective"]["cooldownMinutes"] == 8
    assert result["effective"]["shortConversation"]["enabled"] is True
    assert result["effective"]["shortConversation"]["active"] is False
    assert result["blockers"] == []


@pytest.mark.asyncio
async def test_agent_diagnostics_proactive_switch_gates_interject_subfeature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = SimpleNamespace(
        enabled=True,
        reply_trigger_enabled=True,
        explicit_wakeup_enabled=True,
        proactive_enabled=False,
        proactive_active_enabled=True,
        short_conversation_enabled=True,
        daily_limit=30,
        cooldown_minutes=8,
        last_agent_at=None,
        last_proactive_at=None,
        active_topic=None,
        media_cache_enabled=False,
        proactive_count=0,
        proactive_day=None,
    )

    class FakeSession:
        async def get(self, model: object, _key: object) -> object | None:
            if model is config_models.GroupAgentConfig:
                return config
            return None

    async def fake_memory_status(_session: object, group_id: int) -> dict[str, object]:
        return {
            "groupId": str(group_id),
            "runtimeEnabled": True,
            "pendingMessages": 0,
            "lastCompactedMessageId": None,
            "lastCompactedAt": None,
            "countsByType": {},
            "total": 0,
            "oldestUpdatedAt": None,
            "newestUpdatedAt": None,
            "rebuildRequired": False,
            "lastAttemptAt": None,
            "lastSuccessAt": None,
            "lastError": None,
            "consecutiveFailures": 0,
            "inFlight": False,
        }

    routes = [
        {
            "task": task,
            "profile": "default",
            "provider": "default",
            "model": "model",
            "thinking": "auto",
            "multimodal": "auto",
            "configured": True,
        }
        for task in ("agent_dialogue", "agent_proactive", "agent_memory", "agent_image")
    ]
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

    assert result["effective"]["enabled"] is True
    assert result["effective"]["proactiveEnabled"] is False
    assert result["effective"]["proactiveActiveEnabled"] is False
    assert [item["code"] for item in result["blockers"]] == ["proactive_disabled"]


@pytest.mark.asyncio
async def test_agent_diagnostics_master_switch_suppresses_secondary_blockers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSession:
        async def get(self, _model: object, _group_id: int) -> None:
            return None

    async def fake_runtime_enabled(
        _session: object, _group_id: int, *, config: object | None = None
    ) -> bool:
        _ = config
        return False

    async def fake_memory_status(_session: object, group_id: int) -> dict[str, object]:
        return {
            "groupId": str(group_id),
            "runtimeEnabled": False,
            "pendingMessages": 0,
            "lastCompactedMessageId": None,
            "lastCompactedAt": None,
            "countsByType": {},
            "total": 0,
            "oldestUpdatedAt": None,
            "newestUpdatedAt": None,
            "rebuildRequired": False,
            "lastAttemptAt": None,
            "lastSuccessAt": None,
            "lastError": None,
            "consecutiveFailures": 0,
            "inFlight": False,
        }

    routes = [
        {
            "task": task,
            "profile": "default",
            "provider": "default",
            "model": "",
            "thinking": "auto",
            "multimodal": "auto",
            "configured": False,
        }
        for task in ("agent_dialogue", "agent_proactive", "agent_memory", "agent_image")
    ]
    monkeypatch.setattr(service, "agent_runtime_enabled", fake_runtime_enabled)
    monkeypatch.setattr(service, "agent_memory_status", fake_memory_status)
    monkeypatch.setattr(
        service,
        "_llm_runtime_status",
        lambda: {"routes": routes, "unconfiguredProviders": ["default"]},
    )
    monkeypatch.setattr(service, "get_bots", dict)

    result = await service.agent_diagnostics(FakeSession(), 100)

    assert result["effective"]["enabled"] is False
    assert result["effective"]["proactiveEnabled"] is False
    assert result["effective"]["proactiveActiveEnabled"] is False
    assert result["effective"]["shortConversation"]["enabled"] is False
    assert [item["code"] for item in result["blockers"]] == ["agent_disabled"]
