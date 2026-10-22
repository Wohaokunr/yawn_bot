# ruff: noqa: E501,F401,I001,TID252,PLR0911,PLR0912,PLR0913,PLR0915,PLR0917,PLR2004,C901,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100,SIM117
"""群聊 Agent 对话主流程：一轮明确触发从准备到收尾的编排。

一轮对话分三段：

1. ``_prepare_turn``：在群锁和独立 DB 会话里加载上下文、计算本轮工具与
   媒体输入、构建 Prompt，得到一个 ``_DialogueTurn``；
2. ``_run_turn``：有界的模型/工具循环，每一轮交给 ``_run_tool_round``；
3. ``_finalize_turn_reply``：最终正文经 SpeechPlan → outbound 发送，群里
   有人插话时自动引用触发消息，避免多人同时聊天时答非所问。

历史选择、上下文装箱、工具注册与执行、发送细节都在各自模块里；这里只
负责把它们按顺序串起来。
"""

import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot_plugin_orm import get_session
from sqlalchemy import func, select

from ..data_models.group_agent_config import GroupAgentConfig
from ..data_models.group_agent_message import GroupAgentMessage
from ..llm import (
    LLMMultimodalUnsupportedError,
    complete_with_tools_result,
    resolve_llm_request,
    vision_model_configured,
)
from ..metrics import (
    record_agent_context_db_queries,
    record_agent_provider_cache_tokens,
    record_agent_tool_discovery,
    record_agent_tool_rounds,
    record_agent_tool_selection,
)
from .capabilities import (
    BotGroupCapabilities,
    get_segment_capabilities,
    local_group_capabilities,
    probe_group_capabilities,
    user_can_manage_group,
)
from .collector import group_lock, is_pending_trigger_expired
from .config_store import agent_runtime_enabled, get_or_create_config
from .conversation import mark_bot_reply
from .context_loader import load_context as _load_context_impl
from .dialogue_support import (
    contains_word,
    current_turn_focus_ids as _current_turn_focus_ids,
    is_recent_duplicate as _is_recent_duplicate,
    persist_bot_reply,
)
from .dialogue_turn_support import (
    DiscoveryRoundGuard,
    semantic_query_text as _semantic_query_text,
    trace_intake as _trace_intake,
    trace_message_shape as _trace_message_shape,
)
from .dialogue_runtime_support import (
    accumulate_turn_usage as _accumulate_turn_usage_impl,
    abort_after_commit_failure as _abort_after_commit_failure,
    commit_tool_batch as _commit_tool_batch,
    extract_message_id as _extract_message_id,
    resolve_turn_tool_capabilities as _resolve_turn_tool_capabilities,
    send_unless_expired as _send_unless_expired_impl,
    trace_prompt_shape as _trace_prompt_shape,
    visible_tool_send_ends_turn as _visible_tool_send_ends_turn,
)
from .context import CurrentTurn, build_current_turn, now_beijing
from .context_history import (
    bot_message_meta as _bot_message_meta,
    history_message_meta as _history_message_meta,
    history_message_payload as _history_message_payload,
    select_context_messages_only as _select_context_messages,
)
from .execution_trace import (
    begin_execution_trace,
    bind_execution_trace,
    finish_execution_trace,
    reset_execution_trace,
    trace_event,
)
from .log import dbg, dbg_exc
from .media import MediaInput, get_cached_caption
from .media_caption import describe_images as _describe_images
from .media_context import (
    project_context_for_llm,
    project_tool_result_media,
    public_media_diagnostics,
    resolve_media_context,
)
from .message_parser import NormalizedMessage
from .outbound import (
    PreparedOutboundMessage,
    SendResult,
    send_prepared_outbound,
)
from .persona import persona_behavior, resolve_persona
from .speech import SpeechPlan
from .speech_finalize import (
    apply_speech_topic,
    finalize_reply as _speech_finalize_reply,
    record_tool_send as _speech_record_tool_send,
)
from .speech_runtime import build_runtime_speech_plan
from .tool_result_speech import build_speech_evidence
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
)
from .tool_execution import ToolExecutionContext
from .turn_fallbacks import (
    FALLBACK_CURSOR_LIMIT as _FALLBACK_CURSOR_LIMIT,
    FALLBACK_NOTICES as _FALLBACK_NOTICES,
    WAIT_NOTICE as _WAIT_NOTICE,
    WAIT_NOTICE_DEFAULT_DELAY as _WAIT_NOTICE_DEFAULT_DELAY,
    WAIT_NOTICE_TASK as _WAIT_NOTICE_TASK,
    WEEKDAY_NAMES as _WEEKDAY_NAMES,
    _FALLBACK_CURSOR,
    cancel_wait_notice as _cancel_wait_notice,
    deterministic_reply as _deterministic_fallback_reply,
    fallback_notice as _fallback_notice,
    start_wait_notice as _start_wait_notice_impl,
    wait_notice_delay as _wait_notice_delay,
)

_PROMPT_CACHE_KEYS: OrderedDict[str, None] = OrderedDict()
_PROMPT_CACHE_LIMIT = 256
_MAX_TURN_SECONDS = 120.0
_CONTEXT_TOKEN_LIMIT = 2400
_COMPLETION_RESERVE = 800
_FALLBACK_NOTICE = "现在有点忙，稍后再试～"
_TURN_END_NOTICE = "这个话题我先记下了，稍后再继续聊～"
_VISIBLE_SEND_TOOLS = frozenset({"send_message", "send_forward"})
_MEDIA_FAILED_STATUSES = frozenset(
    {"dropped_unavailable", "unavailable", "vision_failed"}
)
_MEDIA_DEGRADED_STATUSES = frozenset(
    {
        "caption_ready",
        "image_url_ready",
        "inline_file_ready",
        "dropped_unavailable",
        "unavailable",
        "vision_unsupported",
        "vision_failed",
    }
)
_TOOL_ROUND_COUNT: ContextVar[int] = ContextVar(
    "agent_dialogue_tool_round_count", default=0
)
_TURN_PROVIDER_CACHE_USAGE: ContextVar[tuple[int, int]] = ContextVar(
    "agent_dialogue_provider_cache_usage", default=(0, 0)
)


