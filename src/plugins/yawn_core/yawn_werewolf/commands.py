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

from ..command_definition import build_matcher  # noqa: TID252
from ..command_ux import command_failure  # noqa: TID252
from ..permission import (  # noqa: TID252
    check_feature_permission,
    is_group_admin,
    require_feature,
)
from . import ai_player, api, engine, house_rules
from .command_definitions import COMMAND_BY_NAME
from .config import Config
from .dsl import _DM_HINT, parse_dm_action
from .models import WerewolfGame, WerewolfPlayer
from .roles import (
    BOARDS,
    WOLF_ROLES,
    Faction,
    Role,
    build_role_card,
    parse_role,
    resolve_board_key,
    role_guide,
    suggest_boards,
)
from .state import (
    DUEL_PHASES,
    NIGHT_PHASES,
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
    last_review,
    last_roster,
    leave_signup,
    note_signup_name,
    phase_remaining,
    remove_ai_signup,
    room_prefs,
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


_NIGHT_PHASES: frozenset[Phase] = NIGHT_PHASES


def _seat_list_text(seats: list[int]) -> str:
    """把座位号渲染成公开播报用的短列表。"""
    return "、".join(f"{seat}号" for seat in seats) if seats else "无"


def _rules_line(game: Game) -> str:
    """房规一行摘要：预设名或与标准不同的项。"""
    preset = house_rules.preset_name(game.rules)
    diff = house_rules.diff_from_default(game.rules)
    if preset and preset != "标准":
        return f"{preset}（{'；'.join(diff)}）"
    return "；".join(diff) or "标准"


def format_game_status(game: Game) -> str:  # noqa: C901,PLR0912
    """生成不泄露身份和夜间行动细节的公开进度。"""
    if game.phase is Phase.SIGNUP:
        eff_min, eff_max = _eff_limits(game)
        lines = [
            "═══ 狼人杀 · 当前进度 ═══",
            "阶段：报名中",
            f"板子：{game.board}",
            f"房规：{_rules_line(game)}",
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
                "房主可发送 /开始游戏；/板子 切换板子，/房规 调整规则",
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
        f"板子：{game.board}（{len(game.players)} 人）",
        f"回合：{round_text}",
        f"阶段：{phase_name}",
        f"存活（{len(alive)}）：{_seat_list_text(alive)}",
        f"出局（{len(dead)}）：{_seat_list_text(dead)}",
        f"警长：{sheriff_text}",
    ]
    revealed = [p.seat for p in game.players if p.idiot_revealed and p.alive]
    if revealed:
        lines.append(f"已翻牌白痴：{_seat_list_text(revealed)}")
    if game.phase in _VOTE_PHASES and game.vote_targets:
        lines.append(f"当前可投：{_seat_list_text(game.vote_targets)}")
    elif game.current_speaker is not None:
        lines.append(f"当前发言：{game.current_speaker}号")
    remain = phase_remaining(game)
    if remain is not None and game.phase not in _NIGHT_PHASES:
        # 夜间子阶段的剩余时间会暴露当前轮到谁，只在白天显示
        lines.append(f"本阶段剩余：约 {remain} 秒")
    rules_text = _rules_line(game)
    if rules_text != "标准":
        lines.append(f"房规：{rules_text}")
    if game.phase in _NIGHT_PHASES:
        lines.append("夜间行动不会在群内显示，请按私聊提示操作。")
    return "\n".join(lines)


def _seat_from_arg(game: Game, arg: Message) -> Optional[int]:
    """参数解析为座位：支持 "3"、"3号" 与 @某人（按在局玩家反查座位）。"""
    for seg in arg:
        if seg.type == "at" and str(seg.data.get("qq", "")).isdigit():
            player = game.player_by_user(int(seg.data["qq"]))
            return player.seat if player is not None else None
    return _parse_seat(arg.extract_plain_text())


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
    Phase.NIGHT_GUARD: "夜晚",
    Phase.NIGHT_GRAVEKEEPER: "夜晚",
    Phase.NIGHT_WOLVES: "夜晚",
    Phase.NIGHT_WITCH: "夜晚",
    Phase.NIGHT_SEER: "夜晚",
    Phase.NIGHT_ELDER: "夜晚",
    Phase.DAY_ANNOUNCE: "天亮结算",
    Phase.LAST_WORDS: "遗言环节",
    Phase.HUNTER_SHOT: "死亡技能",
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
    Role.GUARD: "守护N（可守自己，不可连续两晚同人）/ 过（空守）",
    Role.WOLF_KING: "刀N / 过 / 说XXX；出局后 开枪N / 不开枪",
    Role.WHITE_WOLF_KING: "刀N / 过 / 说XXX；白天群内 /自爆 N 带走一人",
    Role.HIDDEN_WOLF: "刀N（击杀）/ 过（空刀）/ 说XXX（与狼队友讨论）",
    Role.GRAVEKEEPER: "（无需操作，每晚我会私聊告诉你昨日放逐者的阵营）",
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
    Phase.NIGHT_GUARD: frozenset({ActionKind.GUARD, ActionKind.SKIP}),
    Phase.NIGHT_GRAVEKEEPER: frozenset(),
    Phase.NIGHT_WOLVES: frozenset({ActionKind.KILL, ActionKind.SKIP, ActionKind.SAY}),
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
    Phase.SHERIFF_FINAL_SPEECH: frozenset({ActionKind.SKIP, ActionKind.SELF_DETONATE}),
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


# 夜间技能的角色闸门（先于阶段闸门）：非技能持有者（含已出局者）不论
# 当前处于哪个夜间子阶段都得到同一句回复，否则"静默受理 vs 还不是时候"
# 的差异可被用来探测当前轮到哪个角色行动
_NO_SKILL = "你没有这个技能（或已无法行动）~"

# 夜间私聊行动 → 可使用它的角色
_NIGHT_SKILL_ROLES: dict[ActionKind, frozenset[Role]] = {
    ActionKind.KILL: WOLF_ROLES,
    ActionKind.SAY: WOLF_ROLES,
    ActionKind.GUARD: frozenset({Role.GUARD}),
    ActionKind.SAVE: frozenset({Role.WITCH}),
    ActionKind.POISON: frozenset({Role.WITCH}),
    ActionKind.CHECK: frozenset({Role.SEER}),
    ActionKind.CHOOSE_OWNER: frozenset({Role.HALFBLOOD}),
    ActionKind.SILENCE: frozenset({Role.SILENT_ELDER}),
    ActionKind.SKIP: WOLF_ROLES | {Role.WITCH, Role.SILENT_ELDER, Role.GUARD},
}

# 夜间子阶段 → 当前行动的角色集合
_NIGHT_PHASE_ROLE: dict[Phase, frozenset[Role]] = {
    Phase.NIGHT_HALFBLOOD: frozenset({Role.HALFBLOOD}),
    Phase.NIGHT_GUARD: frozenset({Role.GUARD}),
    Phase.NIGHT_WOLVES: WOLF_ROLES,
    Phase.NIGHT_WITCH: frozenset({Role.WITCH}),
    Phase.NIGHT_SEER: frozenset({Role.SEER}),
    Phase.NIGHT_ELDER: frozenset({Role.SILENT_ELDER}),
    Phase.NIGHT_GRAVEKEEPER: frozenset({Role.GRAVEKEEPER}),
}


def _lacks_skill(
    game: Optional[Game],
    user_id: int,
    role: "Role | frozenset[Role]",
) -> bool:
    """在局玩家不持有该角色或已出局（结果与当前阶段无关）。"""
    if game is None:
        return False
    player = game.player_by_user(user_id)
    roles = role if isinstance(role, frozenset) else frozenset({role})
    return player is None or not player.alive or player.role not in roles


def _sender_display(event: GroupMessageEvent) -> str:
    """报名者显示名：群名片 > 昵称 > QQ 号。"""
    return event.sender.card or event.sender.nickname or str(event.user_id)


# ── 开局与报名 ────────────────────────────────────────────

wolf_open = build_matcher(COMMAND_BY_NAME["狼人杀"])


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
        await wolf_open.finish(
            command_failure(
                "狼人杀房间未创建",
                "本群狼人杀正在进行",
                "/狼人状态 查看进度 · /操作 查看当前可做操作",
            )
        )
    if game_of_user(user_id) is not None:
        await wolf_open.finish(
            command_failure(
                "狼人杀房间未创建",
                "你已经参加其他对局",
                "先结束或退出当前对局，再重新开房",
            )
        )
    game = create_game(
        group_id,
        user_id,
        queue_max=config.ww_action_queue_max,
    )
    if game is None:
        await wolf_open.finish(
            command_failure(
                "狼人杀房间创建失败",
                "群或账号刚被其他玩法占用",
                "/狼人状态 查看当前对局，结束后再重试",
            )
        )
    # 注入收到本事件的 Bot：多机器人在线时 nonebot.get_bot() 会抛
    # ValueError，引擎不能依赖它选连接（见 engine.run_game）
    game.bot = bot
    note_signup_name(game, user_id, _sender_display(event))
    # 沿用本群上一局的板子与房规（房主可再改）
    prefs = room_prefs(group_id)
    prefs_note = ""
    if prefs is not None and prefs[0] in BOARDS:
        game.board, game.rules = prefs
        prefs_note = f"\n已沿用上局设置：板子「{game.board}」，房规 {_rules_line(game)}"
    game.worker = asyncio.create_task(engine.run_game(game))
    logger.info(f"狼人杀群 {group_id} 由 {user_id} 开房")
    hint = "可私聊 /选身份 身份名请求期望角色~" if config.ww_role_request else ""
    await wolf_open.finish(
        f"狼人杀房间已创建，房主已自动报名~{prefs_note}\n{hint}".rstrip()
    )


signup_cmd = build_matcher(COMMAND_BY_NAME["报名"])


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
    if int(event.get_user_id()) in game.kicked_user_ids:
        await signup_cmd.finish("你已被房主移出本局报名，下局再来吧~")
    if not join_signup(game, int(event.get_user_id())):
        await signup_cmd.finish("你已在局中，无需重复报名~")
    note_signup_name(game, int(event.get_user_id()), _sender_display(event))
    logger.info(
        f"狼人杀群 {int(event.group_id)} {int(event.get_user_id())} 报名"
        f"（{len(game.signup_user_ids)}/{eff_max}）"
    )
    count = len(game.signup_user_ids)
    if count >= eff_max:
        progress = "，已满员，马上开局！"
    elif count >= eff_min:
        progress = "，人数已够，房主可发 /开始游戏"
    else:
        progress = f"，还差 {eff_min - count} 人开局"
    response = (
        MessageSegment.at(event.user_id)
        + f"报名成功！当前 {count}/{eff_max} 人{progress}"
    )
    if config.ww_role_request:
        response += "\n可私聊 /选身份 身份名请求期望角色~"
    await signup_cmd.finish(response)


leave_cmd = build_matcher(COMMAND_BY_NAME["退报名"])


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


view_cmd = build_matcher(COMMAND_BY_NAME["查看报名"])


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
        f"房规：{_rules_line(game)}（房主可发 /房规 调整）",
    ]
    for idx, uid in enumerate(game.signup_user_ids, start=1):
        host_mark = "（房主）" if uid == game.host_user_id else ""
        lines.append(f"{idx}. {display_name_of(game, uid)}{host_mark}")
    lines.append("──────────────")
    lines.append(
        f"当前 {len(game.signup_user_ids)}/{eff_max} 人，至少 {eff_min} 人开局"
    )
    if config.ww_role_request:
        lines.append("报名后可私聊 /选身份 身份名，未请求则随机发牌")
    await view_cmd.finish("\n".join(lines))


