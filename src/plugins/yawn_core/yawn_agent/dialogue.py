# ruff: noqa: E501,F401,I001,TID252,PLR0911,PLR0912,PLR0913,PLR0915,PLR0917,PLR2004,C901,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100,SIM117
"""群聊 Agent 对话主流程：上下文加载、多模态降级、LLM 工具循环与回复收尾。"""

import asyncio
import hashlib
import json
import os
import re
import time
from collections import OrderedDict
from contextvars import ContextVar
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot_plugin_orm import get_session
from sqlalchemy import case, exists, func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from ..data_models.agent_memory import AgentMemory, AgentPrivacy, AgentRelation
from ..data_models.bot_group import BotGroup
from ..data_models.group_agent_config import GroupAgentConfig
from ..data_models.group_agent_message import GroupAgentMessage
from ..data_models.user_group import UserGroup
from ..llm import (
    LLMMultimodalUnsupportedError,
    complete,
    complete_with_tools_result,
    resolve_llm_request,
    vision_model_configured,
)
from ..metrics import (
    record_agent_context_db_queries,
    record_agent_phase,
    record_agent_provider_cache_tokens,
    record_agent_tool_rounds,
)
from .capabilities import (
    get_segment_capabilities,
    probe_group_capabilities,
    user_can_manage_group,
)
from .collector import group_lock, is_pending_trigger_expired
from .config_store import agent_runtime_enabled, get_or_create_config
from .conversation import mark_bot_reply
from .context import (
    ActivitySnapshot,
    CurrentTurn,
    build_context,
    build_current_turn,
    now_beijing,
    trim_context_messages,
)
from .context_history import (
    bot_message_meta as _bot_message_meta,
    history_message_meta as _history_message_meta,
    history_message_payload as _history_message_payload,
    select_context_messages,
    select_context_messages_only as _select_context_messages,
)
from .context_budget import pack_context
from .context_repository import AgentContextRepository
from .context_loader import activity_window_counts, load_context
from .emotion import emotion_context_state
from .execution_trace import (
    begin_execution_trace,
    bind_execution_trace,
    finish_execution_trace,
    reset_execution_trace,
    trace_event,
)
from .log import dbg, dbg_exc
from .media import prepare_image_inputs, store_caption
from .memory import effective_relation_confidence, rank_memories
from .message_parser import NormalizedMessage
from .outbound import (
    DELIVERY_CONFIRMED_FAILURE,
    PreparedOutboundMessage,
    SendResult,
    extract_message_id as extract_outbound_message_id,
    prepare_text_message,
    send_prepared_outbound,
)
from .persona import persona_behavior, persona_editor_profile, resolve_persona
from .prompt import (
    build_messages,
    prompt_cache_key,
    render_current_turn,
    stable_context_key,
)
from .tools import (
    MAX_TOOL_ROUNDS,
    build_tool_schemas,
    dialogue_tool_round_limit,
    execute_tool_with_meta,
    select_dialogue_message_segment_types,
    select_dialogue_tool_names,
)
from .tool_execution import ToolExecutionContext
from .turn_runtime import TOOL_ROUND_COUNT as _TOOL_ROUND_COUNT, TurnRuntimeHooks, run_dialogue_turn
from .turn_fallbacks import (
    FALLBACK_CURSOR_LIMIT as _FALLBACK_CURSOR_LIMIT,
    FALLBACK_NOTICES as _FALLBACK_NOTICES,
    WAIT_NOTICE as _WAIT_NOTICE,
    WAIT_NOTICE_DEFAULT_DELAY as _WAIT_NOTICE_DEFAULT_DELAY,
    WAIT_NOTICE_TASK as _WAIT_NOTICE_TASK,
    WEEKDAY_NAMES as _WEEKDAY_NAMES,
    _FALLBACK_CURSOR,
    cancel_wait_notice as cancel_fallback_wait_notice,
    contains_word as fallback_contains_word,
    deterministic_reply as deterministic_fallback_reply,
    fallback_notice as next_fallback_notice,
    start_wait_notice as start_fallback_wait_notice,
    wait_notice_delay as fallback_wait_notice_delay,
)

