# ruff: noqa: TID252, TRY003
from __future__ import annotations

from typing import Any

from ..context import now_beijing
from ..tool_execution import ToolExecutionContext, ToolHandlerResult
from ..tool_support import (
    _compact_essence,
    _compact_group_info,
    _compact_group_member,
    _compact_notice,
    _compact_onebot_message,
    _payload_list,
    _require_known_message,
    _tool_result_limit,
)

FAMILY = "history"
_MAX_RECENT_MESSAGE_COUNT = 30
NAMES = frozenset(
    [
        "get_group_info",
        "get_message",
        "get_recent_group_messages",
        "list_group_notices",
        "list_essence_messages",
        "list_muted_members",
        "get_group_honor",
    ]
)


async def handle(  # noqa: C901, PLR0912, PLR0915
    name: str, args: dict[str, Any], context: ToolExecutionContext
) -> ToolHandlerResult:
    bot = context.bot
    group_id = context.group_id
    session = context.session
    now_beijing()
    if name == "get_group_info":
        result = _compact_group_info(
            await bot.call_api("get_group_info", group_id=group_id)
        )
    elif name == "get_message":
        target_message_id = int(args["message_id"])
        await _require_known_message(session, group_id, target_message_id)
        raw_message = await bot.call_api("get_msg", message_id=target_message_id)
        if (
            isinstance(raw_message, dict)
            and raw_message.get("group_id") is not None
            and int(raw_message["group_id"]) != int(group_id)
        ):
            raise PermissionError("消息不属于当前群")
        result = _compact_onebot_message(raw_message)
    elif name == "get_recent_group_messages":
        try:
            count = int(args.get("count") or 10)
        except (TypeError, ValueError) as exc:
            raise ValueError("count 必须是整数") from exc
        if count < 1 or count > _MAX_RECENT_MESSAGE_COUNT:
            raise ValueError("count 必须在 1~30 之间")
        raw_history = await bot.call_api(
            "get_group_msg_history", group_id=group_id, count=count
        )
        messages = _payload_list(raw_history, "messages", "message_list", "items")
        if (
            not messages
            and raw_history not in ([], {"messages": []})
            and not isinstance(raw_history, (list, dict))
        ):
            raise ValueError("群历史消息响应格式错误")
        compact_messages = [
            _compact_onebot_message(item)
            for item in messages[-count:]
            if isinstance(item, dict)
        ]
        result = {"items": compact_messages, "count": len(compact_messages)}
    elif name == "list_group_notices":
        raw_notices = await bot.call_api("_get_group_notice", group_id=group_id)
        notices = _payload_list(raw_notices, "notices", "items")
        if (
            isinstance(raw_notices, dict)
            and not notices
            and any(key in raw_notices for key in ("notice_id", "content", "message"))
        ):
            notices = [raw_notices]
        result = [
            _compact_notice(item) for item in notices[:20] if isinstance(item, dict)
        ]
    elif name == "list_essence_messages":
        raw_essence = await bot.call_api("get_essence_msg_list", group_id=group_id)
        essence_items = _payload_list(raw_essence, "items", "messages", "list")
        result = [
            _compact_essence(item)
            for item in essence_items[:30]
            if isinstance(item, dict)
        ]
    elif name == "list_muted_members":
        limit = _tool_result_limit(args, default=30, maximum=50)
        raw_muted = await bot.call_api("get_group_shut_list", group_id=group_id)
        muted_items = _payload_list(raw_muted, "items", "members", "list")
        compact_muted: list[dict[str, Any]] = []
        for item in muted_items[:limit]:
            if not isinstance(item, dict):
                continue
            member = _compact_group_member(item)
            if item.get("shut_up_timestamp") is not None:
                member["shut_up_timestamp"] = item.get("shut_up_timestamp")
            compact_muted.append(member)
        result = {"items": compact_muted, "count": len(compact_muted)}
    elif name == "get_group_honor":
        honor_type = str(args.get("type") or "all")
        raw_honor = await bot.call_api(
            "get_group_honor_info", group_id=group_id, type=honor_type
        )
        if not isinstance(raw_honor, dict):
            raise ValueError("群荣誉响应格式错误")
        honor_result: dict[str, Any] = {"group_id": raw_honor.get("group_id")}
        for key in (
            "current_talkative",
            "talkative_list",
            "performer_list",
            "legend_list",
            "strong_newbie_list",
            "emotion_list",
        ):
            value = raw_honor.get(key)
            if isinstance(value, dict):
                honor_result[key] = _compact_group_member(value)
            elif isinstance(value, list):
                honor_result[key] = [
                    _compact_group_member(item)
                    for item in value[:20]
                    if isinstance(item, dict)
                ]
        result = {
            key: value
            for key, value in honor_result.items()
            if value not in (None, [], {})
        }
    else:
        raise ValueError(f"{FAMILY} handler 不支持工具: {name}")
    return ToolHandlerResult(result)
