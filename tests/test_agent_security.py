from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import nonebot
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BOT_ID = 100
ACTOR_ADMIN_ID = 200
PREEXISTING_ADMIN_TOOL_COUNT = 7
MEMBER_RESULT_LIMIT = 30
MEMBER_TOTAL = 125
EXPANDED_MEMBER_RESULT_LIMIT = 50
RECENT_MESSAGE_RESULT_LIMIT = 3
STANDARD_TOOL_ROUNDS = 2
EXTENDED_TOOL_ROUNDS = 3
TITLE_MEMBER_INDEX = 2


def _load_agent_modules() -> tuple[Any, Any, Any]:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()
    if (
        nonebot.get_plugin("yawn_core") is None
        and nonebot.get_plugin("src.plugins.yawn_core") is None
    ):
        nonebot.load_from_toml("pyproject.toml")
    from src.plugins.yawn_core.yawn_agent import capabilities, media, tools

    return capabilities, media, tools


def _tool_names(schemas: list[dict[str, Any]]) -> set[str]:
    return {str(item["function"]["name"]) for item in schemas}


def test_admin_tool_schemas_require_actor_management_permission() -> None:
    capabilities, _media, tools = _load_agent_modules()
    bot_admin = capabilities.BotGroupCapabilities(
        role="admin",
        can_manage=True,
        actions=frozenset({"send_group_msg", "set_group_ban", "send_group_notice"}),
    )

    member_tools = _tool_names(tools.build_tool_schemas(bot_admin))
    admin_tools = _tool_names(
        tools.build_tool_schemas(bot_admin, allow_admin_tools=True)
    )

    assert "mute_member" not in member_tools
    assert "create_group_announcement" not in member_tools
    assert {"mute_member", "create_group_announcement"} <= admin_tools


