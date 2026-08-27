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
        "panel_data": importlib.import_module("src.plugins.yawn_core.panel_data"),
        "panel_menu": importlib.import_module("src.plugins.yawn_core.panel_menu"),
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
    member_context = catalog.CommandAccessContext(
        user_id=1,
        group_id=10,
        enabled_features=frozenset({"game"}),
    )
    admin_context = catalog.CommandContext(
        user_id=2,
        group_id=10,
        enabled_features=frozenset({"game"}),
        is_group_admin=True,
    )

    member_sections = help_panel._collect_visible_sections(
        context=member_context,
    )
    admin_sections = help_panel._collect_visible_sections(
        context=admin_context,
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


def test_command_access_context_owns_shared_visibility_rules(
    catalog_modules: dict[str, Any],
) -> None:
    catalog = catalog_modules["catalog"]
    context = catalog.CommandAccessContext(
        user_id=1,
        group_id=10,
        enabled_features=frozenset({"rpg"}),
        is_room_host=True,
        is_player=True,
        current_game="rpg",
    )

    assert context.chat_scope == "group"
    assert context.can_manage_room is True
    assert context.allows(scope="group", feature="rpg", permission="room_host_or_admin")
    assert context.allows(scope="group", feature="rpg", permission="player")
    assert not context.allows(scope="private", feature="rpg", permission="everyone")
    assert not context.allows(scope="group", feature="werewolf", permission="everyone")
    assert not context.allows(scope="group", feature="rpg", permission="superuser")


def test_game_registry_exposes_host_player_and_current_game() -> None:
    registry = importlib.import_module("src.plugins.yawn_core.game_registry")
    registry.reset_for_tests()
    try:
        assert registry.reserve_game("rpg", 10, 1)
        assert registry.reserve_user("rpg", 10, 2)

        host = registry.resolve_game_access(10, 1)
        player = registry.resolve_game_access(None, 2)
        outsider = registry.resolve_game_access(10, 3)

        assert host is not None and host.is_room_host and host.is_player
        assert player is not None and player.kind == "rpg" and player.is_player
        assert outsider is not None and not outsider.is_player
    finally:
        registry.reset_for_tests()


def test_panel_menu_uses_typed_navigation_state(
    catalog_modules: dict[str, Any],
) -> None:
    panel_data = catalog_modules["panel_data"]
    panel_menu = catalog_modules["panel_menu"]
    flow = panel_menu.PanelFlow(user_id=1, mode=panel_menu.PanelMode.PRIVATE)
    group = panel_data.GroupListItem(
        group_id=10,
        group_name="测试群",
        is_admin=True,
        last_active_at=None,
        first_seen_at=None,
        last_seen_at=None,
    )

    assert flow.view is panel_menu.PanelView.MAIN
    flow.view = panel_menu.PanelView.GROUPS
    flow.groups = (group,)
    menu = panel_menu.group_list_text(flow.groups)
    assert "[管理员] 测试群 (10)" in menu
    assert "返回 上一级" in menu
    assert "菜单 重新显示" in menu


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


def test_operation_selection_uses_filtered_command_metadata_only(
    catalog_modules: dict[str, Any],
) -> None:
    catalog = catalog_modules["catalog"]
    help_panel = catalog_modules["help"]

    def view_for(commands: tuple[Any, ...]) -> Any:
        group = catalog.PluginCommandGroup(
            plugin_id=f"fixture.operation.{commands[0].name}",
            display_name="样本",
            entrypoint=commands[0].name,
            commands=commands,
        )
        section = next(
            section for section in help_panel.HELP_SECTIONS if section.key == "basic"
        )
        return help_panel.HelpSectionView(section, ((group, commands, None),))

    lobby = (
        catalog.CommandSpec("报名", "加入", display_level="lobby"),
        catalog.CommandSpec("查看报名", "名单", display_level="lobby"),
        catalog.CommandSpec(
            "开始游戏",
            "开始",
            permission="room_host_or_admin",
            display_level="lobby",
        ),
        catalog.CommandSpec("板子", "设置", display_level="advanced"),
        catalog.CommandSpec(
            "结束游戏",
            "结束",
            permission="room_host_or_admin",
            display_level="contextual",
        ),
    )
    assert [
        command.name
        for command in help_panel._select_operation_commands((view_for(lobby),))
    ] == ["报名", "查看报名", "开始游戏"]

    seer = (
        catalog.CommandSpec(
            "身份",
            "身份卡",
            scope="private",
            display_level="active",
            operation_support=True,
        ),
        catalog.CommandSpec(
            "查验",
            "查验目标",
            scope="private",
            display_level="contextual",
        ),
    )
    assert [
        command.name
        for command in help_panel._select_operation_commands((view_for(seer),))
    ] == ["查验"]

    combat = (
        catalog.CommandSpec("跑团帮助", "帮助", display_level="entry"),
        catalog.CommandSpec(
            "状态",
            "个人状态",
            display_level="active",
            operation_support=True,
        ),
        catalog.CommandSpec("攻击", "攻击目标", display_level="contextual"),
        catalog.CommandSpec("跳过", "跳过行动", display_level="contextual"),
    )
    assert [
        command.name
        for command in help_panel._select_operation_commands((view_for(combat),))
    ] == ["攻击", "跳过", "状态"]


def test_second_level_groups_current_commands_by_purpose(
    catalog_modules: dict[str, Any],
) -> None:
    catalog = catalog_modules["catalog"]
    help_panel = catalog_modules["help"]
    sections = help_panel._build_command_sections(
        (
            catalog.CommandSpec("攻击", "攻击", display_level="contextual"),
            catalog.CommandSpec("状态", "状态", display_level="active"),
            catalog.CommandSpec(
                "结束游戏",
                "结束",
                permission="room_host_or_admin",
                display_level="contextual",
            ),
        )
    )

    assert [section.title for section in sections] == [
        "推荐操作",
        "常用",
        "管理操作",
    ]
    assert [[command.name for command in section.commands] for section in sections] == [
        ["攻击"],
        ["状态"],
        ["结束游戏"],
    ]


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

    host_context = catalog.CommandContext(
        user_id=1, group_id=10, is_room_host=True, is_player=True
    )
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
        catalog.CommandContext(
            user_id=1, group_id=None, is_room_host=True, is_player=True
        )
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

    host_context = catalog.CommandContext(
        user_id=1, group_id=20, is_room_host=True, is_player=True
    )
    assert context_module.get_available_commands(host_context) == {"狼人杀", "战绩"}
    assert "创建房间后" in context_module.get_help_hint(host_context)

    current_game = game
    signup = context_module.get_available_commands(host_context)
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
        catalog.CommandContext(
            user_id=1, group_id=None, is_room_host=True, is_player=True
        )
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
        catalog.CommandContext(
            user_id=1, group_id=None, is_room_host=True, is_player=True
        )
    )
    assert {"身份", "查验"} <= private_commands
    assert "刀" not in private_commands

    game.phase = state.Phase.DAY_VOTE
    group_commands = context_module.get_available_commands(
        catalog.CommandContext(
            user_id=1, group_id=20, is_room_host=True, is_player=True
        )
    )
    assert {"狼人状态", "投票", "弃票"} <= group_commands
    assert "查验" not in group_commands
    assert "战绩" not in group_commands

    player.role = roles.Role.WEREWOLF
    wolf_group_commands = context_module.get_available_commands(
        catalog.CommandContext(
            user_id=1, group_id=20, is_room_host=True, is_player=True
        )
    )
    assert "自爆" not in wolf_group_commands

    player.role = roles.Role.HUNTER
    player.alive = False
    game.phase = state.Phase.HUNTER_SHOT
    hunter_commands = context_module.get_available_commands(
        catalog.CommandContext(
            user_id=1, group_id=None, is_room_host=True, is_player=True
        )
    )
    assert {"身份", "开枪", "不开枪"} <= hunter_commands


