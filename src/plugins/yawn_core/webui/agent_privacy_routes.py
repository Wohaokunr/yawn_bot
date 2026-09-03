# ruff: noqa: F401, FAST002, TC001, TID252
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
from ..yawn_agent.persona import (
    apply_persona_editor_profile,
    persona_behavior,
    persona_behavior_draft,
    persona_editor_profile,
    persona_summary,
    reset_persona,
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
from .agent_route_helpers import is_guest_view as _is_guest_view
from .agent_route_helpers import memory_privacy_clauses as _memory_privacy_clauses
from .config import API_PATH
from .deps import AdminReadSession, AdminWriteSession, GroupViewSession, ok, page_params
from .hub import hub
from .route_helpers import check_version, require_group
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

@router.get("/agent/groups/{group_id}/privacy")
async def get_privacy(
    group_id: int,
    _session: AdminReadSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=100),
) -> dict[str, Any]:
    page, page_size = page_params(page, page_size)
    clause = AgentPrivacy.group_id == group_id
    async with get_session() as db:
        await require_group(db, group_id)
        total = int(
            await db.scalar(
                select(func.count()).select_from(AgentPrivacy).where(clause)
            )
            or 0
        )
        rows = list(
            (
                await db.execute(
                    select(AgentPrivacy)
                    .where(clause)
                    .order_by(AgentPrivacy.updated_at.desc(), AgentPrivacy.user_id)
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
    return ok(
        [
            {
                "groupId": str(row.group_id),
                "userId": str(row.user_id),
                "optedOut": row.opted_out,
                "updatedAt": iso(row.updated_at),
            }
            for row in rows
        ],
        page_meta(page, page_size, total),
    )

@router.patch("/agent/groups/{group_id}/privacy/{user_id}")
async def patch_privacy(
    group_id: int, user_id: int, body: PrivacyPatchBody, _session: AdminWriteSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        privacy = await db.get(AgentPrivacy, (group_id, user_id))
        if privacy is None:
            privacy = AgentPrivacy(group_id=group_id, user_id=user_id)
            db.add(privacy)
        privacy.opted_out = body.opted_out
        if body.opted_out:
            # 与 /Agent隐私 命令同语义：退出即连带清除该成员已沉淀的记忆。
            await delete_member_memories(db, group_id, user_id)
        else:
            await db.commit()
        await db.refresh(privacy)
        result = {
            "groupId": str(privacy.group_id),
            "userId": str(privacy.user_id),
            "optedOut": privacy.opted_out,
            "updatedAt": iso(privacy.updated_at),
        }
    await hub.notify_change("agent_privacy", f"{group_id}:{user_id}")
    return ok(result)
