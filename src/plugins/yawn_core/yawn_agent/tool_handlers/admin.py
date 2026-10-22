# ruff: noqa: TID252, TRY003, E501, C901
from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from ...data_models.group_agent_message import GroupAgentMessage
from ..capabilities import (
    actor_can_act_on_target,
    actor_is_group_owner,
    target_can_be_muted,
)
from ..context import now_beijing
from ..log import dbg
from ..tool_args import ERROR_NOT_FOUND, ToolArgumentError, ToolError
from ..tool_execution import ToolExecutionContext, ToolHandlerResult
from ..tool_support import (
    _require_current_group_message_api,
    _require_group_member_api,
    clean_single_line,
)

# QQ 专属头衔上限 18 字节（UTF-8，约 6 个汉字）。
SPECIAL_TITLE_MAX_BYTES = 18
# 普通成员只能撤回自己 2 分钟内的消息；机器人自撤回沿用同一窗口。
BOT_RECALL_WINDOW = timedelta(minutes=2)

FAMILY = "admin"
NAMES = frozenset(
    [
        "set_essence_message",
        "remove_essence_message",
        "set_group_card",
        "set_special_title",
        "set_group_name",
        "mute_member",
        "unmute_member",
        "recall_message",
        "recall_bot_message",
        "kick_member",
        "set_whole_group_mute",
        "set_group_admin",
    ]
)


async def handle(  # noqa: PLR0912, PLR0915
    name: str, args: dict[str, Any], context: ToolExecutionContext
) -> ToolHandlerResult:
    bot = context.bot
    group_id = context.group_id
    capabilities = context.capabilities
    now_beijing()
    if name in {"recall_message", "recall_bot_message"}:
        return await _handle_recall(name, args, context)
    if name == "set_essence_message":
        target_message_id = int(args["message_id"])
        await _require_current_group_message_api(bot, group_id, target_message_id)
        result = await bot.call_api("set_essence_msg", message_id=target_message_id)
    elif name == "remove_essence_message":
        target_message_id = int(args["message_id"])
        await _require_current_group_message_api(bot, group_id, target_message_id)
        result = await bot.call_api("delete_essence_msg", message_id=target_message_id)
    elif name == "set_group_card":
        user_id = int(args["user_id"])
        await _require_group_member_api(bot, group_id, user_id)
        card = clean_single_line(args.get("card"), limit=80)
        await bot.call_api(
            "set_group_card", group_id=group_id, user_id=user_id, card=card
        )
        result = {"user_id": user_id, "card": card, "cleared": not card}
    elif name == "set_special_title":
        if capabilities.role != "owner":
            raise PermissionError("设置专属头衔需要机器人是群主")
        user_id = int(args["user_id"])
        special_title = clean_single_line(args.get("special_title"), limit=80)
        if len(special_title.encode("utf-8")) > SPECIAL_TITLE_MAX_BYTES:
            raise ToolArgumentError(
                "专属头衔超过 QQ 上限",
                hint=f"头衔最多 {SPECIAL_TITLE_MAX_BYTES} 字节，大约 6 个汉字或 18 个英文字母",
            )
        await _require_group_member_api(bot, group_id, user_id)
        await bot.call_api(
            "set_group_special_title",
            group_id=group_id,
            user_id=user_id,
            special_title=special_title,
        )
        result = {"user_id": user_id, "special_title": special_title}
    elif name == "set_group_name":
        group_name = clean_single_line(args.get("group_name"), limit=100)
        if not group_name:
            raise ToolArgumentError("group_name 不能为空")
        await bot.call_api("set_group_name", group_id=group_id, group_name=group_name)
        result = {"group_name": group_name}
    elif name in {"mute_member", "unmute_member"}:
        user_id = int(args["user_id"])
        if user_id == int(getattr(bot, "self_id", 0) or 0):
            raise PermissionError("机器人不能禁言或解禁自己")
        if not await target_can_be_muted(bot, group_id, user_id, capabilities.role):
            dbg(f"群 {group_id} mute_member 拒绝: 机器人无权禁言成员 {user_id}")
            raise PermissionError("机器人无权禁言该成员")
        if not await actor_can_act_on_target(
            bot, group_id, context.actor_user_id, user_id
        ):
            dbg(f"群 {group_id} mute_member 拒绝: 调用者无权处置成员 {user_id}")
            raise PermissionError("调用者无权禁言该成员：管理员和群主只能由群主处置")
        duration = (
            0 if name == "unmute_member" else max(1, min(int(args["duration"]), 2592000))
        )
        dbg(f"群 {group_id} {name}: 成员 {user_id} duration={duration}s")
        await bot.call_api(
            "set_group_ban", group_id=group_id, user_id=user_id, duration=duration
        )
        if duration:
            until = now_beijing() + timedelta(seconds=duration)
            result = {
                "user_id": user_id,
                "muted": True,
                "duration_seconds": duration,
                "until": until.strftime("%Y-%m-%d %H:%M"),
            }
        else:
            result = {"user_id": user_id, "muted": False}
    elif name == "kick_member":
        user_id = int(args["user_id"])
        if user_id == int(getattr(bot, "self_id", 0) or 0):
            raise PermissionError("机器人不能把自己移出群聊")
        if not await target_can_be_muted(bot, group_id, user_id, capabilities.role):
            raise PermissionError("机器人无权移出该成员")
        if not await actor_can_act_on_target(
            bot, group_id, context.actor_user_id, user_id
        ):
            dbg(f"群 {group_id} kick_member 拒绝: 调用者无权处置成员 {user_id}")
            raise PermissionError("调用者无权移出该成员：管理员和群主只能由群主处置")
        result = await bot.call_api(
            "set_group_kick",
            group_id=group_id,
            user_id=user_id,
            reject_add_request=args.get("reject_add_request") is True,
        )
    elif name == "set_whole_group_mute":
        # enable 已由 tool_args 严格解析为 bool；字符串 "false" 不会再变成 True。
        enable = args["enable"] is True
        await bot.call_api("set_group_whole_ban", group_id=group_id, enable=enable)
        result = {"whole_group_muted": enable}
    elif name == "set_group_admin":
        if capabilities.role != "owner":
            raise PermissionError("设置群管理员需要机器人是群主")
        if not await actor_is_group_owner(bot, group_id, context.actor_user_id):
            dbg(f"群 {group_id} set_group_admin 拒绝: 调用者不是群主")
            raise PermissionError("只有群主本人可以设置或取消群管理员")
        user_id = int(args["user_id"])
        target = await _require_group_member_api(bot, group_id, user_id)
        if str(target.get("role") or "member") == "owner":
            raise PermissionError("不能修改群主的管理员状态")
        enable = args["enable"] is True
        await bot.call_api(
            "set_group_admin", group_id=group_id, user_id=user_id, enable=enable
        )
        result = {"user_id": user_id, "admin": enable}
    else:
        raise ValueError(f"{FAMILY} handler 不支持工具: {name}")
    return ToolHandlerResult(result)


