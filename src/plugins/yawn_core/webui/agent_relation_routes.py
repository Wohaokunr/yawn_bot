# ruff: noqa: F401, FAST002, TC001, TID252
"""Split Agent WebUI route module."""


from __future__ import annotations

import asyncio
import json
import time
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, status
from nonebot import get_bots, logger
from nonebot_plugin_orm import get_session
from sqlalchemy import BigInteger, cast, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from ..data_models.agent_memory import AgentMemory, AgentPrivacy, AgentRelation
from ..data_models.bot_user import BotUser
from ..data_models.group_agent_config import GroupAgentConfig
from ..data_models.group_agent_message import GroupAgentMessage
from ..data_models.user_group import UserGroup
from ..llm import (
    ai_config,
    complete_with_tools_result,
    resolve_llm_request,
    resolve_provider,
)
from ..yawn_agent.capabilities import (
    BotGroupCapabilities,
    capability_runtime_snapshot,
    clear_group_capability_cache,
    get_segment_capabilities,
    probe_group_capabilities,
    user_can_manage_group,
)
from ..yawn_agent.config_store import agent_runtime_enabled, set_agent_runtime_enabled
from ..yawn_agent.context import build_current_turn, now_beijing, trim_context_messages
from ..yawn_agent.conversation import close_group_conversations
from ..yawn_agent.dialogue import _history_message_meta, _load_context
from ..yawn_agent.emotion import emotion_context_state, emotion_public_state
from ..yawn_agent.execution_trace import (
    begin_execution_trace,
    execution_trace_by_id,
    finish_execution_trace,
    recent_execution_trace_summaries,
    trace_event,
)
from ..yawn_agent.memory import (
    compact_group_memory,
    delete_group_memories,
    delete_member_memories,
    is_memory_compacting,
    normalize_relation_type,
    rebuild_group_memories,
    record_memory_failure,
)
from ..yawn_agent.proactive import _build_user_prompt, _decide_proactive_reply
from ..yawn_agent.prompt import PROMPT_VERSION, build_messages
from ..yawn_agent.tools import (
    build_tool_schemas,
    dialogue_tool_round_limit,
    select_dialogue_message_segment_types,
    select_dialogue_tool_names,
    tool_permission_snapshot,
)
from .agent_route_helpers import is_guest_view as _is_guest_view
from .agent_route_helpers import memory_privacy_clauses as _memory_privacy_clauses
from .config import API_PATH
from .deps import AdminReadSession, AdminWriteSession, GroupViewSession, ok, page_params
from .hub import hub
from .route_helpers import check_version, require_group
from .route_models import (
    AgentConfigPatch,
    AgentDebugRunBody,
    MemoryCreateBody,
    MemoryPatchBody,
    PersonaPatch,
    PrivacyPatchBody,
    RelationBatchDeleteBody,
    RelationCreateBody,
    RelationPatchBody,
)
from .service import (
    RELATION_GRAPH_LIMIT,
    agent_diagnostics,
    agent_memory_status,
    delete_one_memory,
    group_feature_rows,
    iso,
    load_relation_graph,
    page_meta,
    serialize_agent_config,
    serialize_agent_message,
    serialize_guest_memory,
    serialize_guest_relation,
    serialize_memory,
    serialize_persona,
    serialize_relation,
)

router = APIRouter(prefix=API_PATH)

# 低置信阈值与前端“待复核”口径一致。
LOW_CONFIDENCE_THRESHOLD = 0.4


