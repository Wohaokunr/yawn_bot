# ruff: noqa: FAST002,TC001,TID252
"""Group and group-member administration endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from nonebot_plugin_orm import get_session

from ..data_models.user_group import UserGroup
from ..permission import FEATURE_REGISTRY
from ..yawn_agent.config_store import get_or_create_config
from ..yawn_agent.conversation import close_group_conversations
from .config import API_PATH
from .deps import (
    AdminReadSession,
    AdminWriteSession,
    AuthenticatedSession,
    GroupViewSession,
    ok,
    page_params,
)
from .hub import hub
from .route_helpers import require_group
from .route_models import FeatureOverrideBody
from .service import (
    get_group,
    group_feature_rows,
    list_group_members,
    list_groups,
    list_guest_groups,
    page_meta,
    set_group_feature,
    set_user_feature,
    user_feature_rows,
)

router = APIRouter(prefix=API_PATH)


@router.get("/groups")
async def get_groups(
    _session: AdminReadSession,
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


@router.get("/guest/groups")
async def get_guest_groups(
    _session: AuthenticatedSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=100),
    search: str = Query(default="", max_length=120),
) -> dict[str, Any]:
    if _session.role != "guest":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "仅访客会话可访问")
    page, page_size = page_params(page, page_size)
    async with get_session() as db:
        rows, total = await list_guest_groups(
            db, page=page, page_size=page_size, search=search.strip()
        )
    return ok(rows, page_meta(page, page_size, total))


@router.get("/groups/{group_id}")
async def get_group_detail(group_id: int, _session: GroupViewSession) -> dict[str, Any]:
    async with get_session() as db:
        result = await get_group(db, group_id)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "群不存在")
    if _session.role == "guest":
        result = {key: result[key] for key in ("groupId", "groupName", "memberCount")}
    return ok(result)


@router.get("/groups/{group_id}/members")
async def get_members(
    group_id: int,
    _session: AdminReadSession,
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
    group_id: int, feature: str, body: FeatureOverrideBody, _session: AdminWriteSession
) -> dict[str, Any]:
    if feature not in FEATURE_REGISTRY:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "功能不存在")
    async with get_session() as db:
        await require_group(db, group_id)
        await set_group_feature(db, group_id, feature, body.override)
        if feature == "group_agent":
            agent_config = await get_or_create_config(db, group_id)
            if agent_config is None:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "Agent 配置暂时不可用",
                )
            # 通用功能的“继承”语义是回到默认开启，因此同时恢复专用总开关。
            agent_config.enabled = body.override is not False
        await db.commit()
        rows = await group_feature_rows(db, group_id)
    if feature == "group_agent" and body.override is False:
        close_group_conversations(group_id, reason="WebUI 群功能关闭 Agent 总开关")
    await hub.notify_change("group_feature", f"{group_id}:{feature}", group_id=group_id)
    if feature == "group_agent":
        await hub.notify_change("agent_config", str(group_id), group_id=group_id)
    return ok(next(row for row in rows if row["key"] == feature))


@router.get("/groups/{group_id}/members/{user_id}/features")
async def get_member_features(
    group_id: int, user_id: int, _session: AdminReadSession
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
    _session: AdminWriteSession,
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
    await hub.notify_change(
        "user_feature", f"{group_id}:{user_id}:{feature}", group_id=group_id
    )
    return ok(next(row for row in rows if row["key"] == feature))
