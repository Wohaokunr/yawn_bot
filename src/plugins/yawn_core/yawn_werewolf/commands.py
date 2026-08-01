"""狼人杀命令入口：群命令、私聊命令与私聊自由文本监听。

处理器只做轻量校验（在局状态、阶段），把行动投入
game.action_queue 交给引擎裁决；私聊自由文本监听器在
行动阶段拦截在局玩家的私聊（其余私聊放行给 ai_chat）。
"""

import asyncio
import re
from typing import Optional

from nonebot import (
    get_driver,
    get_plugin_config,
    logger,
    on_command,
    on_message,
)
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.params import CommandArg
from nonebot.rule import Rule
from nonebot_plugin_orm import async_scoped_session, get_session
from sqlalchemy import select

from ..permission import (  # noqa: TID252
    check_feature_permission,
    is_group_admin,
    require_feature,
)
from . import ai_player, api, engine
from .config import Config
from .dsl import _DM_HINT, parse_dm_action
from .models import WerewolfPlayer
from .roles import BOARDS, Role
from .state import (
    DUEL_PHASES,
    SELF_DETONATE_PHASES,
    Action,
    ActionKind,
    Game,
    Phase,
    PlayerState,
    add_ai_signup,
    count_ai_signup,
    create_game,
    display_name_of,
    game_of_user,
    get_game,
    is_ai_uid,
    join_signup,
    leave_signup,
    note_signup_name,
    remove_ai_signup,
    stop_game,
)

config = get_plugin_config(Config)

_VOTE_PHASES: frozenset[Phase] = frozenset(
    {
        Phase.SHERIFF_VOTE,
        Phase.SHERIFF_REVOTE,
        Phase.DAY_VOTE,
        Phase.PK_VOTE,
    }
)


def _is_su(user_id: int) -> bool:
    """是否为超级用户。"""
    return str(user_id) in get_driver().config.superusers


def _eff_limits(game: Game) -> tuple[int, int]:
    """报名阶段的有效人数区间（配置项与板子支持人数的交集）。"""
    board = BOARDS[game.board]
    return (
        max(config.ww_min_players, min(board.counts)),
        min(config.ww_max_players, max(board.counts)),
    )


def _parse_seat(text: object) -> Optional[int]:
    """从参数文本解析座位号（支持 "3" 与 "3号"）。"""
    match = re.fullmatch(r"(\d+)\s*号?", str(text).strip())
    return int(match.group(1)) if match else None


# 阶段中文名（报错/提示用）。夜间子阶段一律折叠为"夜晚"：
# 报错与提示不得暴露当前轮到哪个角色行动
_PHASE_CN: dict[Phase, str] = {
    Phase.SIGNUP: "报名中",
    Phase.NIGHT_HALFBLOOD: "夜晚",
    Phase.NIGHT_WOLVES: "夜晚",
    Phase.NIGHT_WITCH: "夜晚",
    Phase.NIGHT_SEER: "夜晚",
    Phase.NIGHT_ELDER: "夜晚",
    Phase.DAY_ANNOUNCE: "天亮结算",
    Phase.LAST_WORDS: "遗言环节",
    Phase.HUNTER_SHOT: "猎人开枪决策",
    Phase.BADGE_TRANSFER: "警徽移交",
    Phase.SHERIFF_REGISTER: "警长竞选报名",
    Phase.SHERIFF_SPEECH: "竞选发言",
    Phase.SHERIFF_VOTE: "警长投票",
    Phase.SHERIFF_REVOTE: "警长终辩投票",
    Phase.DAY_SPEECH: "白天发言",
    Phase.DAY_VOTE: "放逐投票",
    Phase.PK_SPEECH: "PK 发言",
    Phase.PK_VOTE: "PK 投票",
    Phase.ENDED: "已结束",
}


def _not_now(game: Optional[Game], guidance: str) -> str:
    """阶段闸门报错：当前阶段 + 一行该怎么做。"""
    if game is None:
        return f"现在还不是时候~\n{guidance}"
    return f"现在还不是时候~（当前阶段：{_PHASE_CN[game.phase]}）\n{guidance}"