async def _relation_clauses(db: Any, group_id: int) -> list[Any]:
    """Return the relation visibility clauses shared by list/summary/types.

    opted-out members are excluded for both admin and guest relation views so the
    lightweight summary and type filters always describe the same visible rows as
    the paginated table.
    """

    opted_out = set(
        (
            await db.execute(
                select(AgentPrivacy.user_id).where(
                    AgentPrivacy.group_id == group_id,
                    AgentPrivacy.opted_out.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    clauses: list[Any] = [AgentRelation.group_id == group_id]
    if opted_out:
        clauses.extend(
            (
                AgentRelation.subject_user_id.not_in(opted_out),
                AgentRelation.object_user_id.not_in(opted_out),
            )
        )
    return clauses


async def _relation_page_names(
    db: Any, group_id: int, rows: list[AgentRelation]
) -> dict[int, str]:
    """Resolve display names only for relation endpoints on the current page."""

    user_ids = {
        int(user_id)
        for row in rows
        for user_id in (row.subject_user_id, row.object_user_id)
    }
    if not user_ids:
        return {}
    members = (
        await db.execute(
            select(UserGroup, BotUser)
            .join(BotUser, BotUser.user_id == UserGroup.user_id)
            .where(
                UserGroup.group_id == group_id,
                UserGroup.user_id.in_(user_ids),
            )
        )
    ).all()
    return {
        int(user.user_id): (
            membership.group_nickname or user.nickname or str(user.user_id)
        )
        for membership, user in members
    }


# 列表可排序字段白名单；id 作为稳定次序兜底，翻页时不跳行。
_RELATION_SORTS: dict[str, Any] = {
    "confidence": AgentRelation.confidence,
    "lastSeen": AgentRelation.last_seen_at,
    "evidence": AgentRelation.evidence_count,
    "type": AgentRelation.relation_type,
}


def _relation_order(sort: str, order: str) -> list[Any]:
    column = _RELATION_SORTS.get(sort, AgentRelation.confidence)
    if order == "asc":
        return [column.asc(), AgentRelation.id.asc()]
    return [column.desc(), AgentRelation.id.desc()]


async def _relation_name_matches(db: Any, group_id: int, keyword: str) -> list[int]:
    """按群昵称/QQ 昵称模糊匹配成员，供关系列表按名字搜索。"""

    pattern = f"%{keyword}%"
    rows = (
        await db.execute(
            select(UserGroup.user_id)
            .join(BotUser, BotUser.user_id == UserGroup.user_id)
            .where(
                UserGroup.group_id == group_id,
                or_(
                    BotUser.nickname.ilike(pattern),
                    UserGroup.group_nickname.ilike(pattern),
                ),
            )
            .limit(200)
        )
    ).scalars()
    return [int(user_id) for user_id in rows]


async def _relation_page_mutual(
    db: Any, group_id: int, rows: list[AgentRelation]
) -> set[tuple[int, int, str]]:
    """当前页每条边是否存在同类型反向边（A→B 与 B→A 互相成立）。"""

    if not rows:
        return set()
    endpoints = {
        int(user_id)
        for row in rows
        for user_id in (row.subject_user_id, row.object_user_id)
    }
    reverse = (
        await db.execute(
            select(
                AgentRelation.subject_user_id,
                AgentRelation.object_user_id,
                AgentRelation.relation_type,
            ).where(
                AgentRelation.group_id == group_id,
                AgentRelation.subject_user_id.in_(endpoints),
                AgentRelation.object_user_id.in_(endpoints),
            )
        )
    ).all()
    return {
        (int(subject_id), int(object_id), str(relation_type))
        for subject_id, object_id, relation_type in reverse
    }


@router.get("/agent/groups/{group_id}/relations")
async def get_relations(  # noqa: PLR0913, PLR0917
    group_id: int,
    _session: GroupViewSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=100),
    search: str = Query(default="", max_length=24),
    relation_type: str = Query(default="", alias="type", max_length=32),
    source: str = Query(default="", max_length=16),
    member: int | None = Query(default=None, gt=0),
    min_confidence: float | None = Query(
        default=None, alias="minConfidence", ge=0, le=1
    ),
    max_confidence: float | None = Query(
        default=None, alias="maxConfidence", ge=0, le=1
    ),
    sort: str = Query(default="confidence", max_length=16),
    order: str = Query(default="desc", max_length=4),
) -> dict[str, Any]:
    page, page_size = page_params(page, page_size)
    guest = _is_guest_view(_session)
    async with get_session() as db:
        await require_group(db, group_id)
        clauses = await _relation_clauses(db, group_id)
        if relation_type:
            clauses.append(AgentRelation.relation_type == relation_type)
        # 来源属于管理员投影字段，访客视图忽略该过滤以免借此探测来源。
        if source and not guest:
            clauses.append(AgentRelation.source_kind == source)
        if min_confidence is not None:
            clauses.append(AgentRelation.confidence >= min_confidence)
        if max_confidence is not None:
            clauses.append(AgentRelation.confidence <= max_confidence)
        if member is not None:
            clauses.append(
                or_(
                    AgentRelation.subject_user_id == member,
                    AgentRelation.object_user_id == member,
                )
            )
        keyword = search.strip()
        if keyword.isdigit():
            target = int(keyword)
            clauses.append(
                or_(
                    AgentRelation.subject_user_id == target,
                    AgentRelation.object_user_id == target,
                )
            )
        elif keyword:
            matched = await _relation_name_matches(db, group_id, keyword)
            if not matched:
                return ok([], page_meta(page, page_size, 0))
            clauses.append(
                or_(
                    AgentRelation.subject_user_id.in_(matched),
                    AgentRelation.object_user_id.in_(matched),
                )
            )
        total = int(
            await db.scalar(
                select(func.count()).select_from(AgentRelation).where(*clauses)
            )
            or 0
        )
        rows = list(
            (
                await db.execute(
                    select(AgentRelation)
                    .where(*clauses)
                    .order_by(*_relation_order(sort, order))
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        names = await _relation_page_names(db, group_id, rows)
        mutual = await _relation_page_mutual(db, group_id, rows)
    serializer = serialize_guest_relation if guest else serialize_relation
    data: list[dict[str, Any]] = []
    for row in rows:
        item = serializer(row)
        subject_id = int(row.subject_user_id)
        object_id = int(row.object_user_id)
        item["subjectName"] = names.get(subject_id, str(subject_id))
        item["objectName"] = names.get(object_id, str(object_id))
        item["mutual"] = (object_id, subject_id, row.relation_type) in mutual
        data.append(item)
    return ok(data, page_meta(page, page_size, total))


@router.get("/agent/groups/{group_id}/relations/summary")
async def get_relation_summary(
    group_id: int, _session: GroupViewSession
) -> dict[str, Any]:
    """Small aggregate used by the table view without loading the full graph."""

    async with get_session() as db:
        await require_group(db, group_id)
        clauses = await _relation_clauses(db, group_id)
        edge_count, last_seen_at = (
            await db.execute(
                select(
                    func.count(AgentRelation.id),
                    func.max(AgentRelation.last_seen_at),
                ).where(*clauses)
            )
        ).one()
        endpoint_ids = (
            select(AgentRelation.subject_user_id.label("user_id"))
            .where(*clauses)
            .union_all(
                select(AgentRelation.object_user_id.label("user_id")).where(*clauses)
            )
            .subquery()
        )
        linked_member_count = int(
            await db.scalar(select(func.count(func.distinct(endpoint_ids.c.user_id))))
            or 0
        )
        type_rows = (
            await db.execute(
                select(AgentRelation.relation_type, func.count(AgentRelation.id))
                .where(*clauses)
                .group_by(AgentRelation.relation_type)
                .order_by(
                    func.count(AgentRelation.id).desc(),
                    AgentRelation.relation_type,
                )
            )
        ).all()
        now = now_beijing()
        avg_confidence, fresh_count, stale_count, low_count = (
            await db.execute(
                select(
                    func.avg(AgentRelation.confidence),
                    func.count(AgentRelation.id).filter(
                        AgentRelation.last_seen_at >= now - timedelta(days=7)
                    ),
                    func.count(AgentRelation.id).filter(
                        AgentRelation.last_seen_at < now - timedelta(days=60)
                    ),
                    func.count(AgentRelation.id).filter(
                        AgentRelation.confidence < LOW_CONFIDENCE_THRESHOLD
                    ),
                ).where(*clauses)
            )
        ).one()
        reverse = aliased(AgentRelation)
        mutual_count = int(
            await db.scalar(
                select(func.count(AgentRelation.id)).where(
                    *clauses,
                    exists().where(
                        reverse.group_id == AgentRelation.group_id,
                        reverse.subject_user_id == AgentRelation.object_user_id,
                        reverse.object_user_id == AgentRelation.subject_user_id,
                        reverse.relation_type == AgentRelation.relation_type,
                    ),
                )
            )
            or 0
        )
        source_rows = (
            []
            if _is_guest_view(_session)
            else (
                await db.execute(
                    select(AgentRelation.source_kind, func.count(AgentRelation.id))
                    .where(*clauses)
                    .group_by(AgentRelation.source_kind)
                    .order_by(func.count(AgentRelation.id).desc())
                )
            ).all()
        )
    result: dict[str, Any] = {
        "edgeCount": int(edge_count or 0),
        "linkedMemberCount": linked_member_count,
        "typeCounts": [
            {"type": str(relation_type), "count": int(count)}
            for relation_type, count in type_rows
        ],
        "lastSeenAt": iso(last_seen_at),
        "avgConfidence": (
            round(float(avg_confidence), 4) if avg_confidence is not None else None
        ),
        "mutualEdgeCount": mutual_count,
        "freshEdgeCount": int(fresh_count or 0),
        "staleEdgeCount": int(stale_count or 0),
        "lowConfidenceCount": int(low_count or 0),
    }
    if not _is_guest_view(_session):
        result["sourceCounts"] = [
            {"source": str(source_kind), "count": int(count)}
            for source_kind, count in source_rows
        ]
    return ok(result)


@router.get("/agent/groups/{group_id}/relations/graph")
async def get_relation_graph(
    group_id: int, _session: GroupViewSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        return ok(
            await load_relation_graph(db, group_id, guest=_is_guest_view(_session))
        )


@router.get("/agent/groups/{group_id}/relations/types")
async def get_relation_types(
    group_id: int, _session: GroupViewSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        clauses = await _relation_clauses(db, group_id)
        rows = (
            (
                await db.execute(
                    select(AgentRelation.relation_type)
                    .where(*clauses)
                    .distinct()
                    .order_by(AgentRelation.relation_type)
                )
            )
            .scalars()
            .all()
        )
    return ok([str(row) for row in rows])


@router.post("/agent/groups/{group_id}/relations")
async def create_relation(
    group_id: int, body: RelationCreateBody, _session: AdminWriteSession
) -> dict[str, Any]:
    relation_type = normalize_relation_type(body.type)
    if not relation_type:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "关系类型不能为空")
    if body.subject_user_id == body.object_user_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "关系两端不能是同一个人"
        )
    async with get_session() as db:
        await require_group(db, group_id)
        opted_out = set(
            (
                await db.execute(
                    select(AgentPrivacy.user_id).where(
                        AgentPrivacy.group_id == group_id,
                        AgentPrivacy.opted_out.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        if body.subject_user_id in opted_out or body.object_user_id in opted_out:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "关系一方已隐私退出，不得建立关系边",
            )
        row = AgentRelation(
            group_id=group_id,
            subject_user_id=body.subject_user_id,
            object_user_id=body.object_user_id,
            relation_type=relation_type,
            source_kind="manual",
            note=body.note.strip(),
            confidence=body.confidence,
            evidence_count=1,
            last_seen_at=now_beijing(),
        )
        db.add(row)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status.HTTP_409_CONFLICT, "这两个成员的该类型关系边已存在"
            ) from None
        await db.refresh(row)
        result = serialize_relation(row)
    await hub.notify_change("agent_relation", str(row.id), group_id=group_id)
    return ok(result)


@router.put("/agent/groups/{group_id}/relations/{relation_id}")
async def update_relation(
    group_id: int,
    relation_id: int,
    body: RelationPatchBody,
    _session: AdminWriteSession,
) -> dict[str, Any]:
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "没有可更新的字段")
    async with get_session() as db:
        await require_group(db, group_id)
        row = await db.scalar(
            select(AgentRelation).where(
                AgentRelation.group_id == group_id,
                AgentRelation.id == relation_id,
            )
        )
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "关系不存在")
        if "note" in updates:
            row.note = str(updates["note"] or "").strip()
        if updates.get("confidence") is not None:
            row.confidence = float(updates["confidence"])
        await db.commit()
        await db.refresh(row)
        result = serialize_relation(row)
    await hub.notify_change("agent_relation", str(relation_id), group_id=group_id)
    return ok(result)


@router.delete("/agent/groups/{group_id}/relations/{relation_id}")
async def delete_relation(
    group_id: int, relation_id: int, _session: AdminWriteSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        row = await db.scalar(
            select(AgentRelation).where(
                AgentRelation.group_id == group_id,
                AgentRelation.id == relation_id,
            )
        )
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "关系不存在")
        await db.delete(row)
        await db.commit()
    await hub.notify_change("agent_relation", str(relation_id), group_id=group_id)
    return ok({"deleted": 1})


@router.post("/agent/groups/{group_id}/relations/batch-delete")
async def batch_delete_relations(
    group_id: int, body: RelationBatchDeleteBody, _session: AdminWriteSession
) -> dict[str, Any]:
    """批量删除关系边；只删本群内存在的 id，其余静默忽略并在结果里计数。"""

    async with get_session() as db:
        await require_group(db, group_id)
        rows = list(
            (
                await db.execute(
                    select(AgentRelation).where(
                        AgentRelation.group_id == group_id,
                        AgentRelation.id.in_(body.ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        deleted_ids = [int(row.id) for row in rows]
        for row in rows:
            await db.delete(row)
        await db.commit()
    for relation_id in deleted_ids:
        await hub.notify_change("agent_relation", str(relation_id), group_id=group_id)
    return ok(
        {
            "deleted": len(deleted_ids),
            "missing": len(set(body.ids)) - len(deleted_ids),
        }
    )
