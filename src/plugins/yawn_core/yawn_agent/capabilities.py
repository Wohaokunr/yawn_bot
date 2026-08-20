# ruff: noqa: E501,F401,I001,TID252,PLR0912,PLR0913,PLR0917,C901,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100
"""OneBot action 能力和机器人权限探测。"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any


@dataclass(frozen=True, slots=True)
class BotGroupCapabilities:
    role: str
    can_manage: bool
    actions: frozenset[str]

    def has(self, action: str) -> bool:
        return action in self.actions


_CAPABILITY_TTL = 60.0
_capability_cache: dict[tuple[int, int], tuple[float, BotGroupCapabilities]] = {}


async def probe_group_capabilities(
    bot: Any, group_id: int, *, refresh: bool = False
) -> BotGroupCapabilities:
    """读取机器人自身群角色；API 不可用时按普通成员降级。"""

    key = (int(getattr(bot, "self_id", 0) or 0), int(group_id))
    cached = _capability_cache.get(key)
    now = time.monotonic()
    if not refresh and cached is not None and now - cached[0] < _CAPABILITY_TTL:
        return cached[1]
    role = "member"
    self_id = int(getattr(bot, "self_id", 0) or 0)
    try:
        info = await bot.call_api("get_group_member_info", group_id=group_id, user_id=self_id)
        if isinstance(info, dict):
            role = str(info.get("role") or "member")
    except Exception:  # noqa: BLE001
        pass
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
    _capability_cache[key] = (now, result)
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
        return False
    role = str(info.get("role") or "member") if isinstance(info, dict) else "member"
    return role in {"owner", "admin"}


async def target_can_be_muted(bot: Any, group_id: int, user_id: int, bot_role: str) -> bool:
    try:
        info = await bot.call_api("get_group_member_info", group_id=group_id, user_id=user_id)
    except Exception:  # noqa: BLE001
        return False
    target_role = str(info.get("role") or "member") if isinstance(info, dict) else "member"
    if target_role == "owner":
        return False
    if target_role == "admin" and bot_role != "owner":
        return False
    return bot_role in {"owner", "admin"}


__all__ = [
    "BotGroupCapabilities",
    "probe_group_capabilities",
    "reset_capability_cache",
    "target_can_be_muted",
    "user_can_manage_group",
]

