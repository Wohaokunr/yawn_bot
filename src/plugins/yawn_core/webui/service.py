# ruff: noqa: FBT001,TC001,TC002,TID252
"""WebUI 查询与写入服务。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from nonebot import get_bots
from nonebot_plugin_orm import get_session
from sqlalchemy import String, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..data_models.agent_audit import AgentAudit
from ..data_models.agent_memory import AgentMemory, AgentPrivacy, AgentRelation
from ..data_models.bot_group import BotGroup
from ..data_models.bot_user import BotUser
from ..data_models.global_user_feature import GlobalUserFeature
from ..data_models.group_agent_config import GroupAgentConfig
from ..data_models.group_agent_message import GroupAgentMessage
from ..data_models.group_feature import GroupFeature
from ..data_models.user_feature import UserFeature
from ..data_models.user_group import UserGroup
from ..data_models.web_admin_audit import WebAdminAudit
from ..metrics import snapshot_metrics
from ..permission import FEATURE_REGISTRY
from ..yawn_agent.memory import is_memory_compacting
from ..yawn_agent.persona import PERSONA_FIELDS, resolve_persona

TRIGGER_MODES = frozenset(
    {"mention_only", "mention_or_reply", "explicit_wakeup", "mention_or_proactive"}
)
ADMIN_TOOLS = frozenset({"mute_member", "create_group_announcement"})


# 全库约定 naive datetime 为北京时间（UTC+8），序列化时必须按此时区标注，
# 否则前端会把库里的北京时间当作 UTC 显示，整体偏移 8 小时。
BEIJING_TZ = timezone(timedelta(hours=8))


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=BEIJING_TZ)
    return value.isoformat()


def version(value: datetime | None) -> str | None:
    return iso(value)


def page_meta(page: int, page_size: int, total: int) -> dict[str, int]:
    return {"page": page, "pageSize": page_size, "total": total}


async def overview() -> dict[str, Any]:
    async with get_session() as session:
        group_count = int(
            await session.scalar(select(func.count()).select_from(BotGroup)) or 0
        )
        user_count = int(
            await session.scalar(select(func.count()).select_from(BotUser)) or 0
        )
        enabled_agents = int(
            await session.scalar(
                select(func.count())
                .select_from(GroupAgentConfig)
                .where(GroupAgentConfig.enabled.is_(True))
            )
            or 0
        )
        recent = list(
            (
                await session.execute(
                    select(AgentAudit).order_by(AgentAudit.id.desc()).limit(5)
                )
            )
            .scalars()
            .all()
        )
    from .. import get_sub_plugin_load_report

    report = get_sub_plugin_load_report()
    wanted = {"群聊 Agent", "狼人杀", "跑团", "番茄小说"}
    plugins = [
        {"name": "Core", "state": "loaded", "detail": None},
        *[
            {"name": item.label, "state": item.state, "detail": item.detail}
            for item in report
            if item.label in wanted
        ],
    ]
    return {
        "bots": [str(bot_id) for bot_id in sorted(get_bots())],
        "plugins": plugins,
        "counts": {
            "groups": group_count,
            "users": user_count,
            "enabledAgents": enabled_agents,
        },
        "recentAgentActions": [serialize_agent_audit(row) for row in recent],
        "metrics": snapshot_metrics(),
        "generatedAt": datetime.now(BEIJING_TZ).isoformat(),
    }


async def list_groups(
    session: AsyncSession, *, page: int, page_size: int, search: str
) -> tuple[list[dict[str, Any]], int]:
    conditions = []
    if search:
        pattern = f"%{search}%"
        conditions.append(
            or_(
                BotGroup.group_name.ilike(pattern),
                BotGroup.group_id.cast(String).like(pattern),
            )
        )
    count_stmt = select(func.count()).select_from(BotGroup)
    stmt = select(BotGroup).order_by(BotGroup.last_active_at.desc(), BotGroup.group_id)
    if conditions:
        count_stmt = count_stmt.where(*conditions)
        stmt = stmt.where(*conditions)
    total = int(await session.scalar(count_stmt) or 0)
    rows = list(
        (await session.execute(stmt.offset((page - 1) * page_size).limit(page_size)))
        .scalars()
        .all()
    )
    group_ids = [row.group_id for row in rows]
    member_counts: dict[int, int] = {}
    configs: dict[int, GroupAgentConfig] = {}
    if group_ids:
        member_counts = {
            int(group_id): int(count)
            for group_id, count in (
                await session.execute(
                    select(UserGroup.group_id, func.count())
                    .where(UserGroup.group_id.in_(group_ids))
                    .group_by(UserGroup.group_id)
                )
            ).all()
        }
        configs = {
            row.group_id: row
            for row in (
                await session.execute(
                    select(GroupAgentConfig).where(
                        GroupAgentConfig.group_id.in_(group_ids)
                    )
                )
            )
            .scalars()
            .all()
        }
    return [
        {
            "groupId": str(row.group_id),
            "groupName": row.group_name,
            "firstSeenAt": iso(row.first_seen_at),
            "lastActiveAt": iso(row.last_active_at),
            "memberCount": member_counts.get(row.group_id, 0),
            "agentEnabled": configs[row.group_id].enabled
            if row.group_id in configs
            else True,
        }
        for row in rows
    ], total


async def get_group(session: AsyncSession, group_id: int) -> dict[str, Any] | None:
    row = await session.get(BotGroup, group_id)
    if row is None:
        return None
    member_count = int(
        await session.scalar(
            select(func.count())
            .select_from(UserGroup)
            .where(UserGroup.group_id == group_id)
        )
        or 0
    )
    return {
        "groupId": str(row.group_id),
        "groupName": row.group_name,
        "firstSeenAt": iso(row.first_seen_at),
        "lastActiveAt": iso(row.last_active_at),
        "memberCount": member_count,
        "features": await group_feature_rows(session, group_id),
    }


async def list_group_members(
    session: AsyncSession, group_id: int, *, page: int, page_size: int, search: str
) -> tuple[list[dict[str, Any]], int]:
    stmt = (
        select(UserGroup, BotUser)
        .join(BotUser, BotUser.user_id == UserGroup.user_id)
        .where(UserGroup.group_id == group_id)
    )
    count_stmt = (
        select(func.count())
        .select_from(UserGroup)
        .where(UserGroup.group_id == group_id)
    )
    if search:
        pattern = f"%{search}%"
        clause = or_(
            BotUser.nickname.ilike(pattern),
            UserGroup.group_nickname.ilike(pattern),
            BotUser.user_id.cast(String).like(pattern),
        )
        stmt = stmt.where(clause)
        count_stmt = count_stmt.join(
            BotUser, BotUser.user_id == UserGroup.user_id
        ).where(clause)
    total = int(await session.scalar(count_stmt) or 0)
    rows = (
        await session.execute(
            stmt.order_by(UserGroup.last_seen_at.desc(), UserGroup.user_id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return [
        {
            "userId": str(user.user_id),
            "nickname": user.nickname,
            "groupNickname": membership.group_nickname,
            "role": membership.role,
            "title": membership.title,
            "lastSeenAt": iso(membership.last_seen_at),
            "active": membership.is_active,
        }
        for membership, user in rows
    ], total


async def list_users(
    session: AsyncSession, *, page: int, page_size: int, search: str
) -> tuple[list[dict[str, Any]], int]:
    stmt = select(BotUser).order_by(BotUser.last_interaction_at.desc(), BotUser.user_id)
    count_stmt = select(func.count()).select_from(BotUser)
    if search:
        pattern = f"%{search}%"
        clause = or_(
            BotUser.nickname.ilike(pattern),
            BotUser.user_id.cast(String).like(pattern),
        )
        stmt = stmt.where(clause)
        count_stmt = count_stmt.where(clause)
    total = int(await session.scalar(count_stmt) or 0)
    rows = list(
        (await session.execute(stmt.offset((page - 1) * page_size).limit(page_size)))
        .scalars()
        .all()
    )
    return [
        {
            "userId": str(row.user_id),
            "nickname": row.nickname,
            "firstInteractionAt": iso(row.first_interaction_at),
            "lastInteractionAt": iso(row.last_interaction_at),
            "affinity": row.affinity,
        }
        for row in rows
    ], total


async def group_feature_rows(
    session: AsyncSession, group_id: int
) -> list[dict[str, Any]]:
    overrides = {
        row.feature: row
        for row in (
            await session.execute(
                select(GroupFeature).where(GroupFeature.group_id == group_id)
            )
        )
        .scalars()
        .all()
    }
    return [
        {
            "key": key,
            "name": name,
            "override": overrides[key].enabled if key in overrides else None,
            "effective": overrides[key].enabled if key in overrides else True,
            "source": "group" if key in overrides else "default",
        }
        for key, name in FEATURE_REGISTRY.items()
    ]


async def user_feature_rows(
    session: AsyncSession, user_id: int, group_id: int | None
) -> list[dict[str, Any]]:
    if group_id is None:
        overrides = {
            row.feature: row
            for row in (
                await session.execute(
                    select(GlobalUserFeature).where(
                        GlobalUserFeature.user_id == user_id
                    )
                )
            )
            .scalars()
            .all()
        }
        return [
            {
                "key": key,
                "name": name,
                "override": overrides[key].enabled if key in overrides else None,
                "effective": overrides[key].enabled if key in overrides else True,
                "source": "global_user" if key in overrides else "default",
            }
            for key, name in FEATURE_REGISTRY.items()
        ]
    group_overrides = {
        row.feature: row
        for row in (
            await session.execute(
                select(GroupFeature).where(GroupFeature.group_id == group_id)
            )
        )
        .scalars()
        .all()
    }
    overrides = {
        row.feature: row
        for row in (
            await session.execute(
                select(UserFeature).where(
                    UserFeature.group_id == group_id, UserFeature.user_id == user_id
                )
            )
        )
        .scalars()
        .all()
    }
    result = []
    for key, name in FEATURE_REGISTRY.items():
        user_row = overrides.get(key)
        group_row = group_overrides.get(key)
        effective = (
            user_row.enabled if user_row else group_row.enabled if group_row else True
        )
        source = "user" if user_row else "group" if group_row else "default"
        result.append(
            {
                "key": key,
                "name": name,
                "override": user_row.enabled if user_row else None,
                "effective": effective,
                "source": source,
            }
        )
    return result


async def set_group_feature(
    session: AsyncSession, group_id: int, feature: str, override: bool | None
) -> None:
    row = await session.get(GroupFeature, {"group_id": group_id, "feature": feature})
    if override is None:
        if row is not None:
            await session.delete(row)
        return
    if row is None:
        session.add(GroupFeature(group_id=group_id, feature=feature, enabled=override))
    else:
        row.enabled = override


async def set_user_feature(
    session: AsyncSession,
    user_id: int,
    feature: str,
    override: bool | None,
    *,
    group_id: int | None,
) -> None:
    if group_id is None:
        row = await session.get(
            GlobalUserFeature, {"user_id": user_id, "feature": feature}
        )
        if override is None:
            if row is not None:
                await session.delete(row)
        elif row is None:
            session.add(
                GlobalUserFeature(user_id=user_id, feature=feature, enabled=override)
            )
        else:
            row.enabled = override
        return
    row = await session.get(
        UserFeature, {"group_id": group_id, "user_id": user_id, "feature": feature}
    )
    if override is None:
        if row is not None:
            await session.delete(row)
    elif row is None:
        session.add(
            UserFeature(
                group_id=group_id, user_id=user_id, feature=feature, enabled=override
            )
        )
    else:
        row.enabled = override


def serialize_agent_config(
    row: GroupAgentConfig | None, group_id: int
) -> dict[str, Any]:
    if row is None:
        row = GroupAgentConfig(group_id=group_id)
    return {
        "groupId": str(group_id),
        "enabled": row.enabled,
        "triggerMode": row.trigger_mode,
        "proactiveProbability": row.proactive_probability,
        "proactiveActiveEnabled": row.proactive_active_enabled,
        "proactiveActiveProbability": row.proactive_active_probability,
        "proactiveActiveWindowMinutes": row.proactive_active_window_minutes,
        "idleThresholdMinutes": row.idle_threshold_minutes,
        "cooldownMinutes": row.cooldown_minutes,
        "dailyLimit": row.daily_limit,
        "rawRetentionDays": row.raw_retention_days,
        "crossGroupVisibility": row.cross_group_visibility,
        "mediaCacheEnabled": row.media_cache_enabled,
        "adminToolDailyLimit": row.admin_tool_daily_limit,
        "toolAllowlist": list(row.tool_allowlist or []),
        "proactiveToday": row.proactive_count,
        "adminToolsToday": row.admin_tool_count,
        "version": version(row.updated_at),
    }


def serialize_persona(row: GroupAgentConfig | None, group_id: int) -> dict[str, Any]:
    return {
        "groupId": str(group_id),
        "enabled": row.persona_enabled if row else True,
        "resolved": resolve_persona(row),
        "overrides": dict(row.persona_override or {}) if row else {},
        "fields": list(PERSONA_FIELDS),
        "version": version(row.updated_at) if row else None,
    }


def serialize_memory(row: AgentMemory) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "groupId": str(row.group_id) if row.group_id is not None else None,
        "subjectUserId": str(row.subject_user_id)
        if int(row.subject_user_id or 0) != 0
        else None,
        "scope": row.scope,
        "type": row.memory_type,
        "key": row.memory_key,
        "content": row.content,
        "sourceKind": row.source_kind,
        "relatedUserIds": [str(value) for value in row.related_user_ids or []],
        "salience": row.salience,
        "confidence": row.confidence,
        "visibility": row.visibility,
        "createdAt": iso(row.created_at),
        "updatedAt": iso(row.updated_at),
        "expiresAt": iso(row.expires_at),
    }


def serialize_relation(row: AgentRelation) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "groupId": str(row.group_id),
        "subjectUserId": str(row.subject_user_id),
        "objectUserId": str(row.object_user_id),
        "type": row.relation_type,
        "sourceKind": row.source_kind,
        "note": row.note,
        "confidence": row.confidence,
        "evidenceCount": row.evidence_count,
        "lastSeenAt": iso(row.last_seen_at),
    }


# 图谱端点与导出端点同口径：全量边上限 5000，超出时以 meta.truncated 告知前端。
RELATION_GRAPH_LIMIT = 5000


async def load_relation_graph(
    session: AsyncSession, group_id: int
) -> dict[str, Any]:
    """关系图谱数据：全群边 + 成员节点（含无边成员，linked 标记是否出现在边中）。"""

    opted_out = set(
        (
            await session.execute(
                select(AgentPrivacy.user_id).where(
                    AgentPrivacy.group_id == group_id,
                    AgentPrivacy.opted_out.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    relation_clauses = [AgentRelation.group_id == group_id]
    if opted_out:
        relation_clauses.extend(
            (
                AgentRelation.subject_user_id.not_in(opted_out),
                AgentRelation.object_user_id.not_in(opted_out),
            )
        )
    relation_rows = list(
        (
            await session.execute(
                select(AgentRelation)
                .where(*relation_clauses)
                .order_by(AgentRelation.id)
                .limit(RELATION_GRAPH_LIMIT + 1)
            )
        )
        .scalars()
        .all()
    )
    relation_truncated = len(relation_rows) > RELATION_GRAPH_LIMIT
    if relation_truncated:
        relation_rows = relation_rows[:RELATION_GRAPH_LIMIT]

    degrees: dict[int, int] = {}
    for row in relation_rows:
        degrees[int(row.subject_user_id)] = (
            degrees.get(int(row.subject_user_id), 0) + 1
        )
        degrees[int(row.object_user_id)] = degrees.get(int(row.object_user_id), 0) + 1

    member_rows = list(
        (
            await session.execute(
                select(UserGroup, BotUser)
                .join(BotUser, BotUser.user_id == UserGroup.user_id)
                .where(UserGroup.group_id == group_id)
                .order_by(UserGroup.user_id)
                .limit(RELATION_GRAPH_LIMIT + 1)
            )
        )
        .all()
    )
    member_truncated = len(member_rows) > RELATION_GRAPH_LIMIT
    if member_truncated:
        member_rows = member_rows[:RELATION_GRAPH_LIMIT]

    nodes: dict[int, dict[str, Any]] = {}
    for membership, user in member_rows:
        nodes[int(user.user_id)] = {
            "userId": str(user.user_id),
            "nickname": user.nickname,
            "groupNickname": membership.group_nickname,
            "role": membership.role,
            "linked": int(user.user_id) in degrees,
            "degree": degrees.get(int(user.user_id), 0),
        }
    # 关系端点可能已不在成员表（退群残留），补齐为昵称回退节点，避免边悬空。
    for user_id, degree in degrees.items():
        if user_id not in nodes:
            nodes[user_id] = {
                "userId": str(user_id),
                "nickname": "",
                "groupNickname": None,
                "role": "member",
                "linked": True,
                "degree": degree,
            }

    return {
        "nodes": [nodes[key] for key in sorted(nodes)],
        "edges": [serialize_relation(row) for row in relation_rows],
        "meta": {
            "relationTruncated": relation_truncated,
            "memberTruncated": member_truncated,
        },
    }


def serialize_agent_message(row: GroupAgentMessage) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "groupId": str(row.group_id),
        "userId": str(row.user_id),
        "senderName": row.sender_name,
        "role": row.role,
        "title": row.title,
        "text": row.normalized_text,
        "receivedAt": iso(row.received_at),
        "expiresAt": iso(row.expires_at),
    }


async def agent_memory_status(session: AsyncSession, group_id: int) -> dict[str, Any]:
    """记忆治理状态：未整理消息量、整理游标与按类型的记忆计数。"""

    now = datetime.now(BEIJING_TZ).replace(tzinfo=None)
    config = await session.get(GroupAgentConfig, group_id)
    cursor = int(config.last_compacted_message_id or 0) if config else 0
    # 与整理取数口径一致：过期但未整理的消息仍算待整理（purge 会保留
    # 到硬上限），状态页不得把它们显示成"已无积压"。
    pending_clauses = [
        GroupAgentMessage.group_id == group_id,
        GroupAgentMessage.id > cursor,
    ]
    pending = int(
        await session.scalar(
            select(func.count()).select_from(GroupAgentMessage).where(*pending_clauses)
        )
        or 0
    )
    last_compacted_at = None
    if cursor > 0:
        last_compacted_at = await session.scalar(
            select(GroupAgentMessage.received_at).where(GroupAgentMessage.id == cursor)
        )
    memory_clauses = [
        AgentMemory.group_id == group_id,
        AgentMemory.visibility.in_(("group", "public")),
        (AgentMemory.expires_at.is_(None) | (AgentMemory.expires_at >= now)),
    ]
    type_rows = (
        await session.execute(
            select(AgentMemory.memory_type, func.count())
            .where(*memory_clauses)
            .group_by(AgentMemory.memory_type)
        )
    ).all()
    counts_by_type = {str(row[0]): int(row[1] or 0) for row in type_rows}
    return {
        "groupId": str(group_id),
        "pendingMessages": pending,
        "lastCompactedMessageId": cursor or None,
        "lastCompactedAt": iso(last_compacted_at),
        "countsByType": counts_by_type,
        "total": sum(counts_by_type.values()),
        "oldestUpdatedAt": iso(
            await session.scalar(
                select(func.min(AgentMemory.updated_at)).where(*memory_clauses)
            )
        ),
        "newestUpdatedAt": iso(
            await session.scalar(
                select(func.max(AgentMemory.updated_at)).where(*memory_clauses)
            )
        ),
        "rebuildRequired": bool(config and config.memory_rebuild_required),
        "lastAttemptAt": iso(config.memory_last_attempt_at if config else None),
        "lastSuccessAt": iso(config.memory_last_success_at if config else None),
        "lastError": config.memory_last_error if config else None,
        "consecutiveFailures": int(
            config.memory_consecutive_failures if config else 0
        ),
        "inFlight": is_memory_compacting(group_id),
    }


def serialize_agent_audit(row: AgentAudit) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "groupId": str(row.group_id),
        "actorUserId": str(row.actor_user_id)
        if row.actor_user_id is not None
        else None,
        "toolName": row.tool_name,
        "arguments": row.arguments,
        "result": row.result,
        "detail": row.detail,
        "createdAt": iso(row.created_at),
    }


def serialize_web_audit(row: WebAdminAudit) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "requestId": row.request_id,
        "actorSession": row.actor_session,
        "action": row.action,
        "resourceType": row.resource_type,
        "resourceId": row.resource_id,
        "result": row.result,
        "detail": row.detail,
        "createdAt": iso(row.created_at),
    }


async def delete_one_memory(
    session: AsyncSession, group_id: int, memory_id: int
) -> int:
    row = await session.scalar(
        select(AgentMemory).where(
            AgentMemory.group_id == group_id,
            AgentMemory.id == memory_id,
        )
    )
    if row is None:
        return 0
    await session.delete(row)
    return 1
