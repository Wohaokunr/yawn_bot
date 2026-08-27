"""狼人杀帮助目录的只读动态可用性判断。"""

from ..command_catalog import CommandContext  # noqa: TID252
from .commands import config
from .roles import Role
from .state import Game, Phase, PlayerState, game_of_user, get_game

_ENTRY_COMMANDS = frozenset({"狼人杀", "战绩"})
_VOTE_PHASES = frozenset(
    {Phase.SHERIFF_VOTE, Phase.SHERIFF_REVOTE, Phase.DAY_VOTE, Phase.PK_VOTE}
)
_SPEECH_PHASES = frozenset(
    {
        Phase.LAST_WORDS,
        Phase.SHERIFF_SPEECH,
        Phase.SHERIFF_FINAL_SPEECH,
        Phase.DAY_SPEECH,
        Phase.PK_SPEECH,
    }
)
def _signup_commands(context: CommandContext, game: Game) -> set[str]:
    if context.group_id is None:
        if config.ww_role_request and context.user_id in game.signup_user_ids:
            return {"选身份", "取消选身份"}
        return set()

    available = {"查看报名"}
    available.add("退报名" if context.user_id in game.signup_user_ids else "报名")
    if (
        context.can_manage_room
    ):
        available.update({"板子", "开始游戏", "结束游戏", "添加AI", "移除AI"})
    return available


def _inactive_player_commands(game: Game, player: PlayerState | None) -> set[str]:
    if player is None or game.phase is not Phase.BADGE_TRANSFER:
        return set()
    return {"移交警徽", "撕警徽"} if game.sheriff() is player else set()


def _group_action_commands(game: Game, player: PlayerState) -> set[str]:
    available: set[str] = set()
    if game.phase is Phase.SHERIFF_REGISTER:
        available.add("退水" if player.sheriff_candidate else "上警")
    elif game.phase is Phase.SHERIFF_SPEECH and player.sheriff_candidate:
        available.add("退水")
    if (
        game.phase in _VOTE_PHASES
        and player.can_vote
        and player.seat not in game.vote_exclude
    ):
        available.update({"投票", "弃票"})
    if game.phase in _SPEECH_PHASES and game.current_speaker == player.seat:
        available.add("过")
    if (
        game.phase is Phase.DAY_SPEECH
        and game.current_speaker is None
        and player.is_sheriff
    ):
        available.add("排序")
    return available


def _private_role_actions(game: Game, player: PlayerState) -> set[str]:
    """只返回该身份在当前私聊窗口仍可执行的动作。"""

    available: set[str] = set()
    if game.phase is Phase.HUNTER_SHOT and player.role is Role.HUNTER:
        available.update({"开枪", "不开枪"})
    elif not player.alive:
        pass
    elif (
        game.phase is Phase.NIGHT_HALFBLOOD
        and player.role is Role.HALFBLOOD
        and player.owner_seat is None
    ):
        available.add("认主")
    elif game.phase is Phase.NIGHT_WOLVES and player.role is Role.WEREWOLF:
        available.add("刀")
    elif game.phase is Phase.NIGHT_WITCH and player.role is Role.WITCH:
        if not player.save_used:
            available.add("救")
        if not player.poison_used:
            available.add("毒")
    elif game.phase is Phase.NIGHT_SEER and player.role is Role.SEER:
        available.add("查验")
    elif game.phase is Phase.NIGHT_ELDER and player.role is Role.SILENT_ELDER:
        available.add("禁言")
    return available


def get_available_commands(context: CommandContext) -> frozenset[str]:
    """按阶段、玩家身份和当前行动窗口返回可用命令名。"""

    available: set[str] = set()
    game = (
        get_game(context.group_id)
        if context.group_id is not None
        else game_of_user(context.user_id)
    )
    if game is None:
        return _ENTRY_COMMANDS

    player = game.player_by_user(context.user_id)

    if game.phase is Phase.SIGNUP:
        available.update(_signup_commands(context, game))
        return frozenset(available)

    if context.group_id is not None:
        available.add("狼人状态")
        if (
            context.can_manage_room
        ):
            available.add("结束游戏")
    elif player is not None:
        available.add("身份")

    if context.group_id is None and player is not None:
        available.update(_private_role_actions(game, player))
        return frozenset(available)

    if player is None or not player.alive:
        available.update(_inactive_player_commands(game, player))
        return frozenset(available)

    available.update(_group_action_commands(game, player))
    return frozenset(available)


def get_help_hint(context: CommandContext) -> str | None:
    """给无房间场景补一条状态说明，不预告具体游戏结构。"""

    if context.group_id is not None and get_game(context.group_id) is None:
        return "创建房间后会自动显示报名和游戏相关操作。"
    return None
