from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import nonebot
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def catalog_modules() -> dict[str, Any]:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()
    if nonebot.get_plugin("yawn_core") is None:
        nonebot.load_from_toml("pyproject.toml")
    return {
        "core": importlib.import_module("src.plugins.yawn_core"),
        "catalog": importlib.import_module("src.plugins.yawn_core.command_catalog"),
        "help": importlib.import_module("src.plugins.yawn_core.help_panel"),
        "rpg_context": importlib.import_module(
            "src.plugins.yawn_core.yawn_rpg.command_context"
        ),
        "rpg_state": importlib.import_module("src.plugins.yawn_core.yawn_rpg.state"),
        "ww_context": importlib.import_module(
            "src.plugins.yawn_core.yawn_werewolf.command_context"
        ),
        "ww_state": importlib.import_module(
            "src.plugins.yawn_core.yawn_werewolf.state"
        ),
        "ww_roles": importlib.import_module(
            "src.plugins.yawn_core.yawn_werewolf.roles"
        ),
    }


def test_loaded_optional_plugins_register_typed_groups(
    catalog_modules: dict[str, Any],
) -> None:
    core = catalog_modules["core"]
    catalog = catalog_modules["catalog"]
    groups = {
        group.plugin_id: group for group in catalog.get_registered_command_groups()
    }
    plugin_ids = {
        "yawn_werewolf": "yawn_werewolf",
        "yawn_rpg": "yawn_rpg",
        "yawn_fanqie": "yawn_fanqie",
        "yawn_agent": "yawn_agent",
    }

    for status in core.get_sub_plugin_load_report():
        plugin_id = plugin_ids[status.module_name.rsplit(".", 1)[-1]]
        assert (plugin_id in groups) is (status.state == "loaded")

    assert all(
        isinstance(command, catalog.CommandSpec)
        for group in groups.values()
        for command in group.commands
    )