def test_large_plugins_use_command_definitions_as_single_source(
    catalog_modules: dict[str, Any],
) -> None:
    catalog = catalog_modules["catalog"]
    definition_api = importlib.import_module("src.plugins.yawn_core.command_definition")
    groups = {
        group.plugin_id: group for group in catalog.get_registered_command_groups()
    }

    for plugin_id in ("yawn_agent", "yawn_rpg", "yawn_werewolf"):
        definitions = importlib.import_module(
            f"src.plugins.yawn_core.{plugin_id}.command_definitions"
        ).COMMAND_DEFINITIONS
        assert groups[plugin_id].commands is definitions
        assert all(
            isinstance(command, definition_api.CommandDefinition)
            for command in definitions
        )


def test_namespaced_commands_and_short_aliases_share_one_definition(
    catalog_modules: dict[str, Any],
) -> None:
    _ = catalog_modules
    agent_defs = importlib.import_module(
        "src.plugins.yawn_core.yawn_agent.command_definitions"
    ).COMMAND_BY_NAME
    rpg_defs = importlib.import_module(
        "src.plugins.yawn_core.yawn_rpg.command_definitions"
    ).COMMAND_BY_NAME
    wolf_defs = importlib.import_module(
        "src.plugins.yawn_core.yawn_werewolf.command_definitions"
    ).COMMAND_BY_NAME

    assert agent_defs["Agent设置"].qualified_name == "Agent 设置"
    assert "Agent 设置" in agent_defs["Agent设置"].matcher_aliases()
    assert agent_defs["Agent设置"].help_aliases[0] == "Agent设置"

    assert rpg_defs["报名"].qualified_name == "跑团 报名"
    assert {"上车", "加一"} <= rpg_defs["报名"].matcher_aliases()
    assert "跑团 报名" in rpg_defs["报名"].matcher_aliases()
    assert "跑团 上车" in rpg_defs["报名"].matcher_aliases()

    assert wolf_defs["狼人状态"].qualified_name == "狼人杀 状态"
    assert "狼人杀 状态" in wolf_defs["狼人状态"].matcher_aliases()
    assert "狼人杀 板子" in wolf_defs["板子"].matcher_aliases()


