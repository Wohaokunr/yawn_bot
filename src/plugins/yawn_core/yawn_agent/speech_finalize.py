"""SpeechPlan finalization and post-send state updates for dialogue."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from nonebot.adapters.onebot.v11 import Bot

from ..data_models.group_agent_config import GroupAgentConfig
from .context import now_beijing
from .execution_trace import trace_event
from .log import dbg, dbg_exc
from .message_parser import NormalizedMessage
from .outbound import PreparedOutboundMessage, SendResult, prepare_speech_plan
from .persona import persona_behavior
from .speech import SpeechPlan
from .topic_state import TOPIC_ACTION_CLOSE, TOPIC_ACTION_SHIFT

SendFunc = Callable[..., Awaitable[SendResult]]
PersistFunc = Callable[..., Awaitable[None]]
MarkFunc = Callable[..., Any]
DuplicateFunc = Callable[[object, str, str, Any], bool]


def apply_speech_topic(config: GroupAgentConfig, plan: SpeechPlan) -> str | None:
    current = str(config.active_topic or "").strip() or None
    next_topic = current
    if plan.topic_action == TOPIC_ACTION_CLOSE:
        next_topic = None
    elif plan.topic_action == TOPIC_ACTION_SHIFT and plan.topic:
        next_topic = plan.topic[:240]
    elif plan.topic and not current:
        next_topic = plan.topic[:240]
    if next_topic != current:
        config.context_epoch += 1
        config.active_topic = next_topic
        dbg(
            f"群 {config.group_id} 话题状态变更: epoch={config.context_epoch} "
            f"action={plan.topic_action} topic={next_topic!r}"
        )
    return next_topic


async def finalize_reply(  # noqa: PLR0913
    bot: Bot,
    group_id: int,
    config: GroupAgentConfig,
    session: Any,
    normalized: NormalizedMessage,
    content: SpeechPlan | PreparedOutboundMessage,
    user_prompt: str,
    enqueued_at: float | None,
    message_id: Any,
    *,
    send_func: SendFunc,
    persist_func: PersistFunc,
    mark_func: MarkFunc,
    duplicate_func: DuplicateFunc,
    emotion_state: object = None,
) -> None:
    if isinstance(content, SpeechPlan):
        recent_speech = tuple(
            str(item.get("text") or "")
            for item in (config.recent_response_fingerprints or [])
            if isinstance(item, dict) and item.get("text")
        )[-4:]
        prepared = await prepare_speech_plan(
            content,
            session=session,
            group_id=group_id,
            actor_user_id=None,
            speech_user_text=normalized.plain_text,
            recent_speech=recent_speech,
            trace_context={"emotion": emotion_state},
        )
        speech_plan = content
    else:
        prepared = content
        speech_plan = None
    reply_text = prepared.normalized_text
    short_conversation_enabled = bool(config.short_conversation_enabled)
    max_followup_bot_turns = (
        persona_behavior(config).max_followup_bot_turns
        if short_conversation_enabled
        else 1
    )
    fingerprint_source = reply_text or json.dumps(
        list(prepared.segment_records), ensure_ascii=False, sort_keys=True
    )
    input_fingerprint = hashlib.sha256(user_prompt.casefold().encode("utf-8")).hexdigest()
    response_fingerprint = hashlib.sha256(
        fingerprint_source.casefold().encode("utf-8")
    ).hexdigest()
    now = now_beijing()
    recent = list(config.recent_response_fingerprints or [])
    duplicate = any(
        duplicate_func(item, input_fingerprint, response_fingerprint, now)
        for item in recent
    )
    if duplicate:
        trace_event(
            "outbound",
            "重复回复抑制",
            status="skipped",
            output={"sent": False},
            detail="与近 10 分钟同一输入/回复指纹重复",
        )
        dbg(f"群 {group_id} 回复与近 10 分钟内重复,抑制发送: {reply_text!r}")
        return
    sent = await send_func(
        bot,
        group_id,
        prepared,
        enqueued_at,
        label="正文发送",
        message_id=message_id,
        session=session,
        actor_user_id=None,
        source="dialogue",
    )
    if not sent.ends_turn:
        dbg(f"群 {group_id} 回复确认未发送(触发过期或明确失败),放弃本轮状态更新")
        return
    next_active_topic = (
        apply_speech_topic(config, speech_plan)
        if speech_plan is not None
        else str(config.active_topic or "").strip() or None
    )

    try:
        if sent.sent:
            await persist_func(
                session,
                int(bot.self_id),
                group_id,
                sent.message_id,
                sent.normalized_text,
                int(config.raw_retention_days),
                segments=sent.segments,
                reply_chain=sent.reply_chain,
                forward_tree=sent.forward_tree,
                media_refs=sent.media_refs,
            )
        else:
            dbg(f"群 {group_id} 回复投递状态未知,按可能已送达推进冷却/去重但不写消息历史")
        recent.append(
            {
                "input": input_fingerprint,
                "response": response_fingerprint,
                "text": reply_text[:500],
                "at": now.isoformat(),
            }
        )
        config.recent_response_fingerprints = recent[-8:]
        config.last_response_fingerprint = response_fingerprint
        config.last_response_input_fingerprint = input_fingerprint
        config.last_response_at = now
        config.last_agent_at = now
        await session.commit()
    except Exception as exc:  # noqa: BLE001
        trace_event(
            "state",
            "回复后状态提交",
            status="degraded",
            output={
                "rolled_back": True,
                "delivery_state": sent.delivery_state,
                "error_type": type(exc).__name__,
            },
            detail="消息已结束投递流程，但本地消息历史/去重/冷却状态写入失败",
        )
        dbg_exc(f"群 {group_id} 回复后状态提交失败,已回滚")
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            dbg_exc(f"群 {group_id} 回复后状态回滚失败(忽略)")
    else:
        trace_event(
            "state",
            "回复后状态提交",
            output={
                "recent_fingerprints": len(recent[-8:]),
                "context_epoch": config.context_epoch,
                "delivery_state": sent.delivery_state,
                "topic": next_active_topic,
                "topic_action": speech_plan.topic_action if speech_plan else "compat",
            },
        )
        dbg(f"群 {group_id} 回复后状态已提交(指纹记录 {len(recent[-8:])} 条)")

    if short_conversation_enabled:
        try:
            mark_func(
                int(bot.self_id),
                group_id,
                topic=next_active_topic,
                source="dialogue",
                max_bot_turns=max_followup_bot_turns,
            )
        except Exception as exc:  # noqa: BLE001
            trace_event(
                "state",
                "短会话状态推进",
                status="degraded",
                output={"error_type": type(exc).__name__},
                detail="正文已经结束投递流程，但短会话内存状态推进失败",
            )
            dbg_exc(f"群 {group_id} 短会话状态推进失败(忽略)")


__all__ = ["apply_speech_topic", "finalize_reply"]
