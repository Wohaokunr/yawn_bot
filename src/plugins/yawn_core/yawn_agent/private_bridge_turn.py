# ruff: noqa: TID252, E501, PLR2004, RUF001
"""群里转私聊的开场回合：群里答应了“私聊你”，私聊这边主动开口接着办。

走完整的私聊主回合（``handle_private_turn``，同一份开头和工具表，命中前缀缓存），
只是没有他刚发的消息：把那件事作为一条 user 通知（``private.handoff_notice``）接在
历史后面，本轮资料里放 ``from_groups``，再加一段开场说明（``private.handoff_turn``）。
主回合里的后台任务、虚拟电脑、个人助理、发文件都能用。拿回合锁，和他自己发来的
消息排队，不会两边同时说话。
"""

from __future__ import annotations

import json
from typing import Any

from . import agent_bridge as bridge
from .context import now_beijing
from .log import dbg, dbg_exc


def handoff_payload(handoff: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": handoff.get("id"),
        "group": handoff.get("group_name"),
        "topic": handoff.get("topic"),
        "his_words_in_group": handoff.get("asked") or None,
        "why_private": bridge.REASON_LABELS.get(str(handoff.get("reason")), handoff.get("reason")),
    }


def fallback_opening(handoff: dict[str, Any]) -> str:
    topic = str(handoff.get("topic") or "")
    topic = topic if len(topic) <= 30 else topic[:30] + "…"
    return f"来啦～群里说的「{topic}」，我们在这儿接着说吧"


async def open_handoff(bot: Any, user_id: int, handoff: dict[str, Any]) -> bool:
    """私聊开口；成功送达返回 True。模型没给回复时发一句模板开场，保证他收到。"""

    from nonebot_plugin_orm import get_session

    from ..private_chat_bridge import PrivateTurnRequest, user_turn_lock
    from .private_agent import (  # pyright: ignore[reportPrivateUsage]
        _recent_history,
        handle_private_turn,
    )
    from .private_prompt import PRIVATE_HANDOFF_NOTICE
    from .private_scheduler import record_outgoing
    from .prompt_sections import apply_section

    handoff_id = str(handoff.get("id"))
    async with user_turn_lock(user_id):
        current = next((row for row in bridge.handoffs(user_id) if str(row.get("id")) == handoff_id), handoff)
        if current.get("status") not in {"opening", None}:
            dbg(f"私聊用户 {user_id} 转来的事 {handoff_id} 已经在处理，不重复开口")
            return True
        notice = apply_section("private.handoff_notice", PRIVATE_HANDOFF_NOTICE) + "\n" + json.dumps(
            handoff_payload(current), ensure_ascii=False
        )
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
                    extra={"handoff": current},
                )
                text = await handle_private_turn(request)
        except Exception:  # noqa: BLE001
            dbg_exc(f"私聊用户 {user_id} 转私聊开场回合出错，改用模板")
            text = None
        if text is None:
            text = fallback_opening(current)
            try:
                await bot.send_private_msg(user_id=int(user_id), message=text)
            except Exception:  # noqa: BLE001
                dbg_exc(f"私聊用户 {user_id} 转私聊开场发不出去")
                bridge.update_handoff(user_id, handoff_id, status="failed")
                return False
        if text:
            try:
                await record_outgoing(int(user_id), text, now_beijing())
            except Exception:  # noqa: BLE001
                dbg_exc(f"私聊用户 {user_id} 转私聊开场入库失败(忽略)")
        bridge.update_handoff(user_id, handoff_id, status="opened", opened_at=bridge.fmt(now_beijing()))
        dbg(f"私聊用户 {user_id} 转私聊开场完成: {handoff_id}")
        return True


__all__ = ["fallback_opening", "handoff_payload", "open_handoff"]
