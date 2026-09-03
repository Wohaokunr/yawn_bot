# ruff: noqa: E501, PLR0912, PLR0913, PLR0915, PLR2004, C901, TC001, TC003, TID252
"""Agent 对话上下文加载器。

只负责从 Repository 读取、筛选并装箱 Prompt Context；不负责 LLM 或 Tool 循环。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, exists, func, select

from ..data_models.agent_memory import AgentMemory, AgentPrivacy
from ..data_models.group_agent_config import GroupAgentConfig
from ..data_models.group_agent_message import GroupAgentMessage
from ..metrics import record_agent_context_db_queries
from .context import ActivitySnapshot, build_context, now_beijing, trim_context_messages
from .context_budget import pack_context
from .context_history import history_message_payload as _history_message_payload
from .context_history import select_context_messages
from .context_repository import AgentContextRepository
from .emotion import emotion_context_state
from .log import dbg
from .memory import effective_relation_confidence, rank_memories
from .persona import persona_editor_profile

_MEMORY_CONTEXT_CHAR_BUDGET = 6_000
_MEMORY_CONTEXT_LIMIT = 24


async def activity_window_counts(
    session: Any,
    group_id: int,
    now: datetime,
    *,
    bot_id: int | None = None,
    exclude_user_ids: set[int] | None = None,
    retention_at: datetime | None = None,
) -> dict[str, Any]:
    """60 分钟窗口活跃度的一条 SQL 聚合；对话与主动发言路径共用。

    旧实现从"最新 40/60 条消息"在 Python 侧数窗口，活跃群覆盖不全会
    低估 messages_60m/participants_60m/member_messages_60m；聚合查询不受
    加载条数截断影响。隐私退出用户统一排除（原主动发言路径未排除，
    与对话读路径口径不一致）。last_message_at 不限窗口，供冷场判定。
    """

    clauses: list[Any] = [
        GroupAgentMessage.group_id == group_id,
        (
            GroupAgentMessage.expires_at.is_(None)
            | (GroupAgentMessage.expires_at >= (retention_at or now))
        ),
        GroupAgentMessage.received_at <= now,
    ]
    if exclude_user_ids is not None and exclude_user_ids:
        clauses.append(GroupAgentMessage.user_id.not_in(exclude_user_ids))
    elif exclude_user_ids is None:
        opted_out = select(AgentPrivacy.user_id).where(
            AgentPrivacy.group_id == group_id,
            AgentPrivacy.user_id == GroupAgentMessage.user_id,
            AgentPrivacy.opted_out.is_(True),
        )
        clauses.append(~exists(opted_out))
    if bot_id is not None:
        clauses.append(GroupAgentMessage.bot_id == bot_id)
    in_window = GroupAgentMessage.received_at >= now - timedelta(hours=1)
    in_5m = GroupAgentMessage.received_at >= now - timedelta(minutes=5)
    is_member = GroupAgentMessage.role != "bot"
    row = (
        await session.execute(
            select(
                func.max(GroupAgentMessage.received_at),
                func.max(
                    case((in_window & is_member, GroupAgentMessage.received_at))
                ),
                func.sum(
                    case(
                        (
                            GroupAgentMessage.received_at >= now - timedelta(minutes=5),
                            1,
                        ),
                        else_=0,
                    )
                ),
                func.sum(
                    case(
                        (
                            GroupAgentMessage.received_at
                            >= now - timedelta(minutes=20),
                            1,
                        ),
                        else_=0,
                    )
                ),
                func.sum(case((in_window, 1), else_=0)),
                func.sum(case((in_window & is_member, 1), else_=0)),
                func.sum(case((in_5m & is_member, 1), else_=0)),
                func.count(
                    func.distinct(
                        case((in_5m & is_member, GroupAgentMessage.user_id))
                    )
                ),
                func.count(func.distinct(case((in_window, GroupAgentMessage.user_id)))),
                func.sum(
                    case(
                        (
                            in_window & GroupAgentMessage.normalized_text.contains("@"),
                            1,
                        ),
                        else_=0,
                    )
                ),
                func.sum(
                    case(
                        (
                            in_window
                            & (
                                func.json_array_length(GroupAgentMessage.reply_chain)
                                > 0
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
            ).where(*clauses)
        )
    ).one()
    return {
        "last_message_at": row[0],
        "last_member_message_at": row[1],
        "messages_5m": int(row[2] or 0),
        "messages_20m": int(row[3] or 0),
        "messages_60m": int(row[4] or 0),
        "member_messages_60m": int(row[5] or 0),
        "member_messages_5m": int(row[6] or 0),
        "member_participants_5m": int(row[7] or 0),
        "participants_60m": int(row[8] or 0),
        "mentions_60m": int(row[9] or 0),
        "replies_60m": int(row[10] or 0),
    }


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
    _record_db_queries: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    now = now_beijing()
    context_now = reference_at or now
    repo = AgentContextRepository(session)
    scope = await repo.load_scope_metadata(group_id)
    group = scope.group
    opted_out = set(scope.opted_out_user_ids)
    dbg(
        f"群 {group_id} 加载上下文: 隐私退出用户数={len(opted_out)} ids={sorted(opted_out)}"
    )
    rows = await repo.load_recent_messages(
        group_id,
        now,
        bot_id=bot_id,
        opted_out=opted_out,
        message_cutoff=message_cutoff,
        exclude_message_id=exclude_message_id,
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
    member_rows = await repo.load_members(group_id, relevant_member_ids)
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
    memory_rows = await repo.load_local_memories(
        group_id,
        now,
        focus_ids=focus_ids,
    )
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
        shared_rows = await repo.load_shared_public_summaries(group_id, now)
        source_groups = {int(row.group_id or 0) for row in shared_rows}
        shared_optouts = await repo.load_shared_optouts(source_groups)
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
    relation_rows = await repo.load_relations(
        group_id,
        opted_out=opted_out,
        participant_ids=participant_ids,
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
    # 活跃度由 Repository 用一条聚合 SQL 精确统计 60 分钟窗口。
    counts = await repo.load_activity_window(
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
    (_record_db_queries or record_agent_context_db_queries)(repo.query_count)
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


__all__ = ["activity_window_counts", "load_context"]