_TURN_END_NOTICE = "这个话题我先记下了，稍后再继续聊～"
_MEMORY_CONTEXT_CHAR_BUDGET = 6_000
_MEMORY_CONTEXT_LIMIT = 24
_TURN_PROVIDER_CACHE_USAGE: ContextVar[tuple[int, int]] = ContextVar(
    "agent_dialogue_provider_cache_usage", default=(0, 0)
)
_VISION_SYSTEM_PROMPT = (
    "你是图片识别器。只描述图片中可见且与用户问题相关的事实，"
    "不猜测身份、隐私或图片外的信息。"
)


def _accumulate_turn_usage(total: dict[str, int], result: Any) -> dict[str, Any]:
    """累计一次用户回合内多次 LLM 请求的真实 token 用量。"""

    total["rounds"] = total.get("rounds", 0) + 1
    fields = (
        ("prompt_tokens", "input"),
        ("completion_tokens", "output"),
        ("cached_tokens", "cached"),
        ("cache_miss_tokens", "cache_miss"),
    )
    current: dict[str, int | None] = {}
    try:
        from ..metrics import record_ai_tokens

        for field, source in fields:
            raw = getattr(result, field, None)
            value = int(raw) if isinstance(raw, int) and raw >= 0 else None
            current[field] = value
            if value is None:
                continue
            total[field] = total.get(field, 0) + value
            if value > 0:
                record_ai_tokens("agent_dialogue_turn", source, value)
    except Exception:  # noqa: BLE001
        dbg_exc("累计 Agent 回合 token 指标失败(忽略)")
    _TURN_PROVIDER_CACHE_USAGE.set(
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


def _trace_prompt_shape(messages: list[dict[str, Any]]) -> dict[str, Any]:
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


def _visible_tool_send_ends_turn(result: dict[str, Any]) -> bool:
    """用户可见发送一旦成功，本轮必须结束，禁止再追加最终纯文本。"""

    return result.get("sent") is True


async def _commit_tool_batch(
    session: Any,
    config: GroupAgentConfig,
    group_id: int,
    round_index: int,
    tool_names: list[str],
    *,
    immediate: bool = False,
) -> bool:
    """提交一批 Tool 产生的状态；常规路径每个模型轮次最多调用一次。"""

    try:
        await session.commit()
    except SQLAlchemyError:
        trace_event(
            "state",
            "高权限工具状态提交" if immediate else "工具批次状态提交",
            status="failed",
            detail="数据库提交失败并已回滚",
            output={"tools": tool_names},
            round_index=round_index,
        )
        dbg_exc(f"群 {group_id} 工具批次状态提交失败,已回滚")
        await session.rollback()
        return False
    trace_event(
        "state",
        "高权限工具状态提交" if immediate else "工具批次状态提交",
        output={"tools": tool_names, "count": len(tool_names)},
        round_index=round_index,
    )
    # commit 会过期 ORM 对象；只在真正提交后刷新一次，而不是每 Tool 刷新。
    await session.refresh(config)
    return True


def _extract_message_id(result: Any) -> int | None:
    """OneBot 实现对 send_group_msg 返回值不统一：dict、对象或裸 int；0 视为缺失。"""

    return extract_outbound_message_id(result)


async def _send_group_text(
    bot: Bot, group_id: int, text: str
) -> tuple[bool, int | None]:
    """返回 (是否发出, message_id)；message_id 缺失不视为发送失败。"""

    try:
        prepared = prepare_text_message(text)
        result = await send_prepared_outbound(bot, group_id, prepared)
    except Exception:  # noqa: BLE001
        logger.warning("群聊 Agent 发送群消息失败: %s", group_id)
        dbg_exc(f"群 {group_id} 发送群消息失败 text={text!r}")
        return False, None
    dbg(f"群 {group_id} 发送群消息成功 text={text!r}")
    return result.ends_turn, result.message_id


def contains_word(text: str, word: str) -> bool:
    return fallback_contains_word(text, word)



def _current_turn_focus_ids(
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


def _is_recent_duplicate(
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


def _deterministic_reply(text: str) -> str | None:
    return deterministic_fallback_reply(text, now=now_beijing())



def _fallback_notice(group_id: int) -> str:
    return next_fallback_notice(group_id)



def _wait_notice_delay() -> float:
    return fallback_wait_notice_delay()



def _cancel_wait_notice() -> None:
    cancel_fallback_wait_notice()



def _start_wait_notice(
    bot: Bot,
    group_id: int,
    enqueued_at: float | None,
    message_id: Any,
) -> None:
    start_fallback_wait_notice(
        bot,
        group_id,
        enqueued_at,
        message_id,
        send_unless_expired=_send_unless_expired,
        debug=dbg,
        debug_exception=dbg_exc,
    )



async def _send_unless_expired(
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
    cancel_wait_notice: bool = True,
) -> SendResult:
    """过期触发不发送；普通文本与复合 Message 统一走 sender。"""

    if cancel_wait_notice:
        # 正式输出即将出现，等待提示不该再冒出来。
        _cancel_wait_notice()
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
    """把 bot 自己发出的消息以 role="bot" 落库，让后续上下文记得自己说过什么。

    message_id 缺失或撞 (bot_id, message_id) 唯一键时跳过；随调用方事务提交。
    """

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


_activity_window_counts = activity_window_counts


async def _load_context(
    session: Any,
    group_id: int,
    config: GroupAgentConfig,
    bot_id: int | None = None,
    *,
    focus_user_ids: Sequence[int] | None = None,
    query_text: str | None = None,
    compact_history: bool = False,
    message_cutoff: datetime | None = None,
    include_active_profiles: bool = False,
    exclude_message_id: int | None = None,
    reference_at: datetime | None = None,
    selection_trace: list[dict[str, Any]] | None = None,
    budget_trace: list[dict[str, Any]] | None = None,
    context_model: str | None = None,
    completion_reserve: int = 2048,
    context_token_limit: int | None = None,
) -> dict[str, Any]:
    """兼容入口：实际数据库上下文装箱由 context_loader 负责。"""

    return await load_context(
        session,
        group_id,
        config,
        bot_id,
        focus_user_ids=focus_user_ids,
        query_text=query_text,
        compact_history=compact_history,
        message_cutoff=message_cutoff,
        include_active_profiles=include_active_profiles,
        exclude_message_id=exclude_message_id,
        reference_at=reference_at,
        selection_trace=selection_trace,
        budget_trace=budget_trace,
        context_model=context_model,
        completion_reserve=completion_reserve,
        context_token_limit=context_token_limit,
        _record_db_queries=record_agent_context_db_queries,
    )




async def _caption_single_image(
    group_id: int, normalized: NormalizedMessage, block: dict[str, Any]
) -> str | None:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _VISION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [{"type": "text", "text": normalized.prompt_text()}, block],
        },
    ]
    result = await complete(  # pyright: ignore[reportArgumentType]
        messages,  # pyright: ignore[reportArgumentType]
        task="agent_image",
        max_tokens=500,
        timeout=30,
    )  # pyright: ignore[reportArgumentType]
    return (result or "").strip() or None


async def _describe_images(
    group_id: int,
    normalized: NormalizedMessage,
    blocks: list[dict[str, Any]],
    session: Any,
    config: GroupAgentConfig,
    cached: list[tuple[str, str]],
    digests: list[str],
) -> str:
    """视觉转述降级路径：逐图独立生成并缓存 caption。

    多图必须逐图转述，否则单一 caption 写进每个 digest 的缓存后，
    任一单图命中缓存都会拿到混合描述。URL 透传图没有 digest、
    不可缓存，但仍参与转述；data: 前缀的 block 与 digests 按序对齐。
    """

    has_vision_model = vision_model_configured()
    if not blocks:
        dbg(f"群 {group_id} 跳过图片识别: 无可用图片 block")
        return "[图片未识别：没有可用的图片数据]"
    caption_by_digest = dict(cached)
    digest_iter = iter(digests)
    parts: list[str] = []
    for block in blocks:
        url = str(((block.get("image_url") or {}).get("url")) or "")
        digest = next(digest_iter, None) if url.startswith("data:") else None
        caption = caption_by_digest.get(digest) if digest else None
        if caption:
            parts.append(f"[图片转述（缓存）] {caption}")
            continue
        if not has_vision_model:
            parts.append("[图片未识别：当前未配置可用的识图模型]")
            continue
        caption = await _caption_single_image(group_id, normalized, block)
        if caption is None:
            dbg(f"群 {group_id} 视觉模型返回空结果 digest={digest}")
            parts.append("[图片未识别：视觉模型没有返回结果]")
            continue
        dbg(f"群 {group_id} 视觉模型识别完成 digest={digest} caption={caption!r}")
        if digest:
            await store_caption(
                session,
                group_id,
                digest,
                caption,
                resolve_llm_request("agent_image").model,
                cache_enabled=bool(config.media_cache_enabled),
            )
        parts.append(f"[图片转述] {caption[:2000]}")
    return "\n".join(parts)


async def _prepare_media_prompt(
    group_id: int,
    normalized: NormalizedMessage,
    session: Any,
    config: GroupAgentConfig,
    media_blocks: list[dict[str, Any]],
    cached_captions: list[tuple[str, str]],
    media_digests: list[str],
) -> tuple[str, list[dict[str, Any]]]:
    """多模态关闭时改为视觉转述注入 prompt；否则透传媒体 block。"""

    mode = resolve_llm_request("agent_dialogue").multimodal
    dbg(f"群 {group_id} 多模态模式={mode!r}")
    user_prompt = normalized.prompt_text()
    if media_blocks and mode == "unsupported":
        dbg(f"群 {group_id} 多模态关闭,改走视觉转述注入 prompt")
        user_prompt = f"{user_prompt}\n{await _describe_images(group_id, normalized, media_blocks, session, config, cached_captions, media_digests)}"
        return user_prompt, []
    if cached_captions:
        dbg(f"群 {group_id} 追加缓存字幕 {len(cached_captions)} 条到 prompt")
        user_prompt = f"{user_prompt}\n" + "\n".join(
            f"[图片转述（缓存）] {caption}" for _digest, caption in cached_captions
        )
    return user_prompt, media_blocks


async def _finalize_reply(
    bot: Bot,
    group_id: int,
    config: GroupAgentConfig,
    session: Any,
    normalized: NormalizedMessage,
    content: str | PreparedOutboundMessage,
    user_prompt: str,
    enqueued_at: float | None,
    message_id: Any,
) -> None:
    """最终回复分支：去重检查、发送、指纹记录、话题推进与状态提交。"""

    prepared = prepare_text_message(content) if isinstance(content, str) else content
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
    input_fingerprint = hashlib.sha256(
        user_prompt.casefold().encode("utf-8")
    ).hexdigest()
    response_fingerprint = hashlib.sha256(
        fingerprint_source.casefold().encode("utf-8")
    ).hexdigest()
    now = now_beijing()
    recent = list(config.recent_response_fingerprints or [])
    duplicate = any(
        _is_recent_duplicate(item, input_fingerprint, response_fingerprint, now)
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
    sent = await _send_unless_expired(
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
    next_active_topic = config.active_topic
    topic_changed = bool(
        normalized.plain_text
        and normalized.plain_text[:240] != config.active_topic
    )
    if topic_changed:
        next_active_topic = normalized.plain_text[:240]

    # 发送已经是不可逆外部副作用。之后的消息历史、去重、冷却等本地状态
    # 只能降级失败，不能再把整轮标记成“执行失败”，否则 WebUI 会出现
    # “OneBot 已确认成功，但 Trace 最终失败”的假阴性。
    try:
        if sent.sent:
            # 只有确认成功才写入 Bot 消息历史；unknown 不能伪造一条确定存在的 QQ 消息。
            await persist_bot_reply(
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
        if topic_changed:
            config.context_epoch += 1
            config.active_topic = next_active_topic
            dbg(
                f"群 {group_id} 话题切换: epoch={config.context_epoch} "
                f"topic={config.active_topic!r}"
            )
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
        # 消息已经发出或投递结果未知；状态丢失只影响本地上下文/重复抑制，不能上抛。
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
            },
        )
        dbg(f"群 {group_id} 回复后状态已提交(指纹记录 {len(recent[-8:])} 条)")

    if short_conversation_enabled:
        try:
            mark_bot_reply(
                int(bot.self_id),
                group_id,
                topic=str(next_active_topic or normalized.plain_text or ""),
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


async def _process_group_message(
    bot: Bot,
    event: GroupMessageEvent,
    normalized: NormalizedMessage,
    *,
    enqueued_at: float | None = None,
) -> None:
    """兼容入口：实际单回合 LLM/Tool 编排由 turn_runtime 执行。"""

    hooks = TurnRuntimeHooks(
        load_context=_load_context,
        current_turn_focus_ids=_current_turn_focus_ids,
        prepare_media_prompt=_prepare_media_prompt,
        trace_prompt_shape=_trace_prompt_shape,
        accumulate_turn_usage=_accumulate_turn_usage,
        describe_images=_describe_images,
        deterministic_reply=_deterministic_reply,
        fallback_notice=_fallback_notice,
        send_unless_expired=_send_unless_expired,
        finalize_reply=_finalize_reply,
        cancel_wait_notice=_cancel_wait_notice,
        commit_tool_batch=_commit_tool_batch,
        visible_tool_send_ends_turn=_visible_tool_send_ends_turn,
        persist_bot_reply=persist_bot_reply,
        extract_message_id=_extract_message_id,
        start_wait_notice=_start_wait_notice,
    )
    await run_dialogue_turn(
        bot,
        event,
        normalized,
        hooks=hooks,
        enqueued_at=enqueued_at,
    )




async def process_group_message(
    bot: Bot,
    event: GroupMessageEvent,
    normalized: NormalizedMessage,
    *,
    enqueued_at: float | None = None,
) -> None:
    """处理明确触发并记录低基数的端到端回合指标。"""

    started = time.monotonic()
    outcome = "completed"
    trace = begin_execution_trace(
        int(event.group_id),
        mode="dialogue",
        source="runtime",
        trigger_source=normalized.trigger_source or "explicit_call",
        actor_user_id=int(event.get_user_id()),
        message_id=(
            int(event.message_id)
            if getattr(event, "message_id", None) is not None
            else None
        ),
    )
    token = bind_execution_trace(trace)
    tool_round_token = _TOOL_ROUND_COUNT.set(0)
    provider_cache_token = _TURN_PROVIDER_CACHE_USAGE.set((0, 0))
    for stage in normalized.parse_trace:
        if not isinstance(stage, dict):
            continue
        trace_event(
            "parse",
            str(stage.get("label") or "消息解析"),
            output=(
                stage.get("output")
                if isinstance(stage.get("output"), dict)
                else {}
            ),
            duration_ms=(
                float(stage.get("duration_ms") or 0.0)
                if stage.get("duration_ms") is not None
                else None
            ),
        )
    trace_event(
        "intake",
        "消息归一化完成",
        output={
            "trigger_source": normalized.trigger_source or "explicit_call",
            "trigger_signals": dict(normalized.trigger_signals),
            "text_chars": len(normalized.plain_text),
            "text_preview": normalized.plain_text[:240],
            "segment_types": [item.type for item in normalized.segments],
            "media": [
                {
                    "type": item.get("type"),
                    "source": item.get("source", "current"),
                }
                for item in normalized.media_refs
            ],
            "reply_depth": len(normalized.reply_chain),
            "forward_nodes": len(normalized.forward_tree),
            "mentions": normalized.mentions,
            "truncated": normalized.truncated,
            "queue_wait_ms": (
                round(max(started - enqueued_at, 0.0) * 1000, 1)
                if enqueued_at is not None
                else None
            ),
        },
    )
    try:
        await _process_group_message(
            bot,
            event,
            normalized,
            enqueued_at=enqueued_at,
        )
    except BaseException as exc:
        outcome = "error"
        trace_event(
            "turn",
            "执行异常",
            status="failed",
            output={
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:320],
            },
            detail="未处理异常终止本轮",
        )
        raise
    finally:
        # 兜底：任何退出路径都不留下待发的等待提示。
        _cancel_wait_notice()
        tool_rounds = _TOOL_ROUND_COUNT.get()
        record_agent_tool_rounds(tool_rounds, "dialogue")
        cached_tokens, cache_miss_tokens = _TURN_PROVIDER_CACHE_USAGE.get()
        if tool_rounds > 0:
            record_agent_provider_cache_tokens(
                cached=cached_tokens,
                cache_miss=cache_miss_tokens,
            )
        _TOOL_ROUND_COUNT.reset(tool_round_token)
        _TURN_PROVIDER_CACHE_USAGE.reset(provider_cache_token)
        trace_event(
            "turn",
            "回合结束",
            status="failed" if outcome == "error" else "success",
            output={"outcome": outcome},
            duration_ms=max(time.monotonic() - started, 0.0) * 1000,
        )
        finish_execution_trace(trace, outcome=outcome)
        reset_execution_trace(token)
        try:
            from ..metrics import record_agent_turn

            record_agent_turn(
                "dialogue",
                outcome,
                max(time.monotonic() - started, 0.0),
                queue_wait_seconds=(
                    max(started - enqueued_at, 0.0)
                    if enqueued_at is not None
                    else None
                ),
            )
        except Exception:  # noqa: BLE001
            dbg_exc("Agent 对话回合指标上报失败(忽略)")
