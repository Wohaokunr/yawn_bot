# ruff: noqa: F401, TC001, TID252, E501
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
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

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
    PersonaCard,
    apply_persona_card,
    persona_override_reason,
    reset_persona,
)
from ..yawn_agent.persona_library import (
    PersonaLibraryError,
    create_template,
    delete_template,
    get_template,
    list_templates,
    refresh_global_persona,
    refresh_private_library,
    serialize_template,
    template_card,
    update_template,
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
    PersonaApplyTemplateBody,
    PersonaPatch,
    PersonaTemplateCreateBody,
    PersonaTemplatePatchBody,
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
    persona_content_version,
    serialize_agent_config,
    serialize_agent_message,
    serialize_guest_memory,
    serialize_guest_relation,
    serialize_memory,
    serialize_persona,
    serialize_relation,
)

router = APIRouter(prefix=API_PATH)


def _new_config(group_id: int) -> GroupAgentConfig:
    # 列默认值只在 INSERT 时生效；新建行显式给出人设字段初值，
    # 否则 persona_version 为 None，``+= 1`` 会直接 500。
    return GroupAgentConfig(
        group_id=group_id,
        persona_enabled=False,
        persona_version=1,
        persona_profile={},
    )


def _require_safe_card(card: PersonaCard) -> None:
    reason = persona_override_reason(card)
    if reason is not None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{reason}；人设只描述角色，系统规则不能被人设覆盖",
        )


