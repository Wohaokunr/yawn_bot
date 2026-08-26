# ruff: noqa: FAST002,TC001,TID252
"""Administrator endpoints for persistent WebUI guest access policy."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from nonebot_plugin_orm import get_session
from sqlalchemy import select

from ..data_models.bot_group import BotGroup
from ..data_models.guest_access import GuestGroupAccess
from .config import API_PATH
from .deps import AdminReadSession, AdminWriteSession, ok, page_params
from .guest_access import (
    apply_enabled,
    apply_new_credential,
    generate_guest_credential,
    get_or_create_config,
    policy_snapshot,
    utc_now,
)
from .hub import hub
from .route_models import GuestAccessPatch, GuestGroupAccessPatch
from .service import list_groups, page_meta

router = APIRouter(prefix=API_PATH)


def _serialize_policy(snapshot: Any) -> dict[str, Any]:
    return {
        "enabled": snapshot.enabled,
        "credentialConfigured": snapshot.credential_configured,
        "credentialVersion": snapshot.credential_version,
        "authorizedGroupCount": snapshot.authorized_group_count,
        "updatedAt": snapshot.updated_at.isoformat() if snapshot.updated_at else None,
    }


@router.get("/guest-access")
async def get_guest_access(_session: AdminReadSession) -> dict[str, Any]:
    async with get_session() as db:
        snapshot = await policy_snapshot(db)
    return ok(_serialize_policy(snapshot))


@router.patch("/guest-access")
async def patch_guest_access(
    body: GuestAccessPatch, _session: AdminWriteSession
) -> dict[str, Any]:
    async with get_session() as db:
        config = await get_or_create_config(db)
        if body.enabled and not config.credential_hash:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "请先生成访客访问码，再开启访客登录",
            )
        apply_enabled(config, enabled=body.enabled)
        await db.commit()
        await db.refresh(config)
        snapshot = await policy_snapshot(db)
    await hub.notify_change("guest_access", "policy")
    return ok(_serialize_policy(snapshot))


@router.post("/guest-access/credential")
async def rotate_guest_credential(_session: AdminWriteSession) -> dict[str, Any]:
    credential = generate_guest_credential()
    async with get_session() as db:
        config = await get_or_create_config(db)
        apply_new_credential(config, credential)
        await db.commit()
        await db.refresh(config)
        snapshot = await policy_snapshot(db)
    await hub.notify_change("guest_access", "credential")
    return ok({**_serialize_policy(snapshot), "credential": credential})


@router.get("/guest-access/groups")
async def get_guest_access_groups(
    _session: AdminReadSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=100),
    search: str = Query(default="", max_length=120),
) -> dict[str, Any]:
    page, page_size = page_params(page, page_size)
    async with get_session() as db:
        rows, total = await list_groups(
            db,
            page=page,
            page_size=page_size,
            search=search.strip(),
        )
        group_ids = [int(row["groupId"]) for row in rows]
        allowed: set[int] = set()
        if group_ids:
            allowed = {
                int(group_id)
                for group_id in (
                    await db.execute(
                        select(GuestGroupAccess.group_id).where(
                            GuestGroupAccess.group_id.in_(group_ids)
                        )
                    )
                ).scalars()
            }
    return ok(
        [{**row, "guestAllowed": int(row["groupId"]) in allowed} for row in rows],
        page_meta(page, page_size, total),
    )


@router.patch("/guest-access/groups/{group_id}")
async def patch_guest_access_group(
    group_id: int,
    body: GuestGroupAccessPatch,
    _session: AdminWriteSession,
) -> dict[str, Any]:
    async with get_session() as db:
        if await db.get(BotGroup, group_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "群聊不存在")
        row = await db.get(GuestGroupAccess, group_id)
        if body.allowed:
            if row is None:
                db.add(GuestGroupAccess(group_id=group_id, updated_at=utc_now()))
            else:
                row.updated_at = utc_now()
        elif row is not None:
            await db.delete(row)
        await db.commit()
    await hub.notify_change("guest_access", str(group_id))
    return ok({"groupId": str(group_id), "guestAllowed": body.allowed})
