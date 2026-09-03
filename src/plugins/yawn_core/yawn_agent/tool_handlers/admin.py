# ruff: noqa: TID252, TRY003
from __future__ import annotations

from typing import Any

from ..capabilities import target_can_be_muted
from ..context import now_beijing
from ..log import dbg
from ..tool_execution import ToolExecutionContext, ToolHandlerResult
from ..tool_support import (
    _require_current_group_message_api,
    _require_group_member_api,
)

FAMILY = "admin"
NAMES = frozenset(
    [
        "create_group_announcement",
        "set_essence_message",
        "remove_essence_message",
        "delete_group_notice",
        "set_group_card",
        "set_special_title",
        "set_group_name",
        "mute_member",
        "kick_member",
        "set_whole_group_mute",
        "set_group_admin",
    ]
)


async def handle(  # noqa: C901, PLR0912, PLR0915
    name: str, args: dict[str, Any], context: ToolExecutionContext
) -> ToolHandlerResult:
    bot = context.bot
    group_id = context.group_id
    capabilities = context.capabilities
    now_beijing()
    if name == "create_group_announcement":
        # NapCat/go-cqhttp 使用 `_send_group_notice`；只有适配器明确只暴露
        # `send_group_notice` 时才使用别名。不要在失败后自动重试另一个
        # action，避免公告其实已创建但回执失败时产生重复公告。
        action = (
            "_send_group_notice"
            if capabilities.has("_send_group_notice")
            else "send_group_notice"
        )
        result = await bot.call_api(
            action, group_id=group_id, content=str(args["content"])[:1000]
        )
    elif name == "set_essence_message":
        target_message_id = int(args["message_id"])
        await _require_current_group_message_api(bot, group_id, target_message_id)
        result = await bot.call_api("set_essence_msg", message_id=target_message_id)
    elif name == "remove_essence_message":
        target_message_id = int(args["message_id"])
        await _require_current_group_message_api(bot, group_id, target_message_id)
        result = await bot.call_api("delete_essence_msg", message_id=target_message_id)
    elif name == "delete_group_notice":
        notice_id = str(args.get("notice_id") or "").strip()
        if not notice_id:
            raise ValueError("notice_id 不能为空")
        result = await bot.call_api(
            "_del_group_notice",
            group_id=group_id,
            notice_id=notice_id,
        )
    elif name == "set_group_card":
        user_id = int(args["user_id"])
        await _require_group_member_api(bot, group_id, user_id)
        result = await bot.call_api(
            "set_group_card",
            group_id=group_id,
            user_id=user_id,
            card=str(args.get("card") or "")[:80],
        )
    elif name == "set_special_title":
        if capabilities.role != "owner":
            raise PermissionError("设置专属头衔需要机器人是群主")
        user_id = int(args["user_id"])
        await _require_group_member_api(bot, group_id, user_id)
        result = await bot.call_api(
            "set_group_special_title",
            group_id=group_id,
            user_id=user_id,
            special_title=str(args.get("special_title") or "")[:80],
        )
    elif name == "set_group_name":
        group_name = str(args.get("group_name") or "").strip()[:100]
        if not group_name:
            raise ValueError("group_name 不能为空")
        result = await bot.call_api(
            "set_group_name", group_id=group_id, group_name=group_name
        )
    elif name == "mute_member":
        user_id = int(args["user_id"])
        if not await target_can_be_muted(bot, group_id, user_id, capabilities.role):
            dbg(f"群 {group_id} mute_member 拒绝: 机器人无权禁言成员 {user_id}")
            raise PermissionError("机器人无权禁言该成员")
        dbg(f"群 {group_id} mute_member: 禁言成员 {user_id} {args.get('duration')}s")
        result = await bot.call_api(
            "set_group_ban",
            group_id=group_id,
            user_id=user_id,
            duration=max(1, min(int(args["duration"]), 2592000)),
        )
    elif name == "kick_member":
        user_id = int(args["user_id"])
        if user_id == int(getattr(bot, "self_id", 0) or 0):
            raise PermissionError("机器人不能把自己移出群聊")
        if not await target_can_be_muted(bot, group_id, user_id, capabilities.role):
            raise PermissionError("机器人无权移出该成员")
        result = await bot.call_api(
            "set_group_kick",
            group_id=group_id,
            user_id=user_id,
            reject_add_request=bool(args.get("reject_add_request", False)),
        )
    elif name == "set_whole_group_mute":
        result = await bot.call_api(
            "set_group_whole_ban",
            group_id=group_id,
            enable=bool(args["enable"]),
        )
    elif name == "set_group_admin":
        if capabilities.role != "owner":
            raise PermissionError("设置群管理员需要机器人是群主")
        user_id = int(args["user_id"])
        target = await _require_group_member_api(bot, group_id, user_id)
        if str(target.get("role") or "member") == "owner":
            raise PermissionError("不能修改群主的管理员状态")
        result = await bot.call_api(
            "set_group_admin",
            group_id=group_id,
            user_id=user_id,
            enable=bool(args["enable"]),
        )
    else:
        raise ValueError(f"{FAMILY} handler 不支持工具: {name}")
    return ToolHandlerResult(result)
