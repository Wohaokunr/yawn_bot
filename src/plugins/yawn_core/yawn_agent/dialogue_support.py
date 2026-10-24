# ruff: noqa: TID252, TC001, PLR0913, PLR0917
"""Small compatibility-safe helpers shared by dialogue/proactive paths."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from ..data_models.group_agent_message import GroupAgentMessage
from .context import now_beijing
from .log import dbg, dbg_exc
from .message_parser import NormalizedMessage


def contains_word(text: str, word: str) -> bool:
    if not word:
        return False
    if re.fullmatch(r"[a-z0-9 ]+", word):
        return (
            re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) is not None
        )
    return word in text


def current_turn_focus_ids(
    actor_user_id: int,
    normalized: NormalizedMessage,
    *,
    bot_id: int | None = None,
) -> list[int]:
    focus = [int(actor_user_id)]
    focus.extend(
        int(user_id)
        for user_id in normalized.mentions
        if bot_id is None or int(user_id) != bot_id
    )
    if normalized.reply_chain:
        raw_user_id = normalized.reply_chain[0].get("user_id")
        try:
            reply_user_id = int(str(raw_user_id))
        except (TypeError, ValueError):
            reply_user_id = 0
        if reply_user_id > 0 and reply_user_id != bot_id:
            focus.append(reply_user_id)
    return list(dict.fromkeys(focus))


def is_recent_duplicate(
    item: object,
    input_fingerprint: str,
    response_fingerprint: str,
    now: datetime,
) -> bool:
    if (
        not isinstance(item, dict)
        or item.get("input") != input_fingerprint
        or item.get("response") != response_fingerprint
    ):
        return False
    raw_at = item.get("at")
    if not raw_at:
        return True
    try:
        return now - datetime.fromisoformat(str(raw_at)) < timedelta(minutes=10)
    except (TypeError, ValueError):
        return False


async def persist_bot_reply(
    session: Any,
    bot_id: int,
    group_id: int,
    message_id: int | None,
    text: str,
    retention_days: int,
    *,
    segments: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    reply_chain: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    forward_tree: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    media_refs: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
) -> None:
    if not message_id:
        dbg(f"群 {group_id} bot 发言缺少 message_id,跳过自言落库")
        return
    duplicate = await session.scalar(
        select(GroupAgentMessage).where(
            GroupAgentMessage.bot_id == bot_id,
            GroupAgentMessage.group_id == group_id,
            GroupAgentMessage.message_id == message_id,
        )
    )
    if duplicate is not None:
        dbg(f"群 {group_id} bot 发言 {message_id} 已落库过,去重跳过")
        return
    now = now_beijing()
    retention = max(1, min(int(retention_days), 365))
    session.add(
        GroupAgentMessage(
            bot_id=bot_id,
            message_id=message_id,
            group_id=group_id,
            user_id=bot_id,
            sender_name=None,
            role="bot",
            title=None,
            normalized_text=text,
            segments=list(segments or []),
            reply_chain=list(reply_chain or []),
            forward_tree=list(forward_tree or []),
            media_refs=list(media_refs or []),
            received_at=now,
            expires_at=now + timedelta(days=retention),
        )
    )
    dbg(f"群 {group_id} bot 发言 {message_id} 已加入自言落库(role=bot)")



async def quote_target(
    session: Any,
    *,
    group_id: int,
    bot_id: int,
    actor_user_id: int,
    message_id: int | None,
    normalized: NormalizedMessage,
) -> int | None:
    """群里在触发之后有人插话（或本轮合并了多条消息）时，引用触发消息。

    一对一的连续对话不引用，读起来更自然；多人同时说话时不引用就分不清
    Bot 在回谁。查询失败只是不引用，不影响发送。
    """

    if message_id is None or int(message_id) == 0:
        return None
    if normalized.merged_message_ids:
        return int(message_id)
    try:
        trigger_row_id = await session.scalar(
            select(GroupAgentMessage.id).where(
                GroupAgentMessage.bot_id == bot_id,
                GroupAgentMessage.group_id == group_id,
                GroupAgentMessage.message_id == int(message_id),
            )
        )
        if trigger_row_id is None:
            return None
        interleaved = await session.scalar(
            select(func.count(GroupAgentMessage.id)).where(
                GroupAgentMessage.bot_id == bot_id,
                GroupAgentMessage.group_id == group_id,
                GroupAgentMessage.id > int(trigger_row_id),
                GroupAgentMessage.user_id != actor_user_id,
            )
        )
    except Exception:  # noqa: BLE001
        dbg_exc(f"群 {group_id} 判断是否需要引用触发消息失败(按不引用处理)")
        return None
    return int(message_id) if int(interleaved or 0) > 0 else None


__all__ = [
    "contains_word",
    "current_turn_focus_ids",
    "is_recent_duplicate",
    "persist_bot_reply",
    "quote_target",
]
