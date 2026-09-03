# ruff: noqa: TID252, TRY003
from __future__ import annotations

from typing import Any

from ..context import now_beijing
from ..outbound import send_forward_message, send_outbound_message
from ..reactions import search_reactions
from ..tool_execution import ToolExecutionContext, ToolHandlerResult
from ..tool_support import (
    _require_known_message,
)

FAMILY = "message"
NAMES = frozenset(
    ["search_reactions", "react_to_message", "send_message", "send_forward"]
)


async def handle(
    name: str, args: dict[str, Any], context: ToolExecutionContext
) -> ToolHandlerResult:
    bot = context.bot
    group_id = context.group_id
    actor_user_id = context.actor_user_id
    session = context.session
    now_beijing()
    if name == "search_reactions":
        query = str(args.get("query") or "").strip()
        if not query:
            raise ValueError("query 不能为空")
        result = search_reactions(query, limit=int(args.get("limit") or 5))
    elif name == "react_to_message":
        target_message_id = int(args["message_id"])
        emoji_id = str(args.get("emoji_id") or "").strip()
        if not emoji_id.isdigit():
            raise ValueError("emoji_id 必须是数字字符串")
        await _require_known_message(session, group_id, target_message_id)
        await bot.call_api(
            "set_msg_emoji_like",
            message_id=target_message_id,
            emoji_id=emoji_id,
        )
        result = {
            "message_id": target_message_id,
            "emoji_id": emoji_id,
            "reacted": True,
        }
    elif name == "send_message":
        sent = await send_outbound_message(
            bot,
            group_id,
            args.get("segments"),
            session=session,
            actor_user_id=actor_user_id,
            source="tool",
        )
        result = {
            "sent": sent.sent,
            "message_id": sent.message_id,
            "segment_types": list(sent.segment_types),
            "message_type": sent.message_type,
            "outcome": sent.outcome,
            "delivery_state": sent.delivery_state,
            "degraded_from": sent.degraded_from,
            "text": sent.normalized_text[:500],
            "outbound": sent.storage_payload(),
        }
    elif name == "send_forward":
        sent = await send_forward_message(
            bot,
            group_id,
            args.get("nodes"),
            session=session,
            actor_user_id=actor_user_id,
            source="tool",
        )
        result = {
            "sent": sent.sent,
            "message_id": sent.message_id,
            "segment_types": list(sent.segment_types),
            "message_type": sent.message_type,
            "outcome": sent.outcome,
            "delivery_state": sent.delivery_state,
            "degraded_from": sent.degraded_from,
            "text": sent.normalized_text[:500],
            "outbound": sent.storage_payload(),
        }
    else:
        raise ValueError(f"{FAMILY} handler 不支持工具: {name}")
    ends_turn = bool(
        name in {"send_message", "send_forward"}
        and isinstance(result, dict)
        and result.get("delivery_state")
        in {"confirmed_success", "degraded_success", "unknown"}
    )
    return ToolHandlerResult(result, ends_turn=ends_turn)
