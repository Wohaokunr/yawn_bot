# ruff: noqa: FAST002,TC001,TID252,PLR0913,PLR0917,FBT001
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
from .directory import (
    BATCH_LIMIT,
    GROUP_SORTS,
    MEMBER_SORTS,
    existing_groups,
    existing_members,
    group_member_overrides,
    group_overview,
    groups_summary,
    list_group_members_v2,
    list_groups_v2,
    reset_group_features,
    reset_member_features,
)
from .hub import hub
from .route_helpers import require_group
from .route_models import FeatureBatchBody, FeatureOverrideBody, GroupFeatureResetBody
from .service import (
    get_group,
    group_feature_rows,
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
    agent: str = Query(default="", pattern="^(|on|off)$"),
    overrides: str = Query(default="", pattern="^(|group|member|any)$"),
    active: str = Query(default="", pattern="^(|24h|7d|30d|dormant)$"),
    sort: str = Query(default="active", max_length=20),
) -> dict[str, Any]:
    page, page_size = page_params(page, page_size)
    async with get_session() as db:
        rows, total = await list_groups_v2(
            db,
            page=page,
            page_size=page_size,
            search=search.strip(),
            agent=agent,
            overrides=overrides,
            active=active,
            sort=sort if sort in GROUP_SORTS else "active",
        )
    return ok(rows, page_meta(page, page_size, total))


@router.get("/groups/summary")
async def get_groups_summary(_session: AdminReadSession) -> dict[str, Any]:
    """群组页状态条：群总数、Agent 开启数、活跃群、有覆盖的群、今日签到。"""

    async with get_session() as db:
        return ok(await groups_summary(db))


def _parse_ids(raw: list[str], label: str) -> list[int]:
    ids: list[int] = []
    for item in raw:
        text = str(item).strip()
        if not text.isdigit():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{label}格式不对：{text}")
        value = int(text)
        if value not in ids:
            ids.append(value)
    if len(ids) > BATCH_LIMIT:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"一次最多处理 {BATCH_LIMIT} 个"
        )
    return ids


async def _apply_group_feature(
    db: Any, group_id: int, feature: str, override: bool | None
) -> None:
    """写群功能覆盖；group_agent 同步专用总开关（与单群 PATCH 同语义）。"""

    await set_group_feature(db, group_id, feature, override)
    if feature == "group_agent":
        agent_config = await get_or_create_config(db, group_id)
        if agent_config is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Agent 配置暂时不可用",
            )
        # 通用功能的“继承”语义是回到默认开启，因此同时恢复专用总开关。
        agent_config.enabled = override is not False


async def _notify_group_feature(group_id: int, feature: str) -> None:
    await hub.notify_change("group_feature", f"{group_id}:{feature}", group_id=group_id)
    if feature == "group_agent":
        await hub.notify_change("agent_config", str(group_id), group_id=group_id)


