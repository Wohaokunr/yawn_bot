"""Overview and plugin-summary endpoints for the WebUI API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from .config import API_PATH
from .deps import ReadSession, ok
from .service import overview

router = APIRouter(prefix=API_PATH)

@router.get("/overview")
async def get_overview(_session: ReadSession) -> dict[str, Any]:
    return ok(await overview())


@router.get("/plugins")
async def get_plugins(_session: ReadSession) -> dict[str, Any]:
    snapshot = await overview()
    return ok(snapshot["plugins"])
