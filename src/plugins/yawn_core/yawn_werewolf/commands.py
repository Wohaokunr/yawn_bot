"""狼人杀命令入口：群命令、私聊命令与私聊自由文本监听。

处理器只做轻量校验（在局状态、阶段），把行动投入
game.action_queue 交给引擎裁决；私聊自由文本监听器在
行动阶段拦截在局玩家的私聊（其余私聊放行给 ai_chat）。
"""

import asyncio
import re
from typing import Optional

from nonebot import get_driver, get_plugin_config, on_command, on_message
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
from . import api, engine
from .config import Config
from .models import WerewolfPlayer
from .roles import Role
from .state import (
    SELF_DETONATE_PHASES,
    Action,
    ActionKind,
    Phase,
    create_game,
    game_of_user,
    get_game,
    join_signup,
    leave_signup,
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


def _parse_seat(text: object) -> Optional[int]:
    """从参数文本解析座位号（支持 "3" 与 "3号"）。"""
    match = re.fullmatch(r"(\d+)\s*号?", str(text).strip())
    return int(match.group(1)) if match else None


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
    game.worker = asyncio.create_task(engine.run_game(game))
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
    if len(game.signup_user_ids) >= config.ww_max_players:
        await signup_cmd.finish("报名已满员，等待开局~")
    if not join_signup(game, int(event.get_user_id())):
        await signup_cmd.finish("你已在局中，无需重复报名~")
    await signup_cmd.finish(
        MessageSegment.at(event.user_id)
        + f"报名成功！当前 {len(game.signup_user_ids)}"
        + f"/{config.ww_max_players} 人"
        + f"（至少 {config.ww_min_players} 人开局）"
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
    if not game.signup_user_ids:
        # 空房：直接取消引擎任务（报名阶段取消不广播）
        game.phase = Phase.ENDED
        task = game.worker
        game.worker = None
        if task is not None and not task.done():
            task.cancel()
        await leave_cmd.finish("房间已解散")
    if game.host_user_id == user_id:
        game.host_user_id = game.signup_user_ids[0]
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
    lines = ["═══ 狼人杀 · 报名名单 ═══"]
    lines += [f"{idx}. {uid}" for idx, uid in enumerate(game.signup_user_ids, start=1)]
    lines.append("──────────────")
    lines.append(
        f"当前 {len(game.signup_user_ids)}/{config.ww_max_players} 人，"
        f"至少 {config.ww_min_players} 人开局"
    )
    await view_cmd.finish("\n".join(lines))


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
    game.action_queue.put_nowait(Action(ActionKind.START_GAME, user_id))
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
        await kill_cmd.finish("现在不是狼人行动阶段")
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
        await check_cmd.finish("现在不是预言家行动阶段")
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
        await save_cmd.finish("现在不是女巫行动阶段")
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
        await poison_cmd.finish("现在不是女巫行动阶段")
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
        await shoot_cmd.finish("现在不是开枪决策阶段")
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
        await no_shoot_cmd.finish("现在不是开枪决策阶段")
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
        await run_cmd.finish("现在不是警长竞选报名阶段")
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
        await withdraw_cmd.finish("现在不是警长竞选阶段")
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
        await order_cmd.finish("现在不是发言排序阶段")
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
        await pass_badge_cmd.finish("现在不是警徽移交阶段")
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
        await tear_badge_cmd.finish("现在不是警徽移交阶段")
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
        await detonate_cmd.finish("现在不是可以自爆的白天阶段")
    game.action_queue.put_nowait(
        Action(ActionKind.SELF_DETONATE, int(event.get_user_id()))
    )
    await detonate_cmd.finish()


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
        await vote_cmd.finish("现在不是投票阶段")
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
        await abstain_cmd.finish("现在不是投票阶段")
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
        await skip_cmd.finish("现在没有可以跳过的发言环节")
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


_DM_PATTERNS: list[tuple[str, ActionKind, bool]] = [
    # (正则, 行动类型, 是否需要座位参数)
    (r"刀\s*(\d+)\s*号?", ActionKind.KILL, True),
    (r"(?:查验|验)\s*(\d+)\s*号?", ActionKind.CHECK, True),
    (r"救", ActionKind.SAVE, False),
    (r"毒\s*(\d+)\s*号?", ActionKind.POISON, True),
    (r"(?:开枪|带)\s*(\d+)\s*号?", ActionKind.SHOOT, True),
    (r"(?:不开枪|压枪)", ActionKind.NO_SHOOT, False),
    (r"(?:过|跳过)", ActionKind.SKIP, False),
    (r"自爆", ActionKind.SELF_DETONATE, False),
    (r"(?:上警|竞选)", ActionKind.RUN, False),
    (r"退水", ActionKind.WITHDRAW, False),
    (r"移交警徽\s*(\d+)\s*号?", ActionKind.PASS_BADGE, True),
    (r"撕警徽", ActionKind.TEAR_BADGE, False),
]

_DM_HINT = (
    "无法识别的指令。可用格式：\n"
    "刀N / 查验N / 救 / 毒N / 开枪N / 不开枪 / 过\n"
    "自爆 / 上警 / 退水 / 移交警徽N / 撕警徽\n"
    "说XXX（狼人讨论，转发给队友）"
)


def _parse_dm_action(text: str, user_id: int) -> Optional[Action]:
    """解析私聊自由文本为行动；无法解析返回 None。"""
    text = text.lstrip("/").strip()
    if not text:
        return None
    order_match = re.fullmatch(r"排序\s*(\d+)\s*号?\s*(顺|逆)?", text)
    if order_match is not None:
        aux = "ccw" if order_match.group(2) == "逆" else "cw"
        return Action(
            ActionKind.ORDER,
            user_id,
            int(order_match.group(1)),
            aux,
        )
    say_match = re.fullmatch(r"(?:说|发言|讨论)\s*(.+)", text, re.DOTALL)
    if say_match is not None:
        return Action(
            ActionKind.SAY,
            user_id,
            None,
            say_match.group(1).strip(),
        )
    for pattern, kind, need_seat in _DM_PATTERNS:
        match = re.fullmatch(pattern, text)
        if match is None:
            continue
        seat = int(match.group(1)) if need_seat else None
        return Action(kind, user_id, seat)
    return None


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
    action = _parse_dm_action(event.get_plaintext(), user_id)
    if action is None:
        await private_listener.finish(_DM_HINT)
    game.action_queue.put_nowait(action)
    await private_listener.finish()


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
    stmt = select(WerewolfPlayer).where(WerewolfPlayer.user_id == target)
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
