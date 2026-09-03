# ruff: noqa: PLR0913, TC003, TID252
"""Small runtime helpers kept out of dialogue orchestration."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from nonebot import logger
from sqlalchemy.exc import SQLAlchemyError

from .. import metrics
from .execution_trace import trace_event
from .log import dbg_exc
from .outbound import (
    DELIVERY_CONFIRMED_FAILURE,
    PreparedOutboundMessage,
    SendResult,
    extract_message_id,
    prepare_text_message,
)


async def resolve_turn_tool_capabilities(
    bot: Any,
    group_id: int,
    actor_user_id: int,
    tool_intent_text: str,
    *,
    has_reply: bool,
    has_mentions: bool,
    has_media: bool,
) -> tuple[Any, bool, frozenset[str], str]:
    """Build the minimal progressive-disclosure bootstrap capability bundle."""

    from .capabilities import local_group_capabilities
    from .tools import select_dialogue_tool_names

    del group_id, actor_user_id
    baseline = local_group_capabilities(bot)
    bootstrap_names = select_dialogue_tool_names(
        tool_intent_text,
        has_reply=has_reply,
        has_mentions=has_mentions,
        has_media=has_media,
        allow_admin_tools=False,
    )
    return baseline, False, bootstrap_names, "progressive_bootstrap"


async def commit_tool_batch(
    session: Any,
    config: Any,
    group_id: int,
    round_index: int,
    tool_names: list[str],
    *,
    immediate: bool = False,
) -> bool:
    """Commit one regular Tool batch, or one explicitly immediate side effect."""

    try:
        await session.commit()
    except SQLAlchemyError:
        trace_event(
            "state",
            "工具轮状态提交",
            status="failed",
            detail="数据库提交失败并已回滚",
            round_index=round_index,
        )
        dbg_exc(f"群 {group_id} 工具轮状态提交失败(已回滚)")
        await session.rollback()
        return False
    await session.refresh(config)
    trace_event(
        "state",
        "工具轮状态提交",
        output={
            "tools": list(dict.fromkeys(tool_names)),
            "immediate": immediate,
        },
        round_index=round_index,
    )
    return True


def accumulate_turn_usage(
    total: dict[str, int],
    result: Any,
    *,
    cache_usage: ContextVar[tuple[int, int]] | None = None,
) -> dict[str, Any]:
    """Aggregate provider-reported token usage across one dialogue turn."""

    total["rounds"] = total.get("rounds", 0) + 1
    fields = (
        ("prompt_tokens", "input"),
        ("completion_tokens", "output"),
        ("cached_tokens", "cached"),
        ("cache_miss_tokens", "cache_miss"),
    )
    current: dict[str, int | None] = {}
    try:
        for field, source in fields:
            raw = getattr(result, field, None)
            value = int(raw) if isinstance(raw, int) and raw >= 0 else None
            current[field] = value
            if value is None:
                continue
            total[field] = total.get(field, 0) + value
            if value > 0:
                metrics.record_ai_tokens("agent_dialogue_turn", source, value)
    except Exception:  # noqa: BLE001
        dbg_exc("累计 Agent 回合 token 指标失败(忽略)")
    if cache_usage is not None:
        cache_usage.set(
            (
                total.get("cached_tokens", 0),
                total.get("cache_miss_tokens", 0),
            )
        )
    return {
        "request": current,
        "turn": {
            "rounds": total.get("rounds", 0),
            **{field: total.get(field, 0) for field, _source in fields},
        },
    }


def trace_prompt_shape(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Return useful prompt diagnostics without retaining the full prompt."""

    roles: dict[str, int] = {}
    text_chars = 0
    media_blocks = 0
    tool_call_messages = 0
    for message in messages:
        role = str(message.get("role") or "unknown")
        roles[role] = roles.get(role, 0) + 1
        if message.get("tool_calls"):
            tool_call_messages += 1
        content = message.get("content")
        if isinstance(content, str):
            text_chars += len(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text_chars += len(str(block.get("text") or ""))
                elif str(block.get("type") or "").startswith("image"):
                    media_blocks += 1
    return {
        "roles": roles,
        "text_chars": text_chars,
        "media_blocks": media_blocks,
        "tool_call_messages": tool_call_messages,
    }


def visible_tool_send_ends_turn(result: dict[str, Any]) -> bool:
    return result.get("sent") is True


async def send_unless_expired(
    bot: Any,
    group_id: int,
    message: str | PreparedOutboundMessage,
    enqueued_at: float | None,
    *,
    label: str,
    sender: Any,
    expiry_checker: Any,
    cancel_wait: Any = None,
    cancel_wait_notice: bool = True,
    message_id: Any = None,
    session: Any = None,
    actor_user_id: int | None = None,
    source: str = "dialogue",
) -> SendResult:
    """Send one visible message with trigger-expiry and wait-notice guards."""

    del message_id
    if cancel_wait_notice and cancel_wait is not None:
        cancel_wait()
    if enqueued_at is not None and expiry_checker(enqueued_at):
        trace_event(
            "outbound",
            label,
            status="skipped",
            output={"sent": False, "reason": "trigger_expired"},
            detail="触发消息在队列/群锁等待期间过期，取消用户可见发送",
        )
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
        return await sender(
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


__all__ = [
    "accumulate_turn_usage",
    "commit_tool_batch",
    "extract_message_id",
    "resolve_turn_tool_capabilities",
    "send_unless_expired",
    "trace_prompt_shape",
    "visible_tool_send_ends_turn",
]