def test_help_builds_progressive_sections_with_admin_gating(
    catalog_modules: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = catalog_modules["catalog"]
    help_panel = catalog_modules["help"]
    group = catalog.PluginCommandGroup(
        plugin_id="fixture",
        display_name="样本",
        entrypoint="入口",
        commands=(
            catalog.CommandSpec(name="入口", description="主入口", feature="game"),
            catalog.CommandSpec(
                name="房间命令",
                description="仅房间可用",
                feature="game",
                display_level="lobby",
            ),
            catalog.CommandSpec(
                name="管理命令",
                description="只向管理员展示",
                feature="game",
                permission="group_admin",
                help_section="admin",
            ),
        ),
        get_available_commands=lambda _context: {"入口", "管理命令"},
        get_help_hint=lambda _context: "只显示当前可用操作。",
    )
    monkeypatch.setattr(help_panel, "get_registered_command_groups", lambda: (group,))
    member_context = catalog.CommandContext(user_id=1, group_id=10)
    admin_context = catalog.CommandContext(
        user_id=2,
        group_id=10,
        is_group_admin=True,
    )

    member_sections = help_panel._collect_visible_sections(
        context=member_context,
        enabled_features={"game"},
    )
    admin_sections = help_panel._collect_visible_sections(
        context=admin_context,
        enabled_features={"game"},
    )

    assert [view.section.key for view in member_sections] == ["basic"]
    assert [view.section.key for view in admin_sections] == ["basic", "admin"]
    assert [command.name for command in member_sections[0].groups[0][1]] == ["入口"]
    assert member_sections[0].groups[0][2] == "只显示当前可用操作。"

    menu = help_panel._build_section_menu(admin_sections)
    assert "个人与基础功能" in menu
    assert "管理功能" in menu
    assert "/入口" not in menu
    assert "/管理命令" not in menu
    assert "只显示当前可用操作" not in menu


def test_help_resolves_direct_topic_and_menu_number(
    catalog_modules: dict[str, Any],
) -> None:
    catalog = catalog_modules["catalog"]
    help_panel = catalog_modules["help"]
    group = catalog.PluginCommandGroup(
        plugin_id="fixture.agent",
        display_name="群聊 Agent",
        entrypoint="Agent状态",
        help_section="agent",
        commands=(
            catalog.CommandSpec(
                name="Agent状态",
                description="查看状态",
                aliases=("群AI状态",),
            ),
        ),
    )
    view = help_panel.HelpSectionView(
        next(section for section in help_panel.HELP_SECTIONS if section.key == "agent"),
        ((group, group.commands, "当前阶段提示"),),
    )
    sections = (view,)

    assert help_panel._resolve_section("1", sections) is view
    assert help_panel._resolve_section("Agent", sections) is view
    assert help_panel._resolve_section("群聊 Agent", sections) is view
    assert help_panel._resolve_section("不存在", sections) is None

    detail = help_panel._build_section_text(view)
    assert "/Agent状态" in detail
    assert "/群AI状态" in detail
    assert "提示：当前阶段提示" in detail


def test_rpg_availability_is_owned_by_rpg_state(
    catalog_modules: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = catalog_modules["catalog"]
    context_module = catalog_modules["rpg_context"]
    state = catalog_modules["rpg_state"]
    game = state.Game(group_id=10, host_user_id=1, signup_user_ids=[1])
    current_game = None
    monkeypatch.setattr(context_module, "get_game", lambda _group_id: current_game)
    monkeypatch.setattr(context_module, "game_of_user", lambda _user_id: current_game)

    host_context = catalog.CommandContext(user_id=1, group_id=10)
    guest_context = catalog.CommandContext(user_id=2, group_id=10)
    assert context_module.get_available_commands(host_context) == {
        "跑团",
        "模组列表",
        "跑团帮助",
    }

    current_game = game
    host_signup = context_module.get_available_commands(host_context)
    guest_signup = context_module.get_available_commands(guest_context)

    assert {"选择模组", "开始游戏", "退报名"} <= host_signup
    assert "报名" in guest_signup
    assert "跑团" not in host_signup
    assert "检定" not in host_signup

    game.phase = state.Phase.CHAR_CREATE
    group_card = context_module.get_available_commands(host_context)
    private_card = context_module.get_available_commands(
        catalog.CommandContext(user_id=1, group_id=None)
    )
    assert group_card == {"跑团帮助", "局面", "结束游戏"}
    assert private_card == {"跑团帮助"}
    assert "建卡阶段" in context_module.get_help_hint(host_context)

    game.phase = state.Phase.PLAY
    game.players = [state.PlayerState(user_id=1, seat=1)]
    game.current_scene = "scene"
    game.pending_deduction = state.PendingDeduction(
        proposer_user_id=1,
        clue_ids=("clue",),
        conclusion="结论",
        scene_id="scene",
        explore_round=1,
    )
    active = context_module.get_available_commands(host_context)

    assert {"状态", "检定", "撤回推理"} <= active
    assert "报名" not in active
    assert "NPC 对话直接说话" in context_module.get_help_hint(host_context)

    game.combat_order = [1]
    combat = context_module.get_available_commands(host_context)
    assert {"攻击", "跳过"} <= combat
    assert "检定" not in combat
    assert "撤回推理" not in combat


def test_werewolf_availability_tracks_role_and_action_window(
    catalog_modules: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = catalog_modules["catalog"]
    context_module = catalog_modules["ww_context"]
    state = catalog_modules["ww_state"]
    roles = catalog_modules["ww_roles"]
    game = state.Game(group_id=20, host_user_id=1, signup_user_ids=[1])
    current_game = None
    monkeypatch.setattr(context_module, "get_game", lambda _group_id: current_game)
    monkeypatch.setattr(context_module, "game_of_user", lambda _user_id: current_game)

    host_context = catalog.CommandContext(user_id=1, group_id=20)
    assert context_module.get_available_commands(host_context) == {"狼人杀", "战绩"}
    assert "创建房间后" in context_module.get_help_hint(host_context)

    current_game = game
    signup = context_module.get_available_commands(
        host_context
    )
    guest_signup = context_module.get_available_commands(
        catalog.CommandContext(user_id=2, group_id=20)
    )
    assert {"查看报名", "板子", "开始游戏", "添加AI"} <= signup
    assert "板子" not in guest_signup
    assert "狼人杀" not in signup
    assert "狼人状态" not in signup
    assert "投票" not in signup
    monkeypatch.setattr(context_module.config, "ww_role_request", True)
    private_signup = context_module.get_available_commands(
        catalog.CommandContext(user_id=1, group_id=None)
    )
    assert private_signup == {"选身份", "取消选身份"}

    player = state.PlayerState(
        user_id=1,
        seat=1,
        role=roles.Role.SEER,
        faction=roles.Faction.GOOD,
    )
    game.players = [player]
    game.phase = state.Phase.NIGHT_SEER
    private_commands = context_module.get_available_commands(
        catalog.CommandContext(user_id=1, group_id=None)
    )
    assert {"身份", "查验"} <= private_commands
    assert "刀" not in private_commands

    game.phase = state.Phase.DAY_VOTE
    group_commands = context_module.get_available_commands(
        catalog.CommandContext(user_id=1, group_id=20)
    )
    assert {"狼人状态", "投票", "弃票"} <= group_commands
    assert "查验" not in group_commands
    assert "战绩" not in group_commands

    player.role = roles.Role.WEREWOLF
    wolf_group_commands = context_module.get_available_commands(
        catalog.CommandContext(user_id=1, group_id=20)
    )
    assert "自爆" not in wolf_group_commands

    player.role = roles.Role.HUNTER
    player.alive = False
    game.phase = state.Phase.HUNTER_SHOT
    hunter_commands = context_module.get_available_commands(
        catalog.CommandContext(user_id=1, group_id=None)
    )
    assert {"身份", "开枪", "不开枪"} <= hunter_commands
