# ruff: noqa: FAST002,TID252
"""Agent and WebUI audit log endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from nonebot_plugin_orm import get_session
from sqlalchemy import func, select

from ..data_models.agent_audit import AgentAudit
from ..data_models.web_admin_audit import WebAdminAudit
from .config import API_PATH
from .deps import AdminReadSession, ok, page_params
from .service import page_meta, serialize_agent_audit, serialize_web_audit

router = APIRouter(prefix=API_PATH)

@router.get("/agent/audits")
async def get_agent_audits(
    _session: AdminReadSession,
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
    _session: AdminReadSession,
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
