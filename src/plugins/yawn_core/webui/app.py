# ruff: noqa: C901,FAST002,PERF203,PLR0913,PLR0917,PLR2004,TID252,TRY003,TRY004
"""YawnBot Core / Agent 管理 WebUI 的 FastAPI 适配器。"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
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
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, or_, select

from ..data_models.agent_audit import AgentAudit
from ..data_models.agent_memory import AgentMemory, AgentPrivacy
from ..data_models.bot_group import BotGroup
from ..data_models.bot_user import BotUser
from ..data_models.group_agent_config import GroupAgentConfig
from ..data_models.user_group import UserGroup
from ..data_models.web_admin_audit import WebAdminAudit
from ..permission import FEATURE_REGISTRY
from ..yawn_agent.memory import delete_group_memories, delete_member_memories
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
from .fanqie import router as fanqie_router
from .games import router as games_router
from .hub import hub
from .service import (
    BEIJING_TZ,
    delete_one_memory,
    get_group,
    group_feature_rows,
    iso,
    list_group_members,
    list_groups,
    list_users,
    overview,
    page_meta,
    serialize_agent_audit,
    serialize_agent_config,
    serialize_memory,
    serialize_persona,
    serialize_web_audit,
    set_group_feature,
    set_user_feature,
    user_feature_rows,
    version,
)

router = APIRouter(prefix=API_PATH)
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class LoginBody(BaseModel):
    token: str = Field(min_length=1, max_length=4096)


class FeatureOverrideBody(BaseModel):
    override: bool | None


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


def check_version(row: GroupAgentConfig | None, supplied: str | None) -> None:
    current = version(row.updated_at) if row else None
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


@router.get("/agent/groups/{group_id}/memories/export")
async def export_memories(group_id: int, _session: ReadSession) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        rows = list(
            (
                await db.execute(
                    select(AgentMemory)
                    .where(
                        AgentMemory.group_id == group_id,
                        AgentMemory.visibility.in_(("group", "public")),
                    )
                    .order_by(AgentMemory.id)
                    .limit(5000)
                )
            )
            .scalars()
            .all()
        )
    return ok(
        {"groupId": str(group_id), "memories": [serialize_memory(row) for row in rows]}
    )


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
