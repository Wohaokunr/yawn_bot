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

from . import ai_player, api
from .config import Config
from .models import WerewolfGame, WerewolfPlayer
from .roles import (
    GOD_ROLES,
    ROLE_COMPOSITION,
    ROLE_FACTION,
    DeathCause,
    Faction,
    Role,
    build_role_card,
    build_role_deck,
)
from .state import (
    SELF_DETONATE_PHASES,
    Action,
    ActionKind,
    Game,
    Phase,
    PlayerState,
    discard_game,
    is_ai_uid,
)

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import Message

config = get_plugin_config(Config)

_BJ_TZ = timezone(timedelta(hours=8))

# 警长平票终辩的固定时长（秒）
_FINAL_SPEECH_TIMEOUT = 60


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


# ── 基础辅助 ──────────────────────────────────────────────


@dataclass
class _Timer:
    """阶段计时器：截止时间 + 剩余提醒。"""

    deadline: float
    warn_remain: Optional[float] = None
    warn_text: str = ""
    warned: bool = False

    def remaining(self) -> float:
        """距离截止的剩余秒数。"""
        return self.deadline - _loop_time()

    async def next_action(self, game: Game) -> Optional[Action]:
        """等待至多一个行动；到提醒点时群播报一次；超时返回 None。"""
        left = self.remaining()
        if left <= 0:
            return None
        if (
            self.warn_remain is not None
            and not self.warned
            and left <= self.warn_remain
        ):
            self.warned = True
            await _announce(game, self.warn_text)
        step = left
        if self.warn_remain is not None and not self.warned:
            step = min(left, max(left - self.warn_remain, 0.5))
        return await _get_action(game, step)


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
    return action


def _enter_phase(game: Game, phase: Phase) -> None:
    """切换阶段并记日志；同阶段重复赋值不重复记录。"""
    if game.phase is phase:
        return
    logger.info(
        f"狼人杀群 {game.group_id} 进入阶段 {phase.value}（第 {game.round_no} 回合）"
    )
    game.phase = phase


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
    """解禁所有玩家。"""
    for p in game.players:
        await _unban(game, p.user_id)


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
    """屠边规则判胜：无狼人→好人胜；神职或村民全灭→狼人胜。"""
    alive = game.alive_players()
    if not any(p.faction is Faction.WOLF for p in alive):
        return Faction.GOOD
    if not any(p.role in GOD_ROLES for p in alive):
        return Faction.WOLF
    if not any(p.role is Role.VILLAGER for p in alive):
        return Faction.WOLF
    return None


# ── 夜晚阶段 ──────────────────────────────────────────────


async def _phase_wolves(  # noqa: C901,PLR0912
    game: Game,
    cfg: Config,
) -> Optional[int]:
    """狼人阶段：私聊征刀、多数决；返回刀口座位（None=空刀）。"""
    _enter_phase(game, Phase.NIGHT_WOLVES)
    wolves = game.alive_players_of_role(Role.WEREWOLF)
    if not wolves:
        return None
    names = "、".join(f"{w.seat}号" for w in wolves)
    for w in wolves:
        await _dm(
            game,
            w,
            f"狼人请睁眼，本局狼人共 {len(wolves)} 名：{names}。\n"
            "可先讨论：回复 说XXX（如 说刀5），我会转发给其他狼人。\n"
            "统一目标后回复 刀N（如 刀3），超时未刀视为空刀。",
        )
    votes: dict[int, int] = {}
    submitted: dict[int, int] = {}  # 狼人QQ -> 已确定的刀口座位
    timer = _Timer(
        _loop_time() + cfg.ww_wolf_timeout,
        cfg.ww_night_warn_remain,
        f"狼人行动还剩 {cfg.ww_night_warn_remain} 秒",
    )
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
        if action.kind is not ActionKind.KILL:
            continue
        actor = game.player_by_user(action.actor_user_id)
        if actor is None or not actor.alive or actor.role is not Role.WEREWOLF:
            continue
        if actor.user_id in submitted:
            await _dm(
                game,
                actor,
                f"你的刀口已确定为 {submitted[actor.user_id]}号，本夜不可更改",
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
                f"当前刀型：{tally}（已提交 {len(submitted)}/{len(wolves)}）",
            )
    if not votes:
        logger.info(f"狼人杀群 {game.group_id} 狼人空刀（无人提交刀口）")
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
    timer = _Timer(
        _loop_time() + cfg.ww_night_timeout,
        cfg.ww_night_warn_remain,
        f"女巫行动还剩 {cfg.ww_night_warn_remain} 秒",
    )
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
    return False, None