def _library_http_error(exc: PersonaLibraryError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


async def _save_group_card(
    group_id: int,
    card: PersonaCard,
    *,
    version: str | None,
    enabled: bool,
) -> dict[str, Any]:
    changed = False
    async with optimistic_write_lock, get_session() as db:
        await require_group(db, group_id)
        row = await db.get(GroupAgentConfig, group_id)
        check_version(row, version, version_of=persona_content_version)
        if row is None:
            row = _new_config(group_id)
            db.add(row)
        mutation = apply_persona_card(row, card, enabled=enabled)
        if mutation.semantic_changed:
            row.persona_version = int(row.persona_version or 1) + 1
            changed = True
        if mutation.storage_changed:
            await db.commit()
            await db.refresh(row)
        result = serialize_persona(row, group_id)
    if changed:
        await hub.notify_change("agent_persona", str(group_id), group_id=group_id)
    return result


@router.get("/agent/groups/{group_id}/persona")
async def get_agent_persona(
    group_id: int, _session: AdminReadSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        row = await db.get(GroupAgentConfig, group_id)
        return ok(serialize_persona(row, group_id))


@router.put("/agent/groups/{group_id}/persona")
async def put_agent_persona(
    group_id: int, body: PersonaPatch, _session: AdminWriteSession
) -> dict[str, Any]:
    _require_safe_card(body.card)
    return ok(
        await _save_group_card(
            group_id, body.card, version=body.version, enabled=body.enabled
        )
    )


@router.post("/agent/groups/{group_id}/persona/apply-template")
async def apply_agent_persona_template(
    group_id: int, body: PersonaApplyTemplateBody, _session: AdminWriteSession
) -> dict[str, Any]:
    """复制人设库里的一张卡到本群，并切换为本群自定义。"""

    async with get_session() as db:
        try:
            template = await get_template(db, body.template_id)
        except PersonaLibraryError as exc:
            raise _library_http_error(exc) from exc
        card = template_card(template)
    return ok(await _save_group_card(group_id, card, version=body.version, enabled=True))


@router.delete("/agent/groups/{group_id}/persona")
async def reset_agent_persona(
    group_id: int,
    _session: AdminWriteSession,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    changed = False
    async with optimistic_write_lock, get_session() as db:
        await require_group(db, group_id)
        row = await db.get(GroupAgentConfig, group_id)
        check_version(row, if_match, version_of=persona_content_version)
        if row is None:
            row = _new_config(group_id)
            db.add(row)
        mutation = reset_persona(row)
        if mutation.semantic_changed:
            row.persona_version = int(row.persona_version or 1) + 1
            changed = True
        if mutation.storage_changed:
            await db.commit()
            await db.refresh(row)
        result = serialize_persona(row, group_id)
    if changed:
        await hub.notify_change("agent_persona", str(group_id), group_id=group_id)
    return ok(result)


# ---------------------------------------------------------------- 人设库


async def _library_changed(*, global_changed: bool) -> None:
    # 人设库不属于某个群；人设页同时监听这个资源，全局人设变化时会自动刷新。
    # 私聊每用户人设按模板 id 引用人设库：任何增删改都要刷新私聊侧的快照。
    try:
        async with get_session() as db:
            await refresh_private_library(db)
    except Exception:  # noqa: BLE001
        logger.exception("刷新私聊人设库快照失败(忽略)")
    await hub.notify_change("agent_persona_library", "global" if global_changed else None)
    await hub.notify_change("private_agent_persona", "library")


@router.get("/agent/persona-library")
async def list_persona_library(_session: AdminReadSession) -> dict[str, Any]:
    async with get_session() as db:
        try:
            rows = await list_templates(db)
        except SQLAlchemyError as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "人设库表不可用，请先执行数据库迁移（nb orm upgrade）",
            ) from exc
        usage = _private_usage()
        return ok(
            [
                {**serialize_template(row), "privateUsers": usage.get(str(row.id), 0)}
                for row in rows
            ]
        )


def _private_usage() -> dict[str, int]:
    """每张模板被多少位私聊用户跟随（删除前提示用）。"""

    try:
        from ..yawn_agent.private_persona import template_usage

        return template_usage()
    except Exception:  # noqa: BLE001
        return {}


@router.post("/agent/persona-library")
async def create_persona_library_item(
    body: PersonaTemplateCreateBody, _session: AdminWriteSession
) -> dict[str, Any]:
    _require_safe_card(body.card)
    async with optimistic_write_lock, get_session() as db:
        try:
            row = await create_template(
                db, name=body.name, card=body.card, is_global=body.is_global
            )
        except PersonaLibraryError as exc:
            raise _library_http_error(exc) from exc
        except IntegrityError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, "人设库里已经有同名人设了") from exc
        await db.commit()
        await db.refresh(row)
        result = serialize_template(row)
        if body.is_global:
            await refresh_global_persona(db)
    await _library_changed(global_changed=body.is_global)
    return ok(result)


@router.patch("/agent/persona-library/{template_id}")
async def patch_persona_library_item(
    template_id: int, body: PersonaTemplatePatchBody, _session: AdminWriteSession
) -> dict[str, Any]:
    if body.card is not None:
        _require_safe_card(body.card)
    async with optimistic_write_lock, get_session() as db:
        try:
            before = await get_template(db, template_id)
            was_global = bool(before.is_global)
            row = await update_template(
                db,
                template_id,
                version=body.version,
                name=body.name,
                card=body.card,
                is_global=body.is_global,
            )
        except PersonaLibraryError as exc:
            raise _library_http_error(exc) from exc
        except IntegrityError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, "人设库里已经有同名人设了") from exc
        await db.commit()
        await db.refresh(row)
        result = serialize_template(row)
        global_changed = was_global or bool(row.is_global)
        if global_changed:
            await refresh_global_persona(db)
    await _library_changed(global_changed=global_changed)
    return ok(result)


@router.delete("/agent/persona-library/{template_id}")
async def delete_persona_library_item(
    template_id: int,
    _session: AdminWriteSession,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    async with optimistic_write_lock, get_session() as db:
        try:
            was_global = await delete_template(db, template_id, version=if_match)
        except PersonaLibraryError as exc:
            raise _library_http_error(exc) from exc
        await db.commit()
        if was_global:
            await refresh_global_persona(db)
    await _library_changed(global_changed=was_global)
    return ok(
        {
            "id": str(template_id),
            "deleted": True,
            "wasGlobal": was_global,
            # 跟随这张模板的私聊用户会回退到私聊默认人设（不会报错）。
            "privateUsersAffected": _private_usage().get(str(template_id), 0),
        }
    )