status_cmd = build_matcher(COMMAND_BY_NAME["狼人状态"])


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


board_cmd = build_matcher(COMMAND_BY_NAME["板子"])


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
        current = len(game.signup_user_ids)
        lines = ["═══ 狼人杀 · 可选板子 ═══"]
        for spec in BOARDS.values():
            marker = "（当前）" if spec.key == game.board else ""
            fits = "✓" if current in spec.counts else " "
            lines.append(
                f"{fits} {spec.key}{marker}【{spec.level}】{spec.counts_summary()} 人"
            )
            if spec.description:
                lines.append(f"   {spec.description}")
        lines.append("──────────────")
        lines.append(f"✓ = 适合当前 {current} 人")
        lines.append(
            "房主发送 /板子 名称 切换（如 /板子 新手局），/狼人规则 板子名 看详情"
        )
        await board_cmd.finish("\n".join(lines))
    user_id = int(event.get_user_id())
    if not (user_id == game.host_user_id or is_group_admin(event) or _is_su(user_id)):
        await board_cmd.finish("只有房主、群管理员或超管可以切换板子~")
    resolved = resolve_board_key(text)
    if resolved is None:
        keys = "、".join(BOARDS)
        await board_cmd.finish(
            f"没有名为「{text}」的板子（或名称有歧义）。可选：{keys}"
        )
    text = resolved
    spec = BOARDS[text]
    # 切到与人数配置无交集、或容纳不下已报名人数的板子会让对局直接流局，提前拦下
    lo = max(config.ww_min_players, min(spec.counts))
    hi = min(config.ww_max_players, max(spec.counts))
    if lo > hi:
        await board_cmd.finish(
            f"板子「{text}」支持 {spec.counts_summary()} 人，"
            f"与本群人数配置（{config.ww_min_players}-{config.ww_max_players}）不兼容~"
        )
    if len(game.signup_user_ids) > hi:
        await board_cmd.finish(
            f"板子「{text}」最多 {hi} 人，当前已报名 {len(game.signup_user_ids)} 人，"
            "请先 /移除AI 或请人 /退报名 再切换~"
        )
    game.board = text
    current = len(game.signup_user_ids)
    note = (
        f"\n提示：当前已报名 {current} 人，不在该板子支持的人数范围"
        f"（{spec.counts_summary()} 人），开局前请用 /添加AI 或 /退报名 调整"
        if current not in spec.counts
        else ""
    )
    if spec.description:
        note = f"\n{spec.description}{note}"
    logger.info(f"狼人杀群 {int(event.group_id)} {user_id} 切换板子为 {text}")
    await board_cmd.finish(
        f"已切换板子：{text}（{spec.roles_summary()}，"
        f"支持 {spec.counts_summary()} 人）{note}"
    )


