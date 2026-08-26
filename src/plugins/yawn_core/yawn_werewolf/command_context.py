"""狼人杀帮助目录的只读动态可用性判断。"""

from ..command_catalog import CommandContext  # noqa: TID252
from .commands import config
from .roles import Role
from .state import (
    DUEL_PHASES,
    SELF_DETONATE_PHASES,
    Game,
    Phase,
    PlayerState,
    game_of_user,
    get_game,
)

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
_PRIVATE_ROLE_ACTIONS = {
    (Phase.NIGHT_HALFBLOOD, Role.HALFBLOOD): frozenset({"认主"}),
    (Phase.NIGHT_WOLVES, Role.WEREWOLF): frozenset({"刀"}),
    (Phase.NIGHT_WITCH, Role.WITCH): frozenset({"救", "毒"}),
    (Phase.NIGHT_SEER, Role.SEER): frozenset({"查验"}),
    (Phase.NIGHT_ELDER, Role.SILENT_ELDER): frozenset({"禁言"}),
    (Phase.HUNTER_SHOT, Role.HUNTER): frozenset({"开枪", "不开枪"}),
}


def _signup_commands(context: CommandContext, game: Game) -> set[str]:
    if context.group_id is None:
        if config.ww_role_request and context.user_id in game.signup_user_ids:
            return {"选身份", "取消选身份"}
        return set()

    available = {"查看报名", "狼人状态", "板子"}
    available.add("退报名" if context.user_id in game.signup_user_ids else "报名")
    if (
        context.user_id == game.host_user_id
        or context.is_group_admin
        or context.is_superuser
    ):
        available.update({"开始游戏", "结束游戏", "添加AI", "移除AI"})
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
    if game.phase in _VOTE_PHASES and player.can_vote:
        available.update({"投票", "弃票"})
    if game.phase in _SPEECH_PHASES and game.current_speaker == player.seat:
        available.add("过")
    if game.phase is Phase.DAY_SPEECH and player.is_sheriff:
        available.add("排序")
    if game.phase in SELF_DETONATE_PHASES and player.role is Role.WEREWOLF:
        available.add("自爆")
    if game.phase in DUEL_PHASES and player.role is Role.KNIGHT:
        available.add("决斗")
    return available


def get_available_commands(context: CommandContext) -> frozenset[str]:
    """按阶段、玩家身份和当前行动窗口返回可用命令名。"""

    available = set(_ENTRY_COMMANDS)
    game = (
        get_game(context.group_id)
        if context.group_id is not None
        else game_of_user(context.user_id)
    )
    if game is None:
        if context.group_id is not None and (
            context.is_group_admin or context.is_superuser
        ):
            available.add("结束游戏")
        return frozenset(available)

    player = game.player_by_user(context.user_id)

    if game.phase is Phase.SIGNUP:
        available.update(_signup_commands(context, game))
        return frozenset(available)

    if context.group_id is not None:
        available.add("狼人状态")
        if (
            context.user_id == game.host_user_id
            or context.is_group_admin
            or context.is_superuser
        ):
            available.add("结束游戏")
    elif player is not None:
        available.add("身份")

    if player is None or not player.alive:
        available.update(_inactive_player_commands(game, player))
        return frozenset(available)

    if context.group_id is None:
        available.update(_PRIVATE_ROLE_ACTIONS.get((game.phase, player.role), ()))
        return frozenset(available)

    available.update(_group_action_commands(game, player))
    return frozenset(available)