# 各角色可用的私聊行动（提示用；白天行动走群命令，不在此列）
_ROLE_DM_ACTIONS: dict[Role, str] = {
    Role.WEREWOLF: "刀N（击杀）/ 说XXX（与狼队友讨论）",
    Role.WITCH: "救 / 毒N / 过",
    Role.SEER: "查验N",
    Role.HUNTER: "开枪N / 不开枪（死亡后）",
    Role.HALFBLOOD: "认主N（仅首夜）",
    Role.SILENT_ELDER: "禁言N（禁票板为 禁票N）/ 过",
    Role.VILLAGER: "（本角色无夜间行动，耐心等待即可）",
    Role.IDIOT: "（本角色无夜间行动，耐心等待即可）",
    Role.KNIGHT: "（夜间无行动；白天发言阶段可在群内 /决斗 N）",
}


def _dm_hint_for(game: Game, player: Optional[PlayerState], raw: str) -> str:
    """按角色与阶段裁剪的私聊指令提示（替代整面指令墙）。"""
    if player is None:
        return _DM_HINT
    actions = _ROLE_DM_ACTIONS.get(player.role, "（当前无可用的私聊行动）")
    return (
        "═══ 指令提示 ═══\n"
        f"当前阶段：{_PHASE_CN[game.phase]}\n"
        f"你的身份：{player.role.value}\n"
        f"可用私聊指令：{actions}\n"
        f"直接私聊发送即可（无需斜杠）。无法识别：{raw}"
    )


def _sender_display(event: GroupMessageEvent) -> str:
    """报名者显示名：群名片 > 昵称 > QQ 号。"""
    return event.sender.card or event.sender.nickname or str(event.user_id)


# ── 开局与报名 ────────────────────────────────────────────

wolf_open = on_command(
    "狼人杀",
    aliases={"开狼", "来把狼人杀"},
    priority=5,
    block=True,
)