add_ai_cmd = build_matcher(COMMAND_BY_NAME["添加AI"])


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


remove_ai_cmd = build_matcher(COMMAND_BY_NAME["移除AI"])


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


start_cmd = build_matcher(COMMAND_BY_NAME["开始游戏"])


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
    can_fill = config.ww_ai_enabled and config.ww_ai_autofill
    if current not in board.counts and (not can_fill or not targets):
        fits = suggest_boards(current)
        hint = (
            f"适合 {current} 人的板子：{'、'.join(fits)}" if fits else "请调整报名人数"
        )
        await start_cmd.finish(
            f"板子「{board.key}」支持 {board.counts_summary()} 人，"
            f"当前 {current} 人。\n{hint}"
        )
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


end_cmd = build_matcher(COMMAND_BY_NAME["结束游戏"])


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

wish_cmd = build_matcher(COMMAND_BY_NAME["选身份"])


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
            f"格式：/选身份 身份名。可选身份：{board.roles_summary()}{current_note}"
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


unwish_cmd = build_matcher(COMMAND_BY_NAME["取消选身份"])


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

identity_cmd = build_matcher(COMMAND_BY_NAME["身份"])


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
    card = build_role_card(
        player.seat,
        player.role,
        len(game.players),
        silence_mode=board.silence_mode,
        roster=roster,
        rule_notes=house_rules.diff_from_default(game.rules),
    )
    notes = _private_notes(game, player)
    if notes:
        card += "\n─── 你的记录 ───\n" + "\n".join(notes)
    delivered = await engine._dm(game, player, card)
    if delivered:
        await identity_cmd.finish("身份卡已私聊重发，请查收~")
    await identity_cmd.finish("身份卡发送失败，请先加机器人为好友后再发送 /身份~")