@router.post("/groups/batch/features")
async def batch_group_feature(
    body: FeatureBatchBody, _session: AdminWriteSession
) -> dict[str, Any]:
    """给多个群同时设置同一个功能覆盖（单事务，不存在的群会被拒绝）。"""

    if body.feature not in FEATURE_REGISTRY:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "功能不存在")
    group_ids = _parse_ids(body.ids, "群号")
    async with get_session() as db:
        found = await existing_groups(db, group_ids)
        missing = [str(item) for item in group_ids if item not in found]
        if missing:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"群不存在：{'、'.join(missing[:5])}"
            )
        for group_id in group_ids:
            await _apply_group_feature(db, group_id, body.feature, body.override)
        await db.commit()
    for group_id in group_ids:
        if body.feature == "group_agent" and body.override is False:
            close_group_conversations(group_id, reason="WebUI 批量关闭 Agent 总开关")
        await _notify_group_feature(group_id, body.feature)
    return ok({"updated": len(group_ids), "feature": body.feature})


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
    role: str = Query(default="", pattern="^(|owner|admin|member)$"),
    activity: str = Query(default="", pattern="^(|24h|7d|30d|dormant)$"),
    overrides: bool = Query(default=False),
    sort: str = Query(default="recent", max_length=20),
) -> dict[str, Any]:
    page, page_size = page_params(page, page_size)
    async with get_session() as db:
        await require_group(db, group_id)
        rows, total = await list_group_members_v2(
            db,
            group_id,
            page=page,
            page_size=page_size,
            search=search.strip(),
            role=role,
            activity=activity,
            overrides=overrides,
            sort=sort if sort in MEMBER_SORTS else "recent",
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
        await _apply_group_feature(db, group_id, feature, body.override)
        await db.commit()
        rows = await group_feature_rows(db, group_id)
    if feature == "group_agent" and body.override is False:
        close_group_conversations(group_id, reason="WebUI 群功能关闭 Agent 总开关")
    await _notify_group_feature(group_id, feature)
    return ok(next(row for row in rows if row["key"] == feature))


@router.post("/groups/{group_id}/features/reset")
async def reset_group_feature_overrides(
    group_id: int, body: GroupFeatureResetBody, _session: AdminWriteSession
) -> dict[str, Any]:
    """群功能全部恢复继承。默认保留群聊 Agent 总开关，避免误把 Agent 打开。"""

    async with get_session() as db:
        await require_group(db, group_id)
        removed = await reset_group_features(
            db, group_id, include_agent=body.include_agent
        )
        if body.include_agent:
            agent_config = await get_or_create_config(db, group_id)
            if agent_config is not None:
                agent_config.enabled = True
        await db.commit()
        rows = await group_feature_rows(db, group_id)
    for feature in removed:
        await _notify_group_feature(group_id, feature)
    if body.include_agent and "group_agent" not in removed:
        await hub.notify_change("agent_config", str(group_id), group_id=group_id)
    return ok({"removed": removed, "features": rows})


@router.get("/groups/{group_id}/overview")
async def get_group_overview(
    group_id: int, _session: AdminReadSession
) -> dict[str, Any]:
    """群概览：角色分布、活跃成员、签到、连签榜、群好感榜、Agent 状态与覆盖数。"""

    async with get_session() as db:
        await require_group(db, group_id)
        return ok(await group_overview(db, group_id))


@router.get("/groups/{group_id}/overrides")
async def get_group_member_overrides(
    group_id: int, _session: AdminReadSession
) -> dict[str, Any]:
    """本群全部成员级功能覆盖。"""

    async with get_session() as db:
        await require_group(db, group_id)
        return ok(await group_member_overrides(db, group_id))


@router.post("/groups/{group_id}/members/batch/features")
async def batch_member_feature(
    group_id: int, body: FeatureBatchBody, _session: AdminWriteSession
) -> dict[str, Any]:
    """给本群多个成员同时设置同一个功能覆盖。"""

    if body.feature not in FEATURE_REGISTRY:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "功能不存在")
    user_ids = _parse_ids(body.ids, "QQ 号")
    async with get_session() as db:
        await require_group(db, group_id)
        found = await existing_members(db, group_id, user_ids)
        missing = [str(item) for item in user_ids if item not in found]
        if missing:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"不是本群成员：{'、'.join(missing[:5])}",
            )
        for user_id in user_ids:
            await set_user_feature(
                db, user_id, body.feature, body.override, group_id=group_id
            )
        await db.commit()
    for user_id in user_ids:
        await hub.notify_change(
            "user_feature", f"{group_id}:{user_id}:{body.feature}", group_id=group_id
        )
    return ok({"updated": len(user_ids), "feature": body.feature})


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


@router.delete("/groups/{group_id}/members/{user_id}/features")
async def reset_member_feature_overrides(
    group_id: int, user_id: int, _session: AdminWriteSession
) -> dict[str, Any]:
    """清掉这位成员在本群的全部功能覆盖，回到跟随群设置。"""

    async with get_session() as db:
        membership = await db.get(UserGroup, (group_id, user_id))
        if membership is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "群成员不存在")
        removed = await reset_member_features(db, group_id, user_id)
        await db.commit()
        rows = await user_feature_rows(db, user_id, group_id)
    await hub.notify_change(
        "user_feature", f"{group_id}:{user_id}:*", group_id=group_id
    )
    return ok({"removed": removed, "features": rows})
