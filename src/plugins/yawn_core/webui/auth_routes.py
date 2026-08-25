# ruff: noqa: TC001
"""Authentication endpoints for the WebUI API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status

from .auth import check_admin_token, client_key, create_session, login_allowed
from .config import API_PATH, BASE_PATH, COOKIE_NAME, config
from .deps import ReadSession, WriteSession, ok
from .route_models import LoginBody

router = APIRouter(prefix=API_PATH)

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
