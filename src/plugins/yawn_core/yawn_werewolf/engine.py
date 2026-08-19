"""狼人杀游戏引擎：每局一个 asyncio 任务。

引擎独占所有状态变更与群播报；命令处理器只做校验，
并把 Action 投入 game.action_queue。所有可能卡住的
await 均包 asyncio.wait_for 超时，超时即托管，绝不踢人。
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional, Union

from nonebot import get_bot, get_plugin_config, logger
from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot_plugin_orm import get_session
from sqlalchemy import select

from ..event_log import record_game_event  # noqa: TID252
from ..replay import register_replay_participants  # noqa: TID252
from . import ai_player, api
from .config import Config
from .models import WerewolfGame, WerewolfPlayer
from .roles import (
    BOARDS,
    GOD_ROLES,
    ROLE_FACTION,
    VILLAGER_SIDE_ROLES,
    BoardSpec,
    DeathCause,
    Faction,
    Role,
    build_role_card,
    build_role_deck,
)
from .state import (
    DUEL_PHASES,
    SELF_DETONATE_PHASES,
    Action,
    ActionKind,
    Game,
    Phase,
    PlayerState,
    discard_game,
    display_name_of,
    is_ai_uid,
    release_action,
)

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import Message

config = get_plugin_config(Config)

_BJ_TZ = timezone(timedelta(hours=8))

# 警长平票终辩的固定时长（秒）
_FINAL_SPEECH_TIMEOUT = 60

# 夜间心跳播报文案：轮换使用。不得包含角色 / 座位 / 阶段信息——
# 夜间子阶段可能被跳过（如女巫双药已用），任何"缺席的播报"
# 都会被用来反推角色状态
_NIGHT_AMBIENT_LINES: tuple[str, ...] = (
    "夜深了，各方仍在暗中行动……",
    "夜晚还未结束，请耐心等候~",
    "寂静笼罩村庄，行动仍在继续……",
)


def _now_bj() -> datetime:
    """返回当前北京时间（naive），与项目时间约定一致。"""
    return datetime.now(_BJ_TZ).replace(tzinfo=None)


def _loop_time() -> float:
    """事件循环时钟，用于阶段截止时间计算。"""
    return asyncio.get_running_loop().time()


class _DetonatedError(Exception):
    """内部控制流：狼人自爆，需要立即中断当天流程。"""

    def __init__(self, player: PlayerState) -> None:
        self.player = player
        super().__init__(f"{player.seat}号自爆")


class _DuelNightError(Exception):
    """内部控制流：骑士决斗到狼人，当天流程立即结束进入夜晚。"""


class _ConcludedError(Exception):
    """内部控制流：白天流程中已决出胜负（如骑士决斗失败后狼人屠尽）。"""

    def __init__(self, winner: Faction) -> None:
        self.winner = winner
        super().__init__(f"{winner.value} 获胜")


# ── 基础辅助 ──────────────────────────────────────────────


@dataclass
class _Timer:
    """阶段计时器：截止时间 + 剩余查询。"""

    deadline: float

    def remaining(self) -> float:
        """距离截止的剩余秒数。"""
        return self.deadline - _loop_time()

    async def next_action(self, game: Game) -> Optional[Action]:
        """等待至多一个行动；超时返回 None。"""
        left = self.remaining()
        if left <= 0:
            return None
        return await _get_action(game, left)


async def _get_action(game: Game, step: float) -> Optional[Action]:
    """等待至多 step 秒获取一个行动；超时返回 None。"""
    try:
        action = await asyncio.wait_for(
            game.action_queue.get(),
            timeout=step,
        )
    except asyncio.TimeoutError:
        return None
    game.action_queue.task_done()
    release_action(game, action)
    player = game.player_by_user(action.actor_user_id)
    record_game_event(
        game,
        "werewolf",
        "action_received",
        phase=game.phase,
        round_no=game.round_no,
        actor_seat=player.seat if player is not None else None,
        payload={"action_kind": action.kind.value},
    )
    return action


def _enter_phase(game: Game, phase: Phase) -> None:
    """切换阶段并记日志；同阶段重复赋值不重复记录。"""
    if game.phase is phase:
        return
    logger.info(
        f"狼人杀群 {game.group_id} 进入阶段 {phase.value}（第 {game.round_no} 回合）"
    )
    game.phase = phase
    game.phase_token += 1
    record_game_event(
        game,
        "werewolf",
        "phase_changed",
        phase=phase,
        round_no=game.round_no,
        payload={"phase_token": game.phase_token},
    )
    ai_player.on_phase_change(game)


async def _announce(game: Game, text: Union[str, "Message"]) -> None:
    """群播报；机器人缺失时群侧静默，但始终抄送 AI 驱动的公共记录。"""
    ai_player.on_announce(game, str(text))
    if game.bot is None:
        return
    await api.safe_group_msg(game.bot, game.group_id, text)


async def _dm(game: Game, player: PlayerState, text: str) -> bool:
    """私聊玩家；首次失败时群内 @ 提示并标记 dm_ok=False。"""
    if player.is_ai:
        # AI 玩家不发真实私聊，提示词记入其座位上下文（ai_player 消费）
        ai_player.on_dm(game, player, text)
        return True
    if game.bot is None:
        return False
    ok = await api.send_dm(game.bot, player.user_id, text)
    if not ok and player.dm_ok:
        player.dm_ok = False
        await _announce(
            game,
            MessageSegment.at(player.user_id)
            + " 你的私聊发送失败：请加机器人为好友。\n"
            + "本局你的夜间行动将超时托管。",
        )
    return ok


async def _ban(game: Game, user_id: int, duration: int) -> None:
    """禁言群成员（duration=0 解除）；AI 合成 ID 无群成员，跳过。"""
    if is_ai_uid(user_id):
        return
    if game.bot is not None:
        await api.safe_ban(game.bot, game.group_id, user_id, duration)


async def _unban(game: Game, user_id: int) -> None:
    """解除群成员禁言；AI 合成 ID 无群成员，跳过。"""
    if is_ai_uid(user_id):
        return
    if game.bot is not None:
        await api.safe_unban(game.bot, game.group_id, user_id)


async def _whole_ban(game: Game, *, enable: bool) -> None:
    """切换全员禁言。"""
    if game.bot is not None:
        await api.safe_whole_ban(game.bot, game.group_id, enable=enable)


async def _unban_all_players(game: Game) -> None:
    """恢复白天发言权限：解禁存活者并重新单独禁言死者。"""
    for p in game.alive_players():
        await _unban(game, p.user_id)
    for p in game.players:
        if not p.alive:
            await _ban(game, p.user_id, 1800)


async def _ban_living_except(
    game: Game,
    except_user_id: Optional[int] = None,
) -> None:
    """禁言所有存活玩家（可排除一人），用于决策窗口控场。"""
    for p in game.alive_players():
        if p.user_id != except_user_id:
            await _ban(game, p.user_id, 1800)


async def _unban_living(game: Game) -> None:
    """解禁所有存活玩家。"""
    for p in game.alive_players():
        await _unban(game, p.user_id)


def _kill(game: Game, player: PlayerState, cause: DeathCause) -> None:
    """标记玩家死亡。"""
    if not player.alive:
        return
    player.alive = False
    player.death_cause = cause
    player.death_round = game.round_no
    logger.info(
        f"狼人杀群 {game.group_id} {player.seat}号 死亡"
        f"（{player.role.value}，死因 {cause.value}，第 {game.round_no} 回合）"
    )


def _board(game: Game) -> BoardSpec:
    """当前板子配置。"""
    return BOARDS[game.board]


def _effective_player_limits(game: Game, cfg: Config) -> Optional[tuple[int, int]]:
    """返回当前板子与全局配置交集；无合法人数时返回 None。"""
    board = _board(game)
    minimum = max(cfg.ww_min_players, min(board.counts))
    maximum = min(cfg.ww_max_players, max(board.counts))
    if minimum > maximum:
        return None
    return minimum, maximum


def _resolve_role_requests(game: Game, deck: list[Role]) -> dict[int, Role]:
    """结算报名阶段的身份请求，返回如愿的 user_id -> 角色。

    按份数满足：某角色的请求人数不超过牌堆份数则全部如愿，
    超出则在请求者中随机抽取份数个赢家。落选者与过期请求
    （报名中途 /板子 切换后角色不在牌堆里）不登记，回随机池。
    """
    counts: dict[Role, int] = {}
    for role in deck:
        counts[role] = counts.get(role, 0) + 1
    by_role: dict[Role, list[int]] = {}
    for uid in game.signup_user_ids:
        role = game.role_requests.get(uid)
        if role is not None and role in counts:
            by_role.setdefault(role, []).append(uid)
    wished: dict[int, Role] = {}
    for role, uids in by_role.items():
        n = counts[role]
        winners = uids if len(uids) <= n else random.sample(uids, n)
        for uid in winners:
            wished[uid] = role
    return wished


def _as_detonation(
    game: Game,
    action: Action,
) -> Optional[PlayerState]:
    """校验自爆行动：合法返回自爆者，否则 None。"""
    if action.kind is not ActionKind.SELF_DETONATE:
        return None
    if game.phase not in SELF_DETONATE_PHASES:
        return None
    player = game.player_by_user(action.actor_user_id)
    if player is None or not player.alive or player.role is not Role.WEREWOLF:
        return None
    return player


def _as_duel(
    game: Game,
    action: Action,
) -> Optional[tuple[PlayerState, PlayerState]]:
    """校验决斗行动：合法返回 (骑士, 目标)，否则 None。"""
    if action.kind is not ActionKind.DUEL:
        return None
    if game.phase not in DUEL_PHASES:
        return None
    knight = game.player_by_user(action.actor_user_id)
    if knight is None or not knight.alive or knight.role is not Role.KNIGHT:
        return None
    target = game.player_by_seat(action.value or -1)
    if target is None or not target.alive or target.user_id == knight.user_id:
        return None
    return knight, target


def _clockwise_order(
    players: list[PlayerState],
    start_seat: int,
    *,
    clockwise: bool,
) -> list[PlayerState]:
    """按座位从 start_seat 起顺/逆时针排列。"""
    ordered = sorted(players, key=lambda p: p.seat)
    if not ordered:
        return []
    idx = next(
        (i for i, p in enumerate(ordered) if p.seat == start_seat),
        0,
    )
    if clockwise:
        return ordered[idx:] + ordered[:idx]
    return [
        ordered[idx],
        *ordered[:idx][::-1],
        *ordered[idx + 1 :][::-1],
    ]


def _check_winner(game: Game) -> Optional[Faction]:
    """屠边规则判胜：无狼人→好人胜；神职或民边全灭→狼人胜。"""
    alive = game.alive_players()
    if not any(p.faction is Faction.WOLF for p in alive):
        return Faction.GOOD
    if not any(p.role in GOD_ROLES for p in alive):
        return Faction.WOLF
    if not any(p.role in VILLAGER_SIDE_ROLES for p in alive):
        return Faction.WOLF
    return None


def _seat_list(players: list[PlayerState]) -> str:
    """座位列表文本，如 "3、5、7号"。"""
    return "、".join(f"{p.seat}号" for p in players)


# ── 夜晚阶段 ──────────────────────────────────────────────


async def _phase_wolves(  # noqa: C901,PLR0912,PLR0915
    game: Game,
    cfg: Config,
) -> Optional[int]:
    """狼人阶段：私聊征刀、多数决；返回刀口座位（None=空刀）。"""
    _enter_phase(game, Phase.NIGHT_WOLVES)
    wolves = game.alive_players_of_role(Role.WEREWOLF)
    if not wolves:
        return None
    names = _seat_list(wolves)
    targets = _seat_list(
        [p for p in game.alive_players() if p.faction is not Faction.WOLF]
    )
    for w in wolves:
        await _dm(
            game,
            w,
            f"狼人请睁眼，本局狼人共 {len(wolves)} 名：{names}。\n"
            "可先讨论：回复 说XXX（如 说刀5），我会转发给其他狼人。\n"
            "统一目标后回复 刀N（如 刀3），或回复 过 明确选择空刀；"
            "超时未刀也视为空刀。\n"
            f"可刀对象：{targets}。",
        )
    votes: dict[int, int] = {}
    # 狼人 QQ -> 已确定刀口；None 表示明确选择空刀。
    # 明确空刀同样算已响应，所有狼人响应后即可提前结束阶段。
    submitted: dict[int, Optional[int]] = {}
    timer = _Timer(_loop_time() + cfg.ww_wolf_timeout)
    while len(submitted) < len(wolves) and timer.remaining() > 0:
        action = await timer.next_action(game)
        if action is None:
            continue
        if action.kind is ActionKind.SAY:
            speaker = game.player_by_user(action.actor_user_id)
            if (
                speaker is not None
                and speaker.alive
                and speaker.role is Role.WEREWOLF
                and action.aux
            ):
                for w in wolves:
                    if w.user_id != speaker.user_id:
                        await _dm(
                            game,
                            w,
                            f"【狼队】{speaker.seat}号：{action.aux}",
                        )
            continue
        if action.kind not in (ActionKind.KILL, ActionKind.SKIP):
            continue
        actor = game.player_by_user(action.actor_user_id)
        if actor is None or not actor.alive or actor.role is not Role.WEREWOLF:
            continue
        if actor.user_id in submitted:
            chosen = submitted[actor.user_id]
            choice = f"{chosen}号" if chosen is not None else "空刀"
            await _dm(
                game,
                actor,
                f"你的选择已确定为 {choice}，本夜不可更改",
            )
            continue
        if action.kind is ActionKind.SKIP:
            submitted[actor.user_id] = None
            await _dm(
                game,
                actor,
                f"已选择空刀（已响应 {len(submitted)}/{len(wolves)}）",
            )
            continue
        target = game.player_by_seat(action.value or -1)
        if target is None or not target.alive or target.faction is Faction.WOLF:
            await _dm(
                game,
                actor,
                "目标无效：请选择一名存活的非狼人玩家，回复 刀N",
            )
            continue
        submitted[actor.user_id] = target.seat
        votes[target.seat] = votes.get(target.seat, 0) + 1
        tally = "、".join(
            f"{seat}号{count}票"
            for seat, count in sorted(votes.items(), key=lambda kv: -kv[1])
        )
        for w in wolves:
            await _dm(
                game,
                w,
                f"当前刀型：{tally}（已响应 {len(submitted)}/{len(wolves)}）",
            )
    if not votes:
        all_responded = len(submitted) == len(wolves)
        reason = "全员明确选择空刀" if all_responded else "未形成有效刀口"
        logger.info(f"狼人杀群 {game.group_id} 狼人空刀（{reason}）")
        for w in wolves:
            await _dm(
                game,
                w,
                "狼队全员已响应，本夜空刀。"
                if all_responded
                else "狼队行动时间结束，未形成有效刀口，本夜视为空刀。",
            )
        return None
    max_count = max(votes.values())
    top = [seat for seat, count in votes.items() if count == max_count]
    kill_seat = random.choice(top)
    tie_note = f"（平票 {top} 随机选定）" if len(top) > 1 else ""
    logger.info(f"狼人杀群 {game.group_id} 狼人刀口：{kill_seat}号{tie_note}")
    return kill_seat


async def _phase_witch(  # noqa: C901
    game: Game,
    cfg: Config,
    kill_seat: Optional[int],
) -> tuple[bool, Optional[int]]:
    """女巫阶段：返回 (是否使用解药, 毒杀座位)。"""
    _enter_phase(game, Phase.NIGHT_WITCH)
    witches = game.alive_players_of_role(Role.WITCH)
    if not witches:
        return False, None
    witch = witches[0]
    if witch.save_used and witch.poison_used:
        return False, None
    kill_desc = f"{kill_seat}号" if kill_seat is not None else "无人"
    options = ["过（回复 过）"]
    can_save = kill_seat is not None and not witch.save_used and kill_seat != witch.seat
    if can_save:
        options.insert(0, "救（回复 救）")
    if not witch.poison_used:
        options.insert(1, "毒N（回复 毒N）")
    await _dm(
        game,
        witch,
        f"女巫请睁眼。昨晚 {kill_desc} 被刀（全程不可自救）。\n"
        f"可选操作：{'，'.join(options)}。",
    )
    timer = _Timer(_loop_time() + cfg.ww_night_timeout)
    while timer.remaining() > 0:
        action = await timer.next_action(game)
        if action is None or action.actor_user_id != witch.user_id:
            continue
        if action.kind is ActionKind.SAVE:
            if can_save:
                witch.save_used = True
                logger.info(
                    f"狼人杀群 {game.group_id} 女巫 {witch.seat}号 "
                    f"使用解药救活 {kill_seat}号"
                )
                await _dm(game, witch, f"已使用解药救活 {kill_seat}号")
                return True, None
            await _dm(
                game,
                witch,
                "无法使用解药（无刀口/解药已用/不可自救），请重新选择",
            )
        elif action.kind is ActionKind.POISON:
            target = game.player_by_seat(action.value or -1)
            if (
                not witch.poison_used
                and target is not None
                and target.alive
                and target.seat != witch.seat
            ):
                witch.poison_used = True
                logger.info(
                    f"狼人杀群 {game.group_id} 女巫 {witch.seat}号 "
                    f"使用毒药毒杀 {target.seat}号"
                )
                await _dm(game, witch, f"已使用毒药毒杀 {target.seat}号")
                return False, target.seat
            await _dm(game, witch, "目标无效或毒药已用，请重新选择")
        elif action.kind is ActionKind.SKIP:
            logger.info(f"狼人杀群 {game.group_id} 女巫 {witch.seat}号 选择不使用药剂")
            await _dm(game, witch, "选择不使用药剂")
            return False, None
    logger.info(f"狼人杀群 {game.group_id} 女巫 {witch.seat}号 超时未用药")
    await _dm(game, witch, "你超时未用药，已视为不使用药剂。")
    return False, None


async def _phase_seer(game: Game, cfg: Config) -> None:
    """预言家阶段：查验结果私聊反馈。"""
    _enter_phase(game, Phase.NIGHT_SEER)
    seers = game.alive_players_of_role(Role.SEER)
    if not seers:
        return
    seer = seers[0]
    others = [p for p in game.alive_players() if p.user_id != seer.user_id]
    await _dm(
        game,
        seer,
        "预言家请睁眼。回复 查验N（如 查验5）查验一名玩家的身份。\n"
        f"可选玩家：{_seat_list(others)}。",
    )
    timer = _Timer(_loop_time() + cfg.ww_night_timeout)
    while timer.remaining() > 0:
        action = await timer.next_action(game)
        if (
            action is None
            or action.actor_user_id != seer.user_id
            or action.kind is not ActionKind.CHECK
        ):
            continue
        target = game.player_by_seat(action.value or -1)
        if target is None or not target.alive or target.seat == seer.seat:
            await _dm(
                game,
                seer,
                "目标无效：请选择一名其他存活玩家，回复 查验N",
            )
            continue
        identity = "狼人" if target.role is Role.WEREWOLF else "好人"
        logger.info(
            f"狼人杀群 {game.group_id} 预言家 {seer.seat}号 "
            f"查验 {target.seat}号 → {identity}"
        )
        await _dm(game, seer, f"查验结果：{target.seat}号 是 {identity}")
        return
    logger.info(f"狼人杀群 {game.group_id} 预言家 {seer.seat}号 超时未查验")
    await _dm(game, seer, "你超时未查验，本夜没有获得查验结果。")


async def _phase_halfblood(game: Game, cfg: Config) -> None:
    """混血儿阶段（仅首夜，率先睁眼）：认主；超时随机指定。"""
    halfbloods = [
        p for p in game.alive_players_of_role(Role.HALFBLOOD) if p.owner_seat is None
    ]
    if not halfbloods:
        return
    _enter_phase(game, Phase.NIGHT_HALFBLOOD)
    halfblood = halfbloods[0]
    others = [p for p in game.alive_players() if p.user_id != halfblood.user_id]
    names = _seat_list(others)
    await _dm(
        game,
        halfblood,
        "混血儿请睁眼。第一夜你率先行动，选择一位主人。\n"
        f"可选玩家：{names}。\n"
        "回复 认主N（如 认主5）选定主人：你不知道 TA 的身份，"
        "TA 也不会知道被你选中。\n"
        "本局你的胜负随主人所在阵营。超时未选我会随机为你指定。",
    )
    timer = _Timer(_loop_time() + cfg.ww_night_timeout)
    while timer.remaining() > 0:
        action = await timer.next_action(game)
        if (
            action is None
            or action.actor_user_id != halfblood.user_id
            or action.kind is not ActionKind.CHOOSE_OWNER
        ):
            continue
        target = game.player_by_seat(action.value or -1)
        if target is None or not target.alive or target.seat == halfblood.seat:
            await _dm(
                game,
                halfblood,
                "目标无效：请选择一名其他存活玩家，回复 认主N",
            )
            continue
        halfblood.owner_seat = target.seat
        logger.info(
            f"狼人杀群 {game.group_id} 混血儿 {halfblood.seat}号 认主 {target.seat}号"
        )
        await _dm(
            game,
            halfblood,
            f"你选择了 {target.seat}号 作为主人。你不知道 TA 的身份，"
            "本局你的胜负随主人所在阵营。",
        )
        return
    # 认主是强制的：超时随机指定
    owner = random.choice(others)
    halfblood.owner_seat = owner.seat
    logger.info(
        f"狼人杀群 {game.group_id} 混血儿 {halfblood.seat}号 "
        f"超时未认主，随机指定 {owner.seat}号"
    )
    await _dm(
        game,
        halfblood,
        f"超时未选，已随机指定 {owner.seat}号 作为你的主人。",
    )


async def _phase_elder(game: Game, cfg: Config) -> None:
    """禁言长老阶段：选定次日禁言/禁票目标；放弃或超时打断连续链。"""
    # 先清隔夜残留：长老死亡 / 放弃 / 超时都不能让昨日禁言延续到今天
    game.silenced_seat = None
    board = _board(game)
    if board.silence_mode is None:
        return
    elders = game.alive_players_of_role(Role.SILENT_ELDER)
    if not elders:
        return
    elder = elders[0]
    _enter_phase(game, Phase.NIGHT_ELDER)
    mode_name = "禁言" if board.silence_mode == "speech" else "禁票"
    others = [
        p
        for p in game.alive_players()
        if p.user_id != elder.user_id and p.seat != elder.elder_last_target
    ]
    names = _seat_list(others)
    repeat_note = (
        f"\n注意：昨晚你{mode_name}了 {elder.elder_last_target}号，今晚不可连续选 TA。"
        if elder.elder_last_target is not None
        else ""
    )
    await _dm(
        game,
        elder,
        f"禁言长老请睁眼。回复 {mode_name}N（如 {mode_name}5）{mode_name}"
        f"一位玩家，或回复 过 放弃。{repeat_note}\n可选玩家：{names}。",
    )
    timer = _Timer(_loop_time() + cfg.ww_night_timeout)
    while timer.remaining() > 0:
        action = await timer.next_action(game)
        if action is None or action.actor_user_id != elder.user_id:
            continue
        if action.kind is ActionKind.SKIP:
            elder.elder_last_target = None
            logger.info(
                f"狼人杀群 {game.group_id} 禁言长老 {elder.seat}号 放弃{mode_name}"
            )
            await _dm(game, elder, f"今晚放弃{mode_name}")
            return
        if action.kind is not ActionKind.SILENCE:
            continue
        target = game.player_by_seat(action.value or -1)
        if (
            target is None
            or not target.alive
            or target.seat in (elder.seat, elder.elder_last_target)
        ):
            await _dm(
                game,
                elder,
                f"目标无效：请选择一名其他存活玩家（不可连续两晚同人），"
                f"请重新回复 {mode_name}N",
            )
            continue
        game.silenced_seat = target.seat
        elder.elder_last_target = target.seat
        logger.info(
            f"狼人杀群 {game.group_id} 禁言长老 {elder.seat}号 "
            f"{mode_name} {target.seat}号"
        )
        await _dm(game, elder, f"已{mode_name} {target.seat}号，明日生效")
        return
    # 超时视为放弃：打断连续链
    elder.elder_last_target = None
    logger.info(f"狼人杀群 {game.group_id} 禁言长老 {elder.seat}号 超时未{mode_name}")
    await _dm(game, elder, f"你超时未{mode_name}，今晚视为放弃。")


async def _night_heartbeat(game: Game, cfg: Config) -> None:
    """夜间全群禁言期间的通用进度播报。

    每隔 cfg.ww_night_warn_remain 秒播报一条不含角色/阶段信息的
    氛围文案，填补长夜死寂。旧的"按子阶段点名提醒"会向全群暴露
    当前行动角色（且被跳过的子阶段泄漏角色状态），已废弃。
    """
    idx = 0
    while True:
        await asyncio.sleep(cfg.ww_night_warn_remain)
        line = _NIGHT_AMBIENT_LINES[idx % len(_NIGHT_AMBIENT_LINES)]
        idx += 1
        await _announce(game, line)


async def _run_night(game: Game, cfg: Config) -> list[PlayerState]:
    """完整夜晚流程，返回当夜死者列表。"""
    await _announce(game, f"天黑请闭眼（第 {game.round_no} 夜）")
    await _whole_ban(game, enable=True)
    game.drain_actions()
    heartbeat = asyncio.create_task(_night_heartbeat(game, cfg))
    try:
        await _phase_halfblood(game, cfg)
        kill_seat = await _phase_wolves(game, cfg)
        saved, poison_seat = await _phase_witch(game, cfg, kill_seat)
        await _phase_seer(game, cfg)
        await _phase_elder(game, cfg)
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
    deaths: list[PlayerState] = []
    if kill_seat is not None and not saved:
        victim = game.player_by_seat(kill_seat)
        if victim is not None and victim.alive:
            _kill(game, victim, DeathCause.WOLF_KILL)
            deaths.append(victim)
    if poison_seat is not None:
        victim = game.player_by_seat(poison_seat)
        if victim is not None and victim.alive:
            _kill(game, victim, DeathCause.WITCH_POISON)
            deaths.append(victim)
    return deaths


# ── 白天子阶段 ────────────────────────────────────────────


async def _last_words(game: Game, cfg: Config, player: PlayerState) -> None:
    """遗言环节：群内限时发言，可被狼人自爆打断。"""
    _enter_phase(game, Phase.LAST_WORDS)
    await _unban(game, player.user_id)
    game.current_speaker = player.seat
    try:
        await _announce(
            game,
            f"请 {player.seat}号 发表遗言"
            f"（{cfg.ww_last_words_timeout} 秒，发送 /过 结束遗言）",
        )
        timer = _Timer(_loop_time() + cfg.ww_last_words_timeout)
        while timer.remaining() > 0:
            action = await timer.next_action(game)
            if action is None:
                continue
            det = _as_detonation(game, action)
            if det is not None:
                raise _DetonatedError(det)
            if (
                action.actor_user_id == player.user_id
                and action.kind is ActionKind.SKIP
            ):
                break
    finally:
        game.current_speaker = None
    await _ban(game, player.user_id, 1800)


async def _hunter_prompt(
    game: Game,
    cfg: Config,
    hunter: PlayerState,
) -> Optional[int]:
    """询问猎人是否开枪；返回带走座位（None=不开枪）。"""
    _enter_phase(game, Phase.HUNTER_SHOT)
    await _unban(game, hunter.user_id)
    await _announce(
        game,
        f"猎人 {hunter.seat}号 死亡，开枪决策请私聊机器人"
        f"（回复 开枪N 或 不开枪，{cfg.ww_hunter_timeout} 秒，"
        "超时视为不开枪）",
    )
    await _dm(
        game,
        hunter,
        "你已死亡，可以开枪：回复 开枪N 带走一名玩家，或回复 不开枪。\n"
        f"可开枪对象：{_seat_list(game.alive_players())}。",
    )
    await _ban_living_except(game)
    try:
        timer = _Timer(_loop_time() + cfg.ww_hunter_timeout)
        while timer.remaining() > 0:
            action = await timer.next_action(game)
            if action is None or action.actor_user_id != hunter.user_id:
                continue
            if action.kind is ActionKind.SHOOT:
                target = game.player_by_seat(action.value or -1)
                if target is not None and target.alive:
                    await _ban(game, hunter.user_id, 1800)
                    return target.seat
                await _announce(game, "开枪目标无效，请重新发送 /开枪 N")
            elif action.kind in (ActionKind.NO_SHOOT, ActionKind.SKIP):
                # 无效目标与超时都有反馈，显式放弃也补一条确认
                await _dm(game, hunter, "收到，你选择不开枪，猎枪已压下。")
                await _ban(game, hunter.user_id, 1800)
                return None
        await _ban(game, hunter.user_id, 1800)
        return None
    finally:
        await _unban_living(game)


async def _resolve_hunter_shot(
    game: Game,
    cfg: Config,
    seat: int,
    hunter_seat: int,
) -> None:
    """结算猎人开枪：击杀目标并处理警徽。"""
    victim = game.player_by_seat(seat)
    if victim is None or not victim.alive:
        return
    _kill(game, victim, DeathCause.HUNTER_SHOT)
    await _announce(
        game,
        f"猎人 {hunter_seat}号 开枪带走了 {victim.seat}号，"
        f"其身份是 {victim.role.value}",
    )
    if victim.is_sheriff:
        await _badge_transfer(game, cfg, victim)


async def _resolve_pending_day_effects(
    game: Game,
    cfg: Config,
    pending: list[PlayerState],
) -> None:
    """按死亡队列结算猎人连锁开枪和警徽，保证放逐/遗言/自爆路径一致。"""
    processed: set[int] = set()
    index = 0
    while index < len(pending):
        dead = pending[index]
        index += 1
        if dead.role is Role.HUNTER and dead.death_cause is not DeathCause.WITCH_POISON:
            if dead.user_id in processed:
                continue
            processed.add(dead.user_id)
            shot = await _hunter_prompt(game, cfg, dead)
            if shot is not None:
                await _resolve_hunter_shot(game, cfg, shot, dead.seat)
                victim = game.player_by_seat(shot)
                if victim is not None:
                    pending.append(victim)
        if not dead.is_sheriff:
            continue
        if dead.death_cause is DeathCause.WITCH_POISON:
            dead.is_sheriff = False
            await _announce(game, f"警长 {dead.seat}号 被毒杀，警徽随其一同失效")
        else:
            await _badge_transfer(game, cfg, dead)


async def _resolve_knight_duel(
    game: Game,
    cfg: Config,
    knight: PlayerState,
    target: PlayerState,
) -> None:
    """结算骑士决斗。

    决斗到狼人：目标死亡，抛 _DuelNightError 令当天立即入夜；
    决斗到好人：双方身份公示、骑士死亡，若狼人随即获胜则抛
    _ConcludedError，否则正常返回（白天流程继续）。决斗死亡无遗言。
    """
    logger.info(
        f"狼人杀群 {game.group_id} 骑士 {knight.seat}号 决斗 {target.seat}号"
        f"（身份 {target.role.value}）"
    )
    if target.faction is Faction.WOLF:
        _kill(game, target, DeathCause.KNIGHT_KILL)
        await _announce(
            game,
            f"{knight.seat}号 翻牌骑士，决斗 {target.seat}号！"
            f"{target.seat}号 是狼人，被骑士决斗致死！",
        )
        if target.is_sheriff:
            await _badge_transfer(game, cfg, target)
        await _announce(game, "白天流程立即结束，进入夜晚。")
        raise _DuelNightError
    await _announce(
        game,
        f"{knight.seat}号 翻牌骑士，决斗 {target.seat}号！"
        f"{target.seat}号 是好人（{target.role.value}），骑士决斗失败，"
        f"{knight.seat}号 骑士身亡",
    )
    _kill(game, knight, DeathCause.KNIGHT_DEATH)
    if knight.is_sheriff:
        await _badge_transfer(game, cfg, knight)
    winner = _check_winner(game)
    if winner is not None:
        raise _ConcludedError(winner)
    await _announce(game, "白天流程继续")


async def _badge_transfer(
    game: Game,
    cfg: Config,
    sheriff: PlayerState,
) -> None:
    """警徽移交环节：超时视为撕警徽。"""
    _enter_phase(game, Phase.BADGE_TRANSFER)
    await _unban(game, sheriff.user_id)
    await _announce(
        game,
        f"警长 {sheriff.seat}号 死亡，请移交警徽："
        f"发送 /移交警徽 N（移交给存活玩家）或 /撕警徽"
        f"（{cfg.ww_badge_timeout} 秒，超时视为撕警徽）",
    )
    others = [p for p in game.alive_players() if p.user_id != sheriff.user_id]
    await _dm(
        game,
        sheriff,
        "你已死亡，请决定警徽流向：回复 移交警徽N 或 撕警徽\n"
        f"可移交对象：{_seat_list(others)}。",
    )
    await _ban_living_except(game)
    tore_explicitly = False
    try:
        timer = _Timer(_loop_time() + cfg.ww_badge_timeout)
        new_sheriff: Optional[PlayerState] = None
        while timer.remaining() > 0:
            action = await timer.next_action(game)
            if action is None or action.actor_user_id != sheriff.user_id:
                continue
            if action.kind is ActionKind.PASS_BADGE:
                target = game.player_by_seat(action.value or -1)
                if (
                    target is not None
                    and target.alive
                    and target.user_id != sheriff.user_id
                ):
                    new_sheriff = target
                    break
                await _announce(game, "移交目标无效，请重新发送 /移交警徽 N")
            elif action.kind is ActionKind.TEAR_BADGE:
                tore_explicitly = True
                break
    finally:
        await _unban_living(game)
    sheriff.is_sheriff = False
    if new_sheriff is not None:
        new_sheriff.is_sheriff = True
        new_sheriff.was_sheriff = True
        await _announce(
            game,
            f"{sheriff.seat}号 将警徽移交给 {new_sheriff.seat}号，新任警长产生",
        )
    elif tore_explicitly:
        await _announce(
            game,
            f"{sheriff.seat}号 撕掉了警徽，本局不再有警长",
        )
    else:
        await _announce(
            game,
            f"{sheriff.seat}号 超时未移交警徽，警徽随之失效，本局不再有警长",
        )
    await _ban(game, sheriff.user_id, 1800)


# ── 发言与投票 ────────────────────────────────────────────


async def _speech_rotation(  # noqa: C901,PLR0912,PLR0913,PLR0915
    game: Game,
    cfg: Config,
    order: list[PlayerState],
    phase: Phase,
    *,
    allow_withdraw: bool = False,
    speech_timeout: Optional[int] = None,
) -> None:
    """单人禁言轮换发言；可被狼人自爆 / 骑士决斗打断。"""
    _enter_phase(game, phase)
    # 清空上一子阶段滞留的行动（如 DAY_VOTE 期间压入的决斗指令）：
    # 骑士决斗只认发言轮换窗口内到达的行动
    game.drain_actions()
    if phase in DUEL_PHASES:
        for knight in game.alive_players_of_role(Role.KNIGHT):
            await _dm(
                game,
                knight,
                "你是骑士：本发言阶段可随时在群内发送 /决斗N "
                "翻牌决斗一位玩家。决斗到狼人——其立即死亡并直接进入黑夜；"
                "决斗到好人——双方身份公示，你死亡，白天流程继续。"
                "也可以不决斗。",
            )
    timeout = speech_timeout or cfg.ww_speech_timeout
    ban_total = timeout * max(len(order), 1) + 600
    for p in game.alive_players():
        await _ban(game, p.user_id, ban_total)
    for p in game.players:
        if not p.alive:
            await _ban(game, p.user_id, ban_total)
    board = _board(game)
    for speaker in order:
        if not speaker.alive:
            continue
        if allow_withdraw and not speaker.sheriff_candidate:
            continue
        if board.silence_mode == "speech" and speaker.seat == game.silenced_seat:
            # 被禁言者全程不解禁（物理禁言），跳过其发言窗口
            await _announce(game, f"{speaker.seat}号 被禁言，跳过发言")
            continue
        await _unban(game, speaker.user_id)
        # current_speaker 供 AI 驱动识别发言窗口；try/finally 防自爆中断遗留
        game.current_speaker = speaker.seat
        try:
            await _announce(
                game,
                f"请 {speaker.seat}号 发言（{timeout} 秒，发送 /过 结束发言）",
            )
            timer = _Timer(_loop_time() + timeout)
            window_done = False  # 发言者主动结束（SKIP/退水），区别于超时
            while timer.remaining() > 0:
                action = await timer.next_action(game)
                if action is None:
                    continue
                det = _as_detonation(game, action)
                if det is not None:
                    raise _DetonatedError(det)
                duel = _as_duel(game, action)
                if action.kind is ActionKind.DUEL and duel is None:
                    actor = game.player_by_user(action.actor_user_id)
                    if actor is not None and actor.alive and actor.role is Role.KNIGHT:
                        await _dm(
                            game,
                            actor,
                            "决斗目标无效：请选择一名其他存活玩家，请重新发送 /决斗N",
                        )
                    continue
                if duel is not None:
                    knight, duel_target = duel
                    # 成功→_DuelNightError 入夜；失败且狼人屠尽→_ConcludedError
                    await _resolve_knight_duel(game, cfg, knight, duel_target)
                    # 走到这里说明决斗失败、白天继续：_badge_transfer 以全员
                    # 解禁收尾，需恢复"除发言者外全员禁言"的轮换不变式，
                    # 并回到本阶段（移交把阶段切去了 BADGE_TRANSFER）
                    for p in game.alive_players():
                        if speaker.alive and p.user_id == speaker.user_id:
                            continue
                        await _ban(game, p.user_id, ban_total)
                    _enter_phase(game, phase)
                    if not speaker.alive:
                        # 骑士决斗失败身亡且正是当前发言者：结束本窗口
                        window_done = True
                        break
                    continue
                if allow_withdraw and action.kind is ActionKind.WITHDRAW:
                    wd = game.player_by_user(action.actor_user_id)
                    if wd is not None and wd.sheriff_candidate:
                        wd.sheriff_candidate = False
                        await _announce(game, f"{wd.seat}号 退水")
                        if wd.user_id == speaker.user_id:
                            window_done = True
                            break
                    continue
                if (
                    action.actor_user_id == speaker.user_id
                    and action.kind is ActionKind.SKIP
                ):
                    window_done = True
                    break
            if not window_done:
                # 窗口超时：给一个可见交代，避免"请 N号 发言"后一片死寂
                await _announce(game, f"{speaker.seat}号 超时未发言")
        finally:
            game.current_speaker = None
        await _ban(game, speaker.user_id, 1800)
    await _unban_all_players(game)


async def _decide_speech_order(
    game: Game,
    cfg: Config,
) -> list[PlayerState]:
    """决定发言顺序：警长定序，无警长则随机起顺时针。"""
    alive = game.alive_players()
    sheriff = game.sheriff()
    if sheriff is not None and sheriff.alive:
        _enter_phase(game, Phase.DAY_SPEECH)
        await _unban(game, sheriff.user_id)
        await _announce(
            game,
            f"请警长 {sheriff.seat}号 决定发言顺序："
            "发送 /排序 N 顺（N号起顺时针）或 /排序 N 逆（N号起逆时针），"
            f"{cfg.ww_badge_timeout} 秒后随机决定",
        )
        await _ban_living_except(game, sheriff.user_id)
        try:
            timer = _Timer(_loop_time() + cfg.ww_badge_timeout)
            while timer.remaining() > 0:
                action = await timer.next_action(game)
                if action is None:
                    continue
                det = _as_detonation(game, action)
                if det is not None:
                    raise _DetonatedError(det)
                if (
                    action.kind is ActionKind.ORDER
                    and action.actor_user_id == sheriff.user_id
                ):
                    start = game.player_by_seat(action.value or -1)
                    if start is not None and start.alive:
                        clockwise = action.aux != "ccw"
                        direction = "顺时针" if clockwise else "逆时针"
                        await _announce(
                            game,
                            f"警长决定：从 {start.seat}号 开始{direction}发言",
                        )
                        return _clockwise_order(
                            alive,
                            start.seat,
                            clockwise=clockwise,
                        )
                    await _announce(game, "起始座位无效，请重新发送 /排序 N 顺|逆")
        finally:
            await _unban_living(game)
    start = random.choice(alive)
    await _announce(
        game,
        f"随机决定发言顺序：从 {start.seat}号 开始顺时针发言",
    )
    return _clockwise_order(alive, start.seat, clockwise=True)


async def _collect_votes(  # noqa: C901
    game: Game,
    cfg: Config,
    target_seats: list[int],
    phase: Phase,
    exclude_seats: tuple[int, ...] = (),
) -> dict[int, Optional[int]]:
    """收票：返回 {投票者QQ: 目标座位或None(弃票)}。"""
    _enter_phase(game, phase)
    # 合法投票目标供 AI 驱动校验/构建提示词
    game.vote_targets = list(target_seats)
    game.vote_exclude = tuple(exclude_seats)
    await _unban_all_players(game)
    votes: dict[int, Optional[int]] = {}
    eligible = [
        p for p in game.alive_players() if p.can_vote and p.seat not in exclude_seats
    ]
    timer = _Timer(_loop_time() + cfg.ww_vote_timeout)
    while timer.remaining() > 0 and len(votes) < len(eligible):
        action = await timer.next_action(game)
        if action is None:
            continue
        det = _as_detonation(game, action)
        if det is not None:
            raise _DetonatedError(det)
        actor = game.player_by_user(action.actor_user_id)
        if (
            actor is None
            or not actor.alive
            or not actor.can_vote
            or actor.seat in exclude_seats
        ):
            continue
        if actor.user_id in votes and action.kind in (
            ActionKind.VOTE,
            ActionKind.ABSTAIN,
        ):
            await _announce(
                game,
                f"{actor.seat}号 已完成本轮投票，本轮不可改票",
            )
            continue
        if action.kind is ActionKind.VOTE:
            target = game.player_by_seat(action.value or -1)
            if target is not None and target.alive and target.seat in target_seats:
                votes[actor.user_id] = target.seat
                await _announce(
                    game,
                    f"{actor.seat}号 投票给 {target.seat}号",
                )
                continue
            await _announce(
                game,
                f"{actor.seat}号 投票无效，请重新发送 /投票 N",
            )
        elif action.kind is ActionKind.ABSTAIN:
            votes[actor.user_id] = None
            await _announce(game, f"{actor.seat}号 弃票")
    detail_parts: list[str] = []
    for uid, seat in votes.items():
        actor = game.player_by_user(uid)
        if actor is None:
            continue
        detail_parts.append(
            f"{actor.seat}号→{f'{seat}号' if seat is not None else '弃票'}"
        )
    logger.info(
        f"狼人杀群 {game.group_id} {phase.value} 收票结束"
        f"（{len(votes)}/{len(eligible)} 人）：{'、'.join(detail_parts) or '无'}"
    )
    await _announce_vote_tally(game, votes, eligible)
    return votes


def _vote_counts(
    game: Game,
    votes: dict[int, Optional[int]],
) -> dict[int, float]:
    """统计各座位得票（警长票权重 1.5）。"""
    sheriff = game.sheriff()
    counts: dict[int, float] = {}
    for voter_uid, seat in votes.items():
        if seat is None:
            continue
        weight = 1.5 if sheriff and voter_uid == sheriff.user_id else 1.0
        counts[seat] = counts.get(seat, 0) + weight
    return counts


async def _announce_vote_tally(
    game: Game,
    votes: dict[int, Optional[int]],
    eligible: list[PlayerState],
) -> None:
    """收票结束后群播计票汇总：得票（标注警长 1.5 票）+ 弃票/未投票。"""
    counts = _vote_counts(game, votes)
    sheriff = game.sheriff()
    sheriff_target = votes.get(sheriff.user_id) if sheriff else None
    lines = ["═══ 计票结果 ═══"]
    if counts:
        for seat, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            note = "（含警长 1.5 票）" if seat == sheriff_target else ""
            lines.append(f"{seat}号：{count:g} 票{note}")
    else:
        lines.append("无人获得投票")
    abstained = [p for p in eligible if p.user_id in votes and votes[p.user_id] is None]
    no_vote = [p for p in eligible if p.user_id not in votes]
    if abstained:
        lines.append(f"弃票：{_seat_list(abstained)}")
    if no_vote:
        lines.append(f"未投票：{_seat_list(no_vote)}")
    await _announce(game, "\n".join(lines))


def _tally_votes(
    game: Game,
    votes: dict[int, Optional[int]],
) -> tuple[Optional[int], list[int]]:
    """统计票数（警长 1.5 票）：返回 (唯一最高者, 平票列表)。"""
    counts = _vote_counts(game, votes)
    if not counts:
        return None, []
    max_count = max(counts.values())
    top = [seat for seat, count in counts.items() if count == max_count]
    if len(top) == 1:
        return top[0], top
    return None, top


# ── 警长竞选 ──────────────────────────────────────────────


async def _elect_sheriff(game: Game, seat: int) -> None:
    """宣布警长当选。"""
    player = game.player_by_seat(seat)
    if player is None:
        return
    player.is_sheriff = True
    player.was_sheriff = True
    await _announce(
        game,
        f"{seat}号 当选警长！其放逐投票权重为 1.5 票，并可决定白天的发言顺序",
    )


async def _sheriff_campaign(  # noqa: C901,PLR0912,PLR0915
    game: Game,
    cfg: Config,
) -> None:
    """警长竞选（仅第 1 天）；可被 _DetonatedError 打断。"""
    _enter_phase(game, Phase.SHERIFF_REGISTER)
    # 有 AI 参与时延长报名窗口：AI 的竞选决策是一次 LLM 调用，
    # 不延长则迟到的上警会落进发言阶段被丢弃，AI 连候选人都当不上
    register_timeout = cfg.ww_sheriff_register_timeout + (
        cfg.ww_ai_register_buffer if any(p.is_ai for p in game.alive_players()) else 0
    )
    await _announce(
        game,
        "警长竞选开始：想竞选的玩家请发送 /上警"
        f"（{register_timeout} 秒），"
        "竞选期间可发送 /退水 退出竞选",
    )
    timer = _Timer(_loop_time() + register_timeout)
    while timer.remaining() > 0:
        action = await timer.next_action(game)
        if action is None:
            continue
        det = _as_detonation(game, action)
        if det is not None:
            raise _DetonatedError(det)
        player = game.player_by_user(action.actor_user_id)
        if player is None or not player.alive:
            continue
        if action.kind is ActionKind.RUN:
            if not player.sheriff_candidate:
                player.sheriff_candidate = True
                await _announce(game, f"{player.seat}号 上警")
        elif action.kind is ActionKind.WITHDRAW and player.sheriff_candidate:
            player.sheriff_candidate = False
            await _announce(game, f"{player.seat}号 退水")
    # 报名窗口关闭时队列里可能仍有压线到达的行动（多为 AI 决策），
    # 全部取出按报名规则处理，避免被后续发言阶段无声丢弃
    while not game.action_queue.empty():
        action = game.action_queue.get_nowait()
        game.action_queue.task_done()
        release_action(game, action)
        player = game.player_by_user(action.actor_user_id)
        if player is None or not player.alive:
            continue
        if action.kind is ActionKind.RUN and not player.sheriff_candidate:
            player.sheriff_candidate = True
            logger.info(
                f"狼人杀群 {game.group_id} {player.seat}号 压线上警（报名窗口刚关闭）"
            )
            await _announce(game, f"{player.seat}号 上警")
        elif action.kind is ActionKind.WITHDRAW and player.sheriff_candidate:
            player.sheriff_candidate = False
            await _announce(game, f"{player.seat}号 退水")
    candidates = [p for p in game.alive_players() if p.sheriff_candidate]
    if not candidates:
        await _announce(game, "无人上警，本局没有警长")
        return
    names = "、".join(f"{p.seat}号" for p in candidates)
    await _announce(game, f"竞选玩家：{names}，下面依次进行竞选发言")
    start = random.choice(candidates)
    order = _clockwise_order(candidates, start.seat, clockwise=True)
    await _speech_rotation(
        game,
        cfg,
        order,
        Phase.SHERIFF_SPEECH,
        allow_withdraw=True,
    )
    candidates = [p for p in candidates if p.sheriff_candidate and p.alive]
    if not candidates:
        await _announce(game, "所有竞选玩家均已退水，本局没有警长")
        return
    names = "、".join(f"{p.seat}号" for p in candidates)
    await _announce(
        game,
        f"请投票选出警长：/投票 N（候选人：{names}）或 /弃票"
        f"（{cfg.ww_vote_timeout} 秒）",
    )
    votes = await _collect_votes(
        game,
        cfg,
        [p.seat for p in candidates],
        Phase.SHERIFF_VOTE,
    )
    winner_seat, tied = _tally_votes(game, votes)
    if winner_seat is not None:
        await _elect_sheriff(game, winner_seat)
        return
    if not tied:
        await _announce(game, "无人获得警长票，本局没有警长")
        return
    # 平票：终辩后在平票者间重投一轮
    tied_names = "、".join(f"{s}号" for s in tied)
    await _announce(
        game,
        f"警长投票平票：{tied_names}，进行终辩后重投",
    )
    tied_players = [p for p in candidates if p.seat in tied]
    await _speech_rotation(
        game,
        cfg,
        tied_players,
        Phase.SHERIFF_FINAL_SPEECH,
        speech_timeout=_FINAL_SPEECH_TIMEOUT,
    )
    tied_players = [p for p in tied_players if p.sheriff_candidate and p.alive]
    if not tied_players:
        await _announce(game, "终辩后已无候选人，本局没有警长")
        return
    names = "、".join(f"{p.seat}号" for p in tied_players)
    await _announce(
        game,
        f"请在平票候选人中重投：/投票 N（{names}）或 /弃票（{cfg.ww_vote_timeout} 秒）",
    )
    votes2 = await _collect_votes(
        game,
        cfg,
        [p.seat for p in tied_players],
        Phase.SHERIFF_REVOTE,
    )
    winner2, _ = _tally_votes(game, votes2)
    if winner2 is not None:
        await _elect_sheriff(game, winner2)
    else:
        await _announce(game, "再次平票，本局没有警长")


# ── 放逐 ──────────────────────────────────────────────────


async def _vote_phase(game: Game, cfg: Config) -> Optional[int]:
    """放逐投票（含平票 PK）；返回放逐座位或 None。"""
    candidates = [p for p in game.alive_players() if not p.idiot_revealed]
    names = "、".join(f"{p.seat}号" for p in candidates)
    await _announce(
        game,
        f"放逐投票开始：/投票 N 或 /弃票（{cfg.ww_vote_timeout} 秒）。"
        f"可投对象：{names}",
    )
    # 禁票长老：被禁票者不可参与放逐环节投票（含 PK 投票），但可发言、可被投
    ban_vote: tuple[int, ...] = ()
    if _board(game).silence_mode == "vote" and game.silenced_seat is not None:
        silenced = game.player_by_seat(game.silenced_seat)
        if silenced is not None and silenced.alive:
            ban_vote = (silenced.seat,)
    votes = await _collect_votes(
        game,
        cfg,
        [p.seat for p in candidates],
        Phase.DAY_VOTE,
        exclude_seats=ban_vote,
    )
    winner_seat, tied = _tally_votes(game, votes)
    if winner_seat is not None:
        return winner_seat
    if not tied:
        await _announce(game, "无人获得有效票数，今天平安日，无人被放逐")
        return None
    tied_names = "、".join(f"{s}号" for s in tied)
    await _announce(game, f"投票平票：{tied_names}，进入 PK 环节")
    pk_players = [p for p in candidates if p.seat in tied]
    await _speech_rotation(game, cfg, pk_players, Phase.PK_SPEECH)
    await _announce(
        game,
        f"PK 投票开始：只能在 {tied_names} 之间投票（/投票 N）"
        f"或 /弃票，PK 双方不参与本轮投票（{cfg.ww_vote_timeout} 秒）",
    )
    votes2 = await _collect_votes(
        game,
        cfg,
        tied,
        Phase.PK_VOTE,
        exclude_seats=tuple(tied) + ban_vote,
    )
    winner2, _ = _tally_votes(game, votes2)
    if winner2 is not None:
        return winner2
    await _announce(game, "PK 再次平票，今天平安日，无人被放逐")
    return None


async def _execute_exile(game: Game, cfg: Config, seat: int) -> None:
    """放逐结算：白痴翻牌 / 遗言 / 猎人开枪 / 警徽。"""
    player = game.player_by_seat(seat)
    if player is None or not player.alive:
        return
    if player.role is Role.IDIOT and not player.idiot_revealed:
        player.idiot_revealed = True
        player.can_vote = False
        await _announce(
            game,
            f"被放逐的 {player.seat}号 是白痴！"
            "白痴翻牌免死，但从此失去投票权，也不能再被投票",
        )
        return
    _kill(game, player, DeathCause.VOTED)
    await _announce(
        game,
        f"{player.seat}号 被放逐，其身份是 {player.role.value}",
    )
    try:
        await _last_words(game, cfg, player)
    except _DetonatedError:
        await _resolve_pending_day_effects(game, cfg, [player])
        raise
    await _resolve_pending_day_effects(game, cfg, [player])


# ── 白天主流程 ────────────────────────────────────────────


async def _run_day(  # noqa: C901
    game: Game,
    cfg: Config,
    night_deaths: list[PlayerState],
) -> Optional[Faction]:
    """白天完整流程；返回获胜阵营或 None；可能抛出 _DetonatedError。"""
    _enter_phase(game, Phase.DAY_ANNOUNCE)
    await _whole_ban(game, enable=False)
    await _unban_all_players(game)
    # 禁言长老：禁言/禁票情况随死讯一并公布（目标当夜死亡则省略）
    silence_note = ""
    if game.silenced_seat is not None:
        silenced = game.player_by_seat(game.silenced_seat)
        if silenced is not None and silenced.alive:
            if _board(game).silence_mode == "speech":
                silence_note = (
                    f"\n{silenced.seat}号 昨晚被禁言长老禁言，今天发言阶段无法发言"
                )
            elif _board(game).silence_mode == "vote":
                silence_note = (
                    f"\n{silenced.seat}号 昨晚被禁言长老禁票，今天放逐投票无法投票"
                )
    if night_deaths:
        names = "、".join(f"{p.seat}号" for p in night_deaths)
        await _announce(
            game,
            f"天亮了（第 {game.round_no} 天）。昨晚 {names} 倒牌{silence_note}",
        )
    else:
        await _announce(
            game,
            f"天亮了（第 {game.round_no} 天）。昨晚是平安夜，无人死亡{silence_note}",
        )
    # 清空夜间滞留的行动，防滞后指令（如夜里发的自爆）流入白天被误裁决
    game.drain_actions()
    pending = list(night_deaths)
    # 第 1 夜死者有遗言
    if game.round_no == 1:
        try:
            for dead in night_deaths:
                await _last_words(game, cfg, dead)
        except _DetonatedError:
            await _resolve_pending_day_effects(game, cfg, pending)
            raise
    # 猎人开枪与警徽移交（枪杀目标同样进入递归待处理队列）
    await _resolve_pending_day_effects(game, cfg, pending)
    winner = _check_winner(game)
    if winner is not None:
        return winner
    # 第 1 天警长竞选
    if game.round_no == 1:
        await _sheriff_campaign(game, cfg)
    # 轮流发言
    order = await _decide_speech_order(game, cfg)
    await _speech_rotation(game, cfg, order, Phase.DAY_SPEECH)
    # 放逐投票
    exile_seat = await _vote_phase(game, cfg)
    if exile_seat is not None:
        await _execute_exile(game, cfg, exile_seat)
    return _check_winner(game)


async def _handle_detonation(
    game: Game,
    cfg: Config,
    player: PlayerState,
) -> None:
    """处理狼人自爆：死亡公示并直接进入夜晚。"""
    _kill(game, player, DeathCause.SELF_DETONATION)
    await _announce(
        game,
        f"{player.seat}号 玩家自爆！其身份是 狼人。\n白天流程立即结束，进入夜晚。",
    )
    if player.is_sheriff:
        await _badge_transfer(game, cfg, player)


# ── 持久化 ────────────────────────────────────────────────


async def _persist_start(game: Game) -> None:
    """开局写库：对局行 + 玩家行。失败不影响游戏。"""
    register_replay_participants(
        game.event_log_id,
        "werewolf",
        {player.user_id: player.seat for player in game.players},
    )
    persistence = "failed"
    try:
        async with get_session() as session:
            row = WerewolfGame(
                group_id=game.group_id,
                host_user_id=game.host_user_id,
                board=game.board,
                player_count=len(game.players),
                started_at=_now_bj(),
            )
            session.add(row)
            await session.flush()
            game.game_row_id = row.id
            for p in game.players:
                session.add(
                    WerewolfPlayer(
                        game_id=row.id,
                        user_id=p.user_id,
                        seat=p.seat,
                        role=p.role.value,
                        faction=p.faction.value,
                        is_ai=p.is_ai,
                    )
                )
            await session.commit()
            persistence = "ok"
    except Exception:  # noqa: BLE001
        logger.warning(
            f"狼人杀群 {game.group_id} 开局写库失败",
            exc_info=True,
        )
    record_game_event(
        game,
        "werewolf",
        "game_started",
        phase=game.phase,
        round_no=game.round_no,
        payload={
            "board": game.board,
            "persistence": persistence,
            "player_count": len(game.players),
        },
    )


async def _persist_end(game: Game, winner: Faction) -> None:
    """终局写库：对局结果 + 玩家胜负/死因。"""
    record_game_event(
        game,
        "werewolf",
        "game_ended",
        phase=game.phase,
        round_no=game.round_no,
        payload={"round": game.round_no, "winner": winner.value},
    )
    if game.game_row_id is None:
        return
    try:
        async with get_session() as session:
            row = await session.get(WerewolfGame, game.game_row_id)
            if row is not None:
                row.ended_at = _now_bj()
                row.winner_faction = winner.value
                row.end_round = game.round_no
            for p in game.players:
                stmt = select(WerewolfPlayer).where(
                    WerewolfPlayer.game_id == game.game_row_id,
                    WerewolfPlayer.user_id == p.user_id,
                )
                prow = (await session.execute(stmt)).scalar_one_or_none()
                if prow is None:
                    continue
                if p.role is Role.HALFBLOOD and p.owner_seat is not None:
                    # 混血儿胜负随主人阵营（其 faction 恒为 good，不能直接用）
                    owner = game.player_by_seat(p.owner_seat)
                    prow.is_winner = owner is not None and owner.faction is winner
                else:
                    prow.is_winner = p.faction is winner
                prow.is_sheriff = p.was_sheriff
                prow.death_round = p.death_round
                prow.death_cause = p.death_cause.value if p.death_cause else None
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.warning(
            f"狼人杀群 {game.group_id} 终局写库失败",
            exc_info=True,
        )
# ── 引擎入口 ──────────────────────────────────────────────


async def run_game(  # noqa: C901,PLR0911,PLR0912,PLR0915
    game: Game,
) -> None:
    """引擎主任务：报名 → 发牌 → 昼夜循环 → 终局。"""
    cfg = config
    logger.info(f"狼人杀群 {game.group_id} 引擎启动（房主 {game.host_user_id}）")
    record_game_event(
        game,
        "werewolf",
        "game_created",
        phase=game.phase,
        round_no=game.round_no,
        payload={"player_count": len(game.signup_user_ids)},
    )
    try:
        # 优先使用命令层开房时注入的 Bot（即收到 /狼人杀 事件的那个连接）；
        # 仅在未注入时回退 get_bot()——多机器人在线时 get_bot() 会抛
        # ValueError，直接依赖它会让房间无声死亡
        try:
            if game.bot is None:
                game.bot = get_bot()
        except ValueError:
            # 无机器人连接：此前命令层已回复"房间已创建"，
            # 必须明确记日志，否则房间会无声消失
            logger.error(f"狼人杀群 {game.group_id} 无可用机器人连接，对局取消")
            return
        # ── 报名阶段 ──
        _enter_phase(game, Phase.SIGNUP)
        board = _board(game)
        # 有效人数区间：配置项与板子支持人数的交集。报名期间允许切板，
        # 因而初始播报和循环内裁决都必须以当下板子重新计算。
        limits = _effective_player_limits(game, cfg)
        if limits is None:
            await _announce(
                game,
                f"配置冲突：板子「{board.key}」支持 {board.counts_summary()} 人，"
                f"与当前人数配置（{cfg.ww_min_players}-{cfg.ww_max_players}）"
                "没有交集，本局流局~",
            )
            return
        eff_min, eff_max = limits
        await _announce(
            game,
            "\n".join(
                [
                    "═══ 狼人杀 · 开局报名 ═══",
                    f"板子：{board.key}（{board.roles_summary()}）",
                    f"人数：{eff_min}-{eff_max} 人"
                    "（满员自动开局，房主可发 /板子 切换）",
                    "发送 /报名 加入，/退报名 退出，/查看报名 查看名单",
                    f"报名倒计时 {cfg.ww_signup_timeout} 秒",
                ]
            ),
        )
        deadline = _loop_time() + cfg.ww_signup_timeout
        # 提醒阈值降序：最大的未触发阈值即下一个将到达的提醒点
        warn_points = sorted(
            {cfg.ww_signup_warn_remain, cfg.ww_signup_warn_remain_final},
            reverse=True,
        )
        fired: set[int] = set()
        while True:
            board = _board(game)
            limits = _effective_player_limits(game, cfg)
            if limits is None:
                await _announce(
                    game,
                    f"配置冲突：板子「{board.key}」支持 {board.counts_summary()} 人，"
                    f"与当前人数配置（{cfg.ww_min_players}-{cfg.ww_max_players}）"
                    "没有交集，本局流局~",
                )
                return
            eff_min, eff_max = limits
            if len(game.signup_user_ids) >= eff_max:
                break
            remaining = deadline - _loop_time()
            if remaining <= 0:
                break
            for point in warn_points:
                if point not in fired and remaining <= point:
                    fired.add(point)
                    await _announce(
                        game,
                        f"报名还剩 {point} 秒，"
                        f"当前 {len(game.signup_user_ids)} 人已报名"
                        f"（至少 {eff_min} 人开局）",
                    )
            step = min(remaining, 1.0)
            next_point = next((p for p in warn_points if p not in fired), None)
            if next_point is not None:
                step = min(step, max(remaining - next_point, 0.5))
            action = await _get_action(game, step)
            if action is not None and action.kind is ActionKind.START_GAME:
                # /板子 与 /开始游戏 可能紧邻到达；收到开始请求时再次读取
                # 当前板子的门槛，避免按开房时缓存的旧门槛误开或误拒。
                board = _board(game)
                limits = _effective_player_limits(game, cfg)
                if limits is None:
                    await _announce(
                        game,
                        f"配置冲突：板子「{board.key}」支持 "
                        f"{board.counts_summary()} 人，与当前人数配置"
                        f"（{cfg.ww_min_players}-{cfg.ww_max_players}）"
                        "没有交集，本局流局~",
                    )
                    return
                eff_min, eff_max = limits
                if len(game.signup_user_ids) >= eff_min:
                    break
                await _announce(
                    game,
                    f"当前仅 {len(game.signup_user_ids)} 人报名，"
                    f"至少需要 {eff_min} 人才能开局~",
                )
        board = _board(game)
        limits = _effective_player_limits(game, cfg)
        if limits is None:
            await _announce(
                game,
                f"配置冲突：板子「{board.key}」支持 {board.counts_summary()} 人，"
                f"与当前人数配置（{cfg.ww_min_players}-{cfg.ww_max_players}）"
                "没有交集，本局流局~",
            )
            return
        eff_min, _ = limits
        if len(game.signup_user_ids) < eff_min:
            await _announce(
                game,
                f"报名人数不足 {eff_min} 人，本局流局~",
            )
            return
        # ── 发牌 ──
        # 报名阶段房主可能 /板子 切换过，这里按当前板子重新校验
        board = _board(game)
        if len(game.signup_user_ids) not in board.counts:
            # 开局人数不在板子支持范围（改配置或切了 12 人板子所致），
            # 提前拦截，避免 build_role_deck 抛 KeyError 炸掉引擎任务
            supported = board.counts_summary()
            logger.error(
                f"狼人杀群 {game.group_id} 开局人数 "
                f"{len(game.signup_user_ids)} 无匹配角色配置"
                f"（板子 {board.key} 支持 {supported} 人），流局"
            )
            await _announce(
                game,
                f"当前人数（{len(game.signup_user_ids)}）没有匹配的角色配置"
                f"（板子「{board.key}」支持 {supported} 人），本局流局~",
            )
            return
        # 发牌前锁定报名名单、房主和板子；后续命令仅允许进入行动队列。
        _enter_phase(game, Phase.DEALING)
        deck = build_role_deck(game.board, len(game.signup_user_ids))
        # 身份请求先按份数结算，如愿者从牌堆取走对应角色，
        # 剩余角色洗牌后随机分给其余座位
        wished = _resolve_role_requests(game, deck)
        remaining = list(deck)
        for role in wished.values():
            remaining.remove(role)
        random.shuffle(remaining)
        game.players = []
        for idx, uid in enumerate(game.signup_user_ids):
            role = wished.get(uid) or remaining.pop()
            game.players.append(
                PlayerState(
                    user_id=uid,
                    seat=idx + 1,
                    role=role,
                    faction=ROLE_FACTION[role],
                    is_ai=is_ai_uid(uid),
                    display_name=game.ai_names.get(uid),
                )
            )
        deal_desc = "、".join(
            f"{p.seat}号={p.role.value}"
            + (f"(AI {p.display_name or '?'})" if p.is_ai else f"({p.user_id})")
            for p in game.players
        )
        logger.info(f"狼人杀群 {game.group_id} 发牌：{deal_desc}")
        if wished:
            wish_desc = "、".join(f"{uid}={role.value}" for uid, role in wished.items())
            logger.info(
                f"狼人杀群 {game.group_id} 选身份如愿 {len(wished)} 人：{wish_desc}"
            )
        # 身份卡私聊之前启动 AI 驱动（AI 座位身份由驱动的 system 提示
        # 承载，卡片 DM 不再记入其私聊上下文，见 ai_player.on_dm）
        ai_player.start_driver(game)
        # 座位名单：全体玩家收到相同一份，不标注 AI（保持伪装）
        roster = [(p.seat, display_name_of(game, p.user_id)) for p in game.players]
        role_card_failures: list[PlayerState] = []
        for p in game.players:
            delivered = await _dm(
                game,
                p,
                build_role_card(
                    p.seat,
                    p.role,
                    len(game.players),
                    silence_mode=board.silence_mode,
                    roster=roster,
                ),
            )
            if not delivered:
                role_card_failures.append(p)
        if game.bot is not None and not await api.is_bot_admin(
            game.bot,
            game.group_id,
        ):
            await _announce(
                game,
                "提示：机器人不是本群管理员，禁言功能不可用，游戏照常进行",
            )
        await _persist_start(game)
        if role_card_failures:
            failed_seats = _seat_list(role_card_failures)
            await _announce(
                game,
                f"身份卡投递：成功 {len(game.players) - len(role_card_failures)} 人，"
                f"失败 {len(role_card_failures)} 人（{failed_seats}）。\n"
                "失败玩家请先加机器人为好友，再私聊发送 /身份 重取。"
                "游戏即将开始——",
            )
        else:
            await _announce(
                game,
                f"身份已私聊下发（共 {len(game.players)} 人）。游戏即将开始——",
            )
        # ── 昼夜循环 ──
        winner: Optional[Faction] = None
        while winner is None:
            game.round_no += 1
            night_deaths = await _run_night(game, cfg)
            try:
                winner = await _run_day(game, cfg, night_deaths)
            except _DetonatedError as detonated:
                await _handle_detonation(game, cfg, detonated.player)
                winner = _check_winner(game)
            except _DuelNightError:
                winner = _check_winner(game)
            except _ConcludedError as concluded:
                winner = concluded.winner
        await _finish(game, winner)
    except asyncio.CancelledError:
        # 常规取消路径（空房解散 / /结束游戏）由命令层自行播报，
        # 并已把阶段置为 ENDED；仅对意外取消兜底播报
        if game.phase not in (Phase.SIGNUP, Phase.ENDED):
            with contextlib.suppress(Exception):
                await _announce(game, "对局已被强制结束")
        logger.info(f"狼人杀群 {game.group_id} 引擎任务被取消")
        raise
    except Exception:  # noqa: BLE001
        logger.exception(f"狼人杀群 {game.group_id} 引擎异常")
        with contextlib.suppress(Exception):
            await _announce(game, "游戏引擎发生异常，本局已结束")
    finally:
        _enter_phase(game, Phase.ENDED)
        await ai_player.stop_driver(game)
        user_ids = [p.user_id for p in game.players] or list(game.signup_user_ids)
        # AI 合成 ID 无群成员，禁言恢复只对真人调用
        user_ids = [uid for uid in user_ids if not is_ai_uid(uid)]
        if game.bot is not None:
            await api.cleanup_group(
                game.bot,
                game.group_id,
                user_ids,
            )
        discard_game(game)
        logger.info(f"狼人杀群 {game.group_id} 引擎任务结束")


async def _finish(game: Game, winner: Faction) -> None:
    """终局播报 + 写库。"""
    _enter_phase(game, Phase.ENDED)
    winner_name = "狼人阵营" if winner is Faction.WOLF else "好人阵营"
    logger.info(
        f"狼人杀群 {game.group_id} 对局结束："
        f"{winner_name}获胜（第 {game.round_no} 回合）"
    )
    lines = [
        "═══ 游戏结束 ═══",
        f"获胜阵营：{winner_name}",
        f"总回合数：{game.round_no}",
        "─── 身份公示 ───",
    ]
    for p in sorted(game.players, key=lambda x: x.seat):
        status = "存活" if p.alive else f"第 {p.death_round} 回合死亡"
        sheriff_mark = "（曾任警长）" if p.was_sheriff else ""
        owner_mark = (
            f"（主人：{p.owner_seat}号）"
            if p.role is Role.HALFBLOOD and p.owner_seat is not None
            else ""
        )
        lines.append(f"{p.seat}号：{p.role.value} {status}{sheriff_mark}{owner_mark}")
    lines.append("──────────────")
    lines.append(f"回放编号：{game.event_log_id}")
    lines.append("感谢大家的游玩~ 发送 /战绩 查看胜率")
    await _announce(game, "\n".join(lines))
    await _persist_end(game, winner)
