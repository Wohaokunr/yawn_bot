# ruff: noqa: E501
"""成员自己管自己的表情包：看贡献过哪些、改名、删掉、说“别偷我的图”。

群聊工具 ``my_stickers`` 和私聊工具 ``stickers`` 共用这一份，规则只有一处：

- “我的”只有两种：我让 Bot 收藏的（``added_by`` 是我），和 Bot 从我这儿偷的
  （``origin.sender_id`` 是我）。群里只看本群来源的，私聊里看我名下全部；
- 改名只能改自己收藏的；删掉两种都可以（被偷的图本人有权让它消失）；
- 删掉 = 进回收站（策略 ``trash_days`` 为 0 时直接删），管理员还能捞回来；
- “别偷我的图”写进策略里的 ``steal_protected_users``，和管理员在 WebUI 填的是同一份名单；
- 两个开关：``member_manage_own``、``member_self_protect``。

这里不收 user_id 参数的“别人”：调用方只能传当前说话的人。
"""

from __future__ import annotations

from typing import Any

from .sticker_library import (
    STATUS_PENDING,
    Sticker,
    StickerError,
    all_stickers,
    clean_name,
    delete_sticker,
    trash_sticker,
    update_sticker,
)
from .sticker_policy import forget_stats, load_settings, set_protected, stats_snapshot
from .tool_args import ERROR_INVALID_ARGS, ERROR_NOT_FOUND, ERROR_PERMISSION, ToolError

LIST_LIMIT = 20


def relation(item: Sticker, user_id: int, group_id: int | None) -> str:
    """这张和他什么关系：``collected``（他收藏的）/ ``stolen``（从他那偷的）/ 空串（没关系）。"""

    origin = item.origin
    if item.added_by == user_id and item.source != "stolen" and (group_id is None or item.added_group == group_id):
        return "collected"
    sent_by_him = str(origin.get("sender_id") or "") == str(user_id)
    if sent_by_him and (group_id is None or str(origin.get("group_id") or "") == str(group_id) or (origin.get("via") == "private" and group_id is None)):
        return "stolen"
    if item.added_by == user_id and item.source == "stolen" and group_id is None:
        # 私聊里他让存的图（source=stolen、added_by 是他）。
        return "stolen"
    return ""


def owned(user_id: int, group_id: int | None) -> list[tuple[Sticker, str]]:
    return [(item, how) for item in all_stickers() if (how := relation(item, user_id, group_id))]


def _status(item: Sticker) -> str:
    if item.usable:
        return "可用"
    return "待审核" if item.status == STATUS_PENDING else "停用"


def list_own(user_id: int, group_id: int | None) -> dict[str, Any]:
    rows = owned(user_id, group_id)
    stats = stats_snapshot()
    settings = load_settings()

    def uses(item: Sticker) -> int:
        row = stats.get(item.sticker_id)
        return int(row.get("uses", 0)) if isinstance(row, dict) else 0

    return {
        "count": len(rows),
        "collected": sum(1 for _item, how in rows if how == "collected"),
        "stolen_from_him": sum(1 for _item, how in rows if how == "stolen"),
        "stickers": [
            {"name": item.name, "status": _status(item), "how": "他收藏的" if how == "collected" else "从他那偷的", "sent_times": uses(item)}
            for item, how in rows[-LIST_LIMIT:]
        ],
        "protected": user_id in settings.steal_protected_users,
        "can_manage": settings.member_manage_own,
    }


def _find(user_id: int, group_id: int | None, name: str) -> tuple[Sticker, str]:
    wanted = "".join(str(name or "").split()).casefold()
    if not wanted:
        raise ToolError("要写是哪一张（name）", code=ERROR_INVALID_ARGS, hint="先用 list 看他有哪些")
    matches = [(item, how) for item, how in owned(user_id, group_id) if "".join(item.name.split()).casefold() == wanted or item.sticker_id.casefold() == wanted]
    if not matches:
        raise ToolError(
            f"他名下没有叫“{str(name)[:16]}”的表情包",
            code=ERROR_NOT_FOUND,
            hint="只能动他自己收藏的、或从他那偷的；先用 list 看名字，别说已经改好/删掉了",
        )
    return matches[-1]


def _require_manage() -> None:
    if not load_settings().member_manage_own:
        raise ToolError("管理员没开放成员自己改/删表情包", code=ERROR_PERMISSION, hint="如实说做不了，可以让他找管理员")


def rename_own(user_id: int, group_id: int | None, name: str, new_name: str) -> dict[str, Any]:
    _require_manage()
    item, how = _find(user_id, group_id, name)
    if how != "collected":
        raise ToolError("这张是从他那偷的，不是他收藏的，名字由管理员定", code=ERROR_PERMISSION, hint="他可以让你把它删掉（remove）")
    cleaned = clean_name(new_name)
    if not cleaned:
        raise ToolError("新名字不能为空", code=ERROR_INVALID_ARGS)
    try:
        updated = update_sticker(item.sticker_id, name=cleaned)
    except StickerError as exc:
        raise ToolError(str(exc), code=ERROR_INVALID_ARGS) from exc
    return {"renamed": True, "old_name": item.name, "name": updated.name, "next": f"以后写 [表情:{updated.name}] 发它" if updated.usable else "这张还在等管理员审核"}


def remove_own(user_id: int, group_id: int | None, name: str) -> dict[str, Any]:
    _require_manage()
    item, how = _find(user_id, group_id, name)
    try:
        if load_settings().trash_days > 0:
            trash_sticker(item.sticker_id)
            recoverable = True
        else:
            delete_sticker(item.sticker_id)
            forget_stats(item.sticker_id)
            recoverable = False
    except StickerError as exc:
        raise ToolError(str(exc), code=ERROR_NOT_FOUND) from exc
    return {
        "removed": True,
        "name": item.name,
        "how": "他收藏的" if how == "collected" else "从他那偷的",
        "note": "已经从库里拿掉，不会再发了" + ("（管理员在回收站里还能看到）" if recoverable else ""),
    }


def protect(user_id: int, *, on: bool) -> dict[str, Any]:
    settings = load_settings()
    if not settings.member_self_protect:
        raise ToolError("管理员没开放成员自己设置“别偷我的图”", code=ERROR_PERMISSION, hint="如实说做不了，可以让他找管理员")
    changed = set_protected(user_id, on)
    already = not changed and (user_id in load_settings().steal_protected_users) == on
    if not changed and not already:
        raise ToolError("名单满了，没加上", code=ERROR_PERMISSION, hint="如实说没设置成功，让他找管理员")
    return {
        "protected": on,
        "changed": changed,
        "note": ("以后不偷他发的图了" if on else "以后可以偷他发的图了") + ("" if changed else "（本来就是这样）"),
    }


__all__ = ["list_own", "owned", "protect", "relation", "remove_own", "rename_own"]