def _load_context(*args: Any, **kwargs: Any) -> Any:
    kwargs.setdefault("_record_db_queries", record_agent_context_db_queries)
    return _load_context_impl(*args, **kwargs)


def _deterministic_reply(text: str) -> str | None:
    return _deterministic_fallback_reply(text, now=now_beijing())


def _accumulate_turn_usage(total: dict[str, int], result: Any) -> dict[str, Any]:
    return _accumulate_turn_usage_impl(
        total, result, cache_usage=_TURN_PROVIDER_CACHE_USAGE
    )


async def _send_unless_expired(
    bot: Bot,
    group_id: int,
    message: str | PreparedOutboundMessage,
    enqueued_at: float | None,
    **kwargs: Any,
) -> SendResult:
    return await _send_unless_expired_impl(
        bot,
        group_id,
        message,
        enqueued_at,
        sender=send_prepared_outbound,
        expiry_checker=is_pending_trigger_expired,
        cancel_wait=_cancel_wait_notice,
        **kwargs,
    )


def _start_wait_notice(
    bot: Bot, group_id: int, enqueued_at: float | None, message_id: Any
) -> None:
    _start_wait_notice_impl(
        bot,
        group_id,
        enqueued_at,
        message_id,
        send_unless_expired=_send_unless_expired,
        debug=dbg,
        debug_exception=dbg_exc,
    )


def _media_trace_status(statuses: set[str], *, failed: frozenset[str]) -> str:
    if statuses and statuses <= failed:
        return "failed"
    if statuses.intersection(_MEDIA_DEGRADED_STATUSES):
        return "degraded"
    return "success"


async def _unresolved_media_note(
    group_id: int, session: Any, diagnostics: list[dict[str, Any]]
) -> str:
    """把无法交给模型的媒体诊断投影成一段事实说明。"""

    notes = await project_context_for_llm(
        [],
        task="agent_dialogue",
        group_id=group_id,
        session=session,
        unresolved_diagnostics=diagnostics,
    )
    return "\n".join(
        str(block.get("text") or "")
        for block in notes.content_blocks
        if block.get("type") == "text" and block.get("text")
    )


