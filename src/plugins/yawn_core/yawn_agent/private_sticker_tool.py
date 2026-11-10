# ruff: noqa: E501, TRY003, PLR2004
"""私聊工具 ``stickers``：私聊里也能“偷”他发来的表情包、查表情包库、看他贡献过哪些。

- ``steal``：把他本轮（或 10 分钟内）发来的一张图存进表情包库。可以是他让你收藏，
  也可以是你自己喜欢就偷——两种都走这一个动作；名字/标签不写就让识图模型起；
- ``search``：按名字/情绪/图上的字在私聊能用的（所有群可用的）表情包里找，返回名字，
  正文写 ``[表情:名字]`` 就能发；
- ``mine``：他收藏/被偷过的表情包（只列名字和状态）。

闸门：表情包总开关、``private_steal_enabled``、``steal_mode``、每人每日 ``private_daily_steals``、
冷却；默认只收 QQ 标成表情包的图（``steal_photos`` 打开才收照片），私聊来的照片一律待审核；
他在“别偷我的图”名单里时不偷。偷来的图对所有群开放与否由管理员在 WebUI 决定（默认全部可用）。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from .sticker_intake import intake_image
from .sticker_library import (
    STATUS_ACTIVE,
    STATUS_PENDING,
    StickerError,
    all_stickers,
    available_stickers,
    search_text,
)
from .sticker_policy import (
    load_settings,
    note_steal,
    steal_cooldown_remaining,
    steal_key,
    steals_today,
)
from .tool_args import (
    ERROR_INVALID_ARGS,
    ERROR_NOT_FOUND,
    ERROR_PERMISSION,
    ERROR_QUOTA,
    ERROR_UPSTREAM,
    ToolError,
)

if TYPE_CHECKING:
    from .private_tools import PrivateToolContext

ACTIONS = ("steal", "search", "mine")
RECENT_SECONDS = 10 * 60
RECENT_PER_USER = 4
SEARCH_LIMIT = 8
DESCRIPTION = (
    "表情包：steal 把他发来的一张图存进你的表情包库（他让你收藏，或者你自己喜欢就偷，都用它；"
    "默认取他这条消息里的图，没有就取 10 分钟内他发的最近一张，index 指定第几张；name/tags 不写就让识图模型看图起）；"
    "search 按名字/情绪找能发的表情包（query），正文写 [表情:名字] 发；mine 看他收藏/被你偷过的表情包。"
    "没有本轮成功的 steal 不能说“偷到了/收藏好了”"
)
PROPERTIES: dict[str, Any] = {
    "action": {"type": "string", "enum": list(ACTIONS)},
    "index": {"type": "integer", "minimum": 1, "maximum": 4, "description": "第几张图（从 1 数，默认 1）"},
    "name": {"type": "string", "minLength": 1, "maxLength": 16},
    "tags": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 10}, "maxItems": 6},
    "description": {"type": "string", "maxLength": 120},
    "query": {"type": "string", "minLength": 1, "maxLength": 20},
}

_recent: dict[int, list[tuple[float, dict[str, Any]]]] = {}


def remember_media(user_id: int, media: list[dict[str, Any]] | None, *, now: float | None = None) -> None:
    """记住他最近发来的图片引用（进程内、有界、10 分钟），供之后几轮 steal 用。"""

    images = [dict(item) for item in media or [] if str(item.get("type") or "") == "image"]
    if not images:
        return
    now = time.monotonic() if now is None else now
    rows = [row for row in _recent.get(user_id, []) if now - row[0] < RECENT_SECONDS]
    rows.extend((now, item) for item in images)
    _recent[user_id] = rows[-RECENT_PER_USER:]
    if len(_recent) > 4096:
        _recent.pop(next(iter(_recent)))


def recent_media(user_id: int, *, now: float | None = None) -> list[dict[str, Any]]:
    now = time.monotonic() if now is None else now
    rows = [row for row in _recent.get(user_id, []) if now - row[0] < RECENT_SECONDS]
    _recent[user_id] = rows
    return [item for _ts, item in rows]


def reset_for_tests() -> None:
    _recent.clear()


def _is_qq_sticker(ref: dict[str, Any]) -> bool:
    sub_type = ref.get("sub_type", ref.get("subType"))
    return str(sub_type or "") == "1"


def _pick(ctx: PrivateToolContext, index: int) -> dict[str, Any]:
    current = [item for item in getattr(ctx, "media", []) or [] if str(item.get("type") or "") == "image"]
    pool = current or list(reversed(recent_media(ctx.user_id)))
    if not pool:
        raise ToolError(
            "他这条消息和最近 10 分钟都没有发图片",
            code=ERROR_NOT_FOUND,
            hint="让他先把那张表情包发过来",
        )
    if index > len(pool):
        raise ToolError(f"只有 {len(pool)} 张图", code=ERROR_INVALID_ARGS)
    return pool[index - 1]


async def _steal(args: dict[str, Any], ctx: PrivateToolContext) -> dict[str, Any]:  # noqa: C901, PLR0912
    settings = load_settings()
    if not settings.enabled or not settings.private_steal_enabled or settings.steal_mode == "off":
        raise ToolError("表情包收藏/偷图在私聊里没开", code=ERROR_PERMISSION, hint="如实说现在存不了，别说存好了")
    if ctx.user_id in settings.steal_protected_users:
        raise ToolError("他说过别存他的图", code=ERROR_PERMISSION, hint="尊重他的意思")
    key = steal_key(None, ctx.user_id)
    if not ctx.is_superuser and steals_today(key) >= settings.private_daily_steals:
        raise ToolError(f"今天已经存了 {settings.private_daily_steals} 张他发的图，明天再说", code=ERROR_QUOTA)
    wait = steal_cooldown_remaining(key)
    if wait > 0 and not ctx.is_superuser:
        raise ToolError(f"刚存过一张，还要等 {int(wait) + 1} 秒", code=ERROR_QUOTA)
    ref = _pick(ctx, int(args.get("index") or 1))
    qq_sticker = _is_qq_sticker(ref)
    if not qq_sticker and not settings.steal_photos:
        raise ToolError(
            "这张是普通照片/截图，不是表情包，不存",
            code=ERROR_PERMISSION,
            hint="只收 QQ 表情包；如实说明",
        )
    from .sandbox_inbox import Attachment, InboxError, fetch_bytes

    attachment = Attachment(
        key="m1",
        media_type="image",
        source="current",
        ref={key_: value for key_, value in ref.items() if key_ != "type"},
        index=1,
        sender_id=ctx.user_id,
    )
    try:
        fetched = await fetch_bytes(ctx.bot, 0, attachment, max_bytes=settings.max_bytes)
    except InboxError as exc:
        raise ToolError("没取到这张图", code=ERROR_UPSTREAM, hint="图可能过期了，让他重新发一次；别说存好了") from exc
    # 私聊来的照片一律待审核；表情包看偷图模式（超级管理员发的直接可用）。
    auto = settings.steal_mode == "auto" and qq_sticker
    status = STATUS_ACTIVE if auto or ctx.is_superuser else STATUS_PENDING
    origin: dict[str, Any] = {"via": "private", "sender_id": ctx.user_id, "qq_sticker": qq_sticker}
    if ctx.nickname:
        origin["sender_name"] = str(ctx.nickname)[:24]
    try:
        result = await intake_image(
            fetched.data,
            name=str(args.get("name") or ""),
            tags=args.get("tags") or [],
            description=str(args.get("description") or ""),
            source="stolen",
            status=status,
            added_by=ctx.user_id,
            origin=origin,
            settings=settings,
        )
    except StickerError as exc:
        raise ToolError(
            str(exc),
            code=ERROR_INVALID_ARGS,
            hint="自己看图起个名字，name 再调用一次" if exc.reason == "no_name" else "别说存好了",
        ) from exc
    if result.created:
        note_steal(key)
    sticker = result.sticker
    payload: dict[str, Any] = {
        "action": "steal",
        "done": result.created,
        "already_in_library": not result.created,
        "name": sticker.name,
        "tags": list(sticker.tags),
    }
    if sticker.available_in(None):
        payload["status"] = "可用"
        payload["next"] = f"私聊和群里都能写 [表情:{sticker.name}] 发它"
    elif sticker.usable:
        payload["status"] = "只在部分群可用"
    else:
        payload["status"] = "待管理员审核"
        payload["next"] = "告诉他交给管理员审核了，通过后才能用；别说已经能用了"
    if result.named_by == "ai":
        payload["named_by"] = "识图模型看图起的名字"
    if result.held_for_review:
        payload["note"] = "识图模型觉得这张不太适合当表情包，先给管理员看看"
    return payload


def _search(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip().casefold()
    if not query:
        raise ToolError("search 要写 query", code=ERROR_INVALID_ARGS)
    rows = []
    for item in available_stickers(None):
        if query in search_text(item).casefold():
            rows.append({"name": item.name, "tags": list(item.tags[:3])})
        if len(rows) >= SEARCH_LIMIT:
            break
    return {
        "action": "search",
        "found": rows,
        "next": "正文写 [表情:名字] 发" if rows else "没找到；别编名字",
    }


def _mine(ctx: PrivateToolContext) -> dict[str, Any]:
    rows = []
    for item in all_stickers():
        origin = item.origin
        if item.added_by == ctx.user_id or str(origin.get("sender_id") or "") == str(ctx.user_id):
            rows.append({"name": item.name, "status": "可用" if item.usable else ("待审核" if item.status == STATUS_PENDING else "停用"), "stolen": item.source == "stolen"})
    return {"action": "mine", "count": len(rows), "stickers": rows[-20:]}


async def stickers_tool(args: dict[str, Any], ctx: PrivateToolContext) -> dict[str, Any]:
    action = str(args.get("action") or "")
    if action == "steal":
        return await _steal(args, ctx)
    if action == "search":
        return _search(args)
    if action == "mine":
        return _mine(ctx)
    raise ToolError(f"不认识的动作 {action}", code=ERROR_INVALID_ARGS)


__all__ = [
    "ACTIONS",
    "DESCRIPTION",
    "PROPERTIES",
    "recent_media",
    "remember_media",
    "reset_for_tests",
    "stickers_tool",
]
