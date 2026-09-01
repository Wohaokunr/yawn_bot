# ruff: noqa: E501,F401,I001,TID252,PLR0911,PLR0912,PLR0913,PLR0915,PLR0917,PLR2004,C901,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100,SIM117
"""群聊 Agent 对话主流程：上下文加载、多模态降级、LLM 工具循环与回复收尾。"""

import asyncio
import hashlib
import json
import time
from collections import OrderedDict
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
from .capabilities import (
    get_segment_capabilities,
    probe_group_capabilities,
    user_can_manage_group,
)
from .collector import group_lock, is_pending_trigger_expired
from .config_store import agent_runtime_enabled, get_or_create_config
from .conversation import mark_bot_reply
from .activity import activity_window_counts as _activity_window_counts
from .context_loader import load_context as _load_context
from .dialogue_support import (
    contains_word,
    current_turn_focus_ids as _current_turn_focus_ids,
    deterministic_reply as _deterministic_reply,
    is_recent_duplicate as _is_recent_duplicate,
    persist_bot_reply,
    send_group_text as _send_group_text,
    send_unless_expired as _send_unless_expired,
)
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
from .speech import SpeechPlan
from .speech_finalize import apply_speech_topic, finalize_reply as _speech_finalize_reply
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
    execute_tool,
    select_dialogue_message_segment_types,
    select_dialogue_tool_names,
)

_PROMPT_CACHE_KEYS: OrderedDict[str, None] = OrderedDict()
_PROMPT_CACHE_LIMIT = 256
_MAX_TURN_SECONDS = 120.0
_FALLBACK_NOTICE = "现在有点忙，稍后再试～"
_TURN_END_NOTICE = "这个话题我先记下了，稍后再继续聊～"
_VISIBLE_SEND_TOOLS = frozenset({"send_message", "send_forward"})
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


def _extract_message_id(result: Any) -> int | None:
    """OneBot 实现对 send_group_msg 返回值不统一：dict、对象或裸 int；0 视为缺失。"""

    return extract_outbound_message_id(result)


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
    content: str | PreparedOutboundMessage | SpeechPlan,
    user_prompt: str,
    enqueued_at: float | None,
    message_id: Any,
    *,
    context: dict[str, Any] | None = None,
    current_turn: CurrentTurn | None = None,
    after_tool: bool = False,
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


