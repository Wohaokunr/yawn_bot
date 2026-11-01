# ruff: noqa: C901, PLR0912, PLR0913, PLR0915, TID252
"""群组与用户目录的查询与批量写入（WebUI 群组 / 用户页 2.0）。

只读查询都按“当前页再补统计”的方式做：先用筛选 + 排序拿到一页 id，
再对这一页做分组聚合，避免对全表逐行查询。所有时间比较用 naive 北京时间，
与数据库列的写入口径一致（见 data_models/timeutil.py）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import String, and_, delete, exists, func, or_, select
from sqlalchemy.orm import aliased

from ..affinity import level_for
from ..checkin_service import checkin_today
from ..data_models.bot_group import BotGroup
from ..data_models.bot_user import BotUser
from ..data_models.checkin_record import CheckinRecord
from ..data_models.checkin_user import CheckinUser
from ..data_models.global_user_feature import GlobalUserFeature
from ..data_models.group_agent_config import GroupAgentConfig
from ..data_models.group_feature import GroupFeature
from ..data_models.user_feature import UserFeature
from ..data_models.user_group import UserGroup
from ..permission import FEATURE_REGISTRY
from .service import BEIJING_TZ, iso

# 批量接口一次最多处理多少个对象；超过就让前端分批，避免一个事务锁太久。
BATCH_LIMIT = 100

ActivityWindow = Literal["24h", "7d", "30d", "dormant"]
_WINDOWS: dict[str, timedelta] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}

GROUP_SORTS = ("active", "members", "name", "first_seen", "checkins")
MEMBER_SORTS = ("recent", "affinity", "streak", "joined", "name")
MEMBER_ROLES = ("owner", "admin", "member")
USER_SORTS = ("recent", "affinity", "first_seen", "groups")


def now_naive() -> datetime:
    """naive 北京时间，用来和库里的时间列比较。"""

    return datetime.now(BEIJING_TZ).replace(tzinfo=None)


def _activity_clause(column: Any, window: str, now: datetime) -> Any | None:
    """把“最近 24 小时 / 7 天 / 30 天活跃 / 30 天以上没动静”翻成条件。"""

    if window in _WINDOWS:
        return column >= now - _WINDOWS[window]
    if window == "dormant":
        return or_(column.is_(None), column < now - _WINDOWS["30d"])
    return None


def _effective_streak(last: date | None, stored: int, today: date) -> int:
    """连签在上次签到早于昨天时已经断了，展示为 0（与 checkin_overview 一致）。"""

    if last is None or last < today - timedelta(days=1):
        return 0
    return int(stored or 0)


def _search_clause(search: str, *columns: Any) -> Any | None:
    if not search:
        return None
    pattern = f"%{search}%"
    parts = [column.ilike(pattern) for column in columns[:-1]]
    parts.append(columns[-1].cast(String).like(pattern))
    return or_(*parts)


def _agent_on_expression(config: Any, feature: Any) -> Any:
    """群 Agent 是否开启：专用开关与群功能 group_agent 都没关才算开。"""

    return and_(
        func.coalesce(config.enabled, True).is_(True),
        func.coalesce(feature.enabled, True).is_(True),
    )


async def _count_by(session: Any, statement: Any) -> dict[int, int]:
    return {
        int(key): int(value) for key, value in (await session.execute(statement)).all()
    }


# ── 群组列表 ────────────────────────────────────────────────


async def list_groups_v2(
    session: Any,
    *,
    page: int,
    page_size: int,
    search: str = "",
    agent: str = "",
    overrides: str = "",
    active: str = "",
    sort: str = "active",
) -> tuple[list[dict[str, Any]], int]:
    now = now_naive()
    today = checkin_today()
    config = aliased(GroupAgentConfig)
    agent_feature = aliased(GroupFeature)
    base = (
        select(BotGroup.group_id)
        .outerjoin(config, config.group_id == BotGroup.group_id)
        .outerjoin(
            agent_feature,
            and_(
                agent_feature.group_id == BotGroup.group_id,
                agent_feature.feature == "group_agent",
            ),
        )
    )
    conditions: list[Any] = []
    clause = _search_clause(search, BotGroup.group_name, BotGroup.group_id)
    if clause is not None:
        conditions.append(clause)
    agent_on = _agent_on_expression(config, agent_feature)
    if agent == "on":
        conditions.append(agent_on)
    elif agent == "off":
        conditions.append(~agent_on)
    group_override = exists().where(GroupFeature.group_id == BotGroup.group_id)
    member_override = exists().where(UserFeature.group_id == BotGroup.group_id)
    if overrides == "group":
        conditions.append(group_override)
    elif overrides == "member":
        conditions.append(member_override)
    elif overrides == "any":
        conditions.append(or_(group_override, member_override))
    activity = _activity_clause(BotGroup.last_active_at, active, now)
    if activity is not None:
        conditions.append(activity)
    if conditions:
        base = base.where(*conditions)

    total = int(
        await session.scalar(select(func.count()).select_from(base.subquery())) or 0
    )
    if sort == "members":
        counts = (
            select(UserGroup.group_id, func.count().label("n"))
            .group_by(UserGroup.group_id)
            .subquery()
        )
        ordered = base.outerjoin(
            counts, counts.c.group_id == BotGroup.group_id
        ).order_by(func.coalesce(counts.c.n, 0).desc(), BotGroup.group_id)
    elif sort == "checkins":
        counts = (
            select(CheckinUser.group_id, func.count().label("n"))
            .where(CheckinUser.last_checkin_date == today)
            .group_by(CheckinUser.group_id)
            .subquery()
        )
        ordered = base.outerjoin(
            counts, counts.c.group_id == BotGroup.group_id
        ).order_by(func.coalesce(counts.c.n, 0).desc(), BotGroup.group_id)
    elif sort == "name":
        # 没名字的群排在最后，其余按名字。
        ordered = base.order_by(
            BotGroup.group_name.is_(None), BotGroup.group_name, BotGroup.group_id
        )
    elif sort == "first_seen":
        ordered = base.order_by(BotGroup.first_seen_at.desc(), BotGroup.group_id)
    else:
        # 从没活跃过的群（NULL）放到最后，而不是被数据库排到最前。
        ordered = base.order_by(
            BotGroup.last_active_at.is_(None),
            BotGroup.last_active_at.desc(),
            BotGroup.group_id,
        )
    ids = [
        int(value)
        for value in (
            await session.execute(
                ordered.offset((page - 1) * page_size).limit(page_size)
            )
        ).scalars()
    ]
    if not ids:
        return [], total
    groups = {
        int(row.group_id): row
        for row in (
            await session.execute(select(BotGroup).where(BotGroup.group_id.in_(ids)))
        ).scalars()
    }
    configs = {
        int(row.group_id): row
        for row in (
            await session.execute(
                select(GroupAgentConfig).where(GroupAgentConfig.group_id.in_(ids))
            )
        ).scalars()
    }
    group_overrides: dict[int, dict[str, bool]] = {}
    for row in (
        await session.execute(
            select(GroupFeature).where(GroupFeature.group_id.in_(ids))
        )
    ).scalars():
        group_overrides.setdefault(int(row.group_id), {})[str(row.feature)] = bool(
            row.enabled
        )
    member_counts = await _count_by(
        session,
        select(UserGroup.group_id, func.count())
        .where(UserGroup.group_id.in_(ids))
        .group_by(UserGroup.group_id),
    )
    active_members = await _count_by(
        session,
        select(UserGroup.group_id, func.count())
        .where(
            UserGroup.group_id.in_(ids),
            UserGroup.last_seen_at >= now - _WINDOWS["7d"],
        )
        .group_by(UserGroup.group_id),
    )
    member_override_counts = await _count_by(
        session,
        select(UserFeature.group_id, func.count())
        .where(UserFeature.group_id.in_(ids))
        .group_by(UserFeature.group_id),
    )
    checkins = await _count_by(
        session,
        select(CheckinUser.group_id, func.count())
        .where(CheckinUser.group_id.in_(ids), CheckinUser.last_checkin_date == today)
        .group_by(CheckinUser.group_id),
    )
    rows: list[dict[str, Any]] = []
    for group_id in ids:
        row = groups[group_id]
        cfg = configs.get(group_id)
        features = group_overrides.get(group_id, {})
        agent_enabled = bool(
            (cfg.enabled if cfg is not None else True)
            and features.get("group_agent", True)
        )
        rows.append(
            {
                "groupId": str(group_id),
                "groupName": row.group_name,
                "firstSeenAt": iso(row.first_seen_at),
                "lastActiveAt": iso(row.last_active_at),
                "memberCount": member_counts.get(group_id, 0),
                "activeMembers7d": active_members.get(group_id, 0),
                "agentEnabled": agent_enabled,
                "personaEnabled": bool(cfg.persona_enabled) if cfg else False,
                "lastAgentAt": iso(cfg.last_agent_at) if cfg else None,
                "overrideCount": len(features),
                "disabledFeatures": sorted(
                    key for key, enabled in features.items() if not enabled
                ),
                "memberOverrideCount": member_override_counts.get(group_id, 0),
                "checkinsToday": checkins.get(group_id, 0),
            }
        )
    return rows, total


async def groups_summary(session: Any) -> dict[str, Any]:
    now = now_naive()
    today = checkin_today()
    config = aliased(GroupAgentConfig)
    agent_feature = aliased(GroupFeature)
    total = int(await session.scalar(select(func.count()).select_from(BotGroup)) or 0)
    agent_enabled = int(
        await session.scalar(
            select(func.count())
            .select_from(BotGroup)
            .outerjoin(config, config.group_id == BotGroup.group_id)
            .outerjoin(
                agent_feature,
                and_(
                    agent_feature.group_id == BotGroup.group_id,
                    agent_feature.feature == "group_agent",
                ),
            )
            .where(_agent_on_expression(config, agent_feature))
        )
        or 0
    )

    async def active_since(delta: timedelta) -> int:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(BotGroup)
                .where(BotGroup.last_active_at >= now - delta)
            )
            or 0
        )

    with_overrides = int(
        await session.scalar(
            select(func.count())
            .select_from(BotGroup)
            .where(
                or_(
                    exists().where(GroupFeature.group_id == BotGroup.group_id),
                    exists().where(UserFeature.group_id == BotGroup.group_id),
                )
            )
        )
        or 0
    )
    checkins = int(
        await session.scalar(
            select(func.count())
            .select_from(CheckinUser)
            .where(CheckinUser.last_checkin_date == today)
        )
        or 0
    )
    memberships = int(
        await session.scalar(select(func.count()).select_from(UserGroup)) or 0
    )
    return {
        "total": total,
        "agentEnabled": agent_enabled,
        "active24h": await active_since(_WINDOWS["24h"]),
        "active7d": await active_since(_WINDOWS["7d"]),
        "withOverrides": with_overrides,
        "checkinsToday": checkins,
        "members": memberships,
    }


# ── 单个群：概览、成员、覆盖一览 ─────────────────────────────


def _member_name(membership: Any, user: Any) -> str:
    return str(membership.group_nickname or user.nickname or membership.user_id)


async def group_overview(session: Any, group_id: int) -> dict[str, Any]:
    now = now_naive()
    today = checkin_today()
    roles = {
        str(role or "member"): int(count)
        for role, count in (
            await session.execute(
                select(UserGroup.role, func.count())
                .where(UserGroup.group_id == group_id)
                .group_by(UserGroup.role)
            )
        ).all()
    }

    async def active_since(delta: timedelta) -> int:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(UserGroup)
                .where(
                    UserGroup.group_id == group_id,
                    UserGroup.last_seen_at >= now - delta,
                )
            )
            or 0
        )

    async def checkins_on(day: date) -> int:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(CheckinRecord)
                .where(
                    CheckinRecord.group_id == group_id,
                    CheckinRecord.checkin_date == day,
                )
            )
            or 0
        )

    streak_rows = (
        await session.execute(
            select(CheckinUser, UserGroup, BotUser)
            .join(
                UserGroup,
                and_(
                    UserGroup.group_id == CheckinUser.group_id,
                    UserGroup.user_id == CheckinUser.user_id,
                ),
            )
            .join(BotUser, BotUser.user_id == CheckinUser.user_id)
            .where(
                CheckinUser.group_id == group_id,
                CheckinUser.last_checkin_date >= today - timedelta(days=1),
                CheckinUser.streak_days > 0,
            )
            .order_by(CheckinUser.streak_days.desc(), CheckinUser.total_days.desc())
            .limit(5)
        )
    ).all()
    affinity_rows = (
        await session.execute(
            select(UserGroup, BotUser)
            .join(BotUser, BotUser.user_id == UserGroup.user_id)
            .where(UserGroup.group_id == group_id, UserGroup.group_affinity > 0)
            .order_by(UserGroup.group_affinity.desc())
            .limit(5)
        )
    ).all()
    recent_rows = (
        await session.execute(
            select(UserGroup, BotUser)
            .join(BotUser, BotUser.user_id == UserGroup.user_id)
            .where(UserGroup.group_id == group_id, UserGroup.last_seen_at.is_not(None))
            .order_by(UserGroup.last_seen_at.desc())
            .limit(6)
        )
    ).all()
    cfg = await session.get(GroupAgentConfig, group_id)
    group_feature_count = int(
        await session.scalar(
            select(func.count())
            .select_from(GroupFeature)
            .where(GroupFeature.group_id == group_id)
        )
        or 0
    )
    agent_override = await session.get(
        GroupFeature, {"group_id": group_id, "feature": "group_agent"}
    )
    member_override_count = int(
        await session.scalar(
            select(func.count())
            .select_from(UserFeature)
            .where(UserFeature.group_id == group_id)
        )
        or 0
    )
    members_with_overrides = int(
        await session.scalar(
            select(func.count(func.distinct(UserFeature.user_id))).where(
                UserFeature.group_id == group_id
            )
        )
        or 0
    )
    return {
        "groupId": str(group_id),
        "roles": roles,
        "activeMembers": {
            "24h": await active_since(_WINDOWS["24h"]),
            "7d": await active_since(_WINDOWS["7d"]),
            "30d": await active_since(_WINDOWS["30d"]),
        },
        "checkins": {
            "today": await checkins_on(today),
            "yesterday": await checkins_on(today - timedelta(days=1)),
            "streakTop": [
                {
                    "userId": str(summary.user_id),
                    "name": _member_name(membership, user),
                    "streakDays": int(summary.streak_days or 0),
                    "totalDays": int(summary.total_days or 0),
                    "checkedToday": summary.last_checkin_date == today,
                }
                for summary, membership, user in streak_rows
            ],
        },
        "affinityTop": [
            {
                "userId": str(membership.user_id),
                "name": _member_name(membership, user),
                "groupAffinity": int(membership.group_affinity or 0),
            }
            for membership, user in affinity_rows
        ],
        "recentMembers": [
            {
                "userId": str(membership.user_id),
                "name": _member_name(membership, user),
                "role": membership.role,
                "lastSeenAt": iso(membership.last_seen_at),
            }
            for membership, user in recent_rows
        ],
        "agent": {
            "configured": cfg is not None,
            "enabled": bool(
                (cfg.enabled if cfg is not None else True)
                and (agent_override.enabled if agent_override is not None else True)
            ),
            "personaEnabled": bool(cfg.persona_enabled) if cfg else False,
            "proactiveEnabled": bool(cfg.proactive_enabled) if cfg else True,
            "dailyLimit": int(cfg.daily_limit) if cfg else None,
            "lastAgentAt": iso(cfg.last_agent_at) if cfg else None,
        },
        "overrides": {
            "group": group_feature_count,
            "member": member_override_count,
            "membersWithOverrides": members_with_overrides,
        },
    }


async def list_group_members_v2(
    session: Any,
    group_id: int,
    *,
    page: int,
    page_size: int,
    search: str = "",
    role: str = "",
    activity: str = "",
    overrides: bool = False,
    sort: str = "recent",
) -> tuple[list[dict[str, Any]], int]:
    now = now_naive()
    today = checkin_today()
    base = (
        select(UserGroup.user_id)
        .join(BotUser, BotUser.user_id == UserGroup.user_id)
        .outerjoin(
            CheckinUser,
            and_(
                CheckinUser.group_id == UserGroup.group_id,
                CheckinUser.user_id == UserGroup.user_id,
            ),
        )
        .where(UserGroup.group_id == group_id)
    )
    clause = _search_clause(
        search, BotUser.nickname, UserGroup.group_nickname, BotUser.user_id
    )
    if clause is not None:
        base = base.where(clause)
    if role in MEMBER_ROLES:
        base = base.where(UserGroup.role == role)
    window = _activity_clause(UserGroup.last_seen_at, activity, now)
    if window is not None:
        base = base.where(window)
    if overrides:
        base = base.where(
            exists().where(
                UserFeature.group_id == UserGroup.group_id,
                UserFeature.user_id == UserGroup.user_id,
            )
        )
    total = int(
        await session.scalar(select(func.count()).select_from(base.subquery())) or 0
    )
    if sort == "affinity":
        ordered = base.order_by(UserGroup.group_affinity.desc(), UserGroup.user_id)
    elif sort == "streak":
        # 断签的人库里 streak 还没清零，按“昨天或今天签过”才算数。
        alive = CheckinUser.last_checkin_date >= today - timedelta(days=1)
        ordered = base.order_by(
            alive.desc(),
            func.coalesce(CheckinUser.streak_days, 0).desc(),
            UserGroup.user_id,
        )
    elif sort == "joined":
        ordered = base.order_by(UserGroup.first_seen_at.desc(), UserGroup.user_id)
    elif sort == "name":
        ordered = base.order_by(
            func.coalesce(UserGroup.group_nickname, BotUser.nickname).is_(None),
            func.coalesce(UserGroup.group_nickname, BotUser.nickname),
            UserGroup.user_id,
        )
    else:
        ordered = base.order_by(
            UserGroup.last_seen_at.is_(None),
            UserGroup.last_seen_at.desc(),
            UserGroup.user_id,
        )
    ids = [
        int(value)
        for value in (
            await session.execute(
                ordered.offset((page - 1) * page_size).limit(page_size)
            )
        ).scalars()
    ]
    if not ids:
        return [], total
    rows = {
        int(membership.user_id): (membership, user, summary)
        for membership, user, summary in (
            await session.execute(
                select(UserGroup, BotUser, CheckinUser)
                .join(BotUser, BotUser.user_id == UserGroup.user_id)
                .outerjoin(
                    CheckinUser,
                    and_(
                        CheckinUser.group_id == UserGroup.group_id,
                        CheckinUser.user_id == UserGroup.user_id,
                    ),
                )
                .where(UserGroup.group_id == group_id, UserGroup.user_id.in_(ids))
            )
        ).all()
    }
    override_counts = await _count_by(
        session,
        select(UserFeature.user_id, func.count())
        .where(UserFeature.group_id == group_id, UserFeature.user_id.in_(ids))
        .group_by(UserFeature.user_id),
    )
    result: list[dict[str, Any]] = []
    for user_id in ids:
        membership, user, summary = rows[user_id]
        last = summary.last_checkin_date if summary else None
        result.append(
            {
                "userId": str(user_id),
                "nickname": user.nickname,
                "groupNickname": membership.group_nickname,
                "role": membership.role,
                "title": membership.title,
                "firstSeenAt": iso(membership.first_seen_at),
                "lastSeenAt": iso(membership.last_seen_at),
                "active": membership.is_active,
                "groupAffinity": int(membership.group_affinity or 0),
                "streakDays": _effective_streak(
                    last, int(summary.streak_days or 0) if summary else 0, today
                ),
                "totalDays": int(summary.total_days or 0) if summary else 0,
                "checkedToday": last == today,
                "overrideCount": override_counts.get(user_id, 0),
            }
        )
    return result, total


async def group_member_overrides(session: Any, group_id: int) -> list[dict[str, Any]]:
    """本群所有成员级覆盖，附带“不覆盖时群里是开还是关”方便对照。"""

    group_flags = {
        str(row.feature): bool(row.enabled)
        for row in (
            await session.execute(
                select(GroupFeature).where(GroupFeature.group_id == group_id)
            )
        ).scalars()
    }
    cfg = await session.get(GroupAgentConfig, group_id)
    agent_master = bool(
        (cfg.enabled if cfg is not None else True)
        and group_flags.get("group_agent", True)
    )
    rows = (
        await session.execute(
            select(UserFeature, UserGroup, BotUser)
            .join(
                UserGroup,
                and_(
                    UserGroup.group_id == UserFeature.group_id,
                    UserGroup.user_id == UserFeature.user_id,
                ),
            )
            .join(BotUser, BotUser.user_id == UserFeature.user_id)
            .where(UserFeature.group_id == group_id)
            .order_by(UserFeature.feature, UserFeature.user_id)
        )
    ).all()
    result = []
    for override, membership, user in rows:
        feature = str(override.feature)
        group_effective = group_flags.get(feature, True)
        effective = bool(override.enabled)
        blocked = False
        if feature == "group_agent" and not agent_master:
            group_effective = False
            # 群级总开关是硬门禁，成员显式开启也不生效。
            blocked = effective
            effective = False
        result.append(
            {
                "userId": str(override.user_id),
                "name": _member_name(membership, user),
                "role": membership.role,
                "feature": feature,
                "featureName": FEATURE_REGISTRY.get(feature, feature),
                "enabled": bool(override.enabled),
                "effective": effective,
                "groupEffective": group_effective,
                "blockedByGroup": blocked,
                "redundant": bool(override.enabled) == group_effective and not blocked,
            }
        )
    return result


async def reset_group_features(
    session: Any, group_id: int, *, include_agent: bool = False
) -> list[str]:
    """把群功能恢复继承；默认不碰群聊 Agent 总开关（它在 Agent 工作台管）。"""

    statement = select(GroupFeature).where(GroupFeature.group_id == group_id)
    if not include_agent:
        statement = statement.where(GroupFeature.feature != "group_agent")
    rows = list((await session.execute(statement)).scalars())
    for row in rows:
        await session.delete(row)
    return [str(row.feature) for row in rows]


async def reset_member_features(session: Any, group_id: int, user_id: int) -> int:
    result = await session.execute(
        delete(UserFeature).where(
            UserFeature.group_id == group_id, UserFeature.user_id == user_id
        )
    )
    return int(result.rowcount or 0)


async def existing_members(
    session: Any, group_id: int, user_ids: list[int]
) -> set[int]:
    return {
        int(value)
        for value in (
            await session.execute(
                select(UserGroup.user_id).where(
                    UserGroup.group_id == group_id, UserGroup.user_id.in_(user_ids)
                )
            )
        ).scalars()
    }


async def existing_groups(session: Any, group_ids: list[int]) -> set[int]:
    return {
        int(value)
        for value in (
            await session.execute(
                select(BotGroup.group_id).where(BotGroup.group_id.in_(group_ids))
            )
        ).scalars()
    }


async def existing_users(session: Any, user_ids: list[int]) -> set[int]:
    return {
        int(value)
        for value in (
            await session.execute(
                select(BotUser.user_id).where(BotUser.user_id.in_(user_ids))
            )
        ).scalars()
    }


# ── 用户 ────────────────────────────────────────────────────


async def list_users_v2(
    session: Any,
    *,
    page: int,
    page_size: int,
    search: str = "",
    overrides: str = "",
    activity: str = "",
    sort: str = "recent",
) -> tuple[list[dict[str, Any]], int]:
    now = now_naive()
    base = select(BotUser.user_id)
    clause = _search_clause(search, BotUser.nickname, BotUser.user_id)
    if clause is not None:
        base = base.where(clause)
    private_override = exists().where(GlobalUserFeature.user_id == BotUser.user_id)
    member_override = exists().where(UserFeature.user_id == BotUser.user_id)
    if overrides == "private":
        base = base.where(private_override)
    elif overrides == "member":
        base = base.where(member_override)
    elif overrides == "any":
        base = base.where(or_(private_override, member_override))
    window = _activity_clause(BotUser.last_interaction_at, activity, now)
    if window is not None:
        base = base.where(window)
    total = int(
        await session.scalar(select(func.count()).select_from(base.subquery())) or 0
    )
    if sort == "affinity":
        ordered = base.order_by(BotUser.affinity.desc(), BotUser.user_id)
    elif sort == "first_seen":
        ordered = base.order_by(BotUser.first_interaction_at.desc(), BotUser.user_id)
    elif sort == "groups":
        counts = (
            select(UserGroup.user_id, func.count().label("n"))
            .group_by(UserGroup.user_id)
            .subquery()
        )
        ordered = base.outerjoin(counts, counts.c.user_id == BotUser.user_id).order_by(
            func.coalesce(counts.c.n, 0).desc(), BotUser.user_id
        )
    else:
        ordered = base.order_by(
            BotUser.last_interaction_at.is_(None),
            BotUser.last_interaction_at.desc(),
            BotUser.user_id,
        )
    ids = [
        int(value)
        for value in (
            await session.execute(
                ordered.offset((page - 1) * page_size).limit(page_size)
            )
        ).scalars()
    ]
    if not ids:
        return [], total
    users = {
        int(row.user_id): row
        for row in (
            await session.execute(select(BotUser).where(BotUser.user_id.in_(ids)))
        ).scalars()
    }
    group_counts = await _count_by(
        session,
        select(UserGroup.user_id, func.count())
        .where(UserGroup.user_id.in_(ids))
        .group_by(UserGroup.user_id),
    )
    private_counts = await _count_by(
        session,
        select(GlobalUserFeature.user_id, func.count())
        .where(GlobalUserFeature.user_id.in_(ids))
        .group_by(GlobalUserFeature.user_id),
    )
    member_counts = await _count_by(
        session,
        select(UserFeature.user_id, func.count())
        .where(UserFeature.user_id.in_(ids))
        .group_by(UserFeature.user_id),
    )
    result = []
    for user_id in ids:
        row = users[user_id]
        result.append(
            {
                "userId": str(user_id),
                "nickname": row.nickname,
                "firstInteractionAt": iso(row.first_interaction_at),
                "lastInteractionAt": iso(row.last_interaction_at),
                "affinity": int(row.affinity or 0),
                "affinityLevel": level_for(row.affinity).name,
                "groupCount": group_counts.get(user_id, 0),
                "overrideCount": private_counts.get(user_id, 0),
                "memberOverrideCount": member_counts.get(user_id, 0),
            }
        )
    return result, total


async def users_summary(session: Any, paused: int) -> dict[str, Any]:
    now = now_naive()
    today = checkin_today()

    async def count(statement: Any) -> int:
        return int(await session.scalar(statement) or 0)

    return {
        "total": await count(select(func.count()).select_from(BotUser)),
        "active24h": await count(
            select(func.count())
            .select_from(BotUser)
            .where(BotUser.last_interaction_at >= now - _WINDOWS["24h"])
        ),
        "active7d": await count(
            select(func.count())
            .select_from(BotUser)
            .where(BotUser.last_interaction_at >= now - _WINDOWS["7d"])
        ),
        "withOverrides": await count(
            select(func.count(func.distinct(GlobalUserFeature.user_id)))
        ),
        "withMemberOverrides": await count(
            select(func.count(func.distinct(UserFeature.user_id)))
        ),
        "checkinsToday": await count(
            select(func.count(func.distinct(CheckinUser.user_id))).where(
                CheckinUser.last_checkin_date == today
            )
        ),
        "privateAgentPaused": paused,
    }


async def user_profile(session: Any, user: Any) -> dict[str, Any]:
    """用户档案：基本信息 + 好感等级 + 所在的每个群的身份、签到、群好感与覆盖。"""

    today = checkin_today()
    user_id = int(user.user_id)
    level = level_for(user.affinity)
    points = int(user.affinity or 0)
    rows = (
        await session.execute(
            select(UserGroup, BotGroup, CheckinUser)
            .join(BotGroup, BotGroup.group_id == UserGroup.group_id)
            .outerjoin(
                CheckinUser,
                and_(
                    CheckinUser.group_id == UserGroup.group_id,
                    CheckinUser.user_id == UserGroup.user_id,
                ),
            )
            .where(UserGroup.user_id == user_id)
            .order_by(
                UserGroup.last_seen_at.is_(None),
                UserGroup.last_seen_at.desc(),
                UserGroup.group_id,
            )
        )
    ).all()
    group_ids = [int(membership.group_id) for membership, _group, _summary in rows]
    override_counts: dict[int, int] = {}
    configs: dict[int, Any] = {}
    agent_flags: dict[int, bool] = {}
    if group_ids:
        override_counts = await _count_by(
            session,
            select(UserFeature.group_id, func.count())
            .where(UserFeature.user_id == user_id, UserFeature.group_id.in_(group_ids))
            .group_by(UserFeature.group_id),
        )
        configs = {
            int(row.group_id): row
            for row in (
                await session.execute(
                    select(GroupAgentConfig).where(
                        GroupAgentConfig.group_id.in_(group_ids)
                    )
                )
            ).scalars()
        }
        agent_flags = {
            int(row.group_id): bool(row.enabled)
            for row in (
                await session.execute(
                    select(GroupFeature).where(
                        GroupFeature.group_id.in_(group_ids),
                        GroupFeature.feature == "group_agent",
                    )
                )
            ).scalars()
        }
    private_overrides = int(
        await session.scalar(
            select(func.count())
            .select_from(GlobalUserFeature)
            .where(GlobalUserFeature.user_id == user_id)
        )
        or 0
    )
    memberships = []
    for membership, group, summary in rows:
        group_id = int(membership.group_id)
        cfg = configs.get(group_id)
        last = summary.last_checkin_date if summary else None
        memberships.append(
            {
                "groupId": str(group_id),
                "groupName": group.group_name,
                "groupNickname": membership.group_nickname,
                "role": membership.role,
                "title": membership.title,
                "active": membership.is_active,
                "firstSeenAt": iso(membership.first_seen_at),
                "lastSeenAt": iso(membership.last_seen_at),
                "groupAffinity": int(membership.group_affinity or 0),
                "streakDays": _effective_streak(
                    last, int(summary.streak_days or 0) if summary else 0, today
                ),
                "totalDays": int(summary.total_days or 0) if summary else 0,
                "checkedToday": last == today,
                "overrideCount": override_counts.get(group_id, 0),
                "agentEnabled": bool(
                    (cfg.enabled if cfg is not None else True)
                    and agent_flags.get(group_id, True)
                ),
            }
        )
    return {
        "userId": str(user_id),
        "nickname": user.nickname,
        "firstInteractionAt": iso(user.first_interaction_at),
        "lastInteractionAt": iso(user.last_interaction_at),
        "affinity": points,
        "affinityLevel": level.name,
        "level": {
            "name": level.name,
            "nextLevel": level.next_name,
            "toNext": level.to_next(points),
            "progress": level.progress(points),
        },
        "groupCount": len(memberships),
        "checkedTodayCount": sum(1 for item in memberships if item["checkedToday"]),
        "privateOverrides": private_overrides,
        "memberOverrides": sum(override_counts.values()),
        "memberships": memberships,
    }
