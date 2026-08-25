# ruff: noqa: FAST002,PLR0913,PLR0917,TC001,TID252
"""Agent configuration, memory, privacy, relation and message endpoints."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, status
from nonebot import logger
from nonebot_plugin_orm import get_session
from sqlalchemy import BigInteger, cast, exists, func, or_, select
from sqlalchemy.exc import IntegrityError

from ..data_models.agent_memory import AgentMemory, AgentPrivacy, AgentRelation
from ..data_models.bot_user import BotUser
from ..data_models.group_agent_config import GroupAgentConfig
from ..data_models.group_agent_message import GroupAgentMessage
from ..data_models.user_group import UserGroup
from ..yawn_agent.config_store import agent_runtime_enabled, set_agent_runtime_enabled
from ..yawn_agent.context import now_beijing
from ..yawn_agent.conversation import close_group_conversations
from ..yawn_agent.memory import (
    compact_group_memory,
    delete_group_memories,
    delete_member_memories,
    is_memory_compacting,
    normalize_relation_type,
    rebuild_group_memories,
    record_memory_failure,
)
from .config import API_PATH
from .deps import ReadSession, WriteSession, ok, page_params
from .hub import hub
from .route_helpers import check_version, require_group
from .route_models import (
    AgentConfigPatch,
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
    serialize_memory,
    serialize_persona,
    serialize_relation,
)

router = APIRouter(prefix=API_PATH)


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
async def get_agent_config(group_id: int, _session: ReadSession) -> dict[str, Any]:
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
async def get_agent_diagnostics(group_id: int, _session: ReadSession) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        return ok(await agent_diagnostics(db, group_id))


@router.patch("/agent/groups/{group_id}/config")
async def patch_agent_config(
    group_id: int, body: AgentConfigPatch, _session: WriteSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        row = await db.get(GroupAgentConfig, group_id)
        check_version(row, body.version)
        if row is None:
            row = GroupAgentConfig(group_id=group_id)
            db.add(row)
        updates = body.model_dump(exclude_unset=True, exclude={"version"})
        if not updates:
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
async def get_agent_persona(group_id: int, _session: ReadSession) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        row = await db.get(GroupAgentConfig, group_id)
        return ok(serialize_persona(row, group_id))


@router.put("/agent/groups/{group_id}/persona")
async def put_agent_persona(
    group_id: int, body: PersonaPatch, _session: WriteSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        row = await db.get(GroupAgentConfig, group_id)
        check_version(row, body.version)
        if row is None:
            row = GroupAgentConfig(group_id=group_id)
            db.add(row)
        row.persona_enabled = body.enabled
        overrides: dict[str, object] = dict(body.overrides)
        row.persona_override = overrides
        row.persona_version += 1
        await db.commit()
        await db.refresh(row)
        result = serialize_persona(row, group_id)
    await hub.notify_change("agent_persona", str(group_id))
    return ok(result)


@router.delete("/agent/groups/{group_id}/persona")
async def reset_agent_persona(
    group_id: int,
    _session: WriteSession,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        row = await db.get(GroupAgentConfig, group_id)
        check_version(row, if_match)
        if row is None:
            row = GroupAgentConfig(group_id=group_id)
            db.add(row)
        row.persona_override = {}
        row.persona_enabled = True
        row.persona_version += 1
        await db.commit()
        await db.refresh(row)
        result = serialize_persona(row, group_id)
    await hub.notify_change("agent_persona", str(group_id))
    return ok(result)


@router.get("/agent/groups/{group_id}/memories")
async def get_memories(
    group_id: int,
    _session: ReadSession,
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
    return ok(
        [serialize_memory(row) for row in rows], page_meta(page, page_size, total)
    )


# 成员画像面板的成员索引口径：profile/core/manual 都是按成员沉淀的可读事实
# （对话注入同口径），群级行（subject=0）与 summary 不参与。
_SUBJECT_MEMORY_TYPES = ("profile", "core", "manual")


@router.get("/agent/groups/{group_id}/memories/subjects")
async def get_memory_subjects(group_id: int, _session: ReadSession) -> dict[str, Any]:
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
async def export_memories(group_id: int, _session: ReadSession) -> dict[str, Any]:
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
async def get_memory_status(group_id: int, _session: ReadSession) -> dict[str, Any]:
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
    group_id: int, _session: WriteSession
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
    group_id: int, _session: WriteSession
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
    group_id: int, body: MemoryCreateBody, _session: WriteSession
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
    group_id: int, memory_id: int, body: MemoryPatchBody, _session: WriteSession
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
    group_id: int, memory_id: int, _session: WriteSession
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
    group_id: int, user_id: int, _session: WriteSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        count = await delete_member_memories(db, group_id, user_id)
    await hub.notify_change("agent_member_data", f"{group_id}:{user_id}")
    return ok({"deleted": count})


@router.delete("/agent/groups/{group_id}/data")
async def delete_group_agent_data(
    group_id: int, _session: WriteSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        count = await delete_group_memories(db, group_id)
    await hub.notify_change("agent_group_data", str(group_id))
    return ok({"deleted": count})


@router.get("/agent/groups/{group_id}/privacy")
async def get_privacy(
    group_id: int,
    _session: ReadSession,
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
    _session: ReadSession,
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
    return ok(
        [serialize_relation(row) for row in rows], page_meta(page, page_size, total)
    )


@router.get("/agent/groups/{group_id}/relations/graph")
async def get_relation_graph(group_id: int, _session: ReadSession) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        return ok(await load_relation_graph(db, group_id))


@router.get("/agent/groups/{group_id}/relations/types")
async def get_relation_types(group_id: int, _session: ReadSession) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        rows = (
            (
                await db.execute(
                    select(AgentRelation.relation_type)
                    .where(AgentRelation.group_id == group_id)
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
    group_id: int, body: RelationCreateBody, _session: WriteSession
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
    group_id: int, relation_id: int, body: RelationPatchBody, _session: WriteSession
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
    group_id: int, relation_id: int, _session: WriteSession
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
    _session: ReadSession,
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


@router.patch("/agent/groups/{group_id}/privacy/{user_id}")
async def patch_privacy(
    group_id: int, user_id: int, body: PrivacyPatchBody, _session: WriteSession
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
