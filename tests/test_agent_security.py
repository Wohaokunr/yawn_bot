from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import nonebot
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BOT_ID = 100
MEMBER_RESULT_LIMIT = 30
MEMBER_TOTAL = 125
EXPANDED_MEMBER_RESULT_LIMIT = 50
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


def test_dialogue_tool_policy_keeps_plain_chat_schema_free() -> None:
    capabilities, _media, tools = _load_agent_modules()
    caps = capabilities.BotGroupCapabilities(
        role="member",
        can_manage=False,
        actions=frozenset({"send_group_msg", "get_group_member_list"}),
    )

    assert tools.select_dialogue_tool_names("今天有点困") == frozenset()
    assert tools.select_dialogue_tool_names(
        "今天有点困", has_reply=True, has_media=True
    ) == frozenset()
    assert tools.select_dialogue_tool_names(
        "你好", has_mentions=True
    ) == frozenset({"send_message"})
    memory_bundle = tools.select_dialogue_tool_names("你还记得我上次说的吗")
    assert memory_bundle == frozenset({"search_group_memory"})
    reaction_bundle = tools.select_dialogue_tool_names("发个无语表情包")
    assert {"send_message", "search_reactions"} <= reaction_bundle

    schemas = tools.build_tool_schemas(caps, include_names=memory_bundle)
    assert _tool_names(schemas) == {"search_group_memory"}

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
