# ruff: noqa: TID252, TRY003
from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select

from ...data_models.agent_memory import AgentMemory, AgentPrivacy, AgentRelation
from ...data_models.user_group import UserGroup
from ..context import now_beijing
from ..log import dbg
from ..memory import (
    effective_relation_confidence,
    normalize_relation_type,
)
from ..memory_access import record_access
from ..memory_hygiene import looks_like_instruction, sanitize_fact
from ..memory_retrieval import (
    evidence_images,
    opted_out_user_ids,
    search_group_memories,
)
from ..memory_self import forget_member_facts, remember_member_fact
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
        "remember_about_me",
        "forget_about_me",
    ]
)
_WRITE_TOOLS = frozenset(
    {"record_user_relation", "remember_about_me", "forget_about_me"}
)
# 有效置信度低于该值的关系边（沉寂已久的弱观察）不再返回给模型。
_MIN_RELATION_EFFECTIVE_CONFIDENCE = 0.2
# Agent 重复观察同一关系时的置信度增量与上限；未经管理员确认不超过 0.9。
_AGENT_RELATION_STEP = 0.05
_AGENT_RELATION_CAP = 0.9
# 调用者不是关系任何一方时（替别人记关系），初始置信度更低。
_AGENT_RELATION_THIRD_PARTY_CONFIDENCE = 0.45
_PROFILE_TYPE_ORDER = {"core": 0, "profile": 1}


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
            # 旧实现无 ORDER BY：画像超过 limit 时返回的是数据库任意子集，
            # 称呼这类核心事实可能被挤掉。这里核心优先、再按置信度与新鲜度。
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
                .order_by(
                    AgentMemory.confidence.desc(),
                    AgentMemory.updated_at.desc(),
                    AgentMemory.id.desc(),
                )
                .limit(30)
            )
            rows = (
                list((await session.execute(stmt)).scalars().all())
                if session is not None
                else []
            )
            rows.sort(
                key=lambda row: _PROFILE_TYPE_ORDER.get(str(row.memory_type), 2)
            )
            rows = rows[:limit]
            record_access((int(row.id or 0) for row in rows), now=now)
        result = [
            {
                "key": row.memory_key,
                "content": str(row.content or "")[:600],
                "confidence": round(float(row.confidence or 0.0), 3),
                "core": row.memory_type == "core",
                "self_reported": row.source_kind == "agent",
                "last_confirmed": (row.updated_at or now).date().isoformat(),
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
        rows = (
            await search_group_memories(
                session, group_id, query, limit=limit, now=now
            )
            if session is not None
            else []
        )
        images = (
            await evidence_images(session, group_id, rows)
            if session is not None
            else {}
        )
        result = []
        for row in rows:
            item: dict[str, Any] = {
                "type": row.memory_type,
                "key": row.memory_key,
                "content": str(row.content or "")[:600],
                "confidence": round(float(row.confidence or 0.0), 3),
                "date": (row.updated_at or now).date().isoformat(),
            }
            if int(row.subject_user_id or 0):
                item["subject_user_id"] = int(row.subject_user_id)
            media_refs = images.get(int(row.id or 0), [])
            if media_refs:
                item["media_types"] = ["image"] * len(media_refs)
                item["_agent_media_refs"] = media_refs[:8]
            result.append(item)
        dbg(
            f"群 {group_id} search_group_memory: query={query!r} "
            f"返回 {len(result)} 条"
        )
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
        opted_out = await opted_out_user_ids(session, group_id)
        if subject_id in opted_out:
            rows = []
        else:
            # 先取宽候选，再按"原始置信度 × 最后见到降权"排序：沉寂半年的
            # 高置信老边不应压过上周刚确认的新边。
            candidates = list(
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
                        .limit(max(limit * 3, 30))
                    )
                )
                .scalars()
                .all()
            )
            scored = [
                (
                    effective_relation_confidence(
                        float(row.confidence or 0.0), row.last_seen_at, now
                    ),
                    row,
                )
                for row in candidates
            ]
            scored = [
                item
                for item in scored
                if item[0] >= _MIN_RELATION_EFFECTIVE_CONFIDENCE
                or item[1].source_kind == "manual"
            ]
            scored.sort(key=lambda item: (-item[0], -int(item[1].id or 0)))
            rows = [row for _score, row in scored[:limit]]
        result = [
            {
                "subject_user_id": int(row.subject_user_id),
                "subject_name": member_names.get(int(row.subject_user_id)),
                "object_user_id": int(row.object_user_id),
                "object_name": member_names.get(int(row.object_user_id)),
                "type": row.relation_type,
                "note": row.note,
                "source": row.source_kind,
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
        note = sanitize_fact(str(args.get("note") or "")) or ""
        if not relation_type:
            raise ValueError("关系类型不能为空")
        if looks_like_instruction(relation_type):
            raise ValueError("关系类型不能包含指令")
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
        opted_out = await opted_out_user_ids(session, group_id)
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
        actor_is_party = context.actor_user_id in (subject, target)
        if edge is None:
            session.add(
                AgentRelation(
                    group_id=group_id,
                    subject_user_id=subject,
                    object_user_id=target,
                    relation_type=relation_type,
                    source_kind="agent",
                    note=note,
                    confidence=(
                        0.6
                        if actor_is_party
                        else _AGENT_RELATION_THIRD_PARTY_CONFIDENCE
                    ),
                    evidence_count=1,
                    last_seen_at=now,
                )
            )
            result = {"ok": True, "created": True, "type": relation_type}
        else:
            edge.evidence_count += 1
            # 再次观察到同一关系逐步增强；auto 边被对话确认后升为 agent 边，
            # 之后的自动整理不再覆盖它。
            edge.confidence = min(
                _AGENT_RELATION_CAP,
                max(float(edge.confidence or 0.0), 0.5) + _AGENT_RELATION_STEP,
            )
            if edge.source_kind in ("auto", "mention") and actor_is_party:
                edge.source_kind = "agent"
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
    elif name == "remember_about_me":
        if session is None:
            raise PermissionError("记忆写入需要数据库会话")
        if context.actor_user_id is None:
            raise PermissionError("记忆写入需要明确的调用者")
        result = await remember_member_fact(
            session,
            group_id,
            int(context.actor_user_id),
            str(args.get("key") or ""),
            str(args.get("content") or ""),
            now=now,
        )
        dbg(
            f"群 {group_id} remember_about_me: 成员 {context.actor_user_id} "
            f"key={result['key']} created={result['created']}"
        )
    elif name == "forget_about_me":
        if session is None:
            raise PermissionError("记忆删除需要数据库会话")
        if context.actor_user_id is None:
            raise PermissionError("记忆删除需要明确的调用者")
        result = await forget_member_facts(
            session,
            group_id,
            int(context.actor_user_id),
            raw_key=str(args.get("key") or "") or None,
            keyword=str(args.get("keyword") or "") or None,
        )
        dbg(
            f"群 {group_id} forget_about_me: 成员 {context.actor_user_id} "
            f"removed={result['removed']} trimmed={result['trimmed']}"
        )
    else:
        raise ValueError(f"{FAMILY} handler 不支持工具: {name}")
    mutated = name in _WRITE_TOOLS
    return ToolHandlerResult(result, mutated_db=mutated, needs_commit=mutated)
