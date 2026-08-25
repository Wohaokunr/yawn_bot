# ruff: noqa: FAST002,TC001,TID252
"""Global user administration endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from nonebot_plugin_orm import get_session

from ..permission import FEATURE_REGISTRY
from .config import API_PATH
from .deps import ReadSession, WriteSession, ok, page_params
from .hub import hub
from .route_helpers import require_user
from .route_models import FeatureOverrideBody
from .service import list_users, page_meta, set_user_feature, user_feature_rows

router = APIRouter(prefix=API_PATH)

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
