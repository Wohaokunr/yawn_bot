# ruff: noqa: PLR0911,PLR2004,RUF001,FBT003,C901,TC001
"""冷场暖场的纯策略：没人接话就收手、指数退避、每段冷场只掷一次骰子。

旧逻辑只看“群里最后一条消息”的时间，而 Bot 自己的暖场消息也会刷新这个时间，
于是没人说话的群会每隔一个冷场阈值就被 Bot 自言自语一次，直到每日上限。
这里把判定改成以“真人最后一次说话”为锚点，并统计这段沉默里 Bot 已经主动说了
几条没人接的话：

- 连续未获回应的主动消息达到上限（随人设活跃度 1~2 条）后，暖场暂停到真人开口；
- 每多一条没人接的消息，下次暖场需要的安静时间翻倍；
- 模型在暖场场景回答 ``close`` 表示“这段冷场不该打扰”，同样暂停到真人开口；
  ``wait`` / 生成失败按连续次数指数退避，不再每 4 分钟抽一次卡；
- 一段冷场只掷一次骰子（种子 = 群号 + 真人最后发言时间 + 已说条数），配置里的
  暖场概率因此真正表示“这次冷场有多大概率暖场”，而不是“每分钟的概率”；
- 深夜、群里两天以上没有真人说话、当日暖场次数已满时都不暖场。

调度与数据库读写在 ``activity.warmup_state`` / ``proactive.py``；本模块不做 I/O。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .persona import PersonaBehavior

# 冷场判定阈值的下限：配置成 0 时旧逻辑会每分钟都进入暖场分支。
WARMUP_MIN_IDLE_MINUTES = 5
# 一段沉默里 Bot 主动说了这么多条都没人接，就停到真人开口为止。
WARMUP_MAX_UNANSWERED = 2
# 低活跃度人设只尝试一次。
WARMUP_MAX_UNANSWERED_RESERVED = 1
# 每多一条没人接的消息，下一次暖场要求的安静时长乘以该系数。
WARMUP_BACKOFF_FACTOR = 2
WARMUP_MAX_BACKOFF_MINUTES = 6 * 60
# 模型说 wait 或生成失败后的退避上限。
WARMUP_SKIP_BACKOFF_MAX_MINUTES = 3 * 60
# 真人沉默超过这么久视为“群已经不聊了”，不再暖场。
WARMUP_DEAD_GROUP_HOURS = 48
# 真人沉默越久，暖场成功率越低；这两档之后概率依次减半。
WARMUP_DECAY_HOURS = (6, 24)
# 北京时间 [start, end) 小时内不暖场（深夜/清晨没人想被叫醒）。
WARMUP_QUIET_HOURS = (1, 8)
# 每个群每天最多暖场几次；与 daily_limit 取较小值。
WARMUP_DAILY_CAP = 4
# 单次暖场概率的硬上限。
WARMUP_PROBABILITY_CAP = 0.6
# 已有没人接的消息时，每条把概率再乘以该系数。
WARMUP_STREAK_PROBABILITY_SCALE = 0.5
# 近似撞重阈值（字符二元组 Jaccard）。
NEAR_DUPLICATE_THRESHOLD = 0.6

WARMUP_SKIP_ACTIONS = frozenset({"wait", "error", "send_failed"})
WARMUP_SENT_RESULTS = frozenset({"speak", "delivery_unknown"})


@dataclass(frozen=True, slots=True)
class WarmupState:
    """一段沉默的事实快照；全部由消息与审计推导，进程重启不会丢失。"""

    last_human_at: datetime | None
    last_bot_at: datetime | None = None
    # 真人最后一次说话之后，Bot 主动发出（含投递未知）的消息数。
    unanswered: int = 0
    # 真人最后一次说话之后，暖场被模型 wait / 生成失败 / 发送失败的次数。
    skips: int = 0
    last_skip_at: datetime | None = None
    # 真人最后一次说话之后，模型是否在暖场场景说过 close。
    closed: bool = False
    warmups_today: int = 0
    recent_topics: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        def iso(value: datetime | None) -> str | None:
            return value.isoformat() if value is not None else None

        return {
            "lastHumanAt": iso(self.last_human_at),
            "lastBotAt": iso(self.last_bot_at),
            "unanswered": self.unanswered,
            "skips": self.skips,
            "lastSkipAt": iso(self.last_skip_at),
            "closed": self.closed,
            "warmupsToday": self.warmups_today,
            "recentTopics": list(self.recent_topics),
        }


@dataclass(frozen=True, slots=True)
class WarmupGate:
    """暖场门槛结论；``allowed=False`` 时 ``code`` 说明原因。"""

    allowed: bool
    code: str
    reason: str
    idle_minutes: float = 0.0
    required_idle_minutes: float = 0.0
    max_unanswered: int = WARMUP_MAX_UNANSWERED
    next_eligible_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "code": self.code,
            "reason": self.reason,
            "idleMinutes": round(self.idle_minutes, 1),
            "requiredIdleMinutes": round(self.required_idle_minutes, 1),
            "maxUnanswered": self.max_unanswered,
            "nextEligibleAt": (
                self.next_eligible_at.isoformat()
                if self.next_eligible_at is not None
                else None
            ),
        }


def effective_idle_threshold_minutes(config: Any) -> int:
    """冷场阈值，至少 ``WARMUP_MIN_IDLE_MINUTES`` 分钟。"""

    try:
        raw = int(getattr(config, "idle_threshold_minutes", 15) or 0)
    except (TypeError, ValueError):
        raw = 15
    return max(raw, WARMUP_MIN_IDLE_MINUTES)


def max_unanswered_for(behavior: PersonaBehavior | None) -> int:
    """低活跃度人设只尝试一次，其余最多两次。"""

    if behavior is not None and behavior.sociability <= 1:
        return WARMUP_MAX_UNANSWERED_RESERVED
    return WARMUP_MAX_UNANSWERED


def warmup_daily_cap(config: Any) -> int:
    try:
        daily_limit = int(getattr(config, "daily_limit", WARMUP_DAILY_CAP))
    except (TypeError, ValueError):
        daily_limit = WARMUP_DAILY_CAP
    return max(min(WARMUP_DAILY_CAP, daily_limit), 0)


def required_idle_minutes(config: Any, unanswered: int) -> float:
    """已有 ``unanswered`` 条没人接时，距 Bot 上一条消息还需安静多久。"""

    threshold = effective_idle_threshold_minutes(config)
    scaled = threshold * (WARMUP_BACKOFF_FACTOR ** max(unanswered, 0))
    return float(min(scaled, max(WARMUP_MAX_BACKOFF_MINUTES, threshold)))


def skip_retry_minutes(config: Any, skips: int) -> float:
    """暖场被跳过（wait/失败）``skips`` 次后，下次重试至少再等多久。"""

    if skips <= 0:
        return 0.0
    threshold = effective_idle_threshold_minutes(config)
    scaled = threshold * (WARMUP_BACKOFF_FACTOR ** (skips - 1))
    return float(min(scaled, max(WARMUP_SKIP_BACKOFF_MAX_MINUTES, threshold)))


def in_quiet_hours(now: datetime) -> bool:
    start, end = WARMUP_QUIET_HOURS
    return start <= now.hour < end


def _quiet_hours_end(now: datetime) -> datetime:
    return now.replace(hour=WARMUP_QUIET_HOURS[1], minute=0, second=0, microsecond=0)


def evaluate_warmup_gate(
    config: Any,
    state: WarmupState,
    now: datetime,
    *,
    behavior: PersonaBehavior | None = None,
) -> WarmupGate:
    """在骰子之前判断这段冷场还能不能暖场；只读事实，不产生副作用。"""

    max_unanswered = max_unanswered_for(behavior)
    if state.last_human_at is None:
        return WarmupGate(
            False,
            "no_human",
            "保留期内没有真人发言，暖场没有对象",
            max_unanswered=max_unanswered,
        )
    idle_minutes = max((now - state.last_human_at).total_seconds() / 60, 0.0)
    if idle_minutes >= WARMUP_DEAD_GROUP_HOURS * 60:
        return WarmupGate(
            False,
            "dead_group",
            f"真人已沉默超过 {WARMUP_DEAD_GROUP_HOURS} 小时，不再暖场",
            idle_minutes=idle_minutes,
            max_unanswered=max_unanswered,
        )
    if state.closed:
        return WarmupGate(
            False,
            "closed",
            "模型判定这段冷场不该打扰，等真人开口后再说",
            idle_minutes=idle_minutes,
            max_unanswered=max_unanswered,
        )
    if state.unanswered >= max_unanswered:
        return WarmupGate(
            False,
            "unanswered_cap",
            f"已连续主动说了 {state.unanswered} 条没人接，等真人开口后再说",
            idle_minutes=idle_minutes,
            max_unanswered=max_unanswered,
        )
    cap = warmup_daily_cap(config)
    if state.warmups_today >= cap:
        return WarmupGate(
            False,
            "daily_cap",
            f"今日暖场已达 {state.warmups_today}/{cap} 次",
            idle_minutes=idle_minutes,
            max_unanswered=max_unanswered,
        )
    if in_quiet_hours(now):
        return WarmupGate(
            False,
            "quiet_hours",
            f"{WARMUP_QUIET_HOURS[0]}:00–{WARMUP_QUIET_HOURS[1]}:00 不暖场",
            idle_minutes=idle_minutes,
            max_unanswered=max_unanswered,
            next_eligible_at=_quiet_hours_end(now),
        )
    required = required_idle_minutes(config, state.unanswered)
    if idle_minutes < required:
        return WarmupGate(
            False,
            "human_idle",
            f"真人沉默 {idle_minutes:.0f} 分钟，未到 {required:.0f} 分钟",
            idle_minutes=idle_minutes,
            required_idle_minutes=required,
            max_unanswered=max_unanswered,
            next_eligible_at=state.last_human_at + timedelta(minutes=required),
        )
    if state.last_bot_at is not None and state.last_bot_at > state.last_human_at:
        bot_idle = max((now - state.last_bot_at).total_seconds() / 60, 0.0)
        if bot_idle < required:
            return WarmupGate(
                False,
                "backoff",
                (
                    f"已有 {state.unanswered} 条没人接，距 Bot 上一条 "
                    f"{bot_idle:.0f} 分钟，退避到 {required:.0f} 分钟"
                ),
                idle_minutes=idle_minutes,
                required_idle_minutes=required,
                max_unanswered=max_unanswered,
                next_eligible_at=state.last_bot_at + timedelta(minutes=required),
            )
    retry = skip_retry_minutes(config, state.skips)
    if retry and state.last_skip_at is not None:
        retry_at = state.last_skip_at + timedelta(minutes=retry)
        if now < retry_at:
            return WarmupGate(
                False,
                "skip_backoff",
                f"本段冷场已跳过 {state.skips} 次，退避 {retry:.0f} 分钟",
                idle_minutes=idle_minutes,
                required_idle_minutes=required,
                max_unanswered=max_unanswered,
                next_eligible_at=retry_at,
            )
    return WarmupGate(
        True,
        "ok",
        "冷场暖场门槛通过",
        idle_minutes=idle_minutes,
        required_idle_minutes=required,
        max_unanswered=max_unanswered,
    )


def silence_decay(idle_minutes: float) -> float:
    """真人沉默越久，越不像“暂时冷场”，而像“大家都去忙了”。"""

    scale = 1.0
    for hours in WARMUP_DECAY_HOURS:
        if idle_minutes >= hours * 60:
            scale *= 0.5
    return scale


def streak_scale(unanswered: int) -> float:
    return WARMUP_STREAK_PROBABILITY_SCALE ** max(unanswered, 0)


def episode_roll(group_id: int, anchor: datetime | None, unanswered: int) -> float:
    """同一段冷场（同一真人锚点、同一已说条数）每分钟得到同一个骰子点数。"""

    seed = f"{int(group_id)}|{anchor.isoformat() if anchor else '-'}|{unanswered}"
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def char_bigrams(text: str) -> set[str]:
    compact = "".join(ch for ch in text.casefold() if ch.isalnum())
    if len(compact) < 2:
        return {compact} if compact else set()
    return {compact[i : i + 2] for i in range(len(compact) - 1)}


def near_duplicate_of(
    text: str,
    previous: list[str] | tuple[str, ...],
    *,
    threshold: float = NEAR_DUPLICATE_THRESHOLD,
) -> str | None:
    """返回与 ``text`` 近似重复的旧句子；只换了标点/语气词的复读也能拦住。"""

    current = char_bigrams(text)
    if not current:
        return None
    for line in previous:
        other = char_bigrams(line)
        if not other:
            continue
        overlap = len(current & other) / len(current | other)
        if overlap >= threshold:
            return line
    return None


def warmup_prompt_context(state: WarmupState | None, now: datetime) -> str:
    """把这段沉默的事实告诉模型，让“要不要说”有依据。"""

    if state is None or state.last_human_at is None:
        return ""
    idle = max(int((now - state.last_human_at).total_seconds() // 60), 0)
    lines = [f"真人群友已经安静了约 {idle} 分钟。"]
    if state.unanswered > 0:
        lines.append(
            f"这段安静里你已经主动说了 {state.unanswered} 条，都没有人接。"
            "这通常说明大家在忙；除非有明显不同、确实值得一说的新内容，"
            "否则保持沉默(speak=false，或 action=close 表示这段冷场不再打扰)。"
            "绝对不要接着自己上一条往下说、不要自问自答、"
            "不要说“怎么没人理我”“人呢”之类的话。"
        )
    if state.recent_topics:
        lines.append(
            "这段安静里你已经提过："
            + "；".join(state.recent_topics)
            + "。不要再提同一话题。"
        )
    return "\n".join(lines)


__all__ = [
    "NEAR_DUPLICATE_THRESHOLD",
    "WARMUP_BACKOFF_FACTOR",
    "WARMUP_DAILY_CAP",
    "WARMUP_DEAD_GROUP_HOURS",
    "WARMUP_DECAY_HOURS",
    "WARMUP_MAX_BACKOFF_MINUTES",
    "WARMUP_MAX_UNANSWERED",
    "WARMUP_MIN_IDLE_MINUTES",
    "WARMUP_PROBABILITY_CAP",
    "WARMUP_QUIET_HOURS",
    "WARMUP_SENT_RESULTS",
    "WARMUP_SKIP_ACTIONS",
    "WarmupGate",
    "WarmupState",
    "char_bigrams",
    "effective_idle_threshold_minutes",
    "episode_roll",
    "evaluate_warmup_gate",
    "in_quiet_hours",
    "max_unanswered_for",
    "near_duplicate_of",
    "required_idle_minutes",
    "silence_decay",
    "skip_retry_minutes",
    "streak_scale",
    "warmup_daily_cap",
    "warmup_prompt_context",
]
