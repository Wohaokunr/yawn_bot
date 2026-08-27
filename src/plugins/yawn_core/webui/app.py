# ruff: noqa: C901,PERF203,PLR0915,PLR2004,TID252,TRY003,TRY004
"""FastAPI application assembly for the YawnBot WebUI."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

from fastapi import (
    FastAPI,
    HTTPException,
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

from ..data_models.web_admin_audit import WebAdminAudit
from .agent import router as agent_router
from .audits import router as audits_router
from .auth import Session, verify_session, websocket_session
from .auth_routes import router as auth_router
from .config import API_PATH, BASE_PATH, COOKIE_NAME, DIST_DIR
from .environment_routes import router as environment_router
from .fanqie import router as fanqie_router
from .games import router as games_router
from .groups import router as groups_router
from .guest_access_routes import router as guest_access_router
from .hub import hub
from .overview_routes import router as overview_router
from .rpg_modules import router as rpg_modules_router
from .service import BEIJING_TZ, overview
from .users import router as users_router

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


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
                        "actorRole": session.role,
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


def register(app: FastAPI, *, include_spa: bool = True) -> None:
    app.include_router(auth_router)
    app.include_router(overview_router)
    app.include_router(environment_router)
    app.include_router(guest_access_router)
    app.include_router(groups_router)
    app.include_router(users_router)
    app.include_router(agent_router)
    app.include_router(audits_router)
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
        if session.role != "admin":
            await websocket.close(code=4403)
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

    if include_spa:
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
            if (
                spa_path
                and DIST_DIR.resolve() in requested.parents
                and requested.is_file()
            ):
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