def _private_notes(game: Game, player: PlayerState) -> list[str]:
    """/身份 附带的私有记录：只含本人本就知道的信息。"""
    notes: list[str] = []
    if not player.alive:
        notes.append("你已出局，请不要在群里透露信息~")
    if player.role is Role.SEER and player.seer_checks:
        notes.append(
            "查验："
            + "；".join(
                f"第{n}夜 {seat}号={res}" for n, seat, res in player.seer_checks
            )
        )
    if player.role is Role.WITCH:
        notes.append(
            f"解药{'已用' if player.save_used else '可用'}，"
            f"毒药{'已用' if player.poison_used else '可用'}"
        )
    if player.role is Role.GUARD and player.guard_last_target is not None:
        notes.append(f"昨晚守护：{player.guard_last_target}号（今晚不可连守）")
    if player.role is Role.HALFBLOOD and player.owner_seat is not None:
        notes.append(f"你的主人：{player.owner_seat}号")
    if player.role is Role.SILENT_ELDER and player.elder_last_target is not None:
        notes.append(f"昨晚目标：{player.elder_last_target}号（今晚不可连续选）")
    if player.faction is Faction.WOLF and player.role in WOLF_ROLES:
        mates = [
            f"{p.seat}号{'（已出局）' if not p.alive else ''}"
            for p in game.players
            if p.role in WOLF_ROLES and p.user_id != player.user_id
        ]
        if mates:
            notes.append(f"狼队友：{'、'.join(mates)}")
    if player.role in (Role.HUNTER, Role.WOLF_KING) and player.shot_used:
        notes.append("你已经开过枪了")
    return notes


# ── 仅私聊的特殊夜间行动命令 ──────────────────────────────

choose_owner_cmd = build_matcher(COMMAND_BY_NAME["认主"])


