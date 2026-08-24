# ruff: noqa: C901,FAST002,PERF203,PLR0913,PLR0917,PLR2004,TID252,TRY003,TRY004
"""YawnBot Core / Agent 管理 WebUI 的 FastAPI 适配器。"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import (
    APIRouter,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from nonebot import get_driver, logger
from nonebot_plugin_orm import get_session
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import BigInteger, cast, exists, func, or_, select
from sqlalchemy.exc import IntegrityError

from ..data_models.agent_audit import AgentAudit
from ..data_models.agent_memory import AgentMemory, AgentPrivacy, AgentRelation
from ..data_models.bot_group import BotGroup
from ..data_models.bot_user import BotUser
from ..data_models.group_agent_config import GroupAgentConfig
from ..data_models.group_agent_message import GroupAgentMessage
from ..data_models.user_group import UserGroup
from ..data_models.web_admin_audit import WebAdminAudit
from ..llm import LLMProviderConfig, test_llm_connection
from ..permission import FEATURE_REGISTRY
from ..yawn_agent.context import now_beijing
from ..yawn_agent.memory import (
    compact_group_memory,
    delete_group_memories,
    delete_member_memories,
    is_memory_compacting,
    normalize_relation_type,
    rebuild_group_memories,
    record_memory_failure,
)
from ..yawn_agent.persona import MAX_FIELD_LENGTH, PERSONA_FIELDS
from .auth import (
    Session,
    check_admin_token,
    client_key,
    create_session,
    login_allowed,
    verify_session,
    websocket_session,
)
from .config import API_PATH, BASE_PATH, COOKIE_NAME, DIST_DIR, config
from .deps import ReadSession, WriteSession, ok, page_params
from .environment import (
    EnvironmentConflictError,
    EnvironmentValidationError,
    load_environment,
    resolve_llm_provider,
    update_environment,
)
from .fanqie import router as fanqie_router
from .games import router as games_router
from .hub import hub
from .rpg_modules import router as rpg_modules_router
from .service import (
    BEIJING_TZ,
    RELATION_GRAPH_LIMIT,
    agent_diagnostics,
    agent_memory_status,
    delete_one_memory,
    get_group,
    group_feature_rows,
    iso,
    list_group_members,
    list_groups,
    list_users,
    load_relation_graph,
    overview,
    page_meta,
    serialize_agent_audit,
    serialize_agent_config,
    serialize_agent_message,
    serialize_memory,
    serialize_persona,
    serialize_relation,
    serialize_web_audit,
    set_group_feature,
    set_user_feature,
    user_feature_rows,
    version,
)

router = APIRouter(prefix=API_PATH)
_LLM_TEST_CONCURRENCY = asyncio.Semaphore(2)


def _memory_privacy_clauses(user_ids: set[int]) -> tuple[Any, ...]:
    if not user_ids:
        return ()
    related = func.json_each(AgentMemory.related_user_ids).table_valued("value")
    has_related_optout = exists(
        select(1)
        .select_from(related)
        .where(cast(related.c.value, BigInteger).in_(user_ids))
    )
    return (
        AgentMemory.subject_user_id.not_in(user_ids),
        ~has_related_optout,
    )
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class LoginBody(BaseModel):
    token: str = Field(min_length=1, max_length=4096)


class FeatureOverrideBody(BaseModel):
    override: bool | None


class EnvironmentChange(BaseModel):
    key: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=128)
    value: str | None = Field(default=None, max_length=16384)


class EnvironmentProviderPatch(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    base_url: str = Field(min_length=1, max_length=2048, alias="baseUrl")
    api_key: str | None = Field(
        default=None, min_length=1, max_length=4096, alias="apiKey"
    )


class EnvironmentPatch(BaseModel):
    version: str = Field(min_length=64, max_length=64)
    changes: list[EnvironmentChange] = Field(default_factory=list, max_length=256)
    providers: list[EnvironmentProviderPatch] | None = Field(
        default=None, min_length=1, max_length=17
    )

    @model_validator(mode="after")
    def require_changes(self) -> "EnvironmentPatch":
        if not self.changes and self.providers is None:
            raise ValueError("至少提交一个配置变更")
        return self


class LLMConnectionTestBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    provider_id: str = Field(
        pattern=r"^[a-z][a-z0-9_-]{0,31}$", alias="providerId"
    )
    base_url: str | None = Field(default=None, max_length=2048, alias="baseUrl")
    api_key: str | None = Field(
        default=None, min_length=1, max_length=4096, alias="apiKey"
    )
    model: str = Field(min_length=1, max_length=256)



class AgentConfigPatch(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    version: str | None
    enabled: bool | None = None
    trigger_mode: (
        Literal[
            "mention_only",
            "mention_or_reply",
            "explicit_wakeup",
            "mention_or_proactive",
        ]
        | None
    ) = Field(default=None, alias="triggerMode")
    proactive_probability: float | None = Field(
        default=None, ge=0, le=1, alias="proactiveProbability"
    )
    proactive_active_enabled: bool | None = Field(
        default=None, alias="proactiveActiveEnabled"
    )
    proactive_active_probability: float | None = Field(
        default=None, ge=0, le=1, alias="proactiveActiveProbability"
    )
    proactive_active_window_minutes: int | None = Field(
        default=None, ge=1, le=1440, alias="proactiveActiveWindowMinutes"
    )
    idle_threshold_minutes: int | None = Field(
        default=None, ge=1, le=10080, alias="idleThresholdMinutes"
    )
    cooldown_minutes: int | None = Field(
        default=None, ge=0, le=10080, alias="cooldownMinutes"
    )
    daily_limit: int | None = Field(default=None, ge=0, le=1000, alias="dailyLimit")
    raw_retention_days: int | None = Field(
        default=None, ge=1, le=365, alias="rawRetentionDays"
    )
    cross_group_visibility: Literal["isolated", "public_summary"] | None = Field(
        default=None, alias="crossGroupVisibility"
    )
    media_cache_enabled: bool | None = Field(default=None, alias="mediaCacheEnabled")
    admin_tool_daily_limit: int | None = Field(
        default=None, ge=1, le=1000, alias="adminToolDailyLimit"
    )
    tool_allowlist: list[Literal["mute_member", "create_group_announcement"]] | None = (
        Field(default=None, alias="toolAllowlist")
    )

    @field_validator("tool_allowlist")
    @classmethod
    def unique_tools(cls, value: list[str] | None) -> list[str] | None:
        return list(dict.fromkeys(value)) if value is not None else None


class PersonaPatch(BaseModel):
    version: str | None
    enabled: bool
    overrides: dict[str, str]

    @field_validator("overrides")
    @classmethod
    def validate_overrides(cls, value: dict[str, str]) -> dict[str, str]:
        clean: dict[str, str] = {}
        for key, raw in value.items():
            if key not in PERSONA_FIELDS:
                raise ValueError(f"不支持的人设字段：{key}")
            text = " ".join(raw.strip().split())
            if not text:
                continue
            if len(text) > MAX_FIELD_LENGTH:
                raise ValueError(f"人设字段 {key} 最长 {MAX_FIELD_LENGTH} 字符")
            clean[key] = text
        return clean


class MemoryCreateBody(BaseModel):
    """手动新增记忆；manual/core 是运维置顶事实，无整理任务回写。"""

    model_config = ConfigDict(populate_by_name=True)

    type: Literal["summary", "profile", "manual", "core"]
    key: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=2000)
    subject_user_id: Annotated[int, Field(gt=0)] | None = Field(
        default=None, alias="subjectUserId"
    )
    related_user_ids: list[Annotated[int, Field(gt=0)]] = Field(
        default_factory=list, max_length=100, alias="relatedUserIds"
    )
    salience: float = Field(default=0.7, ge=0, le=1)
    confidence: float = Field(default=0.9, ge=0, le=1)
    expires_in_days: int | None = Field(
        default=None, ge=1, le=3650, alias="expiresInDays"
    )


class MemoryPatchBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    version: str | None
    content: str | None = Field(default=None, min_length=1, max_length=2000)
    salience: float | None = Field(default=None, ge=0, le=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    expires_in_days: int | None = Field(
        default=None, ge=1, le=3650, alias="expiresInDays"
    )


class PrivacyPatchBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    opted_out: bool = Field(alias="optedOut")


class RelationCreateBody(BaseModel):
    """手动新增关系边；manual 来源的边不会被整理任务与重建覆盖。"""

    model_config = ConfigDict(populate_by_name=True)

    subject_user_id: Annotated[int, Field(gt=0)] = Field(alias="subjectUserId")
    object_user_id: Annotated[int, Field(gt=0)] = Field(alias="objectUserId")
    type: str = Field(min_length=1, max_length=32)
    note: str = Field(default="", max_length=200)
    confidence: float = Field(default=0.9, ge=0, le=1)


class RelationPatchBody(BaseModel):
    """只允许改备注与置信度；类型/两端属于边身份，改动请删除后重建。"""

    model_config = ConfigDict(populate_by_name=True)

    note: str | None = Field(default=None, max_length=200)
    confidence: float | None = Field(default=None, ge=0, le=1)


async def require_group(session: Any, group_id: int) -> BotGroup:
    group = await session.get(BotGroup, group_id)
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "群不存在")
    return group


async def require_user(session: Any, user_id: int) -> BotUser:
    user = await session.get(BotUser, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    return user


def check_version(row: Any, supplied: str | None) -> None:
    # 乐观锁按 updated_at 序列化值比对；GroupAgentConfig 与 AgentMemory 通用。
    current = version(row.updated_at) if row is not None else None
    if current != supplied:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "配置已被其他操作修改，请刷新后重试"
        )


@router.post("/auth/login")
async def login(
    request: Request, body: LoginBody, response: Response
) -> dict[str, Any]:
    key = client_key(request)
    if not login_allowed(key):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "登录尝试过多，请稍后重试"
        )
    if not check_admin_token(key, body.token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token 不正确")
    cookie, session = create_session()
    response.set_cookie(
        COOKIE_NAME,
        cookie,
        max_age=config.webui_session_ttl_hours * 3600,
        httponly=True,
        secure=config.webui_cookie_secure,
        samesite="strict",
        path=BASE_PATH,
    )
    return ok(
        {
            "authenticated": True,
            "csrfToken": session.csrf_token,
            "expiresAt": session.expires_at,
        }
    )


@router.post("/auth/logout")
async def logout(response: Response, _session: WriteSession) -> dict[str, Any]:
    response.delete_cookie(COOKIE_NAME, path=BASE_PATH)
    return ok({"authenticated": False})


@router.get("/auth/session")
async def get_auth_session(session: ReadSession) -> dict[str, Any]:
    return ok(
        {
            "authenticated": True,
            "csrfToken": session.csrf_token,
            "expiresAt": session.expires_at,
        }
    )


@router.get("/overview")
async def get_overview(_session: ReadSession) -> dict[str, Any]:
    return ok(await overview())


@router.get("/plugins")
async def get_plugins(_session: ReadSession) -> dict[str, Any]:
    snapshot = await overview()
    return ok(snapshot["plugins"])


@router.get("/environment")
async def get_environment(_session: ReadSession) -> dict[str, Any]:
    return ok(await asyncio.to_thread(load_environment))


@router.patch("/environment")
async def patch_environment(
    body: EnvironmentPatch, _session: WriteSession
) -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(
            update_environment,
            body.version,
            [(item.key, item.value) for item in body.changes],
            (
                [
                    item.model_dump(by_alias=True, exclude_unset=True)
                    for item in body.providers
                ]
                if body.providers is not None
                else None
            ),
        )
    except EnvironmentConflictError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "环境配置已被其他操作修改，请刷新后重试"
        ) from exc
    except EnvironmentValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return ok(result)


@router.post("/llm/test")
async def test_llm(
    body: LLMConnectionTestBody, _session: WriteSession
) -> dict[str, Any]:
    stored_base_url = ""
    stored_api_key: str | None = None
    try:
        stored_base_url, stored_api_key = await asyncio.to_thread(
            resolve_llm_provider, body.provider_id
        )
    except EnvironmentValidationError:
        if body.base_url is None or "api_key" not in body.model_fields_set:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "新提供商测试必须填写 Base URL 和 API Key",
            ) from None

    base_url = (body.base_url or stored_base_url).strip().rstrip("/")
    api_key = (
        body.api_key
        if "api_key" in body.model_fields_set
        else stored_api_key
    )
    if not api_key:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "该提供商尚未配置 API Key"
        )
    try:
        validated = LLMProviderConfig(id=body.provider_id, base_url=base_url)
        async with _LLM_TEST_CONCURRENCY:
            elapsed_ms = await test_llm_connection(
                base_url=validated.base_url,
                api_key=api_key,
                model=body.model.strip(),
                timeout=10.0,
            )
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Base URL 格式不正确"
        ) from exc
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT, "连接测试超时（10 秒）"
        ) from exc
    except Exception as exc:
        message = str(exc).replace(api_key, "[REDACTED]")[:500]
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"连接测试失败：{message or type(exc).__name__}",
        ) from exc
    return ok({"success": True, "latencyMs": round(elapsed_ms, 1)})


@router.get("/groups")
async def get_groups(
    _session: ReadSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=100),
    search: str = Query(default="", max_length=120),
) -> dict[str, Any]:
    page, page_size = page_params(page, page_size)
    async with get_session() as db:
        rows, total = await list_groups(
            db, page=page, page_size=page_size, search=search.strip()
        )
    return ok(rows, page_meta(page, page_size, total))


@router.get("/groups/{group_id}")
async def get_group_detail(group_id: int, _session: ReadSession) -> dict[str, Any]:
    async with get_session() as db:
        result = await get_group(db, group_id)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "群不存在")
    return ok(result)


@router.get("/groups/{group_id}/members")
async def get_members(
    group_id: int,
    _session: ReadSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=100),
    search: str = Query(default="", max_length=120),
) -> dict[str, Any]:
    page, page_size = page_params(page, page_size)
    async with get_session() as db:
        await require_group(db, group_id)
        rows, total = await list_group_members(
            db, group_id, page=page, page_size=page_size, search=search.strip()
        )
    return ok(rows, page_meta(page, page_size, total))


@router.patch("/groups/{group_id}/features/{feature}")
async def patch_group_feature(
    group_id: int, feature: str, body: FeatureOverrideBody, _session: WriteSession
) -> dict[str, Any]:
    if feature not in FEATURE_REGISTRY:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "功能不存在")
    async with get_session() as db:
        await require_group(db, group_id)
        await set_group_feature(db, group_id, feature, body.override)
        await db.commit()
        rows = await group_feature_rows(db, group_id)
    await hub.notify_change("group_feature", f"{group_id}:{feature}")
    return ok(next(row for row in rows if row["key"] == feature))


@router.get("/groups/{group_id}/members/{user_id}/features")
async def get_member_features(
    group_id: int, user_id: int, _session: ReadSession
) -> dict[str, Any]:
    async with get_session() as db:
        membership = await db.get(UserGroup, (group_id, user_id))
        if membership is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "群成员不存在")
        rows = await user_feature_rows(db, user_id, group_id)
    return ok(rows)


@router.patch("/groups/{group_id}/members/{user_id}/features/{feature}")
async def patch_member_feature(
    group_id: int,
    user_id: int,
    feature: str,
    body: FeatureOverrideBody,
    _session: WriteSession,
) -> dict[str, Any]:
    if feature not in FEATURE_REGISTRY:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "功能不存在")
    async with get_session() as db:
        membership = await db.get(UserGroup, (group_id, user_id))
        if membership is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "群成员不存在")
        await set_user_feature(db, user_id, feature, body.override, group_id=group_id)
        await db.commit()
        rows = await user_feature_rows(db, user_id, group_id)
    await hub.notify_change("user_feature", f"{group_id}:{user_id}:{feature}")
    return ok(next(row for row in rows if row["key"] == feature))


@router.get("/users")
async def get_users(
    _session: ReadSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=100),
    search: str = Query(default="", max_length=120),
) -> dict[str, Any]:
    page, page_size = page_params(page, page_size)
    async with get_session() as db:
        rows, total = await list_users(
            db, page=page, page_size=page_size, search=search.strip()
        )
    return ok(rows, page_meta(page, page_size, total))


@router.get("/users/{user_id}/features")
async def get_global_user_features(
    user_id: int, _session: ReadSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_user(db, user_id)
        rows = await user_feature_rows(db, user_id, None)
    return ok(rows)


@router.patch("/users/{user_id}/features/{feature}")
async def patch_global_user_feature(
    user_id: int, feature: str, body: FeatureOverrideBody, _session: WriteSession
) -> dict[str, Any]:
    if feature not in FEATURE_REGISTRY:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "功能不存在")
    async with get_session() as db:
        await require_user(db, user_id)
        await set_user_feature(db, user_id, feature, body.override, group_id=None)
        await db.commit()
        rows = await user_feature_rows(db, user_id, None)
    await hub.notify_change("global_user_feature", f"{user_id}:{feature}")
    return ok(next(row for row in rows if row["key"] == feature))


@router.get("/agent/groups/{group_id}/config")
async def get_agent_config(group_id: int, _session: ReadSession) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        row = await db.get(GroupAgentConfig, group_id)
        return ok(serialize_agent_config(row, group_id))


@router.get("/agent/groups/{group_id}/diagnostics")
async def get_agent_diagnostics(group_id: int, _session: ReadSession) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        return ok(await agent_diagnostics(db, group_id))


@router.patch("/agent/groups/{group_id}/config")
async def patch_agent_config(
    group_id: int, body: AgentConfigPatch, _session: WriteSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        row = await db.get(GroupAgentConfig, group_id)
        check_version(row, body.version)
        if row is None:
            row = GroupAgentConfig(group_id=group_id)
            db.add(row)
        updates = body.model_dump(exclude_unset=True, exclude={"version"})
        if not updates:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "没有可更新的字段"
            )
        for field, value in updates.items():
            setattr(row, field, value)
        await db.commit()
        await db.refresh(row)
        result = serialize_agent_config(row, group_id)
    await hub.notify_change("agent_config", str(group_id))
    return ok(result)


@router.get("/agent/groups/{group_id}/persona")
async def get_agent_persona(group_id: int, _session: ReadSession) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        row = await db.get(GroupAgentConfig, group_id)
        return ok(serialize_persona(row, group_id))


@router.put("/agent/groups/{group_id}/persona")
async def put_agent_persona(
    group_id: int, body: PersonaPatch, _session: WriteSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        row = await db.get(GroupAgentConfig, group_id)
        check_version(row, body.version)
        if row is None:
            row = GroupAgentConfig(group_id=group_id)
            db.add(row)
        row.persona_enabled = body.enabled
        overrides: dict[str, object] = dict(body.overrides)
        row.persona_override = overrides
        row.persona_version += 1
        await db.commit()
        await db.refresh(row)
        result = serialize_persona(row, group_id)
    await hub.notify_change("agent_persona", str(group_id))
    return ok(result)


@router.delete("/agent/groups/{group_id}/persona")
async def reset_agent_persona(
    group_id: int,
    _session: WriteSession,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        row = await db.get(GroupAgentConfig, group_id)
        check_version(row, if_match)
        if row is None:
            row = GroupAgentConfig(group_id=group_id)
            db.add(row)
        row.persona_override = {}
        row.persona_enabled = True
        row.persona_version += 1
        await db.commit()
        await db.refresh(row)
        result = serialize_persona(row, group_id)
    await hub.notify_change("agent_persona", str(group_id))
    return ok(result)


@router.get("/agent/groups/{group_id}/memories")
async def get_memories(
    group_id: int,
    _session: ReadSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=100),
    search: str = Query(default="", max_length=120),
    memory_type: str = Query(default="", alias="type", max_length=24),
    subject_user_id: int | None = Query(default=None, alias="subjectUserId"),
) -> dict[str, Any]:
    page, page_size = page_params(page, page_size)
    clauses = [
        AgentMemory.group_id == group_id,
        AgentMemory.visibility.in_(("group", "public")),
        (
            AgentMemory.expires_at.is_(None)
            | (AgentMemory.expires_at >= now_beijing())
        ),
    ]
    if search:
        pattern = f"%{search}%"
        clauses.append(
            or_(
                AgentMemory.memory_key.ilike(pattern),
                AgentMemory.content.ilike(pattern),
            )
        )
    if memory_type:
        clauses.append(AgentMemory.memory_type == memory_type)
    if subject_user_id is not None:
        clauses.append(AgentMemory.subject_user_id == subject_user_id)
    async with get_session() as db:
        await require_group(db, group_id)
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
        if opted_out:
            clauses.extend(_memory_privacy_clauses(opted_out))
        total = int(
            await db.scalar(
                select(func.count()).select_from(AgentMemory).where(*clauses)
            )
            or 0
        )
        rows = list(
            (
                await db.execute(
                    select(AgentMemory)
                    .where(*clauses)
                    .order_by(AgentMemory.updated_at.desc(), AgentMemory.id.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
    return ok(
        [serialize_memory(row) for row in rows], page_meta(page, page_size, total)
    )


# 成员画像面板的成员索引口径：profile/core/manual 都是按成员沉淀的可读事实
# （对话注入同口径），群级行（subject=0）与 summary 不参与。
_SUBJECT_MEMORY_TYPES = ("profile", "core", "manual")


@router.get("/agent/groups/{group_id}/memories/subjects")
async def get_memory_subjects(group_id: int, _session: ReadSession) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
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
        clauses = [
            AgentMemory.group_id == group_id,
            AgentMemory.subject_user_id != 0,
            AgentMemory.memory_type.in_(_SUBJECT_MEMORY_TYPES),
            AgentMemory.visibility.in_(("group", "public")),
            (
                AgentMemory.expires_at.is_(None)
                | (AgentMemory.expires_at >= now_beijing())
            ),
        ]
        if opted_out:
            clauses.extend(_memory_privacy_clauses(opted_out))
        rows = list(
            (
                await db.execute(
                    select(AgentMemory)
                    .where(*clauses)
                    .order_by(AgentMemory.updated_at.desc(), AgentMemory.id.desc())
                    .limit(RELATION_GRAPH_LIMIT)
                )
            )
            .scalars()
            .all()
        )
        # rows 按 updated_at 降序：首次见到某成员即其最新更新时间，dict 保持
        # 插入序使结果天然按最近更新排列，与图谱端点的 Python 聚合模式一致。
        subjects: dict[int, dict[str, Any]] = {}
        for row in rows:
            user_id = int(row.subject_user_id)
            entry = subjects.get(user_id)
            if entry is None:
                entry = {
                    "userId": str(user_id),
                    "counts": dict.fromkeys(_SUBJECT_MEMORY_TYPES, 0),
                    "total": 0,
                    "updatedAt": iso(row.updated_at),
                }
                subjects[user_id] = entry
            entry["counts"][row.memory_type] += 1
            entry["total"] += 1
        # 昵称联接走全群成员表（同图谱端点），避免大 in_ 参数；退群残留回退空昵称。
        member_rows = list(
            (
                await db.execute(
                    select(UserGroup, BotUser)
                    .join(BotUser, BotUser.user_id == UserGroup.user_id)
                    .where(UserGroup.group_id == group_id)
                    .order_by(UserGroup.user_id)
                    .limit(RELATION_GRAPH_LIMIT)
                )
            )
            .all()
        )
        member_names: dict[int, tuple[str, str | None]] = {
            int(user.user_id): (user.nickname, membership.group_nickname)
            for membership, user in member_rows
        }
        result = []
        for user_id, entry in subjects.items():
            nickname, group_nickname = member_names.get(user_id, ("", None))
            result.append(
                {**entry, "nickname": nickname, "groupNickname": group_nickname}
            )
    return ok(result)


@router.get("/agent/groups/{group_id}/memories/export")
async def export_memories(group_id: int, _session: ReadSession) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
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
        clauses = [
            AgentMemory.group_id == group_id,
            AgentMemory.visibility.in_(("group", "public")),
            (
                AgentMemory.expires_at.is_(None)
                | (AgentMemory.expires_at >= now_beijing())
            ),
            *_memory_privacy_clauses(opted_out),
        ]
        rows = list(
            (
                await db.execute(
                    select(AgentMemory)
                    .where(*clauses)
                    .order_by(AgentMemory.id)
                    .limit(5000)
                )
            )
            .scalars()
            .all()
        )
        relation_clauses = [AgentRelation.group_id == group_id]
        if opted_out:
            relation_clauses.extend(
                (
                    AgentRelation.subject_user_id.not_in(opted_out),
                    AgentRelation.object_user_id.not_in(opted_out),
                )
            )
        relation_rows = list(
            (
                await db.execute(
                    select(AgentRelation)
                    .where(*relation_clauses)
                    .order_by(AgentRelation.id)
                    .limit(5000)
                )
            )
            .scalars()
            .all()
        )
    return ok(
        {
            "groupId": str(group_id),
            "memories": [serialize_memory(row) for row in rows],
            "relations": [serialize_relation(row) for row in relation_rows],
        }
    )


@router.get("/agent/groups/{group_id}/memories/status")
async def get_memory_status(group_id: int, _session: ReadSession) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        return ok(await agent_memory_status(db, group_id))


# 手动整理是重操作（LLM 摘要可达数十秒）：后台执行并按群防重复触发。
_compact_inflight: set[int] = set()
_bg_tasks: set[asyncio.Task[None]] = set()


async def _run_manual_compact(group_id: int) -> None:
    try:
        async with get_session() as db:
            await compact_group_memory(db, group_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("WebUI 手动记忆整理失败: %s", group_id)
        try:
            async with get_session() as db:
                await record_memory_failure(
                    db, group_id, f"手动整理异常: {type(exc).__name__}"
                )
        except Exception:  # noqa: BLE001
            logger.exception("WebUI 手动记忆失败状态写入失败: %s", group_id)
    finally:
        _compact_inflight.discard(group_id)
        await hub.notify_change("agent_memory", str(group_id))


async def _run_manual_rebuild(group_id: int) -> None:
    try:
        async with get_session() as db:
            await rebuild_group_memories(db, group_id)
        async with get_session() as db:
            await compact_group_memory(db, group_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("WebUI 手动记忆重建失败: %s", group_id)
        try:
            async with get_session() as db:
                await record_memory_failure(
                    db, group_id, f"手动重建异常: {type(exc).__name__}"
                )
        except Exception:  # noqa: BLE001
            logger.exception("WebUI 手动重建失败状态写入失败: %s", group_id)
    finally:
        _compact_inflight.discard(group_id)
        await hub.notify_change("agent_memory", str(group_id))


@router.post("/agent/groups/{group_id}/memories/compact")
async def trigger_memory_compact(
    group_id: int, _session: WriteSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
    if group_id in _compact_inflight or is_memory_compacting(group_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "该群正在整理记忆，请稍后再试")
    _compact_inflight.add(group_id)
    task = asyncio.create_task(_run_manual_compact(group_id))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return ok({"started": True})


@router.post("/agent/groups/{group_id}/memories/rebuild")
async def trigger_memory_rebuild(
    group_id: int, _session: WriteSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
    if group_id in _compact_inflight or is_memory_compacting(group_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "该群正在整理记忆，请稍后再试")
    _compact_inflight.add(group_id)
    task = asyncio.create_task(_run_manual_rebuild(group_id))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return ok({"started": True, "rebuild": True})


@router.post("/agent/groups/{group_id}/memories")
async def create_memory(
    group_id: int, body: MemoryCreateBody, _session: WriteSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        now = now_beijing()
        expires_at = (
            now + timedelta(days=body.expires_in_days)
            if body.expires_in_days is not None
            else None
        )
        row = AgentMemory(
            group_id=group_id,
            scope="group",
            subject_user_id=body.subject_user_id or 0,
            memory_type=body.type,
            memory_key=body.key.strip(),
            content=body.content.strip(),
            evidence_message_ids=[],
            source_kind="manual",
            related_user_ids=sorted(
                {
                    *body.related_user_ids,
                    *([body.subject_user_id] if body.subject_user_id else []),
                }
            ),
            salience=body.salience,
            confidence=body.confidence,
            visibility="group",
            expires_at=expires_at,
        )
        db.add(row)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status.HTTP_409_CONFLICT, "同类型同 key 的记忆已存在"
            ) from None
        await db.refresh(row)
        result = serialize_memory(row)
    await hub.notify_change("agent_memory", str(row.id))
    return ok(result)


@router.put("/agent/groups/{group_id}/memories/{memory_id}")
async def update_memory(
    group_id: int, memory_id: int, body: MemoryPatchBody, _session: WriteSession
) -> dict[str, Any]:
    updates = body.model_dump(exclude_unset=True, exclude={"version"})
    if not updates:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "没有可更新的字段")
    async with get_session() as db:
        await require_group(db, group_id)
        row = await db.scalar(
            select(AgentMemory).where(
                AgentMemory.group_id == group_id,
                AgentMemory.id == memory_id,
            )
        )
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "记忆不存在")
        check_version(row, body.version)
        if "content" in updates:
            row.content = str(updates["content"]).strip()
        if "salience" in updates:
            row.salience = float(updates["salience"])
        if "confidence" in updates:
            row.confidence = float(updates["confidence"])
        if "expires_in_days" in updates:
            row.expires_at = (
                now_beijing() + timedelta(days=int(updates["expires_in_days"]))
                if updates["expires_in_days"] is not None
                else None
            )
        await db.commit()
        await db.refresh(row)
        result = serialize_memory(row)
    await hub.notify_change("agent_memory", str(memory_id))
    return ok(result)


@router.delete("/agent/groups/{group_id}/memories/{memory_id}")
async def delete_memory(
    group_id: int, memory_id: int, _session: WriteSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        count = await delete_one_memory(db, group_id, memory_id)
        if not count:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "记忆不存在")
        await db.commit()
    await hub.notify_change("agent_memory", str(memory_id))
    return ok({"deleted": count})


@router.delete("/agent/groups/{group_id}/members/{user_id}/data")
async def delete_member_agent_data(
    group_id: int, user_id: int, _session: WriteSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        count = await delete_member_memories(db, group_id, user_id)
    await hub.notify_change("agent_member_data", f"{group_id}:{user_id}")
    return ok({"deleted": count})


@router.delete("/agent/groups/{group_id}/data")
async def delete_group_agent_data(
    group_id: int, _session: WriteSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        count = await delete_group_memories(db, group_id)
    await hub.notify_change("agent_group_data", str(group_id))
    return ok({"deleted": count})


@router.get("/agent/groups/{group_id}/privacy")
async def get_privacy(
    group_id: int,
    _session: ReadSession,
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


@router.get("/agent/groups/{group_id}/relations")
async def get_relations(
    group_id: int,
    _session: ReadSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=100),
    search: str = Query(default="", max_length=24),
    relation_type: str = Query(default="", alias="type", max_length=32),
) -> dict[str, Any]:
    page, page_size = page_params(page, page_size)
    clauses = [AgentRelation.group_id == group_id]
    if relation_type:
        clauses.append(AgentRelation.relation_type == relation_type)
    if search.strip().isdigit():
        target = int(search.strip())
        clauses.append(
            or_(
                AgentRelation.subject_user_id == target,
                AgentRelation.object_user_id == target,
            )
        )
    async with get_session() as db:
        await require_group(db, group_id)
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
        if opted_out:
            clauses.extend(
                (
                    AgentRelation.subject_user_id.not_in(opted_out),
                    AgentRelation.object_user_id.not_in(opted_out),
                )
            )
        total = int(
            await db.scalar(
                select(func.count()).select_from(AgentRelation).where(*clauses)
            )
            or 0
        )
        rows = list(
            (
                await db.execute(
                    select(AgentRelation)
                    .where(*clauses)
                    .order_by(AgentRelation.confidence.desc(), AgentRelation.id.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
    return ok(
        [serialize_relation(row) for row in rows], page_meta(page, page_size, total)
    )


@router.get("/agent/groups/{group_id}/relations/graph")
async def get_relation_graph(group_id: int, _session: ReadSession) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        return ok(await load_relation_graph(db, group_id))


@router.get("/agent/groups/{group_id}/relations/types")
async def get_relation_types(group_id: int, _session: ReadSession) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        rows = (
            (
                await db.execute(
                    select(AgentRelation.relation_type)
                    .where(AgentRelation.group_id == group_id)
                    .distinct()
                    .order_by(AgentRelation.relation_type)
                )
            )
            .scalars()
            .all()
        )
    return ok([str(row) for row in rows])


@router.post("/agent/groups/{group_id}/relations")
async def create_relation(
    group_id: int, body: RelationCreateBody, _session: WriteSession
) -> dict[str, Any]:
    relation_type = normalize_relation_type(body.type)
    if not relation_type:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "关系类型不能为空")
    if body.subject_user_id == body.object_user_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "关系两端不能是同一个人"
        )
    async with get_session() as db:
        await require_group(db, group_id)
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
        if body.subject_user_id in opted_out or body.object_user_id in opted_out:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "关系一方已隐私退出，不得建立关系边",
            )
        row = AgentRelation(
            group_id=group_id,
            subject_user_id=body.subject_user_id,
            object_user_id=body.object_user_id,
            relation_type=relation_type,
            source_kind="manual",
            note=body.note.strip(),
            confidence=body.confidence,
            evidence_count=1,
            last_seen_at=now_beijing(),
        )
        db.add(row)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status.HTTP_409_CONFLICT, "这两个成员的该类型关系边已存在"
            ) from None
        await db.refresh(row)
        result = serialize_relation(row)
    await hub.notify_change("agent_relation", str(row.id))
    return ok(result)


@router.put("/agent/groups/{group_id}/relations/{relation_id}")
async def update_relation(
    group_id: int, relation_id: int, body: RelationPatchBody, _session: WriteSession
) -> dict[str, Any]:
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "没有可更新的字段")
    async with get_session() as db:
        await require_group(db, group_id)
        row = await db.scalar(
            select(AgentRelation).where(
                AgentRelation.group_id == group_id,
                AgentRelation.id == relation_id,
            )
        )
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "关系不存在")
        if "note" in updates:
            row.note = str(updates["note"] or "").strip()
        if updates.get("confidence") is not None:
            row.confidence = float(updates["confidence"])
        await db.commit()
        await db.refresh(row)
        result = serialize_relation(row)
    await hub.notify_change("agent_relation", str(relation_id))
    return ok(result)


@router.delete("/agent/groups/{group_id}/relations/{relation_id}")
async def delete_relation(
    group_id: int, relation_id: int, _session: WriteSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        row = await db.scalar(
            select(AgentRelation).where(
                AgentRelation.group_id == group_id,
                AgentRelation.id == relation_id,
            )
        )
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "关系不存在")
        await db.delete(row)
        await db.commit()
    await hub.notify_change("agent_relation", str(relation_id))
    return ok({"deleted": 1})


@router.get("/agent/groups/{group_id}/messages")
async def get_agent_messages(
    group_id: int,
    _session: ReadSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=100),
    search: str = Query(default="", max_length=120),
    role: str = Query(default="", max_length=24),
) -> dict[str, Any]:
    page, page_size = page_params(page, page_size)
    now = now_beijing()
    clauses = [
        GroupAgentMessage.group_id == group_id,
        GroupAgentMessage.expires_at.is_not(None),
        GroupAgentMessage.expires_at >= now,
    ]
    if role:
        clauses.append(GroupAgentMessage.role == role)
    if search:
        pattern = f"%{search}%"
        clauses.append(
            or_(
                GroupAgentMessage.normalized_text.ilike(pattern),
                GroupAgentMessage.sender_name.ilike(pattern),
            )
        )
    async with get_session() as db:
        await require_group(db, group_id)
        # 隐私退出是读路径级别的：管理台同样不得回看其消息。
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
        if opted_out:
            clauses.append(GroupAgentMessage.user_id.not_in(opted_out))
        total = int(
            await db.scalar(
                select(func.count()).select_from(GroupAgentMessage).where(*clauses)
            )
            or 0
        )
        rows = list(
            (
                await db.execute(
                    select(GroupAgentMessage)
                    .where(*clauses)
                    .order_by(GroupAgentMessage.id.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
    return ok(
        [serialize_agent_message(row) for row in rows],
        page_meta(page, page_size, total),
    )


@router.patch("/agent/groups/{group_id}/privacy/{user_id}")
async def patch_privacy(
    group_id: int, user_id: int, body: PrivacyPatchBody, _session: WriteSession
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


@router.get("/agent/audits")
async def get_agent_audits(
    _session: ReadSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=100),
    group_id: int | None = Query(default=None, alias="groupId"),
    result: str = Query(default="", max_length=24),
) -> dict[str, Any]:
    page, page_size = page_params(page, page_size)
    clauses = []
    if group_id is not None:
        clauses.append(AgentAudit.group_id == group_id)
    if result:
        clauses.append(AgentAudit.result == result)
    async with get_session() as db:
        total = int(
            await db.scalar(
                select(func.count()).select_from(AgentAudit).where(*clauses)
            )
            or 0
        )
        rows = list(
            (
                await db.execute(
                    select(AgentAudit)
                    .where(*clauses)
                    .order_by(AgentAudit.id.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
    return ok(
        [serialize_agent_audit(row) for row in rows], page_meta(page, page_size, total)
    )


@router.get("/web-audits")
async def get_web_audits(
    _session: ReadSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=100),
    result: str = Query(default="", max_length=24),
) -> dict[str, Any]:
    page, page_size = page_params(page, page_size)
    clauses = [WebAdminAudit.result == result] if result else []
    async with get_session() as db:
        total = int(
            await db.scalar(
                select(func.count()).select_from(WebAdminAudit).where(*clauses)
            )
            or 0
        )
        rows = list(
            (
                await db.execute(
                    select(WebAdminAudit)
                    .where(*clauses)
                    .order_by(WebAdminAudit.id.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
    return ok(
        [serialize_web_audit(row) for row in rows], page_meta(page, page_size, total)
    )


async def _record_request_audit(
    *, request_id: str, session: Session, request: Request, status_code: int
) -> None:
    relative = request.url.path.removeprefix(API_PATH).strip("/")
    parts = relative.split("/") if relative else []
    resource_type = "/".join(parts[:3])[:64] or "root"
    resource_id = "/".join(parts[3:])[:255] or None
    try:
        async with get_session() as db:
            db.add(
                WebAdminAudit(
                    request_id=request_id,
                    actor_session=session.actor_fingerprint,
                    action=request.method,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    result="success" if status_code < 400 else "failed",
                    detail={
                        "statusCode": status_code,
                        "queryKeys": sorted(request.query_params.keys()),
                    },
                )
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("WebUI 操作审计写入失败")


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def web_http_exception(request: Request, exc: HTTPException) -> Response:
        if request.url.path.startswith(API_PATH):
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "error": {
                        "code": f"http_{exc.status_code}",
                        "message": str(exc.detail),
                        "fields": {},
                    }
                },
                headers=exc.headers,
            )
        return await http_exception_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def web_validation_exception(
        request: Request, exc: RequestValidationError
    ) -> Response:
        if request.url.path.startswith(API_PATH):
            fields = {
                ".".join(str(part) for part in item["loc"]): item["msg"]
                for item in exc.errors()
            }
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={
                    "error": {
                        "code": "validation_error",
                        "message": "请求参数不正确",
                        "fields": fields,
                    }
                },
            )
        return await request_validation_exception_handler(request, exc)


def register(app: FastAPI) -> None:
    app.include_router(router)
    app.include_router(games_router)
    app.include_router(fanqie_router)
    app.include_router(rpg_modules_router)

    @app.middleware("http")
    async def webui_audit_middleware(request: Request, call_next: Any) -> Response:
        should_audit = (
            request.url.path.startswith(API_PATH) and request.method in _WRITE_METHODS
        )
        authenticated = (
            verify_session(request.cookies.get(COOKIE_NAME)) if should_audit else None
        )
        request_id = str(uuid.uuid4())
        try:
            response = await call_next(request)
        except Exception:
            if authenticated is not None:
                await _record_request_audit(
                    request_id=request_id,
                    session=authenticated,
                    request=request,
                    status_code=500,
                )
            if request.url.path.startswith(API_PATH):
                logger.exception("WebUI API 未处理异常")
                return JSONResponse(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    content={
                        "error": {
                            "code": "internal_error",
                            "message": "服务器内部错误",
                            "fields": {},
                        }
                    },
                )
            raise
        if authenticated is not None:
            await _record_request_audit(
                request_id=request_id,
                session=authenticated,
                request=request,
                status_code=response.status_code,
            )
            response.headers["X-Request-ID"] = request_id
        return response

    @app.websocket(f"{API_PATH}/stream")
    async def stream(websocket: WebSocket) -> None:
        session = websocket_session(websocket)
        if session is None:
            await websocket.close(code=4401)
            return
        await hub.connect(websocket)
        try:
            await hub.send(
                websocket,
                {"type": "snapshot", "version": 1, "data": await overview()},
            )
            while True:
                try:
                    await asyncio.wait_for(websocket.receive_text(), timeout=30)
                except TimeoutError:
                    await hub.send(
                        websocket,
                        {
                            "type": "heartbeat",
                            "data": {"at": datetime.now(BEIJING_TZ).isoformat()},
                        },
                    )
        except WebSocketDisconnect:
            pass
        finally:
            hub.disconnect(websocket)

    assets = DIST_DIR / "assets"
    if assets.is_dir():
        app.mount(
            f"{BASE_PATH}/assets",
            StaticFiles(directory=assets),
            name="yawn-webui-assets",
        )

    index = DIST_DIR / "index.html"

    @app.get(BASE_PATH, include_in_schema=False)
    @app.get(f"{BASE_PATH}/{{spa_path:path}}", include_in_schema=False)
    async def spa(spa_path: str = "") -> FileResponse:
        if spa_path.startswith("api/"):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "接口不存在")
        requested = (DIST_DIR / spa_path).resolve()
        if spa_path and DIST_DIR.resolve() in requested.parents and requested.is_file():
            return FileResponse(requested)
        return FileResponse(index)

    _register_exception_handlers(app)


def install() -> None:
    driver = get_driver()
    app = getattr(driver, "server_app", None)
    if not isinstance(app, FastAPI):
        raise RuntimeError("WebUI 需要 NoneBot FastAPI Driver")
    register(app)

    async def start_hub() -> None:
        hub.start(overview)

    driver.on_startup(start_hub)
    driver.on_shutdown(hub.stop)
