# ruff: noqa: E501,PLR0913,TID252
"""Agent Prompt 上下文的顺序化 DB Repository。

同一个 AsyncSession 上保持串行 await，但减少往返次数；这里集中维护真实查询形状，
便于用 EXPLAIN 和长期 metrics 验证热路径。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, func, or_, select

from ..data_models.agent_memory import AgentMemory, AgentPrivacy, AgentRelation
from ..data_models.bot_group import BotGroup
from ..data_models.group_agent_config import GroupAgentConfig
from ..data_models.group_agent_message import GroupAgentMessage
from ..data_models.user_group import UserGroup

LOCAL_MEMORY_CANDIDATE_LIMIT = 260
RECENT_MEMORY_DAYS = 21
MIN_MENTION_RELATION_EVIDENCE = 2


@dataclass(frozen=True, slots=True)
class ContextScopeMetadata:
    group: BotGroup | None
    opted_out_user_ids: frozenset[int]


class AgentContextRepository:
    """一轮 context build 专用的有界查询入口。"""

    def __init__(self, session: Any) -> None:
        self.session = session
        self.query_count = 0

    async def _execute(self, statement: Any) -> Any:
        self.query_count += 1
        return await self.session.execute(statement)

    async def load_scope_metadata(self, group_id: int) -> ContextScopeMetadata:
        rows = (
            await self._execute(
                select(BotGroup, AgentPrivacy.user_id)
                .outerjoin(
                    AgentPrivacy,
                    and_(
                        AgentPrivacy.group_id == BotGroup.group_id,
                        AgentPrivacy.opted_out.is_(True),
                    ),
                )
                .where(BotGroup.group_id == group_id)
            )
        ).all()
        if not rows:
            return ContextScopeMetadata(None, frozenset())
        group = rows[0][0]
        opted_out = frozenset(int(user_id) for _group, user_id in rows if user_id is not None)
        return ContextScopeMetadata(group, opted_out)

    async def load_recent_messages(
        self,
        group_id: int,
        now: datetime,
        *,
        bot_id: int | None,
        opted_out: set[int] | frozenset[int],
        message_cutoff: datetime | None,
        exclude_message_id: int | None,
    ) -> list[GroupAgentMessage]:
        statement = select(GroupAgentMessage).where(
            GroupAgentMessage.group_id == group_id,
            (
                GroupAgentMessage.expires_at.is_(None)
                | (GroupAgentMessage.expires_at >= now)
            ),
        )
        if message_cutoff is not None:
            statement = statement.where(GroupAgentMessage.received_at <= message_cutoff)
        if opted_out:
            statement = statement.where(GroupAgentMessage.user_id.not_in(opted_out))
        if bot_id is not None:
            statement = statement.where(GroupAgentMessage.bot_id == bot_id)
        if exclude_message_id is not None:
            statement = statement.where(
                GroupAgentMessage.message_id != int(exclude_message_id)
            )
        return list(
            (
                await self._execute(
                    statement.order_by(GroupAgentMessage.id.desc()).limit(40)
                )
            )
            .scalars()
            .all()
        )

    async def load_members(
        self, group_id: int, relevant_member_ids: set[int]
    ) -> list[UserGroup]:
        if not relevant_member_ids:
            return []
        return list(
            (
                await self._execute(
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
        )

    async def load_local_memories(
        self,
        group_id: int,
        now: datetime,
        *,
        focus_ids: list[int],
    ) -> list[AgentMemory]:
        """一次扫描构造 summary/profile/recent/salience 共用候选池。"""

        base = [
            AgentMemory.group_id == group_id,
            AgentMemory.visibility.in_(("group", "public")),
            (AgentMemory.expires_at.is_(None) | (AgentMemory.expires_at >= now)),
        ]
        focus_condition = (
            AgentMemory.subject_user_id.in_(focus_ids)
            & AgentMemory.memory_type.in_(("core", "profile", "manual"))
            if focus_ids
            else None
        )
        recent_cutoff = now - timedelta(days=RECENT_MEMORY_DAYS)
        priority = case(
            *((focus_condition, 0),) if focus_condition is not None else (),
            (AgentMemory.memory_type == "summary", 1),
            (AgentMemory.updated_at >= recent_cutoff, 2),
            else_=3,
        )
        type_priority = case(
            (AgentMemory.memory_type == "core", 0),
            (AgentMemory.memory_type == "profile", 1),
            (AgentMemory.memory_type == "manual", 2),
            else_=3,
        )
        return list(
            (
                await self._execute(
                    select(AgentMemory)
                    .where(*base)
                    .order_by(
                        priority,
                        type_priority,
                        AgentMemory.salience.desc(),
                        AgentMemory.updated_at.desc(),
                        AgentMemory.id.desc(),
                    )
                    .limit(LOCAL_MEMORY_CANDIDATE_LIMIT)
                )
            )
            .scalars()
            .all()
        )

    async def load_shared_public_summaries(
        self, group_id: int, now: datetime
    ) -> list[AgentMemory]:
        return list(
            (
                await self._execute(
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

    async def load_shared_optouts(
        self, source_groups: set[int]
    ) -> set[tuple[int, int]]:
        if not source_groups:
            return set()
        return set(
            (
                await self._execute(
                    select(AgentPrivacy.group_id, AgentPrivacy.user_id).where(
                        AgentPrivacy.group_id.in_(source_groups),
                        AgentPrivacy.opted_out.is_(True),
                    )
                )
            ).all()
        )

    async def load_relations(
        self,
        group_id: int,
        *,
        opted_out: set[int] | frozenset[int],
        participant_ids: set[int],
    ) -> list[AgentRelation]:
        if not participant_ids:
            return []
        return list(
            (
                await self._execute(
                    select(AgentRelation)
                    .where(
                        AgentRelation.group_id == group_id,
                        AgentRelation.subject_user_id.not_in(opted_out),
                        AgentRelation.object_user_id.not_in(opted_out),
                        or_(
                            AgentRelation.source_kind != "mention",
                            AgentRelation.evidence_count
                            >= MIN_MENTION_RELATION_EVIDENCE,
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

    async def load_activity_window(
        self,
        group_id: int,
        now: datetime,
        *,
        bot_id: int | None,
        exclude_user_ids: set[int] | frozenset[int],
        retention_at: datetime,
    ) -> dict[str, Any]:
        clauses: list[Any] = [
            GroupAgentMessage.group_id == group_id,
            (
                GroupAgentMessage.expires_at.is_(None)
                | (GroupAgentMessage.expires_at >= retention_at)
            ),
            GroupAgentMessage.received_at <= now,
        ]
        if exclude_user_ids:
            clauses.append(GroupAgentMessage.user_id.not_in(exclude_user_ids))
        if bot_id is not None:
            clauses.append(GroupAgentMessage.bot_id == bot_id)
        in_window = GroupAgentMessage.received_at >= now - timedelta(hours=1)
        in_5m = GroupAgentMessage.received_at >= now - timedelta(minutes=5)
        is_member = GroupAgentMessage.role != "bot"
        row = (
            await self._execute(
                select(
                    func.max(GroupAgentMessage.received_at),
                    func.max(case((in_window & is_member, GroupAgentMessage.received_at))),
                    func.sum(case((in_5m, 1), else_=0)),
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
                    func.count(
                        func.distinct(case((in_window, GroupAgentMessage.user_id)))
                    ),
                    func.sum(
                        case(
                            (
                                in_window
                                & GroupAgentMessage.normalized_text.contains("@"),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    func.sum(
                        case(
                            (
                                in_window
                                & (func.json_array_length(GroupAgentMessage.reply_chain) > 0),
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


__all__ = ["AgentContextRepository", "ContextScopeMetadata"]