async def _phase_seer(game: Game, cfg: Config) -> None:
    """预言家阶段：查验结果私聊反馈。"""
    _enter_phase(game, Phase.NIGHT_SEER)
    seers = game.alive_players_of_role(Role.SEER)
    if not seers:
        return
    seer = seers[0]
    await _dm(
        game,
        seer,
        "预言家请睁眼。回复 查验N（如 查验5）查验一名玩家的身份。",
    )
    timer = _Timer(
        _loop_time() + cfg.ww_night_timeout,
        cfg.ww_night_warn_remain,
        f"预言家行动还剩 {cfg.ww_night_warn_remain} 秒",
    )
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


async def _run_night(game: Game, cfg: Config) -> list[PlayerState]:
    """完整夜晚流程，返回当夜死者列表。"""
    await _announce(game, f"天黑请闭眼（第 {game.round_no} 夜）")
    await _whole_ban(game, enable=True)
    game.drain_actions()
    kill_seat = await _phase_wolves(game, cfg)
    saved, poison_seat = await _phase_witch(game, cfg, kill_seat)
    await _phase_seer(game, cfg)
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
        "你已死亡，可以开枪：回复 开枪N 带走一名玩家，或回复 不开枪。",
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
    await _dm(
        game,
        sheriff,
        "你已死亡，请决定警徽流向：回复 移交警徽N 或 撕警徽",
    )
    await _ban_living_except(game)
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
    else:
        await _announce(
            game,
            f"{sheriff.seat}号 撕掉了警徽，本局不再有警长",
        )
    await _ban(game, sheriff.user_id, 1800)


# ── 发言与投票 ────────────────────────────────────────────


async def _speech_rotation(  # noqa: C901,PLR0912,PLR0913
    game: Game,
    cfg: Config,
    order: list[PlayerState],
    phase: Phase,
    *,
    allow_withdraw: bool = False,
    speech_timeout: Optional[int] = None,
) -> None:
    """单人禁言轮换发言；可被狼人自爆打断。"""
    _enter_phase(game, phase)
    timeout = speech_timeout or cfg.ww_speech_timeout
    ban_total = timeout * max(len(order), 1) + 600
    for p in game.alive_players():
        await _ban(game, p.user_id, ban_total)
    for p in game.players:
        if not p.alive:
            await _ban(game, p.user_id, ban_total)
    for speaker in order:
        if not speaker.alive:
            continue
        if allow_withdraw and not speaker.sheriff_candidate:
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


async def _collect_votes(
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
            or actor.user_id in votes
        ):
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
    return votes


