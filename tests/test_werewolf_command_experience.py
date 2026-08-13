"""狼人杀公开状态与玩家入口的体验回归测试。"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path
from typing import Any

import nonebot
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROJECT_ROOT / "src" / "plugins" / "yawn_core"


@pytest.fixture(scope="module")
def ww_command_modules() -> tuple[Any, Any, Any, Any]:
    """加载命令模块，避免启动整个机器人。"""
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()
    if nonebot.get_plugin("nonebot_plugin_orm") is None:
        nonebot.load_plugin("nonebot_plugin_orm")
    package = types.ModuleType("yawn_core")
    package.__path__ = [str(PLUGIN_ROOT)]
    sys.modules.setdefault("yawn_core", package)
    ww_package = types.ModuleType("yawn_core.yawn_werewolf")
    ww_package.__path__ = [str(PLUGIN_ROOT / "yawn_werewolf")]
    sys.modules.setdefault("yawn_core.yawn_werewolf", ww_package)
    commands = importlib.import_module("yawn_core.yawn_werewolf.commands")
    roles = importlib.import_module("yawn_core.yawn_werewolf.roles")
    state = importlib.import_module("yawn_core.yawn_werewolf.state")
    dsl = importlib.import_module("yawn_core.yawn_werewolf.dsl")
    return commands, roles, state, dsl


def test_signup_status_shows_public_roster(
    ww_command_modules: tuple[Any, Any, Any, Any],
) -> None:
    commands, _roles, state, _dsl = ww_command_modules
    game = state.Game(
        group_id=301,
        host_user_id=1,
        signup_user_ids=[1, 2],
        signup_names={1: "房主", 2: "小鱼"},
    )

    text = commands.format_game_status(game)

    assert "报名中" in text
    assert "房主" in text and "小鱼" in text
    assert "当前 2/12 人" in text
    assert "身份" not in text


def test_running_status_hides_night_role_and_action_details(
    ww_command_modules: tuple[Any, Any, Any, Any],
) -> None:
    commands, roles, state, _dsl = ww_command_modules
    game = state.Game(
        group_id=302,
        host_user_id=1,
        phase=state.Phase.NIGHT_WITCH,
        round_no=2,
        players=[
            state.PlayerState(1, 1, roles.Role.WITCH, roles.Faction.GOOD),
            state.PlayerState(2, 2, roles.Role.WEREWOLF, roles.Faction.WOLF),
        ],
    )

    text = commands.format_game_status(game)

    assert "第 2 夜" in text
    assert "存活座位：1号、2号" in text
    assert "夜间行动不会在群内显示" in text
    assert "女巫" not in text
    assert "NIGHT_WITCH" not in text
    assert "已行动" not in text


def test_slash_special_actions_are_supported_by_shared_dsl(
    ww_command_modules: tuple[Any, Any, Any, Any],
) -> None:
    _commands, _roles, state, dsl = ww_command_modules

    owner = dsl.parse_dm_action("/认主 3", 11)
    elder = dsl.parse_dm_action("/禁言 4", 12)

    assert owner is not None and owner.kind is state.ActionKind.CHOOSE_OWNER
    assert elder is not None and elder.kind is state.ActionKind.SILENCE
