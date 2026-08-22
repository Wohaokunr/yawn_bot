# ruff: noqa: E501,F401,I001,TID252,PLR0912,PLR0913,PLR0917,C901,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100
"""OneBot action 能力和机器人权限探测。"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any

from .log import dbg, dbg_exc


@dataclass(frozen=True, slots=True)
class BotGroupCapabilities:
    role: str
    can_manage: bool
    actions: frozenset[str]

    def has(self, action: str) -> bool:
        return action in self.actions


_CAPABILITY_TTL = 60.0
# 探测失败只是瞬时抖动的概率很高，降级结果只短暂缓存，
# 避免一次 API 失败把管理工具锁死整整一分钟。
_DEGRADED_TTL = 5.0
_MAX_CACHE_ENTRIES = 256
_capability_cache: dict[tuple[int, int], tuple[float, BotGroupCapabilities, float]] = {}


async def probe_group_capabilities(
    bot: Any, group_id: int, *, refresh: bool = False
) -> BotGroupCapabilities:
    """读取机器人自身群角色；API 不可用时按普通成员降级。"""

    key = (int(getattr(bot, "self_id", 0) or 0), int(group_id))
    cached = _capability_cache.get(key)
    now = time.monotonic()
    if cached is not None and not refresh and now - cached[0] < cached[2]:
        dbg(f"群 {group_id} 能力探测命中缓存: role={cached[1].role!r} key={key}")
        return cached[1]
    dbg(f"群 {group_id} 能力探测缓存未命中,发起 API 探测(refresh={refresh})")
    role = "member"
    degraded = False
    self_id = int(getattr(bot, "self_id", 0) or 0)
    try:
        info = await bot.call_api(
            "get_group_member_info", group_id=group_id, user_id=self_id
        )
        if isinstance(info, dict):
            role = str(info.get("role") or "member")
    except Exception:  # noqa: BLE001
        degraded = True
        dbg_exc(f"群 {group_id} 能力探测失败,按普通成员降级(短 TTL {_DEGRADED_TTL}s)")
    actions = {
        "send_group_msg",
        "get_group_info",
        "get_group_member_info",
        "get_group_member_list",
        "get_msg",
        "get_forward_msg",
        "send_group_forward_msg",
        # OneBot adapters may allow file delivery for ordinary members; the
        # executor still validates path/domain/size before sending.
        "upload_group_file",
    }
    if role in {"owner", "admin"}:
        actions.update({"set_group_ban", "send_group_notice", "_send_group_notice"})
    declared = getattr(bot, "supported_actions", None)
    if isinstance(declared, (set, frozenset, list, tuple)):
        actions.intersection_update(str(item) for item in declared)
    result = BotGroupCapabilities(role, role in {"owner", "admin"}, frozenset(actions))
    if len(_capability_cache) >= _MAX_CACHE_ENTRIES:
        oldest = min(_capability_cache, key=lambda item: _capability_cache[item][0])
        _capability_cache.pop(oldest, None)
    _capability_cache[key] = (
        now,
        result,
        _DEGRADED_TTL if degraded else _CAPABILITY_TTL,
    )
    dbg(
        f"群 {group_id} 能力探测完成: role={result.role!r} can_manage={result.can_manage} "
        f"actions={sorted(result.actions)} degraded={degraded}"
    )
    return result


def reset_capability_cache() -> None:
    _capability_cache.clear()


async def user_can_manage_group(bot: Any, group_id: int, user_id: int) -> bool:
    """实时确认调用者仍是群主或管理员。"""

    try:
        info = await bot.call_api(
            "get_group_member_info", group_id=group_id, user_id=user_id
        )
    except Exception:  # noqa: BLE001
        dbg_exc(f"群 {group_id} 查询用户 {user_id} 管理权限失败,视为无权限")
        return False
    role = str(info.get("role") or "member") if isinstance(info, dict) else "member"
    allowed = role in {"owner", "admin"}
    dbg(f"群 {group_id} 用户 {user_id} 管理权限判定: role={role!r} → {allowed}")
    return allowed


async def target_can_be_muted(
    bot: Any, group_id: int, user_id: int, bot_role: str
) -> bool:
    try:
        info = await bot.call_api(
            "get_group_member_info", group_id=group_id, user_id=user_id
        )
    except Exception:  # noqa: BLE001
        dbg_exc(f"群 {group_id} 查询成员 {user_id} 禁言可行性失败,视为不可禁言")
        return False
    target_role = (
        str(info.get("role") or "member") if isinstance(info, dict) else "member"
    )
    if target_role == "owner":
        dbg(f"群 {group_id} 成员 {user_id} 是群主,不可禁言")
        return False
    if target_role == "admin" and bot_role != "owner":
        dbg(f"群 {group_id} 成员 {user_id} 是管理员且机器人非群主,不可禁言")
        return False
    allowed = bot_role in {"owner", "admin"}
    dbg(
        f"群 {group_id} 禁言判定: target_role={target_role!r} bot_role={bot_role!r} → {allowed}"
    )
    return allowed


__all__ = [
    "BotGroupCapabilities",
    "probe_group_capabilities",
    "reset_capability_cache",
    "target_can_be_muted",
    "user_can_manage_group",
]
