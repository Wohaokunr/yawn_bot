# ruff: noqa: TID252, E501
"""到点替他去办：agent 模式的私聊提醒到点时，私聊的我主动开口去办那件事。

和群里定时任务的“唤醒回合”对应：走完整的私聊主回合（``handle_private_turn``，同一份
开头和工具表，命中前缀缓存），把任务作为一条 user 通知（``private.scheduled_notice``）
接在历史后面，再加一段说明（``private.scheduled_turn``）。拿回合锁，和他自己发来的
消息排队。

这一轮里 :data:`WAKE_BLOCKED_TOOLS` 一律拦下（执行时 ``allowed_tools`` 不放行）：
不许再设提醒或定时任务（防止自我繁殖），不许无人值守地替他去群里说话或管群。
模型没给回复时返回 ``fallback_needed``，调度器改发提醒原文，保证他收到。
"""

from __future__ import annotations

import json
from typing import Any

from .context import now_beijing
from .log import dbg, dbg_exc
from .private_schedule import Firing, fmt

WAKE_BLOCKED_TOOLS = frozenset({"reminders", "schedules", "group_manage", "group_bridge"})


def wake_payload(firing: Firing) -> dict[str, Any]:
    data: dict[str, Any] = {
        "task": firing.content,
        "planned_at": fmt(firing.planned),
        "set_from": {"private": "私聊里", "group": "群里", "webui": "网页上"}.get(firing.origin, "私聊里"),
    }
    if firing.title:
        data["title"] = firing.title
    late = int(firing.late.total_seconds() // 60)
    if late >= 5:  # noqa: PLR2004
        data["late_minutes"] = late
    if firing.last:
        data["last_time"] = True
    return data


async def run_scheduled_turn(bot: Any, user_id: int, firing: Firing) -> str:
    """跑一轮；送达返回 ``done``，模型没回复/出错返回 ``fallback_needed``。"""

    from nonebot_plugin_orm import get_session

    from ..private_chat_bridge import PrivateTurnRequest, user_turn_lock
    from .private_agent import (  # pyright: ignore[reportPrivateUsage]
        _recent_history,
        handle_private_turn,
    )
    from .private_prompt import PRIVATE_SCHEDULED_NOTICE
    from .private_scheduler import record_outgoing
    from .prompt_sections import apply_section

    payload = wake_payload(firing)
    notice = apply_section("private.scheduled_notice", PRIVATE_SCHEDULED_NOTICE) + "\n" + json.dumps(payload, ensure_ascii=False)
    async with user_turn_lock(user_id):
        text: str | None = None
        try:
            async with get_session() as session:
                history = await _recent_history(session, user_id)
                request = PrivateTurnRequest(
                    bot=bot,
                    event=None,
                    user_id=user_id,
                    user_input=notice,
                    raw_text="",
                    history=[*history, {"role": "user", "content": notice}],
                    session=session,
                    extra={"scheduled": payload},
                )
                text = await handle_private_turn(request)
        except Exception:  # noqa: BLE001
            dbg_exc(f"私聊用户 {user_id} 定时代办回合出错")
            return "fallback_needed"
        if not text:
            return "fallback_needed"
        try:
            await record_outgoing(int(user_id), text, now_beijing())
        except Exception:  # noqa: BLE001
            dbg_exc(f"私聊用户 {user_id} 定时代办入库失败(忽略)")
        dbg(f"私聊用户 {user_id} 定时代办完成: 提醒 {firing.item_id}")
        return "done"


def bot_waker(bot: Any) -> Any:
    async def wake(user_id: int, firing: Firing) -> str:
        return await run_scheduled_turn(bot, user_id, firing)

    return wake


__all__ = ["WAKE_BLOCKED_TOOLS", "bot_waker", "run_scheduled_turn", "wake_payload"]
