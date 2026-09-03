# ruff: noqa: C901, FAST002, PLR0912, PLR0915, TC001, TID252
"""Agent configuration, memory, privacy, relation and message endpoints."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from nonebot import get_bots
from nonebot_plugin_orm import get_session
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..data_models.agent_memory import AgentPrivacy
from ..data_models.bot_user import BotUser
from ..data_models.group_agent_config import GroupAgentConfig
from ..data_models.group_agent_message import GroupAgentMessage
from ..data_models.user_group import UserGroup
from ..llm import (
    ai_config,
    complete_with_tools_result,
    resolve_llm_request,
    resolve_provider,
)
from ..yawn_agent.capabilities import (
    BotGroupCapabilities,
    capability_runtime_snapshot,
    clear_group_capability_cache,
    get_segment_capabilities,
    probe_group_capabilities,
    user_can_manage_group,
)
from ..yawn_agent.context import build_current_turn, now_beijing, trim_context_messages
from ..yawn_agent.dialogue import _history_message_meta, _load_context
from ..yawn_agent.emotion import emotion_context_state, emotion_public_state
from ..yawn_agent.execution_trace import (
    begin_execution_trace,
    execution_trace_by_id,
    finish_execution_trace,
    recent_execution_trace_summaries,
    trace_event,
)
from ..yawn_agent.persona import (
    persona_behavior,
    persona_behavior_draft,
    persona_editor_profile,
    persona_summary,
    resolve_persona,
    resolve_persona_draft,
)
from ..yawn_agent.proactive import _build_user_prompt, _decide_proactive_reply
from ..yawn_agent.prompt import PROMPT_VERSION, build_messages
from ..yawn_agent.tools import (
    build_tool_schemas,
    dialogue_tool_round_limit,
    select_dialogue_message_segment_types,
    select_dialogue_tool_names,
    tool_permission_snapshot,
)
from . import (
    agent_config_routes as _agent_config_routes,
)
from . import (
    agent_memory_routes as _agent_memory_routes,
)
from . import (
    agent_message_routes as _agent_message_routes,
)
from . import (
    agent_persona_routes as _agent_persona_routes,
)
from . import (
    agent_privacy_routes as _agent_privacy_routes,
)
from . import (
    agent_relation_routes as _agent_relation_routes,
)
from .config import API_PATH
from .deps import AdminReadSession, AdminWriteSession, ok
from .hub import hub
from .route_helpers import check_version, require_group
from .route_models import (
    AgentDebugRunBody,
)
from .service import (
    agent_diagnostics,
)


def _sync_split_route_runtime(module: Any) -> None:
    """保持旧 ``webui.agent`` monkeypatch 入口对直接函数调用仍然有效。"""

    module.get_session = get_session
    module.require_group = require_group
    module.check_version = check_version
    module.hub = hub
    module.now_beijing = now_beijing
    module.IntegrityError = IntegrityError


async def get_memory_subjects(*args: Any, **kwargs: Any) -> dict[str, Any]:
    _sync_split_route_runtime(_agent_memory_routes)
    return await _agent_memory_routes.get_memory_subjects(*args, **kwargs)


async def get_relation_graph(*args: Any, **kwargs: Any) -> dict[str, Any]:
    _sync_split_route_runtime(_agent_relation_routes)
    return await _agent_relation_routes.get_relation_graph(*args, **kwargs)


async def create_relation(*args: Any, **kwargs: Any) -> dict[str, Any]:
    _sync_split_route_runtime(_agent_relation_routes)
    return await _agent_relation_routes.create_relation(*args, **kwargs)


async def update_relation(*args: Any, **kwargs: Any) -> dict[str, Any]:
    _sync_split_route_runtime(_agent_relation_routes)
    return await _agent_relation_routes.update_relation(*args, **kwargs)


router = APIRouter()
_debug_router = APIRouter(prefix=API_PATH)
_AGENT_DEBUG_RUNS = asyncio.Semaphore(2)
_DEBUG_HISTORY_LIMIT = 40
_DEBUG_MEMORY_LIMIT = 30
_DEBUG_RELATION_LIMIT = 20
_DEBUG_TIMEOUT_SECONDS = 30.0








@_debug_router.get("/agent/groups/{group_id}/diagnostics")
async def get_agent_diagnostics(
    group_id: int, _session: AdminReadSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        return ok(await agent_diagnostics(db, group_id))


@_debug_router.get("/agent/groups/{group_id}/capabilities")
async def get_agent_capabilities(
    group_id: int, _session: AdminReadSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
    bots = _debug_bots()
    if not bots:
        return ok(
            {
                "botId": None,
                "groupId": str(group_id),
                "offline": True,
                "action": {
                    "cached": False,
                    "role": "offline",
                    "canManage": False,
                    "actions": [],
                    "degraded": True,
                    "lastError": "bot_offline",
                    "probedAt": None,
                    "cacheRemainingSeconds": 0,
                },
                "segments": [],
            }
        )
    bot = bots[0]
    await probe_group_capabilities(bot, group_id)
    return ok({"offline": False, **capability_runtime_snapshot(bot, group_id)})


@_debug_router.get("/agent/groups/{group_id}/execution-traces")
async def get_agent_execution_traces(
    group_id: int,
    _session: AdminReadSession,
    status: str | None = Query(default=None, max_length=24),
) -> dict[str, Any]:
    """返回当前进程内最近真实 Agent 执行的轻量摘要。

    Trace 是短生命周期诊断缓冲，不从数据库重建，也不携带原始媒体 URL、
    本机路径或裸 OneBot payload。
    """

    async with get_session() as db:
        await require_group(db, group_id)
    return ok(recent_execution_trace_summaries(group_id, status=status))


@_debug_router.get("/agent/groups/{group_id}/execution-traces/{trace_id}")
async def get_agent_execution_trace(
    group_id: int, trace_id: str, _session: AdminReadSession
) -> dict[str, Any]:
    """返回一条 Trace 的完整事件详情。"""

    async with get_session() as db:
        await require_group(db, group_id)
    trace = execution_trace_by_id(group_id, trace_id)
    if trace is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "执行 Trace 不存在")
    return ok(trace)


@_debug_router.post("/agent/groups/{group_id}/capabilities/refresh")
async def refresh_agent_capabilities(
    group_id: int, _session: AdminWriteSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
    bots = _debug_bots()
    if not bots:
        raise HTTPException(status.HTTP_409_CONFLICT, "Bot 当前离线，无法重新探测能力")
    bot = bots[0]
    clear_group_capability_cache(bot, group_id)
    await probe_group_capabilities(bot, group_id, refresh=True)
    return ok({"offline": False, **capability_runtime_snapshot(bot, group_id)})












# 成员画像面板的成员索引口径：profile/core/manual 都是按成员沉淀的可读事实
# （对话注入同口径），群级行（subject=0）与 summary 不参与。








# 手动整理是重操作（LLM 摘要可达数十秒）：后台执行并按群防重复触发。
_compact_inflight: set[int] = set()
_bg_tasks: set[asyncio.Task[None]] = set()




































def _debug_context_stats(context: dict[str, Any]) -> dict[str, Any]:
    messages = list(context.get("messages") or [])
    memories = list(context.get("memories") or [])
    relations = list(context.get("relations") or [])
    media_summary_count = sum(
        1
        for item in messages
        if item.get("media_types") or item.get("forward_nodes")
    )
    return {
        "history": {
            "count": len(messages),
            "limit": _DEBUG_HISTORY_LIMIT,
            "characters": sum(len(str(item.get("text") or "")) for item in messages),
            "limitReached": len(messages) >= _DEBUG_HISTORY_LIMIT,
        },
        "memory": {
            "count": len(memories),
            "limit": _DEBUG_MEMORY_LIMIT,
            "characters": sum(len(str(item.get("content") or "")) for item in memories),
            "limitReached": len(memories) >= _DEBUG_MEMORY_LIMIT,
        },
        "relation": {
            "count": len(relations),
            "limit": _DEBUG_RELATION_LIMIT,
            "characters": sum(len(str(item)) for item in relations),
            "limitReached": len(relations) >= _DEBUG_RELATION_LIMIT,
        },
        "memberCount": len(list(context.get("members") or [])),
        "mediaSummaryCount": media_summary_count,
    }


def _debug_tool_calls(message: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for call in list(getattr(message, "tool_calls", None) or []):
        function = getattr(call, "function", None)
        raw_arguments = str(getattr(function, "arguments", "") or "")
        try:
            arguments: object = json.loads(raw_arguments)
        except (TypeError, ValueError):
            arguments = raw_arguments
        calls.append(
            {
                "name": str(getattr(function, "name", "") or ""),
                "arguments": arguments,
            }
        )
    return calls


def _debug_model_payload(result: Any, mode: str) -> dict[str, Any]:
    message = result.message
    content = str(getattr(message, "content", "") or "").strip()
    payload: dict[str, Any] = {
        "outcome": result.outcome,
        "text": content,
        "toolCalls": _debug_tool_calls(message),
        "finishReason": result.finish_reason,
        "usage": {
            "promptTokens": result.prompt_tokens,
            "completionTokens": result.completion_tokens,
            "cachedTokens": result.cached_tokens,
            "cacheMissTokens": result.cache_miss_tokens,
        },
        "durationMs": round(float(result.duration_ms), 1),
    }
    if mode != "dialogue" and content:
        decision = _decide_proactive_reply(content)
        payload["decision"] = {
            "action": decision.action,
            "targetUserId": (
                str(decision.target_user_id)
                if decision.target_user_id is not None
                else None
            ),
            "text": decision.text,
            "topic": decision.topic,
            "reason": decision.reason,
            "confidence": decision.confidence,
            "segments": list(decision.segments),
        }
    return payload


def _debug_bots() -> list[Any]:
    try:
        return list(get_bots().values())
    except Exception:  # noqa: BLE001
        return []


async def _run_debug_completion(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    task: str,
    max_tokens: int,
) -> Any:
    async with _AGENT_DEBUG_RUNS:
        return await complete_with_tools_result(  # pyright: ignore[reportArgumentType]
            messages,  # pyright: ignore[reportArgumentType]
            tools,  # pyright: ignore[reportArgumentType]
            task=task,  # pyright: ignore[reportArgumentType]
            max_tokens=max_tokens,
            timeout=_DEBUG_TIMEOUT_SECONDS,
        )


@_debug_router.post("/agent/groups/{group_id}/debug/run")
async def run_agent_debug(
    group_id: int, body: AgentDebugRunBody, _session: AdminWriteSession
) -> dict[str, Any]:
    """构建线上同源提示词；可选调用模型，但不执行工具、发送或落库。"""

    async with get_session() as db:
        await require_group(db, group_id)
        config = await db.get(GroupAgentConfig, group_id)
        if config is None:
            config = GroupAgentConfig(group_id=group_id)

        source: GroupAgentMessage | None = None
        if body.message_id is not None:
            source = (
                (
                    await db.execute(
                        select(GroupAgentMessage)
                        .where(
                            GroupAgentMessage.group_id == group_id,
                            GroupAgentMessage.message_id == body.message_id,
                            GroupAgentMessage.expires_at.is_not(None),
                            GroupAgentMessage.expires_at >= now_beijing(),
                        )
                        .order_by(GroupAgentMessage.id.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .one_or_none()
            )
            if source is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "消息不存在或已过期")
            actor_user_id = int(source.user_id)
            actor_name = source.sender_name
            actor_role = source.role
            actor_title = source.title
            received_at = source.received_at
        else:
            actor_user_id = int(body.actor_user_id or 0)
            member_row = (
                await db.execute(
                    select(UserGroup, BotUser.nickname)
                    .outerjoin(BotUser, BotUser.user_id == UserGroup.user_id)
                    .where(
                        UserGroup.group_id == group_id,
                        UserGroup.user_id == actor_user_id,
                    )
                )
            ).first()
            if member_row is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "模拟发言人不在当前群")
            member, nickname = member_row
            actor_name = member.group_nickname or nickname
            actor_role = member.role
            actor_title = member.title
            received_at = now_beijing()

        opted_out = set(
            (
                await db.execute(
                    select(AgentPrivacy.user_id).where(
                        AgentPrivacy.group_id == group_id,
                        AgentPrivacy.opted_out.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        if actor_user_id in opted_out:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "消息不存在或成员已退出记忆")

        bots = _debug_bots()
        preferred_bot_id = int(source.bot_id) if source is not None else None
        bot = next(
            (
                item
                for item in bots
                if preferred_bot_id is not None
                and int(str(getattr(item, "self_id", 0) or 0)) == preferred_bot_id
            ),
            bots[0] if bots else None,
        )
        bot_id = (
            int(str(getattr(bot, "self_id", 0) or 0))
            if bot is not None
            else preferred_bot_id
        )

        if source is not None:
            meta = _history_message_meta(source)
            mentions = [
                int(user_id)
                for user_id in meta.get("mentions", [])
                if int(user_id) not in opted_out
            ]
            reply_chain = [
                item
                for item in list(source.reply_chain or [])
                if isinstance(item, dict)
                and int(str(item.get("user_id") or 0)) not in opted_out
            ]
            trigger = "history_replay"
            if bot_id is not None and bot_id in mentions:
                trigger = "mention"
            elif (
                reply_chain
                and int(str(reply_chain[0].get("user_id") or 0)) == bot_id
            ):
                trigger = "reply_to_bot"
            current_turn = build_current_turn(
                message_id=int(source.message_id),
                user_id=actor_user_id,
                name=actor_name,
                role=actor_role,
                title=actor_title,
                content=str(source.normalized_text or "[媒体消息]"),
                mentions=mentions,
                reply_chain=reply_chain,
                trigger=trigger,
                received_at=received_at,
                media_refs=list(source.media_refs or []),
                forward_nodes=len(list(source.forward_tree or [])),
            )
        else:
            current_turn = build_current_turn(
                message_id=None,
                user_id=actor_user_id,
                name=actor_name,
                role=actor_role,
                title=actor_title,
                content=str(body.text or ""),
                trigger="debug_simulation",
                received_at=received_at,
            )

        debug_trace = begin_execution_trace(
            group_id,
            mode=body.mode,
            source="debug",
            actor_user_id=actor_user_id,
            message_id=current_turn.message_id,
        )
        trace_event(
            "intake",
            "载入调试场景",
            output={
                "trigger": current_turn.trigger,
                "text_chars": len(current_turn.content),
                "media": list(current_turn.media),
                "reply_to": current_turn.reply_to,
                "forward_nodes": current_turn.forward_nodes,
            },
            trace=debug_trace,
        )

        focus_ids = [
            current_turn.user_id,
            *(user_id for user_id in current_turn.mentions if user_id != bot_id),
        ]
        if (
            current_turn.reply_to
            and current_turn.reply_to.get("user_id")
            and int(str(current_turn.reply_to["user_id"])) != bot_id
        ):
            focus_ids.append(int(str(current_turn.reply_to["user_id"])))
        task = "agent_dialogue" if body.mode == "dialogue" else "agent_proactive"
        route = resolve_llm_request(task)
        context_selection: list[dict[str, Any]] = []
        context_budget: list[dict[str, Any]] = []
        context_started = time.monotonic()
        context = await _load_context(
            db,
            group_id,
            config,
            bot_id,
            focus_user_ids=focus_ids,
            query_text=current_turn.content if body.mode == "dialogue" else None,
            compact_history=body.mode != "dialogue",
            message_cutoff=received_at,
            include_active_profiles=True,
            exclude_message_id=(
                int(source.message_id)
                if source is not None and body.mode == "dialogue"
                else None
            ),
            reference_at=received_at,
            selection_trace=context_selection,
            budget_trace=context_budget,
            context_model=route.model,
            completion_reserve=(
                800
                if body.mode == "dialogue"
                else max(2048, int(ai_config.ai_max_tokens))
            ),
        )
        trace_event(
            "context",
            "上下文筛选与 Token 装箱",
            input={"focus_user_ids": focus_ids, "model": route.model},
            output={
                "messages": len(list(context.get("messages") or [])),
                "members": len(list(context.get("members") or [])),
                "memories": len(list(context.get("memories") or [])),
                "relations": len(list(context.get("relations") or [])),
                "selection_candidates": len(context_selection),
                "budget_components": len(context_budget),
            },
            duration_ms=(time.monotonic() - context_started) * 1000,
            trace=debug_trace,
        )
        if source is None and body.mode != "dialogue":
            context["messages"] = trim_context_messages(
                [
                    *list(context.get("messages") or []),
                    {
                        "message_id": None,
                        "user_id": current_turn.user_id,
                        "name": current_turn.name,
                        "role": current_turn.role,
                        "title": current_turn.title,
                        "text": current_turn.content,
                        "minutes_ago": 0,
                        "topic_break_before": False,
                        "mentions": list(current_turn.mentions),
                        "reply_to": current_turn.reply_to,
                        "media_types": [],
                        "forward_nodes": 0,
                    },
                ]
            )

        tools: list[dict[str, Any]] = []
        tool_permissions: list[dict[str, Any]] = []
        capability_started = time.monotonic()
        if body.mode == "dialogue":
            privileged_allowlist = set(config.tool_allowlist or [])
            selected_tool_names = select_dialogue_tool_names(
                body.text,
                allow_admin_tools=False,
            )
            message_segment_types = select_dialogue_message_segment_types(body.text)
            if bot is None:
                capabilities = BotGroupCapabilities(
                    role="offline", can_manage=False, actions=frozenset()
                )
                tools = build_tool_schemas(
                    capabilities,
                    privileged_allowlist=privileged_allowlist,
                    include_names=selected_tool_names,
                    message_segment_types=(
                        message_segment_types
                        if "send_message" in selected_tool_names
                        else None
                    ),
                )
                tool_permissions = tool_permission_snapshot(
                    capabilities,
                    privileged_allowlist=privileged_allowlist,
                )
            else:
                capabilities = await probe_group_capabilities(bot, group_id)
                allow_privileged_tools = await user_can_manage_group(
                    bot, group_id, actor_user_id
                )
                selected_tool_names = select_dialogue_tool_names(
                    body.text,
                    allow_admin_tools=allow_privileged_tools,
                )
                message_segment_types = select_dialogue_message_segment_types(body.text)
                tools = build_tool_schemas(
                    capabilities,
                    allow_admin_tools=allow_privileged_tools,
                    segment_capabilities=get_segment_capabilities(bot, group_id),
                    privileged_allowlist=privileged_allowlist,
                    include_names=selected_tool_names,
                    message_segment_types=(
                        message_segment_types
                        if "send_message" in selected_tool_names
                        else None
                    ),
                )
                tool_permissions = tool_permission_snapshot(
                    capabilities,
                    allow_admin_tools=allow_privileged_tools,
                    privileged_allowlist=privileged_allowlist,
                )
            trace_event(
                "capability",
                "协议能力与工具权限计算",
                output={
                    "tool_count": len(tools),
                    "round_limit": dialogue_tool_round_limit(selected_tool_names),
                    "message_segment_types": sorted(message_segment_types),
                    "selected_tools": sorted(selected_tool_names),
                    "exposed_tools": [
                        item["name"] for item in tool_permissions if item.get("exposed")
                    ],
                    "blocked_tools": [
                        {"name": item["name"], "reason": item["reason"]}
                        for item in tool_permissions
                        if not item.get("exposed")
                    ],
                },
                duration_ms=(time.monotonic() - capability_started) * 1000,
                trace=debug_trace,
            )
        else:
            trace_event(
                "capability",
                "工具权限计算",
                status="skipped",
                detail="主动/暖场调试不向模型暴露对话工具",
                trace=debug_trace,
            )

        persisted_editor = persona_editor_profile(config)
        applied_behavior = (
            persona_behavior_draft(config, body.persona_draft)
            if body.persona_draft is not None
            else persona_behavior(config)
        )
        applied_expressiveness = (
            body.persona_draft.expressiveness
            if body.persona_draft is not None
            else persisted_editor.expressiveness
        )
        emotion_raw = (
            config.emotion_state if isinstance(config.emotion_state, dict) else {}
        )
        persisted_emotion = emotion_public_state(
            emotion_raw, expressiveness=persisted_editor.expressiveness
        )
        applied_emotion = emotion_public_state(
            emotion_raw, expressiveness=applied_expressiveness
        )
        draft_emotion_context = emotion_context_state(
            emotion_raw, expressiveness=applied_expressiveness
        )
        context = dict(context)
        if draft_emotion_context:
            context["emotion_state"] = draft_emotion_context
        else:
            context.pop("emotion_state", None)
        user_prompt = (
            current_turn.content
            if body.mode == "dialogue"
            else _build_user_prompt(
                body.mode,
                config,
                turn=2 if body.mode == "followup" else 1,
                behavior=applied_behavior,
            )
        )
        applied_persona = (
            resolve_persona_draft(config, body.persona_draft)
            if body.persona_draft is not None
            else resolve_persona(config)
        )
        prompt_started = time.monotonic()
        prompt_messages, _fingerprint = build_messages(
            persona=applied_persona,
            tools=tools,
            context=context,
            user_prompt=user_prompt,
            current_turn=current_turn if body.mode == "dialogue" else None,
        )
        trace_event(
            "prompt",
            "Prompt 构建",
            input={
                "tool_count": len(tools),
                "mode": body.mode,
                "persona_source": (
                    "draft" if body.persona_draft is not None else "persisted"
                ),
                "persona_behavior": applied_behavior.as_dict(),
            },
            output={
                "message_count": len(prompt_messages),
                "prompt_version": PROMPT_VERSION,
                "fingerprint": _fingerprint[:12],
            },
            duration_ms=(time.monotonic() - prompt_started) * 1000,
            trace=debug_trace,
        )
        base_url, api_key = resolve_provider(route.provider)
        result_payload: dict[str, Any] | None = None
        if body.run_model:
            model_started = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    _run_debug_completion(
                        prompt_messages,
                        tools,
                        task=task,
                        max_tokens=(
                            800
                            if body.mode == "dialogue"
                            else max(2048, int(ai_config.ai_max_tokens))
                        ),
                    ),
                    timeout=_DEBUG_TIMEOUT_SECONDS,
                )
                result_payload = _debug_model_payload(result, body.mode)
                trace_event(
                    "llm",
                    "真实模型试跑",
                    status=(
                        "success"
                        if result_payload.get("outcome") == "success"
                        else "degraded"
                    ),
                    output={
                        "outcome": result_payload.get("outcome"),
                        "finish_reason": result_payload.get("finishReason"),
                        "content_chars": len(str(result_payload.get("text") or "")),
                        "tool_calls": [
                            item.get("name")
                            for item in list(result_payload.get("toolCalls") or [])
                            if isinstance(item, dict)
                        ],
                        "usage": result_payload.get("usage"),
                    },
                    duration_ms=(time.monotonic() - model_started) * 1000,
                    trace=debug_trace,
                )
                for tool_call in list(result_payload.get("toolCalls") or []):
                    if not isinstance(tool_call, dict):
                        continue
                    trace_event(
                        "tool",
                        f"工具意图 {tool_call.get('name') or '[unknown]'}",
                        status="planned",
                        input={"arguments": tool_call.get("arguments")},
                        output={"executed": False},
                        detail="调试模式禁止执行工具副作用",
                        trace=debug_trace,
                    )
                if result_payload.get("toolCalls"):
                    trace_event(
                        "outbound",
                        "发送阶段",
                        status="skipped",
                        output={"sent": False},
                        detail="调试模式不发送群消息，也不执行工具内发送",
                        trace=debug_trace,
                    )
            except asyncio.TimeoutError:
                result_payload = {
                    "outcome": "timeout",
                    "text": "",
                    "toolCalls": [],
                    "finishReason": None,
                    "usage": {
                        "promptTokens": None,
                        "completionTokens": None,
                        "cachedTokens": None,
                        "cacheMissTokens": None,
                    },
                    "durationMs": _DEBUG_TIMEOUT_SECONDS * 1000,
                }
                trace_event(
                    "llm",
                    "真实模型试跑",
                    status="failed",
                    output={"outcome": "timeout"},
                    detail="超过调试请求时限",
                    duration_ms=(time.monotonic() - model_started) * 1000,
                    trace=debug_trace,
                )
        else:
            trace_event(
                "llm",
                "模型调用",
                status="skipped",
                output={"run_model": False},
                detail="本次仅生成执行快照，未请求模型",
                trace=debug_trace,
            )

        trace_event(
            "state",
            "状态写入",
            status="skipped",
            output={"database_mutated": False},
            detail="调试执行全程只读，不修改 Agent 状态",
            trace=debug_trace,
        )

        stats = _debug_context_stats(context)
        warnings: list[str] = []
        if (
            current_turn.media_types
            or current_turn.forward_nodes
            or stats["mediaSummaryCount"]
        ):
            warnings.append("媒体和合并转发仅以安全摘要展示，未参与模型重放")
        if bot is None:
            warnings.append("当前没有在线 Bot，工具列表按离线能力收敛")
        trace_outcome = (
            str(result_payload.get("outcome"))
            if result_payload is not None
            else "snapshot"
        )
        trace_event(
            "turn",
            "调试执行结束",
            output={"outcome": trace_outcome, "warnings": len(warnings)},
            trace=debug_trace,
        )
        finish_execution_trace(
            debug_trace,
            outcome=trace_outcome,
            status=("failed" if trace_outcome == "timeout" else "completed"),
            store=False,
        )
        payload = {
            "promptVersion": PROMPT_VERSION,
            "mode": body.mode,
            "persona": {
                "source": "draft" if body.persona_draft is not None else "persisted",
                "persistedSummary": persona_summary(config),
                "persistedProfile": persisted_editor.model_dump(by_alias=True),
                "persistedBehavior": persona_behavior(config).as_dict(),
                "appliedBehavior": applied_behavior.as_dict(),
                "persistedEmotion": persisted_emotion,
                "appliedEmotion": applied_emotion,
                "appliedProfile": (
                    body.persona_draft.model_dump(by_alias=True)
                    if body.persona_draft is not None
                    else persisted_editor.model_dump(by_alias=True)
                ),
            },
            "currentTurn": current_turn.as_dict(),
            "context": context,
            "contextSelection": context_selection,
            "contextBudget": context_budget,
            "promptMessages": prompt_messages,
            "tools": tools,
            "toolPermissions": tool_permissions,
            "route": {
                "task": task,
                "profile": route.profile,
                "provider": route.provider,
                "model": route.model,
                "thinking": route.thinking,
                "multimodal": route.multimodal,
                "configured": bool(
                    base_url.strip() and api_key and route.model.strip()
                ),
            },
            "stats": stats,
            "warnings": warnings,
            "result": result_payload,
            "executionTrace": debug_trace.as_dict(),
        }
    return ok(payload)




for _route_group in (
    _agent_config_routes.router.routes,
    _agent_persona_routes.router.routes,
    _agent_memory_routes.router.routes,
    _agent_relation_routes.router.routes,
    _agent_message_routes.router.routes,
    _agent_privacy_routes.router.routes,
    _debug_router.routes,
):
    router.routes.extend(_route_group)