async def _process_group_message(
    bot: Bot,
    event: GroupMessageEvent,
    normalized: NormalizedMessage,
    *,
    enqueued_at: float | None = None,
) -> None:
    group_id = int(event.group_id)
    bot_id = int(bot.self_id)
    turn_started_at = time.monotonic()
    message_id = getattr(event, "message_id", None)
    dbg(
        f"群 {group_id} 开始处理消息: bot={bot_id} user={event.get_user_id()} "
        f"message_id={message_id} 完整消息={normalized.prompt_text()!r}"
    )
    async with group_lock(group_id, bot_id):
        if enqueued_at is not None and is_pending_trigger_expired(enqueued_at):
            dbg(
                f"群 {group_id} 触发在等待群锁期间过期,跳过回复: message_id={message_id}"
            )
            return
        dbg(f"群 {group_id} 已取得群锁,开始处理")
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
            actor_user_id = int(event.get_user_id())
            model = resolve_llm_request("agent_dialogue").model
            context_started = time.monotonic()
            context = await _load_context(
                session,
                group_id,
                config,
                bot_id,
                focus_user_ids=_current_turn_focus_ids(
                    actor_user_id, normalized, bot_id=bot_id
                ),
                query_text=normalized.prompt_text(),
                exclude_message_id=int(message_id) if message_id is not None else None,
                context_model=model,
                completion_reserve=800,
                context_token_limit=2400,
            )
            trace_event(
                "context",
                "上下文选择与装箱",
                input={
                    "focus_user_ids": _current_turn_focus_ids(
                        actor_user_id, normalized, bot_id=bot_id
                    ),
                    "query_chars": len(normalized.prompt_text()),
                    "query_preview": normalized.prompt_text()[:240],
                    "context_token_limit": 2400,
                    "completion_reserve": 800,
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
            capabilities = await probe_group_capabilities(bot, group_id)
            allow_admin_tools = await user_can_manage_group(
                bot, group_id, actor_user_id
            )
            dbg(
                f"群 {group_id} 能力探测完成: bot_role={capabilities.role!r} "
                f"can_manage={capabilities.can_manage} actions={len(capabilities.actions)} 个 "
                f"发起人 {actor_user_id} 管理工具权限={allow_admin_tools}"
            )
            has_target_mentions = any(
                int(user_id) != int(bot_id) for user_id in normalized.mentions
            )
            tool_intent_text = normalized.intent_text()
            has_reply_context = bool(normalized.reply_chain)
            has_media_context = bool(normalized.media_refs)
            selected_tool_names = select_dialogue_tool_names(
                tool_intent_text,
                has_reply=has_reply_context,
                has_mentions=has_target_mentions,
                has_media=has_media_context,
                allow_admin_tools=allow_admin_tools,
            )
            message_segment_types = select_dialogue_message_segment_types(
                tool_intent_text,
                has_target_mentions=has_target_mentions,
                has_reply=has_reply_context,
                has_media=has_media_context,
            )
            tools = build_tool_schemas(
                capabilities,
                allow_admin_tools=allow_admin_tools,
                segment_capabilities=get_segment_capabilities(bot, group_id),
                privileged_allowlist=set(config.tool_allowlist or []),
                include_names=selected_tool_names,
                message_segment_types=(
                    message_segment_types
                    if "send_message" in selected_tool_names
                    else None
                ),
            )
            round_limit = dialogue_tool_round_limit(selected_tool_names)
            trace_event(
                "capability",
                "协议能力与工具权限计算",
                output={
                    "bot_role": capabilities.role,
                    "bot_can_manage": capabilities.can_manage,
                    "onebot_actions": sorted(capabilities.actions),
                    "actor_can_manage": allow_admin_tools,
                    "round_limit": round_limit,
                    "message_segment_types": sorted(message_segment_types),
                    "selected_tool_names": sorted(selected_tool_names),
                    "tool_names": [
                        str(item.get("function", {}).get("name") or "")
                        for item in tools
                    ],
                    "tool_schema_chars": len(
                        json.dumps(tools, ensure_ascii=False, separators=(",", ":"))
                    ),
                    "tool_count": len(tools),
                },
                duration_ms=(time.monotonic() - capability_started) * 1000,
            )
            dbg(
                f"群 {group_id} 本轮可用工具 {len(tools)} 个,"
                f"模型轮次上限={round_limit}"
            )
            media_started = time.monotonic()
            media_diagnostics: list[dict[str, Any]] = []
            media_blocks, cached_captions, media_digests = await prepare_image_inputs(
                bot,
                group_id,
                normalized.media_refs,
                session=session,
                cache_enabled=bool(config.media_cache_enabled),
                diagnostics=media_diagnostics,
            )
            dbg(
                f"群 {group_id} 媒体输入: media_blocks={len(media_blocks)} "
                f"缓存字幕={len(cached_captions)} digests={media_digests}"
            )
            user_prompt, media_blocks = await _prepare_media_prompt(
                group_id,
                normalized,
                session,
                config,
                media_blocks,
                cached_captions,
                media_digests,
            )
            trace_event(
                "media",
                "多模态输入准备",
                input={
                    "media": [
                        {"type": item.get("type"), "source": item.get("source", "current")}
                        for item in normalized.media_refs
                    ]
                },
                output={
                    "vision_blocks": len(media_blocks),
                    "cached_captions": len(cached_captions),
                    "content_hashes": [digest[:12] for digest in media_digests],
                    "items": media_diagnostics,
                    "cache_enabled": bool(config.media_cache_enabled),
                    "multimodal_mode": resolve_llm_request("agent_dialogue").multimodal,
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
            dbg(f"群 {group_id} 对话模型={model!r}")
            prompt_started = time.monotonic()
            messages, _prefix_fingerprint = build_messages(
                persona=resolve_persona(config),
                tools=tools,
                context=context,
                user_prompt=user_prompt,
                current_turn=current_turn,
                media_inputs=media_blocks
                if resolve_llm_request("agent_dialogue").multimodal
                != "unsupported"
                else None,
            )
            cache_key = prompt_cache_key(
                persona=resolve_persona(config),
                tools=tools,
                model=model,
                persona_version=config.persona_version,
            )
            stable_key = stable_context_key(context)
            prompt_shape = _trace_prompt_shape(messages)
            trace_event(
                "prompt",
                "Prompt 构建",
                input={
                    "tool_count": len(tools),
                    "media_blocks": len(media_blocks),
                    "persona_version": config.persona_version,
                },
                output={
                    "message_count": len(messages),
                    **prompt_shape,
                    "current_turn_chars": len(user_prompt),
                    "current_turn_preview": user_prompt[:240],
                    "tool_names": [
                        str(item.get("function", {}).get("name") or "")
                        for item in tools
                    ],
                    "prefix_fingerprint": _prefix_fingerprint[:12],
                    "prompt_cache": "hit" if cache_key in _PROMPT_CACHE_KEYS else "miss",
                    "context_cache": "hit" if stable_key in _PROMPT_CACHE_KEYS else "miss",
                },
                duration_ms=(time.monotonic() - prompt_started) * 1000,
            )
            dbg(
                f"群 {group_id} 提示词构建完成: messages={len(messages)} 条 "
                f"prompt 前缀指纹={_prefix_fingerprint[:12]}… "
                f"前缀稳定性={'复用' if cache_key in _PROMPT_CACHE_KEYS else '变化'} "
                f"稳定上下文={'复用' if stable_key in _PROMPT_CACHE_KEYS else '变化'} "
                f"用户 prompt={user_prompt!r}"
            )
            try:
                from ..metrics import record_agent_cache

                record_agent_cache(
                    "prompt", "hit" if cache_key in _PROMPT_CACHE_KEYS else "miss"
                )
                # 只观测本地前缀是否稳定；服务商实际缓存 token 由 usage 指标记录。
                record_agent_cache(
                    "context", "hit" if stable_key in _PROMPT_CACHE_KEYS else "miss"
                )
            except Exception:  # noqa: BLE001
                dbg_exc(f"群 {group_id} 上报 prompt 缓存指标失败(忽略)")
            for key in (cache_key, stable_key):
                _PROMPT_CACHE_KEYS[key] = None
                _PROMPT_CACHE_KEYS.move_to_end(key)
            while len(_PROMPT_CACHE_KEYS) > _PROMPT_CACHE_LIMIT:
                _PROMPT_CACHE_KEYS.popitem(last=False)
            fallback_attempted = False
            deadline = time.monotonic() + _MAX_TURN_SECONDS
            rounds = 0
            had_tool_results = False
            turn_usage: dict[str, int] = {}
            while rounds < round_limit:
                if time.monotonic() > deadline:
                    dbg(
                        f"群 {group_id} 工具循环超过 {_MAX_TURN_SECONDS}s 时限,发送收尾提示"
                    )
                    await _send_unless_expired(
                        bot,
                        group_id,
                        _TURN_END_NOTICE,
                        enqueued_at,
                        label="收尾",
                        message_id=message_id,
                    )
                    return
                llm_started = time.monotonic()
                try:
                    completion = await complete_with_tools_result(  # pyright: ignore[reportArgumentType]
                        messages,  # pyright: ignore[reportArgumentType]
                        tools,  # pyright: ignore[reportArgumentType]
                        task="agent_dialogue",
                        max_tokens=800,
                        timeout=30,
                        multimodal=bool(media_blocks),
                        raise_on_unsupported=bool(media_blocks)
                        and not fallback_attempted,
                    )
                except LLMMultimodalUnsupportedError:
                    trace_event(
                        "llm",
                        "模型多模态请求",
                        status="degraded",
                        output={"model": model, "fallback": "vision_caption"},
                        detail="模型不支持当前多模态输入，改用视觉转述后重建 Prompt",
                        duration_ms=(time.monotonic() - llm_started) * 1000,
                        round_index=rounds + 1,
                    )
                    dbg(
                        f"群 {group_id} 模型不支持多模态,降级为视觉转述重建提示词(不占轮次)"
                    )
                    fallback_attempted = True
                    user_prompt = f"{normalized.prompt_text()}\n{await _describe_images(group_id, normalized, media_blocks, session, config, cached_captions, media_digests)}"
                    current_turn = CurrentTurn(
                        **{**current_turn.as_dict(), "content": user_prompt}
                    )
                    messages, _prefix_fingerprint = build_messages(
                        persona=resolve_persona(config),
                        tools=tools,
                        context=context,
                        user_prompt=user_prompt,
                        current_turn=current_turn,
                    )
                    media_blocks = []
                    # 多模态降级重建提示词，不占用工具轮次。
                    continue
                rounds += 1
                usage = _accumulate_turn_usage(turn_usage, completion)
                response = completion.message
                if response is None:
                    trace_event(
                        "llm",
                        "模型调用",
                        status="degraded",
                        output={
                            "model": model,
                            "response": "none",
                            "outcome": completion.outcome,
                            "usage": usage,
                        },
                        detail="LLM 返回空结果，进入确定性兜底回复",
                        duration_ms=(time.monotonic() - llm_started) * 1000,
                        round_index=rounds,
                    )
                    fallback = (
                        _deterministic_reply(normalized.plain_text) or _FALLBACK_NOTICE
                    )
                    dbg(
                        f"群 {group_id} 第 {rounds} 轮 LLM 返回 None,降级回复={fallback!r}"
                    )
                    await _send_unless_expired(
                        bot,
                        group_id,
                        fallback,
                        enqueued_at,
                        label="兜底回复",
                        message_id=message_id,
                    )
                    return
                content = (response.content or "").strip()
                tool_calls = response.tool_calls or []
                trace_event(
                    "llm",
                    "模型调用",
                    output={
                        "model": model,
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
                    round_index=rounds,
                )
                dbg(
                    f"群 {group_id} 第 {rounds}/{round_limit} 轮 LLM 响应: "
                    f"content={content!r} tool_calls={[getattr(getattr(c, 'function', None), 'name', None) for c in tool_calls]}"
                )
                if not tool_calls:
                    if content:
                        await _finalize_reply(
                            bot,
                            group_id,
                            config,
                            session,
                            normalized,
                            content,
                            render_current_turn(current_turn),
                            enqueued_at,
                            message_id,
                            context=context,
                            current_turn=current_turn,
                            after_tool=had_tool_results,
                        )
                    return
                assistant: dict[str, Any] = {
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": [],
                }
                messages.append(assistant)
                round_sent_message = False
                discovered_tool_names: set[str] = set()
                for call in tool_calls:
                    function = getattr(call, "function", None)
                    if function is None:
                        dbg(
                            f"群 {group_id} 跳过缺少 function 的 tool_call id={getattr(call, 'id', None)}"
                        )
                        continue
                    tool_name = str(getattr(function, "name", "") or "")
                    raw_args = getattr(function, "arguments", "{}") or "{}"
                    tool_started = time.monotonic()
                    dbg(
                        f"群 {group_id} 第 {rounds} 轮工具调用: "
                        f"name={tool_name!r} args={raw_args}"
                    )
                    try:
                        args = json.loads(raw_args)
                        if not isinstance(args, dict):
                            raise ValueError("工具参数必须是对象")
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        args = {}
                        result = {"ok": False, "error": str(exc)}
                        dbg(f"群 {group_id} 工具参数解析失败: {exc}")
                    else:
                        if (
                            tool_name in _VISIBLE_SEND_TOOLS
                            and enqueued_at is not None
                            and is_pending_trigger_expired(enqueued_at)
                        ):
                            result = {
                                "ok": False,
                                "error": "触发消息已过期，取消发送",
                                "expired": True,
                            }
                            dbg(
                                f"群 {group_id} 工具 {tool_name} 发送前触发已过期,取消副作用"
                            )
                        else:
                            result = await execute_tool(
                                tool_name,
                                args,
                                bot=bot,
                                group_id=group_id,
                                actor_user_id=actor_user_id,
                                session=session,
                                capabilities=capabilities,
                            )
                    had_tool_results = True
                    if tool_name == "discover_tools" and bool(result.get("ok")):
                        discovery = result.get("result")
                        discovery_rows = (
                            discovery.get("tools", [])
                            if isinstance(discovery, dict)
                            else []
                        )
                        for item in discovery_rows:
                            if isinstance(item, dict) and item.get("name"):
                                discovered_tool_names.add(str(item["name"]))
                    trace_event(
                        "tool",
                        f"工具 {tool_name or '[unknown]'}",
                        status=(
                            "success"
                            if bool(result.get("ok"))
                            else "failed"
                        ),
                        input={"arguments": args},
                        output={
                            "ok": bool(result.get("ok")),
                            "error": result.get("error"),
                            "ends_turn": _visible_tool_send_ends_turn(result),
                        },
                        duration_ms=(time.monotonic() - tool_started) * 1000,
                        round_index=rounds,
                    )
                    dbg(
                        f"群 {group_id} 工具 {tool_name!r} 返回: "
                        f"{json.dumps(result, ensure_ascii=False)}"
                    )
                    if _visible_tool_send_ends_turn(result):
                        round_sent_message = True
                        payload = (
                            result.get("result", {}).get("outbound", {})
                            if isinstance(result.get("result"), dict)
                            else {}
                        )
                        if isinstance(payload, dict):
                            await persist_bot_reply(
                                session,
                                int(bot.self_id),
                                group_id,
                                _extract_message_id(payload.get("message_id")),
                                str(payload.get("text") or ""),
                                int(config.raw_retention_days),
                                segments=(
                                    payload.get("segments")
                                    if isinstance(payload.get("segments"), list)
                                    else []
                                ),
                                reply_chain=(
                                    payload.get("reply_chain")
                                    if isinstance(payload.get("reply_chain"), list)
                                    else []
                                ),
                                forward_tree=(
                                    payload.get("forward_tree")
                                    if isinstance(payload.get("forward_tree"), list)
                                    else []
                                ),
                                media_refs=(
                                    payload.get("media_refs")
                                    if isinstance(payload.get("media_refs"), list)
                                    else []
                                ),
                            )
                            now = now_beijing()
                            fingerprint_source = str(payload.get("text") or "") or json.dumps(
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
                                    "text": str(payload.get("text") or "")[:500],
                                    "at": now.isoformat(),
                                }
                            )
                            config.recent_response_fingerprints = recent[-8:]
                            config.last_response_fingerprint = response_fingerprint
                            config.last_response_input_fingerprint = input_fingerprint
                            config.last_response_at = now
                            config.last_agent_at = now
                            tool_speech_plan = build_runtime_speech_plan(
                                text=str(payload.get("text") or ""),
                                persona=resolve_persona(config),
                                current_turn=current_turn,
                                context=context,
                                source="dialogue",
                            )
                            tool_topic = apply_speech_topic(config, tool_speech_plan)
                            if config.short_conversation_enabled:
                                mark_bot_reply(
                                    int(bot.self_id),
                                    group_id,
                                    topic=tool_topic,
                                    source="dialogue",
                                    max_bot_turns=persona_behavior(
                                        config
                                    ).max_followup_bot_turns,
                                )
                    assistant["tool_calls"].append(
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": raw_args,
                            },
                        }
                    )
                    tool_payload = dict(result)
                    tool_payload["speech_evidence"] = build_speech_evidence(
                        tool_name, result
                    ).prompt_dict()
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps(tool_payload, ensure_ascii=False),
                        }
                    )
                    try:
                        await session.commit()
                    except SQLAlchemyError:
                        trace_event(
                            "state",
                            "工具轮状态提交",
                            status="failed",
                            detail="数据库提交失败并已回滚",
                            round_index=rounds,
                        )
                        dbg_exc(f"群 {group_id} 工具轮状态提交失败,已回滚")
                        await session.rollback()
                    else:
                        trace_event(
                            "state",
                            "工具轮状态提交",
                            output={"tool": tool_name},
                            round_index=rounds,
                        )
                    # 提交会过期会话内对象；后续轮次还要读取 config 属性，
                    # 先刷新避免同步惰性加载（MissingGreenlet）。
                    await session.refresh(config)
                    if round_sent_message:
                        # 一次模型决策最多执行一个用户可见发送动作；避免模型同一轮
                        # 同时调用 send_message/send_forward 连发多条。
                        break
                if discovered_tool_names and not round_sent_message:
                    selected_tool_names = frozenset(
                        set(selected_tool_names) | discovered_tool_names
                    )
                    tools = build_tool_schemas(
                        capabilities,
                        allow_admin_tools=allow_admin_tools,
                        segment_capabilities=get_segment_capabilities(bot, group_id),
                        privileged_allowlist=set(config.tool_allowlist or []),
                        include_names=selected_tool_names,
                        message_segment_types=(
                            message_segment_types
                            if "send_message" in selected_tool_names
                            else None
                        ),
                    )
                    loaded_names = {
                        str(item.get("function", {}).get("name") or "")
                        for item in tools
                    }
                    loaded_discoveries = sorted(
                        name for name in discovered_tool_names if name in loaded_names
                    )
                    round_limit = max(
                        round_limit,
                        min(MAX_TOOL_ROUNDS, rounds + 2),
                    )
                    trace_event(
                        "capability",
                        "动态工具发现",
                        output={
                            "requested": sorted(discovered_tool_names),
                            "loaded": loaded_discoveries,
                            "tool_count": len(tools),
                            "round_limit": round_limit,
                        },
                        round_index=rounds,
                    )
                if round_sent_message:
                    dbg(f"群 {group_id} 工具已发送用户可见消息,结束本轮避免重复回复")
                    return
            # 走到这里说明所有轮次都被工具调用耗尽、始终没有最终回复。
            # 其余分支均已 return；给用户一个交代，不能静默。
            dbg(
                f"群 {group_id} {round_limit} 轮全部被工具调用耗尽,无最终回复,"
                f"发送收尾提示;整轮耗时 {time.monotonic() - turn_started_at:.1f}s"
            )
            await _send_unless_expired(
                bot,
                group_id,
                _TURN_END_NOTICE,
                enqueued_at,
                label="工具收尾",
                message_id=message_id,
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
