"""Small compatibility-safe helpers shared by dialogue/proactive paths."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot
from sqlalchemy import select

from ..data_models.group_agent_message import GroupAgentMessage
from .collector import is_pending_trigger_expired
from .context import now_beijing
from .execution_trace import trace_event
from .log import dbg, dbg_exc
from .message_parser import NormalizedMessage
from .outbound import (
    DELIVERY_CONFIRMED_FAILURE,
    PreparedOutboundMessage,
    SendResult,
    prepare_text_message,
    send_prepared_outbound,
)

_GREETING_WORDS = ("你好", "嗨", "hello", "hi", "早上好", "晚上好", "在吗", "在不在")


def contains_word(text: str, word: str) -> bool:
    if not word:
        return False
    if re.fullmatch(r"[a-z0-9 ]+", word):
        return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) is not None
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


def deterministic_reply(text: str) -> str | None:
    normalized = " ".join(text.lower().split())
    if any(contains_word(normalized, word) for word in _GREETING_WORDS):
        return "我在呀，有事直接说～"
    if "agent状态" in normalized or "群聊agent" in normalized:
        return "群聊 Agent 在线；复杂对话需要配置 AI_API_KEY。"
    return None


async def send_group_text(
    bot: Bot, group_id: int, text: str
) -> tuple[bool, int | None]:
    try:
        prepared = prepare_text_message(text)
        result = await send_prepared_outbound(bot, group_id, prepared)
    except Exception:  # noqa: BLE001
        logger.warning("群聊 Agent 发送群消息失败: %s", group_id)
        dbg_exc(f"群 {group_id} 发送群消息失败 text={text!r}")
        return False, None
    dbg(f"群 {group_id} 发送群消息成功 text={text!r}")
    return result.ends_turn, result.message_id


async def send_unless_expired(
    bot: Bot,
    group_id: int,
    message: str | PreparedOutboundMessage,
    enqueued_at: float | None,
    *,
    label: str,
    message_id: Any = None,
    session: Any = None,
    actor_user_id: int | None = None,
    source: str = "dialogue",
) -> SendResult:
    if enqueued_at is not None and is_pending_trigger_expired(enqueued_at):
        trace_event(
            "outbound",
            label,
            status="skipped",
            output={"sent": False, "reason": "trigger_expired"},
            detail="触发消息在队列/群锁等待期间过期，取消用户可见发送",
        )
        dbg(f"群 {group_id} {label}前触发已过期,跳过发送: message_id={message_id}")
        return SendResult(
            sent=False,
            message_id=None,
            normalized_text="",
            segment_types=(),
            outcome="expired",
            delivery_state=DELIVERY_CONFIRMED_FAILURE,
        )
    prepared = prepare_text_message(message) if isinstance(message, str) else message
    try:
        return await send_prepared_outbound(
            bot,
            group_id,
            prepared,
            session=session,
            actor_user_id=actor_user_id,
            source=source,
        )
    except Exception:  # noqa: BLE001
        logger.warning("群聊 Agent 发送群消息失败: %s", group_id)
        dbg_exc(f"群 {group_id} {label}失败")
        return SendResult(
            sent=False,
            message_id=None,
            normalized_text=prepared.normalized_text,
            segment_types=(),
            outcome="send_failed",
            delivery_state=DELIVERY_CONFIRMED_FAILURE,
        )


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


__all__ = [
    "contains_word",
    "current_turn_focus_ids",
    "deterministic_reply",
    "is_recent_duplicate",
    "persist_bot_reply",
    "send_group_text",
    "send_unless_expired",
]
