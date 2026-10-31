# ruff: noqa: E501,TID252,TC001,TC002,PLR0915,PLR0917,SIM114,C901,PLR0912
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
from .member_standing import reward_group_interaction
from .message_parser import NormalizedMessage
from .outbound import (
    PreparedOutboundMessage,
    PreparedSpeechParts,
    SendResult,
    prepare_speech_parts,
)
from .persona import persona_behavior
from .speech import SpeechPlan
from .speech_delivery import deliver_parts, persist_delivered, queued_trigger_check
from .speech_rhythm import single_rhythm
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
            f"群 {getattr(config, 'group_id', '?')} 话题状态变更: epoch={config.context_epoch} "
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
    actor_user_id: int | None = None,
) -> None:
    if isinstance(content, SpeechPlan):
        recent_speech = tuple(
            str(item.get("text") or "")
            for item in (config.recent_response_fingerprints or [])
            if isinstance(item, dict) and item.get("text")
        )[-4:]
        prepared = await prepare_speech_parts(
            content,
            session=session,
            group_id=group_id,
            actor_user_id=None,
            speech_user_text=normalized.plain_text,
            recent_speech=recent_speech,
            trace_context={"emotion": emotion_state},
            emotion=emotion_state,
        )
        speech_plan = content
    else:
        prepared = PreparedSpeechParts(
            (content,), single_rhythm(content.normalized_text, source="prepared")
        )
        speech_plan = None
    reply_text = prepared.normalized_text
    short_conversation_enabled = bool(config.short_conversation_enabled)
    max_followup_bot_turns = (
        persona_behavior(config).max_followup_bot_turns
        if short_conversation_enabled
        else 1
    )
    fingerprint_source = reply_text or json.dumps(
        [record for item in prepared.messages for record in item.segment_records],
        ensure_ascii=False,
        sort_keys=True,
    )
    input_fingerprint = hashlib.sha256(
        user_prompt.casefold().encode("utf-8")
    ).hexdigest()
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
        for item in prepared.messages:
            for path in item.temporary_files:
                path.unlink(missing_ok=True)
        return

    async def send_one(part: PreparedOutboundMessage, index: int) -> SendResult:
        # 只有第一条做触发过期检查：已经开口了就把话说完。
        return await send_func(
            bot,
            group_id,
            part,
            enqueued_at if index == 0 else None,
            label="正文发送" if index == 0 else f"正文分条 {index + 1}",
            message_id=message_id,
            session=session,
            actor_user_id=None,
            source="dialogue",
        )

    bot_id = int(bot.self_id)
    outcome = await deliver_parts(
        prepared,
        send_one=send_one,
        hurry=queued_trigger_check(group_id, bot_id),
        group_id=group_id,
    )
    sent = outcome.combined()
    if outcome.visible_text:
        reply_text = outcome.visible_text
    if not sent.ends_turn:
        dbg(f"群 {group_id} 回复确认未发送(触发过期或明确失败),放弃本轮状态更新")
        return
    next_active_topic = (
        apply_speech_topic(config, speech_plan)
        if speech_plan is not None
        else str(config.active_topic or "").strip() or None
    )

    try:
        if outcome.delivered:
            await persist_delivered(
                outcome,
                persist_func,
                session,
                bot_id,
                group_id,
                int(config.raw_retention_days),
            )
        else:
            dbg(
                f"群 {group_id} 回复投递状态未知,按可能已送达推进冷却/去重但不写消息历史"
            )
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
        if outcome.delivered:
            await reward_group_interaction(
                session,
                group_id=group_id,
                bot_id=bot_id,
                actor_user_id=actor_user_id,
                trigger_source=normalized.trigger_source,
                now=now,
            )
        # AsyncSession.commit() may expire ORM attributes. Snapshot values needed
        # by post-commit diagnostics before committing to avoid implicit IO /
        # MissingGreenlet when Trace serialization reads them synchronously.
        committed_context_epoch = config.context_epoch
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
                "context_epoch": committed_context_epoch,
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


async def record_tool_send(  # noqa: PLR0913
    session: Any,
    config: GroupAgentConfig,
    result: dict[str, Any],
    *,
    bot_id: int,
    group_id: int,
    current_turn: Any,
    context: dict[str, Any],
    persist_func: PersistFunc,
    mark_func: MarkFunc,
) -> None:
    """send_message/send_forward 成功后与正文发送一致地落库并推进状态。

    状态写入随本轮工具批次一起提交，这里不单独 commit。
    """

    from .outbound import extract_message_id
    from .persona import resolve_persona
    from .prompt import render_current_turn
    from .speech_runtime import build_runtime_speech_plan

    raw_result = result.get("result")
    payload = raw_result.get("outbound", {}) if isinstance(raw_result, dict) else {}
    if not isinstance(payload, dict):
        return

    text = str(payload.get("text") or "")
    parts = raw_result.get("outbound_parts") if isinstance(raw_result, dict) else None
    entries = (
        [item for item in parts if isinstance(item, dict)]
        if isinstance(parts, list) and parts
        else [payload]
    )
    for entry in entries:

        def _entry_list(key: str, source: dict[str, Any] = entry) -> list[Any]:
            value = source.get(key)
            return value if isinstance(value, list) else []

        await persist_func(
            session,
            bot_id,
            group_id,
            extract_message_id(entry.get("message_id")),
            str(entry.get("text") or ""),
            int(config.raw_retention_days),
            segments=_entry_list("segments"),
            reply_chain=_entry_list("reply_chain"),
            forward_tree=_entry_list("forward_tree"),
            media_refs=_entry_list("media_refs"),
        )
    now = now_beijing()
    fingerprint_source = text or json.dumps(
        {
            "segments": payload.get("segments", []),
            "forward_tree": payload.get("forward_tree", []),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    response_fingerprint = hashlib.sha256(
        fingerprint_source.casefold().encode("utf-8")
    ).hexdigest()
    input_fingerprint = hashlib.sha256(
        render_current_turn(current_turn).casefold().encode("utf-8")
    ).hexdigest()
    recent = list(config.recent_response_fingerprints or [])
    recent.append(
        {
            "input": input_fingerprint,
            "response": response_fingerprint,
            "text": text[:500],
            "at": now.isoformat(),
        }
    )
    config.recent_response_fingerprints = recent[-8:]
    config.last_response_fingerprint = response_fingerprint
    config.last_response_input_fingerprint = input_fingerprint
    config.last_response_at = now
    config.last_agent_at = now
    speech_plan = build_runtime_speech_plan(
        text=text,
        persona=resolve_persona(config),
        current_turn=current_turn,
        context=context,
        source="dialogue",
    )
    topic = apply_speech_topic(config, speech_plan)
    if config.short_conversation_enabled:
        mark_func(
            bot_id,
            group_id,
            topic=topic,
            source="dialogue",
            max_bot_turns=persona_behavior(config).max_followup_bot_turns,
        )


__all__ = ["apply_speech_topic", "finalize_reply", "record_tool_send"]
