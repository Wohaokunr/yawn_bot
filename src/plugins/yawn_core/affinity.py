# ruff: noqa: E501, TRY003
"""好感度：全局唯一的加减入口。

``BotUser.affinity`` 原来只在面板里显示、从来没有代码写过它。现在它是 Bot 对一个
人的全局好感余额，签到、私聊、群里和 Bot 的真实来往都通过 :func:`grant` 推动它；
``UserGroup.group_affinity`` 记的是在某个群里攒下的那部分。每次变化写一条
``AffinityEvent`` 流水，用来做“每个来源每天的上限”，也让面板和 WebUI 能解释
“为什么涨了”。

约束：

- 只有这里写 ``BotUser.affinity`` / ``UserGroup.group_affinity``，别处不要直接改。
- 每个来源有每日上限（:data:`SOURCE_RULES`），防止刷消息刷好感；超出部分静默丢弃，
  返回值里 ``capped=True``。
- 流水表没迁移时整段降级：不加不减、``available=False``，调用方（比如签到）照常完成。
- 不在这里做任何关键词或情绪判断：给不给好感由调用方按**结构事实**决定
  （签到成功、私聊回合真的回复了、群里有人 @ 或回复了 Bot）。
- 给模型看的只有等级名（:func:`level_for`），不报数值，避免 Bot 张口就说“你的好感 87”。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from nonebot import logger
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError, ProgrammingError

from .data_models.affinity_event import AffinityEvent
from .data_models.bot_user import BotUser
from .data_models.user_group import UserGroup

_BJ_TZ = timezone(timedelta(hours=8))

# 全局好感上限：足够大，等级只到“挚友”，这里只是防溢出。
MAX_AFFINITY = 9999
# 流水备注的最大长度（与表字段一致）。
_NOTE_LIMIT = 120
# 流水表缺失后，多久再试一次（秒）；迁移跑完不用重启也能恢复。
_MISSING_RETRY_SECONDS = 600.0


@dataclass(frozen=True, slots=True)
class SourceRule:
    """一个好感来源：展示名与每日上限（None 表示不设上限）。"""

    label: str
    daily_cap: Optional[int]
    allow_negative: bool = False


SOURCE_RULES: dict[str, SourceRule] = {
    # 每天在所有群签到合计最多加这么多；多群签到不会成倍刷好感。
    "checkin": SourceRule("签到", 8),
    # 连签 7/30/100/365 天的一次性奖励，天然不可刷，不设日上限。
    "milestone": SourceRule("签到里程碑", None),
    # 私聊 Agent 真正回复了的一轮。
    "private_chat": SourceRule("私聊", 5),
    # 群里明确 @ / 回复 Bot 且 Bot 回了话的一轮。
    "group_chat": SourceRule("群聊互动", 4),
    # 管理员在 WebUI 手动调整，可正可负。
    "manual": SourceRule("管理员调整", None, allow_negative=True),
    # 用签到积分送礼物；实际日上限由 affinity_policy.gift_affinity_daily_cap 决定。
    "gift": SourceRule("送礼", 10),
    # 完成每日任务；实际日上限 = 每个任务奖励 × 每天任务数。
    "quest": SourceRule("每日任务", 6),
    # /击掌 /摸头 等互动；实际日上限由 interact_affinity_daily_cap 决定。
    "interact": SourceRule("互动", 3),
    # 长时间没有来往的衰减（只扣不加，默认只扣到当前等级下限）。
    "decay": SourceRule("久未来往", None, allow_negative=True),
    # Bot 自己记下的心动瞬间（+）/ 被冒犯（-）；上下限由 affinity_policy.moments 决定。
    "moment": SourceRule("心动瞬间", 3, allow_negative=True),
    # 连续来往的里程碑奖励（同一段连续每档一次，天然不可刷）。
    "streak": SourceRule("连续来往", None),
    # /今日缘分 每天第一次抽。
    "fortune": SourceRule("今日缘分", 2),
    # 周任务奖励；实际上限 = 每个奖励 × 每周任务数（按周不按天，这里不设日上限）。
    "weekly": SourceRule("周任务", None),
}


@dataclass(frozen=True, slots=True)
class AffinityLevel:
    """好感等级：只给词语，数值留在系统里。"""

    index: int
    name: str
    floor: int
    next_floor: Optional[int]
    next_name: Optional[str]

    def progress(self, points: int) -> float:
        """在本级里走了多少（0~1）；最高级恒为 1。"""

        if self.next_floor is None:
            return 1.0
        span = max(self.next_floor - self.floor, 1)
        return round(min(max((points - self.floor) / span, 0.0), 1.0), 3)

    def to_next(self, points: int) -> Optional[int]:
        if self.next_floor is None:
            return None
        return max(self.next_floor - points, 0)


# (下限, 名字)，按下限升序。门槛按“每天签到 + 偶尔聊天”大约一周进一级、
# 几个月才到挚友来定。
LEVELS: tuple[tuple[int, str], ...] = (
    (0, "陌生"),
    (20, "面熟"),
    (60, "熟人"),
    (150, "朋友"),
    (320, "好朋友"),
    (600, "挚友"),
)

# 每个等级给模型的一句相处提示（只在发言人层出现，不含数值）。
LEVEL_TONE: dict[str, str] = {
    "陌生": "还不太熟，礼貌友好即可",
    "面熟": "见过几次，可以随意一点",
    "熟人": "比较熟了，可以开开玩笑",
    "朋友": "是朋友，说话可以亲近自然",
    "好朋友": "关系很好，可以更亲昵、多关心对方",
    "挚友": "非常亲近的老朋友，像老友一样相处",
}


def level_for(points: int | None) -> AffinityLevel:
    """按余额给出等级。负数按 0 处理（余额本身不会低于 0）。"""

    value = max(int(points or 0), 0)
    index = 0
    for position, (floor, _name) in enumerate(LEVELS):
        if value >= floor:
            index = position
    floor, name = LEVELS[index]
    if index + 1 < len(LEVELS):
        next_floor, next_name = LEVELS[index + 1]
    else:
        next_floor, next_name = None, None
    return AffinityLevel(index, name, floor, next_floor, next_name)


def level_tone(name: str) -> str:
    """等级的相处提示：WebUI 策略里改过就用改过的，否则用内置的 LEVEL_TONE。"""

    try:
        from .affinity_policy import load_policy

        tone = load_policy().perks(name).tone
    except Exception:  # noqa: BLE001
        tone = ""
    return tone or LEVEL_TONE.get(name, "")


def level_words(points: int | None) -> str:
    """给 Prompt 用的一句话：“好朋友（关系很好，可以更亲昵、多关心对方）”。"""

    level = level_for(points)
    return f"{level.name}（{level_tone(level.name)}）"


@dataclass(frozen=True, slots=True)
class AffinityChange:
    """一次 :func:`grant` 的结果；调用方据此决定要不要在回复里提一句。"""

    source: str
    requested: int
    applied: int
    before: int
    after: int
    capped: bool = False
    available: bool = True

    @property
    def level_before(self) -> AffinityLevel:
        return level_for(self.before)

    @property
    def level_after(self) -> AffinityLevel:
        return level_for(self.after)

    @property
    def leveled_up(self) -> bool:
        return self.level_after.index > self.level_before.index

    def public_dict(self) -> dict[str, Any]:
        """给工具结果/WebUI 用的投影（模型看得到等级名，看不到余额）。"""

        payload: dict[str, Any] = {
            "changed": self.applied != 0,
            "level": self.level_after.name,
        }
        if self.leveled_up:
            payload["leveled_up_to"] = self.level_after.name
        if self.capped:
            payload["daily_cap_reached"] = True
        if not self.available:
            payload["unavailable"] = True
        return payload


def _beijing_now() -> datetime:
    return datetime.now(_BJ_TZ).replace(tzinfo=None)


def _table_missing(error: Exception) -> bool:
    text = str(error).lower()
    return "no such table" in text or "does not exist" in text


_missing_since: float | None = None


def ledger_known_missing() -> bool:
    """流水表最近确认缺失时返回 True（过一段时间会自动重试）。"""

    if _missing_since is None:
        return False
    return time.monotonic() - _missing_since < _MISSING_RETRY_SECONDS


def _mark_missing() -> None:
    global _missing_since  # noqa: PLW0603
    if _missing_since is None or not ledger_known_missing():
        logger.warning(
            "好感度流水表尚未迁移，好感变化暂时跳过（请执行 nb orm upgrade）"
        )
    _missing_since = time.monotonic()


def _mark_available() -> None:
    global _missing_since  # noqa: PLW0603
    _missing_since = None


def reset_ledger_state() -> None:
    """测试用：清掉“表缺失”的进程缓存。"""

    _mark_available()


async def used_today(session: Any, user_id: int, source: str, day: date) -> int:
    """某来源今天已经加了多少（只算正数）。"""

    total = await session.scalar(
        select(func.coalesce(func.sum(AffinityEvent.delta), 0)).where(
            AffinityEvent.user_id == user_id,
            AffinityEvent.source == source,
            AffinityEvent.day == day,
            AffinityEvent.delta > 0,
        )
    )
    return int(total or 0)


async def _ensure_user(session: Any, user_id: int, now: datetime) -> BotUser:
    user = await session.get(BotUser, user_id)
    if user is None:
        user = BotUser(user_id=user_id, last_interaction_at=now, affinity=0)
        session.add(user)
        await session.flush()
    return user


async def grant(  # noqa: C901, PLR0913
    session: Any,
    user_id: int,
    source: str,
    amount: int,
    *,
    group_id: int | None = None,
    note: str = "",
    now: datetime | None = None,
) -> AffinityChange:
    """给某人加（或减）好感，返回实际变化。

    - 正数受来源的每日上限约束，超出部分丢弃；
    - 负数只允许 ``allow_negative`` 的来源（管理员调整）；
    - 余额夹在 ``[0, MAX_AFFINITY]``；
    - 带 ``group_id`` 时同步给该群的 ``UserGroup.group_affinity``（成员关系存在才写）；
    - 整段在 SAVEPOINT 里执行，失败只回滚这一段，不影响调用方的事务。

    不提交事务：调用方决定什么时候 commit（签到命令、Agent 工具执行器都有自己的提交点）。
    """

    rule = SOURCE_RULES.get(source)
    if rule is None:
        raise ValueError(f"未知好感来源: {source}")
    requested = int(amount)
    if requested < 0 and not rule.allow_negative:
        raise ValueError(f"来源 {source} 不允许扣好感")
    daily_cap = _daily_cap(source, rule)
    if requested == 0 or ledger_known_missing():
        return AffinityChange(
            source, requested, 0, 0, 0, available=not ledger_known_missing()
        )
    moment = now or _beijing_now()
    day = moment.date()
    try:
        async with session.begin_nested():
            applied = requested
            capped = False
            if daily_cap is not None and requested > 0:
                remaining = max(
                    daily_cap - await used_today(session, user_id, source, day), 0
                )
                if remaining < requested:
                    applied = remaining
                    capped = True
            user = await _ensure_user(session, user_id, moment)
            before = max(int(user.affinity or 0), 0)
            after = min(max(before + applied, 0), MAX_AFFINITY)
            applied = after - before
            if applied:
                user.affinity = after
                if group_id is not None:
                    membership = await session.get(UserGroup, (group_id, user_id))
                    if membership is not None:
                        membership.group_affinity = max(
                            int(membership.group_affinity or 0) + applied, 0
                        )
                session.add(
                    AffinityEvent(
                        user_id=user_id,
                        group_id=group_id,
                        source=source,
                        delta=applied,
                        balance_after=after,
                        note=str(note or "")[:_NOTE_LIMIT],
                        day=day,
                        created_at=moment,
                    )
                )
                await session.flush()
    except (OperationalError, ProgrammingError) as error:
        if not _table_missing(error):
            raise
        _mark_missing()
        return AffinityChange(source, requested, 0, 0, 0, available=False)
    _mark_available()
    change = AffinityChange(source, requested, applied, before, after, capped=capped)
    _after_grant(change, user_id=user_id, source=source, group_id=group_id, now=moment)
    return change


def _daily_cap(source: str, rule: SourceRule) -> Optional[int]:
    """来源的每日上限：礼物/互动/任务由好感玩法策略决定，其余用 SOURCE_RULES。"""

    try:
        from .affinity_perks import dynamic_daily_cap

        value = dynamic_daily_cap(source)
    except Exception:  # noqa: BLE001
        value = False
    if value is False:
        return rule.daily_cap
    return value  # type: ignore[return-value]


def _after_grant(change: AffinityChange, **kwargs: Any) -> None:
    """任务进度、升级记录、余额缓存（affinity_perks）；任何异常都不影响好感本身。"""

    try:
        from .affinity_perks import after_grant

        after_grant(change, **kwargs)
    except Exception:  # noqa: BLE001
        logger.opt(exception=True).debug("好感钩子失败(忽略)")


def _remember(user_id: int, points: int) -> None:
    try:
        from .affinity_perks import remember_points

        remember_points(user_id, points)
    except Exception:  # noqa: BLE001
        pass


@dataclass(frozen=True, slots=True)
class AffinitySnapshot:
    """读一个人的好感现状（只读，不写库）。"""

    user_id: int
    points: int
    level: AffinityLevel
    today: dict[str, int]
    available: bool

    def public_dict(self, *, include_points: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "level": self.level.name,
            "next_level": self.level.next_name,
            "progress": self.level.progress(self.points),
        }
        if include_points:
            payload["points"] = self.points
            payload["to_next"] = self.level.to_next(self.points)
            payload["today"] = dict(self.today)
        if not self.available:
            payload["unavailable"] = True
        return payload


async def snapshot(
    session: Any, user_id: int, *, now: datetime | None = None
) -> AffinitySnapshot:
    """好感余额、等级和今天各来源已加的量。"""

    user = await session.get(BotUser, user_id)
    points = max(int(user.affinity or 0), 0) if user is not None else 0
    _remember(user_id, points)
    today: dict[str, int] = {}
    available = not ledger_known_missing()
    if available:
        day = (now or _beijing_now()).date()
        try:
            rows = (
                await session.execute(
                    select(AffinityEvent.source, func.sum(AffinityEvent.delta))
                    .where(
                        AffinityEvent.user_id == user_id,
                        AffinityEvent.day == day,
                    )
                    .group_by(AffinityEvent.source)
                )
            ).all()
            today = {str(source): int(total or 0) for source, total in rows}
        except (OperationalError, ProgrammingError) as error:
            if not _table_missing(error):
                raise
            _mark_missing()
            available = False
    return AffinitySnapshot(user_id, points, level_for(points), today, available)


async def recent_events(
    session: Any, user_id: int, *, limit: int = 20
) -> list[dict[str, Any]] | None:
    """最近的流水（新到旧）；表缺失时返回 None。"""

    try:
        rows = (
            (
                await session.execute(
                    select(AffinityEvent)
                    .where(AffinityEvent.user_id == user_id)
                    .order_by(AffinityEvent.id.desc())
                    .limit(max(1, min(int(limit), 200)))
                )
            )
            .scalars()
            .all()
        )
    except (OperationalError, ProgrammingError) as error:
        if not _table_missing(error):
            raise
        _mark_missing()
        return None
    return [event_dict(row) for row in rows]


def event_dict(row: AffinityEvent) -> dict[str, Any]:
    rule = SOURCE_RULES.get(str(row.source))
    return {
        "id": int(row.id),
        "group_id": int(row.group_id) if row.group_id is not None else None,
        "source": str(row.source),
        "source_label": rule.label if rule else str(row.source),
        "delta": int(row.delta),
        "balance_after": int(row.balance_after),
        "note": str(row.note or ""),
        "day": row.day.isoformat() if row.day else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def source_breakdown(
    session: Any, user_id: int, *, days: int = 30, now: datetime | None = None
) -> dict[str, int] | None:
    """近 N 天各来源的净变化；表缺失时返回 None。"""

    since = (now or _beijing_now()).date() - timedelta(days=max(int(days), 1) - 1)
    try:
        rows = (
            await session.execute(
                select(AffinityEvent.source, func.sum(AffinityEvent.delta))
                .where(AffinityEvent.user_id == user_id, AffinityEvent.day >= since)
                .group_by(AffinityEvent.source)
            )
        ).all()
    except (OperationalError, ProgrammingError) as error:
        if not _table_missing(error):
            raise
        _mark_missing()
        return None
    return {str(source): int(total or 0) for source, total in rows}


async def group_ranking(
    session: Any, group_id: int, *, limit: int = 10
) -> list[tuple[int, int]]:
    """群内好感排行（成员 user_id, 本群好感），用于面板/WebUI。"""

    rows = (
        await session.execute(
            select(UserGroup.user_id, UserGroup.group_affinity)
            .where(
                UserGroup.group_id == group_id,
                UserGroup.is_active.is_(True),
                UserGroup.group_affinity > 0,
            )
            .order_by(UserGroup.group_affinity.desc(), UserGroup.user_id)
            .limit(max(1, min(int(limit), 50)))
        )
    ).all()
    return [(int(user_id), int(points or 0)) for user_id, points in rows]


__all__ = [
    "LEVELS",
    "LEVEL_TONE",
    "MAX_AFFINITY",
    "SOURCE_RULES",
    "AffinityChange",
    "AffinityLevel",
    "AffinitySnapshot",
    "SourceRule",
    "event_dict",
    "grant",
    "group_ranking",
    "ledger_known_missing",
    "level_for",
    "level_tone",
    "level_words",
    "recent_events",
    "reset_ledger_state",
    "snapshot",
    "source_breakdown",
    "used_today",
]
