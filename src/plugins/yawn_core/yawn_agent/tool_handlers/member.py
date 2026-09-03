# ruff: noqa: TID252, TRY003
from __future__ import annotations

from typing import Any

from ..context import now_beijing
from ..tool_execution import ToolExecutionContext, ToolHandlerResult
from ..tool_support import (
    DEFAULT_MEMBER_TOOL_LIMIT,
    MAX_MEMBER_TOOL_LIMIT,
    _compact_group_member,
    _tool_result_limit,
)

FAMILY = "member"
NAMES = frozenset(["get_group_member", "list_group_members"])


async def handle(
    name: str, args: dict[str, Any], context: ToolExecutionContext
) -> ToolHandlerResult:
    bot = context.bot
    group_id = context.group_id
    now_beijing()
    if name == "get_group_member":
        result = _compact_group_member(
            await bot.call_api(
                "get_group_member_info",
                group_id=group_id,
                user_id=int(args["user_id"]),
            )
        )
    elif name == "list_group_members":
        members = await bot.call_api("get_group_member_list", group_id=group_id)
        if not isinstance(members, list):
            raise ValueError("群成员列表响应格式错误")
        limit = _tool_result_limit(
            args, default=DEFAULT_MEMBER_TOOL_LIMIT, maximum=MAX_MEMBER_TOOL_LIMIT
        )
        compact_members = [
            _compact_group_member(member)
            for member in members
            if isinstance(member, dict)
        ]
        result = {
            "items": compact_members[:limit],
            "total": len(members),
            "truncated": len(members) > limit,
        }
    else:
        raise ValueError(f"{FAMILY} handler 不支持工具: {name}")
    return ToolHandlerResult(result)