def test_local_group_capabilities_never_calls_onebot() -> None:
    capabilities, _media, _tools = _load_agent_modules()

    class Bot:
        self_id = "100"
        supported_actions = frozenset({"send_group_msg", "get_group_info"})

        async def call_api(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError

    caps = capabilities.local_group_capabilities(Bot())

    assert caps.role == "member"
    assert caps.can_manage is False
    assert caps.actions == frozenset({"send_group_msg", "get_group_info"})


@pytest.mark.asyncio
async def test_ordinary_dialogue_tool_routing_does_not_probe_onebot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capabilities, _media, _tools = _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent import dialogue

    class Bot:
        self_id = "100"

    async def forbidden_probe(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError

    monkeypatch.setattr(dialogue, "user_can_manage_group", forbidden_probe)
    monkeypatch.setattr(dialogue, "probe_group_capabilities", forbidden_probe)

    caps, allow_admin, tool_names, source = (
        await dialogue._resolve_turn_tool_capabilities(
            Bot(),
            1,
            200,
            "帮我看看群信息",
            has_reply=False,
            has_mentions=False,
            has_media=False,
        )
    )

    assert source == "local_baseline"
    assert allow_admin is False
    assert caps == capabilities.local_group_capabilities(Bot())
    assert "get_group_info" in tool_names


@pytest.mark.asyncio
async def test_read_tool_execution_does_not_force_role_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capabilities, _media, tools = _load_agent_modules()

    class Bot:
        self_id = "100"

        async def call_api(self, action: str, **_params: Any) -> dict[str, Any]:
            assert action == "get_group_info"
            return {"group_id": 1, "group_name": "测试群", "member_count": 3}

    async def forbidden_probe(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError

    monkeypatch.setattr(tools, "probe_group_capabilities", forbidden_probe)
    bot = Bot()
    result = await tools.execute_tool(
        "get_group_info",
        {},
        bot=bot,
        group_id=1,
        capabilities=capabilities.local_group_capabilities(bot),
    )

    assert result["ok"] is True
    assert result["result"]["group_id"] == 1


@pytest.mark.asyncio
async def test_one_onebot_action_failure_does_not_disable_other_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capabilities, _media, tools = _load_agent_modules()

    class Bot:
        self_id = "100"

        async def call_api(self, action: str, **_params: Any) -> dict[str, Any]:
            if action == "get_group_member_info":
                raise RuntimeError
            if action == "get_group_info":
                return {"group_id": 1, "group_name": "测试群", "member_count": 3}
            raise AssertionError

    async def forbidden_probe(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError

    monkeypatch.setattr(tools, "probe_group_capabilities", forbidden_probe)
    bot = Bot()
    caps = capabilities.local_group_capabilities(bot)

    failed = await tools.execute_tool(
        "get_group_member",
        {"user_id": 200},
        bot=bot,
        group_id=1,
        capabilities=caps,
    )
    succeeded = await tools.execute_tool(
        "get_group_info",
        {},
        bot=bot,
        group_id=1,
        capabilities=caps,
    )

    assert failed["ok"] is False
    assert succeeded["ok"] is True
    assert succeeded["result"]["group_id"] == 1


@pytest.mark.asyncio
async def test_group_announcement_capability_defaults_to_napcat_action() -> None:
    capabilities, _media, _tools = _load_agent_modules()

    class Bot:
        self_id = "100"

        async def call_api(self, action: str, **params: Any) -> dict[str, Any]:
            assert action == "get_group_member_info"
            assert int(params["user_id"]) == BOT_ID
            return {"role": "admin"}

    default_caps = await capabilities.probe_group_capabilities(
        Bot(), 910001, refresh=True
    )
    assert "_send_group_notice" in default_caps.actions
    assert "send_group_notice" not in default_caps.actions

    class AliasBot(Bot):
        supported_actions = frozenset({"get_group_member_info", "send_group_notice"})

    alias_caps = await capabilities.probe_group_capabilities(
        AliasBot(), 910002, refresh=True
    )
    assert "send_group_notice" in alias_caps.actions
    assert "_send_group_notice" not in alias_caps.actions


@pytest.mark.asyncio
async def test_create_group_announcement_prefers_napcat_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capabilities, _media, tools = _load_agent_modules()
    caps = capabilities.BotGroupCapabilities(
        role="admin",
        can_manage=True,
        actions=frozenset({"send_group_notice", "_send_group_notice"}),
    )

    class Bot:
        calls: list[tuple[str, dict[str, Any]]]

        def __init__(self) -> None:
            self.calls = []

        async def call_api(self, action: str, **params: Any) -> dict[str, Any]:
            self.calls.append((action, params))
            return {"ok": True}

    async def probe(*_args: Any, **_kwargs: Any) -> Any:
        return caps

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(tools, "probe_group_capabilities", probe)
    monkeypatch.setattr(tools, "_check_tool_policy", noop)
    monkeypatch.setattr(tools, "_consume_admin_quota", noop)
    monkeypatch.setattr(tools, "_audit", noop)

    bot = Bot()
    result = await tools.execute_tool(
        "create_group_announcement",
        {"content": "测试公告"},
        bot=bot,
        group_id=802027793,
        actor_user_id=200,
        capabilities=caps,
    )

    assert result == {"ok": True, "result": {"ok": True}}
    assert bot.calls == [
        (
            "_send_group_notice",
            {"group_id": 802027793, "content": "测试公告"},
        )
    ]


def test_tool_permission_levels_gate_side_effects_and_allowlist() -> None:
    capabilities, _media, tools = _load_agent_modules()
    caps = capabilities.BotGroupCapabilities(
        role="admin",
        can_manage=True,
        actions=frozenset(
            {
                "send_group_msg",
                "send_group_forward_msg",
                "upload_group_file",
                "set_group_ban",
            }
        ),
    )

    member_names = _tool_names(tools.build_tool_schemas(caps))
    assert "send_message" in member_names
    assert "record_user_relation" in member_names
    assert "send_file" not in member_names
    assert "mute_member" not in member_names

    admin_names = _tool_names(
        tools.build_tool_schemas(
            caps,
            allow_admin_tools=True,
            privileged_allowlist={"send_file"},
        )
    )
    assert "send_file" in admin_names
    assert "mute_member" not in admin_names

    snapshot = tools.tool_permission_snapshot(
        caps,
        allow_admin_tools=True,
        privileged_allowlist={"send_file"},
    )
    by_name = {row["name"]: row for row in snapshot}
    assert by_name["get_group_info"]["permissionLevel"] == "read"
    assert by_name["record_user_relation"]["permissionLevel"] == "state_write"
    assert by_name["send_message"]["permissionLevel"] == "message_send"
    assert by_name["send_file"] == {
        "name": "send_file",
        "permissionLevel": "privileged",
        "exposed": True,
        "reason": "exposed",
        "actions": ["upload_group_file"],
    }
    assert by_name["mute_member"]["reason"] == "not_allowlisted"


@pytest.mark.asyncio
async def test_send_file_is_privileged_and_rechecks_actor_role() -> None:
    capabilities, _media, tools = _load_agent_modules()

    class Bot:
        self_id = "100"
        uploaded = False

        async def call_api(self, action: str, **params: Any) -> dict[str, Any]:
            if action == "get_group_member_info":
                return {
                    "role": "admin" if int(params["user_id"]) == BOT_ID else "member"
                }
            if action == "upload_group_file":
                self.uploaded = True
                return {}
            raise AssertionError(action)

    bot = Bot()
    stale = capabilities.BotGroupCapabilities(
        role="admin", can_manage=True, actions=frozenset({"upload_group_file"})
    )
    result = await tools.execute_tool(
        "send_file",
        {"file": "anything", "name": "x.txt"},
        bot=bot,
        group_id=1,
        actor_user_id=200,
        capabilities=stale,
    )

    assert result == {"ok": False, "error": "特权工具仅允许群主或管理员触发"}
    assert not bot.uploaded


def test_tool_registry_filters_removed_tools_and_forward_capability() -> None:
    capabilities, _media, tools = _load_agent_modules()
    base = capabilities.BotGroupCapabilities(
        role="member",
        can_manage=False,
        actions=frozenset({"send_group_msg"}),
    )
    names = _tool_names(tools.build_tool_schemas(base))
    assert {"send_text", "get_recent_messages", "get_group_activity"}.isdisjoint(names)
    assert "send_image" not in names
    assert "send_message" in names
    assert "send_forward" not in names

    forward = capabilities.BotGroupCapabilities(
        role="member",
        can_manage=False,
        actions=frozenset({"send_group_forward_msg"}),
    )
    assert "send_forward" in _tool_names(tools.build_tool_schemas(forward))


def test_dialogue_tool_policy_keeps_core_bundle_available() -> None:
    capabilities, _media, tools = _load_agent_modules()
    caps = capabilities.BotGroupCapabilities(
        role="member",
        can_manage=False,
        actions=frozenset(
            {
                "send_group_msg",
                "get_msg",
                "get_group_member_list",
                "set_msg_emoji_like",
            }
        ),
    )

    core = frozenset(
        {"send_message", "search_group_memory", "get_message", "discover_tools"}
    )
    assert tools.select_dialogue_tool_names("今天有点困") == core
    reply_bundle = tools.select_dialogue_tool_names(
        "今天有点困", has_reply=True, has_media=True
    )
    assert core | {"react_to_message"} <= reply_bundle
    mention_bundle = tools.select_dialogue_tool_names("你好", has_mentions=True)
    assert core | {"get_group_member", "get_person_profile"} <= mention_bundle
    memory_bundle = tools.select_dialogue_tool_names("你还记得我上次说的吗")
    assert core <= memory_bundle
    reaction_bundle = tools.select_dialogue_tool_names("发个无语表情包")
    assert {"send_message", "search_reactions"} <= reaction_bundle

    schemas = tools.build_tool_schemas(caps, include_names=memory_bundle)
    assert _tool_names(schemas) == core

    segment_types = tools.select_dialogue_message_segment_types(
        "回复他一个无语表情包", has_target_mentions=True
    )
    assert segment_types == frozenset({"text", "reply", "at", "reaction"})
    send_schema = tools.build_tool_schemas(
        caps,
        include_names={"send_message"},
        message_segment_types={"text", "reply"},
    )[0]
    item_schema = send_schema["function"]["parameters"]["properties"]["segments"][
        "items"
    ]
    assert item_schema["properties"]["type"]["enum"] == ["text", "reply"]
    assert set(item_schema["properties"]) == {"type", "text", "message_id"}


def test_dialogue_tool_round_budget_is_small_by_default() -> None:
    _capabilities, _media, tools = _load_agent_modules()

    assert tools.dialogue_tool_round_limit(frozenset()) == 1
    assert tools.dialogue_tool_round_limit({"send_message"}) == STANDARD_TOOL_ROUNDS
    assert (
        tools.dialogue_tool_round_limit({"discover_tools", "send_message"})
        == EXTENDED_TOOL_ROUNDS
    )
    assert (
        tools.dialogue_tool_round_limit({"search_group_memory"})
        == STANDARD_TOOL_ROUNDS
    )
    assert tools.dialogue_tool_round_limit(
        {"search_reactions", "send_message"}
    ) == STANDARD_TOOL_ROUNDS
    assert tools.dialogue_tool_round_limit(
        {
            "search_group_memory",
            "get_person_profile",
            "list_user_relations",
            "get_group_info",
        }
    ) == EXTENDED_TOOL_ROUNDS
    assert (
        tools.dialogue_tool_round_limit({"mute_member"}) == EXTENDED_TOOL_ROUNDS
    )


@pytest.mark.asyncio
async def test_discover_tools_only_returns_currently_exposable_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capabilities, _media, tools = _load_agent_modules()
    current = capabilities.BotGroupCapabilities(
        role="admin",
        can_manage=True,
        actions=frozenset(
            {
                "get_group_member_info",
                "get_essence_msg_list",
                "set_essence_msg",
                "set_group_kick",
            }
        ),
    )

    class Config:
        tool_allowlist = ("set_essence_message", "kick_member")

    class Session:
        async def get(self, _model: object, _group_id: int) -> Config:
            return Config()

    class Bot:
        async def call_api(self, action: str, **_params: Any) -> dict[str, Any]:
            assert action == "get_group_member_info"
            return {"role": "admin"}

    async def probe(*_args: object, **_kwargs: object) -> Any:
        return current

    async def no_audit(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(tools, "probe_group_capabilities", probe)
    monkeypatch.setattr(tools, "_audit", no_audit)
    result = await tools.execute_tool(
        "discover_tools",
        {"query": "精华消息"},
        bot=Bot(),
        group_id=1,
        actor_user_id=ACTOR_ADMIN_ID,
        session=Session(),
        capabilities=current,
    )

    assert result["ok"] is True
    discovered = {item["name"] for item in result["result"]["tools"]}
    assert "set_essence_message" in discovered
    # critical tools are never discoverable even if explicitly allowlisted.
    assert "kick_member" not in discovered
    loaded = _tool_names(
        tools.build_tool_schemas(
            current,
            allow_admin_tools=True,
            privileged_allowlist={"set_essence_message", "kick_member"},
            include_names={"discover_tools", *discovered},
        )
    )
    assert "set_essence_message" in loaded


@pytest.mark.asyncio
async def test_p5_essence_tool_rechecks_policy_and_current_group_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capabilities, _media, tools = _load_agent_modules()
    current = capabilities.BotGroupCapabilities(
        role="admin",
        can_manage=True,
        actions=frozenset({"get_msg", "set_essence_msg"}),
    )

    class Config:
        tool_allowlist = ("set_essence_message",)
        tool_day = ""
        admin_tool_count = 0
        admin_tool_daily_limit = 30
        critical_tool_count = 0
        critical_tool_daily_limit = 5

    config = Config()
    config.tool_day = tools.now_beijing().strftime("%Y-%m-%d")

    class Session:
        async def get(self, _model: object, _group_id: int) -> Config:
            return config

        async def flush(self) -> None:
            return None

    class Bot:
        self_id = "100"

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def call_api(self, action: str, **params: Any) -> dict[str, Any]:
            self.calls.append((action, params))
            if action == "get_group_member_info":
                return {"user_id": params["user_id"], "role": "admin"}
            if action == "get_msg":
                return {"message_id": params["message_id"], "group_id": 1}
            if action == "set_essence_msg":
                return {}
            raise AssertionError(action)

    async def no_audit(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(tools, "_audit", no_audit)
    bot = Bot()
    result = await tools.execute_tool(
        "set_essence_message",
        {"message_id": 99},
        bot=bot,
        group_id=1,
        actor_user_id=ACTOR_ADMIN_ID,
        session=Session(),
        capabilities=current,
    )

    assert result["ok"] is True
    assert ("set_essence_msg", {"message_id": 99}) in bot.calls
    assert config.admin_tool_count == 1


@pytest.mark.asyncio
async def test_critical_tool_rejects_background_and_uses_independent_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capabilities, _media, tools = _load_agent_modules()
    current = capabilities.BotGroupCapabilities(
        role="admin",
        can_manage=True,
        actions=frozenset({"set_group_kick"}),
    )

    class Config:
        tool_allowlist = ("kick_member",)
        tool_day = ""
        admin_tool_count = PREEXISTING_ADMIN_TOOL_COUNT
        admin_tool_daily_limit = 30
        critical_tool_count = 0
        critical_tool_daily_limit = 2

    config = Config()
    config.tool_day = tools.now_beijing().strftime("%Y-%m-%d")

    class Session:
        async def get(self, _model: object, _group_id: int) -> Config:
            return config

        async def flush(self) -> None:
            return None

        async def rollback(self) -> None:
            return None

    class Bot:
        self_id = "100"

        def __init__(self) -> None:
            self.kicked = False

        async def call_api(self, action: str, **params: Any) -> dict[str, Any]:
            if action == "get_group_member_info":
                user_id = int(params["user_id"])
                return {
                    "user_id": user_id,
                    "role": (
                        "admin"
                        if user_id in {BOT_ID, ACTOR_ADMIN_ID}
                        else "member"
                    ),
                }
            if action == "set_group_kick":
                self.kicked = True
                return {}
            raise AssertionError(action)

    async def no_audit(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(tools, "_audit", no_audit)
    bot = Bot()
    background = await tools.execute_tool(
        "kick_member",
        {"user_id": 300},
        bot=bot,
        group_id=1,
        actor_user_id=None,
        session=Session(),
        capabilities=current,
    )
    assert background["ok"] is False
    assert "管理权限" in background["error"]
    assert bot.kicked is False

    foreground = await tools.execute_tool(
        "kick_member",
        {"user_id": 300},
        bot=bot,
        group_id=1,
        actor_user_id=ACTOR_ADMIN_ID,
        session=Session(),
        capabilities=current,
    )
    assert foreground["ok"] is True
    assert bot.kicked is True
    assert config.critical_tool_count == 1
    assert config.admin_tool_count == PREEXISTING_ADMIN_TOOL_COUNT


def test_critical_tools_require_explicit_admin_intent_and_allowlist() -> None:
    capabilities, _media, tools = _load_agent_modules()
    current = capabilities.BotGroupCapabilities(
        role="owner",
        can_manage=True,
        actions=frozenset(
            {
                "set_group_kick",
                "set_group_whole_ban",
                "set_group_admin",
                "delete_group_file",
            }
        ),
    )

    plain = tools.select_dialogue_tool_names("今天聊点什么", allow_admin_tools=True)
    assert "kick_member" not in plain
    assert "set_group_admin" not in plain
    kick = tools.select_dialogue_tool_names("把小明踢出群", allow_admin_tools=True)
    assert "kick_member" in kick

    without_allowlist = _tool_names(
        tools.build_tool_schemas(
            current,
            allow_admin_tools=True,
            privileged_allowlist=set(),
            include_names=kick,
        )
    )
    assert "kick_member" not in without_allowlist
    with_allowlist = _tool_names(
        tools.build_tool_schemas(
            current,
            allow_admin_tools=True,
            privileged_allowlist={"kick_member"},
            include_names=kick,
        )
    )
    assert "kick_member" in with_allowlist


def test_registry_never_exposes_raw_or_credential_actions() -> None:
    _capabilities, _media, tools = _load_agent_modules()
    forbidden = {
        "get_cookies",
        "get_csrf_token",
        "get_credentials",
        "get_clientkey",
        "get_rkey",
        "send_packet",
        "bot_exit",
        "set_group_leave",
    }
    registered_actions = {
        action
        for definition in tools._TOOL_DEFINITIONS
        for action in definition.actions
    }
    assert forbidden.isdisjoint(registered_actions)
    assert "onebot_call" not in {item.name for item in tools._TOOL_DEFINITIONS}


@pytest.mark.asyncio
async def test_group_read_tools_project_onebot_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capabilities, _media, tools = _load_agent_modules()
    current = capabilities.BotGroupCapabilities(
        role="member",
        can_manage=False,
        actions=frozenset({"get_group_info", "get_group_member_info"}),
    )

    async def probe(*_args: object, **_kwargs: object) -> Any:
        return current

    class Bot:
        async def call_api(self, action: str, **_params: Any) -> Any:
            if action == "get_group_info":
                return {
                    "group_id": 1,
                    "group_name": "测试群",
                    "member_count": 12,
                    "max_member_count": 200,
                    "group_memo": "不进入模型",
                }
            if action == "get_group_member_info":
                return {
                    "user_id": 2,
                    "nickname": "昵称",
                    "card": "群名片",
                    "role": "admin",
                    "title": "管理员头衔",
                    "join_time": 1_700_000_000,
                    "last_sent_time": 1_800_000_000,
                }
            raise AssertionError(action)

    monkeypatch.setattr(tools, "probe_group_capabilities", probe)
    group = await tools.execute_tool(
        "get_group_info", {}, bot=Bot(), group_id=1, capabilities=current
    )
    member = await tools.execute_tool(
        "get_group_member",
        {"user_id": 2},
        bot=Bot(),
        group_id=1,
        capabilities=current,
    )

    assert group["result"] == {
        "group_id": 1,
        "group_name": "测试群",
        "member_count": 12,
        "max_member_count": 200,
    }
    assert member["result"] == {
        "user_id": 2,
        "name": "群名片",
        "role": "admin",
        "title": "管理员头衔",
    }


def test_high_value_onebot_tools_are_routed_without_privilege_leaks() -> None:
    capabilities, _media, tools = _load_agent_modules()
    caps = capabilities.BotGroupCapabilities(
        role="member",
        can_manage=False,
        actions=frozenset(
            {
                "send_group_msg",
                "get_msg",
                "get_group_msg_history",
                "_get_group_notice",
                "get_essence_msg_list",
                "get_group_shut_list",
                "get_group_honor_info",
                "get_group_root_files",
                "get_group_files_by_folder",
                "get_group_file_url",
                "set_msg_emoji_like",
            }
        ),
    )

    assert "get_recent_group_messages" in tools.select_dialogue_tool_names(
        "刚才群里在聊什么"
    )
    assert "list_group_notices" in tools.select_dialogue_tool_names("看看群公告")
    muted = tools.select_dialogue_tool_names("谁还在禁言", allow_admin_tools=True)
    assert "list_muted_members" in muted
    assert "mute_member" not in muted
    mute_action = tools.select_dialogue_tool_names(
        "把小明禁言 10 分钟", allow_admin_tools=True
    )
    assert "mute_member" in mute_action
    assert "get_group_honor" in tools.select_dialogue_tool_names("这个群龙王是谁")
    assert "list_group_files" in tools.select_dialogue_tool_names("群文件里有什么")
    assert "get_group_file_link" in tools.select_dialogue_tool_names("给我群文件链接")

    names = _tool_names(tools.build_tool_schemas(caps))
    assert {
        "get_message",
        "get_recent_group_messages",
        "list_group_notices",
        "list_essence_messages",
        "list_muted_members",
        "get_group_honor",
        "list_group_files",
        "get_group_file_link",
        "react_to_message",
    } <= names


@pytest.mark.asyncio
async def test_new_onebot_read_tools_return_compact_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capabilities, _media, tools = _load_agent_modules()
    current = capabilities.BotGroupCapabilities(
        role="member",
        can_manage=False,
        actions=frozenset(
            {
                "get_group_msg_history",
                "_get_group_notice",
                "get_essence_msg_list",
                "get_group_shut_list",
                "get_group_honor_info",
                "get_group_root_files",
                "get_group_files_by_folder",
                "get_group_file_url",
            }
        ),
    )

    async def probe(*_args: object, **_kwargs: object) -> Any:
        return current

    class Bot:
        async def call_api(self, action: str, **_params: Any) -> Any:  # noqa: PLR0911
            if action == "get_group_msg_history":
                assert _params["count"] == RECENT_MESSAGE_RESULT_LIMIT
                return {
                    "messages": [
                        {
                            "message_id": 11,
                            "sender": {"user_id": 2, "nickname": "甲"},
                            "message": [
                                {"type": "text", "data": {"text": "你好"}},
                                {
                                    "type": "image",
                                    "data": {"url": "https://secret.invalid/image"},
                                },
                            ],
                            "raw_payload": {"should": "not leak"},
                        }
                    ]
                }
            if action == "_get_group_notice":
                return [
                    {
                        "notice_id": "n1",
                        "sender_id": 2,
                        "publish_time": 123,
                        "message": "公告正文",
                        "raw": "drop-me",
                    }
                ]
            if action == "get_essence_msg_list":
                return [
                    {
                        "message_id": 12,
                        "sender_id": 3,
                        "sender_nick": "乙",
                        "message": [{"type": "text", "data": {"text": "精华正文"}}],
                        "raw": "drop-me",
                    }
                ]
            if action == "get_group_shut_list":
                return [
                    {
                        "user_id": 4,
                        "nickname": "丙",
                        "role": "member",
                        "shut_up_timestamp": 456,
                        "age": 20,
                    }
                ]
            if action == "get_group_honor_info":
                return {
                    "group_id": 1,
                    "current_talkative": {"user_id": 5, "nickname": "龙王"},
                    "talkative_list": [{"user_id": 6, "nickname": "活跃"}],
                    "raw": "drop-me",
                }
            if action == "get_group_root_files":
                return {
                    "files": [
                        {
                            "file_id": "f1",
                            "file_name": "说明.pdf",
                            "busid": 7,
                            "file_size": 100,
                            "local_path": "C:/secret",
                        }
                    ],
                    "folders": [
                        {"folder_id": "d1", "folder_name": "资料", "file_count": 2}
                    ],
                }
            if action == "get_group_file_url":
                return {
                    "url": "https://example.invalid/download/f1",
                    "token": "drop-me",
                }
            raise AssertionError(action)

    monkeypatch.setattr(tools, "probe_group_capabilities", probe)
    bot = Bot()
    history = await tools.execute_tool(
        "get_recent_group_messages",
        {"count": RECENT_MESSAGE_RESULT_LIMIT},
        bot=bot,
        group_id=1,
        capabilities=current,
    )
    notices = await tools.execute_tool(
        "list_group_notices", {}, bot=bot, group_id=1, capabilities=current
    )
    essence = await tools.execute_tool(
        "list_essence_messages", {}, bot=bot, group_id=1, capabilities=current
    )
    muted = await tools.execute_tool(
        "list_muted_members", {}, bot=bot, group_id=1, capabilities=current
    )
    honor = await tools.execute_tool(
        "get_group_honor", {}, bot=bot, group_id=1, capabilities=current
    )
    files = await tools.execute_tool(
        "list_group_files", {}, bot=bot, group_id=1, capabilities=current
    )
    link = await tools.execute_tool(
        "get_group_file_link",
        {"file_id": "f1", "busid": 7},
        bot=bot,
        group_id=1,
        capabilities=current,
    )

    assert history["result"]["items"][0]["text"] == "你好[图片]"
    assert "secret.invalid" not in str(history["result"])
    assert notices["result"] == [
        {"notice_id": "n1", "sender_id": 2, "publish_time": 123, "content": "公告正文"}
    ]
    assert essence["result"][0]["content"] == "精华正文"
    assert muted["result"]["items"][0] == {
        "user_id": 4,
        "name": "丙",
        "shut_up_timestamp": 456,
    }
    assert "raw" not in honor["result"]
    assert files["result"]["files"][0] == {
        "file_id": "f1",
        "name": "说明.pdf",
        "busid": 7,
        "size": 100,
    }
    assert link["result"] == {
        "file_id": "f1",
        "url": "https://example.invalid/download/f1",
    }


@pytest.mark.asyncio
async def test_get_message_and_reaction_require_known_current_group_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capabilities, _media, tools = _load_agent_modules()
    current = capabilities.BotGroupCapabilities(
        role="member",
        can_manage=False,
        actions=frozenset({"get_msg", "set_msg_emoji_like"}),
    )

    async def probe(*_args: object, **_kwargs: object) -> Any:
        return current

    async def no_audit(*_args: object, **_kwargs: object) -> None:
        return None

    class Session:
        def __init__(self, *, known: bool) -> None:
            self.known = known

        async def scalar(self, _stmt: object) -> object | None:
            return object() if self.known else None

        async def rollback(self) -> None:
            return None

    class Bot:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def call_api(self, action: str, **params: Any) -> Any:
            self.calls.append((action, params))
            if action == "get_msg":
                return {
                    "message_id": params["message_id"],
                    "group_id": 1,
                    "sender": {"user_id": 9, "nickname": "消息作者"},
                    "message": [{"type": "text", "data": {"text": "原消息"}}],
                }
            if action == "set_msg_emoji_like":
                return {}
            raise AssertionError(action)

    monkeypatch.setattr(tools, "probe_group_capabilities", probe)
    monkeypatch.setattr(tools, "_audit", no_audit)
    bot = Bot()

    unknown = await tools.execute_tool(
        "react_to_message",
        {"message_id": 99, "emoji_id": "66"},
        bot=bot,
        group_id=1,
        session=Session(known=False),
        capabilities=current,
    )
    assert unknown["ok"] is False
    assert "当前群近期已知消息" in unknown["error"]
    assert bot.calls == []

    known_session = Session(known=True)
    message = await tools.execute_tool(
        "get_message",
        {"message_id": 99},
        bot=bot,
        group_id=1,
        session=known_session,
        capabilities=current,
    )
    reaction = await tools.execute_tool(
        "react_to_message",
        {"message_id": 99, "emoji_id": "66"},
        bot=bot,
        group_id=1,
        session=known_session,
        capabilities=current,
    )

    assert message["result"] == {
        "message_id": 99,
        "user_id": 9,
        "name": "消息作者",
        "text": "原消息",
    }
    assert reaction["result"] == {
        "message_id": 99,
        "emoji_id": "66",
        "reacted": True,
    }
    assert bot.calls[-1] == (
        "set_msg_emoji_like",
        {"message_id": 99, "emoji_id": "66"},
    )


@pytest.mark.asyncio
async def test_list_group_members_returns_bounded_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capabilities, _media, tools = _load_agent_modules()
    current = capabilities.BotGroupCapabilities(
        role="member",
        can_manage=False,
        actions=frozenset({"get_group_member_list"}),
    )

    async def probe(*_args: object, **_kwargs: object) -> Any:
        return current

    class Bot:
        async def call_api(self, action: str, **_params: Any) -> Any:
            assert action == "get_group_member_list"
            return [
                {
                    "user_id": index,
                    "nickname": f"成员{index}",
                    "card": f"群名片{index}" if index % 2 == 0 else "",
                    "role": "admin" if index == 1 else "member",
                    "title": "活跃成员" if index == TITLE_MEMBER_INDEX else "",
                    "join_time": 1_700_000_000,
                    "last_sent_time": 1_800_000_000,
                    "level": "99",
                    "age": 18,
                }
                for index in range(MEMBER_TOTAL)
            ]

    monkeypatch.setattr(tools, "probe_group_capabilities", probe)
    result = await tools.execute_tool(
        "list_group_members",
        {},
        bot=Bot(),
        group_id=1,
        capabilities=current,
    )

    assert result["ok"] is True
    assert result["result"]["total"] == MEMBER_TOTAL
    assert result["result"]["truncated"] is True
    assert len(result["result"]["items"]) == MEMBER_RESULT_LIMIT
    assert result["result"]["items"][0] == {
        "user_id": 0,
        "name": "群名片0",
    }
    assert "join_time" not in result["result"]["items"][0]

    expanded = await tools.execute_tool(
        "list_group_members",
        {"limit": EXPANDED_MEMBER_RESULT_LIMIT},
        bot=Bot(),
        group_id=1,
        capabilities=current,
    )
    assert len(expanded["result"]["items"]) == EXPANDED_MEMBER_RESULT_LIMIT


@pytest.mark.asyncio
async def test_admin_tool_execution_rechecks_actor_role() -> None:
    capabilities, _media, tools = _load_agent_modules()

    class Bot:
        self_id = "100"
        muted = False

        async def call_api(self, action: str, **params: Any) -> dict[str, Any]:
            if action == "get_group_member_info":
                role = "admin" if int(params["user_id"]) == BOT_ID else "member"
                return {"role": role}
            if action == "set_group_ban":
                self.muted = True
                return {}
            raise AssertionError(action)

    bot = Bot()
    stale = capabilities.BotGroupCapabilities(
        role="admin", can_manage=True, actions=frozenset({"set_group_ban"})
    )
    result = await tools.execute_tool(
        "mute_member",
        {"user_id": 300, "duration": 60},
        bot=bot,
        group_id=1,
        actor_user_id=200,
        capabilities=stale,
    )

    assert result == {"ok": False, "error": "调用者没有群管理权限"}
    assert not bot.muted


@pytest.mark.asyncio
async def test_tool_execution_ignores_stale_bot_admin_cache() -> None:
    capabilities, _media, tools = _load_agent_modules()

    class Bot:
        self_id = "100"
        muted = False

        async def call_api(self, action: str, **params: Any) -> dict[str, Any]:
            del params
            if action == "get_group_member_info":
                return {"role": "member"}
            if action == "set_group_ban":
                self.muted = True
                return {}
            raise AssertionError(action)

    bot = Bot()
    stale = capabilities.BotGroupCapabilities(
        role="admin", can_manage=True, actions=frozenset({"set_group_ban"})
    )
    result = await tools.execute_tool(
        "mute_member",
        {"user_id": 300, "duration": 60},
        bot=bot,
        group_id=1,
        actor_user_id=200,
        capabilities=stale,
    )

    assert result == {"ok": False, "error": "机器人没有群管理权限"}
    assert not bot.muted


@pytest.mark.asyncio
async def test_disallowed_media_url_uses_local_onebot_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _capabilities, media, _tools = _load_agent_modules()
    image_path = tmp_path / "image.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfixture")

    class Bot:
        async def call_api(self, action: str, **params: Any) -> dict[str, Any]:
            del params
            assert action == "get_image"
            return {"file": str(image_path)}

    def safe_roots() -> tuple[Path, ...]:
        return (tmp_path.resolve(),)

    monkeypatch.setattr(media, "_allowed_hosts", frozenset)
    monkeypatch.setattr(media, "_safe_roots", safe_roots)
    blocks, captions, digests = await media.prepare_image_inputs(
        Bot(),
        1,
        [
            {
                "type": "image",
                "url": "https://private.example/signed-image",
                "file": "onebot-image-id",
            }
        ],
    )

    assert captions == []
    assert len(digests) == 1
    assert len(blocks) == 1
    assert blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "private.example" not in str(blocks)


@pytest.mark.asyncio
async def test_disallowed_media_url_is_not_forwarded_to_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capabilities, media, _tools = _load_agent_modules()
    monkeypatch.setattr(media, "_allowed_hosts", frozenset)

    blocks, captions, digests = await media.prepare_image_inputs(
        None,
        1,
        [{"type": "image", "url": "https://private.example/signed-image"}],
    )

    assert blocks == []
    assert captions == []
    assert digests == []


@pytest.mark.asyncio
async def test_default_qq_image_cdn_is_materialized_for_multimodal_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capabilities, media, _tools = _load_agent_modules()
    image_url = "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=test"
    image_bytes = b"\x89PNG\r\n\x1a\nqq-image-fixture"
    fetched: list[str] = []

    async def fetch_url(url: str) -> bytes:
        fetched.append(url)
        return image_bytes

    class Bot:
        async def call_api(self, action: str, **params: Any) -> dict[str, Any]:
            del params
            raise AssertionError(action)

    from src.plugins.yawn_core.llm import LLMRoutingConfig

    default_hosts = LLMRoutingConfig().agent_media_allowed_hosts
    monkeypatch.setattr(media.ai_config, "agent_media_allowed_hosts", default_hosts)
    assert media._url_allowed(image_url)
    assert media._url_allowed("https://gchat.qpic.cn/download?appid=1407")
    assert not media._url_allowed("https://private.example/image.png")
    monkeypatch.setattr(media.ai_config, "agent_media_allowed_hosts", "")
    assert not media._url_allowed(image_url)
    monkeypatch.setattr(media.ai_config, "agent_media_allowed_hosts", default_hosts)
    monkeypatch.setattr(media, "_fetch_url", fetch_url)

    blocks, captions, digests = await media.prepare_image_inputs(
        Bot(),
        1,
        [
            {
                "type": "image",
                "source": "current",
                "file": "AABBCC.png",
                "url": image_url,
            }
        ],
    )

    assert fetched == [image_url]
    assert captions == []
    assert len(digests) == 1
    assert len(blocks) == 1
    assert blocks[0]["type"] == "image_url"
    assert blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "multimedia.nt.qq.com.cn" not in str(blocks)
