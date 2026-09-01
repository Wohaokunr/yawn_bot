"""Prompt context loading; kept outside dialogue orchestration by design."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import case, or_, select

from ..data_models.agent_memory import AgentMemory, AgentPrivacy, AgentRelation
from ..data_models.bot_group import BotGroup
from ..data_models.group_agent_config import GroupAgentConfig
from ..data_models.group_agent_message import GroupAgentMessage
from ..data_models.user_group import UserGroup
from .activity import activity_window_counts as _activity_window_counts
from .context import ActivitySnapshot, build_context, now_beijing, trim_context_messages
from .context_budget import pack_context
from .context_history import history_message_payload as _history_message_payload, select_context_messages
from .emotion import emotion_context_state
from .log import dbg
from .memory import effective_relation_confidence, rank_memories
from .persona import persona_editor_profile

_MEMORY_CONTEXT_CHAR_BUDGET = 6_000
_MEMORY_CONTEXT_LIMIT = 24

async def load_context(
    session: Any,
    group_id: int,
    config: GroupAgentConfig,
    bot_id: int | None = None,
    *,
    focus_user_ids: Sequence[int] | None = None,
    query_text: str | None = None,
    compact_history: bool = False,
    message_cutoff: datetime | None = None,
    include_active_profiles: bool = False,
    exclude_message_id: int | None = None,
    reference_at: datetime | None = None,
    selection_trace: list[dict[str, Any]] | None = None,
    budget_trace: list[dict[str, Any]] | None = None,
    context_model: str | None = None,
    completion_reserve: int = 2048,
    context_token_limit: int | None = None,
) -> dict[str, Any]:
    now = now_beijing()
    context_now = reference_at or now
    # 隐私退出是读路径级别的：历史消息同样不得进入提示词。
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
    dbg(
        f"群 {group_id} 加载上下文: 隐私退出用户数={len(opted_out)} ids={sorted(opted_out)}"
    )
    message_stmt = select(GroupAgentMessage).where(
        GroupAgentMessage.group_id == group_id,
        (
            GroupAgentMessage.expires_at.is_(None)
            | (GroupAgentMessage.expires_at >= now)
        ),
    )
    if message_cutoff is not None:
        message_stmt = message_stmt.where(
            GroupAgentMessage.received_at <= message_cutoff
        )
    if opted_out:
        message_stmt = message_stmt.where(GroupAgentMessage.user_id.not_in(opted_out))
    if bot_id is not None:
        message_stmt = message_stmt.where(GroupAgentMessage.bot_id == bot_id)
    if exclude_message_id is not None:
        message_stmt = message_stmt.where(
            GroupAgentMessage.message_id != int(exclude_message_id)
        )
    rows = (
        (
            await session.execute(
                message_stmt.order_by(GroupAgentMessage.id.desc()).limit(40)
            )
        )
        .scalars()
        .all()
    )
    messages_unbounded: list[dict[str, Any]] = []
    previous_at: datetime | None = None
    for row in reversed(rows):
        messages_unbounded.append(
            _history_message_payload(
                row,
                context_now=context_now,
                previous_at=previous_at,
            )
        )
        previous_at = row.received_at
    if query_text is None and not compact_history:
        # 兼容内部/测试调用：没有当前回合查询、也没有主动会话语义时，
        # 保持原来的有界历史行为。线上被动对话会显式传 query_text；
        # 主动发言/短会话会显式传 compact_history=True。
        messages = trim_context_messages(messages_unbounded)
    else:
        selection = select_context_messages(
            messages_unbounded,
            focus_user_ids=focus_user_ids,
            query_text=query_text,
        )
        messages = selection.messages
        if selection_trace is not None:
            selection_trace.extend(selection.trace)
    dbg(
        f"群 {group_id} 加载上下文: 原始历史 {len(messages_unbounded)} 条 -> "
        f"有效历史 {len(messages)} 条/"
        f"{sum(len(str(item.get('text') or '')) for item in messages)} 字"
    )
    # 记忆相关性只看最终实际注入的历史，并显式加入当前回合文本。
    recent_texts = [str(item["text"] or "") for item in messages[-6:]]
    if query_text:
        recent_texts.append(str(query_text)[:1000])
    previous_speaker_id = next(
        (
            int(item["user_id"])
            for item in reversed(messages)
            if item.get("role") != "bot"
        ),
        None,
    )
    recent_member_ids: list[int] = []
    for item in reversed(messages):
        if item.get("role") == "bot":
            continue
        member_id = int(item["user_id"])
        if member_id not in recent_member_ids:
            recent_member_ids.append(member_id)
    requested_focus = [int(user_id) for user_id in (focus_user_ids or [])]
    speaker_id = requested_focus[0] if requested_focus else previous_speaker_id
    focus_ids: list[int] = []
    focus_candidates = [
        *requested_focus,
        *(
            recent_member_ids
            if include_active_profiles or requested_focus
            else ([speaker_id] if speaker_id is not None else [])
        ),
    ]
    for member_id in focus_candidates:
        if member_id in opted_out or member_id in focus_ids:
            continue
        focus_ids.append(member_id)
        if len(focus_ids) >= 4:
            break
    relevant_member_ids = {
        int(item["user_id"])
        for item in messages
        if item.get("role") != "bot"
    }
    relevant_member_ids.update(focus_ids)
    member_rows = (
        (
            await session.execute(
                select(UserGroup)
                .where(
                    UserGroup.group_id == group_id,
                    UserGroup.user_id.in_(relevant_member_ids),
                )
                .order_by(UserGroup.last_seen_at.desc())
            )
        )
        .scalars()
        .all()
        if relevant_member_ids
        else []
    )
    members = [
        {
            "user_id": row.user_id,
            "name": row.group_nickname,
            "role": row.role,
            "title": row.title,
            "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
        }
        for row in member_rows
    ]
    dbg(
        f"群 {group_id} 加载上下文: 近期相关成员 {len(members)} 人"
        f"(候选 {len(relevant_member_ids)})"
    )
    memory_clauses = [
        AgentMemory.group_id == group_id,
        AgentMemory.visibility.in_(("group", "public")),
        (AgentMemory.expires_at.is_(None) | (AgentMemory.expires_at >= now)),
    ]
    candidate_rows = (
        (
            await session.execute(
                select(AgentMemory)
                .where(*memory_clauses)
                .order_by(AgentMemory.salience.desc(), AgentMemory.updated_at.desc())
                .limit(160)
            )
        )
        .scalars()
        .all()
    )
    # 复现池捞回显著度榜外但近期被再确认的记忆：只按 salience 取候选会
    # 让安静复述的旧知识结构性不可见。
    recency_rows = (
        (
            await session.execute(
                select(AgentMemory)
                .where(*memory_clauses)
                .order_by(AgentMemory.updated_at.desc(), AgentMemory.id.desc())
                .limit(60)
            )
        )
        .scalars()
        .all()
    )
    summary_rows = (
        (
            await session.execute(
                select(AgentMemory)
                .where(*memory_clauses, AgentMemory.memory_type == "summary")
                .order_by(AgentMemory.updated_at.desc())
                .limit(40)
            )
        )
        .scalars()
        .all()
    )
    focus_rows: list[AgentMemory] = []
    if focus_ids:
        # 活跃成员画像独立查询，不能先和群级高显著记忆争抢 SQL LIMIT。
        # core 行排最前；随后按成员轮转分配 12 条预算，避免一人占满。
        focus_rows = list(
            (
                await session.execute(
                    select(AgentMemory)
                    .where(
                        *memory_clauses,
                        AgentMemory.subject_user_id.in_(focus_ids),
                        AgentMemory.memory_type.in_(("core", "profile", "manual")),
                    )
                    .order_by(
                        case(
                            (AgentMemory.memory_type == "core", 0),
                            (AgentMemory.memory_type == "profile", 1),
                            else_=2,
                        ),
                        AgentMemory.salience.desc(),
                        AgentMemory.updated_at.desc(),
                    )
                    .limit(96)
                )
            )
            .scalars()
            .all()
        )

    memory_rows = [*focus_rows, *summary_rows, *candidate_rows, *recency_rows]
    seen_local_ids: set[int] = set()
    local_rows: list[AgentMemory] = []
    for row in memory_rows:
        row_id = int(row.id or 0)
        if row_id in seen_local_ids:
            continue
        seen_local_ids.add(row_id)
        if (
            str(row.memory_key).startswith("public_daily:")
            or int(row.subject_user_id or 0) in opted_out
            or opted_out.intersection(set(row.related_user_ids or []))
        ):
            continue
        local_rows.append(row)
    focus_by_member: dict[int, list[AgentMemory]] = {user_id: [] for user_id in focus_ids}
    for row in local_rows:
        subject_id = int(row.subject_user_id or 0)
        if (
            subject_id in focus_by_member
            and row.memory_type in {"core", "profile", "manual"}
        ):
            focus_by_member[subject_id].append(row)
    focused_profiles: list[AgentMemory] = []
    for position in range(32):
        for member_id in focus_ids:
            rows_for_member = focus_by_member[member_id]
            if position < len(rows_for_member):
                focused_profiles.append(rows_for_member[position])
                if len(focused_profiles) >= 12:
                    break
        if len(focused_profiles) >= 12 or not any(
            position + 1 < len(rows) for rows in focus_by_member.values()
        ):
            break
    summaries = [row for row in local_rows if row.memory_type == "summary"]
    ranked_local = rank_memories(
        local_rows,
        recent_texts,
        speaker_id,
        context_now,
        limit=40,
        topic_hint=str(config.active_topic or ""),
    )

    shared_rows: list[AgentMemory] = []
    if config.cross_group_visibility == "public_summary":
        shared_rows = list(
            (
                await session.execute(
                    select(AgentMemory)
                    .join(
                        GroupAgentConfig,
                        GroupAgentConfig.group_id == AgentMemory.group_id,
                    )
                    .where(
                        AgentMemory.group_id != group_id,
                        AgentMemory.memory_type == "summary",
                        AgentMemory.visibility == "public",
                        AgentMemory.memory_key.startswith("public_daily:"),
                        AgentMemory.expires_at.is_not(None),
                        AgentMemory.expires_at >= now,
                        GroupAgentConfig.cross_group_visibility == "public_summary",
                    )
                    .order_by(AgentMemory.updated_at.desc())
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
        source_groups = {int(row.group_id or 0) for row in shared_rows}
        shared_optouts = set(
            (
                await session.execute(
                    select(AgentPrivacy.group_id, AgentPrivacy.user_id).where(
                        AgentPrivacy.group_id.in_(source_groups),
                        AgentPrivacy.opted_out.is_(True),
                    )
                )
            ).all()
        ) if source_groups else set()
        shared_rows = [
            row
            for row in shared_rows
            if not any(
                (int(row.group_id or 0), int(user_id)) in shared_optouts
                for user_id in row.related_user_ids or []
            )
        ]
        # 跨群共享摘要按 updated_at 确定性取前 4：候选超过 4 条时若按
        # 话题相关性重排，选择会随每条新消息翻转，击穿稳定层前缀缓存。
        shared_rows = shared_rows[:4]

    ordered: list[tuple[AgentMemory, str]] = []
    seen_memory_ids: set[int] = set()
    # source_scope 同时决定记忆进入提示词的稳定层（group_summary/shared_public，
    # 只随整理变化、可被前缀缓存命中）还是易变层（speaker/topic，随请求变化）。
    # 稳定来源排在前面：6000 字符预算从前往后消耗，稳定条目先入账，
    # 其截断点才不会随发言人画像的长短浮动。
    # 活跃成员画像总预算 12 条，按成员轮转；话题记忆维持最多 3 条。
    topic_rows = ranked_local[:3]
    for row, source in [
        *((row, "group_summary") for row in summaries[:5]),
        *((row, "shared_public") for row in shared_rows),
        *((
            row,
            "speaker"
            if speaker_id is not None
            and int(row.subject_user_id or 0) == speaker_id
            else "participant",
        ) for row in focused_profiles),
        *((row, "topic") for row in topic_rows),
    ]:
        row_id = int(row.id or 0)
        if row_id in seen_memory_ids:
            continue
        seen_memory_ids.add(row_id)
        ordered.append((row, source))

    name_by_id = {
        int(item["user_id"]): str(item.get("name") or item["user_id"])
        for item in members
    }
    for item in messages:
        user_id = int(item["user_id"])
        if item.get("name") and user_id not in name_by_id:
            name_by_id[user_id] = str(item["name"])
    memories: list[dict[str, Any]] = []
    # 以最终 JSON 数组的真实字符数计预算（含 [] 和条目间的 ", "）。
    memory_chars = 2
    for row, source in ordered:
        content = str(row.content or "").strip()
        if not content:
            continue
        if len(memories) >= _MEMORY_CONTEXT_LIMIT:
            break
        subject_id = int(row.subject_user_id or 0)
        item: dict[str, Any] = {
            "type": row.memory_type,
            "subject_user_id": subject_id or None,
            "subject_name": name_by_id.get(subject_id) if subject_id else None,
            "key": row.memory_key,
            "content": "",
            "salience": row.salience,
            "confidence": row.confidence,
            "source": row.source_kind,
            "evidence_count": len(row.evidence_message_ids or []),
            "first_observed_date": (row.created_at or now).date().isoformat(),
            "last_confirmed_date": (row.updated_at or row.created_at or now).date().isoformat(),
            "source_scope": source,
            "source_date": str(row.memory_key).rsplit(":", 1)[-1]
            if "daily:" in str(row.memory_key)
            else (row.updated_at or row.created_at or now).date().isoformat(),
        }
        overhead = len(json.dumps(item, ensure_ascii=False))
        separator_chars = 2 if memories else 0
        remaining = (
            _MEMORY_CONTEXT_CHAR_BUDGET
            - memory_chars
            - separator_chars
            - overhead
        )
        if remaining <= 0:
            break
        item["content"] = content[:remaining]
        item_chars = len(json.dumps(item, ensure_ascii=False))
        memories.append(item)
        memory_chars += separator_chars + item_chars
    dbg(
        f"群 {group_id} 加载上下文: 记忆 {len(memories)} 条/"
        f"{memory_chars} 字(预算 {_MEMORY_CONTEXT_CHAR_BUDGET},当前发言人={speaker_id})"
    )
    participant_ids = {
        int(item["user_id"]) for item in messages if item.get("role") != "bot"
    }
    participant_ids.update(focus_ids)
    relation_rows: list[AgentRelation] = []
    if participant_ids:
        # 先在 SQL 层限定当前上下文参与者，再取候选池，避免无关高置信边
        # 把低一些但当前真正相关的关系挤出候选集。
        relation_rows = list(
            (
                await session.execute(
                    select(AgentRelation)
                    .where(
                        AgentRelation.group_id == group_id,
                        AgentRelation.subject_user_id.not_in(opted_out),
                        AgentRelation.object_user_id.not_in(opted_out),
                        or_(
                            AgentRelation.source_kind != "mention",
                            AgentRelation.evidence_count >= 2,
                        ),
                        or_(
                            AgentRelation.subject_user_id.in_(participant_ids),
                            AgentRelation.object_user_id.in_(participant_ids),
                        ),
                    )
                    .order_by(AgentRelation.confidence.desc())
                    .limit(60)
                )
            )
            .scalars()
            .all()
        )

    def _relation_label(user_id: int) -> str:
        # 只加载近期相关成员；解析不到名字时兜底 QQ 号，避免渲染成 null。
        name = name_by_id.get(int(user_id))
        return f"{name}({user_id})" if name else str(user_id)

    # 生效置信度按"最后见到"分段降权：沉寂数月的老边让位给近期仍在互动的新边。
    ranked_relations = sorted(
        relation_rows,
        key=lambda row: (
            -effective_relation_confidence(
                float(row.confidence or 0.0), row.last_seen_at, context_now
            ),
            -int(row.evidence_count or 0),
            int(row.id or 0),
        ),
    )[:20]
    relations: list[str] = []
    for row in ranked_relations:
        line = (
            f"{_relation_label(int(row.subject_user_id))} "
            f"—{row.relation_type}→ {_relation_label(int(row.object_user_id))}"
        )
        note = str(row.note or "").strip()
        relations.append(f"{line}：{note}" if note else line)
    context_pack = pack_context(
        messages=messages,
        members=members,
        memories=memories,
        relations=relations,
        model=context_model,
        completion_reserve=completion_reserve,
        target_context_limit=context_token_limit,
    )
    messages = context_pack.messages
    members = context_pack.members
    memories = context_pack.memories
    relations = context_pack.relations
    if budget_trace is not None:
        budget_trace.extend(context_pack.trace)
    dbg(
        f"群 {group_id} token 上下文装箱: model={context_pack.budget.model!r} "
        f"window={context_pack.budget.context_window} "
        f"context={context_pack.trace[0]['usedTokens']}/{context_pack.budget.context_limit} tokens"
    )
    dbg(
        f"群 {group_id} 加载上下文: 关系 {len(relations)} 条"
        f"(候选 {len(relation_rows)},上限 20)"
    )
    # 活跃度改用聚合查询精确统计 60 分钟窗口，不再受最新 40 条截断影响。
    counts = await _activity_window_counts(
        session,
        group_id,
        context_now,
        bot_id=bot_id,
        exclude_user_ids=opted_out,
        retention_at=now,
    )
    activity = ActivitySnapshot(
        counts["last_message_at"],
        messages_5m=counts["messages_5m"],
        messages_20m=counts["messages_20m"],
        messages_60m=counts["messages_60m"],
        participants_60m=counts["participants_60m"],
        replies_60m=counts["replies_60m"],
        mentions_60m=counts["mentions_60m"],
        last_agent_at=config.last_agent_at,
        proactive_today=config.proactive_count,
        last_member_message_at=counts["last_member_message_at"],
        member_messages_60m=counts["member_messages_60m"],
        member_messages_5m=counts["member_messages_5m"],
        member_participants_5m=counts["member_participants_5m"],
    )
    dbg(
        f"群 {group_id} 活跃度快照: 5m={activity.messages_5m} 20m={activity.messages_20m} "
        f"60m={activity.messages_60m} 参与人数={activity.participants_60m} "
        f"回复数={activity.replies_60m} 提及数={activity.mentions_60m} "
        f"5m真人={activity.member_messages_5m}/"
        f"{activity.member_participants_5m}人 "
        f"今日主动发言={activity.proactive_today} 最后消息={activity.last_message_at} "
        f"最后发言={activity.last_agent_at}"
    )
    group = await session.get(BotGroup, group_id)
    dbg(f"群 {group_id} 上下文组装完成: 群名={group.group_name if group else None!r}")
    return build_context(
        group_id=group_id,
        group_name=group.group_name if group else None,
        messages=messages,
        members=members,
        memories=memories,
        relations=relations,
        activity=activity,
        active_topic=config.active_topic,
        emotion_state=emotion_context_state(
            config.emotion_state if isinstance(config.emotion_state, dict) else {},
            now=context_now,
            expressiveness=persona_editor_profile(config).expressiveness,
        ),
        reference_at=context_now,
    )


__all__ = ["load_context"]
