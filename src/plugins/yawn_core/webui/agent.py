# ruff: noqa: C901,FAST002,PLR0912,PLR0913,PLR0915,PLR0917,TC001,TID252
"""Agent configuration, memory, privacy, relation and message endpoints."""

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
from ..yawn_agent.context_history import history_message_meta as _history_message_meta
from ..yawn_agent.context_loader import load_context as _load_context
from ..yawn_agent.emotion import emotion_context_state, emotion_public_state
from ..yawn_agent.execution_trace import (
    begin_execution_trace,
    finish_execution_trace,
    recent_execution_traces,
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
from ..yawn_agent.speech_runtime import (
    build_runtime_speech_plan,
    speech_simulation_payload,
    trace_speech_decision,
)
from ..yawn_agent.tools import (
    build_tool_schemas,
    dialogue_tool_round_limit,
    select_dialogue_message_segment_types,
    select_dialogue_tool_names,
    tool_permission_snapshot,
)
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
_AGENT_DEBUG_RUNS = asyncio.Semaphore(2)
_DEBUG_HISTORY_LIMIT = 40
_DEBUG_MEMORY_LIMIT = 30
_DEBUG_RELATION_LIMIT = 20
_DEBUG_TIMEOUT_SECONDS = 30.0


def _is_guest_view(session: Any) -> bool:
    return getattr(session, "role", "admin") == "guest"


def _memory_privacy_clauses(user_ids: set[int]) -> tuple[Any, ...]:
    if not user_ids:
        return ()
    related = func.json_each(AgentMemory.related_user_ids).table_valued("value")
    has_related_optout = exists(
        select(1)
        .select_from(related)
        .where(cast(related.c.value, BigInteger).in_(user_ids))
    )
    return (
        AgentMemory.subject_user_id.not_in(user_ids),
        ~has_related_optout,
    )


@router.get("/agent/groups/{group_id}/config")
async def get_agent_config(group_id: int, _session: AdminReadSession) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        row = await db.get(GroupAgentConfig, group_id)
        result = serialize_agent_config(row, group_id)
        if row is not None:
            result["enabled"] = await agent_runtime_enabled(
                db, group_id, config=row
            )
        else:
            features = await group_feature_rows(db, group_id)
            result["enabled"] = bool(
                next(item for item in features if item["key"] == "group_agent")[
                    "effective"
                ]
            )
        return ok(result)


@router.get("/agent/groups/{group_id}/diagnostics")
async def get_agent_diagnostics(
    group_id: int, _session: AdminReadSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        return ok(await agent_diagnostics(db, group_id))


@router.get("/agent/groups/{group_id}/capabilities")
async def get_agent_capabilities(
    group_id: int, _session: AdminReadSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
    bots = _debug_bots()
    if not bots:
        return ok(
            {
                "botId": None,
                "groupId": str(group_id),
                "offline": True,
                "action": {
                    "cached": False,
                    "role": "offline",
                    "canManage": False,
                    "actions": [],
                    "degraded": True,
                    "lastError": "bot_offline",
                    "probedAt": None,
                    "cacheRemainingSeconds": 0,
                },
                "segments": [],
            }
        )
    bot = bots[0]
    await probe_group_capabilities(bot, group_id)
    return ok({"offline": False, **capability_runtime_snapshot(bot, group_id)})


@router.get("/agent/groups/{group_id}/execution-traces")
async def get_agent_execution_traces(
    group_id: int, _session: AdminReadSession
) -> dict[str, Any]:
    """返回当前进程内最近的真实 Agent 执行时间线。

    Trace 是短生命周期诊断缓冲，不从数据库重建，也不携带原始媒体 URL、
    本机路径或裸 OneBot payload。
    """

    async with get_session() as db:
        await require_group(db, group_id)
    return ok(recent_execution_traces(group_id))


@router.post("/agent/groups/{group_id}/capabilities/refresh")
async def refresh_agent_capabilities(
    group_id: int, _session: AdminWriteSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
    bots = _debug_bots()
    if not bots:
        raise HTTPException(status.HTTP_409_CONFLICT, "Bot 当前离线，无法重新探测能力")
    bot = bots[0]
    clear_group_capability_cache(bot, group_id)
    await probe_group_capabilities(bot, group_id, refresh=True)
    return ok({"offline": False, **capability_runtime_snapshot(bot, group_id)})


@router.patch("/agent/groups/{group_id}/config")
async def patch_agent_config(
    group_id: int, body: AgentConfigPatch, _session: AdminWriteSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        row = await db.get(GroupAgentConfig, group_id)
        check_version(row, body.version)
        if row is None:
            row = GroupAgentConfig(group_id=group_id)
            db.add(row)
        updates = body.model_dump(exclude_unset=True, exclude={"version"})
        legacy_mode = updates.pop("trigger_mode", None)
        if legacy_mode is not None:
            legacy_flags = {
                "mention_only": {
                    "reply_trigger_enabled": False,
                    "explicit_wakeup_enabled": False,
                    "proactive_enabled": False,
                },
                "mention_or_reply": {
                    "reply_trigger_enabled": True,
                    "explicit_wakeup_enabled": False,
                    "proactive_enabled": False,
                },
                "explicit_wakeup": {
                    "reply_trigger_enabled": False,
                    "explicit_wakeup_enabled": True,
                    "proactive_enabled": False,
                },
                "mention_or_proactive": {
                    "reply_trigger_enabled": True,
                    "explicit_wakeup_enabled": True,
                    "proactive_enabled": True,
                },
            }[legacy_mode]
            # Explicit new fields win when old and new clients mix payloads.
            for field, value in legacy_flags.items():
                updates.setdefault(field, value)
            row.trigger_mode = legacy_mode
        if not updates and legacy_mode is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "没有可更新的字段"
            )
        for field, value in updates.items():
            if field == "enabled":
                continue
            setattr(row, field, value)
        if "enabled" in updates:
            await set_agent_runtime_enabled(
                db,
                group_id,
                enabled=bool(updates["enabled"]),
                config=row,
            )
        await db.commit()
        await db.refresh(row)
        result = serialize_agent_config(row, group_id)
        if updates.get("enabled") is False:
            close_group_conversations(group_id, reason="WebUI 关闭 Agent 总开关")
        elif updates.get("short_conversation_enabled") is False:
            close_group_conversations(group_id, reason="WebUI 关闭短会话续聊")
    await hub.notify_change("agent_config", str(group_id))
    if "enabled" in updates:
        await hub.notify_change("group_feature", f"{group_id}:group_agent")
    return ok(result)


@router.get("/agent/groups/{group_id}/persona")
async def get_agent_persona(
    group_id: int, _session: AdminReadSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        row = await db.get(GroupAgentConfig, group_id)
        return ok(serialize_persona(row, group_id))


@router.put("/agent/groups/{group_id}/persona")
async def put_agent_persona(
    group_id: int, body: PersonaPatch, _session: AdminWriteSession
) -> dict[str, Any]:
    changed = False
    async with get_session() as db:
        await require_group(db, group_id)
        row = await db.get(GroupAgentConfig, group_id)
        check_version(row, body.version)
        if row is None:
            row = GroupAgentConfig(group_id=group_id)
            db.add(row)
        mutation = apply_persona_editor_profile(
            row, body.profile, enabled=body.enabled
        )
        if mutation.semantic_changed:
            row.persona_version += 1
            changed = True
        if mutation.storage_changed:
            await db.commit()
            await db.refresh(row)
        result = serialize_persona(row, group_id)
    if changed:
        await hub.notify_change("agent_persona", str(group_id))
    return ok(result)


@router.delete("/agent/groups/{group_id}/persona")
async def reset_agent_persona(
    group_id: int,
    _session: AdminWriteSession,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    changed = False
    async with get_session() as db:
        await require_group(db, group_id)
        row = await db.get(GroupAgentConfig, group_id)
        check_version(row, if_match)
        if row is None:
            row = GroupAgentConfig(group_id=group_id)
            db.add(row)
        mutation = reset_persona(row)
        if mutation.semantic_changed:
            row.persona_version += 1
            changed = True
        if mutation.storage_changed:
            await db.commit()
            await db.refresh(row)
        result = serialize_persona(row, group_id)
    if changed:
        await hub.notify_change("agent_persona", str(group_id))
    return ok(result)


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


# 成员画像面板的成员索引口径：profile/core/manual 都是按成员沉淀的可读事实
# （对话注入同口径），群级行（subject=0）与 summary 不参与。
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


# 手动整理是重操作（LLM 摘要可达数十秒）：后台执行并按群防重复触发。
_compact_inflight: set[int] = set()
_bg_tasks: set[asyncio.Task[None]] = set()


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
        await hub.notify_change("agent_memory", str(group_id))


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
        await hub.notify_change("agent_memory", str(group_id))


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
    await hub.notify_change("agent_memory", str(row.id))
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
    await hub.notify_change("agent_memory", str(memory_id))
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
    await hub.notify_change("agent_memory", str(memory_id))
    return ok({"deleted": count})


@router.delete("/agent/groups/{group_id}/members/{user_id}/data")
async def delete_member_agent_data(
    group_id: int, user_id: int, _session: AdminWriteSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        count = await delete_member_memories(db, group_id, user_id)
    await hub.notify_change("agent_member_data", f"{group_id}:{user_id}")
    return ok({"deleted": count})


@router.delete("/agent/groups/{group_id}/data")
async def delete_group_agent_data(
    group_id: int, _session: AdminWriteSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        count = await delete_group_memories(db, group_id)
    await hub.notify_change("agent_group_data", str(group_id))
    return ok({"deleted": count})


@router.get("/agent/groups/{group_id}/privacy")
async def get_privacy(
    group_id: int,
    _session: AdminReadSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=100),
) -> dict[str, Any]:
    page, page_size = page_params(page, page_size)
    clause = AgentPrivacy.group_id == group_id
    async with get_session() as db:
        await require_group(db, group_id)
        total = int(
            await db.scalar(
                select(func.count()).select_from(AgentPrivacy).where(clause)
            )
            or 0
        )
        rows = list(
            (
                await db.execute(
                    select(AgentPrivacy)
                    .where(clause)
                    .order_by(AgentPrivacy.updated_at.desc(), AgentPrivacy.user_id)
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
    return ok(
        [
            {
                "groupId": str(row.group_id),
                "userId": str(row.user_id),
                "optedOut": row.opted_out,
                "updatedAt": iso(row.updated_at),
            }
            for row in rows
        ],
        page_meta(page, page_size, total),
    )


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
    clauses = [AgentRelation.group_id == group_id]
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
            clauses.extend(
                (
                    AgentRelation.subject_user_id.not_in(opted_out),
                    AgentRelation.object_user_id.not_in(opted_out),
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
    serializer = (
        serialize_guest_relation if _is_guest_view(_session) else serialize_relation
    )
    return ok([serializer(row) for row in rows], page_meta(page, page_size, total))


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
        clauses = [AgentRelation.group_id == group_id]
        if _is_guest_view(_session):
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
    await hub.notify_change("agent_relation", str(row.id))
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
    await hub.notify_change("agent_relation", str(relation_id))
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
    await hub.notify_change("agent_relation", str(relation_id))
    return ok({"deleted": 1})


@router.get("/agent/groups/{group_id}/messages")
async def get_agent_messages(
    group_id: int,
    _session: AdminReadSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=100),
    search: str = Query(default="", max_length=120),
    role: str = Query(default="", max_length=24),
) -> dict[str, Any]:
    page, page_size = page_params(page, page_size)
    now = now_beijing()
    clauses = [
        GroupAgentMessage.group_id == group_id,
        GroupAgentMessage.expires_at.is_not(None),
        GroupAgentMessage.expires_at >= now,
    ]
    if role:
        clauses.append(GroupAgentMessage.role == role)
    if search:
        pattern = f"%{search}%"
        clauses.append(
            or_(
                GroupAgentMessage.normalized_text.ilike(pattern),
                GroupAgentMessage.sender_name.ilike(pattern),
            )
        )
    async with get_session() as db:
        await require_group(db, group_id)
        # 隐私退出是读路径级别的：管理台同样不得回看其消息。
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
            clauses.append(GroupAgentMessage.user_id.not_in(opted_out))
        total = int(
            await db.scalar(
                select(func.count()).select_from(GroupAgentMessage).where(*clauses)
            )
            or 0
        )
        rows = list(
            (
                await db.execute(
                    select(GroupAgentMessage)
                    .where(*clauses)
                    .order_by(GroupAgentMessage.id.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
    return ok(
        [serialize_agent_message(row) for row in rows],
        page_meta(page, page_size, total),
    )


def _debug_context_stats(context: dict[str, Any]) -> dict[str, Any]:
    messages = list(context.get("messages") or [])
    memories = list(context.get("memories") or [])
    relations = list(context.get("relations") or [])
    media_summary_count = sum(
        1
        for item in messages
        if item.get("media_types") or item.get("forward_nodes")
    )
    return {
        "history": {
            "count": len(messages),
            "limit": _DEBUG_HISTORY_LIMIT,
            "characters": sum(len(str(item.get("text") or "")) for item in messages),
            "limitReached": len(messages) >= _DEBUG_HISTORY_LIMIT,
        },
        "memory": {
            "count": len(memories),
            "limit": _DEBUG_MEMORY_LIMIT,
            "characters": sum(len(str(item.get("content") or "")) for item in memories),
            "limitReached": len(memories) >= _DEBUG_MEMORY_LIMIT,
        },
        "relation": {
            "count": len(relations),
            "limit": _DEBUG_RELATION_LIMIT,
            "characters": sum(len(str(item)) for item in relations),
            "limitReached": len(relations) >= _DEBUG_RELATION_LIMIT,
        },
        "memberCount": len(list(context.get("members") or [])),
        "mediaSummaryCount": media_summary_count,
    }


def _debug_tool_calls(message: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for call in list(getattr(message, "tool_calls", None) or []):
        function = getattr(call, "function", None)
        raw_arguments = str(getattr(function, "arguments", "") or "")
        try:
            arguments: object = json.loads(raw_arguments)
        except (TypeError, ValueError):
            arguments = raw_arguments
        calls.append(
            {
                "name": str(getattr(function, "name", "") or ""),
                "arguments": arguments,
            }
        )
    return calls


def _debug_model_payload(result: Any, mode: str) -> dict[str, Any]:
    message = result.message
    content = str(getattr(message, "content", "") or "").strip()
    payload: dict[str, Any] = {
        "outcome": result.outcome,
        "text": content,
        "toolCalls": _debug_tool_calls(message),
        "finishReason": result.finish_reason,
        "usage": {
            "promptTokens": result.prompt_tokens,
            "completionTokens": result.completion_tokens,
            "cachedTokens": result.cached_tokens,
            "cacheMissTokens": result.cache_miss_tokens,
        },
        "durationMs": round(float(result.duration_ms), 1),
    }
    if mode != "dialogue" and content:
        decision = _decide_proactive_reply(content)
        payload["decision"] = {
            "action": decision.action,
            "targetUserId": (
                str(decision.target_user_id)
                if decision.target_user_id is not None
                else None
            ),
            "text": decision.text,
            "topic": decision.topic,
            "reason": decision.reason,
            "confidence": decision.confidence,
            "segments": list(decision.segments),
        }
    return payload


def _debug_bots() -> list[Any]:
    try:
        return list(get_bots().values())
    except Exception:  # noqa: BLE001
        return []


async def _run_debug_completion(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    task: str,
    max_tokens: int,
) -> Any:
    async with _AGENT_DEBUG_RUNS:
        return await complete_with_tools_result(  # pyright: ignore[reportArgumentType]
            messages,  # pyright: ignore[reportArgumentType]
            tools,  # pyright: ignore[reportArgumentType]
            task=task,  # pyright: ignore[reportArgumentType]
            max_tokens=max_tokens,
            timeout=_DEBUG_TIMEOUT_SECONDS,
        )


@router.post("/agent/groups/{group_id}/debug/run")
async def run_agent_debug(
    group_id: int, body: AgentDebugRunBody, _session: AdminWriteSession
) -> dict[str, Any]:
    """构建线上同源提示词；可选调用模型，但不执行工具、发送或落库。"""

    async with get_session() as db:
        await require_group(db, group_id)
        config = await db.get(GroupAgentConfig, group_id)
        if config is None:
            config = GroupAgentConfig(group_id=group_id)

        source: GroupAgentMessage | None = None
        if body.message_id is not None:
            source = (
                (
                    await db.execute(
                        select(GroupAgentMessage)
                        .where(
                            GroupAgentMessage.group_id == group_id,
                            GroupAgentMessage.message_id == body.message_id,
                            GroupAgentMessage.expires_at.is_not(None),
                            GroupAgentMessage.expires_at >= now_beijing(),
                        )
                        .order_by(GroupAgentMessage.id.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .one_or_none()
            )
            if source is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "消息不存在或已过期")
            actor_user_id = int(source.user_id)
            actor_name = source.sender_name
            actor_role = source.role
            actor_title = source.title
            received_at = source.received_at
        else:
            actor_user_id = int(body.actor_user_id or 0)
            member_row = (
                await db.execute(
                    select(UserGroup, BotUser.nickname)
                    .outerjoin(BotUser, BotUser.user_id == UserGroup.user_id)
                    .where(
                        UserGroup.group_id == group_id,
                        UserGroup.user_id == actor_user_id,
                    )
                )
            ).first()
            if member_row is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "模拟发言人不在当前群")
            member, nickname = member_row
            actor_name = member.group_nickname or nickname
            actor_role = member.role
            actor_title = member.title
            received_at = now_beijing()

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
        if actor_user_id in opted_out:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "消息不存在或成员已退出记忆")

        bots = _debug_bots()
        preferred_bot_id = int(source.bot_id) if source is not None else None
        bot = next(
            (
                item
                for item in bots
                if preferred_bot_id is not None
                and int(str(getattr(item, "self_id", 0) or 0)) == preferred_bot_id
            ),
            bots[0] if bots else None,
        )
        bot_id = (
            int(str(getattr(bot, "self_id", 0) or 0))
            if bot is not None
            else preferred_bot_id
        )

        if source is not None:
            meta = _history_message_meta(source)
            mentions = [
                int(user_id)
                for user_id in meta.get("mentions", [])
                if int(user_id) not in opted_out
            ]
            reply_chain = [
                item
                for item in list(source.reply_chain or [])
                if isinstance(item, dict)
                and int(str(item.get("user_id") or 0)) not in opted_out
            ]
            trigger = "history_replay"
            if bot_id is not None and bot_id in mentions:
                trigger = "mention"
            elif (
                reply_chain
                and int(str(reply_chain[0].get("user_id") or 0)) == bot_id
            ):
                trigger = "reply_to_bot"
            current_turn = build_current_turn(
                message_id=int(source.message_id),
                user_id=actor_user_id,
                name=actor_name,
                role=actor_role,
                title=actor_title,
                content=str(source.normalized_text or "[媒体消息]"),
                mentions=mentions,
                reply_chain=reply_chain,
                trigger=trigger,
                received_at=received_at,
                media_refs=list(source.media_refs or []),
                forward_nodes=len(list(source.forward_tree or [])),
            )
        else:
            current_turn = build_current_turn(
                message_id=None,
                user_id=actor_user_id,
                name=actor_name,
                role=actor_role,
                title=actor_title,
                content=str(body.text or ""),
                trigger="debug_simulation",
                received_at=received_at,
            )

        debug_trace = begin_execution_trace(
            group_id,
            mode=body.mode,
            source="debug",
            actor_user_id=actor_user_id,
            message_id=current_turn.message_id,
        )
        trace_event(
            "intake",
            "载入调试场景",
            output={
                "trigger": current_turn.trigger,
                "text_chars": len(current_turn.content),
                "media": list(current_turn.media),
                "reply_to": current_turn.reply_to,
                "forward_nodes": current_turn.forward_nodes,
            },
            trace=debug_trace,
        )

        focus_ids = [
            current_turn.user_id,
            *(user_id for user_id in current_turn.mentions if user_id != bot_id),
        ]
        if (
            current_turn.reply_to
            and current_turn.reply_to.get("user_id")
            and int(str(current_turn.reply_to["user_id"])) != bot_id
        ):
            focus_ids.append(int(str(current_turn.reply_to["user_id"])))
        task = "agent_dialogue" if body.mode == "dialogue" else "agent_proactive"
        route = resolve_llm_request(task)
        context_selection: list[dict[str, Any]] = []
        context_budget: list[dict[str, Any]] = []
        context_started = time.monotonic()
        context = await _load_context(
            db,
            group_id,
            config,
            bot_id,
            focus_user_ids=focus_ids,
            query_text=current_turn.content if body.mode == "dialogue" else None,
            compact_history=body.mode != "dialogue",
            message_cutoff=received_at,
            include_active_profiles=True,
            exclude_message_id=(
                int(source.message_id)
                if source is not None and body.mode == "dialogue"
                else None
            ),
            reference_at=received_at,
            selection_trace=context_selection,
            budget_trace=context_budget,
            context_model=route.model,
            completion_reserve=(
                800
                if body.mode == "dialogue"
                else max(2048, int(ai_config.ai_max_tokens))
            ),
        )
        trace_event(
            "context",
            "上下文筛选与 Token 装箱",
            input={"focus_user_ids": focus_ids, "model": route.model},
            output={
                "messages": len(list(context.get("messages") or [])),
                "members": len(list(context.get("members") or [])),
                "memories": len(list(context.get("memories") or [])),
                "relations": len(list(context.get("relations") or [])),
                "selection_candidates": len(context_selection),
                "budget_components": len(context_budget),
            },
            duration_ms=(time.monotonic() - context_started) * 1000,
            trace=debug_trace,
        )
        if source is None and body.mode != "dialogue":
            context["messages"] = trim_context_messages(
                [
                    *list(context.get("messages") or []),
                    {
                        "message_id": None,
                        "user_id": current_turn.user_id,
                        "name": current_turn.name,
                        "role": current_turn.role,
                        "title": current_turn.title,
                        "text": current_turn.content,
                        "minutes_ago": 0,
                        "topic_break_before": False,
                        "mentions": list(current_turn.mentions),
                        "reply_to": current_turn.reply_to,
                        "media_types": [],
                        "forward_nodes": 0,
                    },
                ]
            )

        tools: list[dict[str, Any]] = []
        tool_permissions: list[dict[str, Any]] = []
        capability_started = time.monotonic()
        if body.mode == "dialogue":
            privileged_allowlist = set(config.tool_allowlist or [])
            selected_tool_names = select_dialogue_tool_names(
                body.text,
                allow_admin_tools=False,
            )
            message_segment_types = select_dialogue_message_segment_types(body.text)
            if bot is None:
                capabilities = BotGroupCapabilities(
                    role="offline", can_manage=False, actions=frozenset()
                )
                tools = build_tool_schemas(
                    capabilities,
                    privileged_allowlist=privileged_allowlist,
                    include_names=selected_tool_names,
                    message_segment_types=(
                        message_segment_types
                        if "send_message" in selected_tool_names
                        else None
                    ),
                )
                tool_permissions = tool_permission_snapshot(
                    capabilities,
                    privileged_allowlist=privileged_allowlist,
                )
            else:
                capabilities = await probe_group_capabilities(bot, group_id)
                allow_privileged_tools = await user_can_manage_group(
                    bot, group_id, actor_user_id
                )
                selected_tool_names = select_dialogue_tool_names(
                    body.text,
                    allow_admin_tools=allow_privileged_tools,
                )
                message_segment_types = select_dialogue_message_segment_types(body.text)
                tools = build_tool_schemas(
                    capabilities,
                    allow_admin_tools=allow_privileged_tools,
                    segment_capabilities=get_segment_capabilities(bot, group_id),
                    privileged_allowlist=privileged_allowlist,
                    include_names=selected_tool_names,
                    message_segment_types=(
                        message_segment_types
                        if "send_message" in selected_tool_names
                        else None
                    ),
                )
                tool_permissions = tool_permission_snapshot(
                    capabilities,
                    allow_admin_tools=allow_privileged_tools,
                    privileged_allowlist=privileged_allowlist,
                )
            trace_event(
                "capability",
                "协议能力与工具权限计算",
                output={
                    "tool_count": len(tools),
                    "round_limit": dialogue_tool_round_limit(selected_tool_names),
                    "message_segment_types": sorted(message_segment_types),
                    "selected_tools": sorted(selected_tool_names),
                    "exposed_tools": [
                        item["name"] for item in tool_permissions if item.get("exposed")
                    ],
                    "blocked_tools": [
                        {"name": item["name"], "reason": item["reason"]}
                        for item in tool_permissions
                        if not item.get("exposed")
                    ],
                },
                duration_ms=(time.monotonic() - capability_started) * 1000,
                trace=debug_trace,
            )
        else:
            trace_event(
                "capability",
                "工具权限计算",
                status="skipped",
                detail="主动/暖场调试不向模型暴露对话工具",
                trace=debug_trace,
            )

        persisted_editor = persona_editor_profile(config)
        applied_behavior = (
            persona_behavior_draft(config, body.persona_draft)
            if body.persona_draft is not None
            else persona_behavior(config)
        )
        applied_expressiveness = (
            body.persona_draft.expressiveness
            if body.persona_draft is not None
            else persisted_editor.expressiveness
        )
        emotion_raw = (
            config.emotion_state if isinstance(config.emotion_state, dict) else {}
        )
        persisted_emotion = emotion_public_state(
            emotion_raw, expressiveness=persisted_editor.expressiveness
        )
        applied_emotion = emotion_public_state(
            emotion_raw, expressiveness=applied_expressiveness
        )
        draft_emotion_context = emotion_context_state(
            emotion_raw, expressiveness=applied_expressiveness
        )
        context = dict(context)
        if draft_emotion_context:
            context["emotion_state"] = draft_emotion_context
        else:
            context.pop("emotion_state", None)
        user_prompt = (
            current_turn.content
            if body.mode == "dialogue"
            else _build_user_prompt(
                body.mode,
                config,
                turn=2 if body.mode == "followup" else 1,
                behavior=applied_behavior,
            )
        )
        applied_persona = (
            resolve_persona_draft(config, body.persona_draft)
            if body.persona_draft is not None
            else resolve_persona(config)
        )
        prompt_started = time.monotonic()
        prompt_messages, _fingerprint = build_messages(
            persona=applied_persona,
            tools=tools,
            context=context,
            user_prompt=user_prompt,
            current_turn=current_turn if body.mode == "dialogue" else None,
        )
        trace_event(
            "prompt",
            "Prompt 构建",
            input={
                "tool_count": len(tools),
                "mode": body.mode,
                "persona_source": (
                    "draft" if body.persona_draft is not None else "persisted"
                ),
                "persona_behavior": applied_behavior.as_dict(),
            },
            output={
                "message_count": len(prompt_messages),
                "prompt_version": PROMPT_VERSION,
                "fingerprint": _fingerprint[:12],
            },
            duration_ms=(time.monotonic() - prompt_started) * 1000,
            trace=debug_trace,
        )
        base_url, api_key = resolve_provider(route.provider)
        result_payload: dict[str, Any] | None = None
        preview_plan = build_runtime_speech_plan(
            text="",
            persona=applied_persona,
            current_turn=current_turn,
            context=context,
            source=body.mode,
            action="speak",
        )
        speech_simulation = speech_simulation_payload(
            preview_plan,
            emotion_state=applied_emotion,
            should_speak=None,
            preview_only=True,
            user_text=current_turn.content,
        )
        if body.run_model:
            model_started = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    _run_debug_completion(
                        prompt_messages,
                        tools,
                        task=task,
                        max_tokens=(
                            800
                            if body.mode == "dialogue"
                            else max(2048, int(ai_config.ai_max_tokens))
                        ),
                    ),
                    timeout=_DEBUG_TIMEOUT_SECONDS,
                )
                result_payload = _debug_model_payload(result, body.mode)
                if body.mode == "dialogue":
                    if not result_payload.get("toolCalls") and result_payload.get("text"):
                        simulated_plan = build_runtime_speech_plan(
                            text=result_payload.get("text") or "",
                            persona=applied_persona,
                            current_turn=current_turn,
                            context=context,
                            source="dialogue",
                        )
                        speech_simulation = speech_simulation_payload(
                            simulated_plan,
                            emotion_state=applied_emotion,
                            should_speak=True,
                            user_text=current_turn.content,
                        )
                        trace_speech_decision(
                            simulated_plan,
                            emotion_state=applied_emotion,
                            participation_action="speak",
                            trace=debug_trace,
                        )
                else:
                    simulated_decision = _decide_proactive_reply(
                        str(result_payload.get("text") or "")
                    )
                    simulated_plan = build_runtime_speech_plan(
                        text=simulated_decision.text,
                        segments=list(simulated_decision.segments),
                        persona=applied_persona,
                        current_turn=current_turn,
                        context=context,
                        source=body.mode,
                        action=simulated_decision.action,
                        target_user_id=simulated_decision.target_user_id,
                        suggested_topic=simulated_decision.topic,
                        reason=simulated_decision.reason,
                        confidence=simulated_decision.confidence,
                    )
                    speech_simulation = speech_simulation_payload(
                        simulated_plan,
                        emotion_state=applied_emotion,
                        should_speak=simulated_decision.should_speak,
                        user_text=current_turn.content,
                    )
                    trace_speech_decision(
                        simulated_plan,
                        emotion_state=applied_emotion,
                        participation_action=simulated_decision.action,
                        trace=debug_trace,
                    )
                trace_event(
                    "llm",
                    "真实模型试跑",
                    status=(
                        "success"
                        if result_payload.get("outcome") == "success"
                        else "degraded"
                    ),
                    output={
                        "outcome": result_payload.get("outcome"),
                        "finish_reason": result_payload.get("finishReason"),
                        "content_chars": len(str(result_payload.get("text") or "")),
                        "tool_calls": [
                            item.get("name")
                            for item in list(result_payload.get("toolCalls") or [])
                            if isinstance(item, dict)
                        ],
                        "usage": result_payload.get("usage"),
                    },
                    duration_ms=(time.monotonic() - model_started) * 1000,
                    trace=debug_trace,
                )
                for tool_call in list(result_payload.get("toolCalls") or []):
                    if not isinstance(tool_call, dict):
                        continue
                    trace_event(
                        "tool",
                        f"工具意图 {tool_call.get('name') or '[unknown]'}",
                        status="planned",
                        input={"arguments": tool_call.get("arguments")},
                        output={"executed": False},
                        detail="调试模式禁止执行工具副作用",
                        trace=debug_trace,
                    )
                if result_payload.get("toolCalls"):
                    trace_event(
                        "outbound",
                        "发送阶段",
                        status="skipped",
                        output={"sent": False},
                        detail="调试模式不发送群消息，也不执行工具内发送",
                        trace=debug_trace,
                    )
            except asyncio.TimeoutError:
                result_payload = {
                    "outcome": "timeout",
                    "text": "",
                    "toolCalls": [],
                    "finishReason": None,
                    "usage": {
                        "promptTokens": None,
                        "completionTokens": None,
                        "cachedTokens": None,
                        "cacheMissTokens": None,
                    },
                    "durationMs": _DEBUG_TIMEOUT_SECONDS * 1000,
                }
                trace_event(
                    "llm",
                    "真实模型试跑",
                    status="failed",
                    output={"outcome": "timeout"},
                    detail="超过调试请求时限",
                    duration_ms=(time.monotonic() - model_started) * 1000,
                    trace=debug_trace,
                )
        else:
            trace_event(
                "llm",
                "模型调用",
                status="skipped",
                output={"run_model": False},
                detail="本次仅生成执行快照，未请求模型",
                trace=debug_trace,
            )

        trace_event(
            "state",
            "状态写入",
            status="skipped",
            output={"database_mutated": False},
            detail="调试执行全程只读，不修改 Agent 状态",
            trace=debug_trace,
        )

        stats = _debug_context_stats(context)
        warnings: list[str] = []
        if (
            current_turn.media_types
            or current_turn.forward_nodes
            or stats["mediaSummaryCount"]
        ):
            warnings.append("媒体和合并转发仅以安全摘要展示，未参与模型重放")
        if bot is None:
            warnings.append("当前没有在线 Bot，工具列表按离线能力收敛")
        trace_outcome = (
            str(result_payload.get("outcome"))
            if result_payload is not None
            else "snapshot"
        )
        trace_event(
            "turn",
            "调试执行结束",
            output={"outcome": trace_outcome, "warnings": len(warnings)},
            trace=debug_trace,
        )
        finish_execution_trace(
            debug_trace,
            outcome=trace_outcome,
            status=("failed" if trace_outcome == "timeout" else "completed"),
            store=False,
        )
        payload = {
            "promptVersion": PROMPT_VERSION,
            "mode": body.mode,
            "persona": {
                "source": "draft" if body.persona_draft is not None else "persisted",
                "persistedSummary": persona_summary(config),
                "persistedProfile": persisted_editor.model_dump(by_alias=True),
                "persistedBehavior": persona_behavior(config).as_dict(),
                "appliedBehavior": applied_behavior.as_dict(),
                "persistedEmotion": persisted_emotion,
                "appliedEmotion": applied_emotion,
                "appliedProfile": (
                    body.persona_draft.model_dump(by_alias=True)
                    if body.persona_draft is not None
                    else persisted_editor.model_dump(by_alias=True)
                ),
            },
            "currentTurn": current_turn.as_dict(),
            "context": context,
            "contextSelection": context_selection,
            "contextBudget": context_budget,
            "promptMessages": prompt_messages,
            "tools": tools,
            "toolPermissions": tool_permissions,
            "route": {
                "task": task,
                "profile": route.profile,
                "provider": route.provider,
                "model": route.model,
                "thinking": route.thinking,
                "multimodal": route.multimodal,
                "configured": bool(
                    base_url.strip() and api_key and route.model.strip()
                ),
            },
            "stats": stats,
            "warnings": warnings,
            "result": result_payload,
            "speechSimulation": speech_simulation,
            "executionTrace": debug_trace.as_dict(),
        }
    return ok(payload)


@router.patch("/agent/groups/{group_id}/privacy/{user_id}")
async def patch_privacy(
    group_id: int, user_id: int, body: PrivacyPatchBody, _session: AdminWriteSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        privacy = await db.get(AgentPrivacy, (group_id, user_id))
        if privacy is None:
            privacy = AgentPrivacy(group_id=group_id, user_id=user_id)
            db.add(privacy)
        privacy.opted_out = body.opted_out
        if body.opted_out:
            # 与 /Agent隐私 命令同语义：退出即连带清除该成员已沉淀的记忆。
            await delete_member_memories(db, group_id, user_id)
        else:
            await db.commit()
        await db.refresh(privacy)
        result = {
            "groupId": str(privacy.group_id),
            "userId": str(privacy.user_id),
            "optedOut": privacy.opted_out,
            "updatedAt": iso(privacy.updated_at),
        }
    await hub.notify_change("agent_privacy", f"{group_id}:{user_id}")
    return ok(result)
