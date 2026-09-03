# ruff: noqa: TID252, TRY003
from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select

from ...data_models.agent_memory import AgentMemory, AgentPrivacy, AgentRelation
from ...data_models.group_agent_message import GroupAgentMessage
from ...data_models.user_group import UserGroup
from ..context import now_beijing
from ..log import dbg
from ..memory import (
    effective_relation_confidence,
    normalize_relation_type,
    rank_memories,
)
from ..tool_execution import ToolExecutionContext, ToolHandlerResult
from ..tool_support import (
    DEFAULT_MEMORY_TOOL_LIMIT,
    DEFAULT_PROFILE_TOOL_LIMIT,
    DEFAULT_RELATION_TOOL_LIMIT,
    MAX_MEMORY_TOOL_LIMIT,
    MAX_PROFILE_TOOL_LIMIT,
    MAX_RELATION_TOOL_LIMIT,
    _tool_result_limit,
)

FAMILY = "memory"
NAMES = frozenset(
    [
        "get_person_profile",
        "search_group_memory",
        "list_user_relations",
        "record_user_relation",
    ]
)


async def handle(  # noqa: C901, PLR0912, PLR0915
    name: str, args: dict[str, Any], context: ToolExecutionContext
) -> ToolHandlerResult:
    group_id = context.group_id
    session = context.session
    now = now_beijing()
    if name == "get_person_profile":
        subject_id = int(args["user_id"])
        limit = _tool_result_limit(
            args, default=DEFAULT_PROFILE_TOOL_LIMIT, maximum=MAX_PROFILE_TOOL_LIMIT
        )
        privacy = (
            await session.get(AgentPrivacy, (group_id, subject_id))
            if session is not None
            else None
        )
        # 隐私退出在读路径同样生效：不得再输出其画像。
        rows: list[Any] = []
        if privacy is not None and privacy.opted_out:
            dbg(
                f"群 {group_id} get_person_profile: 用户 {subject_id} "
                "已隐私退出,返回空画像"
            )
        if privacy is None or not privacy.opted_out:
            stmt = (
                select(AgentMemory)
                .where(
                    AgentMemory.group_id == group_id,
                    AgentMemory.subject_user_id == subject_id,
                    AgentMemory.memory_type.in_(("core", "profile")),
                    AgentMemory.visibility.in_(("group", "public")),
                    (
                        AgentMemory.expires_at.is_(None)
                        | (AgentMemory.expires_at >= now)
                    ),
                )
                .limit(limit)
            )
            rows = (
                (await session.execute(stmt)).scalars().all()
                if session is not None
                else []
            )
        result = [
            {
                "key": row.memory_key,
                "content": str(row.content or "")[:600],
                "confidence": round(float(row.confidence or 0.0), 3),
            }
            for row in rows
        ]
    elif name == "search_group_memory":
        query = str(args.get("query", "")).strip()
        if not query:
            raise ValueError("query 不能为空")
        limit = _tool_result_limit(
            args, default=DEFAULT_MEMORY_TOOL_LIMIT, maximum=MAX_MEMORY_TOOL_LIMIT
        )
        stmt = (
            select(AgentMemory)
            .where(
                AgentMemory.group_id == group_id,
                AgentMemory.visibility.in_(("group", "public")),
                # autoescape：查询来自用户原话，%/_ 不得充当通配符。
                AgentMemory.content.contains(query, autoescape=True),
                (AgentMemory.expires_at.is_(None) | (AgentMemory.expires_at >= now)),
            )
            # 先放宽到 30 条子串候选，再按查询词相关性与显著度重排，
            # 避免"碰巧先入库的低相关匹配"挤掉真正贴合查询的记忆。
            .limit(30)
        )
        rows = (
            (await session.execute(stmt)).scalars().all() if session is not None else []
        )
        opted_out = (
            set(
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
            if session is not None
            else set()
        )
        rows = [
            row
            for row in rows
            if int(row.subject_user_id or 0) not in opted_out
            and not opted_out.intersection(set(row.related_user_ids or []))
        ]
        rows = rank_memories(rows, [query], None, now, limit=limit)
        evidence_ids = list(
            dict.fromkeys(
                int(message_id)
                for row in rows
                for message_id in list(row.evidence_message_ids or [])
                if str(message_id).lstrip("-").isdigit()
            )
        )
        evidence_rows = (
            (
                await session.execute(
                    select(GroupAgentMessage).where(
                        GroupAgentMessage.group_id == group_id,
                        GroupAgentMessage.message_id.in_(evidence_ids),
                    )
                )
            )
            .scalars()
            .all()
            if session is not None and evidence_ids
            else []
        )
        evidence_by_message = {
            int(message.message_id): [
                {
                    **dict(ref),
                    "source": "tool",
                    "source_message_id": int(message.message_id),
                }
                for ref in list(message.media_refs or [])
                if isinstance(ref, dict) and str(ref.get("type") or "") == "image"
            ]
            for message in evidence_rows
        }
        result = []
        for row in rows:
            item: dict[str, Any] = {
                "type": row.memory_type,
                "key": row.memory_key,
                "content": str(row.content or "")[:600],
                "confidence": round(float(row.confidence or 0.0), 3),
            }
            media_refs = [
                ref
                for message_id in list(row.evidence_message_ids or [])
                if str(message_id).lstrip("-").isdigit()
                for ref in evidence_by_message.get(int(message_id), [])
            ]
            if media_refs:
                item["media_types"] = ["image"] * len(media_refs)
                item["_agent_media_refs"] = media_refs[:8]
            result.append(item)
    elif name == "list_user_relations":
        if session is None:
            raise PermissionError("关系查询需要数据库会话")
        subject_id = int(args["user_id"])
        limit = _tool_result_limit(
            args,
            default=DEFAULT_RELATION_TOOL_LIMIT,
            maximum=MAX_RELATION_TOOL_LIMIT,
        )
        member_names = {
            int(row_user_id): str(nickname or row_user_id)
            for row_user_id, nickname in (
                await session.execute(
                    select(UserGroup.user_id, UserGroup.group_nickname).where(
                        UserGroup.group_id == group_id
                    )
                )
            ).all()
        }
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
        rows = list(
            (
                await session.execute(
                    select(AgentRelation)
                    .where(
                        AgentRelation.group_id == group_id,
                        AgentRelation.subject_user_id.not_in(opted_out),
                        AgentRelation.object_user_id.not_in(opted_out),
                        or_(
                            AgentRelation.subject_user_id == subject_id,
                            AgentRelation.object_user_id == subject_id,
                        ),
                    )
                    .order_by(AgentRelation.confidence.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        result = [
            {
                "subject_user_id": int(row.subject_user_id),
                "subject_name": member_names.get(int(row.subject_user_id)),
                "object_user_id": int(row.object_user_id),
                "object_name": member_names.get(int(row.object_user_id)),
                "type": row.relation_type,
                "note": row.note,
                "effective_confidence": round(
                    effective_relation_confidence(
                        float(row.confidence or 0.0), row.last_seen_at, now
                    ),
                    3,
                ),
                "last_seen_days": max(
                    0,
                    int((now - (row.last_seen_at or now)).total_seconds() // 86400),
                ),
            }
            for row in rows
        ]
        dbg(
            f"群 {group_id} list_user_relations: 成员 {subject_id} "
            f"隐私退出过滤={sorted(opted_out)} 返回 {len(rows)} 条"
        )
    elif name == "record_user_relation":
        if session is None:
            raise PermissionError("关系记录需要数据库会话")
        subject = int(args["subject_user_id"])
        target = int(args["object_user_id"])
        relation_type = normalize_relation_type(args.get("type"))
        note = str(args.get("note") or "").strip()[:200]
        if not relation_type:
            raise ValueError("关系类型不能为空")
        if subject == target:
            raise ValueError("关系两端不能是同一个人")
        member_ids = set(
            (
                await session.execute(
                    select(UserGroup.user_id).where(UserGroup.group_id == group_id)
                )
            )
            .scalars()
            .all()
        )
        # 双方必须是本群真实成员，防止模型把幻觉人物写进关系图。
        if subject not in member_ids or target not in member_ids:
            raise ValueError("关系双方都必须是本群成员")
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
        if subject in opted_out or target in opted_out:
            raise PermissionError("关系一方已隐私退出，不得记录")
        edge = await session.scalar(
            select(AgentRelation).where(
                AgentRelation.group_id == group_id,
                AgentRelation.subject_user_id == subject,
                AgentRelation.object_user_id == target,
                AgentRelation.relation_type == relation_type,
            )
        )
        if edge is not None and edge.source_kind == "manual":
            raise PermissionError("该关系由管理员维护，Agent 不能修改")
        if edge is None:
            session.add(
                AgentRelation(
                    group_id=group_id,
                    subject_user_id=subject,
                    object_user_id=target,
                    relation_type=relation_type,
                    source_kind="agent",
                    note=note,
                    confidence=0.6,
                    evidence_count=1,
                    last_seen_at=now,
                )
            )
            result = {"ok": True, "created": True, "type": relation_type}
        else:
            edge.evidence_count += 1
            if note and not str(edge.note or "").strip():
                edge.note = note
            edge.last_seen_at = now
            result = {
                "ok": True,
                "created": False,
                "type": relation_type,
                "note": edge.note,
            }
        dbg(
            f"群 {group_id} record_user_relation: {subject} "
            f"—{relation_type}→ {target} note={note!r}"
        )
    else:
        raise ValueError(f"{FAMILY} handler 不支持工具: {name}")
    mutated = name == "record_user_relation"
    return ToolHandlerResult(result, mutated_db=mutated, needs_commit=mutated)
