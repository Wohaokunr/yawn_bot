"""狼人杀命令入口：群命令、私聊命令与私聊自由文本监听。

处理器只做轻量校验（在局状态、阶段），把行动投入
game.action_queue 交给引擎裁决；私聊自由文本监听器在
行动阶段拦截在局玩家的私聊（其余私聊放行给 ai_chat）。
"""

import asyncio
import re
from collections.abc import Coroutine
from typing import Any, Optional

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
from .roles import BOARDS, Role, build_role_card, parse_role
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
    submit_action,
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


def _submit(
    game: Game,
    action: Action,
    *,
    allow_nonmember: bool = False,
) -> str | None:
    """提交行动并返回用户可见的背压提示。"""
    if submit_action(
        game,
        action,
        user_pending_max=config.ww_user_pending_max,
        allow_nonmember=allow_nonmember,
    ):
        return None
    return "当前行动较多或重复，请稍后再试~"


async def _finish_action(matcher: Any, game: Game, action: Action) -> None:
    """提交行动并安全结束命令，避免把空字符串交给 NoneBot。"""
    error = _submit(game, action)
    if error:
        await matcher.finish(error)
    await matcher.finish()


def _is_su(user_id: int) -> bool:
    """是否为超级用户。"""
    return str(user_id) in get_driver().config.superusers


# 发后即忘的后台任务登记：无引用的任务可能在完成前被事件循环
# 的弱引用回收，登记持有强引用直至完成
_bg_tasks: set[asyncio.Task[None]] = set()


def _spawn_background(coro: Coroutine[Any, Any, None]) -> None:
    """后台运行清理类协程，完成后自动出登记。"""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def _eff_limits(game: Game) -> tuple[int, int]:
    """报名阶段的有效人数区间（配置项与板子支持人数的交集）。"""
    board = BOARDS[game.board]
    return (
        max(config.ww_min_players, min(board.counts)),
        min(config.ww_max_players, max(board.counts)),
    )


_NIGHT_PHASES: frozenset[Phase] = frozenset(
    {
        Phase.NIGHT_HALFBLOOD,
        Phase.NIGHT_WOLVES,
        Phase.NIGHT_WITCH,
        Phase.NIGHT_SEER,
        Phase.NIGHT_ELDER,
    }
)


def _seat_list_text(seats: list[int]) -> str:
    """把座位号渲染成公开播报用的短列表。"""
    return "、".join(f"{seat}号" for seat in seats) if seats else "无"


def format_game_status(game: Game) -> str:
    """生成不泄露身份和夜间行动细节的公开进度。"""
    if game.phase is Phase.SIGNUP:
        eff_min, eff_max = _eff_limits(game)
        lines = [
            "═══ 狼人杀 · 当前进度 ═══",
            "阶段：报名中",
            f"板子：{game.board}",
            "报名名单：",
        ]
        if game.signup_user_ids:
            lines.extend(
                f"{index}. {display_name_of(game, user_id)}"
                for index, user_id in enumerate(game.signup_user_ids, start=1)
            )
        else:
            lines.append("暂无")
        lines.extend(
            (
                "──────────────",
                f"当前 {len(game.signup_user_ids)}/{eff_max} 人，至少 {eff_min} 人开局",
                "房主可发送 /开始游戏；发送 /板子 查看或切换板子",
            )
        )
        return "\n".join(lines)

    phase_name = _PHASE_CN.get(game.phase, game.phase.value)
    if game.phase in _NIGHT_PHASES:
        round_text = f"第 {game.round_no} 夜"
    elif game.phase is Phase.DEALING:
        round_text = "开局准备中"
    else:
        round_text = f"第 {game.round_no} 天"
    alive = [player.seat for player in game.players if player.alive]
    dead = [player.seat for player in game.players if not player.alive]
    sheriff = game.sheriff()
    sheriff_text = "无"
    if sheriff is not None:
        sheriff_text = f"{sheriff.seat}号"
        if not sheriff.alive:
            sheriff_text += "（已死亡，警徽处理中）"
    lines = [
        "═══ 狼人杀 · 当前进度 ═══",
        f"板子：{game.board}",
        f"回合：{round_text}",
        f"阶段：{phase_name}",
        f"存活座位：{_seat_list_text(alive)}",
        f"倒牌座位：{_seat_list_text(dead)}",
        f"警长：{sheriff_text}",
    ]
    if game.phase in _VOTE_PHASES and game.vote_targets:
        lines.append(f"当前可投：{_seat_list_text(game.vote_targets)}")
    elif game.current_speaker is not None:
        lines.append(f"当前发言：{game.current_speaker}号")
    if game.phase in _NIGHT_PHASES:
        lines.append("夜间行动不会在群内显示，请按私聊提示操作。")
    return "\n".join(lines)


