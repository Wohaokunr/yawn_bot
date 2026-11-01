# ruff: noqa: C901, F401, TC001, TID252
"""Split Agent WebUI route module."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, status
from nonebot import get_bots, logger
from nonebot_plugin_orm import get_session
from sqlalchemy import BigInteger, cast, exists, func, or_, select
from sqlalchemy.exc import IntegrityError

from ..data_models.agent_memory import AgentMemory, AgentPrivacy, AgentRelation
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
from ..yawn_agent.config_store import agent_runtime_enabled, set_agent_runtime_enabled
from ..yawn_agent.context import build_current_turn, now_beijing, trim_context_messages
from ..yawn_agent.conversation import close_group_conversations
from ..yawn_agent.dialogue import _history_message_meta, _load_context
from ..yawn_agent.emotion import emotion_context_state, emotion_public_state
from ..yawn_agent.execution_trace import (
    begin_execution_trace,
    execution_trace_by_id,
    finish_execution_trace,
    recent_execution_trace_summaries,
    trace_event,
)
from ..yawn_agent.memory import (
    compact_group_memory,
    delete_group_memories,
    delete_member_memories,
    is_memory_compacting,
    normalize_relation_type,
    rebuild_group_memories,
    record_memory_failure,
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
from .agent_route_helpers import is_guest_view as _is_guest_view
from .agent_route_helpers import memory_privacy_clauses as _memory_privacy_clauses
from .config import API_PATH
from .deps import AdminReadSession, AdminWriteSession, GroupViewSession, ok, page_params
from .hub import hub
from .route_helpers import check_version, optimistic_write_lock, require_group
from .route_models import (
    AgentConfigPatch,
    AgentDebugRunBody,
    MemoryCreateBody,
    MemoryPatchBody,
    PersonaPatch,
    PrivacyPatchBody,
    RelationCreateBody,
    RelationPatchBody,
)
from .service import (
    RELATION_GRAPH_LIMIT,
    agent_config_version,
    agent_diagnostics,
    agent_memory_status,
    delete_one_memory,
    group_feature_rows,
    iso,
    load_relation_graph,
    page_meta,
    serialize_agent_config,
    serialize_agent_message,
    serialize_guest_memory,
    serialize_guest_relation,
    serialize_memory,
    serialize_persona,
    serialize_relation,
)

router = APIRouter(prefix=API_PATH)


@router.get("/agent/groups/{group_id}/config")
async def get_agent_config(group_id: int, _session: AdminReadSession) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        row = await db.get(GroupAgentConfig, group_id)
        result = serialize_agent_config(row, group_id)
        if row is not None:
            result["enabled"] = await agent_runtime_enabled(db, group_id, config=row)
        else:
            features = await group_feature_rows(db, group_id)
            result["enabled"] = bool(
                next(item for item in features if item["key"] == "group_agent")[
                    "effective"
                ]
            )
        return ok(result)


@router.patch("/agent/groups/{group_id}/config")
async def patch_agent_config(
    group_id: int, body: AgentConfigPatch, _session: AdminWriteSession
) -> dict[str, Any]:
    async with optimistic_write_lock, get_session() as db:
        await require_group(db, group_id)
        row = await db.get(GroupAgentConfig, group_id)
        check_version(row, body.version, version_of=agent_config_version)
        if row is None:
            row = GroupAgentConfig(group_id=group_id)
            db.add(row)
        updates = body.model_dump(exclude_unset=True, exclude={"version"})
        legacy_mode = updates.pop("trigger_mode", None)
        if legacy_mode is not None:
            legacy_flags = {
                "mention_only": {
                    "reply_trigger_enabled": False,
                    "explicit_wakeup_enabled": False,
                    "proactive_enabled": False,
                },
                "mention_or_reply": {
                    "reply_trigger_enabled": True,
                    "explicit_wakeup_enabled": False,
                    "proactive_enabled": False,
                },
                "explicit_wakeup": {
                    "reply_trigger_enabled": False,
                    "explicit_wakeup_enabled": True,
                    "proactive_enabled": False,
                },
                "mention_or_proactive": {
                    "reply_trigger_enabled": True,
                    "explicit_wakeup_enabled": True,
                    "proactive_enabled": True,
                },
            }[legacy_mode]
            # Explicit new fields win when old and new clients mix payloads.
            for field, value in legacy_flags.items():
                updates.setdefault(field, value)
            row.trigger_mode = legacy_mode
        if not updates and legacy_mode is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "没有可更新的字段"
            )
        for field, value in updates.items():
            if field == "enabled":
                continue
            setattr(row, field, value)
        if "enabled" in updates:
            await set_agent_runtime_enabled(
                db,
                group_id,
                enabled=bool(updates["enabled"]),
                config=row,
            )
        await db.commit()
        await db.refresh(row)
        result = serialize_agent_config(row, group_id)
        if updates.get("enabled") is False:
            close_group_conversations(group_id, reason="WebUI 关闭 Agent 总开关")
        elif updates.get("short_conversation_enabled") is False:
            close_group_conversations(group_id, reason="WebUI 关闭短会话续聊")
    await hub.notify_change("agent_config", str(group_id), group_id=group_id)
    if "enabled" in updates:
        await hub.notify_change(
            "group_feature", f"{group_id}:group_agent", group_id=group_id
        )
    return ok(result)


@router.get("/agent/vision/status")
async def get_agent_vision_status(_session: AdminReadSession) -> dict[str, Any]:
    """识图诊断：现在走哪条识图路线、最近 24 小时取图成败与原因、对应的改法。

    只返回原因码、计数和非敏感配置，不含任何图片链接、本机路径或密钥。
    """

    from ..yawn_agent import media_fetch, media_memory
    from ..yawn_agent.turn_media import vision_route

    route = vision_route()
    stats = media_fetch.stats()
    reasons = [
        {
            "code": code,
            "count": count,
            "message": media_fetch.reason_text(code)[0],
            "hint": media_fetch.reason_text(code)[1],
        }
        for code, count in sorted(
            stats["reasons"].items(), key=lambda item: item[1], reverse=True
        )
    ]
    advice: list[str] = []
    if route.get("known_text_only"):
        advice.append(
            f"对话模型 {route['dialogue_model']} 只收文字，图片要靠识图模型先转述；"
            "如果它其实支持图片，把 AI_DEFAULT_MULTIMODAL 设为 supported。"
        )
    if route["route"] == "none":
        advice.append(
            "对话模型不支持图片，也没有配置识图模型：在环境配置里填 AI_VISION_MODEL，"
            "或把 AI_DEFAULT_MULTIMODAL 设为 supported 并换成支持图片的对话模型。"
        )
    if route["learned_unsupported"]:
        advice.append(
            "对话模型最近拒收了图片，接下来一小时自动改走识图转述；"
            "如果它其实支持图片，检查服务商是否开通了视觉能力。"
        )
    if not media_fetch.host_patterns():
        advice.append("AGENT_MEDIA_ALLOWED_HOSTS 是空的，群图片一律不会下载。")
    advice.extend(
        item["hint"]
        for item in reasons
        if item["code"] in {"protocol_local_only", "tls_failed", "host_blocked"}
    )
    return ok(
        {
            "route": route["route"],
            "dialogueProvider": route["dialogue_provider"],
            "dialogueModel": route["dialogue_model"],
            "dialogueMultimodal": route["dialogue_multimodal"],
            "learnedUnsupported": route["learned_unsupported"],
            "visionConfigured": route["vision_configured"],
            "visionProvider": route["vision_provider"],
            "visionModel": route["vision_model"],
            "stats": {
                "total": stats["total"],
                "loaded": stats["loaded"],
                "failed": stats["failed"],
                "via": stats["via"],
            },
            "reasons": reasons,
            "advice": list(dict.fromkeys(advice)),
            "settings": {
                "allowedHosts": list(media_fetch.host_patterns()),
                "httpFallback": bool(
                    getattr(ai_config, "agent_media_http_fallback", True)
                ),
                "urlPassthrough": bool(
                    getattr(ai_config, "agent_media_url_passthrough", True)
                ),
                "autoCaption": media_memory.auto_caption_mode(),
            },
            "captionMemory": media_memory.size(),
        }
    )
