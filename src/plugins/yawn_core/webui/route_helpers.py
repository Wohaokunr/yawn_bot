# ruff: noqa: TID252
"""Small route-level helpers shared across WebUI API modules."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, status

from ..data_models.bot_group import BotGroup
from ..data_models.bot_user import BotUser
from .service import version

if TYPE_CHECKING:
    from collections.abc import Callable


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


# WebUI 乐观锁写入的进程内互斥：版本校验与写入提交必须在同一临界区内，
# 否则两个持有同一旧版本的请求会先后通过校验并互相覆盖。Bot 为单进程，
# 管理端写入频率很低，一把全局锁足够且不会死锁（临界区内不再嵌套获取）。
optimistic_write_lock = asyncio.Lock()


def check_version(
    row: Any,
    supplied: str | None,
    *,
    version_of: Callable[[Any], str | None] | None = None,
) -> None:
    # version_of 应为管理员可编辑字段的内容摘要（见 service.*_version）；
    # 缺省时退回 updated_at 序列化值，仅为兼容旧调用方。
    if row is None:
        current = None
    elif version_of is not None:
        current = version_of(row)
    else:
        current = version(row.updated_at)
    if current != supplied:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "配置已被其他操作修改，请刷新后重试"
        )
