# ruff: noqa: TID252
"""个人账号会话的权限范围：本人 QQ + 本人当群主/管理员的群。

所有判断都在服务端做，前端传什么群号都要过这里：

- 候选群来自数据库 ``UserGroup.role``（每条消息都会同步），只有 owner/admin 进候选。
- Bot 在线时再用 OneBot 实时确认（``yawn_agent.capabilities.user_can_manage_group``），
  结果缓存一小会儿；被撤掉管理员后最多 ``_POSITIVE_TTL`` 秒就看不到了。
- Bot 不在线时：只读接口按数据库角色放行，写接口一律 503（确认不了身份就不改）。
- 超级管理员（``SUPERUSER_FULL_ACCESS`` 开着）：Bot 认识的所有群都在范围内，不需要
  在群里当群主/管理员，也不需要 Bot 在线确认（身份来自配置）；角色记作 ``superuser``，
  在群里本来就是群主时仍记群主。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status
from nonebot import get_bots, logger
from sqlalchemy import select

from ..data_models.bot_group import BotGroup
from ..data_models.user_group import UserGroup
from ..superuser import SUPERUSER_ROLE, effective_role, superuser_full_access

MANAGER_ROLES = ("owner", "admin")
_POSITIVE_TTL = 120.0
_NEGATIVE_TTL = 15.0
_live_cache: dict[tuple[int, int], tuple[float, bool]] = {}


@dataclass(slots=True)
class SuperuserMembership:
    """超级管理员在他不是群管（或不在群里）的群上的“成员关系”。

    字段对齐 ``UserGroup``，调用方按同样的方式读 ``role``/``group_id``。
    """

    group_id: int
    user_id: int
    role: str = SUPERUSER_ROLE
    group_nickname: str | None = None
    first_seen_at: Any = None
    last_seen_at: Any = None


async def _superuser_rows(db: Any, user_id: int) -> list[tuple[Any, BotGroup]]:
    rows = (
        await db.execute(
            select(BotGroup, UserGroup)
            .outerjoin(
                UserGroup,
                (UserGroup.group_id == BotGroup.group_id)
                & (UserGroup.user_id == user_id),
            )
            .order_by(BotGroup.last_active_at.desc(), BotGroup.group_id)
        )
    ).all()
    result: list[tuple[Any, BotGroup]] = []
    for group, membership in rows:
        if membership is not None and str(membership.role) == "owner":
            result.append((membership, group))
            continue
        result.append(
            (
                SuperuserMembership(
                    group_id=int(group.group_id),
                    user_id=int(user_id),
                    group_nickname=getattr(membership, "group_nickname", None),
                    first_seen_at=getattr(membership, "first_seen_at", None),
                    last_seen_at=getattr(membership, "last_seen_at", None),
                ),
                group,
            )
        )
    return result


async def managed_group_rows(db: Any, user_id: int) -> list[tuple[Any, BotGroup]]:
    """数据库里这位用户是群主/管理员的群（候选，还没实时确认）。

    超级管理员（全权开着）返回 Bot 认识的所有群。
    """

    if superuser_full_access(user_id):
        return await _superuser_rows(db, int(user_id))
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
    """实时确认是不是群主/管理员；Bot 不在线或 Agent 子插件没加载时返回 None。

    超级管理员直接返回 True（身份来自配置，不用问 QQ，Bot 不在线也能确认）。
    """

    if superuser_full_access(user_id):
        return True
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
    if group is not None and superuser_full_access(user_id):
        # 超级管理员：Bot 认识的群都能管，写入也不用等 Bot 在线确认
        return {
            "role": effective_role(role, user_id),
            "verified": True,
            "superuser": True,
        }
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
    "SuperuserMembership",
    "db_role",
    "ensure_group_manager",
    "forget_live_checks",
    "live_manager_check",
    "managed_group_rows",
]
