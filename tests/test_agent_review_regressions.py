# ruff: noqa: ANN001, ANN201, E501, F401, F811, PLR2004
"""Agent 审查回归：删除边界、写动作反馈；不使用真实模型或沙箱。"""

import importlib
from datetime import timedelta

import pytest
from test_private_agent import (
    NOW,
    USER,
    ChatMessage,
    ChatSession,
    _Bot,
    _message,
    _request,
    _script,
    maker,
    pagent,
    pmem,
    ptools,
)

services = importlib.import_module("src.plugins.yawn_core.yawn_agent.private_services")
organizer = importlib.import_module("src.plugins.yawn_core.yawn_agent.private_organizer")
computer = importlib.import_module("src.plugins.yawn_core.yawn_agent.private_computer")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVATE_AGENT_DATA_DIR", str(tmp_path / "users"))
    monkeypatch.setattr(pagent, "now_beijing", lambda: NOW)

    async def unavailable():
        return False, "test"

    monkeypatch.setattr(computer, "computer_status", unavailable)


@pytest.mark.asyncio
async def test_deleted_message_and_session_stay_out_of_agent_reads(maker):
    async with maker() as session:
        active = ChatSession(user_id=USER, updated_at=NOW)
        deleted = ChatSession(user_id=USER, updated_at=NOW - timedelta(days=1), is_deleted=True)
        session.add_all([active, deleted])
        await session.flush()
        session.add_all([
            ChatMessage(session_id=active.id, role="user", content="visible"),
            ChatMessage(session_id=active.id, role="user", content="deleted-message", is_deleted=True),
            ChatMessage(session_id=deleted.id, role="user", content="deleted-session"),
        ])
        await session.commit()
        history = await pagent._recent_history(session, USER)
        assert [row["content"] for row in history] == ["visible"]
        assert await pmem._recent_user_lines(session, USER) == ["user: visible"]
        assert await pmem.user_message_count(session, USER) == 1
        recalled = (await session.execute(services._private_messages(USER))).scalars().all()
        assert [row.content for row in recalled] == ["visible"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["delete", "list"])
async def test_write_action_must_reply_but_read_can_be_silent(maker, monkeypatch, action):
    saved = organizer.notes(USER, {"action": "add", "content": "audit note"}, NOW)
    args = {"action": action}
    if action == "delete":
        args["id"] = saved["saved"]["id"]
    _script(monkeypatch, [_message(calls=[("notes", args)]), _message("【不回】")])
    bot = _Bot()
    async with maker() as session:
        result = await pagent.handle_private_turn(_request(session, bot, "处理笔记"))
    assert bool(result) == (action == "delete")
    assert bool(bot.sent) == (action == "delete")


@pytest.mark.parametrize("name", ["group_manage", "group_bridge", "schedules", "offers", "memory_link", "my_persona"])
def test_mixed_tools_do_not_count_read_only_calls_as_writes(name):
    assert not ptools.has_write_result({name})


@pytest.mark.asyncio
async def test_privacy_server_pagination_and_member_filter(maker, monkeypatch):
    routes = importlib.import_module("src.plugins.yawn_core.webui.agent_privacy_routes")
    model = routes.AgentPrivacy
    monkeypatch.setattr(routes, "get_session", maker)
    async with maker() as session:
        session.add_all(model(group_id=101, user_id=10000 + i, opted_out=True) for i in range(101))
        await session.commit()
    last = await routes.get_privacy(101, None, page=6, page_size=20, user_id=None)
    assert last["meta"]["total"] == 101 and len(last["data"]) == 1
    user_id = int(last["data"][0]["userId"])
    found = await routes.get_privacy(101, None, page=1, page_size=20, user_id=user_id)
    assert found["meta"]["total"] == 1 and found["data"][0]["userId"] == str(user_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_queue_drop_notice_obeys_switch_and_rate_limit(maker, monkeypatch, enabled):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    agent = importlib.import_module("src.plugins.yawn_core.yawn_agent.agent")
    orm = importlib.import_module("nonebot_plugin_orm")
    outbound = importlib.import_module("src.plugins.yawn_core.yawn_agent.outbound")
    send = AsyncMock()
    monkeypatch.setattr(orm, "get_session", maker)
    monkeypatch.setattr(agent, "agent_runtime_enabled", AsyncMock(return_value=enabled))
    monkeypatch.setattr(outbound, "send_outbound_message", send)
    agent._queue_notices.clear()
    bot, event = SimpleNamespace(self_id="9"), SimpleNamespace(group_id=101)
    try:
        await agent._notify_queue_drop(bot, event, SimpleNamespace(trigger_source="mention"))
        await agent._notify_queue_drop(bot, event, SimpleNamespace(trigger_source="mention"))
        assert send.call_count == int(enabled)
    finally:
        agent._queue_notices.clear()
