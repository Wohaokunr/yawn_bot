# ruff: noqa: TID252,PLR0913
"""Shared bounded group activity aggregates for dialogue/proactive paths."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, exists, func, select

from ..data_models.agent_memory import AgentPrivacy
from ..data_models.group_agent_message import GroupAgentMessage


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


__all__ = ["activity_window_counts"]
