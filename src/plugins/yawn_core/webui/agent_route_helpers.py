# ruff: noqa: TID252
"""Agent WebUI route 共用的低层过滤 helper。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import BigInteger, cast, exists, func, select

from ..data_models.agent_memory import AgentMemory
from ..superuser import superuser_full_access


def is_guest_view(session: Any) -> bool:
    """访客和个人账号（群管理员）都只看精简投影：没有来源、证据数等治理字段。

    超级管理员用 QQ 个人账号登录时和运维管理员一样看完整字段（全权开着时）。
    """

    role = getattr(session, "role", "admin")
    if role == "user" and superuser_full_access(getattr(session, "user_id", None)):
        return False
    return role in {"guest", "user"}


def memory_privacy_clauses(user_ids: set[int]) -> tuple[Any, ...]:
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


__all__ = ["is_guest_view", "memory_privacy_clauses"]
