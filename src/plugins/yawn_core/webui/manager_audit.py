# ruff: noqa: TID252
"""群管理员在网页上改本群 Agent 时，记进群管代办的操作记录（群主能看到谁改了什么）。

运维管理员（role=admin）的修改不记在这里，管理台另有审计。
"""

from __future__ import annotations

from typing import Any

from nonebot import logger


def note_manager_change(
    session: Any, group_id: int, action: str, *, detail: str | None = None
) -> None:
    if getattr(session, "role", None) != "user" or session.user_id is None:
        return
    try:
        from ..yawn_agent import group_manage

        group_manage.audit(
            group_id,
            actor_id=int(session.user_id),
            actor_role=None,
            source="webui",
            action=action,
            result="success",
            detail=detail,
        )
    except Exception:  # noqa: BLE001
        logger.exception("记录群管理员网页修改失败（忽略）")


__all__ = ["note_manager_change"]
