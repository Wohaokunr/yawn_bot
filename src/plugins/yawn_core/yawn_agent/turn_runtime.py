# ruff: noqa: E501, PLR0912, PLR0915, C901, RUF001, TC001, TC002, TID252, TRY004, TRY301
"""单回合 Agent 运行时：能力计算、Prompt 构建与多轮 Tool/LLM 循环。"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot_plugin_orm import get_session

from ..llm import (
    LLMMultimodalUnsupportedError,
    complete_with_tools_result,
    resolve_llm_request,
)
from ..metrics import (
    record_agent_phase,
    record_agent_tool_discovery,
    record_agent_tool_selection,
)
from .capabilities import (
    get_segment_capabilities,
    probe_group_capabilities,
    user_can_manage_group,
)
from .collector import group_lock, is_pending_trigger_expired
from .config_store import agent_runtime_enabled, get_or_create_config
from .context import CurrentTurn, build_current_turn, now_beijing
from .conversation import mark_bot_reply
from .execution_trace import trace_event
from .log import dbg, dbg_exc
from .media import prepare_image_inputs
from .message_parser import NormalizedMessage
from .persona import persona_behavior, resolve_persona
from .prompt import (
    build_messages,
    prompt_cache_key,
    render_current_turn,
    stable_context_key,
)
from .tool_execution import ToolExecutionContext
from .tools import (
    MAX_TOOL_ROUNDS,
    build_tool_schemas,
    dialogue_tool_round_limit,
    execute_tool_with_meta,
    select_dialogue_message_segment_types,
    select_dialogue_tool_names,
)

MAX_TURN_SECONDS = 120.0
TURN_END_NOTICE = "这个话题我先记下了，稍后再继续聊～"
VISIBLE_SEND_TOOLS = frozenset({"send_message", "send_forward"})
PROMPT_CACHE_KEYS: OrderedDict[str, None] = OrderedDict()
PROMPT_CACHE_LIMIT = 256
TOOL_ROUND_COUNT: ContextVar[int] = ContextVar("agent_dialogue_tool_round_count", default=0)


@dataclass(frozen=True, slots=True)
class TurnRuntimeHooks:
    load_context: Any
    current_turn_focus_ids: Any
    prepare_media_prompt: Any
    trace_prompt_shape: Any
    accumulate_turn_usage: Any
    describe_images: Any
    deterministic_reply: Any
    fallback_notice: Any
    send_unless_expired: Any
    finalize_reply: Any
    cancel_wait_notice: Any
    commit_tool_batch: Any
    visible_tool_send_ends_turn: Any
    persist_bot_reply: Any
    extract_message_id: Any
    start_wait_notice: Any


async def run_dialogue_turn(
    bot: Bot,
    event: GroupMessageEvent,
    normalized: NormalizedMessage,
    *,
    hooks: TurnRuntimeHooks,
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
            # 排队/抢锁不算“正在思考”，确认本群真的开始处理后才起等待提示。
            hooks.start_wait_notice(bot, group_id, enqueued_at, message_id)
            actor_user_id = int(event.get_user_id())
            model = resolve_llm_request("agent_dialogue").model
            context_started = time.monotonic()
            context = await hooks.load_context(
                session,
                group_id,
                config,
                bot_id,
                focus_user_ids=hooks.current_turn_focus_ids(
                    actor_user_id, normalized, bot_id=bot_id
                ),
                query_text=normalized.prompt_text(),
                exclude_message_id=int(message_id) if message_id is not None else None,
                context_model=model,
                completion_reserve=800,
                context_token_limit=2400,
            )
            context_elapsed = time.monotonic() - context_started
            record_agent_phase("context", context_elapsed)
            trace_event(
                "context",
                "上下文选择与装箱",
                input={
                    "focus_user_ids": hooks.current_turn_focus_ids(
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
                duration_ms=context_elapsed * 1000,
            )
            capability_started = time.monotonic()
            capabilities = await probe_group_capabilities(bot, group_id)
            allow_admin_tools = await user_can_manage_group(
                bot, group_id, actor_user_id
            )
            privileged_allowlist = frozenset(config.tool_allowlist or [])
            tool_execution_context = ToolExecutionContext(
                bot=bot,
                group_id=group_id,
                actor_user_id=actor_user_id,
                session=session,
                capabilities=capabilities,
                actor_can_manage=allow_admin_tools,
                privileged_allowlist=privileged_allowlist,
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
                privileged_allowlist=set(privileged_allowlist),
                include_names=selected_tool_names,
                message_segment_types=(
                    message_segment_types
                    if "send_message" in selected_tool_names
                    else None
                ),
            )
            round_limit = dialogue_tool_round_limit(selected_tool_names)
            capability_elapsed = time.monotonic() - capability_started
            record_agent_phase("capability", capability_elapsed)
            tool_schema_chars = len(
                json.dumps(tools, ensure_ascii=False, separators=(",", ":"))
            )
            record_agent_tool_selection(
                schema_chars=tool_schema_chars,
                selected_count=len(selected_tool_names),
                exposed_count=len(tools),
            )
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
                    "tool_schema_chars": tool_schema_chars,
                    "tool_count": len(tools),
                },
                duration_ms=capability_elapsed * 1000,
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
            user_prompt, media_blocks = await hooks.prepare_media_prompt(
                group_id,
                normalized,
                session,
                config,
                media_blocks,
                cached_captions,
                media_digests,
            )
            media_elapsed = time.monotonic() - media_started
            record_agent_phase("media", media_elapsed)
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
                duration_ms=media_elapsed * 1000,
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
            prompt_shape = hooks.trace_prompt_shape(messages)
            prompt_elapsed = time.monotonic() - prompt_started
            record_agent_phase("prompt", prompt_elapsed)
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
                    "prompt_cache": "hit" if cache_key in PROMPT_CACHE_KEYS else "miss",
                    "context_cache": "hit" if stable_key in PROMPT_CACHE_KEYS else "miss",
                },
                duration_ms=prompt_elapsed * 1000,
            )
            dbg(
                f"群 {group_id} 提示词构建完成: messages={len(messages)} 条 "
                f"prompt 前缀指纹={_prefix_fingerprint[:12]}… "
                f"前缀稳定性={'复用' if cache_key in PROMPT_CACHE_KEYS else '变化'} "
                f"稳定上下文={'复用' if stable_key in PROMPT_CACHE_KEYS else '变化'} "
                f"用户 prompt={user_prompt!r}"
            )
            try:
                from ..metrics import record_agent_cache

                record_agent_cache(
                    "prompt", "hit" if cache_key in PROMPT_CACHE_KEYS else "miss"
                )
                # 只观测本地前缀是否稳定；服务商实际缓存 token 由 usage 指标记录。
                record_agent_cache(
                    "context", "hit" if stable_key in PROMPT_CACHE_KEYS else "miss"
                )
            except Exception:  # noqa: BLE001
                dbg_exc(f"群 {group_id} 上报 prompt 缓存指标失败(忽略)")
            for key in (cache_key, stable_key):
                PROMPT_CACHE_KEYS[key] = None
                PROMPT_CACHE_KEYS.move_to_end(key)
            while len(PROMPT_CACHE_KEYS) > PROMPT_CACHE_LIMIT:
                PROMPT_CACHE_KEYS.popitem(last=False)
            fallback_attempted = False
            deadline = time.monotonic() + MAX_TURN_SECONDS
            rounds = 0
            turn_usage: dict[str, int] = {}
            discovered_available: set[str] = set()
            discovered_used: set[str] = set()
            while rounds < round_limit:
                if time.monotonic() > deadline:
                    dbg(
                        f"群 {group_id} 工具循环超过 {MAX_TURN_SECONDS}s 时限,发送收尾提示"
                    )
                    await hooks.send_unless_expired(
                        bot,
                        group_id,
                        TURN_END_NOTICE,
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
                    llm_elapsed = time.monotonic() - llm_started
                    record_agent_phase("llm", llm_elapsed)
                    trace_event(
                        "llm",
                        "模型多模态请求",
                        status="degraded",
                        output={"model": model, "fallback": "vision_caption"},
                        detail="模型不支持当前多模态输入，改用视觉转述后重建 Prompt",
                        duration_ms=llm_elapsed * 1000,
                        round_index=rounds + 1,
                    )
                    dbg(
                        f"群 {group_id} 模型不支持多模态,降级为视觉转述重建提示词(不占轮次)"
                    )
                    fallback_attempted = True
                    user_prompt = f"{normalized.prompt_text()}\n{await hooks.describe_images(group_id, normalized, media_blocks, session, config, cached_captions, media_digests)}"
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
                TOOL_ROUND_COUNT.set(rounds)
                llm_elapsed = time.monotonic() - llm_started
                record_agent_phase("llm", llm_elapsed)
                usage = hooks.accumulate_turn_usage(turn_usage, completion)
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
                        duration_ms=llm_elapsed * 1000,
                        round_index=rounds,
                    )
                    fallback = hooks.deterministic_reply(
                        normalized.plain_text
                    ) or hooks.fallback_notice(group_id)
                    dbg(
                        f"群 {group_id} 第 {rounds} 轮 LLM 返回 None,降级回复={fallback!r}"
                    )
                    await hooks.send_unless_expired(
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
                    duration_ms=llm_elapsed * 1000,
                    round_index=rounds,
                )
                dbg(
                    f"群 {group_id} 第 {rounds}/{round_limit} 轮 LLM 响应: "
                    f"content={content!r} tool_calls={[getattr(getattr(c, 'function', None), 'name', None) for c in tool_calls]}"
                )
                if not tool_calls:
                    if content:
                        await hooks.finalize_reply(
                            bot,
                            group_id,
                            config,
                            session,
                            normalized,
                            content,
                            render_current_turn(current_turn),
                            enqueued_at,
                            message_id,
                        )
                    return
                assistant: dict[str, Any] = {
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": [],
                }
                messages.append(assistant)
                round_sent_message = False
                round_needs_commit = False
                round_commit_tools: list[str] = []
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
                        execution_result = None
                        dbg(f"群 {group_id} 工具参数解析失败: {exc}")
                    else:
                        if (
                            tool_name in VISIBLE_SEND_TOOLS
                            and enqueued_at is not None
                            and is_pending_trigger_expired(enqueued_at)
                        ):
                            result = {
                                "ok": False,
                                "error": "触发消息已过期，取消发送",
                                "expired": True,
                            }
                            execution_result = None
                            dbg(
                                f"群 {group_id} 工具 {tool_name} 发送前触发已过期,取消副作用"
                            )
                        else:
                            if tool_name in VISIBLE_SEND_TOOLS:
                                # 工具发送不走 hooks.send_unless_expired，这里补一次取消，
                                # 避免等待提示压在正文后面发出。
                                hooks.cancel_wait_notice()
                            execution_result = await execute_tool_with_meta(
                                tool_name,
                                args,
                                context=tool_execution_context,
                            )
                            result = execution_result.payload
                            if execution_result.needs_commit:
                                round_needs_commit = True
                                round_commit_tools.append(tool_name)
                            if execution_result.immediate_commit:
                                await hooks.commit_tool_batch(
                                    session,
                                    config,
                                    group_id,
                                    rounds,
                                    [tool_name],
                                    immediate=True,
                                )
                                round_needs_commit = False
                                round_commit_tools.clear()
                    if tool_name == "discover_tools":
                        record_agent_tool_discovery("called")
                    if tool_name in discovered_available and tool_name not in discovered_used:
                        discovered_used.add(tool_name)
                        record_agent_tool_discovery("used")
                    if tool_name == "discover_tools" and bool(result.get("ok")):
                        discovery = result.get("result")
                        discovery_rows = (
                            discovery.get("tools", [])
                            if isinstance(discovery, dict)
                            else []
                        )
                        if discovery_rows:
                            record_agent_tool_discovery("returned")
                        else:
                            record_agent_tool_discovery("empty")
                        for item in discovery_rows:
                            if isinstance(item, dict) and item.get("name"):
                                discovered_tool_names.add(str(item["name"]))
                    tool_elapsed = time.monotonic() - tool_started
                    record_agent_phase("tool", tool_elapsed)
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
                            "ends_turn": hooks.visible_tool_send_ends_turn(result),
                        },
                        duration_ms=tool_elapsed * 1000,
                        round_index=rounds,
                    )
                    dbg(
                        f"群 {group_id} 工具 {tool_name!r} 返回: "
                        f"{json.dumps(result, ensure_ascii=False)}"
                    )
                    if hooks.visible_tool_send_ends_turn(result):
                        round_sent_message = True
                        payload = (
                            result.get("result", {}).get("outbound", {})
                            if isinstance(result.get("result"), dict)
                            else {}
                        )
                        if isinstance(payload, dict):
                            await hooks.persist_bot_reply(
                                session,
                                int(bot.self_id),
                                group_id,
                                hooks.extract_message_id(payload.get("message_id")),
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
                            if config.short_conversation_enabled:
                                mark_bot_reply(
                                    int(bot.self_id),
                                    group_id,
                                    topic=str(config.active_topic or normalized.plain_text or ""),
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
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
                    if round_sent_message:
                        # 一次模型决策最多执行一个用户可见发送动作；避免模型同一轮
                        # 同时调用 send_message/send_forward 连发多条。
                        break
                if round_needs_commit:
                    await hooks.commit_tool_batch(
                        session,
                        config,
                        group_id,
                        rounds,
                        round_commit_tools,
                    )
                if discovered_tool_names and not round_sent_message:
                    selected_tool_names = frozenset(
                        set(selected_tool_names) | discovered_tool_names
                    )
                    tools = build_tool_schemas(
                        capabilities,
                        allow_admin_tools=allow_admin_tools,
                        segment_capabilities=get_segment_capabilities(bot, group_id),
                        privileged_allowlist=set(privileged_allowlist),
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
                    discovered_available.update(loaded_discoveries)
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
            await hooks.send_unless_expired(
                bot,
                group_id,
                TURN_END_NOTICE,
                enqueued_at,
                label="工具收尾",
                message_id=message_id,
            )


__all__ = ["TOOL_ROUND_COUNT", "TurnRuntimeHooks", "run_dialogue_turn"]
