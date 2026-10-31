# ruff: noqa: FAST002,TC001,TID252
"""Global user administration endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from nonebot_plugin_orm import get_session

from .. import affinity
from ..checkin_service import checkin_overview
from ..permission import FEATURE_REGISTRY
from .config import API_PATH
from .deps import AdminReadSession, AdminWriteSession, ok, page_params
from .hub import hub
from .route_helpers import require_user
from .route_models import AffinityAdjustBody, FeatureOverrideBody
from .service import list_users, page_meta, set_user_feature, user_feature_rows

router = APIRouter(prefix=API_PATH)

@router.get("/users")
async def get_users(
    _session: AdminReadSession,
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
    user_id: int, _session: AdminReadSession
) -> dict[str, Any]:
    async with get_session() as db:
        await require_user(db, user_id)
        rows = await user_feature_rows(db, user_id, None)
    return ok(rows)


@router.patch("/users/{user_id}/features/{feature}")
async def patch_global_user_feature(
    user_id: int, feature: str, body: FeatureOverrideBody, _session: AdminWriteSession
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


async def _affinity_payload(db: Any, user_id: int) -> dict[str, Any]:
    snap = await affinity.snapshot(db, user_id)
    level = snap.level
    events = await affinity.recent_events(db, user_id, limit=30)
    breakdown = await affinity.source_breakdown(db, user_id, days=30)
    groups = await checkin_overview(db, user_id, active_only=False)
    return {
        "userId": str(user_id),
        "points": snap.points,
        "level": level.name,
        "levelIndex": level.index,
        "nextLevel": level.next_name,
        "toNext": level.to_next(snap.points),
        "progress": level.progress(snap.points),
        "today": snap.today,
        "available": snap.available and events is not None,
        "levels": [{"floor": floor, "name": name} for floor, name in affinity.LEVELS],
        "sources": {
            key: {"label": rule.label, "dailyCap": rule.daily_cap}
            for key, rule in affinity.SOURCE_RULES.items()
        },
        "breakdown30d": breakdown or {},
        "events": events or [],
        "groups": [
            {
                "groupId": str(item.group_id),
                "groupName": item.group_name,
                "groupAffinity": item.group_affinity,
                "checkedToday": item.checked_today,
                "streakDays": item.streak_days,
                "totalDays": item.total_days,
                "points": item.points,
                "exp": item.exp,
                "lastCheckinDate": (
                    item.last_checkin_date.isoformat()
                    if item.last_checkin_date
                    else None
                ),
            }
            for item in groups
        ],
    }


@router.get("/users/{user_id}/affinity")
async def get_user_affinity(user_id: int, _session: AdminReadSession) -> dict[str, Any]:
    """好感余额、等级、近 30 天来源、流水和各群签到。"""

    async with get_session() as db:
        await require_user(db, user_id)
        payload = await _affinity_payload(db, user_id)
    return ok(payload)


@router.post("/users/{user_id}/affinity/adjust")
async def adjust_user_affinity(
    user_id: int, body: AffinityAdjustBody, _session: AdminWriteSession
) -> dict[str, Any]:
    """管理员手动加减好感（记一条 manual 流水）。"""

    if body.delta == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "调整量不能为 0")
    async with get_session() as db:
        await require_user(db, user_id)
        change = await affinity.grant(
            db,
            user_id,
            "manual",
            body.delta,
            note=body.note.strip() or "管理员调整",
        )
        if not change.available:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "好感度流水表尚未迁移，请先执行 nb orm upgrade",
            )
        await db.commit()
        payload = await _affinity_payload(db, user_id)
    await hub.notify_change("user_affinity", str(user_id))
    return ok(payload)
