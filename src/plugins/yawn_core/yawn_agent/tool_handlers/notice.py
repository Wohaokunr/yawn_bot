# ruff: noqa: TID252, TRY003, E501
"""群公告：读取、发布（置顶/需确认/防重复）与删除。"""

from __future__ import annotations

import re
from typing import Any

from ..log import dbg, dbg_exc
from ..speech_format import strip_announcement_markup
from ..speech_quality import speech_similarity
from ..tool_args import ERROR_DUPLICATE, ERROR_NOT_FOUND, ToolArgumentError, ToolError
from ..tool_execution import ToolExecutionContext, ToolHandlerResult
from ..tool_support import (
    _compact_notice,
    _payload_list,
    _tool_result_limit,
    sort_by_time_desc,
)

FAMILY = "notice"
NAMES = frozenset(
    ["list_group_notices", "create_group_announcement", "delete_group_notice"]
)
# 与现有公告的相似度达到该值即视为重复发布。
DUPLICATE_NOTICE_SIMILARITY = 0.9
_MAX_NOTICE_SCAN = 20
_MEANINGFUL_RE = re.compile(r"[\w一-鿿]")


async def _fetch_notices(bot: Any, group_id: int) -> list[dict[str, Any]]:
    raw_notices = await bot.call_api("_get_group_notice", group_id=group_id)
    notices = _payload_list(raw_notices, "notices", "items")
    if (
        isinstance(raw_notices, dict)
        and not notices
        and any(key in raw_notices for key in ("notice_id", "content", "message"))
    ):
        notices = [raw_notices]
    return sort_by_time_desc(
        [
            _compact_notice(item)
            for item in notices[:_MAX_NOTICE_SCAN]
            if isinstance(item, dict)
        ]
    )


def _notice_id_from_result(raw: Any) -> str | None:
    if isinstance(raw, dict):
        for key in ("notice_id", "noticeId", "fid", "id"):
            value = raw.get(key)
            if value not in (None, ""):
                return str(value)[:128]
    if isinstance(raw, str) and raw.strip():
        return raw.strip()[:128]
    return None


async def _reject_duplicate(bot: Any, group_id: int, content: str, context: ToolExecutionContext) -> None:
    if not context.capabilities.has("_get_group_notice"):
        return
    try:
        existing = await _fetch_notices(bot, group_id)
    except Exception:  # noqa: BLE001
        # 读公告失败不阻止发布；重复保护是尽力而为。
        dbg_exc(f"群 {group_id} 发布公告前读取现有公告失败,跳过重复检查")
        return
    for notice in existing:
        previous = str(notice.get("content") or "")
        if speech_similarity(previous, content) >= DUPLICATE_NOTICE_SIMILARITY:
            raise ToolError(
                "已有内容几乎相同的群公告，未重复发布",
                code=ERROR_DUPLICATE,
                hint=(
                    f"已有公告 notice_id={notice.get('notice_id')}，"
                    f"发布于 {notice.get('publish_time') or '未知时间'}；直接告诉对方即可"
                ),
            )


async def handle(  # noqa: C901, PLR0912
    name: str, args: dict[str, Any], context: ToolExecutionContext
) -> ToolHandlerResult:
    bot = context.bot
    group_id = context.group_id
    capabilities = context.capabilities
    if name == "list_group_notices":
        limit = _tool_result_limit(args, default=10, maximum=20)
        keyword = str(args.get("keyword") or "").strip().casefold()
        notices = await _fetch_notices(bot, group_id)
        if keyword:
            notices = [
                item
                for item in notices
                if keyword in str(item.get("content") or "").casefold()
            ]
        return ToolHandlerResult(notices[:limit])

    if name == "create_group_announcement":
        content = strip_announcement_markup(args.get("content"))
        if not _MEANINGFUL_RE.search(content):
            raise ToolArgumentError(
                "公告内容为空", hint="先确认要发布的公告正文，再调用"
            )
        await _reject_duplicate(bot, group_id, content, context)
        # NapCat/go-cqhttp 使用 `_send_group_notice`；只有适配器明确只暴露
        # `send_group_notice` 时才使用别名。不要在失败后自动重试另一个
        # action，避免公告其实已创建但回执失败时产生重复公告。
        action = (
            "_send_group_notice"
            if capabilities.has("_send_group_notice")
            else "send_group_notice"
        )
        params: dict[str, Any] = {"group_id": group_id, "content": content}
        # 只在明确需要时传扩展字段，旧版 go-cqhttp 不认识这些参数。
        options = {
            "pinned": "pinned",
            "confirm_required": "confirm_required",
            "show_edit_card": "is_show_edit_card",
        }
        for arg_name, api_name in options.items():
            if args.get(arg_name) is True:
                params[api_name] = 1
        dbg(f"群 {group_id} 发布公告: chars={len(content)} options={sorted(params)}")
        raw = await bot.call_api(action, **params)
        result: dict[str, Any] = {
            "published": True,
            "chars": len(content),
            "preview": content[:60],
        }
        notice_id = _notice_id_from_result(raw)
        if notice_id:
            result["notice_id"] = notice_id
        for arg_name in options:
            if args.get(arg_name) is True:
                result[arg_name] = True
        return ToolHandlerResult(result)

    if name == "delete_group_notice":
        notice_id = str(args.get("notice_id") or "").strip()
        if not notice_id:
            raise ToolArgumentError("notice_id 不能为空")
        target: dict[str, Any] | None = None
        if capabilities.has("_get_group_notice"):
            notices = await _fetch_notices(bot, group_id)
            target = next(
                (item for item in notices if str(item.get("notice_id")) == notice_id),
                None,
            )
            if target is None:
                raise ToolError(
                    "当前群没有这个 notice_id 的公告",
                    code=ERROR_NOT_FOUND,
                    hint="先用 list_group_notices 确认要删除的公告",
                )
        await bot.call_api("_del_group_notice", group_id=group_id, notice_id=notice_id)
        result = {"deleted": True, "notice_id": notice_id}
        if target is not None:
            result["content_preview"] = str(target.get("content") or "")[:60]
            if target.get("publish_time"):
                result["publish_time"] = target["publish_time"]
        return ToolHandlerResult(result)

    raise ValueError(f"{FAMILY} handler 不支持工具: {name}")
