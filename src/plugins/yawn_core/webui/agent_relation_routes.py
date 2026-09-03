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
from ..yawn_agent.persona import (
    apply_persona_editor_profile,
    persona_behavior,
    persona_behavior_draft,
    persona_editor_profile,
    persona_summary,
    reset_persona,
    resolve_persona,
    resolve_persona_draft,
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


async def _visible_relation_clauses(
    db: Any,
    group_id: int,
    *,
    guest: bool,
) -> list[Any]:
    clauses: list[Any] = [AgentRelation.group_id == group_id]
    if not guest:
        return clauses
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
    if opted_out:
        clauses.extend(
            (
                AgentRelation.subject_user_id.not_in(opted_out),
                AgentRelation.object_user_id.not_in(opted_out),
            )
        )
    return clauses


async def _relation_member_labels(
    db: Any,
    group_id: int,
    rows: list[AgentRelation],
) -> dict[int, str]:
    """Load labels only for members referenced by the current relation page."""

    user_ids = {
        int(user_id)
        for row in rows
        for user_id in (row.subject_user_id, row.object_user_id)
    }
    if not user_ids:
        return {}
    memberships = (
        await db.execute(
            select(UserGroup.user_id, UserGroup.group_nickname, BotUser.nickname)
            .outerjoin(BotUser, BotUser.user_id == UserGroup.user_id)
            .where(
                UserGroup.group_id == group_id,
                UserGroup.user_id.in_(user_ids),
            )
        )
    ).all()
    return {
        int(user_id): (
            str(group_nickname or nickname or "").strip() or str(user_id)
        )
        for user_id, group_nickname, nickname in memberships
    }


@router.get("/agent/groups/{group_id}/relations")
async def get_relations(
    group_id: int,
    _session: GroupViewSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=100),
    search: str = Query(default="", max_length=24),
    relation_type: str = Query(default="", alias="type", max_length=32),
) -> dict[str, Any]:
    page, page_size = page_params(page, page_size)
    async with get_session() as db:
        await require_group(db, group_id)
        clauses = await _visible_relation_clauses(
            db,
            group_id,
            guest=_is_guest_view(_session),
        )
        if relation_type:
            clauses.append(AgentRelation.relation_type == relation_type)
        if search.strip().isdigit():
            target = int(search.strip())
            clauses.append(
                or_(
                    AgentRelation.subject_user_id == target,
                    AgentRelation.object_user_id == target,
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
                    .order_by(AgentRelation.confidence.desc(), AgentRelation.id.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        member_labels = await _relation_member_labels(db, group_id, rows)
    serializer = (
        serialize_guest_relation if _is_guest_view(_session) else serialize_relation
    )
    result: list[dict[str, Any]] = []
    for row in rows:
        item = serializer(row)
        subject_id = int(row.subject_user_id)
        object_id = int(row.object_user_id)
        item["subjectDisplayName"] = member_labels.get(subject_id, str(subject_id))
        item["objectDisplayName"] = member_labels.get(object_id, str(object_id))
        result.append(item)
    return ok(result, page_meta(page, page_size, total))


@router.get("/agent/groups/{group_id}/relations/summary")
async def get_relation_summary(
    group_id: int,
    _session: GroupViewSession,
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        clauses = await _visible_relation_clauses(
            db,
            group_id,
            guest=_is_guest_view(_session),
        )
        relation_count = int(
            await db.scalar(
                select(func.count()).select_from(AgentRelation).where(*clauses)
            )
            or 0
        )
        member_ids = (
            select(AgentRelation.subject_user_id.label("user_id"))
            .where(*clauses)
            .union(
                select(AgentRelation.object_user_id.label("user_id")).where(*clauses)
            )
            .subquery()
        )
        member_count = int(
            await db.scalar(select(func.count()).select_from(member_ids)) or 0
        )
        type_rows = (
            await db.execute(
                select(AgentRelation.relation_type, func.count())
                .where(*clauses)
                .group_by(AgentRelation.relation_type)
                .order_by(func.count().desc(), AgentRelation.relation_type)
            )
        ).all()
        last_seen = await db.scalar(
            select(func.max(AgentRelation.last_seen_at)).where(*clauses)
        )
    return ok(
        {
            "relationCount": relation_count,
            "memberCount": member_count,
            "typeCounts": [
                {"type": str(relation_type), "count": int(count)}
                for relation_type, count in type_rows
            ],
            "lastSeenAt": iso(last_seen),
        }
    )


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
        clauses = await _visible_relation_clauses(
            db,
            group_id,
            guest=_is_guest_view(_session),
        )
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
