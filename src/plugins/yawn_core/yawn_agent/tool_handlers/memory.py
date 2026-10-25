# ruff: noqa: TID252, TRY003
from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select

from ...data_models.agent_memory import AgentMemory, AgentPrivacy, AgentRelation
from ...data_models.user_group import UserGroup
from ..bot_bond import (
    BOND_MEMORY_TYPE,
    bond_summary,
    forget_member_bond,
    note_member_bond,
    prefetch_bonds,
)
from ..context import now_beijing
from ..log import dbg
from ..memory import (
    SYMMETRIC_RELATION_TYPES,
    dedupe_symmetric_relations,
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
        "get_my_relationship",
        "note_our_bond",
    ]
)
_WRITE_TOOLS = frozenset(
    {"record_user_relation", "remember_about_me", "forget_about_me", "note_our_bond"}
)
# 关系查询工具一次返回的共同经历条数。
_BOND_TOOL_MOMENTS = 5
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
            rows = dedupe_symmetric_relations([row for _score, row in scored])[
                :limit
            ]
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
        # 无方向关系（好友、同学等）复用已存在的反向边，不再造镜像重复边。
        directions = [(subject, target)]
        if relation_type in SYMMETRIC_RELATION_TYPES:
            directions.append((target, subject))
        edge = await session.scalar(
            select(AgentRelation)
            .where(
                AgentRelation.group_id == group_id,
                AgentRelation.relation_type == relation_type,
                or_(
                    *(
                        (AgentRelation.subject_user_id == left)
                        & (AgentRelation.object_user_id == right)
                        for left, right in directions
                    )
                ),
            )
            .order_by(AgentRelation.id)
            .limit(1)
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
    elif name == "get_my_relationship":
        if session is None:
            raise PermissionError("关系查询需要数据库会话")
        raw_target = args.get("user_id")
        subject_id = (
            int(raw_target)
            if raw_target is not None
            else int(context.actor_user_id or 0)
        )
        if subject_id <= 0:
            raise ValueError("需要 user_id 或明确的调用者")
        bot_id = int(getattr(context.bot, "self_id", 0) or 0)
        opted_out = await opted_out_user_ids(session, group_id)
        bonds = (
            None
            if subject_id in opted_out
            else await prefetch_bonds(session, group_id, user_ids={subject_id})
        )
        bond = (bonds or {}).get((bot_id, subject_id))
        if bond is None:
            result = {"user_id": subject_id, "known": False}
        else:
            summary = bond_summary(bond, now)
            moment_rows = list(
                (
                    await session.execute(
                        select(AgentMemory)
                        .where(
                            AgentMemory.group_id == group_id,
                            AgentMemory.subject_user_id == subject_id,
                            AgentMemory.memory_type == BOND_MEMORY_TYPE,
                            (
                                AgentMemory.expires_at.is_(None)
                                | (AgentMemory.expires_at >= now)
                            ),
                        )
                        .order_by(AgentMemory.updated_at.desc())
                        .limit(_BOND_TOOL_MOMENTS)
                    )
                )
                .scalars()
                .all()
            )
            record_access((int(row.id or 0) for row in moment_rows), now=now)
            result = {
                "known": True,
                **{
                    key: value
                    for key, value in summary.items()
                    if key != "bot_id" and value not in ("", [], None)
                },
                "shared_moments": [
                    {
                        "content": str(row.content or ""),
                        "date": (row.updated_at or now).date().isoformat(),
                    }
                    for row in moment_rows
                ],
            }
        dbg(
            f"群 {group_id} get_my_relationship: 成员 {subject_id} "
            f"known={result.get('known')}"
        )
    elif name == "note_our_bond":
        if session is None:
            raise PermissionError("关系写入需要数据库会话")
        if context.actor_user_id is None:
            raise PermissionError("关系写入需要明确的调用者")
        actor_id = int(context.actor_user_id)
        if actor_id in await opted_out_user_ids(session, group_id):
            raise PermissionError("该成员已退出 Agent 记忆，不能写入")
        result = await note_member_bond(
            session,
            group_id=group_id,
            bot_id=int(getattr(context.bot, "self_id", 0) or 0),
            user_id=actor_id,
            kind=str(args.get("kind") or ""),
            content=str(args.get("content") or ""),
            now=now,
        )
        dbg(
            f"群 {group_id} note_our_bond: 成员 {actor_id} kind={result['kind']} "
            f"created={result['created']}"
        )
    elif name == "forget_about_me" and str(args.get("key") or "") == "bot_bond":
        if session is None:
            raise PermissionError("记忆删除需要数据库会话")
        if context.actor_user_id is None:
            raise PermissionError("记忆删除需要明确的调用者")
        forgotten = await forget_member_bond(
            session, group_id=group_id, user_id=int(context.actor_user_id)
        )
        result = {
            "removed": (
                ["bot_bond"] if forgotten["removed"] or forgotten["moments"] else []
            ),
            "trimmed": [],
            **({"protected": ["bot_bond"]} if forgotten["protected"] else {}),
        }
        dbg(
            f"群 {group_id} forget_about_me(bot_bond): 成员 {context.actor_user_id} "
            f"结果 {forgotten}"
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
        if not args.get("key") and not args.get("keyword"):
            # “把我忘掉”不带范围时，连同他和 Bot 之间的关系一起忘掉。
            forgotten = await forget_member_bond(
                session, group_id=group_id, user_id=int(context.actor_user_id)
            )
            if forgotten["removed"] or forgotten["moments"]:
                result["removed"] = [*result["removed"], "bot_bond"]
        dbg(
            f"群 {group_id} forget_about_me: 成员 {context.actor_user_id} "
            f"removed={result['removed']} trimmed={result['trimmed']}"
        )
    else:
        raise ValueError(f"{FAMILY} handler 不支持工具: {name}")
    mutated = name in _WRITE_TOOLS
    return ToolHandlerResult(result, mutated_db=mutated, needs_commit=mutated)
