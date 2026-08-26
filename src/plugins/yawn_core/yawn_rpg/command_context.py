"""跑团帮助目录的只读动态可用性判断。"""

from ..command_catalog import CommandContext  # noqa: TID252
from .state import Game, Phase, get_game

_ENTRY_COMMANDS = frozenset({"跑团", "模组列表", "跑团帮助"})
_ADVANCED_COMMANDS = frozenset({"跳过引导", "重新引导"})


def _signup_commands(context: CommandContext, game: Game) -> set[str]:
    available = {"查看报名"}
    if context.user_id in game.signup_user_ids:
        available.add("退报名")
    else:
        available.add("报名")
    if (
        context.user_id == game.host_user_id
        or context.is_group_admin
        or context.is_superuser
    ):
        available.update({"选择模组", "开始游戏", "结束游戏"})
    return available


def _play_commands(context: CommandContext, game: Game) -> set[str]:
    available = {"时间", "线索板"}
    player = game.player_by_user(context.user_id)
    if player is None or player.incapped:
        return available
    available.update(
        {
            "状态",
            "技能",
            "线索",
            "检定",
            "前往",
            "等待",
            "协助",
            "分享线索",
            "推理",
            "分享情报",
            "跳过",
        }
    )
    if game.combat_order:
        available.add("攻击")
    pending = game.pending_deduction
    if pending is not None and pending.proposer_user_id == context.user_id:
        available.add("撤回推理")
    elif pending is not None and context.user_id not in pending.confirmations:
        available.add("赞成推理")
    return available


def get_available_commands(context: CommandContext) -> frozenset[str]:
    """按当前房间、玩家身份和行动窗口返回可用命令名。"""

    available = set(_ENTRY_COMMANDS | _ADVANCED_COMMANDS)
    if context.group_id is None:
        return frozenset(available)

    game = get_game(context.group_id)
    if game is None:
        return frozenset(available)

    privileged = (
        context.user_id == game.host_user_id
        or context.is_group_admin
        or context.is_superuser
    )
    if game.phase is Phase.SIGNUP:
        available.update(_signup_commands(context, game))
        return frozenset(available)

    available.add("局面")
    if privileged:
        available.add("结束游戏")
    if game.phase is not Phase.PLAY:
        return frozenset(available)

    available.update(_play_commands(context, game))
    return frozenset(available)