async def _latest_bot_message_id(context: ToolExecutionContext) -> int:
    session = context.session
    if session is None:
        raise PermissionError("撤回机器人消息需要数据库会话")
    bot_id = int(getattr(context.bot, "self_id", 0) or 0)
    row = await session.scalar(
        select(GroupAgentMessage)
        .where(
            GroupAgentMessage.group_id == context.group_id,
            GroupAgentMessage.bot_id == bot_id,
            GroupAgentMessage.role == "bot",
            GroupAgentMessage.received_at >= now_beijing() - BOT_RECALL_WINDOW,
        )
        .order_by(GroupAgentMessage.received_at.desc())
        .limit(1)
    )
    if row is None:
        raise ToolError(
            "机器人最近 2 分钟内没有可撤回的消息",
            code=ERROR_NOT_FOUND,
            hint="超过 2 分钟的消息只能请管理员用 recall_message 撤回",
        )
    return int(row.message_id)


async def _handle_recall(
    name: str, args: dict[str, Any], context: ToolExecutionContext
) -> ToolHandlerResult:
    bot = context.bot
    group_id = context.group_id
    bot_id = int(getattr(bot, "self_id", 0) or 0)
    message_id = args.get("message_id")
    if message_id is None:
        if name != "recall_bot_message":
            raise ToolArgumentError("message_id 不能为空")
        message_id = await _latest_bot_message_id(context)
    message_id = int(message_id)
    raw = await _require_current_group_message_api(bot, group_id, message_id)
    raw_sender = raw.get("sender")
    sender: dict[str, Any] = raw_sender if isinstance(raw_sender, dict) else {}
    try:
        sender_id = int(raw.get("user_id") or sender.get("user_id") or 0)
    except (TypeError, ValueError):
        sender_id = 0
    own_message = sender_id == bot_id and bot_id != 0
    if name == "recall_bot_message":
        if not own_message:
            raise PermissionError("recall_bot_message 只能撤回机器人自己的消息")
        sent_at = raw.get("time")
        try:
            age_seconds = time.time() - float(sent_at) if sent_at is not None else 0.0
        except (TypeError, ValueError):
            age_seconds = 0.0
        if (
            sent_at is not None
            and age_seconds > BOT_RECALL_WINDOW.total_seconds()
            and not context.capabilities.can_manage
        ):
            raise PermissionError("机器人不是管理员，只能撤回 2 分钟内的消息")
    elif not own_message:
        if not await target_can_be_muted(bot, group_id, sender_id, context.capabilities.role):
            raise PermissionError("机器人无权撤回该成员的消息")
        if not await actor_can_act_on_target(
            bot, group_id, context.actor_user_id, sender_id
        ):
            raise PermissionError("调用者无权撤回该成员的消息：管理员和群主的消息只能由群主处理")
    dbg(f"群 {group_id} {name}: 撤回消息 {message_id} sender={sender_id}")
    await bot.call_api("delete_msg", message_id=message_id)
    return ToolHandlerResult(
        {"message_id": message_id, "recalled": True, "own_message": own_message}
    )