def test_explicit_namespace_bypasses_short_command_game_routing(
    catalog_modules: dict[str, Any],
) -> None:
    _ = catalog_modules
    definition_api = importlib.import_module("src.plugins.yawn_core.command_definition")
    registry = importlib.import_module("src.plugins.yawn_core.game_registry")
    rpg_defs = importlib.import_module(
        "src.plugins.yawn_core.yawn_rpg.command_definitions"
    ).COMMAND_BY_NAME
    wolf_defs = importlib.import_module(
        "src.plugins.yawn_core.yawn_werewolf.command_definitions"
    ).COMMAND_BY_NAME
    signup = rpg_defs["报名"]
    wolf_end = wolf_defs["结束游戏"]

    registry.reset_for_tests()
    assert not definition_api.command_context_matches(
        signup, group_id=10, cmd=("报名",)
    )
    assert definition_api.command_context_matches(
        signup, group_id=10, cmd=("跑团 报名",)
    )
    assert definition_api.command_context_matches(
        signup, group_id=10, cmd=("跑团 上车",)
    )

    assert registry.reserve_game("werewolf", 10, 1)
    assert not definition_api.command_context_matches(
        signup, group_id=10, cmd=("报名",)
    )
    assert definition_api.command_context_matches(
        signup, group_id=10, cmd=("跑团 报名",)
    )
    registry.reset_for_tests()

    assert definition_api.command_context_matches(
        wolf_end, group_id=10, cmd=("解散狼局",)
    )
    assert registry.reserve_game("rpg", 10, 1)
    assert not definition_api.command_context_matches(
        wolf_end, group_id=10, cmd=("解散狼局",)
    )
    registry.reset_for_tests()


def test_help_prefers_namespaced_command_and_shows_compatibility_shortcut(
    catalog_modules: dict[str, Any],
) -> None:
    catalog = catalog_modules["catalog"]
    help_panel = catalog_modules["help"]
    signup = importlib.import_module(
        "src.plugins.yawn_core.yawn_rpg.command_definitions"
    ).COMMAND_BY_NAME["报名"]
    group = catalog.PluginCommandGroup(
        plugin_id="fixture.rpg.namespace",
        display_name="跑团",
        entrypoint="报名",
        help_section="rpg",
        commands=(signup,),
    )
    view = help_panel.HelpSectionView(
        next(section for section in help_panel.HELP_SECTIONS if section.key == "rpg"),
        ((group, group.commands, None),),
    )

    detail = help_panel._build_section_text(view)
    assert "/跑团 报名" in detail
    assert "别名：/报名、/上车、/加一" in detail


def test_namespaced_commands_register_literal_space_prefixes(
    catalog_modules: dict[str, Any],
) -> None:
    _ = catalog_modules
    from nonebot.rule import TrieRule

    expected = {
        "/Agent 设置": ("Agent 设置",),
        "/Agent 人设": ("Agent 人设",),
        "/跑团 报名": ("跑团 报名",),
        "/跑团 局面": ("跑团 局面",),
        "/狼人杀 报名": ("狼人杀 报名",),
        "/狼人杀 状态": ("狼人杀 状态",),
        "/狼人杀 板子": ("狼人杀 板子",),
    }
    for prefix, command in expected.items():
        assert prefix in TrieRule.prefix
        assert TrieRule.prefix[prefix].command == command

    assert "/Agent.设置" not in TrieRule.prefix
    assert "/跑团.报名" not in TrieRule.prefix
    assert "/狼人杀.报名" not in TrieRule.prefix
