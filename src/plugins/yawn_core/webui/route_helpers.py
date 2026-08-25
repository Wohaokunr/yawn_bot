# ruff: noqa: TID252
"""Small route-level helpers shared across WebUI API modules."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status

from ..data_models.bot_group import BotGroup
from ..data_models.bot_user import BotUser
from .service import version


async def require_group(session: Any, group_id: int) -> BotGroup:
    group = await session.get(BotGroup, group_id)
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "群不存在")
    return group


async def require_user(session: Any, user_id: int) -> BotUser:
    user = await session.get(BotUser, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    return user


def check_version(row: Any, supplied: str | None) -> None:
    # 乐观锁按 updated_at 序列化值比对；GroupAgentConfig 与 AgentMemory 通用。
    current = version(row.updated_at) if row is not None else None
    if current != supplied:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "配置已被其他操作修改，请刷新后重试"
        )
