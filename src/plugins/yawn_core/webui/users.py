# ruff: noqa: FAST002,TC001,TID252,PLR0913,PLR0917
"""Global user administration endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from nonebot_plugin_orm import get_session

from .. import affinity
from ..checkin_service import checkin_overview
from ..permission import FEATURE_REGISTRY
from ..private_chat_bridge import (
    PRIVATE_MEMORY_LABELS,
    auto_paused,
    auto_paused_user_ids,
    delete_private_memories,
    get_private_handler,
    private_agent_config,
    private_memory_query,
    set_auto_paused,
    turns_today,
)
from .config import API_PATH
from .deps import AdminReadSession, AdminWriteSession, ok, page_params
from .directory import (
    BATCH_LIMIT,
    USER_SORTS,
    existing_users,
    list_users_v2,
    user_profile,
    users_summary,
)
from .hub import hub
from .route_helpers import require_user
from .route_models import (
    AffinityAdjustBody,
    FeatureBatchBody,
    FeatureOverrideBody,
    PrivateAgentPatchBody,
)
from .service import (
    BEIJING_TZ,
    iso,
    page_meta,
    set_user_feature,
    user_feature_rows,
)

router = APIRouter(prefix=API_PATH)


@router.get("/users")
async def get_users(
    _session: AdminReadSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=100),
    search: str = Query(default="", max_length=120),
    overrides: str = Query(default="", pattern="^(|private|member|any)$"),
    activity: str = Query(default="", pattern="^(|24h|7d|30d|dormant)$"),
    sort: str = Query(default="recent", max_length=20),
) -> dict[str, Any]:
    page, page_size = page_params(page, page_size)
    async with get_session() as db:
        rows, total = await list_users_v2(
            db,
            page=page,
            page_size=page_size,
            search=search.strip(),
            overrides=overrides,
            activity=activity,
            sort=sort if sort in USER_SORTS else "recent",
        )
    return ok(rows, page_meta(page, page_size, total))


@router.get("/users/summary")
async def get_users_summary(_session: AdminReadSession) -> dict[str, Any]:
    """用户页状态条：总数、活跃、有覆盖、今日签到、私聊 Agent 暂停人数。"""

    async with get_session() as db:
        return ok(await users_summary(db, len(auto_paused_user_ids())))


@router.post("/users/batch/features")
async def batch_user_feature(
    body: FeatureBatchBody, _session: AdminWriteSession
) -> dict[str, Any]:
    """给多位用户同时设置同一个私聊功能覆盖（单事务）。"""

    if body.feature not in FEATURE_REGISTRY:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "功能不存在")
    user_ids: list[int] = []
    for item in body.ids:
        text = str(item).strip()
        if not text.isdigit():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"QQ 号格式不对：{text}")
        if int(text) not in user_ids:
            user_ids.append(int(text))
    if len(user_ids) > BATCH_LIMIT:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"一次最多处理 {BATCH_LIMIT} 个"
        )
    async with get_session() as db:
        found = await existing_users(db, user_ids)
        missing = [str(item) for item in user_ids if item not in found]
        if missing:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"用户不存在：{'、'.join(missing[:5])}"
            )
        for user_id in user_ids:
            await set_user_feature(
                db, user_id, body.feature, body.override, group_id=None
            )
        await db.commit()
    for user_id in user_ids:
        await hub.notify_change("global_user_feature", f"{user_id}:{body.feature}")
    return ok({"updated": len(user_ids), "feature": body.feature})


@router.get("/users/{user_id}")
async def get_user_profile(user_id: int, _session: AdminReadSession) -> dict[str, Any]:
    """用户档案：好感等级、所在各群的身份/签到/群好感/本群覆盖数、私聊覆盖数。"""

    async with get_session() as db:
        user = await require_user(db, user_id)
        return ok(await user_profile(db, user))


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


# ── 私聊 Agent ──────────────────────────────────────────────

_SOURCE_LABELS = {"agent": "本人要求记的", "auto": "聊天里整理的"}


async def _private_agent_payload(db: Any, user_id: int) -> dict[str, Any]:
    now = datetime.now(BEIJING_TZ).replace(tzinfo=None)
    try:
        rows = (await db.execute(private_memory_query(user_id))).scalars().all()
        memory_available = True
    except Exception:  # noqa: BLE001 - 记忆表未迁移时只读降级
        rows, memory_available = [], False
    facts = [
        {
            "key": str(row.memory_key),
            "label": PRIVATE_MEMORY_LABELS.get(
                str(row.memory_key), str(row.memory_key)
            ),
            "content": str(row.content or ""),
            "source": str(row.source_kind or ""),
            "sourceLabel": _SOURCE_LABELS.get(str(row.source_kind), "管理员设置的"),
            "updatedAt": iso(row.updated_at),
            "expiresAt": iso(row.expires_at),
            "expired": row.expires_at is not None and row.expires_at < now,
        }
        for row in sorted(rows, key=lambda item: str(item.memory_key))
    ]
    cfg = private_agent_config
    return {
        "userId": str(user_id),
        "enabled": bool(cfg.private_agent_enabled),
        "agentLoaded": get_private_handler() is not None,
        "autoReply": bool(cfg.private_agent_auto_reply),
        "friendsOnly": bool(cfg.private_agent_friends_only),
        "dailyTurns": int(cfg.private_agent_daily_turns),
        "autoPaused": auto_paused(user_id),
        "turnsToday": await turns_today(db, user_id, now),
        "memoryAvailable": memory_available,
        "facts": facts,
    }


@router.get("/users/{user_id}/private-agent")
async def get_user_private_agent(
    user_id: int, _session: AdminReadSession
) -> dict[str, Any]:
    """私聊 Agent 对这位用户的状态：是否暂停自动回复、今日轮数、私聊记忆。"""

    async with get_session() as db:
        await require_user(db, user_id)
        payload = await _private_agent_payload(db, user_id)
    return ok(payload)


@router.patch("/users/{user_id}/private-agent")
async def patch_user_private_agent(
    user_id: int, body: PrivateAgentPatchBody, _session: AdminWriteSession
) -> dict[str, Any]:
    """暂停或恢复对这位用户的自动回复（与本人发 /退出、/对话 等价）。"""

    async with get_session() as db:
        await require_user(db, user_id)
        set_auto_paused(user_id, paused=body.auto_paused)
        payload = await _private_agent_payload(db, user_id)
    await hub.notify_change("user_private_agent", str(user_id))
    return ok(payload)


@router.delete("/users/{user_id}/private-agent/memories")
async def delete_user_private_memories(
    user_id: int,
    _session: AdminWriteSession,
    key: str = Query(default="", max_length=64),
) -> dict[str, Any]:
    """删除这位用户的私聊记忆；带 key 只删一个类别。不影响群里的记忆。"""

    if key and key not in PRIVATE_MEMORY_LABELS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不支持的记忆类别")
    async with get_session() as db:
        await require_user(db, user_id)
        removed = await delete_private_memories(db, user_id, key=key or None)
        await db.commit()
        payload = await _private_agent_payload(db, user_id)
    await hub.notify_change("user_private_agent", str(user_id))
    return ok({**payload, "removed": removed})
