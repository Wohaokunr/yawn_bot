from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import nonebot
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BOT_ID = 100


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