def _tally_votes(
    game: Game,
    votes: dict[int, Optional[int]],
) -> tuple[Optional[int], list[int]]:
    """统计票数（警长 1.5 票）：返回 (唯一最高者, 平票列表)。"""
    counts: dict[int, float] = {}
    sheriff = game.sheriff()
    for voter_uid, seat in votes.items():
        if seat is None:
            continue
        weight = 1.5 if sheriff and voter_uid == sheriff.user_id else 1.0
        counts[seat] = counts.get(seat, 0) + weight
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
        Phase.SHERIFF_REVOTE,
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
    votes = await _collect_votes(
        game,
        cfg,
        [p.seat for p in candidates],
        Phase.DAY_VOTE,
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
        exclude_seats=tuple(tied),
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
    await _last_words(game, cfg, player)
    if player.role is Role.HUNTER:
        shot = await _hunter_prompt(game, cfg, player)
        if shot is not None:
            await _resolve_hunter_shot(game, cfg, shot, player.seat)
    if player.is_sheriff:
        await _badge_transfer(game, cfg, player)


# ── 白天主流程 ────────────────────────────────────────────


async def _run_day(  # noqa: C901,PLR0912
    game: Game,
    cfg: Config,
    night_deaths: list[PlayerState],
) -> Optional[Faction]:
    """白天完整流程；返回获胜阵营或 None；可能抛出 _DetonatedError。"""
    _enter_phase(game, Phase.DAY_ANNOUNCE)
    await _whole_ban(game, enable=False)
    await _unban_all_players(game)
    if night_deaths:
        names = "、".join(f"{p.seat}号" for p in night_deaths)
        await _announce(
            game,
            f"天亮了（第 {game.round_no} 天）。昨晚 {names} 倒牌",
        )
    else:
        await _announce(
            game,
            f"天亮了（第 {game.round_no} 天）。昨晚是平安夜，无人死亡",
        )
    # 清空夜间滞留的行动，防滞后指令（如夜里发的自爆）流入白天被误裁决
    game.drain_actions()
    pending = list(night_deaths)
    # 第 1 夜死者有遗言
    if game.round_no == 1:
        for dead in night_deaths:
            await _last_words(game, cfg, dead)
    # 猎人开枪（开枪致死同样进入待处理队列）
    processed: set[int] = set()
    while True:
        hunter = next(
            (
                p
                for p in pending
                if p.role is Role.HUNTER
                and p.death_cause is not DeathCause.WITCH_POISON
                and p.user_id not in processed
            ),
            None,
        )
        if hunter is None:
            break
        processed.add(hunter.user_id)
        shot = await _hunter_prompt(game, cfg, hunter)
        if shot is not None:
            await _resolve_hunter_shot(game, cfg, shot, hunter.seat)
            victim = game.player_by_seat(shot)
            if victim is not None:
                pending.append(victim)
    # 警徽移交（所有非毒死的警长，含被枪杀者）
    for dead in pending:
        if not dead.is_sheriff:
            continue
        if dead.death_cause is DeathCause.WITCH_POISON:
            # 毒死无法移交警徽，警徽直接失效，避免死人长期占据 sheriff()
            dead.is_sheriff = False
            await _announce(
                game,
                f"警长 {dead.seat}号 被毒杀，警徽随其一同失效",
            )
        else:
            await _badge_transfer(game, cfg, dead)
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


async def _handle_detonation(game: Game, player: PlayerState) -> None:
    """处理狼人自爆：死亡公示并直接进入夜晚。"""
    _kill(game, player, DeathCause.SELF_DETONATION)
    await _announce(
        game,
        f"{player.seat}号 玩家自爆！其身份是 狼人。\n白天流程立即结束，进入夜晚。",
    )


# ── 持久化 ────────────────────────────────────────────────


async def _persist_start(game: Game) -> None:
    """开局写库：对局行 + 玩家行。失败不影响游戏。"""
    try:
        async with get_session() as session:
            row = WerewolfGame(
                group_id=game.group_id,
                host_user_id=game.host_user_id,
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
    except Exception:  # noqa: BLE001
        logger.warning(
            f"狼人杀群 {game.group_id} 开局写库失败",
            exc_info=True,
        )


async def _persist_end(game: Game, winner: Faction) -> None:
    """终局写库：对局结果 + 玩家胜负/死因。"""
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


async def run_game(  # noqa: C901,PLR0912,PLR0915
    game: Game,
) -> None:
    """引擎主任务：报名 → 发牌 → 昼夜循环 → 终局。"""
    cfg = config
    logger.info(f"狼人杀群 {game.group_id} 引擎启动（房主 {game.host_user_id}）")
    try:
        try:
            game.bot = get_bot()
        except ValueError:
            # 无机器人连接：此前命令层已回复"房间已创建"，
            # 必须明确记日志，否则房间会无声消失
            logger.error(f"狼人杀群 {game.group_id} 无可用机器人连接，对局取消")
            return
        # ── 报名阶段 ──
        _enter_phase(game, Phase.SIGNUP)
        await _announce(
            game,
            "\n".join(
                [
                    "═══ 狼人杀 · 开局报名 ═══",
                    "板子：预女猎白（狼人/预言家/女巫/猎人/白痴/村民）",
                    f"人数：{cfg.ww_min_players}-{cfg.ww_max_players} 人"
                    "（满员自动开局）",
                    "发送 /报名 加入，/退报名 退出，/查看报名 查看名单",
                    f"报名倒计时 {cfg.ww_signup_timeout} 秒",
                ]
            ),
        )
        deadline = _loop_time() + cfg.ww_signup_timeout
        warned = False
        while len(game.signup_user_ids) < cfg.ww_max_players:
            remaining = deadline - _loop_time()
            if remaining <= 0:
                break
            if not warned and remaining <= cfg.ww_signup_warn_remain:
                warned = True
                await _announce(
                    game,
                    f"报名还剩 {cfg.ww_signup_warn_remain} 秒，"
                    f"当前 {len(game.signup_user_ids)} 人已报名"
                    f"（至少 {cfg.ww_min_players} 人开局）",
                )
            step = min(remaining, 1.0)
            if not warned:
                step = min(
                    step,
                    max(remaining - cfg.ww_signup_warn_remain, 0.5),
                )
            action = await _get_action(game, step)
            if action is not None and action.kind is ActionKind.START_GAME:
                if len(game.signup_user_ids) >= cfg.ww_min_players:
                    break
                await _announce(
                    game,
                    f"当前仅 {len(game.signup_user_ids)} 人报名，"
                    f"至少需要 {cfg.ww_min_players} 人才能开局~",
                )
        if len(game.signup_user_ids) < cfg.ww_min_players:
            await _announce(
                game,
                f"报名人数不足 {cfg.ww_min_players} 人，本局流局~",
            )
            return
        # ── 发牌 ──
        if len(game.signup_user_ids) not in ROLE_COMPOSITION:
            # 配置的人数区间超出板子支持范围（改 WW_MIN/MAX_PLAYERS 所致），
            # 提前拦截，避免 build_role_deck 抛 KeyError 炸掉引擎任务
            supported = "、".join(str(n) for n in sorted(ROLE_COMPOSITION))
            logger.error(
                f"狼人杀群 {game.group_id} 开局人数 "
                f"{len(game.signup_user_ids)} 无匹配角色配置"
                f"（支持 {supported} 人），流局"
            )
            await _announce(
                game,
                f"当前人数（{len(game.signup_user_ids)}）没有匹配的角色配置"
                f"（支持 {supported} 人），本局流局~",
            )
            return
        deck = build_role_deck(len(game.signup_user_ids))
        random.shuffle(deck)
        game.players = [
            PlayerState(
                user_id=uid,
                seat=idx + 1,
                role=role,
                faction=ROLE_FACTION[role],
                is_ai=is_ai_uid(uid),
                display_name=game.ai_names.get(uid),
            )
            for idx, (uid, role) in enumerate(zip(game.signup_user_ids, deck))
        ]
        deal_desc = "、".join(
            f"{p.seat}号={p.role.value}"
            + (f"(AI {p.display_name or '?'})" if p.is_ai else f"({p.user_id})")
            for p in game.players
        )
        logger.info(f"狼人杀群 {game.group_id} 发牌：{deal_desc}")
        # 身份卡私聊之前启动 AI 驱动，使卡片文本落入 AI 座位上下文
        ai_player.start_driver(game)
        for p in game.players:
            await _dm(
                game,
                p,
                build_role_card(p.seat, p.role, len(game.players)),
            )
        if game.bot is not None and not await api.is_bot_admin(
            game.bot,
            game.group_id,
        ):
            await _announce(
                game,
                "提示：机器人不是本群管理员，禁言功能不可用，游戏照常进行",
            )
        await _persist_start(game)
        await _announce(
            game,
            f"身份已私聊下发（共 {len(game.players)} 人）。"
            "收不到私聊的玩家请加机器人为好友。游戏即将开始——",
        )
        # ── 昼夜循环 ──
        winner: Optional[Faction] = None
        while winner is None:
            game.round_no += 1
            night_deaths = await _run_night(game, cfg)
            try:
                winner = await _run_day(game, cfg, night_deaths)
            except _DetonatedError as detonated:
                await _handle_detonation(game, detonated.player)
                winner = _check_winner(game)
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
        lines.append(f"{p.seat}号：{p.role.value} {status}{sheriff_mark}")
    lines.append("──────────────")
    lines.append("感谢大家的游玩~ 发送 /战绩 查看胜率")
    await _announce(game, "\n".join(lines))
    await _persist_end(game, winner)