def _parse_seat(text: object) -> Optional[int]:
    """从参数文本解析座位号（支持 "3" 与 "3号"）。"""
    match = re.fullmatch(r"(\d+)\s*号?", str(text).strip())
    return int(match.group(1)) if match else None


# 阶段中文名（报错/提示用）。夜间子阶段一律折叠为"夜晚"：
# 报错与提示不得暴露当前轮到哪个角色行动
_PHASE_CN: dict[Phase, str] = {
    Phase.SIGNUP: "报名中",
    Phase.DEALING: "发牌中",
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
    Phase.SHERIFF_FINAL_SPEECH: "警长平票终辩",
    Phase.SHERIFF_REVOTE: "警长平票重投",
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
    Role.WEREWOLF: "刀N（击杀）/ 过（空刀）/ 说XXX（与狼队友讨论）",
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


# 私聊自由文本行动的阶段闸门：逐阶段镜像引擎循环实际消费的行动
# 类型（engine.py 各阶段对异类行动静默 continue）。解析成功但当前
# 阶段不消费的行动在此被拒并给出阶段闸门报错，否则玩家会白等一
# 整个窗口而不知指令已被丢弃。夜间子阶段经 _PHASE_CN 折叠为"夜晚"，
# 报错文案不暴露当前轮到哪个角色
_DM_ALLOWED: dict[Phase, frozenset[ActionKind]] = {
    Phase.NIGHT_HALFBLOOD: frozenset({ActionKind.CHOOSE_OWNER}),
    Phase.NIGHT_WOLVES: frozenset(
        {ActionKind.KILL, ActionKind.SKIP, ActionKind.SAY}
    ),
    Phase.NIGHT_WITCH: frozenset({ActionKind.SAVE, ActionKind.POISON, ActionKind.SKIP}),
    Phase.NIGHT_SEER: frozenset({ActionKind.CHECK}),
    Phase.NIGHT_ELDER: frozenset({ActionKind.SILENCE, ActionKind.SKIP}),
    Phase.LAST_WORDS: frozenset({ActionKind.SKIP, ActionKind.SELF_DETONATE}),
    Phase.HUNTER_SHOT: frozenset(
        {ActionKind.SHOOT, ActionKind.NO_SHOOT, ActionKind.SKIP}
    ),
    Phase.BADGE_TRANSFER: frozenset({ActionKind.PASS_BADGE, ActionKind.TEAR_BADGE}),
    Phase.SHERIFF_REGISTER: frozenset(
        {ActionKind.RUN, ActionKind.WITHDRAW, ActionKind.SELF_DETONATE}
    ),
    Phase.SHERIFF_SPEECH: frozenset(
        {ActionKind.SKIP, ActionKind.WITHDRAW, ActionKind.SELF_DETONATE}
    ),
    Phase.SHERIFF_VOTE: frozenset({ActionKind.SELF_DETONATE}),
    Phase.SHERIFF_FINAL_SPEECH: frozenset(
        {ActionKind.SKIP, ActionKind.SELF_DETONATE}
    ),
    Phase.SHERIFF_REVOTE: frozenset({ActionKind.SELF_DETONATE}),
    Phase.DAY_SPEECH: frozenset(
        {ActionKind.SKIP, ActionKind.DUEL, ActionKind.ORDER, ActionKind.SELF_DETONATE}
    ),
    Phase.DAY_VOTE: frozenset({ActionKind.SELF_DETONATE}),
    Phase.PK_SPEECH: frozenset(
        {ActionKind.SKIP, ActionKind.DUEL, ActionKind.SELF_DETONATE}
    ),
    Phase.PK_VOTE: frozenset({ActionKind.SELF_DETONATE}),
}


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
    bot: Bot,
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
    game = create_game(
        group_id,
        user_id,
        queue_max=config.ww_action_queue_max,
    )
    if game is None:
        await wolf_open.finish("开房失败，请稍后重试")
    # 注入收到本事件的 Bot：多机器人在线时 nonebot.get_bot() 会抛
    # ValueError，引擎不能依赖它选连接（见 engine.run_game）
    game.bot = bot
    note_signup_name(game, user_id, _sender_display(event))
    game.worker = asyncio.create_task(engine.run_game(game))
    logger.info(f"狼人杀群 {group_id} 由 {user_id} 开房")
    hint = "可私聊 /选身份 身份名请求期望角色~" if config.ww_role_request else ""
    await wolf_open.finish(f"狼人杀房间已创建，房主已自动报名~\n{hint}".rstrip())


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
    response = (
        MessageSegment.at(event.user_id)
        + f"报名成功！当前 {len(game.signup_user_ids)}"
        + f"/{eff_max} 人"
        + f"（至少 {eff_min} 人开局）"
    )
    if config.ww_role_request:
        response += "\n可私聊 /选身份 身份名请求期望角色~"
    await signup_cmd.finish(response)


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
        _spawn_background(stop_game(game))
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
    if config.ww_role_request:
        lines.append("报名后可私聊 /选身份 身份名，未请求则随机发牌")
    await view_cmd.finish("\n".join(lines))


status_cmd = on_command(
    "狼人状态",
    aliases={"狼局状态", "狼人进度"},
    priority=5,
    block=True,
)


@status_cmd.handle()
async def handle_status(
    event: GroupMessageEvent,
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """查看当前公开进度，不显示身份或夜间行动细节。"""
    game = get_game(int(event.group_id))
    if game is None:
        await status_cmd.finish("本群当前没有狼人杀对局（发送 /狼人杀 开房）")
    await status_cmd.finish(format_game_status(game))


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
    error = _submit(
        game,
        Action(ActionKind.START_GAME, user_id),
        allow_nonmember=user_id not in game.signup_user_ids,
    )
    if error:
        await start_cmd.finish(error)
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
    # stop_game 会等引擎 finally 收尾（逐人解禁等串行 API），大群迟缓：
    # 先回执，后台等待清理完成
    _spawn_background(stop_game(game))
    await end_cmd.finish("对局已结束")


# ── 报名阶段私聊命令（选身份）────────────────────────────

wish_cmd = on_command("选身份", aliases={"想要"}, priority=5, block=True)


@wish_cmd.handle()
async def handle_wish(
    event: PrivateMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """报名阶段私聊请求期望身份（多人同选按份数随机分配）。"""
    user_id = int(event.get_user_id())
    game = game_of_user(user_id)
    if game is None or game.phase is not Phase.SIGNUP:
        await wish_cmd.finish(_not_now(game, "选身份仅在报名阶段可用~"))
    if not config.ww_role_request:
        await wish_cmd.finish("选身份功能未开启~")
    if user_id not in game.signup_user_ids:
        await wish_cmd.finish("你还没报名这局狼人杀，先在群里发 /报名~")
    board = BOARDS[game.board]
    role = parse_role(str(arg))
    if role is None:
        current = game.role_requests.get(user_id)
        current_note = (
            f"\n当前请求：{current.value}（发送 /取消选身份 可清除）"
            if current is not None
            else "\n当前请求：无（未请求则随机发牌）"
        )
        await wish_cmd.finish(
            f"格式：/选身份 身份名。可选身份：{board.roles_summary()}"
            f"{current_note}"
        )
    if role not in board.all_roles():
        await wish_cmd.finish(
            f"板子「{board.key}」没有 {role.value}（可选：{board.roles_summary()}）"
        )
    prev = game.role_requests.get(user_id)
    game.role_requests[user_id] = role
    replaced = f"（原请求【{prev.value}】已替换）" if prev is not None else ""
    logger.info(
        f"狼人杀群 {game.group_id} {user_id} 选身份请求：{role.value}{replaced}"
    )
    await wish_cmd.finish(
        f"已登记：你想要【{role.value}】{replaced}。\n"
        "多人请求同一身份时，发牌将在请求者中按份数随机分配~"
    )


unwish_cmd = on_command("取消选身份", aliases={"不选了"}, priority=5, block=True)


@unwish_cmd.handle()
async def handle_unwish(
    event: PrivateMessageEvent,
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """取消报名阶段的身份请求。"""
    user_id = int(event.get_user_id())
    game = game_of_user(user_id)
    if game is None or game.phase is not Phase.SIGNUP:
        await unwish_cmd.finish(_not_now(game, "取消选身份仅在报名阶段可用~"))
    prev = game.role_requests.pop(user_id, None)
    if prev is None:
        await unwish_cmd.finish("你当前没有选身份请求~")
    logger.info(f"狼人杀群 {game.group_id} {user_id} 取消选身份请求：{prev.value}")
    await unwish_cmd.finish(f"已取消【{prev.value}】的请求，发牌将按随机分配。")


# ── 身份卡补发（仅私聊）───────────────────────────────────

identity_cmd = on_command(
    "身份",
    aliases={"重发身份卡"},
    priority=5,
    block=True,
)


@identity_cmd.handle()
async def handle_identity(
    event: PrivateMessageEvent,
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """私聊重发自己的身份卡，解决首次私聊投递失败或消息遗失。"""
    user_id = int(event.get_user_id())
    game = game_of_user(user_id)
    if game is None:
        await identity_cmd.finish("你当前不在狼人杀对局中~")
    if game.phase in (Phase.SIGNUP, Phase.ENDED) or not game.players:
        await identity_cmd.finish("身份卡将在发牌后提供，请等待游戏开始~")
    player = game.player_by_user(user_id)
    if player is None or player.is_ai:
        await identity_cmd.finish("当前无法为你补发身份卡~")
    board = BOARDS[game.board]
    roster = [(item.seat, display_name_of(game, item.user_id)) for item in game.players]
    delivered = await engine._dm(
        game,
        player,
        build_role_card(
            player.seat,
            player.role,
            len(game.players),
            silence_mode=board.silence_mode,
            roster=roster,
        ),
    )
    if delivered:
        await identity_cmd.finish("身份卡已私聊重发，请查收~")
    await identity_cmd.finish("身份卡发送失败，请先加机器人为好友后再发送 /身份~")


# ── 仅私聊的特殊夜间行动命令 ──────────────────────────────

choose_owner_cmd = on_command(
    "认主",
    aliases={"选主"},
    priority=5,
    block=True,
)


@choose_owner_cmd.handle()
async def handle_choose_owner(
    event: PrivateMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """混血儿首夜选择主人。"""
    user_id = int(event.get_user_id())
    game = game_of_user(user_id)
    if game is None or game.phase is not Phase.NIGHT_HALFBLOOD:
        await choose_owner_cmd.finish(
            _not_now(game, "认主 是混血儿首夜行动，请私聊发送 /认主 N~")
        )
    player = game.player_by_user(user_id)
    if player is None or player.role is not Role.HALFBLOOD:
        await choose_owner_cmd.finish("当前只有混血儿可以认主~")
    seat = _parse_seat(arg)
    if seat is None:
        await choose_owner_cmd.finish("格式：/认主 N（N 为主人座位号）")
    await _finish_action(
        choose_owner_cmd,
        game,
        Action(ActionKind.CHOOSE_OWNER, user_id, seat),
    )


silence_cmd = on_command(
    "禁言",
    aliases={"禁票"},
    priority=5,
    block=True,
)


@silence_cmd.handle()
async def handle_silence(
    event: PrivateMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """禁言/禁票长老夜间选择目标。"""
    user_id = int(event.get_user_id())
    game = game_of_user(user_id)
    if game is None or game.phase is not Phase.NIGHT_ELDER:
        await silence_cmd.finish(
            _not_now(game, "禁言（禁票板为禁票）是长老夜间行动，请私聊发送 /禁言 N~")
        )
    player = game.player_by_user(user_id)
    if player is None or player.role is not Role.SILENT_ELDER:
        await silence_cmd.finish("当前只有禁言长老可以使用这条指令~")
    seat = _parse_seat(arg)
    if seat is None:
        await silence_cmd.finish("格式：/禁言 N（禁票板也可用 /禁票 N）")
    await _finish_action(
        silence_cmd,
        game,
        Action(ActionKind.SILENCE, user_id, seat),
    )


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
    await _finish_action(
        kill_cmd,
        game,
        Action(ActionKind.KILL, int(event.get_user_id()), seat),
    )


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
    await _finish_action(
        check_cmd,
        game,
        Action(ActionKind.CHECK, int(event.get_user_id()), seat),
    )


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
    await _finish_action(
        save_cmd,
        game,
        Action(ActionKind.SAVE, int(event.get_user_id())),
    )


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
    await _finish_action(
        poison_cmd,
        game,
        Action(ActionKind.POISON, int(event.get_user_id()), seat),
    )


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
    await _finish_action(
        shoot_cmd,
        game,
        Action(ActionKind.SHOOT, int(event.get_user_id()), seat),
    )


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
    await _finish_action(
        no_shoot_cmd,
        game,
        Action(ActionKind.NO_SHOOT, int(event.get_user_id())),
    )


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
    await _finish_action(
        run_cmd,
        game,
        Action(ActionKind.RUN, int(event.get_user_id())),
    )


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
    await _finish_action(
        withdraw_cmd,
        game,
        Action(ActionKind.WITHDRAW, int(event.get_user_id())),
    )


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
    await _finish_action(
        order_cmd,
        game,
        Action(
            ActionKind.ORDER,
            int(event.get_user_id()),
            seat,
            aux,
        ),
    )


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
    await _finish_action(
        pass_badge_cmd,
        game,
        Action(ActionKind.PASS_BADGE, int(event.get_user_id()), seat),
    )


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
    await _finish_action(
        tear_badge_cmd,
        game,
        Action(ActionKind.TEAR_BADGE, int(event.get_user_id())),
    )


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
    await _finish_action(
        detonate_cmd,
        game,
        Action(ActionKind.SELF_DETONATE, int(event.get_user_id())),
    )


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
    await _finish_action(
        duel_cmd,
        game,
        Action(ActionKind.DUEL, int(event.get_user_id()), seat),
    )


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
    await _finish_action(
        vote_cmd,
        game,
        Action(ActionKind.VOTE, int(event.get_user_id()), seat),
    )


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
    await _finish_action(
        abstain_cmd,
        game,
        Action(ActionKind.ABSTAIN, int(event.get_user_id())),
    )


skip_cmd = on_command("过", priority=5, block=True)


@skip_cmd.handle()
async def handle_skip(
    event: GroupMessageEvent,
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """结束自己的发言/遗言。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase not in (
        Phase.SHERIFF_SPEECH,
        Phase.SHERIFF_FINAL_SPEECH,
        Phase.DAY_SPEECH,
        Phase.PK_SPEECH,
        Phase.LAST_WORDS,
    ):
        await skip_cmd.finish(
            _not_now(game, "过 用于提前结束自己的发言/遗言，现在没有你的发言窗口~")
        )
    await _finish_action(
        skip_cmd,
        game,
        Action(ActionKind.SKIP, int(event.get_user_id())),
    )


# ── 私聊自由文本监听 ──────────────────────────────────────


async def _is_in_game_dm(event: MessageEvent) -> bool:
    """规则：私聊 ∧ 用户在行动阶段的对局中 ∧ 未被禁用狼人杀功能。"""
    if isinstance(event, GroupMessageEvent):
        return False
    user_id = int(event.get_user_id())
    game = game_of_user(user_id)
    if game is None or game.phase in (Phase.SIGNUP, Phase.ENDED):
        return False
    if event.get_plaintext().strip().startswith("/"):
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
    priority=-1,
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
    if action.kind not in _DM_ALLOWED.get(game.phase, frozenset()):
        logger.info(
            f"狼人杀群 {game.group_id} {user_id} 私聊行动 "
            f"{action.kind.value} 在阶段 {game.phase.value} 不可用，闸门拦截"
        )
        await private_listener.finish(
            _not_now(game, "这条指令在当前阶段不可用，请按私聊提示行动~")
        )
    actor = game.player_by_user(user_id)
    seat_desc = f"{actor.seat}号" if actor is not None else str(user_id)
    target_desc = f"→{action.value}号" if action.value else ""
    logger.info(
        f"狼人杀群 {game.group_id} {seat_desc} 私聊行动："
        f"{action.kind.value}{target_desc}（原文 {text!r}）"
    )
    await _finish_action(private_listener, game, action)


# ── 群发言捕获（供 AI 驱动听取发言）──────────────────────

_SPEECH_CAPTURE_PHASES: frozenset[Phase] = frozenset(
    {
        Phase.LAST_WORDS,
        Phase.SHERIFF_SPEECH,
        Phase.SHERIFF_FINAL_SPEECH,
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
