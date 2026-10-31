# ruff: noqa: E501
"""签到的唯一实现：`/签到` 命令、群 Agent 工具、私聊 Agent 都调这里。

原来签到逻辑写死在命令处理函数里，Agent 想“替我签到”只能另写一份；积分之外，
签到也不影响好感和群经验（面板上这两个数永远是 0）。现在：

- :func:`perform_checkin` 完成一次签到：补齐 BotUser/BotGroup/UserGroup、插入
  当日记录（数据库唯一约束防重复）、更新汇总、加群经验、按连签给好感与里程碑奖励。
  **不提交事务**，调用方决定提交点；重复签到只回滚自己的 SAVEPOINT，不会把调用方
  之前的写入一起回滚。
- :func:`checkin_overview` 只读，列出一个人在各群的签到状态（含“今天签了没”“连签
  是否已断”）。
- :func:`format_checkin_reply` 把结果变成命令回复文本；Agent 走结构化结果自己组织语言。

奖励规则集中在本文件顶部，改数值只改这里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from random import randint
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from . import affinity
from .data_models.bot_group import BotGroup
from .data_models.bot_user import BotUser
from .data_models.checkin_record import CheckinRecord
from .data_models.checkin_user import CheckinUser
from .data_models.user_group import UserGroup

# 签到日期按照中国时间计算
CHECKIN_TIMEZONE = ZoneInfo("Asia/Shanghai")

# 每次签到随机积分区间
POINTS_MIN = 5
POINTS_MAX = 15
# 群经验：基础 + 连签加成（连签第 2 天起每天 +2，最多 +12）
EXP_BASE = 10
EXP_STREAK_STEP = 2
EXP_STREAK_MAX_BONUS = 12
# 好感：基础 + 每连签满 7 天 +1（最多 +3）；每日跨群合计受 affinity 的 checkin 上限约束
AFFINITY_BASE = 2
AFFINITY_STREAK_MAX_BONUS = 3
# 连签里程碑：天数 → (额外积分, 额外好感)
MILESTONES: dict[int, tuple[int, int]] = {
    7: (20, 3),
    30: (60, 8),
    100: (200, 15),
    365: (500, 30),
}
# 私聊一键签到最多处理多少个群
MAX_BATCH_GROUPS = 10

CheckinStatus = Literal["ok", "already"]


def checkin_today(now: datetime | None = None) -> date:
    """北京时间的“今天”。"""

    moment = now or datetime.now(CHECKIN_TIMEZONE)
    if moment.tzinfo is None:
        return moment.date()
    return moment.astimezone(CHECKIN_TIMEZONE).date()


def exp_reward(streak_days: int) -> int:
    bonus = min(max(streak_days - 1, 0) * EXP_STREAK_STEP, EXP_STREAK_MAX_BONUS)
    return EXP_BASE + bonus


def affinity_reward(streak_days: int) -> int:
    return AFFINITY_BASE + min(max(streak_days, 0) // 7, AFFINITY_STREAK_MAX_BONUS)


def next_milestone(streak_days: int) -> Optional[int]:
    for days in sorted(MILESTONES):
        if days > streak_days:
            return days
    return None


@dataclass(frozen=True, slots=True)
class CheckinResult:
    """一次签到的结果（全部是普通值，commit 后也能安全读取）。"""

    status: CheckinStatus
    group_id: int
    user_id: int
    day: date
    reward: int = 0
    total_days: int = 0
    streak_days: int = 0
    points: int = 0
    exp_gain: int = 0
    exp_total: int = 0
    rank_today: int = 0
    milestone: Optional[int] = None
    milestone_points: int = 0
    affinity_changes: tuple[affinity.AffinityChange, ...] = field(default=())
    next_milestone: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def affinity_gain(self) -> int:
        return sum(change.applied for change in self.affinity_changes)

    @property
    def affinity_after(self) -> Optional[int]:
        applied = [c for c in self.affinity_changes if c.available]
        return applied[-1].after if applied else None

    @property
    def leveled_up_to(self) -> Optional[str]:
        if not self.affinity_changes:
            return None
        first, last = self.affinity_changes[0], self.affinity_changes[-1]
        if not (first.available and last.available):
            return None
        before, after = affinity.level_for(first.before), affinity.level_for(last.after)
        return after.name if after.index > before.index else None

    def public_dict(self) -> dict[str, Any]:
        """给 Agent 工具的投影：模型需要的事实，不含好感数值。"""

        payload: dict[str, Any] = {
            "status": self.status,
            "group_id": self.group_id,
            "date": self.day.isoformat(),
            "streak_days": self.streak_days,
            "total_days": self.total_days,
            "points": self.points,
        }
        if self.ok:
            payload["reward_points"] = self.reward + self.milestone_points
            payload["exp_gain"] = self.exp_gain
            payload["rank_today"] = self.rank_today
            payload["affinity_up"] = self.affinity_gain > 0
        if self.milestone:
            payload["milestone_days"] = self.milestone
        if self.leveled_up_to:
            payload["affinity_level_up"] = self.leveled_up_to
        if self.next_milestone:
            payload["next_milestone_days"] = self.next_milestone
        return payload


async def _upsert_identity(
    session: Any,
    *,
    group_id: int,
    user_id: int,
    nickname: Optional[str],
    now: datetime,
) -> UserGroup:
    user = await session.get(BotUser, user_id)
    if user is None:
        user = BotUser(user_id=user_id, nickname=nickname, last_interaction_at=now)
        user.affinity = 0
        session.add(user)
    else:
        user.last_interaction_at = now
        if nickname:
            user.nickname = nickname

    group = await session.get(BotGroup, group_id)
    if group is None:
        session.add(BotGroup(group_id=group_id, last_active_at=now))
    else:
        group.last_active_at = now

    membership = await session.get(
        UserGroup, {"group_id": group_id, "user_id": user_id}
    )
    if membership is None:
        membership = UserGroup(
            group_id=group_id,
            user_id=user_id,
            last_seen_at=now,
            group_nickname=nickname,
            is_active=True,
        )
        # 新建对象的列默认值在 INSERT 前是 None，后面要做加法，显式置 0
        membership.exp = 0
        membership.coins = 0
        membership.group_affinity = 0
        session.add(membership)
    else:
        membership.last_seen_at = now
        membership.is_active = True
        if nickname:
            membership.group_nickname = nickname
    await session.flush()
    return membership


async def _already_result(
    session: Any, *, group_id: int, user_id: int, day: date
) -> CheckinResult:
    summary = await session.get(CheckinUser, {"group_id": group_id, "user_id": user_id})
    membership = await session.get(
        UserGroup, {"group_id": group_id, "user_id": user_id}
    )
    record = await session.scalar(
        select(CheckinRecord).where(
            CheckinRecord.group_id == group_id,
            CheckinRecord.user_id == user_id,
            CheckinRecord.checkin_date == day,
        )
    )
    streak = int(summary.streak_days or 0) if summary else 0
    return CheckinResult(
        status="already",
        group_id=group_id,
        user_id=user_id,
        day=day,
        reward=int(record.reward or 0) if record else 0,
        total_days=int(summary.total_days or 0) if summary else 0,
        streak_days=streak,
        points=int(summary.points or 0) if summary else 0,
        exp_total=int(membership.exp or 0) if membership else 0,
        next_milestone=next_milestone(streak),
    )


async def perform_checkin(  # noqa: PLR0913
    session: Any,
    *,
    group_id: int,
    user_id: int,
    nickname: Optional[str] = None,
    now: datetime | None = None,
    points_reward: int | None = None,
) -> CheckinResult:
    """在某个群签到一次。不提交事务。

    ``points_reward`` 只给测试固定随机数用。
    """

    moment = now or datetime.now(CHECKIN_TIMEZONE)
    if moment.tzinfo is not None:
        moment = moment.astimezone(CHECKIN_TIMEZONE).replace(tzinfo=None)
    today = moment.date()
    yesterday = today - timedelta(days=1)

    membership = await _upsert_identity(
        session, group_id=group_id, user_id=user_id, nickname=nickname, now=moment
    )
    reward = (
        points_reward if points_reward is not None else randint(POINTS_MIN, POINTS_MAX)
    )
    try:
        async with session.begin_nested():
            session.add(
                CheckinRecord(
                    group_id=group_id,
                    user_id=user_id,
                    checkin_date=today,
                    reward=reward,
                    created_at=moment,
                )
            )
            await session.flush()
    except IntegrityError:
        return await _already_result(
            session, group_id=group_id, user_id=user_id, day=today
        )

    summary = await session.get(CheckinUser, {"group_id": group_id, "user_id": user_id})
    if summary is None:
        summary = CheckinUser(group_id=group_id, user_id=user_id)
        # 列默认值只在 INSERT 时生效，新建对象此刻为 None，需显式置 0
        summary.total_days = 0
        summary.points = 0
        summary.streak_days = 0
        session.add(summary)

    summary.total_days = int(summary.total_days or 0) + 1
    if summary.last_checkin_date == yesterday:
        summary.streak_days = int(summary.streak_days or 0) + 1
    else:
        summary.streak_days = 1
    summary.last_checkin_date = today
    streak = int(summary.streak_days)

    milestone = streak if streak in MILESTONES else None
    milestone_points, milestone_affinity = MILESTONES.get(streak, (0, 0))
    summary.points = int(summary.points or 0) + reward + milestone_points

    gained_exp = exp_reward(streak)
    membership.exp = int(membership.exp or 0) + gained_exp
    await session.flush()

    rank = int(
        await session.scalar(
            select(func.count())
            .select_from(CheckinRecord)
            .where(
                CheckinRecord.group_id == group_id,
                CheckinRecord.checkin_date == today,
            )
        )
        or 1
    )

    changes = [
        await affinity.grant(
            session,
            user_id,
            "checkin",
            affinity_reward(streak),
            group_id=group_id,
            note=f"签到（连签 {streak} 天）",
            now=moment,
        )
    ]
    if milestone_affinity:
        changes.append(
            await affinity.grant(
                session,
                user_id,
                "milestone",
                milestone_affinity,
                group_id=group_id,
                note=f"连签 {streak} 天里程碑",
                now=moment,
            )
        )

    return CheckinResult(
        status="ok",
        group_id=group_id,
        user_id=user_id,
        day=today,
        reward=reward,
        total_days=int(summary.total_days),
        streak_days=streak,
        points=int(summary.points),
        exp_gain=gained_exp,
        exp_total=int(membership.exp or 0),
        rank_today=rank,
        milestone=milestone,
        milestone_points=milestone_points,
        affinity_changes=tuple(changes),
        next_milestone=next_milestone(streak),
    )


@dataclass(frozen=True, slots=True)
class GroupCheckinStatus:
    """某人在某个群的签到状态（只读视图）。"""

    group_id: int
    group_name: Optional[str]
    checked_today: bool
    streak_days: int
    streak_broken: bool
    total_days: int
    points: int
    exp: int
    group_affinity: int
    last_checkin_date: Optional[date]

    def public_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "group_name": self.group_name or f"群 {self.group_id}",
            "checked_today": self.checked_today,
            "streak_days": self.streak_days,
            "streak_broken": self.streak_broken,
            "total_days": self.total_days,
            "points": self.points,
            "exp": self.exp,
            "last_checkin_date": (
                self.last_checkin_date.isoformat() if self.last_checkin_date else None
            ),
        }


async def checkin_overview(
    session: Any,
    user_id: int,
    *,
    group_ids: list[int] | None = None,
    now: datetime | None = None,
    active_only: bool = True,
) -> list[GroupCheckinStatus]:
    """列出某人在各群的签到状态，按“今天没签 + 连签长”优先排序。

    连签在“上次签到早于昨天”时视为已断，显示 0（库里的值等下次签到时重置）。
    """

    today = checkin_today(now)
    yesterday = today - timedelta(days=1)
    statement = (
        select(UserGroup, BotGroup.group_name, CheckinUser)
        .join(BotGroup, BotGroup.group_id == UserGroup.group_id)
        .outerjoin(
            CheckinUser,
            (CheckinUser.group_id == UserGroup.group_id)
            & (CheckinUser.user_id == UserGroup.user_id),
        )
        .where(UserGroup.user_id == user_id)
    )
    if active_only:
        statement = statement.where(UserGroup.is_active.is_(True))
    if group_ids is not None:
        if not group_ids:
            return []
        statement = statement.where(UserGroup.group_id.in_(sorted(set(group_ids))))
    rows = (await session.execute(statement)).all()
    result: list[GroupCheckinStatus] = []
    for membership, group_name, summary in rows:
        last = summary.last_checkin_date if summary else None
        stored_streak = int(summary.streak_days or 0) if summary else 0
        broken = bool(last is not None and last < yesterday)
        result.append(
            GroupCheckinStatus(
                group_id=int(membership.group_id),
                group_name=group_name,
                checked_today=last == today,
                streak_days=0 if broken else stored_streak,
                streak_broken=broken and stored_streak > 0,
                total_days=int(summary.total_days or 0) if summary else 0,
                points=int(summary.points or 0) if summary else 0,
                exp=int(membership.exp or 0),
                group_affinity=int(membership.group_affinity or 0),
                last_checkin_date=last,
            )
        )
    result.sort(key=lambda item: (item.checked_today, -item.streak_days, item.group_id))
    return result


async def group_checkin_board(
    session: Any, group_id: int, *, now: datetime | None = None, limit: int = 10
) -> list[tuple[int, int]]:
    """今天本群的签到顺序：[(user_id, 名次)]。"""

    today = checkin_today(now)
    rows = (
        await session.execute(
            select(CheckinRecord.user_id)
            .where(
                CheckinRecord.group_id == group_id,
                CheckinRecord.checkin_date == today,
            )
            .order_by(CheckinRecord.id)
            .limit(max(1, min(int(limit), 50)))
        )
    ).all()
    return [(int(user_id), index + 1) for index, (user_id,) in enumerate(rows)]


def format_checkin_reply(result: CheckinResult) -> str:
    """命令回复正文（不含 @）。"""

    if not result.ok:
        return (
            f"你今天已经签到过了哦~ 连续签到 {result.streak_days} 天，"
            f"当前积分 {result.points}。"
        )
    lines = [
        f"签到成功！你是今天第 {result.rank_today} 个签到的~",
        f"获得 {result.reward} 积分、{result.exp_gain} 点群经验",
        f"累计签到 {result.total_days} 天，连续签到 {result.streak_days} 天，"
        f"当前积分 {result.points}。",
    ]
    if result.milestone:
        lines.append(
            f"🎉 连签 {result.milestone} 天达成！额外奖励 {result.milestone_points} 积分"
        )
    gain = result.affinity_gain
    if gain > 0:
        level = result.leveled_up_to
        lines.append(
            f"好感 +{gain}，关系升级为「{level}」！" if level else f"好感 +{gain}"
        )
    if result.next_milestone and not result.milestone:
        lines.append(
            f"再坚持 {result.next_milestone - result.streak_days} 天达成连签 {result.next_milestone} 天"
        )
    return "\n".join(lines)


def format_batch_reply(results: list[tuple[str, CheckinResult]]) -> str:
    """私聊一键签到的汇总回复。``results`` 为 (群名, 结果)。"""

    if not results:
        return "没有找到可以签到的群~ 先在群里和我说过话才能在这里帮你签哦"
    lines: list[str] = []
    gained = 0
    for name, result in results:
        if result.ok:
            extra = f"，连签 {result.streak_days} 天"
            if result.milestone:
                extra += f"（🎉 {result.milestone} 天里程碑）"
            lines.append(f"✅ {name}：+{result.reward} 积分{extra}")
            gained += result.affinity_gain
        else:
            lines.append(f"☑️ {name}：今天已签，连签 {result.streak_days} 天")
    if gained > 0:
        lines.append(f"好感 +{gained}")
    return "\n".join(lines)


__all__ = [
    "CHECKIN_TIMEZONE",
    "MAX_BATCH_GROUPS",
    "MILESTONES",
    "POINTS_MAX",
    "POINTS_MIN",
    "CheckinResult",
    "GroupCheckinStatus",
    "affinity_reward",
    "checkin_overview",
    "checkin_today",
    "exp_reward",
    "format_batch_reply",
    "format_checkin_reply",
    "group_checkin_board",
    "next_milestone",
    "perform_checkin",
]
