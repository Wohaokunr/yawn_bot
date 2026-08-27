"""跑团帮助目录的只读动态可用性判断。"""

from ..command_catalog import CommandContext  # noqa: TID252
from .state import Game, Phase, game_of_user, get_game

_ENTRY_COMMANDS = frozenset({"跑团", "模组列表", "跑团帮助"})
_MIN_DEDUCTION_CLUES = 2


def _signup_commands(context: CommandContext, game: Game) -> set[str]:
    available = {"查看报名"}
    if context.user_id in game.signup_user_ids:
        available.add("退报名")
    else:
        available.add("报名")
    if (
        context.can_manage_room
    ):
        available.update({"选择模组", "开始游戏", "结束游戏"})
    return available


def _scene_commands(game: Game) -> set[str]:
    if game.current_scene is None:
        return set()
    available = {"检定", "等待", "跳过"}
    if len(game.active_players()) > 1:
        available.add("协助")
    if game.module is not None:
        scene = game.module.scene(game.current_scene)
        if scene is not None and scene.exits:
            available.add("前往")
    return available


def _has_private_clue(context: CommandContext, game: Game) -> bool:
    return any(
        context.user_id in owners and clue_id not in game.public_clues
        for clue_id, owners in game.clue_owners.items()
    )


def _has_private_fact(context: CommandContext, game: Game) -> bool:
    return any(
        context.user_id == user_id
        and bool(fact_ids - game.npc_public_facts.get(npc_id, set()))
        for (npc_id, user_id), fact_ids in game.npc_unlocked_facts.items()
    )


def _can_propose_deduction(context: CommandContext, game: Game) -> bool:
    if game.module is None or not game.module.deductions:
        return False
    visible_clues = set(game.public_clues)
    visible_clues.update(
        clue_id
        for clue_id, owners in game.clue_owners.items()
        if context.user_id in owners
    )
    return len(visible_clues) >= _MIN_DEDUCTION_CLUES


def _has_attack_target(game: Game) -> bool:
    if game.module is None or game.current_scene is None:
        return False
    scene = game.module.scene(game.current_scene)
    if scene is None:
        return False
    if any(target not in game.dead_monsters for target in scene.monsters):
        return True
    return any(
        target not in game.dead_npcs and game.npc_present(target) is not None
        for target in scene.npcs
    )


def _exploration_commands(context: CommandContext, game: Game) -> set[str]:
    available = _scene_commands(game)
    if _has_attack_target(game):
        available.add("攻击")
    if _has_private_clue(context, game):
        available.add("分享线索")
    if _has_private_fact(context, game):
        available.add("分享情报")
    pending = game.pending_deduction
    if pending is None:
        if _can_propose_deduction(context, game):
            available.add("推理")
    elif pending.proposer_user_id == context.user_id:
        available.add("撤回推理")
    elif context.user_id not in pending.confirmations:
        available.add("赞成推理")
    return available


def _play_commands(context: CommandContext, game: Game) -> set[str]:
    available = {"时间", "线索板"}
    player = game.player_by_user(context.user_id)
    if player is None:
        return available
    available.update({"状态", "技能", "线索"})
    if player.incapped:
        return available
    if game.combat_order:
        current_user_id = game.combat_order[game.combat_index]
        if current_user_id == context.user_id:
            available.update({"攻击", "跳过"})
        return available
    available.update(_exploration_commands(context, game))
    return available


def get_available_commands(context: CommandContext) -> frozenset[str]:
    """按当前房间、玩家身份和行动窗口返回可用命令名。"""

    game = (
        get_game(context.group_id)
        if context.group_id is not None
        else game_of_user(context.user_id)
    )
    if game is None:
        return _ENTRY_COMMANDS

    privileged = context.can_manage_room
    if game.phase is Phase.SIGNUP:
        if context.group_id is None:
            return frozenset({"跑团帮助"})
        available = {"模组列表", "跑团帮助"}
        available.update(_signup_commands(context, game))
        return frozenset(available)

    available = {"跑团帮助"}
    if context.group_id is not None:
        available.add("局面")
        if privileged:
            available.add("结束游戏")
    if game.phase is not Phase.PLAY or context.group_id is None:
        return frozenset(available)

    available.update(_play_commands(context, game))
    return frozenset(available)


def get_help_hint(context: CommandContext) -> str | None:
    """补充当前阶段的非 slash 操作说明。"""

    game = (
        get_game(context.group_id)
        if context.group_id is not None
        else game_of_user(context.user_id)
    )
    if game is None:
        return None
    if game.phase is Phase.SIGNUP:
        return "报名后请先加机器人好友，以接收角色卡和个人线索。"
    if game.phase is Phase.CHAR_CREATE:
        return "当前为建卡阶段：参与者请在私聊中按角色卡提示完成确认或调整。"
    if game.phase is Phase.PLAY:
        return "大部分探索和 NPC 对话直接说话即可，不需要背指令。"
    return None
