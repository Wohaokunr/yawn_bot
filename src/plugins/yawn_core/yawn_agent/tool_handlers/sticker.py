# ruff: noqa: TID252, TRY003, E501,C901,PLR0912
"""表情包收藏工具（属于 reaction 族）：成员让 Bot 把一张图收进表情包库。

取图沿用群聊附件入箱的取字节链路（``sandbox_inbox.fetch_bytes``：消息段
base64 → OneBot 接口 → 白名单下载 → 共享目录），模型永远看不到 URL 和路径。
是否直接可用看策略：``auto`` 直接可用、``review`` 进待审核（群主/管理员收藏的
直接可用）、``off`` 不收。

``steal_sticker`` 是 Bot 自己的“偷图”：群友发了好玩的表情包，Bot 觉得以后用得上就存下来，
不用谁开口。闸门比收藏更紧：``steal_mode``、每群每日次数与冷却、默认只偷 QQ 标成表情包的图、
发图的人退出记忆或在“别偷我的图”名单里都不偷。两者没写名字/标签时都由识图模型补
（``sticker_intake``），识图模型判断不适合当表情包的进待审核。
"""

from __future__ import annotations

from typing import Any

from ..sandbox_inbox import Attachment, InboxError, collect_recent, fetch_bytes
from ..sticker_intake import IntakeResult, check_image_shape, intake_image
from ..sticker_library import (
    STATUS_ACTIVE,
    STATUS_PENDING,
    StickerError,
    is_available,
)
from ..sticker_policy import (
    collects_today,
    effective_settings,
    note_collect,
    note_steal,
    sender_steals_today,
    steal_cooldown_remaining,
    steal_key,
    steals_today,
)
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
NAMES = frozenset(["save_sticker", "steal_sticker", "my_stickers"])
RECENT_MINUTES = 15

_FETCH_HINTS = {
    "too_large": "图片太大了，换一张小一点的",
    "expired": "图片已经过期取不到了，请对方重新发一次",
    "host_blocked": "图片来源不在允许下载的范围内",
}


def _images(items: list[Any]) -> list[Attachment]:
    return [item for item in items if isinstance(item, Attachment) and item.media_type == "image"]


def is_sticker_image(attachment: Attachment) -> bool:
    """QQ 把表情包图片标成 ``sub_type=1``（与 message_parser._is_sticker 一致），只看结构。"""

    sub_type = attachment.ref.get("sub_type")
    if sub_type is None:
        sub_type = attachment.ref.get("subType")
    return str(sub_type or "") == "1"


async def _pick(args: dict[str, Any], context: ToolExecutionContext, *, prefer_stickers: bool = False) -> Attachment:
    wanted = str(args.get("id") or "").strip()
    current = _images(list(context.attachments))
    if wanted:
        for item in current:
            if item.key == wanted:
                return item
    elif current:
        # 引用的那张优先：“@Bot 收藏这个”最常见的写法是回复那张表情。
        replies = [item for item in current if item.source == "reply"]
        pool = replies or current
        if prefer_stickers:
            pool = [item for item in pool if is_sticker_image(item)] or pool
        return pool[0]
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
        if prefer_stickers:
            return next((item for item in recent if is_sticker_image(item)), recent[0])
        return recent[0]
    raise ToolError(
        "本条消息、引用的消息和最近的群消息里都没有图片",
        code=ERROR_NOT_FOUND,
        hint="请对方回复那张表情包再说“收藏”，或者先把图发出来",
    )


