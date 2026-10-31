# ruff: noqa: ANN001, ANN201, ARG001, DTZ005, PLR2004
"""WebUI 私聊 Agent 接口：状态、暂停/恢复自动回复、删除私聊记忆（不碰群记忆）。"""

from __future__ import annotations

import importlib
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import nonebot
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
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

_PKG = "src.plugins.yawn_core"
bridge = importlib.import_module(f"{_PKG}.private_chat_bridge")
users = importlib.import_module(f"{_PKG}.webui.users")
route_models = importlib.import_module(f"{_PKG}.webui.route_models")
Model = importlib.import_module("nonebot_plugin_orm").Model
_M = f"{_PKG}.data_models"
BotUser = importlib.import_module(f"{_M}.bot_user").BotUser
AgentMemory = importlib.import_module(f"{_M}.agent_memory").AgentMemory
ChatSession = importlib.import_module(f"{_M}.chat_session").ChatSession
ChatMessage = importlib.import_module(f"{_M}.chat_message").ChatMessage

USER = 42
NOW = datetime.now() + timedelta(hours=8)  # 只用于造数据，过期判断与接口无关


def _memory(
    key: str, content: str, *, scope: str, group_id: int | None, source="agent"
):
    return AgentMemory(
        group_id=group_id,
        subject_user_id=USER,
        scope=scope,
        memory_type="private_fact" if scope == "private" else "profile",
        memory_key=key,
        content=content,
        evidence_message_ids=[],
        source_kind=source,
        related_user_ids=[USER],
        salience=0.8,
        confidence=0.8,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest_asyncio.fixture
async def maker(monkeypatch, tmp_path):
    monkeypatch.setenv("PRIVATE_AGENT_STATE_PATH", str(tmp_path / "state.json"))
    bridge.reset_state_for_tests()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Model.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(BotUser(user_id=USER, nickname="小明", affinity=0))
        await session.flush()
        session.add_all(
            [
                _memory("hobby", "爬山", scope="private", group_id=None),
                _memory(
                    "dislike", "香菜", scope="private", group_id=None, source="auto"
                ),
                _memory("hobby", "群里的爱好", scope="group", group_id=101),
            ]
        )
        chat = ChatSession(user_id=USER, title="t")
        session.add(chat)
        await session.flush()
        session.add(
            ChatMessage(
                session_id=chat.id, role="assistant", content="hi", created_at=NOW
            )
        )
        await session.commit()

    @asynccontextmanager
    async def fake_session():
        async with factory() as session:
            yield session

    notified: list[tuple[str, str]] = []

    async def notify(resource: str, key: str) -> None:
        notified.append((resource, key))

    monkeypatch.setattr(users, "get_session", fake_session)
    monkeypatch.setattr(users.hub, "notify_change", notify)
    yield factory, notified
    bridge.reset_state_for_tests()
    await engine.dispose()


@pytest.mark.asyncio
async def test_private_agent_status_lists_only_private_memories(maker):
    result = await users.get_user_private_agent(USER, None)
    data = result["data"]
    assert data["userId"] == str(USER)
    assert data["autoPaused"] is False
    assert data["memoryAvailable"] is True
    assert {item["content"] for item in data["facts"]} == {"爬山", "香菜"}
    labels = {item["key"]: item["label"] for item in data["facts"]}
    assert labels == {"hobby": "爱好", "dislike": "讨厌的"}
    sources = {item["key"]: item["sourceLabel"] for item in data["facts"]}
    assert sources["dislike"] == "聊天里整理的"
    assert data["turnsToday"] >= 0
    assert data["dailyTurns"] == bridge.private_agent_config.private_agent_daily_turns


@pytest.mark.asyncio
async def test_pause_and_resume_auto_reply(maker):
    _factory, notified = maker
    body = route_models.PrivateAgentPatchBody.model_validate({"autoPaused": True})
    result = await users.patch_user_private_agent(USER, body, None)
    assert result["data"]["autoPaused"] is True
    assert bridge.auto_paused(USER) is True
    body = route_models.PrivateAgentPatchBody(auto_paused=False)
    result = await users.patch_user_private_agent(USER, body, None)
    assert result["data"]["autoPaused"] is False
    assert notified == [("user_private_agent", str(USER))] * 2


@pytest.mark.asyncio
async def test_delete_one_category_then_all_keeps_group_memory(maker):
    factory, _notified = maker
    result = await users.delete_user_private_memories(USER, None, key="hobby")
    assert result["data"]["removed"] == 1
    assert [item["key"] for item in result["data"]["facts"]] == ["dislike"]
    result = await users.delete_user_private_memories(USER, None, key="")
    assert result["data"]["removed"] == 1
    assert result["data"]["facts"] == []
    async with factory() as session:
        remaining = (await session.execute(select(AgentMemory))).scalars().all()
    assert [(row.scope, row.content) for row in remaining] == [("group", "群里的爱好")]


@pytest.mark.asyncio
async def test_delete_rejects_unknown_category_and_unknown_user(maker):
    with pytest.raises(HTTPException) as exc:
        await users.delete_user_private_memories(USER, None, key="password")
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        await users.get_user_private_agent(999, None)
    assert exc.value.status_code == 404


def test_private_agent_patch_body_requires_flag():
    with pytest.raises(ValueError):
        route_models.PrivateAgentPatchBody.model_validate({})


@pytest.mark.asyncio
async def test_affinity_endpoint_and_manual_adjust(maker):
    affinity = importlib.import_module(f"{_PKG}.affinity")
    affinity.reset_ledger_state()
    result = await users.get_user_affinity(USER, None)
    data = result["data"]
    assert data["points"] == 0 and data["level"] == "陌生"
    assert data["available"] is True
    assert "manual" in data["sources"]
    body = route_models.AffinityAdjustBody(delta=25, note="活动奖励")
    result = await users.adjust_user_affinity(USER, body, None)
    data = result["data"]
    assert data["points"] == 25 and data["level"] == "面熟"
    assert data["events"][0]["source"] == "manual"
    with pytest.raises(HTTPException) as exc:
        await users.adjust_user_affinity(
            USER, route_models.AffinityAdjustBody(delta=0), None
        )
    assert exc.value.status_code == 400
