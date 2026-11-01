# ruff: noqa: TID252
"""个人账号会话的权限范围：本人 QQ + 本人当群主/管理员的群。

所有判断都在服务端做，前端传什么群号都要过这里：

- 候选群来自数据库 ``UserGroup.role``（每条消息都会同步），只有 owner/admin 进候选。
- Bot 在线时再用 OneBot 实时确认（``yawn_agent.capabilities.user_can_manage_group``），
  结果缓存一小会儿；被撤掉管理员后最多 ``_POSITIVE_TTL`` 秒就看不到了。
- Bot 不在线时：只读接口按数据库角色放行，写接口一律 503（确认不了身份就不改）。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import HTTPException, status
from nonebot import get_bots, logger
from sqlalchemy import select

from ..data_models.bot_group import BotGroup
from ..data_models.user_group import UserGroup

MANAGER_ROLES = ("owner", "admin")
_POSITIVE_TTL = 120.0
_NEGATIVE_TTL = 15.0
_live_cache: dict[tuple[int, int], tuple[float, bool]] = {}


async def managed_group_rows(db: Any, user_id: int) -> list[tuple[UserGroup, BotGroup]]:
    """数据库里这位用户是群主/管理员的群（候选，还没实时确认）。"""

    rows = (
        await db.execute(
            select(UserGroup, BotGroup)
            .join(BotGroup, BotGroup.group_id == UserGroup.group_id)
            .where(
                UserGroup.user_id == user_id,
                UserGroup.role.in_(MANAGER_ROLES),
            )
            .order_by(BotGroup.last_active_at.desc(), BotGroup.group_id)
        )
    ).all()
    return [(membership, group) for membership, group in rows]


async def db_role(db: Any, group_id: int, user_id: int) -> str | None:
    membership = await db.get(UserGroup, (group_id, user_id))
    return str(membership.role) if membership is not None else None


def _bot() -> Any | None:
    try:
        bots = get_bots()
    except (RuntimeError, ValueError):
        return None
    return next(iter(bots.values()), None)


async def live_manager_check(group_id: int, user_id: int) -> bool | None:
    """实时确认是不是群主/管理员；Bot 不在线或 Agent 子插件没加载时返回 None。"""

    key = (int(group_id), int(user_id))
    now = time.monotonic()
    cached = _live_cache.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]
    bot = _bot()
    if bot is None:
        return None
    try:
        from ..yawn_agent.capabilities import user_can_manage_group
    except Exception:  # noqa: BLE001
        logger.debug("WebUI 个人账号：Agent 子插件不可用，无法实时确认群管理身份")
        return None
    allowed = bool(await user_can_manage_group(bot, group_id, user_id))
    _live_cache[key] = (now + (_POSITIVE_TTL if allowed else _NEGATIVE_TTL), allowed)
    return allowed


def forget_live_checks(user_id: int | None = None) -> None:
    if user_id is None:
        _live_cache.clear()
        return
    for key in [key for key in _live_cache if key[1] == int(user_id)]:
        _live_cache.pop(key, None)


async def ensure_group_manager(
    db: Any, group_id: int, user_id: int, *, write: bool
) -> dict[str, Any]:
    """不是这个群的群主/管理员就 403；返回 ``{"role", "verified"}``。

    返回 404 和 403 用同一句话，不让人借此探测 Bot 在哪些群。
    """

    denied = HTTPException(status.HTTP_403_FORBIDDEN, "你不是这个群的群主或管理员")
    group = await db.get(BotGroup, group_id)
    role = await db_role(db, group_id, user_id) if group is not None else None
    if group is None or role not in MANAGER_ROLES:
        raise denied
    live = await live_manager_check(group_id, user_id)
    if live is False:
        raise denied
    if live is None and write:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Bot 暂时不在线，无法确认你的管理员身份，稍后再改",
        )
    return {"role": role, "verified": live is True}


def reset_for_tests() -> None:
    _live_cache.clear()


__all__ = [
    "MANAGER_ROLES",
    "db_role",
    "ensure_group_manager",
    "forget_live_checks",
    "live_manager_check",
    "managed_group_rows",
]
