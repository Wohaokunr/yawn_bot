# ruff: noqa: TID252, TRY003, TRY004, PLR2004, C901, PLR0912
from __future__ import annotations

from typing import Any

from ..tool_args import ToolArgumentError
from ..tool_execution import ToolExecutionContext, ToolHandlerResult
from ..tool_support import (
    DEFAULT_MEMBER_TOOL_LIMIT,
    MAX_MEMBER_TOOL_LIMIT,
    _compact_group_member,
    _compact_group_member_detail,
    _tool_result_limit,
)

FAMILY = "member"
NAMES = frozenset(
    ["get_group_member", "list_group_members", "find_group_member", "list_group_admins"]
)
_ROLE_ORDER = {"owner": 0, "admin": 1, "member": 2}


async def _member_rows(bot: Any, group_id: int) -> list[dict[str, Any]]:
    members = await bot.call_api("get_group_member_list", group_id=group_id)
    if not isinstance(members, list):
        raise ValueError("群成员列表响应格式错误")
    return [member for member in members if isinstance(member, dict)]


def member_match_score(query: str, member: dict[str, Any]) -> int:
    """昵称/群名片匹配分：完全一致 > 前缀 > 包含 > 字符重叠；0 表示不匹配。"""

    needle = query.casefold().strip().lstrip("@")
    if not needle:
        return 0
    best = 0
    for raw_name in (member.get("card"), member.get("nickname")):
        name = str(raw_name or "").casefold().strip()
        if not name:
            continue
        if name == needle:
            best = max(best, 100)
        elif name.startswith(needle):
            best = max(best, 80)
        elif needle in name:
            best = max(best, 60)
        elif len(needle) >= 2:
            overlap = len(set(needle) & set(name)) / len(set(needle))
            if overlap >= 0.75:
                best = max(best, int(40 * overlap))
    if str(member.get("user_id") or "") == needle:
        best = max(best, 100)
    return best


async def handle(
    name: str, args: dict[str, Any], context: ToolExecutionContext
) -> ToolHandlerResult:
    bot = context.bot
    group_id = context.group_id
    if name == "get_group_member":
        result: Any = _compact_group_member_detail(
            await bot.call_api(
                "get_group_member_info",
                group_id=group_id,
                user_id=int(args["user_id"]),
            )
        )
    elif name == "list_group_members":
        members = await _member_rows(bot, group_id)
        limit = _tool_result_limit(
            args, default=DEFAULT_MEMBER_TOOL_LIMIT, maximum=MAX_MEMBER_TOOL_LIMIT
        )
        role = str(args.get("role") or "").strip()
        if role:
            members = [
                member
                for member in members
                if str(member.get("role") or "member") == role
            ]
        compact_members = [_compact_group_member(member) for member in members]
        result = {
            "items": compact_members[:limit],
            "total": len(members),
            "truncated": len(members) > limit,
        }
    elif name == "find_group_member":
        query = str(args.get("query") or "").strip()
        if not query:
            raise ToolArgumentError("query 不能为空")
        limit = _tool_result_limit(args, default=5, maximum=10)
        self_id = int(getattr(bot, "self_id", 0) or 0)
        scored: list[tuple[int, dict[str, Any]]] = []
        for member in await _member_rows(bot, group_id):
            try:
                if int(member.get("user_id") or 0) == self_id:
                    continue
            except (TypeError, ValueError):
                continue
            score = member_match_score(query, member)
            if score > 0:
                scored.append((score, member))
        scored.sort(key=lambda item: (-item[0], int(item[1].get("user_id") or 0)))
        items = []
        for score, member in scored[:limit]:
            compact = _compact_group_member(member)
            nickname = str(member.get("nickname") or "").strip()
            if nickname and nickname != compact.get("name"):
                compact["nickname"] = nickname[:64]
            compact["exact"] = score >= 100
            items.append(compact)
        result = {
            "items": items,
            "count": len(items),
            "ambiguous": len(items) > 1 and not (items and items[0]["exact"]),
        }
    elif name == "list_group_admins":
        admins = [
            member
            for member in await _member_rows(bot, group_id)
            if str(member.get("role") or "member") in {"owner", "admin"}
        ]
        admins.sort(key=lambda item: _ROLE_ORDER.get(str(item.get("role")), 9))
        result = {
            "items": [_compact_group_member(member) for member in admins],
            "count": len(admins),
        }
    else:
        raise ValueError(f"{FAMILY} handler 不支持工具: {name}")
    return ToolHandlerResult(result)
