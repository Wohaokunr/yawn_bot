# ruff: noqa: TID252,PLR0913
"""Shared bounded group activity aggregates for dialogue/proactive paths."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, exists, func, select

from ..data_models.agent_audit import AgentAudit
from ..data_models.agent_memory import AgentPrivacy
from ..data_models.group_agent_message import GroupAgentMessage
from .idle_warmup import WARMUP_SENT_RESULTS, WARMUP_SKIP_ACTIONS, WarmupState

# 一段沉默里最多回看多少条主动审计；暖场被门槛压得很稀，远够用。
_WARMUP_AUDIT_SCAN_LIMIT = 60
_WARMUP_TODAY_SCAN_LIMIT = 200
_WARMUP_TOPIC_LIMIT = 3


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
                func.max(case((in_window & is_member, GroupAgentMessage.received_at))),
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
                    func.distinct(case((in_5m & is_member, GroupAgentMessage.user_id)))
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


def _audit_scene(row: Any) -> tuple[str, str]:
    arguments = row.arguments if isinstance(row.arguments, dict) else {}
    return str(arguments.get("scene") or ""), str(arguments.get("action") or "")


async def warmup_state(session: Any, group_id: int, now: datetime) -> WarmupState:
    """推导当前这段沉默的暖场事实，全部来自消息与审计，重启不丢。

    “真人”包含隐私退出成员：他们的消息不进 Prompt，但他们在说话就说明
    群没冷场，旧逻辑把他们排除后会对着正在聊天的群暖场。
    """

    last_human_at = await session.scalar(
        select(func.max(GroupAgentMessage.received_at)).where(
            GroupAgentMessage.group_id == group_id,
            GroupAgentMessage.role != "bot",
            GroupAgentMessage.received_at <= now,
        )
    )
    last_bot_message_at = await session.scalar(
        select(func.max(GroupAgentMessage.received_at)).where(
            GroupAgentMessage.group_id == group_id,
            GroupAgentMessage.role == "bot",
            GroupAgentMessage.received_at <= now,
        )
    )
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    since = min(last_human_at, today) if last_human_at is not None else today
    rows = (
        (
            await session.execute(
                select(AgentAudit)
                .where(
                    AgentAudit.group_id == group_id,
                    AgentAudit.tool_name == "proactive_reply",
                    AgentAudit.created_at > since,
                    AgentAudit.created_at <= now,
                )
                .order_by(AgentAudit.id.desc())
                .limit(_WARMUP_AUDIT_SCAN_LIMIT + _WARMUP_TODAY_SCAN_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    unanswered = 0
    skips = 0
    closed = False
    warmups_today = 0
    last_skip_at: datetime | None = None
    last_sent_at: datetime | None = None
    topics: list[str] = []
    for row in rows:
        scene, action = _audit_scene(row)
        result = str(row.result or "")
        created_at = row.created_at
        sent = result in WARMUP_SENT_RESULTS
        if scene == "warmup" and sent and created_at >= today:
            warmups_today += 1
        if last_human_at is not None and created_at <= last_human_at:
            continue
        if sent:
            unanswered += 1
            if last_sent_at is None or created_at > last_sent_at:
                last_sent_at = created_at
            arguments = row.arguments if isinstance(row.arguments, dict) else {}
            topic = str(arguments.get("topic") or "").strip()
            if topic and topic not in topics and len(topics) < _WARMUP_TOPIC_LIMIT:
                topics.append(topic[:40])
        elif scene == "warmup" and action == "close":
            closed = True
        elif scene == "warmup" and action in WARMUP_SKIP_ACTIONS:
            skips += 1
            if last_skip_at is None or created_at > last_skip_at:
                last_skip_at = created_at
    candidates = [item for item in (last_bot_message_at, last_sent_at) if item]
    return WarmupState(
        last_human_at=last_human_at,
        last_bot_at=max(candidates) if candidates else None,
        unanswered=unanswered,
        skips=skips,
        last_skip_at=last_skip_at,
        closed=closed,
        warmups_today=warmups_today,
        recent_topics=tuple(topics),
    )


__all__ = ["activity_window_counts", "warmup_state"]