async def _save(args: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
    if context.actor_user_id is None:
        raise ToolError("只有群成员明确要求时才能收藏表情包", code=ERROR_PERMISSION)
    settings = effective_settings(context.group_id)
    if not settings.enabled or not settings.group_open(context.group_id):
        raise ToolError("本群没有开放表情包功能", code=ERROR_PERMISSION, hint="如实告诉对方，不要说已经收藏")
    if settings.collect_mode == "off":
        raise ToolError("管理员关闭了成员收藏表情包", code=ERROR_PERMISSION, hint="可以让管理员在 WebUI 的表情包页上传")
    is_admin = await _actor_is_admin(context)
    if settings.collect_admin_only and not is_admin:
        raise ToolError("本群只有群主/管理员能收藏表情包", code=ERROR_PERMISSION)
    from ...affinity_perks import bonus_limit

    collect_limit = bonus_limit(settings.member_daily_collect, context.actor_user_id, "sticker_collect_bonus")
    if not is_admin and collects_today(context.group_id, context.actor_user_id) >= collect_limit:
        raise ToolError(
            f"你今天已经收藏了 {collect_limit} 张，明天再来",
            code=ERROR_QUOTA,
        )
    name = str(args.get("name") or "").strip()
    if not name and not (settings.ai_label_enabled and settings.ai_label_on_collect):
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
        result = await intake_image(
            fetched.data,
            name=name,
            tags=args.get("tags") or [],
            description=str(args.get("description") or ""),
            source="member",
            status=status,
            added_by=context.actor_user_id,
            added_group=context.group_id,
            origin=_origin(attachment, context, via="collect"),
            use_ai=settings.ai_label_on_collect,
            settings=settings,
        )
    except StickerError as exc:
        raise _intake_error(exc, "识图模型没起出名字：自己看图起一个，name 重新调用一次", "如实告诉对方没收藏成功") from exc
    if result.created:
        note_collect(context.group_id, context.actor_user_id)
    return _payload(result, context)


_REFUSED = frozenset({"trashed", "blocked", "animated", "too_small"})


def _intake_error(exc: StickerError, name_hint: str, other_hint: str) -> ToolError:
    """入库失败 → 工具错误：管理员删掉/拉黑的图、动图/太小的图都算“不许”，不是额度问题。"""

    if exc.reason in {"no_name", "not_image"}:
        return ToolError(str(exc), code=ERROR_INVALID_ARGS, hint=name_hint if exc.reason == "no_name" else other_hint)
    if exc.reason in _REFUSED:
        return ToolError(str(exc), code=ERROR_PERMISSION, hint=f"{other_hint}；这张就算了，别再试")
    return ToolError(str(exc), code=ERROR_QUOTA, hint=other_hint)


def _origin(attachment: Attachment, context: ToolExecutionContext, *, via: str) -> dict[str, Any]:
    """来源只给 WebUI 看（谁发的、哪条消息），不进提示词；成员要求忘掉时会被抹掉。"""

    origin: dict[str, Any] = {"via": via, "group_id": context.group_id, "from": attachment.source}
    if attachment.sender_id is not None:
        origin["sender_id"] = attachment.sender_id
    if attachment.sender_name:
        origin["sender_name"] = attachment.sender_name[:24]
    if attachment.message_id is not None:
        origin["message_id"] = attachment.message_id
    if is_sticker_image(attachment):
        origin["qq_sticker"] = True
    return origin


def _payload(result: IntakeResult, context: ToolExecutionContext) -> dict[str, Any]:
    sticker, created = result.sticker, result.created
    payload: dict[str, Any] = {
        "saved": created,
        "already_in_library": not created,
        "name": sticker.name,
        "tags": list(sticker.tags),
        "status": "可用" if sticker.usable else ("待管理员审核" if sticker.status == STATUS_PENDING else "已停用"),
    }
    if is_available(sticker, context.group_id):
        payload["next"] = f"以后可以在正文写 [表情:{sticker.name}] 发它"
    elif sticker.usable:
        # 库里已有但只对别的群开放：本群写 [表情:名字] 会被去掉，不能说能用了。
        payload["status"] = "未对本群开放"
        payload["next"] = "告诉对方库里已经有这张图，但管理员没把它开放给本群；不要说已经能用了"
    elif sticker.status == STATUS_PENDING:
        payload["next"] = "告诉对方已经交给管理员审核，审核通过后才会用；不要说已经能用了"
    if not created:
        payload["note"] = "库里已经有这张图了，没有重复收藏"
    if result.named_by == "ai":
        payload["named_by"] = "识图模型看图起的名字和标签"
    if result.held_for_review:
        payload["status"] = "待管理员审核"
        payload["next"] = "这张图识图模型拿不准（不太适合或没把握），交给管理员审核了；如实告诉对方，不要说已经能用了"
    if result.scoped_to_origin:
        payload["scope"] = "只在本群用"
    return payload


async def _steal(args: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
    if context.actor_user_id is None:
        raise ToolError("只有群成员在和你说话的回合里才能偷图", code=ERROR_PERMISSION)
    settings = effective_settings(context.group_id)
    if not settings.enabled or not settings.group_open(context.group_id):
        raise ToolError("本群没有开放表情包功能", code=ERROR_PERMISSION, hint="不要说偷到了")
    if not settings.steal_open(context.group_id):
        raise ToolError("管理员关了偷图（或本群不偷图）", code=ERROR_PERMISSION, hint="不要说偷到了；成员想要可以让他说“收藏这个”")
    key = steal_key(context.group_id)
    if steals_today(key) >= settings.group_daily_steals:
        raise ToolError(f"本群今天已经偷了 {settings.group_daily_steals} 张，明天再偷", code=ERROR_QUOTA, hint="不要说偷到了")
    wait = steal_cooldown_remaining(key)
    if wait > 0:
        raise ToolError(f"刚偷过一张，还要等 {int(wait) + 1} 秒", code=ERROR_QUOTA, hint="这次就算了，不要说偷到了")
    attachment = await _pick(args, context, prefer_stickers=True)
    if not settings.steal_photos and not is_sticker_image(attachment):
        raise ToolError(
            "这张不是表情包（是普通照片/截图），不偷",
            code=ERROR_PERMISSION,
            hint="只偷 QQ 标成表情包的图；成员真想存可以让他说“收藏这个”",
        )
    if attachment.sender_id is not None:
        if context.bot is not None and str(attachment.sender_id) == str(getattr(context.bot, "self_id", "")):
            raise ToolError("这是你自己发的图，不用偷", code=ERROR_INVALID_ARGS)
        if attachment.sender_id in settings.steal_protected_users:
            raise ToolError("发图的人说过别偷他的图", code=ERROR_PERMISSION, hint="尊重他，别偷，也别提名单的事")
        if context.session is not None:
            opted_out = await _opted_out_user_ids(context.session, context.group_id)
            if attachment.sender_id in opted_out:
                raise ToolError("发图的人退出了记忆，不能偷他的图", code=ERROR_PERMISSION)
        if settings.steal_sender_daily > 0 and sender_steals_today(attachment.sender_id) >= settings.steal_sender_daily:
            raise ToolError("今天已经偷了这个人好几张了，别老盯着一个人偷", code=ERROR_QUOTA, hint="这张就算了，不要说偷到了")
    try:
        fetched = await fetch_bytes(context.bot, context.group_id, attachment, max_bytes=settings.max_bytes)
    except InboxError as exc:
        raise ToolError(
            "没取到这张图",
            code=ERROR_UPSTREAM,
            hint=_FETCH_HINTS.get(exc.reason, "这次没偷成，不要说偷到了"),
        ) from exc
    try:
        check_image_shape(fetched.data, settings)
    except StickerError as exc:
        raise _intake_error(exc, "", "不要说偷到了") from exc
    status = STATUS_ACTIVE if settings.steal_mode == "auto" else STATUS_PENDING
    hint = str(args.get("reason") or "").strip()[:60]
    try:
        result = await intake_image(
            fetched.data,
            name=str(args.get("name") or ""),
            tags=args.get("tags") or [],
            description=str(args.get("description") or ""),
            source="stolen",
            status=status,
            added_group=context.group_id,
            origin=_origin(attachment, context, via="steal"),
            hint=f"偷图时的想法：{hint}" if hint else "",
            settings=settings,
        )
    except StickerError as exc:
        raise _intake_error(exc, "自己看图起个名字，name 再调用一次", "这次没偷成，不要说偷到了") from exc
    if result.created:
        note_steal(key, attachment.sender_id)
    payload = _payload(result, context)
    payload["stolen"] = payload.pop("saved")
    if result.created and result.sticker.usable:
        payload["tone"] = "可以俏皮地说一句“这张我偷了”，也可以什么都不说，下次直接用"
    elif not result.created:
        payload["note"] = "库里早就有这张了，不用偷"
    return payload


async def _mine(args: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
    """成员管自己的表情包：只作用于当前说话的人、只看本群来源的。"""

    from .. import sticker_self_service as service

    actor = context.actor_user_id
    if actor is None:
        raise ToolError("只有群成员自己开口时才能看/改他的表情包", code=ERROR_PERMISSION)
    action = str(args.get("action") or "list")
    if action == "list":
        return {"action": action, **service.list_own(actor, context.group_id)}
    if action == "rename":
        return {"action": action, **service.rename_own(actor, context.group_id, str(args.get("name") or ""), str(args.get("new_name") or ""))}
    if action == "remove":
        return {"action": action, **service.remove_own(actor, context.group_id, str(args.get("name") or ""))}
    if action in {"protect", "unprotect"}:
        return {"action": action, **service.protect(actor, on=action == "protect")}
    raise ToolError(f"不认识的动作 {action}", code=ERROR_INVALID_ARGS)


async def handle(name: str, args: dict[str, Any], context: ToolExecutionContext) -> ToolHandlerResult:
    if name == "my_stickers":
        return ToolHandlerResult(await _mine(args, context))
    if name == "save_sticker":
        return ToolHandlerResult(await _save(args, context))
    if name == "steal_sticker":
        return ToolHandlerResult(await _steal(args, context))
    raise ValueError(f"{FAMILY} handler 不支持工具: {name}")


__all__ = ["FAMILY", "NAMES", "handle"]
