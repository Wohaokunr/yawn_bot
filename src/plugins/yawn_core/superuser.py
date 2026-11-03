"""超级管理员（NoneBot ``SUPERUSERS``）的统一判定。

超级管理员是 Bot 的维护者：不需要在某个群当群主/管理员，也能用 Bot 的全部
群管能力（群命令、私聊代办群管理、网页管群 Agent/人设、私聊转告到群……）。
各处“是不是群主/管理员”的判断都应该走这里，而不是各自读
``get_driver().config.superusers``：

- ``is_superuser``：只看配置，不看开关（例如“保护超级管理员不被处置”）。
- ``superuser_full_access``：是超级管理员 **且** ``SUPERUSER_FULL_ACCESS`` 开着，
  用来放行原本要求群主/管理员身份的地方。
- ``effective_role``：把群里的真实角色换成判权限用的角色；超级管理员不是群主时
  记作 ``"superuser"``（权限等同群主，但审计里看得出是谁）。

Bot 自己的身份不在这里放宽：禁言、踢人、发公告等平台操作仍要求 Bot 是群主/
管理员，做不了时用 ``bot_role_hint`` 给出清晰提示。
"""

from __future__ import annotations

from typing import Any

SUPERUSER_ROLE = "superuser"
MANAGER_ROLES = frozenset({"owner", "admin"})
#: 判权限用的“管理身份”：群主、管理员、以及开了全权的超级管理员
EFFECTIVE_MANAGER_ROLES = frozenset({"owner", "admin", SUPERUSER_ROLE})
#: 权限等同群主的身份
OWNER_LEVEL_ROLES = frozenset({"owner", SUPERUSER_ROLE})
ROLE_LABELS = {
    "owner": "群主",
    "admin": "管理员",
    "member": "成员",
    SUPERUSER_ROLE: "超级管理员",
}


def superuser_ids() -> frozenset[str]:
    """运行时读取 SUPERUSERS（不在导入时快照，改配置重启后即生效）。"""

    try:
        from nonebot import get_driver

        return frozenset(str(item) for item in get_driver().config.superusers)
    except Exception:  # noqa: BLE001 - 驱动未初始化（脚本/部分测试）时按没有处理
        return frozenset()


def superuser_numeric_ids() -> tuple[int, ...]:
    return tuple(sorted(int(item) for item in superuser_ids() if item.isdigit()))


def is_superuser(user_id: Any) -> bool:
    if user_id is None:
        return False
    return str(user_id).strip() in superuser_ids()


def _flag(name: str, *, default: bool = True) -> bool:
    try:
        from .private_chat_bridge import private_agent_config

        return bool(getattr(private_agent_config, name, default))
    except Exception:  # noqa: BLE001
        return default


def full_access_enabled() -> bool:
    """``SUPERUSER_FULL_ACCESS``：超级管理员免群管身份用全部能力（默认开）。"""

    return _flag("superuser_full_access")


def superuser_full_access(user_id: Any) -> bool:
    """这个人是超级管理员，并且全权开关开着。"""

    return full_access_enabled() and is_superuser(user_id)


def skips_daily_limits(user_id: Any) -> bool:
    """``SUPERUSER_SKIP_DAILY_LIMITS``：超级管理员不计代办/转告的每日额度。"""

    return superuser_full_access(user_id) and _flag("superuser_skip_daily_limits")


def bypasses_group_policy(user_id: Any) -> bool:
    """``SUPERUSER_BYPASS_GROUP_POLICY``：不受群主设的代办/转告策略限制。"""

    return superuser_full_access(user_id) and _flag("superuser_bypass_group_policy")


def effective_role(role: Any, user_id: Any) -> str:
    """群里真实角色 → 判权限用的角色。

    群主仍是群主；其他情况下（含不在群里）超级管理员记作 superuser。
    """

    normalized = str(role or "member")
    if normalized == "owner":
        return "owner"
    if superuser_full_access(user_id):
        return SUPERUSER_ROLE
    return normalized


def is_manager_role(role: Any) -> bool:
    return str(role or "") in EFFECTIVE_MANAGER_ROLES


def is_owner_level(role: Any) -> bool:
    return str(role or "") in OWNER_LEVEL_ROLES


def role_label(role: Any) -> str:
    return ROLE_LABELS.get(str(role or "member"), str(role or "成员"))


def event_can_manage_group(event: Any) -> bool:
    """群消息事件的发送者能不能用群管命令：群主/管理员，或开了全权的超级管理员。"""

    sender = getattr(event, "sender", None)
    role = str(getattr(sender, "role", None) or "member")
    if role in MANAGER_ROLES:
        return True
    try:
        user_id = event.get_user_id()
    except Exception:  # noqa: BLE001
        user_id = getattr(event, "user_id", None)
    return superuser_full_access(user_id)


def bot_role_hint(bot_role: Any, *, need_owner: bool = False) -> str | None:
    """Bot 自身身份不够时给超级管理员/管理员看的提示；够了返回 None。"""

    role = str(bot_role or "member")
    if need_owner and role != "owner":
        return (
            "这件事需要 Bot 是群主：超级管理员身份只放宽你自己的权限，"
            "QQ 平台仍按 Bot 的身份执行"
        )
    if role not in MANAGER_ROLES:
        return (
            "Bot 在这个群不是管理员：先在 QQ 里把 Bot 设为管理员"
            "（超级管理员身份替代不了 Bot 自己的群权限）"
        )
    return None


__all__ = [
    "EFFECTIVE_MANAGER_ROLES",
    "MANAGER_ROLES",
    "OWNER_LEVEL_ROLES",
    "ROLE_LABELS",
    "SUPERUSER_ROLE",
    "bot_role_hint",
    "bypasses_group_policy",
    "effective_role",
    "event_can_manage_group",
    "full_access_enabled",
    "is_manager_role",
    "is_owner_level",
    "is_superuser",
    "role_label",
    "skips_daily_limits",
    "superuser_full_access",
    "superuser_ids",
    "superuser_numeric_ids",
]