@wolf_open.handle()
async def handle_open(
    event: GroupMessageEvent,
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """开房：创建对局并启动引擎任务，房主自动报名。"""
    group_id = int(event.group_id)
    user_id = int(event.get_user_id())
    if get_game(group_id) is not None:
        await wolf_open.finish("本群已经有正在进行的狼人杀对局~")
    if game_of_user(user_id) is not None:
        await wolf_open.finish("你已经在其他对局中，无法开房~")
    game = create_game(group_id, user_id)
    if game is None:
        await wolf_open.finish("开房失败，请稍后重试")
    note_signup_name(game, user_id, _sender_display(event))
    game.worker = asyncio.create_task(engine.run_game(game))
    logger.info(f"狼人杀群 {group_id} 由 {user_id} 开房")
    await wolf_open.finish("狼人杀房间已创建，房主已自动报名~")


signup_cmd = on_command(
    "报名",
    aliases={"上车", "加一"},
    priority=5,
    block=True,
)


@signup_cmd.handle()
async def handle_signup(
    event: GroupMessageEvent,
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """报名加入当前房间。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.SIGNUP:
        await signup_cmd.finish("本群当前没有报名中的狼人杀对局（发送 /狼人杀 开房）")
    eff_min, eff_max = _eff_limits(game)
    if len(game.signup_user_ids) >= eff_max:
        await signup_cmd.finish("报名已满员，等待开局~")
    if not join_signup(game, int(event.get_user_id())):
        await signup_cmd.finish("你已在局中，无需重复报名~")
    note_signup_name(game, int(event.get_user_id()), _sender_display(event))
    logger.info(
        f"狼人杀群 {int(event.group_id)} {int(event.get_user_id())} 报名"
        f"（{len(game.signup_user_ids)}/{eff_max}）"
    )
    await signup_cmd.finish(
        MessageSegment.at(event.user_id)
        + f"报名成功！当前 {len(game.signup_user_ids)}"
        + f"/{eff_max} 人"
        + f"（至少 {eff_min} 人开局）"
    )


leave_cmd = on_command(
    "退报名",
    aliases={"下车"},
    priority=5,
    block=True,
)


@leave_cmd.handle()
async def handle_leave(
    event: GroupMessageEvent,
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """退出报名；房主退出自动移交，空房自动解散。"""
    game = get_game(int(event.group_id))
    user_id = int(event.get_user_id())
    if game is None or game.phase is not Phase.SIGNUP:
        await leave_cmd.finish("本群当前没有报名中的狼人杀对局")
    if not leave_signup(game, user_id):
        await leave_cmd.finish("你还没有报名~")
    logger.info(f"狼人杀群 {int(event.group_id)} {user_id} 退报名")
    # 只剩 AI 视同空房：AI 无法主持对局
    humans = [uid for uid in game.signup_user_ids if not is_ai_uid(uid)]
    if not humans:
        # 空房：直接取消引擎任务。阶段置 ENDED 后命令层自播"房间已解散"，
        # 引擎取消分支见到 ENDED 不再重复播报"对局已被强制结束"
        game.phase = Phase.ENDED
        task = game.worker
        game.worker = None
        if task is not None and not task.done():
            task.cancel()
        logger.info(f"狼人杀群 {int(event.group_id)} 空房解散")
        await leave_cmd.finish("房间已解散")
    if game.host_user_id == user_id:
        game.host_user_id = humans[0]
        await leave_cmd.finish(f"已退报名，房主移交给 {game.host_user_id}")
    await leave_cmd.finish("已退出报名~")


view_cmd = on_command(
    "查看报名",
    aliases={"报名情况"},
    priority=5,
    block=True,
)


@view_cmd.handle()
async def handle_view(
    event: GroupMessageEvent,
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """查看报名名单。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.SIGNUP:
        await view_cmd.finish("本群当前没有报名中的狼人杀对局")
    eff_min, eff_max = _eff_limits(game)
    lines = [
        "═══ 狼人杀 · 报名名单 ═══",
        f"板子：{game.board}（房主可发 /板子 切换）",
    ]
    for idx, uid in enumerate(game.signup_user_ids, start=1):
        lines.append(f"{idx}. {display_name_of(game, uid)}")
    lines.append("──────────────")
    lines.append(
        f"当前 {len(game.signup_user_ids)}/{eff_max} 人，至少 {eff_min} 人开局"
    )
    await view_cmd.finish("\n".join(lines))


board_cmd = on_command(
    "板子",
    aliases={"选板子", "换板子"},
    priority=5,
    block=True,
)


@board_cmd.handle()
async def handle_board(
    event: GroupMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """报名阶段查看可选板子；房主/群管/超管可切换。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.SIGNUP:
        await board_cmd.finish("本群当前没有报名中的狼人杀对局")
    text = str(arg).strip()
    if not text:
        lines = ["═══ 狼人杀 · 可选板子 ═══"]
        for spec in BOARDS.values():
            marker = "（当前）" if spec.key == game.board else ""
            lines.append(
                f"· {spec.key}{marker}：{spec.roles_summary()}，"
                f"支持 {spec.counts_summary()} 人"
            )
        lines.append("──────────────")
        lines.append("房主发送 /板子 名称 切换（如 /板子 预女猎白混）")
        await board_cmd.finish("\n".join(lines))
    user_id = int(event.get_user_id())
    if not (user_id == game.host_user_id or is_group_admin(event) or _is_su(user_id)):
        await board_cmd.finish("只有房主、群管理员或超管可以切换板子~")
    if text not in BOARDS:
        keys = "、".join(BOARDS)
        await board_cmd.finish(f"没有名为「{text}」的板子。可选：{keys}")
    game.board = text
    spec = BOARDS[text]
    current = len(game.signup_user_ids)
    note = (
        f"\n提示：当前已报名 {current} 人，不在该板子支持的人数范围"
        f"（{spec.counts_summary()} 人），开局前请用 /添加AI 或 /退报名 调整"
        if current not in spec.counts
        else ""
    )
    logger.info(f"狼人杀群 {int(event.group_id)} {user_id} 切换板子为 {text}")
    await board_cmd.finish(
        f"已切换板子：{text}（{spec.roles_summary()}，"
        f"支持 {spec.counts_summary()} 人）{note}"
    )


add_ai_cmd = on_command(
    "添加AI",
    aliases={"加AI", "补人"},
    priority=5,
    block=True,
)


@add_ai_cmd.handle()
async def handle_add_ai(  # noqa: C901
    event: GroupMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """报名阶段添加 AI 玩家（房主/群管/超管）。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.SIGNUP:
        await add_ai_cmd.finish("本群当前没有报名中的狼人杀对局")
    user_id = int(event.get_user_id())
    if not (user_id == game.host_user_id or is_group_admin(event) or _is_su(user_id)):
        await add_ai_cmd.finish("只有房主、群管理员或超管可以添加玩家~")
    if not config.ww_ai_enabled:
        await add_ai_cmd.finish("本群未启用 AI 玩家~")
    text = str(arg).strip()
    count = 1
    if text:
        if not text.isdigit() or int(text) < 1:
            await add_ai_cmd.finish("格式：/添加AI N（N 为人数）")
        count = int(text)
    eff_min, eff_max = _eff_limits(game)
    added = 0
    for _ in range(count):
        if len(game.signup_user_ids) >= eff_max:
            break
        if count_ai_signup(game) >= config.ww_ai_max:
            break
        if add_ai_signup(game) is None:
            break
        added += 1
    if added == 0:
        await add_ai_cmd.finish("添加失败：房间已满或 AI 人数已达上限")
    logger.info(
        f"狼人杀群 {int(event.group_id)} {user_id} 添加 {added} 名 AI"
        f"（{len(game.signup_user_ids)}/{eff_max}）"
    )
    await add_ai_cmd.finish(
        f"已添加 {added} 名玩家！当前 {len(game.signup_user_ids)}"
        f"/{eff_max} 人"
        f"（至少 {eff_min} 人开局）"
    )


remove_ai_cmd = on_command(
    "移除AI",
    aliases={"减AI"},
    priority=5,
    block=True,
)


@remove_ai_cmd.handle()
async def handle_remove_ai(
    event: GroupMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """报名阶段移除已添加的 AI 玩家（房主/群管/超管）。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.SIGNUP:
        await remove_ai_cmd.finish("本群当前没有报名中的狼人杀对局")
    user_id = int(event.get_user_id())
    if not (user_id == game.host_user_id or is_group_admin(event) or _is_su(user_id)):
        await remove_ai_cmd.finish("只有房主、群管理员或超管可以移除玩家~")
    text = str(arg).strip()
    count = 1
    if text:
        if not text.isdigit() or int(text) < 1:
            await remove_ai_cmd.finish("格式：/移除AI N（N 为人数）")
        count = int(text)
    removed = 0
    for _ in range(count):
        if not remove_ai_signup(game):
            break
        removed += 1
    if removed == 0:
        await remove_ai_cmd.finish("当前没有可移除的玩家~")
    _, eff_max = _eff_limits(game)
    logger.info(
        f"狼人杀群 {int(event.group_id)} {user_id} 移除 {removed} 名 AI"
        f"（{len(game.signup_user_ids)}/{eff_max}）"
    )
    await remove_ai_cmd.finish(
        f"已移除 {removed} 名玩家，当前 {len(game.signup_user_ids)}/{eff_max} 人"
    )


start_cmd = on_command(
    "开始游戏",
    aliases={"发车"},
    priority=5,
    block=True,
)


@start_cmd.handle()
async def handle_start(
    event: GroupMessageEvent,
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """手动开局：房主/群管/超管可发起，人数由引擎校验。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.SIGNUP:
        await start_cmd.finish("本群当前没有报名中的狼人杀对局")
    user_id = int(event.get_user_id())
    if not (user_id == game.host_user_id or is_group_admin(event) or _is_su(user_id)):
        await start_cmd.finish("只有房主、群管理员或超管可以开始游戏~")
    # 人数不足时用 AI 自动补位：目标取板子支持人数中
    # 不低于「配置最低人数与当前人数之较大者」的最小值
    board = BOARDS[game.board]
    eff_min, _ = _eff_limits(game)
    current = len(game.signup_user_ids)
    targets = [n for n in board.counts if n >= max(eff_min, current)]
    target = min(targets) if targets else current
    filled = 0
    if config.ww_ai_enabled and config.ww_ai_autofill:
        while (
            len(game.signup_user_ids) < target
            and count_ai_signup(game) < config.ww_ai_max
        ):
            if add_ai_signup(game) is None:
                break
            filled += 1
    game.action_queue.put_nowait(Action(ActionKind.START_GAME, user_id))
    logger.info(
        f"狼人杀群 {int(event.group_id)} {user_id} 请求开始游戏"
        f"（{len(game.signup_user_ids)} 人，AI 补位 {filled}）"
    )
    if filled:
        await start_cmd.finish(
            f"已自动补足 {filled} 人，"
            f"当前 {len(game.signup_user_ids)} 人，游戏即将开始~"
        )
    await start_cmd.finish("已请求开始游戏~")


end_cmd = on_command(
    "结束游戏",
    aliases={"解散狼局"},
    priority=5,
    block=True,
)


@end_cmd.handle()
async def handle_end(
    bot: Bot,
    event: GroupMessageEvent,
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """强制结束对局；无对局时为群管/超管的禁言恢复命令。"""
    group_id = int(event.group_id)
    user_id = int(event.get_user_id())
    game = get_game(group_id)
    if game is None:
        if not (is_group_admin(event) or _is_su(user_id)):
            await end_cmd.finish("本群当前没有狼人杀对局")
        await api.unban_all_members(bot, group_id)
        await end_cmd.finish("已执行恢复操作：关闭全员禁言并解禁全体群成员")
    if not (user_id == game.host_user_id or is_group_admin(event) or _is_su(user_id)):
        await end_cmd.finish("只有房主、群管理员或超管可以结束游戏~")
    logger.info(
        f"狼人杀群 {group_id} {user_id} 强制结束对局"
        f"（阶段 {game.phase.value}，第 {game.round_no} 回合）"
    )
    await stop_game(game)
    await end_cmd.finish("对局已结束")


# ── 夜间私聊命令 ──────────────────────────────────────────

kill_cmd = on_command("刀", aliases={"狼刀"}, priority=5, block=True)


@kill_cmd.handle()
async def handle_kill(
    event: PrivateMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """狼人刀人。"""
    game = game_of_user(int(event.get_user_id()))
    if game is None or game.phase is not Phase.NIGHT_WOLVES:
        await kill_cmd.finish(
            _not_now(game, "刀 是狼人的夜间行动，请按私聊提示在夜里回复 刀N~")
        )
    seat = _parse_seat(arg)
    if seat is None:
        await kill_cmd.finish("格式：/刀 N（N 为目标座位号）")
    game.action_queue.put_nowait(
        Action(ActionKind.KILL, int(event.get_user_id()), seat)
    )
    await kill_cmd.finish()


check_cmd = on_command("查验", aliases={"验"}, priority=5, block=True)


@check_cmd.handle()
async def handle_check(
    event: PrivateMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """预言家查验。"""
    game = game_of_user(int(event.get_user_id()))
    if game is None or game.phase is not Phase.NIGHT_SEER:
        await check_cmd.finish(
            _not_now(game, "查验 是预言家的夜间行动，请按私聊提示在夜里回复 查验N~")
        )
    seat = _parse_seat(arg)
    if seat is None:
        await check_cmd.finish("格式：/查验 N（N 为目标座位号）")
    game.action_queue.put_nowait(
        Action(ActionKind.CHECK, int(event.get_user_id()), seat)
    )
    await check_cmd.finish()


save_cmd = on_command("救", priority=5, block=True)


@save_cmd.handle()
async def handle_save(
    event: PrivateMessageEvent,
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """女巫救人。"""
    game = game_of_user(int(event.get_user_id()))
    if game is None or game.phase is not Phase.NIGHT_WITCH:
        await save_cmd.finish(
            _not_now(game, "救 是女巫的夜间行动，请按私聊提示在夜里回复 救~")
        )
    game.action_queue.put_nowait(Action(ActionKind.SAVE, int(event.get_user_id())))
    await save_cmd.finish()


poison_cmd = on_command("毒", priority=5, block=True)


@poison_cmd.handle()
async def handle_poison(
    event: PrivateMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """女巫毒人。"""
    game = game_of_user(int(event.get_user_id()))
    if game is None or game.phase is not Phase.NIGHT_WITCH:
        await poison_cmd.finish(
            _not_now(game, "毒 是女巫的夜间行动，请按私聊提示在夜里回复 毒N~")
        )
    seat = _parse_seat(arg)
    if seat is None:
        await poison_cmd.finish("格式：/毒 N（N 为目标座位号）")
    game.action_queue.put_nowait(
        Action(ActionKind.POISON, int(event.get_user_id()), seat)
    )
    await poison_cmd.finish()


shoot_cmd = on_command("开枪", aliases={"带"}, priority=5, block=True)


@shoot_cmd.handle()
async def handle_shoot(
    event: PrivateMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """猎人开枪。"""
    game = game_of_user(int(event.get_user_id()))
    if game is None or game.phase is not Phase.HUNTER_SHOT:
        await shoot_cmd.finish(
            _not_now(game, "开枪 是猎人死亡后的决策，请按私聊提示回复 开枪N~")
        )
    seat = _parse_seat(arg)
    if seat is None:
        await shoot_cmd.finish("格式：/开枪 N（N 为目标座位号）")
    game.action_queue.put_nowait(
        Action(ActionKind.SHOOT, int(event.get_user_id()), seat)
    )
    await shoot_cmd.finish()


no_shoot_cmd = on_command(
    "不开枪",
    aliases={"压枪"},
    priority=5,
    block=True,
)


@no_shoot_cmd.handle()
async def handle_no_shoot(
    event: PrivateMessageEvent,
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """猎人放弃开枪。"""
    game = game_of_user(int(event.get_user_id()))
    if game is None or game.phase is not Phase.HUNTER_SHOT:
        await no_shoot_cmd.finish(
            _not_now(game, "不开枪 是猎人死亡后的决策，请按私聊提示回复 不开枪~")
        )
    game.action_queue.put_nowait(Action(ActionKind.NO_SHOOT, int(event.get_user_id())))
    await no_shoot_cmd.finish()


# ── 白天群命令 ────────────────────────────────────────────

run_cmd = on_command("上警", aliases={"竞选"}, priority=5, block=True)


@run_cmd.handle()
async def handle_run(
    event: GroupMessageEvent,
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """竞选警长。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.SHERIFF_REGISTER:
        await run_cmd.finish(
            _not_now(game, "上警 仅在警长竞选报名阶段可用，请留意群内竞选播报~")
        )
    game.action_queue.put_nowait(Action(ActionKind.RUN, int(event.get_user_id())))
    await run_cmd.finish()


withdraw_cmd = on_command("退水", priority=5, block=True)


@withdraw_cmd.handle()
async def handle_withdraw(
    event: GroupMessageEvent,
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """退出警长竞选。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase not in (
        Phase.SHERIFF_REGISTER,
        Phase.SHERIFF_SPEECH,
    ):
        await withdraw_cmd.finish(_not_now(game, "退水 仅在警长竞选阶段可用~"))
    game.action_queue.put_nowait(Action(ActionKind.WITHDRAW, int(event.get_user_id())))
    await withdraw_cmd.finish()


order_cmd = on_command("排序", priority=5, block=True)


@order_cmd.handle()
async def handle_order(
    event: GroupMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """警长决定发言顺序：/排序 N 顺|逆。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.DAY_SPEECH:
        await order_cmd.finish(
            _not_now(game, "排序 由警长在白天发言前决定，格式 /排序 N 顺|逆~")
        )
    text = str(arg).strip()
    match = re.fullmatch(r"(\d+)\s*号?\s*(顺|逆)?", text)
    if match is None:
        await order_cmd.finish("格式：/排序 N 顺 或 /排序 N 逆")
    seat = int(match.group(1))
    aux = "ccw" if match.group(2) == "逆" else "cw"
    game.action_queue.put_nowait(
        Action(
            ActionKind.ORDER,
            int(event.get_user_id()),
            seat,
            aux,
        )
    )
    await order_cmd.finish()


pass_badge_cmd = on_command("移交警徽", priority=5, block=True)


@pass_badge_cmd.handle()
async def handle_pass_badge(
    event: GroupMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """死亡警长移交警徽。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.BADGE_TRANSFER:
        await pass_badge_cmd.finish(
            _not_now(game, "移交警徽 仅在死亡警长的移交阶段可用~")
        )
    seat = _parse_seat(arg)
    if seat is None:
        await pass_badge_cmd.finish("格式：/移交警徽 N（N 为存活玩家座位）")
    game.action_queue.put_nowait(
        Action(ActionKind.PASS_BADGE, int(event.get_user_id()), seat)
    )
    await pass_badge_cmd.finish()


tear_badge_cmd = on_command("撕警徽", priority=5, block=True)


@tear_badge_cmd.handle()
async def handle_tear_badge(
    event: GroupMessageEvent,
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """死亡警长撕掉警徽。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.BADGE_TRANSFER:
        await tear_badge_cmd.finish(
            _not_now(game, "撕警徽 仅在死亡警长的移交阶段可用~")
        )
    game.action_queue.put_nowait(
        Action(ActionKind.TEAR_BADGE, int(event.get_user_id()))
    )
    await tear_badge_cmd.finish()


detonate_cmd = on_command("自爆", priority=5, block=True)


@detonate_cmd.handle()
async def handle_detonate(
    event: GroupMessageEvent,
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """狼人白天自爆。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase not in SELF_DETONATE_PHASES:
        await detonate_cmd.finish(
            _not_now(game, "自爆 是狼人的白天行动，可在发言/投票阶段发动~")
        )
    game.action_queue.put_nowait(
        Action(ActionKind.SELF_DETONATE, int(event.get_user_id()))
    )
    await detonate_cmd.finish()


duel_cmd = on_command("决斗", priority=5, block=True)


@duel_cmd.handle()
async def handle_duel(
    event: GroupMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """骑士白天翻牌决斗（发言阶段）。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase not in DUEL_PHASES:
        await duel_cmd.finish(
            _not_now(game, "决斗 是骑士的白天行动，可在发言阶段发动，格式 /决斗 N~")
        )
    seat = _parse_seat(arg)
    if seat is None:
        await duel_cmd.finish("格式：/决斗 N（N 为目标座位号）")
    game.action_queue.put_nowait(
        Action(ActionKind.DUEL, int(event.get_user_id()), seat)
    )
    await duel_cmd.finish()


vote_cmd = on_command("投票", aliases={"票"}, priority=5, block=True)


@vote_cmd.handle()
async def handle_vote(
    event: GroupMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """投票（警长/放逐/PK 按当前阶段解释）。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase not in _VOTE_PHASES:
        await vote_cmd.finish(_not_now(game, "投票 仅在投票阶段可用，格式 /投票 N~"))
    seat = _parse_seat(arg)
    if seat is None:
        await vote_cmd.finish("格式：/投票 N（N 为目标座位号）")
    game.action_queue.put_nowait(
        Action(ActionKind.VOTE, int(event.get_user_id()), seat)
    )
    await vote_cmd.finish()


abstain_cmd = on_command("弃票", priority=5, block=True)


@abstain_cmd.handle()
async def handle_abstain(
    event: GroupMessageEvent,
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """弃票。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase not in _VOTE_PHASES:
        await abstain_cmd.finish(_not_now(game, "弃票 仅在投票阶段可用~"))
    game.action_queue.put_nowait(Action(ActionKind.ABSTAIN, int(event.get_user_id())))
    await abstain_cmd.finish()


skip_cmd = on_command("过", aliases={"跳过"}, priority=5, block=True)


@skip_cmd.handle()
async def handle_skip(
    event: GroupMessageEvent,
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """结束自己的发言/遗言。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase not in (
        Phase.SHERIFF_SPEECH,
        Phase.SHERIFF_REVOTE,
        Phase.DAY_SPEECH,
        Phase.PK_SPEECH,
        Phase.LAST_WORDS,
    ):
        await skip_cmd.finish(
            _not_now(game, "过 用于提前结束自己的发言/遗言，现在没有你的发言窗口~")
        )
    game.action_queue.put_nowait(Action(ActionKind.SKIP, int(event.get_user_id())))
    await skip_cmd.finish()


# ── 私聊自由文本监听 ──────────────────────────────────────


async def _is_in_game_dm(event: MessageEvent) -> bool:
    """规则：私聊 ∧ 用户在行动阶段的对局中 ∧ 未被禁用狼人杀功能。"""
    if isinstance(event, GroupMessageEvent):
        return False
    user_id = int(event.get_user_id())
    game = game_of_user(user_id)
    if game is None or game.phase in (Phase.SIGNUP, Phase.ENDED):
        return False
    # 与命令处理器的 require_feature 一致：按私聊解析链查全局用户开关
    async with get_session() as session:
        return await check_feature_permission(
            user_id,
            None,
            "werewolf",
            session,  # pyright: ignore[reportArgumentType]
        )


# 私聊行动 DSL（模式表与解析函数）见 dsl.py，与 ai_player 共用


private_listener = on_message(
    rule=Rule(_is_in_game_dm),
    priority=0,
    block=True,
)


@private_listener.handle()
async def handle_in_game_dm(event: PrivateMessageEvent) -> None:
    """在局玩家的私聊行动解析入口（优先于 ai_chat）。"""
    user_id = int(event.get_user_id())
    game = game_of_user(user_id)
    if game is None:
        return
    text = event.get_plaintext()
    action = parse_dm_action(text, user_id)
    if action is None:
        logger.info(f"狼人杀群 {game.group_id} {user_id} 私聊无法解析：{text!r}")
        await private_listener.finish(
            _dm_hint_for(game, game.player_by_user(user_id), text)
        )
    actor = game.player_by_user(user_id)
    seat_desc = f"{actor.seat}号" if actor is not None else str(user_id)
    target_desc = f"→{action.value}号" if action.value else ""
    logger.info(
        f"狼人杀群 {game.group_id} {seat_desc} 私聊行动："
        f"{action.kind.value}{target_desc}（原文 {text!r}）"
    )
    game.action_queue.put_nowait(action)
    await private_listener.finish()


# ── 群发言捕获（供 AI 驱动听取发言）──────────────────────

_SPEECH_CAPTURE_PHASES: frozenset[Phase] = frozenset(
    {
        Phase.LAST_WORDS,
        Phase.SHERIFF_SPEECH,
        Phase.SHERIFF_REVOTE,
        Phase.DAY_SPEECH,
        Phase.PK_SPEECH,
    }
)


async def _is_game_speech(event: MessageEvent) -> bool:
    """规则：群消息 ∧ 发言阶段 ∧ 发送者是当前发言者。"""
    if not isinstance(event, GroupMessageEvent):
        return False
    game = get_game(int(event.group_id))
    if game is None or game.phase not in _SPEECH_CAPTURE_PHASES:
        return False
    if game.current_speaker is None:
        return False
    player = game.player_by_user(int(event.get_user_id()))
    return player is not None and player.seat == game.current_speaker


speech_listener = on_message(
    rule=Rule(_is_game_speech),
    priority=0,
    block=False,  # 不阻断命令匹配；仅旁路记录发言
)


@speech_listener.handle()
async def handle_game_speech(event: GroupMessageEvent) -> None:
    """记录当前发言者的群发言到 AI 公共记录。"""
    game = get_game(int(event.group_id))
    if game is None or game.current_speaker is None:
        return
    text = event.get_plaintext().strip()
    # 空消息与命令（/过 等）不算发言
    if not text or text.startswith("/"):
        return
    ai_player.record_speech(game, game.current_speaker, text)


# ── 战绩 ──────────────────────────────────────────────────

record_cmd = on_command(
    "战绩",
    aliases={"狼人战绩"},
    priority=5,
    block=True,
)


@record_cmd.handle()
async def handle_record(
    event: MessageEvent,
    session: async_scoped_session,
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """查询战绩：默认自己；群管/超管可 @ 他人查询。"""
    user_id = int(event.get_user_id())
    target = user_id
    if isinstance(event, GroupMessageEvent):
        ats = [
            seg.data["qq"]
            for seg in event.message
            if seg.type == "at" and seg.data.get("qq")
        ]
        if ats:
            if not (is_group_admin(event) or _is_su(user_id)):
                await record_cmd.finish("只有群管理员或超管可以查询他人战绩~")
            if not ats[0].isdigit():
                await record_cmd.finish("无法查询全体成员的战绩~")
            target = int(ats[0])
    stmt = select(WerewolfPlayer).where(
        WerewolfPlayer.user_id == target,
        WerewolfPlayer.is_ai == False,  # noqa: E712  # AI 战绩不计入真人
    )
    rows = (await session.execute(stmt)).scalars().all()
    finished = [r for r in rows if r.is_winner is not None]
    if not finished:
        await record_cmd.finish("还没有已结束的狼人杀对局记录~")
    total = len(finished)
    wins = sum(1 for r in finished if r.is_winner)
    sheriff_times = sum(1 for r in finished if r.is_sheriff)
    role_stats: dict[str, list[int]] = {}
    for r in finished:
        entry = role_stats.setdefault(r.role, [0, 0])
        entry[0] += 1
        if r.is_winner:
            entry[1] += 1
    lines = [
        "═══ 狼人战绩 ═══",
        f"总场次：{total}，胜场：{wins}，胜率：{wins / total:.1%}",
        f"曾任警长：{sheriff_times} 次",
        "─── 各角色 ───",
    ]
    for role in Role:
        if role.value in role_stats:
            played, won = role_stats[role.value]
            lines.append(f"{role.value}：{played} 场 {won} 胜")
    lines.append("──────────────")
    await record_cmd.finish("\n".join(lines))
