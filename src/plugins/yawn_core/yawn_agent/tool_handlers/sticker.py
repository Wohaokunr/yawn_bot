# ruff: noqa: TID252, TRY003, E501,C901,PLR0912
"""表情包收藏工具（属于 reaction 族）：成员让 Bot 把一张图收进表情包库。

取图沿用群聊附件入箱的取字节链路（``sandbox_inbox.fetch_bytes``：消息段
base64 → OneBot 接口 → 白名单下载 → 共享目录），模型永远看不到 URL 和路径。
是否直接可用看策略：``auto`` 直接可用、``review`` 进待审核（群主/管理员收藏的
直接可用）、``off`` 不收。
"""

from __future__ import annotations

from typing import Any

from ..sandbox_inbox import Attachment, InboxError, collect_recent, fetch_bytes
from ..sticker_library import (
    STATUS_ACTIVE,
    STATUS_PENDING,
    StickerError,
    add_sticker,
)
from ..sticker_policy import collects_today, load_settings, note_collect
from ..tool_args import (
    ERROR_INVALID_ARGS,
    ERROR_NOT_FOUND,
    ERROR_PERMISSION,
    ERROR_QUOTA,
    ERROR_UPSTREAM,
    ToolError,
)
from ..tool_execution import ToolExecutionContext, ToolHandlerResult
from ..tool_support import _opted_out_user_ids
from .web import _actor_is_admin

FAMILY = "reaction"
NAMES = frozenset(["save_sticker"])
RECENT_MINUTES = 15

_FETCH_HINTS = {
    "too_large": "图片太大了，换一张小一点的",
    "expired": "图片已经过期取不到了，请对方重新发一次",
    "host_blocked": "图片来源不在允许下载的范围内",
}


def _images(items: list[Any]) -> list[Attachment]:
    return [item for item in items if isinstance(item, Attachment) and item.media_type == "image"]


async def _pick(args: dict[str, Any], context: ToolExecutionContext) -> Attachment:
    wanted = str(args.get("id") or "").strip()
    current = _images(list(context.attachments))
    if wanted:
        for item in current:
            if item.key == wanted:
                return item
    elif current:
        # 引用的那张优先：“@Bot 收藏这个”最常见的写法是回复那张表情。
        replies = [item for item in current if item.source == "reply"]
        return (replies or current)[0]
    recent = _images(
        await collect_recent(
            context.session,
            context.group_id,
            minutes=RECENT_MINUTES,
            limit=30,
        )
    )
    if wanted:
        for item in recent:
            if item.key == wanted:
                return item
        raise ToolError(
            f"找不到 id 为 {wanted} 的图片",
            code=ERROR_NOT_FOUND,
            hint="不写 id 默认收藏本条或引用消息里的图；也可以用 list_chat_attachments 看最近的图片 id",
        )
    if recent:
        return recent[0]
    raise ToolError(
        "本条消息、引用的消息和最近的群消息里都没有图片",
        code=ERROR_NOT_FOUND,
        hint="请对方回复那张表情包再说“收藏”，或者先把图发出来",
    )


async def _save(args: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
    if context.actor_user_id is None:
        raise ToolError("只有群成员明确要求时才能收藏表情包", code=ERROR_PERMISSION)
    settings = load_settings()
    if not settings.enabled or not settings.group_open(context.group_id):
        raise ToolError("本群没有开放表情包功能", code=ERROR_PERMISSION, hint="如实告诉对方，不要说已经收藏")
    if settings.collect_mode == "off":
        raise ToolError("管理员关闭了成员收藏表情包", code=ERROR_PERMISSION, hint="可以让管理员在 WebUI 的表情包页上传")
    is_admin = await _actor_is_admin(context)
    if settings.collect_admin_only and not is_admin:
        raise ToolError("本群只有群主/管理员能收藏表情包", code=ERROR_PERMISSION)
    if not is_admin and collects_today(context.group_id, context.actor_user_id) >= settings.member_daily_collect:
        raise ToolError(
            f"你今天已经收藏了 {settings.member_daily_collect} 张，明天再来",
            code=ERROR_QUOTA,
        )
    name = str(args.get("name") or "").strip()
    if not name:
        raise ToolError("要给表情包起个名字", code=ERROR_INVALID_ARGS, hint="name 写几个字说明它表达什么，如“捂脸笑”“狗头”")
    attachment = await _pick(args, context)
    if attachment.sender_id is not None and context.session is not None:
        opted_out = await _opted_out_user_ids(context.session, context.group_id)
        if attachment.sender_id in opted_out:
            raise ToolError("这张图的主人退出了记忆，不能收藏他发的图", code=ERROR_PERMISSION)
    try:
        fetched = await fetch_bytes(context.bot, context.group_id, attachment, max_bytes=settings.max_bytes)
    except InboxError as exc:
        raise ToolError(
            "没取到这张图",
            code=ERROR_UPSTREAM,
            hint=_FETCH_HINTS.get(exc.reason, "如实告诉对方没收藏成功，可以让他重新发一次"),
        ) from exc
    status = STATUS_ACTIVE if settings.collect_mode == "auto" or is_admin else STATUS_PENDING
    try:
        sticker, created = add_sticker(
            fetched.data,
            name=name,
            tags=args.get("tags") or [],
            description=str(args.get("description") or ""),
            source="member",
            status=status,
            added_by=context.actor_user_id,
            added_group=context.group_id,
            max_bytes=settings.max_bytes,
        )
    except StickerError as exc:
        raise ToolError(str(exc), code=ERROR_INVALID_ARGS if exc.reason in {"no_name", "not_image"} else ERROR_QUOTA) from exc
    if created:
        note_collect(context.group_id, context.actor_user_id)
    payload: dict[str, Any] = {
        "saved": created,
        "already_in_library": not created,
        "name": sticker.name,
        "tags": list(sticker.tags),
        "status": "可用" if sticker.usable else ("待管理员审核" if sticker.status == STATUS_PENDING else "已停用"),
    }
    if sticker.available_in(context.group_id):
        payload["next"] = f"以后可以在正文写 [表情:{sticker.name}] 发它"
    elif sticker.usable:
        # 库里已有但只对别的群开放：本群写 [表情:名字] 会被去掉，不能说能用了。
        payload["status"] = "未对本群开放"
        payload["next"] = "告诉对方库里已经有这张图，但管理员没把它开放给本群；不要说已经能用了"
    elif sticker.status == STATUS_PENDING:
        payload["next"] = "告诉对方已经交给管理员审核，审核通过后才会用；不要说已经能用了"
    if not created:
        payload["note"] = "库里已经有这张图了，没有重复收藏"
    return payload


async def handle(name: str, args: dict[str, Any], context: ToolExecutionContext) -> ToolHandlerResult:
    if name == "save_sticker":
        return ToolHandlerResult(await _save(args, context))
    raise ValueError(f"{FAMILY} handler 不支持工具: {name}")


__all__ = ["FAMILY", "NAMES", "handle"]