async def _prepare_media_prompt(
    group_id: int,
    normalized: NormalizedMessage,
    session: Any,
    config: GroupAgentConfig,
    media_inputs: list[MediaInput],
    cached_captions: list[tuple[str, str]],
    media_digests: list[str],
    *,
    diagnostics: list[dict[str, Any]] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Choose direct multimodal input or the dedicated image route deterministically."""

    unresolved_snapshot = [dict(item) for item in (diagnostics or [])]
    dialogue_request = resolve_llm_request("agent_dialogue")
    vision_request = resolve_llm_request("agent_image")
    mode = dialogue_request.multimodal
    dbg(f"群 {group_id} 多模态模式={mode!r}")
    user_prompt = _semantic_query_text(normalized)
    dedicated_vision = (
        bool(media_inputs)
        and vision_model_configured()
        and (
            mode == "unsupported"
            or (
                mode == "auto"
                and (
                    vision_request.provider != dialogue_request.provider
                    or vision_request.model != dialogue_request.model
                )
            )
        )
    )
    if dedicated_vision or (media_inputs and mode == "unsupported"):
        if dedicated_vision:
            dbg(
                f"群 {group_id} 图片优先走独立视觉路由: "
                f"dialogue={dialogue_request.provider}/{dialogue_request.model} "
                f"vision={vision_request.provider}/{vision_request.model}"
            )
        # 没有可用视觉模型时也给用户一个明确的降级事实，不把图片伪装成已处理。
        note_text = await _unresolved_media_note(group_id, session, unresolved_snapshot)
        described = await _describe_images(
            group_id,
            normalized,
            media_inputs,
            session,
            config,
            cached_captions,
            media_digests,
            diagnostics=diagnostics,
        )
        user_prompt = f"{user_prompt}\n{described}"
        if note_text:
            user_prompt = f"{user_prompt}\n{note_text}"
        return user_prompt, []
    if mode == "unsupported":
        note_text = await _unresolved_media_note(group_id, session, unresolved_snapshot)
        if note_text:
            user_prompt = f"{user_prompt}\n{note_text}"
        return user_prompt, []
    projection = await project_context_for_llm(
        media_inputs,
        task="agent_dialogue",
        group_id=group_id,
        session=session,
        cached_captions=dict(cached_captions),
        unresolved_diagnostics=unresolved_snapshot,
        diagnostics=diagnostics,
    )
    return user_prompt, projection.content_blocks


async def _finalize_reply(
    bot: Bot,
    group_id: int,
    config: GroupAgentConfig,
    session: Any,
    normalized: NormalizedMessage,
    content: str | PreparedOutboundMessage | SpeechPlan,
    user_prompt: str,
    enqueued_at: float | None,
    message_id: Any,
    *,
    context: dict[str, Any] | None = None,
    current_turn: CurrentTurn | None = None,
    after_tool: bool = False,
    reply_to_message_id: int | None = None,
) -> None:
    """Compatibility wrapper; stateful finalization now lives in speech_finalize.py."""

    plan_or_prepared: PreparedOutboundMessage | SpeechPlan
    if isinstance(content, str):
        plan_or_prepared = build_runtime_speech_plan(
            text=content,
            persona=resolve_persona(config),
            current_turn=current_turn,
            context=context or {},
            source="dialogue",
            after_tool=after_tool,
            reply_to_message_id=reply_to_message_id,
        )
    else:
        plan_or_prepared = content
    await _speech_finalize_reply(
        bot,
        group_id,
        config,
        session,
        normalized,
        plan_or_prepared,
        user_prompt,
        enqueued_at,
        message_id,
        send_func=_send_unless_expired,
        persist_func=persist_bot_reply,
        mark_func=mark_bot_reply,
        duplicate_func=_is_recent_duplicate,
        emotion_state=(context or {}).get("emotion_state"),
    )


async def _quote_target(
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


@dataclass(slots=True)
class _DialogueTurn:
    """一轮明确触发的可变运行状态；只在本轮内存在。"""

    bot: Bot
    event: GroupMessageEvent
    normalized: NormalizedMessage
    session: Any
    config: GroupAgentConfig
    enqueued_at: float | None
    group_id: int
    bot_id: int
    actor_user_id: int
    message_id: Any
    model: str
    context: dict[str, Any]
    capabilities: BotGroupCapabilities
    allow_admin_tools: bool
    selected_tool_names: frozenset[str]
    capability_source: str
    message_segment_types: frozenset[str]
    tools: list[dict[str, Any]]
    round_limit: int
    media_inputs: list[MediaInput]
    media_blocks: list[dict[str, Any]]
    media_diagnostics: list[dict[str, Any]]
    cached_captions: list[tuple[str, str]]
    media_digests: list[str]
    user_prompt: str
    current_turn: CurrentTurn
    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_context: ToolExecutionContext | None = None
    discovery_guard: DiscoveryRoundGuard | None = None
    turn_usage: dict[str, int] = field(default_factory=dict)
    rounds: int = 0
    had_tool_results: bool = False
    fallback_attempted: bool = False
    deadline: float = 0.0

    @property
    def query_text(self) -> str:
        return _semantic_query_text(self.normalized)

    def rebuild_tools(self) -> None:
        self.tools = build_tool_schemas(
            self.capabilities,
            allow_admin_tools=self.allow_admin_tools,
            segment_capabilities=get_segment_capabilities(self.bot, self.group_id),
            privileged_allowlist=set(self.config.tool_allowlist or []),
            include_names=self.selected_tool_names,
            message_segment_types=(
                self.message_segment_types
                if "send_message" in self.selected_tool_names
                else None
            ),
        )

    def rebuild_messages(self) -> str:
        self.messages, fingerprint = build_messages(
            persona=resolve_persona(self.config),
            tools=self.tools,
            context=self.context,
            user_prompt=self.user_prompt,
            current_turn=self.current_turn,
            media_inputs=self.media_blocks
            if resolve_llm_request("agent_dialogue").multimodal != "unsupported"
            else None,
        )
        return fingerprint

    async def send_notice(self, text: str, *, label: str) -> None:
        await _send_unless_expired(
            self.bot,
            self.group_id,
            text,
            self.enqueued_at,
            label=label,
            message_id=self.message_id,
        )


class _TurnAbortedError(Exception):
    """工具批次提交失败等需要立即结束本轮的情况；已在抛出前完成收尾。"""


@dataclass(slots=True)
class _RoundState:
    sent_message: bool = False
    needs_commit: bool = False
    commit_tools: list[str] = field(default_factory=list)
    discovered_names: set[str] = field(default_factory=set)
    discovered_requires_admin: bool = False
    media_blocks: list[dict[str, Any]] = field(default_factory=list)


async def _load_turn_media(
    turn_bot: Bot,
    session: Any,
    group_id: int,
    normalized: NormalizedMessage,
    config: GroupAgentConfig,
    context: dict[str, Any],
) -> tuple[list[MediaInput], list[dict[str, Any]], list[tuple[str, str]]]:
    diagnostics: list[dict[str, Any]] = []
    media_inputs = await resolve_media_context(
        turn_bot,
        session,
        group_id,
        current_message=normalized,
        reply_chain=normalized.reply_chain,
        selected_history=list(context.get("messages") or []),
        query_text=_semantic_query_text(normalized),
        asset_ttl_seconds=max(int(config.raw_retention_days), 1) * 86400,
        cache_enabled=bool(config.media_cache_enabled),
        diagnostics=diagnostics,
    )
    cached_captions: list[tuple[str, str]] = []
    if config.media_cache_enabled:
        vision_model = resolve_llm_request("agent_image").model
        for media_input in media_inputs:
            caption = await get_cached_caption(
                session, group_id, media_input.content_hash, vision_model
            )
            if caption:
                cached_captions.append((media_input.content_hash, caption))
    return media_inputs, diagnostics, cached_captions


async def _prepare_turn(
    bot: Bot,
    event: GroupMessageEvent,
    normalized: NormalizedMessage,
    session: Any,
    config: GroupAgentConfig,
    *,
    enqueued_at: float | None,
) -> _DialogueTurn:
    group_id = int(event.group_id)
    bot_id = int(bot.self_id)
    message_id = getattr(event, "message_id", None)
    actor_user_id = int(event.get_user_id())
    model = resolve_llm_request("agent_dialogue").model
    query_text = _semantic_query_text(normalized)
    focus_ids = _current_turn_focus_ids(actor_user_id, normalized, bot_id=bot_id)
    excluded_ids = [
        int(item)
        for item in (
            message_id,
            *normalized.merged_message_ids,
        )
        if item is not None
    ]

    context_started = time.monotonic()
    context = await _load_context(
        session,
        group_id,
        config,
        bot_id,
        focus_user_ids=focus_ids,
        query_text=query_text,
        exclude_message_id=(
            excluded_ids[0] if len(excluded_ids) == 1 else (excluded_ids or None)
        ),
        context_model=model,
        completion_reserve=_COMPLETION_RESERVE,
        context_token_limit=_CONTEXT_TOKEN_LIMIT,
    )
    trace_event(
        "context",
        "上下文选择与装箱",
        input={
            "focus_user_ids": focus_ids,
            "query_chars": len(query_text),
            "query_preview": query_text[:240],
            "merged_messages": len(normalized.merged_message_ids),
            "context_token_limit": _CONTEXT_TOKEN_LIMIT,
            "completion_reserve": _COMPLETION_RESERVE,
        },
        output={
            "messages": len(list(context.get("messages") or [])),
            "members": len(list(context.get("members") or [])),
            "memories": len(list(context.get("memories") or [])),
            "relations": len(list(context.get("relations") or [])),
            "model": model,
        },
        duration_ms=(time.monotonic() - context_started) * 1000,
    )

    capability_started = time.monotonic()
    has_target_mentions = any(
        int(user_id) != int(bot_id) for user_id in normalized.mentions
    )
    tool_intent_text = normalized.intent_text()
    has_reply_context = bool(normalized.reply_chain)
    has_media_context = bool(normalized.media_refs)
    (
        capabilities,
        allow_admin_tools,
        selected_tool_names,
        capability_source,
    ) = await _resolve_turn_tool_capabilities(
        bot,
        group_id,
        actor_user_id,
        tool_intent_text,
        has_reply=has_reply_context,
        has_mentions=has_target_mentions,
        has_media=has_media_context,
    )
    message_segment_types = select_dialogue_message_segment_types(
        tool_intent_text,
        has_target_mentions=has_target_mentions,
        has_reply=has_reply_context,
        has_media=has_media_context,
    )

    media_started = time.monotonic()
    media_inputs, media_diagnostics, cached_captions = await _load_turn_media(
        bot, session, group_id, normalized, config, context
    )
    media_digests = [item.content_hash for item in media_inputs]
    dbg(
        f"群 {group_id} 媒体输入: media_inputs={len(media_inputs)} "
        f"缓存字幕={len(cached_captions)} digests={media_digests}"
    )
    user_prompt, media_blocks = await _prepare_media_prompt(
        group_id,
        normalized,
        session,
        config,
        media_inputs,
        cached_captions,
        media_digests,
        diagnostics=media_diagnostics,
    )
    safe_media_diagnostics = public_media_diagnostics(media_diagnostics)
    dialogue_media_request = resolve_llm_request("agent_dialogue")
    trace_event(
        "media",
        "媒体解析",
        status=_media_trace_status(
            {
                str(item.get("status") or "")
                for item in safe_media_diagnostics
                if isinstance(item, dict)
            },
            failed=_MEDIA_FAILED_STATUSES,
        ),
        input={
            "media": [
                {"type": item.get("type"), "source": item.get("source", "current")}
                for item in normalized.media_refs
            ]
        },
        output={
            "vision_blocks": len(media_blocks),
            "delivered_media": sum(
                1
                for item in safe_media_diagnostics
                if bool(item.get("delivered_to_model"))
            ),
            "cached_captions": len(cached_captions),
            "content_hashes": [digest[:12] for digest in media_digests],
            "items": safe_media_diagnostics,
            "cache_enabled": bool(config.media_cache_enabled),
            "provider": dialogue_media_request.provider,
            "model": dialogue_media_request.model,
            "multimodal_mode": dialogue_media_request.multimodal,
        },
        duration_ms=(time.monotonic() - media_started) * 1000,
    )
    current_turn: CurrentTurn = build_current_turn(
        message_id=int(message_id) if message_id is not None else None,
        user_id=actor_user_id,
        name=event.sender.card or event.sender.nickname,
        role=str(event.sender.role or "member"),
        title=event.sender.title,
        content=user_prompt,
        mentions=normalized.mentions,
        reply_chain=normalized.reply_chain,
        trigger=normalized.trigger_source or "explicit_call",
        received_at=now_beijing(),
        media_refs=normalized.media_refs,
        forward_nodes=len(normalized.forward_tree),
        truncated=normalized.truncated,
    )
    turn = _DialogueTurn(
        bot=bot,
        event=event,
        normalized=normalized,
        session=session,
        config=config,
        enqueued_at=enqueued_at,
        group_id=group_id,
        bot_id=bot_id,
        actor_user_id=actor_user_id,
        message_id=message_id,
        model=model,
        context=context,
        capabilities=capabilities,
        allow_admin_tools=allow_admin_tools,
        selected_tool_names=selected_tool_names,
        capability_source=capability_source,
        message_segment_types=message_segment_types,
        tools=[],
        round_limit=dialogue_tool_round_limit(selected_tool_names),
        media_inputs=media_inputs,
        media_blocks=media_blocks,
        media_diagnostics=media_diagnostics,
        cached_captions=cached_captions,
        media_digests=media_digests,
        user_prompt=user_prompt,
        current_turn=current_turn,
    )
    turn.rebuild_tools()
    tool_schema_chars = len(
        json.dumps(turn.tools, ensure_ascii=False, separators=(",", ":"))
    )
    record_agent_tool_selection(
        schema_chars=tool_schema_chars,
        selected_count=len(selected_tool_names),
        exposed_count=len(turn.tools),
    )
    trace_event(
        "capability",
        "协议能力与工具权限计算",
        output={
            "bot_role": capabilities.role,
            "bot_can_manage": capabilities.can_manage,
            "onebot_actions": sorted(capabilities.actions),
            "capability_source": capability_source,
            "actor_can_manage": allow_admin_tools,
            "round_limit": turn.round_limit,
            "message_segment_types": sorted(message_segment_types),
            "selected_tool_names": sorted(selected_tool_names),
            "tool_names": _tool_names(turn.tools),
            "tool_schema_chars": tool_schema_chars,
            "tool_count": len(turn.tools),
        },
        duration_ms=(time.monotonic() - capability_started) * 1000,
    )
    dbg(
        f"群 {group_id} 工具能力计算完成: source={capability_source} "
        f"bot_role={capabilities.role!r} 工具 {len(turn.tools)} 个 "
        f"轮次上限={turn.round_limit} 发起人 {actor_user_id} 管理工具权限={allow_admin_tools}"
    )

    prompt_started = time.monotonic()
    fingerprint = turn.rebuild_messages()
    _trace_prompt(
        turn, fingerprint, duration_ms=(time.monotonic() - prompt_started) * 1000
    )
    turn.tool_context = ToolExecutionContext(
        bot=bot,
        group_id=group_id,
        actor_user_id=actor_user_id,
        session=session,
        capabilities=capabilities,
        actor_can_manage=(True if allow_admin_tools else None),
        privileged_allowlist=frozenset(config.tool_allowlist or []),
        current_intent_text=tool_intent_text,
    )
    turn.discovery_guard = DiscoveryRoundGuard(set(selected_tool_names))
    turn.deadline = time.monotonic() + _MAX_TURN_SECONDS
    return turn


def _tool_names(tools: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("function", {}).get("name") or "") for item in tools]


def _trace_prompt(turn: _DialogueTurn, fingerprint: str, *, duration_ms: float) -> None:
    """记录 Prompt 形状与本地前缀稳定性；不保存完整 Prompt。"""

    cache_key = prompt_cache_key(
        persona=resolve_persona(turn.config),
        tools=turn.tools,
        model=turn.model,
        persona_version=turn.config.persona_version,
    )
    stable_key = stable_context_key(turn.context)
    prompt_hit = cache_key in _PROMPT_CACHE_KEYS
    context_hit = stable_key in _PROMPT_CACHE_KEYS
    trace_event(
        "prompt",
        "Prompt 构建",
        input={
            "tool_count": len(turn.tools),
            "media_blocks": len(turn.media_blocks),
            "persona_version": turn.config.persona_version,
        },
        output={
            "message_count": len(turn.messages),
            **_trace_prompt_shape(turn.messages),
            "current_turn_chars": len(turn.user_prompt),
            "current_turn_preview": turn.user_prompt[:240],
            "tool_names": _tool_names(turn.tools),
            "prefix_fingerprint": fingerprint[:12],
            "prompt_cache": "hit" if prompt_hit else "miss",
            "context_cache": "hit" if context_hit else "miss",
        },
        duration_ms=duration_ms,
    )
    dbg(
        f"群 {turn.group_id} 提示词构建完成: messages={len(turn.messages)} 条 "
        f"前缀指纹={fingerprint[:12]}… 前缀稳定性={'复用' if prompt_hit else '变化'} "
        f"稳定上下文={'复用' if context_hit else '变化'} 用户 prompt={turn.user_prompt!r}"
    )
    try:
        from ..metrics import record_agent_cache

        record_agent_cache("prompt", "hit" if prompt_hit else "miss")
        # 只观测本地前缀是否稳定；服务商实际缓存 token 由 usage 指标记录。
        record_agent_cache("context", "hit" if context_hit else "miss")
    except Exception:  # noqa: BLE001
        dbg_exc(f"群 {turn.group_id} 上报 prompt 缓存指标失败(忽略)")
    for key in (cache_key, stable_key):
        _PROMPT_CACHE_KEYS[key] = None
        _PROMPT_CACHE_KEYS.move_to_end(key)
    while len(_PROMPT_CACHE_KEYS) > _PROMPT_CACHE_LIMIT:
        _PROMPT_CACHE_KEYS.popitem(last=False)


_REBUILT = object()


async def _request_completion(turn: _DialogueTurn) -> Any:
    """请求一次模型；多模态不兼容时改走视觉转述重建 Prompt 并返回 ``_REBUILT``。"""

    llm_started = time.monotonic()
    try:
        return await complete_with_tools_result(  # pyright: ignore[reportArgumentType]
            turn.messages,  # pyright: ignore[reportArgumentType]
            turn.tools,  # pyright: ignore[reportArgumentType]
            task="agent_dialogue",
            max_tokens=_COMPLETION_RESERVE,
            timeout=30,
            multimodal=bool(turn.media_blocks),
            raise_on_unsupported=bool(turn.media_blocks)
            and not turn.fallback_attempted,
        )
    except LLMMultimodalUnsupportedError:
        trace_event(
            "llm",
            "模型多模态请求",
            status="degraded",
            output={"model": turn.model, "fallback": "vision_caption"},
            detail="模型不支持当前多模态输入，改用视觉转述后重建 Prompt",
            duration_ms=(time.monotonic() - llm_started) * 1000,
            round_index=turn.rounds + 1,
        )
        dbg(f"群 {turn.group_id} 模型不支持多模态,降级为视觉转述重建提示词(不占轮次)")
        turn.fallback_attempted = True
        described = await _describe_images(
            turn.group_id,
            turn.normalized,
            turn.media_inputs,
            turn.session,
            turn.config,
            turn.cached_captions,
            turn.media_digests,
            diagnostics=turn.media_diagnostics,
        )
        turn.user_prompt = f"{turn.query_text}\n{described}"
        turn.current_turn = CurrentTurn(
            **{**turn.current_turn.as_dict(), "content": turn.user_prompt}
        )
        turn.media_blocks = []
        turn.rebuild_messages()
        return _REBUILT


async def _run_turn(turn: _DialogueTurn) -> None:
    turn_started_at = time.monotonic()
    while turn.rounds < turn.round_limit:
        if time.monotonic() > turn.deadline:
            dbg(
                f"群 {turn.group_id} 工具循环超过 {_MAX_TURN_SECONDS}s 时限,发送收尾提示"
            )
            await turn.send_notice(_TURN_END_NOTICE, label="收尾")
            return
        llm_started = time.monotonic()
        completion = await _request_completion(turn)
        if completion is _REBUILT:
            # 多模态降级重建提示词，不占用工具轮次。
            continue
        turn.rounds += 1
        _TOOL_ROUND_COUNT.set(turn.rounds)
        usage = _accumulate_turn_usage(turn.turn_usage, completion)
        response = completion.message
        if response is None:
            trace_event(
                "llm",
                "模型调用",
                status="degraded",
                output={
                    "model": turn.model,
                    "response": "none",
                    "outcome": completion.outcome,
                    "usage": usage,
                },
                detail="LLM 返回空结果，进入确定性兜底回复",
                duration_ms=(time.monotonic() - llm_started) * 1000,
                round_index=turn.rounds,
            )
            fallback = _deterministic_reply(
                turn.normalized.plain_text
            ) or _fallback_notice(turn.group_id)
            dbg(
                f"群 {turn.group_id} 第 {turn.rounds} 轮 LLM 返回 None,降级回复={fallback!r}"
            )
            await turn.send_notice(fallback, label="兜底回复")
            return
        content = (response.content or "").strip()
        tool_calls = response.tool_calls or []
        trace_event(
            "llm",
            "模型调用",
            output={
                "model": turn.model,
                "content_chars": len(content),
                "tool_calls": [
                    str(getattr(getattr(call, "function", None), "name", "") or "")
                    for call in tool_calls
                ],
                "finish_reason": completion.finish_reason,
                "content_preview": content[:320],
                "usage": usage,
            },
            duration_ms=(time.monotonic() - llm_started) * 1000,
            round_index=turn.rounds,
        )
        dbg(
            f"群 {turn.group_id} 第 {turn.rounds}/{turn.round_limit} 轮 LLM 响应: "
            f"content={content!r} tool_calls={[getattr(getattr(c, 'function', None), 'name', None) for c in tool_calls]}"
        )
        if not tool_calls:
            if content:
                await _finalize_turn_reply(turn, content)
            return
        try:
            if await _run_tool_round(turn, content, tool_calls):
                return
        except _TurnAbortedError:
            return
    # 走到这里说明所有轮次都被工具调用耗尽、始终没有最终回复。给用户一个交代，不能静默。
    dbg(
        f"群 {turn.group_id} {turn.round_limit} 轮全部被工具调用耗尽,无最终回复,"
        f"发送收尾提示;整轮耗时 {time.monotonic() - turn_started_at:.1f}s"
    )
    await turn.send_notice(_TURN_END_NOTICE, label="工具收尾")


async def _finalize_turn_reply(turn: _DialogueTurn, content: str) -> None:
    reply_to = await _quote_target(
        turn.session,
        group_id=turn.group_id,
        bot_id=turn.bot_id,
        actor_user_id=turn.actor_user_id,
        message_id=turn.message_id,
        normalized=turn.normalized,
    )
    if reply_to is not None:
        trace_event(
            "speech",
            "繁忙群自动引用",
            output={"reply_to_message_id": reply_to},
            detail="触发之后群里有其他人发言，引用触发消息以免答非所问",
        )
    await _finalize_reply(
        turn.bot,
        turn.group_id,
        turn.config,
        turn.session,
        turn.normalized,
        content,
        render_current_turn(turn.current_turn),
        turn.enqueued_at,
        turn.message_id,
        context=turn.context,
        current_turn=turn.current_turn,
        after_tool=turn.had_tool_results,
        reply_to_message_id=reply_to,
    )


async def _commit_or_abort(
    turn: _DialogueTurn,
    round_index: int,
    tool_names: list[str],
    *,
    immediate: bool,
    sent: bool,
) -> None:
    if await _commit_tool_batch(
        turn.session,
        turn.config,
        turn.group_id,
        round_index,
        tool_names,
        immediate=immediate,
    ):
        return
    await _abort_after_commit_failure(
        _send_unless_expired,
        _TURN_END_NOTICE,
        turn.bot,
        turn.group_id,
        enqueued_at=turn.enqueued_at,
        message_id=turn.message_id,
        sent=sent,
    )
    raise _TurnAbortedError


async def _execute_tool_call(
    turn: _DialogueTurn, state: _RoundState, tool_name: str, raw_args: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        args = json.loads(raw_args)
        if not isinstance(args, dict):
            raise ValueError("工具参数必须是对象")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        dbg(f"群 {turn.group_id} 工具参数解析失败: {exc}")
        return {}, {"ok": False, "error": str(exc)}
    if (
        tool_name in _VISIBLE_SEND_TOOLS
        and turn.enqueued_at is not None
        and is_pending_trigger_expired(turn.enqueued_at)
    ):
        dbg(f"群 {turn.group_id} 工具 {tool_name} 发送前触发已过期,取消副作用")
        return args, {"ok": False, "error": "触发消息已过期，取消发送", "expired": True}
    assert turn.discovery_guard is not None and turn.tool_context is not None
    if tool_name == "discover_tools":
        guarded = turn.discovery_guard.preflight(args)
        if guarded is not None:
            return args, guarded
    execution = await execute_tool_with_meta(tool_name, args, context=turn.tool_context)
    if execution.needs_commit:
        state.needs_commit = True
        state.commit_tools.append(tool_name)
    if execution.immediate_commit:
        await _commit_or_abort(
            turn, turn.rounds, [tool_name], immediate=True, sent=state.sent_message
        )
        state.needs_commit = False
        state.commit_tools.clear()
    return args, execution.payload


async def _project_tool_media(
    turn: _DialogueTurn, state: _RoundState, tool_name: str, result: dict[str, Any]
) -> tuple[dict[str, Any], str | None]:
    """把工具结果里的图片交给模型：直连多模态或视觉转述，返回投影后的结果。"""

    diagnostics: list[dict[str, Any]] = []
    tool_media_inputs = await resolve_media_context(
        turn.bot,
        turn.session,
        turn.group_id,
        tool_results=[result],
        query_text=turn.query_text,
        asset_ttl_seconds=max(int(turn.config.raw_retention_days), 1) * 86400,
        cache_enabled=bool(turn.config.media_cache_enabled),
        diagnostics=diagnostics,
    )
    blocks: list[dict[str, Any]] = []
    note: str | None = None
    dialogue_request = resolve_llm_request("agent_dialogue")
    vision_request = resolve_llm_request("agent_image")
    direct = dialogue_request.multimodal == "supported" or (
        dialogue_request.multimodal == "auto"
        and dialogue_request.provider == vision_request.provider
        and dialogue_request.model == vision_request.model
    )
    if direct:
        cached: dict[str, str] = {}
        if turn.config.media_cache_enabled:
            for item in tool_media_inputs:
                caption = await get_cached_caption(
                    turn.session, turn.group_id, item.content_hash, vision_request.model
                )
                if caption:
                    cached[item.content_hash] = caption
        projection = await project_context_for_llm(
            tool_media_inputs,
            task="agent_dialogue",
            group_id=turn.group_id,
            session=turn.session,
            cached_captions=cached,
            unresolved_diagnostics=[dict(item) for item in diagnostics],
            diagnostics=diagnostics,
        )
        blocks = projection.content_blocks
        known = {item.content_hash for item in turn.media_inputs}
        turn.media_inputs.extend(
            item for item in tool_media_inputs if item.content_hash not in known
        )
        if blocks:
            turn.media_blocks.extend(blocks)
            state.media_blocks.extend(blocks)
    else:
        if tool_media_inputs:
            note = await _describe_images(
                turn.group_id,
                turn.normalized,
                tool_media_inputs,
                turn.session,
                turn.config,
                [],
                [item.content_hash for item in tool_media_inputs],
                diagnostics=diagnostics,
            )
        unresolved = await _unresolved_media_note(
            turn.group_id, turn.session, diagnostics
        )
        if unresolved:
            note = "\n".join(part for part in (note, unresolved) if part)
    if diagnostics:
        safe = public_media_diagnostics(diagnostics)
        trace_event(
            "media",
            "工具结果媒体解析",
            status=_media_trace_status(
                {
                    str(item.get("status") or "")
                    for item in safe
                    if isinstance(item, dict)
                },
                failed=frozenset({"dropped_unavailable", "unavailable"}),
            ),
            input={"tool": tool_name, "source": "tool"},
            output={
                "vision_blocks": len(blocks),
                "items": safe,
                "provider": dialogue_request.provider,
                "model": dialogue_request.model,
                "multimodal_mode": dialogue_request.multimodal,
            },
            round_index=turn.rounds,
        )
    return project_tool_result_media(result, tool_media_inputs), note


async def _record_tool_send(turn: _DialogueTurn, result: dict[str, Any]) -> None:
    await _speech_record_tool_send(
        turn.session,
        turn.config,
        result,
        bot_id=turn.bot_id,
        group_id=turn.group_id,
        current_turn=turn.current_turn,
        context=turn.context,
        persist_func=persist_bot_reply,
        mark_func=mark_bot_reply,
    )


def _observe_discovery(
    turn: _DialogueTurn, state: _RoundState, result: dict[str, Any]
) -> None:
    record_agent_tool_discovery("called")
    if not bool(result.get("ok")):
        return
    payload = result.get("result")
    rows = payload.get("tools", []) if isinstance(payload, dict) else []
    record_agent_tool_discovery("returned" if rows else "empty")
    assert turn.discovery_guard is not None
    new_names, requires_admin = turn.discovery_guard.observe(payload)
    state.discovered_names.update(new_names)
    state.discovered_requires_admin = state.discovered_requires_admin or requires_admin


async def _apply_discovery(turn: _DialogueTurn, state: _RoundState) -> None:
    """下一轮动态装载 discover_tools 找到的正式 schema，或在空转时关闭发现。"""

    guard = turn.discovery_guard
    assert guard is not None and turn.tool_context is not None
    if guard.disabled:
        turn.selected_tool_names = frozenset(
            name for name in turn.selected_tool_names if name != "discover_tools"
        )
        turn.rebuild_tools()
        trace_event(
            "capability",
            "动态工具发现保护",
            output={
                "discovery_attempts": guard.attempts,
                "discover_tools_disabled": True,
                "tool_count": len(turn.tools),
            },
            round_index=turn.rounds,
        )
    if not state.discovered_names:
        return
    if state.discovered_requires_admin:
        # 可信的发现结果只会在 actor 权限和 bot 角色校验通过后才包含特权
        # 工具；重新读取能力对象，让下一轮能装载这些 schema。
        turn.capabilities = await probe_group_capabilities(turn.bot, turn.group_id)
        turn.tool_context = turn.tool_context.with_capabilities(turn.capabilities)
        turn.allow_admin_tools = True
        turn.capability_source = "progressive_discovery_live_probe"
    turn.selected_tool_names = frozenset(
        set(turn.selected_tool_names) | state.discovered_names
    )
    turn.rebuild_tools()
    loaded_names = set(_tool_names(turn.tools))
    loaded = sorted(name for name in state.discovered_names if name in loaded_names)
    if loaded:
        record_agent_tool_discovery("used", len(loaded))
    turn.round_limit = max(turn.round_limit, min(MAX_TOOL_ROUNDS, turn.rounds + 2))
    trace_event(
        "capability",
        "动态工具发现",
        output={
            "requested": sorted(state.discovered_names),
            "loaded": loaded,
            "tool_count": len(turn.tools),
            "round_limit": turn.round_limit,
            "capability_source": turn.capability_source,
        },
        round_index=turn.rounds,
    )


async def _run_tool_round(
    turn: _DialogueTurn, content: str, tool_calls: Sequence[Any]
) -> bool:
    """执行一轮模型给出的 tool_calls；返回本轮对话是否已经结束。"""

    assistant: dict[str, Any] = {
        "role": "assistant",
        "content": content or None,
        "tool_calls": [],
    }
    turn.messages.append(assistant)
    state = _RoundState()
    for call in tool_calls:
        function = getattr(call, "function", None)
        if function is None:
            dbg(
                f"群 {turn.group_id} 跳过缺少 function 的 tool_call id={getattr(call, 'id', None)}"
            )
            continue
        tool_name = str(getattr(function, "name", "") or "")
        raw_args = getattr(function, "arguments", "{}") or "{}"
        tool_started = time.monotonic()
        dbg(
            f"群 {turn.group_id} 第 {turn.rounds} 轮工具调用: name={tool_name!r} args={raw_args}"
        )
        args, result = await _execute_tool_call(turn, state, tool_name, raw_args)
        turn.had_tool_results = True
        if tool_name == "discover_tools":
            _observe_discovery(turn, state, result)
        projected, media_note = await _project_tool_media(
            turn, state, tool_name, result
        )
        ends_turn = _visible_tool_send_ends_turn(result)
        trace_event(
            "tool",
            f"工具 {tool_name or '[unknown]'}",
            status=("success" if bool(result.get("ok")) else "failed"),
            input={"arguments": args},
            output={
                "ok": bool(result.get("ok")),
                "error": result.get("error"),
                "ends_turn": ends_turn,
            },
            duration_ms=(time.monotonic() - tool_started) * 1000,
            round_index=turn.rounds,
        )
        dbg(
            f"群 {turn.group_id} 工具 {tool_name!r} 返回: {json.dumps(projected, ensure_ascii=False)}"
        )
        if ends_turn:
            state.sent_message = True
            await _record_tool_send(turn, result)
        assistant["tool_calls"].append(
            {
                "id": call.id,
                "type": "function",
                "function": {"name": tool_name, "arguments": raw_args},
            }
        )
        tool_payload = (
            dict(projected)
            if isinstance(projected, dict)
            else {"ok": False, "error": "tool_result_projection_failed"}
        )
        if media_note:
            tool_payload["media_context"] = media_note
        tool_payload["speech_evidence"] = build_speech_evidence(
            tool_name, tool_payload
        ).prompt_dict()
        turn.messages.append(
            {
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(tool_payload, ensure_ascii=False),
            }
        )
        if state.sent_message:
            # 一次模型决策最多执行一个用户可见发送动作；避免模型同一轮
            # 同时调用 send_message/send_forward 连发多条。
            break
    if state.needs_commit:
        await _commit_or_abort(
            turn,
            turn.rounds,
            state.commit_tools,
            immediate=False,
            sent=state.sent_message,
        )
    if state.sent_message:
        dbg(f"群 {turn.group_id} 工具已发送用户可见消息,结束本轮避免重复回复")
        return True
    if state.media_blocks:
        turn.messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "以下图片来自刚才的工具查询结果，请结合该工具结果继续回答。",
                    },
                    *state.media_blocks,
                ],
            }
        )
    await _apply_discovery(turn, state)
    return False


async def _process_group_message(
    bot: Bot,
    event: GroupMessageEvent,
    normalized: NormalizedMessage,
    *,
    enqueued_at: float | None = None,
) -> None:
    group_id = int(event.group_id)
    bot_id = int(bot.self_id)
    message_id = getattr(event, "message_id", None)
    dbg(
        f"群 {group_id} 开始处理消息: bot={bot_id} user={event.get_user_id()} "
        f"message_id={message_id} 合并={normalized.merged_message_ids} "
        f"完整消息={_semantic_query_text(normalized)!r}"
    )
    async with group_lock(group_id, bot_id):
        if enqueued_at is not None and is_pending_trigger_expired(enqueued_at):
            dbg(
                f"群 {group_id} 触发在等待群锁期间过期,跳过回复: message_id={message_id}"
            )
            return
        async with get_session() as session:
            config = await get_or_create_config(session, group_id)
            if config is None or not await agent_runtime_enabled(
                session, group_id, config=config
            ):
                dbg(
                    f"群 {group_id} 处理中止: Agent 总开关"
                    f"{'配置缺失' if config is None else '已关闭'}"
                )
                return
            _start_wait_notice(bot, group_id, enqueued_at, message_id)
            turn = await _prepare_turn(
                bot, event, normalized, session, config, enqueued_at=enqueued_at
            )
            await _run_turn(turn)


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
    _trace_intake(
        normalized, bot_id=int(bot.self_id), started=started, enqueued_at=enqueued_at
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
                    max(started - enqueued_at, 0.0) if enqueued_at is not None else None
                ),
            )
        except Exception:  # noqa: BLE001
            dbg_exc("Agent 对话回合指标上报失败(忽略)")