@choose_owner_cmd.handle()
async def handle_choose_owner(
    event: PrivateMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """混血儿首夜选择主人。"""
    user_id = int(event.get_user_id())
    game = game_of_user(user_id)
    if _lacks_skill(game, user_id, Role.HALFBLOOD):
        await choose_owner_cmd.finish(_NO_SKILL)
    if game is None or game.phase is not Phase.NIGHT_HALFBLOOD:
        await choose_owner_cmd.finish(
            _not_now(game, "认主 是混血儿首夜行动，请私聊发送 /认主 N~")
        )
    seat = _parse_seat(arg)
    if seat is None:
        await choose_owner_cmd.finish("格式：/认主 N（N 为主人座位号）")
    await _finish_action(
        choose_owner_cmd,
        game,
        Action(ActionKind.CHOOSE_OWNER, user_id, seat),
    )


silence_cmd = build_matcher(COMMAND_BY_NAME["禁言"])


@silence_cmd.handle()
async def handle_silence(
    event: PrivateMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """禁言/禁票长老夜间选择目标。"""
    user_id = int(event.get_user_id())
    game = game_of_user(user_id)
    if _lacks_skill(game, user_id, Role.SILENT_ELDER):
        await silence_cmd.finish(_NO_SKILL)
    if game is None or game.phase is not Phase.NIGHT_ELDER:
        await silence_cmd.finish(
            _not_now(game, "禁言（禁票板为禁票）是长老夜间行动，请私聊发送 /禁言 N~")
        )
    seat = _parse_seat(arg)
    if seat is None:
        await silence_cmd.finish("格式：/禁言 N（禁票板也可用 /禁票 N）")
    await _finish_action(
        silence_cmd,
        game,
        Action(ActionKind.SILENCE, user_id, seat),
    )


guard_cmd = build_matcher(COMMAND_BY_NAME["守护"])


@guard_cmd.handle()
async def handle_guard(
    event: PrivateMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """守卫夜间守护。"""
    user_id = int(event.get_user_id())
    game = game_of_user(user_id)
    if _lacks_skill(game, user_id, Role.GUARD):
        await guard_cmd.finish(_NO_SKILL)
    if game is None or game.phase is not Phase.NIGHT_GUARD:
        await guard_cmd.finish(
            _not_now(game, "守护 是守卫的夜间行动，请按私聊提示在夜里回复 守护N~")
        )
    seat = _parse_seat(arg)
    if seat is None:
        await guard_cmd.finish("格式：/守护 N（N 为目标座位号，可守自己）")
    await _finish_action(guard_cmd, game, Action(ActionKind.GUARD, user_id, seat))


# ── 夜间私聊命令 ──────────────────────────────────────────

kill_cmd = build_matcher(COMMAND_BY_NAME["刀"])


@kill_cmd.handle()
async def handle_kill(
    event: PrivateMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """狼人刀人。"""
    game = game_of_user(int(event.get_user_id()))
    if _lacks_skill(game, int(event.get_user_id()), WOLF_ROLES):
        await kill_cmd.finish(_NO_SKILL)
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


check_cmd = build_matcher(COMMAND_BY_NAME["查验"])


@check_cmd.handle()
async def handle_check(
    event: PrivateMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """预言家查验。"""
    game = game_of_user(int(event.get_user_id()))
    if _lacks_skill(game, int(event.get_user_id()), Role.SEER):
        await check_cmd.finish(_NO_SKILL)
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


save_cmd = build_matcher(COMMAND_BY_NAME["救"])


@save_cmd.handle()
async def handle_save(
    event: PrivateMessageEvent,
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """女巫救人。"""
    game = game_of_user(int(event.get_user_id()))
    if _lacks_skill(game, int(event.get_user_id()), Role.WITCH):
        await save_cmd.finish(_NO_SKILL)
    if game is None or game.phase is not Phase.NIGHT_WITCH:
        await save_cmd.finish(
            _not_now(game, "救 是女巫的夜间行动，请按私聊提示在夜里回复 救~")
        )
    await _finish_action(
        save_cmd,
        game,
        Action(ActionKind.SAVE, int(event.get_user_id())),
    )


poison_cmd = build_matcher(COMMAND_BY_NAME["毒"])


@poison_cmd.handle()
async def handle_poison(
    event: PrivateMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """女巫毒人。"""
    game = game_of_user(int(event.get_user_id()))
    if _lacks_skill(game, int(event.get_user_id()), Role.WITCH):
        await poison_cmd.finish(_NO_SKILL)
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


shoot_cmd = build_matcher(COMMAND_BY_NAME["开枪"])


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


no_shoot_cmd = build_matcher(COMMAND_BY_NAME["不开枪"])


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

run_cmd = build_matcher(COMMAND_BY_NAME["上警"])


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


withdraw_cmd = build_matcher(COMMAND_BY_NAME["退水"])


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


order_cmd = build_matcher(COMMAND_BY_NAME["排序"])


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


pass_badge_cmd = build_matcher(COMMAND_BY_NAME["移交警徽"])


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
    seat = _seat_from_arg(game, arg)
    if seat is None:
        await pass_badge_cmd.finish("格式：/移交警徽 N 或 /移交警徽 @某人")
    await _finish_action(
        pass_badge_cmd,
        game,
        Action(ActionKind.PASS_BADGE, int(event.get_user_id()), seat),
    )


tear_badge_cmd = build_matcher(COMMAND_BY_NAME["撕警徽"])


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


detonate_cmd = build_matcher(COMMAND_BY_NAME["自爆"])


@detonate_cmd.handle()
async def handle_detonate(
    event: GroupMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """狼人白天自爆；白狼王可 /自爆 N 带走一人（其他狼人带参数也只自爆）。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase not in SELF_DETONATE_PHASES:
        await detonate_cmd.finish(
            _not_now(game, "自爆 是狼人的白天行动，可在发言/投票阶段发动~")
        )
    seat = _seat_from_arg(game, arg) if str(arg).strip() else None
    await _finish_action(
        detonate_cmd,
        game,
        Action(ActionKind.SELF_DETONATE, int(event.get_user_id()), seat),
    )


duel_cmd = build_matcher(COMMAND_BY_NAME["决斗"])


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
    seat = _seat_from_arg(game, arg)
    if seat is None:
        await duel_cmd.finish("格式：/决斗 N 或 /决斗 @某人")
    await _finish_action(
        duel_cmd,
        game,
        Action(ActionKind.DUEL, int(event.get_user_id()), seat),
    )


vote_cmd = build_matcher(COMMAND_BY_NAME["投票"])


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
    seat = _seat_from_arg(game, arg)
    if seat is None:
        targets = _seat_list_text(game.vote_targets)
        await vote_cmd.finish(f"格式：/投票 N 或 /投票 @某人（可投：{targets}）")
    await _finish_action(
        vote_cmd,
        game,
        Action(ActionKind.VOTE, int(event.get_user_id()), seat),
    )


abstain_cmd = build_matcher(COMMAND_BY_NAME["弃票"])


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


skip_cmd = build_matcher(COMMAND_BY_NAME["过"])


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
    actor = game.player_by_user(user_id)
    if game.phase in _NIGHT_PHASES:
        # 夜间先查角色：非持有者的回复不随子阶段变化，防探测当前行动角色
        skill_roles = _NIGHT_SKILL_ROLES.get(action.kind)
        if skill_roles is not None and (
            actor is None or not actor.alive or actor.role not in skill_roles
        ):
            await private_listener.finish(_NO_SKILL)
    allowed = action.kind in _DM_ALLOWED.get(game.phase, frozenset())
    phase_role = _NIGHT_PHASE_ROLE.get(game.phase)
    if (
        allowed
        and phase_role is not None
        and (actor is None or actor.role not in phase_role)
    ):
        # 持有该技能、但当前不是自己角色的子阶段（如狼人在女巫阶段回复 过）
        allowed = False
    if not allowed:
        logger.info(
            f"狼人杀群 {game.group_id} {user_id} 私聊行动 "
            f"{action.kind.value} 在阶段 {game.phase.value} 不可用，闸门拦截"
        )
        await private_listener.finish(
            _not_now(game, "这条指令在当前阶段不可用，请按私聊提示行动~")
        )
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


# ── 房规 / 踢出 ───────────────────────────────────────────

rules_cmd = build_matcher(COMMAND_BY_NAME["房规"])


@rules_cmd.handle()
async def handle_rules(
    event: GroupMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """查看房规；房主/群管/超管在报名阶段可套用预设或逐项修改。"""
    game = get_game(int(event.group_id))
    if game is None:
        await rules_cmd.finish(
            "本群当前没有狼人杀对局（发送 /狼人杀 开房后可调整房规）"
        )
    text = str(arg).strip()
    if not text:
        lines = ["═══ 狼人杀 · 本局房规 ═══", *house_rules.describe(game.rules)]
        lines.append("──────────────")
        lines.append(f"预设：{'、'.join(house_rules.PRESETS)}（/房规 预设名 一键套用）")
        lines.append("修改单项：/房规 项目 值，如 /房规 警长 关、/房规 发言时长 90")
        await rules_cmd.finish("\n".join(lines))
    if game.phase is not Phase.SIGNUP:
        await rules_cmd.finish("房规已在发牌时锁定，下局开房后再调整吧~")
    user_id = int(event.get_user_id())
    if not (user_id == game.host_user_id or is_group_admin(event) or _is_su(user_id)):
        await rules_cmd.finish("只有房主、群管理员或超管可以调整房规~")
    if text in house_rules.PRESETS:
        game.rules = house_rules.PRESETS[text]
        diff = house_rules.diff_from_default(game.rules)
        logger.info(f"狼人杀群 {game.group_id} {user_id} 套用房规预设 {text}")
        await rules_cmd.finish(
            f"已套用「{text}」房规：{'；'.join(diff) or '与标准一致'}"
        )
    parts = text.split(maxsplit=1)
    if len(parts) != 2:  # noqa: PLR2004
        await rules_cmd.finish("格式：/房规 项目 值（发送 /房规 查看全部项目）")
    updated, message = house_rules.set_rule(game.rules, parts[0], parts[1])
    if not message.startswith("×"):
        game.rules = updated
        logger.info(f"狼人杀群 {game.group_id} {user_id} 房规：{message}")
    await rules_cmd.finish(message.lstrip("× "))


kick_cmd = build_matcher(COMMAND_BY_NAME["踢出"])


@kick_cmd.handle()
async def handle_kick(  # noqa: C901
    event: GroupMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """报名阶段移出某位报名者（@某人 或 名单序号）；被移出者本局不可再报名。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.SIGNUP:
        await kick_cmd.finish("只能在报名阶段移出玩家~")
    user_id = int(event.get_user_id())
    if not (user_id == game.host_user_id or is_group_admin(event) or _is_su(user_id)):
        await kick_cmd.finish("只有房主、群管理员或超管可以移出玩家~")
    target: Optional[int] = None
    for seg in arg:
        if seg.type == "at" and str(seg.data.get("qq", "")).isdigit():
            target = int(seg.data["qq"])
            break
    plain = arg.extract_plain_text().strip()
    if target is None and plain.isdigit():
        index = int(plain)
        if 1 <= index <= len(game.signup_user_ids):
            target = game.signup_user_ids[index - 1]
    if target is None or target not in game.signup_user_ids:
        await kick_cmd.finish("格式：/踢出 @某人 或 /踢出 序号（见 /查看报名）")
    if target == user_id:
        await kick_cmd.finish("不能移出自己，想退出请发 /退报名~")
    name = display_name_of(game, target)
    if is_ai_uid(target):
        leave_signup(game, target)
        game.ai_names.pop(target, None)
    else:
        leave_signup(game, target)
        game.kicked_user_ids.add(target)
    if target == game.host_user_id:
        humans = [uid for uid in game.signup_user_ids if not is_ai_uid(uid)]
        if humans:
            game.host_user_id = humans[0]
    logger.info(f"狼人杀群 {game.group_id} {user_id} 移出报名者 {target}")
    await kick_cmd.finish(f"已将 {name} 移出报名，当前 {len(game.signup_user_ids)} 人")


# ── 规则说明 / 复盘 / 再来一局 / 排行 ─────────────────────

guide_cmd = build_matcher(COMMAND_BY_NAME["狼人规则"])

_GAME_GUIDE = (
    "═══ 狼人杀 · 快速上手 ═══",
    "1. 开房：/狼人杀，其他人 /报名；人够了房主 /开始游戏（不够可 /添加AI）。",
    "2. 发牌：机器人私聊你身份卡，请先加机器人好友。",
    "3. 夜晚：全员禁言，有技能的人按私聊提示直接回复（如 刀3、查验5、救）。",
    "4. 白天：公布死讯 → 首日竞选警长 → 依次发言（/过 结束）→ /投票 N 放逐。",
    "5. 胜负：狼人全部出局好人胜；神职或平民全灭（屠边）狼人胜。",
    "──────────────",
    "/狼人规则 角色名：看某个角色（如 /狼人规则 守卫）",
    "/狼人规则 板子名：看某个板子的配置（如 /狼人规则 新手局）",
    "/板子 列出全部板子，/房规 查看本局规则，/狼人状态 随时看进度",
)


@guide_cmd.handle()
async def handle_guide(
    arg: Message = CommandArg(),
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """玩法教程：无参数给流程概览；角色名/板子名给详细说明。"""
    text = str(arg).strip()
    if not text:
        await guide_cmd.finish("\n".join(_GAME_GUIDE))
    role = parse_role(text)
    if role is not None:
        await guide_cmd.finish(role_guide(role))
    board_key = resolve_board_key(text)
    if board_key is not None:
        spec = BOARDS[board_key]
        lines = [
            f"═══ 板子 · {spec.key}【{spec.level}】 ═══",
            spec.description or "",
            *spec.composition_lines(),
        ]
        if spec.silence_mode == "vote":
            lines.append("本板子的长老为禁票长老（封投票权）")
        lines.append("──────────────")
        lines.append("/狼人规则 角色名 查看各角色技能")
        await guide_cmd.finish("\n".join(line for line in lines if line))
    await guide_cmd.finish(
        f"没找到「{text}」。可以查角色（如 女巫）或板子（如 预女猎白）~"
    )


review_cmd = build_matcher(COMMAND_BY_NAME["狼人复盘"])


@review_cmd.handle()
async def handle_review(
    event: GroupMessageEvent,
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """查看本群上一局复盘；对局进行中不提供，避免剧透。"""
    group_id = int(event.group_id)
    game = get_game(group_id)
    if game is not None and game.phase not in (Phase.SIGNUP, Phase.ENDED):
        await review_cmd.finish("对局进行中，结束后才能查看复盘哦~")
    text = last_review(group_id)
    if text is None:
        await review_cmd.finish(
            "本群还没有可复盘的狼人杀对局（复盘保存在内存中，重启后清空）"
        )
    await review_cmd.finish(text)


again_cmd = build_matcher(COMMAND_BY_NAME["再来一局"])


@again_cmd.handle()
async def handle_again(  # noqa: C901,PLR0912
    bot: Bot,
    event: GroupMessageEvent,
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """原班人马再开：发起者为房主，上局真人自动报名，AI 按上局人数补回。"""
    group_id = int(event.group_id)
    user_id = int(event.get_user_id())
    roster = last_roster(group_id)
    if roster is None:
        await again_cmd.finish("本群还没有上一局的记录，发送 /狼人杀 开新房吧~")
    if get_game(group_id) is not None:
        await again_cmd.finish("本群已有进行中的狼人杀对局~")
    if game_of_user(user_id) is not None:
        await again_cmd.finish("你已经在其他对局中了~")
    humans, ai_count, names = roster
    game = create_game(group_id, user_id, queue_max=config.ww_action_queue_max)
    if game is None:
        await again_cmd.finish("房间创建失败：群或账号刚被其他玩法占用")
    game.bot = bot
    note_signup_name(game, user_id, _sender_display(event))
    prefs = room_prefs(group_id)
    if prefs is not None and prefs[0] in BOARDS:
        game.board, game.rules = prefs
    _, eff_max = _eff_limits(game)
    rejoined: list[str] = []
    skipped = 0
    for uid in humans:
        if uid == user_id:
            continue
        if len(game.signup_user_ids) >= eff_max or not join_signup(game, uid):
            skipped += 1
            continue
        if uid in names:
            note_signup_name(game, uid, names[uid])
        rejoined.append(display_name_of(game, uid))
    added_ai = 0
    if config.ww_ai_enabled:
        for _ in range(ai_count):
            if len(game.signup_user_ids) >= eff_max or add_ai_signup(game) is None:
                break
            added_ai += 1
    game.worker = asyncio.create_task(engine.run_game(game))
    logger.info(
        f"狼人杀群 {group_id} 由 {user_id} 再来一局"
        f"（回归 {len(rejoined)} 人，AI {added_ai}，跳过 {skipped}）"
    )
    lines = [
        f"再来一局！房主：{display_name_of(game, user_id)}，板子「{game.board}」",
        f"已自动报名上局玩家：{'、'.join(rejoined) or '无'}",
    ]
    if added_ai:
        lines.append(f"已补回 {added_ai} 名玩家")
    if skipped:
        lines.append(f"{skipped} 人因已在其他对局或满员未能自动报名")
    lines.append("不想玩的请发 /退报名，房主 /开始游戏 即可发车")
    await again_cmd.finish("\n".join(lines))


rank_cmd = build_matcher(COMMAND_BY_NAME["狼人排行"])

_RANK_MIN_GAMES = 3
_STREAK_MIN = 2
_RANK_SIZE = 10


@rank_cmd.handle()
async def handle_rank(
    bot: Bot,
    event: GroupMessageEvent,
    session: async_scoped_session,
    _perm: None = require_feature("werewolf"),  # pyright: ignore[reportArgumentType]
) -> None:
    """本群胜场排行：只统计真人、已结束对局，至少 3 局上榜。"""
    group_id = int(event.group_id)
    stmt = (
        select(WerewolfPlayer.user_id, WerewolfPlayer.is_winner)
        .join(WerewolfGame, WerewolfGame.id == WerewolfPlayer.game_id)
        .where(
            WerewolfGame.group_id == group_id,
            WerewolfPlayer.is_ai == False,  # noqa: E712
            WerewolfPlayer.is_winner.is_not(None),
        )
    )
    rows = (await session.execute(stmt)).all()
    stats: dict[int, list[int]] = {}
    for uid, is_winner in rows:
        entry = stats.setdefault(int(uid), [0, 0])
        entry[0] += 1
        if is_winner:
            entry[1] += 1
    ranked = sorted(
        (
            (uid, games, wins)
            for uid, (games, wins) in stats.items()
            if games >= _RANK_MIN_GAMES
        ),
        key=lambda item: (-item[2], -item[2] / item[1], -item[1]),
    )[:_RANK_SIZE]
    if not ranked:
        await rank_cmd.finish(f"本群还没有玩够 {_RANK_MIN_GAMES} 局的玩家，快开一局吧~")
    names: dict[int, str] = {}
    try:
        members = await asyncio.wait_for(
            bot.get_group_member_list(group_id=group_id), timeout=config.ww_api_timeout
        )
        for member in members:
            names[int(member["user_id"])] = (
                member.get("card") or member.get("nickname") or ""
            )
    except Exception:  # noqa: BLE001
        logger.debug(f"狼人杀群 {group_id} 排行获取群名片失败，回退 QQ 号")
    medals = ("🥇", "🥈", "🥉")
    lines = ["═══ 本群狼人杀排行 ═══"]
    for index, (uid, games, wins) in enumerate(ranked):
        mark = medals[index] if index < len(medals) else f"{index + 1}."
        lines.append(
            f"{mark} {names.get(uid) or uid}："
            f"{wins} 胜 / {games} 局（{wins / games:.0%}）"
        )
    lines.append(f"（至少 {_RANK_MIN_GAMES} 局上榜，按胜场排序）")
    await rank_cmd.finish("\n".join(lines))


# ── 战绩 ──────────────────────────────────────────────────

record_cmd = build_matcher(COMMAND_BY_NAME["战绩"])


@record_cmd.handle()
async def handle_record(  # noqa: C901,PLR0912
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
    survived = sum(1 for r in finished if r.death_round is None)
    by_faction: dict[str, list[int]] = {"good": [0, 0], "wolf": [0, 0]}
    for r in finished:
        entry = by_faction.setdefault(r.faction, [0, 0])
        entry[0] += 1
        if r.is_winner:
            entry[1] += 1
    recent = sorted(finished, key=lambda r: r.id)[-10:]
    form = "".join("胜" if r.is_winner else "负" for r in recent)
    streak = 0
    for r in reversed(recent):
        if r.is_winner:
            streak += 1
        else:
            break
    lines = [
        "═══ 狼人战绩 ═══",
        f"总场次：{total}，胜场：{wins}，胜率：{wins / total:.1%}",
        f"存活到终局：{survived} 次，曾任警长：{sheriff_times} 次",
    ]
    for key, label in (("good", "好人阵营"), ("wolf", "狼人阵营")):
        played, won = by_faction.get(key, [0, 0])
        if played:
            lines.append(f"{label}：{played} 场 {won} 胜（{won / played:.0%}）")
    lines.append(
        f"最近 {len(recent)} 局：{form}"
        + (f"（{streak} 连胜中）" if streak >= _STREAK_MIN else "")
    )
    lines.append("─── 各角色 ───")
    for role in Role:
        if role.value in role_stats:
            played, won = role_stats[role.value]
            lines.append(f"{role.value}：{played} 场 {won} 胜")
    lines.append("──────────────")
    await record_cmd.finish("\n".join(lines))
