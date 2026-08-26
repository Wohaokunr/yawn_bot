# ruff: noqa: TC001
"""Authentication endpoints for the WebUI API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status

from .auth import (
    Session,
    check_admin_token,
    clear_login_failures,
    client_key,
    create_session,
    login_allowed,
    record_login_failure,
)
from .config import API_PATH, BASE_PATH, COOKIE_NAME, config
from .deps import AuthenticatedSession, AuthenticatedWriteSession, ok
from .guest_access import authenticate_guest_credential
from .route_models import LoginBody

router = APIRouter(prefix=API_PATH)


def _set_session_cookie(response: Response, cookie: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        cookie,
        max_age=config.webui_session_ttl_hours * 3600,
        httponly=True,
        secure=config.webui_cookie_secure,
        samesite="strict",
        path=BASE_PATH,
    )


def _session_payload(session: Session) -> dict[str, Any]:
    is_admin = session.role == "admin"
    return {
        "authenticated": True,
        "role": session.role,
        "csrfToken": session.csrf_token,
        "expiresAt": session.expires_at,
        "capabilities": {
            "adminConsole": is_admin,
            "adminWrite": is_admin,
            "realtimeAdminStream": is_admin,
            "guestGroupRead": session.role == "guest",
        },
    }

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
    cookie, session = create_session(role="admin")
    _set_session_cookie(response, cookie)
    return ok(_session_payload(session))


@router.post("/auth/guest")
async def guest_login(
    request: Request, body: LoginBody, response: Response
) -> dict[str, Any]:
    key = f"guest:{client_key(request)}"
    if not login_allowed(key):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "登录尝试过多，请稍后重试"
        )
    policy = await authenticate_guest_credential(body.token)
    if policy is None:
        record_login_failure(key)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "访客访问码无效，或访客登录当前未开启",
        )
    clear_login_failures(key)
    cookie, session = create_session(
        role="guest", credential_version=policy.credential_version
    )
    _set_session_cookie(response, cookie)
    return ok(_session_payload(session))


@router.post("/auth/logout")
async def logout(
    response: Response, _session: AuthenticatedWriteSession
) -> dict[str, Any]:
    response.delete_cookie(COOKIE_NAME, path=BASE_PATH)
    return ok({"authenticated": False})


@router.get("/auth/session")
async def get_auth_session(session: AuthenticatedSession) -> dict[str, Any]:
    return ok(_session_payload(session))
