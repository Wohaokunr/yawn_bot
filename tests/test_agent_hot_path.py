from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nonebot
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_FAMILY_TOOL_COUNT = 36
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()
if nonebot.get_plugin("yawn_core") is None:
    nonebot.load_from_toml("pyproject.toml")

tools = importlib.import_module("src.plugins.yawn_core.yawn_agent.tools")
dialogue = importlib.import_module("src.plugins.yawn_core.yawn_agent.dialogue")
handlers = importlib.import_module("src.plugins.yawn_core.yawn_agent.tool_handlers")
registry = importlib.import_module("src.plugins.yawn_core.yawn_agent.tool_registry")


@pytest.mark.asyncio
async def test_normal_multi_tool_batch_reuses_turn_capability_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caps = tools.BotGroupCapabilities(
        role="member",
        can_manage=False,
        actions=frozenset({"get_group_info", "get_group_member_list"}),
    )
    probe_count = 0

    async def unexpected_probe(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal probe_count
        probe_count += 1
        return caps

    monkeypatch.setattr(tools, "probe_group_capabilities", unexpected_probe)

    class Bot:
        async def call_api(self, action: str, **_params: Any) -> Any:
            if action == "get_group_info":
                return {"group_id": 1, "group_name": "测试群", "member_count": 2}
            if action == "get_group_member_list":
                return [
                    {"user_id": 10, "nickname": "甲", "role": "member"},
                    {"user_id": 11, "nickname": "乙", "role": "member"},
                ]
            raise AssertionError(action)

    context = tools.ToolExecutionContext(
        bot=Bot(),
        group_id=1,
        actor_user_id=10,
        session=None,
        capabilities=caps,
        actor_can_manage=False,
        privileged_allowlist=frozenset(),
    )
    first = await tools.execute_tool_with_meta("get_group_info", {}, context=context)
    second = await tools.execute_tool_with_meta(
        "list_group_members", {"limit": 2}, context=context
    )

    assert first.payload["ok"] is True
    assert second.payload["ok"] is True
    assert probe_count == 0


@pytest.mark.asyncio
async def test_normal_permission_levels_never_force_capability_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caps = tools.BotGroupCapabilities(
        role="member",
        can_manage=False,
        actions=frozenset(),
    )
    probe_count = 0

    async def unexpected_probe(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal probe_count
        probe_count += 1
        return caps

    async def no_policy(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def no_handler(*_args: Any, **_kwargs: Any) -> Any:
        return tools.ToolHandlerResult({"ok": True})

    monkeypatch.setattr(tools, "probe_group_capabilities", unexpected_probe)
    monkeypatch.setattr(tools, "_check_tool_policy", no_policy)
    monkeypatch.setattr(tools, "dispatch_tool", no_handler)
    context = tools.ToolExecutionContext(
        bot=object(),
        group_id=1,
        actor_user_id=10,
        session=None,
        capabilities=caps,
    )

    for name in ("get_group_info", "record_user_relation", "send_message"):
        result = await tools.execute_tool_with_meta(name, {}, context=context)
        assert result.payload["ok"] is True

    assert probe_count == 0


@pytest.mark.asyncio
async def test_privileged_tool_forces_fresh_bot_capability_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = tools.BotGroupCapabilities(
        role="member",
        can_manage=False,
        actions=frozenset(),
    )
    fresh = tools.BotGroupCapabilities(
        role="admin",
        can_manage=True,
        actions=frozenset({"set_group_name"}),
    )
    probe_count = 0

    async def fresh_probe(*_args: Any, **kwargs: Any) -> Any:
        nonlocal probe_count
        probe_count += 1
        assert kwargs.get("refresh") is True
        return fresh

    async def no_policy(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def no_handler(
        _name: str, _args: dict[str, Any], context: Any
    ) -> Any:
        assert context.capabilities is fresh
        return tools.ToolHandlerResult({"ok": True})

    monkeypatch.setattr(tools, "probe_group_capabilities", fresh_probe)
    monkeypatch.setattr(tools, "_check_tool_policy", no_policy)
    monkeypatch.setattr(tools, "dispatch_tool", no_handler)
    context = tools.ToolExecutionContext(
        bot=object(),
        group_id=1,
        actor_user_id=10,
        session=None,
        capabilities=stale,
    )

    result = await tools.execute_tool_with_meta("set_group_name", {}, context=context)
    assert result.payload["ok"] is True
    assert probe_count == 1


@pytest.mark.asyncio
async def test_discover_tools_reuses_turn_actor_permission_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caps = tools.BotGroupCapabilities(
        role="admin",
        can_manage=True,
        actions=frozenset({"get_essence_msg_list", "set_essence_msg"}),
    )

    async def unexpected_actor_lookup(*_args: Any, **_kwargs: Any) -> bool:
        raise AssertionError

    monkeypatch.setattr(tools, "user_can_manage_group", unexpected_actor_lookup)
    context = tools.ToolExecutionContext(
        bot=object(),
        group_id=1,
        actor_user_id=20,
        session=None,
        capabilities=caps,
        actor_can_manage=True,
        privileged_allowlist=frozenset({"set_essence_message"}),
    )

    result = await tools.execute_tool_with_meta(
        "discover_tools", {"query": "精华消息"}, context=context
    )
    assert result.payload["ok"] is True
    names = {item["name"] for item in result.payload["result"]["tools"]}
    assert "set_essence_message" in names


@pytest.mark.asyncio
async def test_regular_tool_batch_commit_is_one_commit_and_one_refresh() -> None:
    class Session:
        def __init__(self) -> None:
            self.commits = 0
            self.refreshes = 0
            self.rollbacks = 0

        async def commit(self) -> None:
            self.commits += 1

        async def refresh(self, _config: object) -> None:
            self.refreshes += 1

        async def rollback(self) -> None:
            self.rollbacks += 1

    session = Session()
    config = SimpleNamespace()
    ok = await dialogue._commit_tool_batch(
        session,
        config,
        1,
        2,
        ["get_group_info", "search_group_memory", "record_user_relation"],
    )

    assert ok is True
    assert session.commits == 1
    assert session.refreshes == 1
    assert session.rollbacks == 0


def test_tool_family_dispatch_covers_registry_without_discovery() -> None:
    expected = {
        definition.name
        for definition in registry.TOOL_DEFINITIONS
        if definition.name != "discover_tools"
    }
    assert expected == handlers.HANDLED_TOOL_NAMES
    assert len(expected) == EXPECTED_FAMILY_TOOL_COUNT
