# ruff: noqa: F401, FAST002, PLR0913, PLR0917, TC001, TID252
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
_compact_inflight: set[int] = set()
_bg_tasks: set[asyncio.Task[Any]] = set()

@router.get("/agent/groups/{group_id}/memories")
async def get_memories(
    group_id: int,
    _session: GroupViewSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=100),
    search: str = Query(default="", max_length=120),
    memory_type: str = Query(default="", alias="type", max_length=24),
    subject_user_id: int | None = Query(default=None, alias="subjectUserId"),
) -> dict[str, Any]:
    page, page_size = page_params(page, page_size)
    clauses = [
        AgentMemory.group_id == group_id,
        AgentMemory.visibility.in_(("group", "public")),
        (
            AgentMemory.expires_at.is_(None)
            | (AgentMemory.expires_at >= now_beijing())
        ),
    ]
    if search:
        pattern = f"%{search}%"
        clauses.append(
            or_(
                AgentMemory.memory_key.ilike(pattern),
                AgentMemory.content.ilike(pattern),
            )
        )
    if memory_type:
        clauses.append(AgentMemory.memory_type == memory_type)
    if subject_user_id is not None:
        clauses.append(AgentMemory.subject_user_id == subject_user_id)
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
        if opted_out:
            clauses.extend(_memory_privacy_clauses(opted_out))
        total = int(
            await db.scalar(
                select(func.count()).select_from(AgentMemory).where(*clauses)
            )
            or 0
        )
        rows = list(
            (
                await db.execute(
                    select(AgentMemory)
                    .where(*clauses)
                    .order_by(AgentMemory.updated_at.desc(), AgentMemory.id.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
    serializer = (
        serialize_guest_memory if _is_guest_view(_session) else serialize_memory
    )
    return ok([serializer(row) for row in rows], page_meta(page, page_size, total))

_SUBJECT_MEMORY_TYPES = ("profile", "core", "manual")

@router.get("/agent/groups/{group_id}/memories/subjects")
async def get_memory_subjects(
    group_id: int, _session: GroupViewSession
) -> dict[str, Any]:
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
        clauses = [
            AgentMemory.group_id == group_id,
            AgentMemory.subject_user_id != 0,
            AgentMemory.memory_type.in_(_SUBJECT_MEMORY_TYPES),
            AgentMemory.visibility.in_(("group", "public")),
            (
                AgentMemory.expires_at.is_(None)
                | (AgentMemory.expires_at >= now_beijing())
            ),
        ]
        if opted_out:
            clauses.extend(_memory_privacy_clauses(opted_out))
        rows = list(
            (
                await db.execute(
                    select(AgentMemory)
                    .where(*clauses)
                    .order_by(AgentMemory.updated_at.desc(), AgentMemory.id.desc())
                    .limit(RELATION_GRAPH_LIMIT)
                )
            )
            .scalars()
            .all()
        )
        # rows 按 updated_at 降序：首次见到某成员即其最新更新时间，dict 保持
        # 插入序使结果天然按最近更新排列，与图谱端点的 Python 聚合模式一致。
        subjects: dict[int, dict[str, Any]] = {}
        for row in rows:
            user_id = int(row.subject_user_id)
            entry = subjects.get(user_id)
            if entry is None:
                entry = {
                    "userId": str(user_id),
                    "counts": dict.fromkeys(_SUBJECT_MEMORY_TYPES, 0),
                    "total": 0,
                    "updatedAt": iso(row.updated_at),
                }
                subjects[user_id] = entry
            entry["counts"][row.memory_type] += 1
            entry["total"] += 1
        # 昵称联接走全群成员表（同图谱端点），避免大 in_ 参数；退群残留回退空昵称。
        member_rows = list(
            (
                await db.execute(
                    select(UserGroup, BotUser)
                    .join(BotUser, BotUser.user_id == UserGroup.user_id)
                    .where(UserGroup.group_id == group_id)
                    .order_by(UserGroup.user_id)
                    .limit(RELATION_GRAPH_LIMIT)
                )
            )
            .all()
        )
        member_names: dict[int, tuple[str, str | None]] = {
            int(user.user_id): (user.nickname, membership.group_nickname)
            for membership, user in member_rows
        }
        result = []
        for user_id, entry in subjects.items():
            nickname, group_nickname = member_names.get(user_id, ("", None))
            result.append(
                {**entry, "nickname": nickname, "groupNickname": group_nickname}
            )
    return ok(result)

@router.get("/agent/groups/{group_id}/memories/export")
async def export_memories(group_id: int, _session: AdminReadSession) -> dict[str, Any]:
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
        clauses = [
            AgentMemory.group_id == group_id,
            AgentMemory.visibility.in_(("group", "public")),
            (
                AgentMemory.expires_at.is_(None)
                | (AgentMemory.expires_at >= now_beijing())
            ),
            *_memory_privacy_clauses(opted_out),
        ]
        rows = list(
            (
                await db.execute(
                    select(AgentMemory)
                    .where(*clauses)
                    .order_by(AgentMemory.id)
                    .limit(5000)
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
                await db.execute(
                    select(AgentRelation)
                    .where(*relation_clauses)
                    .order_by(AgentRelation.id)
                    .limit(5000)
                )
            )
            .scalars()
            .all()
        )
    return ok(
        {
            "groupId": str(group_id),
            "memories": [serialize_memory(row) for row in rows],
            "relations": [serialize_relation(row) for row in relation_rows],
        }
    )

@router.get("/agent/groups/{group_id}/memories/status")
async def get_memory_status(
    group_id: int, _session: AdminReadSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        return ok(await agent_memory_status(db, group_id))

async def _run_manual_compact(group_id: int) -> None:
    try:
        async with get_session() as db:
            await compact_group_memory(db, group_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("WebUI 手动记忆整理失败: %s", group_id)
        try:
            async with get_session() as db:
                await record_memory_failure(
                    db, group_id, f"手动整理异常: {type(exc).__name__}"
                )
        except Exception:  # noqa: BLE001
            logger.exception("WebUI 手动记忆失败状态写入失败: %s", group_id)
    finally:
        _compact_inflight.discard(group_id)
        await hub.notify_change("agent_memory", str(group_id), group_id=group_id)

async def _run_manual_rebuild(group_id: int) -> None:
    try:
        async with get_session() as db:
            await rebuild_group_memories(db, group_id)
        async with get_session() as db:
            await compact_group_memory(db, group_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("WebUI 手动记忆重建失败: %s", group_id)
        try:
            async with get_session() as db:
                await record_memory_failure(
                    db, group_id, f"手动重建异常: {type(exc).__name__}"
                )
        except Exception:  # noqa: BLE001
            logger.exception("WebUI 手动重建失败状态写入失败: %s", group_id)
    finally:
        _compact_inflight.discard(group_id)
        await hub.notify_change("agent_memory", str(group_id), group_id=group_id)

@router.post("/agent/groups/{group_id}/memories/compact")
async def trigger_memory_compact(
    group_id: int, _session: AdminWriteSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
    if group_id in _compact_inflight or is_memory_compacting(group_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "该群正在整理记忆，请稍后再试")
    _compact_inflight.add(group_id)
    task = asyncio.create_task(_run_manual_compact(group_id))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return ok({"started": True})

@router.post("/agent/groups/{group_id}/memories/rebuild")
async def trigger_memory_rebuild(
    group_id: int, _session: AdminWriteSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
    if group_id in _compact_inflight or is_memory_compacting(group_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "该群正在整理记忆，请稍后再试")
    _compact_inflight.add(group_id)
    task = asyncio.create_task(_run_manual_rebuild(group_id))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return ok({"started": True, "rebuild": True})

@router.post("/agent/groups/{group_id}/memories")
async def create_memory(
    group_id: int, body: MemoryCreateBody, _session: AdminWriteSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        now = now_beijing()
        expires_at = (
            now + timedelta(days=body.expires_in_days)
            if body.expires_in_days is not None
            else None
        )
        row = AgentMemory(
            group_id=group_id,
            scope="group",
            subject_user_id=body.subject_user_id or 0,
            memory_type=body.type,
            memory_key=body.key.strip(),
            content=body.content.strip(),
            evidence_message_ids=[],
            source_kind="manual",
            related_user_ids=sorted(
                {
                    *body.related_user_ids,
                    *([body.subject_user_id] if body.subject_user_id else []),
                }
            ),
            salience=body.salience,
            confidence=body.confidence,
            visibility="group",
            expires_at=expires_at,
        )
        db.add(row)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status.HTTP_409_CONFLICT, "同类型同 key 的记忆已存在"
            ) from None
        await db.refresh(row)
        result = serialize_memory(row)
    await hub.notify_change("agent_memory", str(row.id), group_id=group_id)
    return ok(result)

@router.put("/agent/groups/{group_id}/memories/{memory_id}")
async def update_memory(
    group_id: int, memory_id: int, body: MemoryPatchBody, _session: AdminWriteSession
) -> dict[str, Any]:
    updates = body.model_dump(exclude_unset=True, exclude={"version"})
    if not updates:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "没有可更新的字段")
    async with get_session() as db:
        await require_group(db, group_id)
        row = await db.scalar(
            select(AgentMemory).where(
                AgentMemory.group_id == group_id,
                AgentMemory.id == memory_id,
            )
        )
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "记忆不存在")
        check_version(row, body.version)
        if "content" in updates:
            row.content = str(updates["content"]).strip()
        if "salience" in updates:
            row.salience = float(updates["salience"])
        if "confidence" in updates:
            row.confidence = float(updates["confidence"])
        if "expires_in_days" in updates:
            row.expires_at = (
                now_beijing() + timedelta(days=int(updates["expires_in_days"]))
                if updates["expires_in_days"] is not None
                else None
            )
        await db.commit()
        await db.refresh(row)
        result = serialize_memory(row)
    await hub.notify_change("agent_memory", str(memory_id), group_id=group_id)
    return ok(result)

@router.delete("/agent/groups/{group_id}/memories/{memory_id}")
async def delete_memory(
    group_id: int, memory_id: int, _session: AdminWriteSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        count = await delete_one_memory(db, group_id, memory_id)
        if not count:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "记忆不存在")
        await db.commit()
    await hub.notify_change("agent_memory", str(memory_id), group_id=group_id)
    return ok({"deleted": count})

@router.delete("/agent/groups/{group_id}/members/{user_id}/data")
async def delete_member_agent_data(
    group_id: int, user_id: int, _session: AdminWriteSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        count = await delete_member_memories(db, group_id, user_id)
    await hub.notify_change("agent_member_data", f"{group_id}:{user_id}", group_id=group_id)
    return ok({"deleted": count})

@router.delete("/agent/groups/{group_id}/data")
async def delete_group_agent_data(
    group_id: int, _session: AdminWriteSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        count = await delete_group_memories(db, group_id)
    await hub.notify_change("agent_group_data", str(group_id), group_id=group_id)
    return ok({"deleted": count})
