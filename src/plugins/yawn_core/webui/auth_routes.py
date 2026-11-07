# ruff: noqa: TC001, TID252
"""Authentication endpoints for the WebUI API."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from nonebot import logger

from .. import webui_accounts, webui_devices
from .auth import (
    Session,
    check_admin_token,
    clear_login_failures,
    client_ip,
    client_key,
    create_session,
    login_allowed,
    login_retry_after,
    record_login_failure,
    renew,
    set_session_cookie,
)
from .config import API_PATH, BASE_PATH, COOKIE_NAME, config
from .deps import AuthenticatedSession, AuthenticatedWriteSession, ok
from .guest_access import authenticate_guest_credential
from .route_models import LoginBody
from .user_account_models import UserLoginBody, UserSetupBody

router = APIRouter(prefix=API_PATH)


def _set_session_cookie(response: Response, cookie: str, session: Session) -> None:
    set_session_cookie(response, cookie, session)


def _session_payload(session: Session) -> dict[str, Any]:
    is_admin = session.role == "admin"
    payload: dict[str, Any] = {
        "authenticated": True,
        "role": session.role,
        "csrfToken": session.csrf_token,
        "expiresAt": session.expires_at,
        "remember": session.remember,
        "rememberDays": config.webui_remember_days,
        "capabilities": {
            "adminConsole": is_admin,
            "adminWrite": is_admin,
            "realtimeAdminStream": is_admin,
            "guestGroupRead": session.role == "guest",
            "selfService": session.role == "user",
        },
    }
    if session.role == "user" and session.user_id is not None:
        payload["userId"] = str(session.user_id)
    return payload


# 个人账号登录限速：按来源 IP 一层、按 QQ 号一层（防止换 IP 慢慢猜同一个号）。
USER_IP_MAX_FAILURES = 20
USER_IP_WINDOW = 300.0
USER_ACCOUNT_MAX_FAILURES = 8
USER_ACCOUNT_WINDOW = 900.0


def _user_limit_keys(request: Request, qq: str, kind: str) -> tuple[str, str]:
    return f"{kind}-ip:{client_key(request)}", f"{kind}-qq:{qq}"


def _check_user_limits(ip_key: str, account_key: str) -> None:
    for key, max_failures, window in (
        (ip_key, USER_IP_MAX_FAILURES, USER_IP_WINDOW),
        (account_key, USER_ACCOUNT_MAX_FAILURES, USER_ACCOUNT_WINDOW),
    ):
        if not login_allowed(key, max_failures=max_failures, window=window):
            retry = login_retry_after(key, window=window)
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"尝试次数过多，请 {max(1, retry // 60)} 分钟后再试",
                headers={"Retry-After": str(retry)},
            )


def _record_user_failure(ip_key: str, account_key: str) -> None:
    record_login_failure(ip_key, window=USER_IP_WINDOW)
    record_login_failure(account_key, window=USER_ACCOUNT_WINDOW)


def _require_user_login_enabled() -> None:
    if not config.webui_user_login_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "个人账号登录当前未开启")


# 登录提醒是后台任务：留个强引用，免得还没发完就被回收。
_ALERT_TASKS: set[asyncio.Task[None]] = set()


def _schedule_login_alert(user_id: int, event: dict[str, Any]) -> None:
    if not config.webui_login_alert or not webui_devices.should_alert(event):
        return

    async def _send() -> None:
        try:
            from ..webui_account_commands import send_login_alert

            await send_login_alert(user_id, event)
        except Exception:  # noqa: BLE001
            logger.opt(exception=True).warning("网页账号：登录提醒发送失败(忽略)")

    try:
        task = asyncio.get_running_loop().create_task(_send())
    except RuntimeError:
        return
    _ALERT_TASKS.add(task)
    task.add_done_callback(_ALERT_TASKS.discard)


def _start_user_session(
    request: Request,
    response: Response,
    user_id: int,
    *,
    remember: bool,
    method: str,
) -> dict[str, Any]:
    version = webui_accounts.session_version(user_id)
    if version is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "QQ 号或密码不正确")
    webui_accounts.record_login(user_id)
    cookie, session = create_session(
        role="user", credential_version=version, user_id=user_id, remember=remember
    )
    event = webui_devices.register_session(
        user_id,
        session.session_id,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        remember=remember,
        expires_at=session.expires_at,
        method=method,
    )
    _schedule_login_alert(user_id, event)
    _set_session_cookie(response, cookie, session)
    payload = _session_payload(session)
    # 给刚登录的页面看一眼“上次登录”和“这次是不是新地点”
    payload["login"] = {
        "newLocation": event["newLocation"],
        "location": event["location"],
        "device": event["device"],
        "activeCount": event["activeCount"],
        "previous": event["previous"],
    }
    return ok(payload)


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
    cookie, session = create_session(role="admin", remember=body.remember)
    _set_session_cookie(response, cookie, session)
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
        role="guest",
        credential_version=policy.credential_version,
        remember=body.remember,
    )
    _set_session_cookie(response, cookie, session)
    return ok(_session_payload(session))


@router.post("/auth/user")
async def user_login(
    request: Request, body: UserLoginBody, response: Response
) -> dict[str, Any]:
    """QQ 号 + 私聊里设的密码登录个人页面。错误信息不区分“没开通”和“密码错”。"""

    _require_user_login_enabled()
    ip_key, account_key = _user_limit_keys(request, body.qq, "user")
    _check_user_limits(ip_key, account_key)
    user_id = int(body.qq)
    encoded = webui_accounts.password_hash_for(user_id)
    if encoded is None:
        await asyncio.to_thread(webui_accounts.burn_password_check, body.password)
        valid = False
    else:
        valid = await asyncio.to_thread(
            webui_accounts.verify_password, body.password, encoded
        )
    if not valid:
        _record_user_failure(ip_key, account_key)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "QQ 号或密码不正确")
    clear_login_failures(account_key)
    return _start_user_session(
        request, response, user_id, remember=body.remember, method="password"
    )


@router.post("/auth/user/setup")
async def user_setup(
    request: Request, body: UserSetupBody, response: Response
) -> dict[str, Any]:
    """用私聊里拿到的一次性设置码设密码，设好直接登录。"""

    _require_user_login_enabled()
    ip_key, account_key = _user_limit_keys(request, body.qq, "setup")
    _check_user_limits(ip_key, account_key)
    user_id = int(body.qq)
    try:
        await asyncio.to_thread(
            webui_accounts.redeem_setup_code, user_id, body.code, body.password
        )
    except webui_accounts.AccountError as exc:
        if exc.code == "bad_code":
            _record_user_failure(ip_key, account_key)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    clear_login_failures(account_key)
    return _start_user_session(
        request, response, user_id, remember=body.remember, method="setup_code"
    )


@router.post("/auth/logout")
async def logout(
    response: Response, session: AuthenticatedWriteSession
) -> dict[str, Any]:
    if session.role == "user" and session.user_id is not None:
        webui_devices.forget_session(session.user_id, session.session_id)
    response.delete_cookie(COOKIE_NAME, path=BASE_PATH)
    return ok({"authenticated": False})


@router.get("/auth/session")
async def get_auth_session(
    session: AuthenticatedSession, response: Response
) -> dict[str, Any]:
    """读会话；剩余有效期不到一半时顺手续签（同一个设备、同一个 CSRF）。

    前端打开页面和定时保活都会调这里，所以“自动登录”只要隔段时间用一次就一直有效。
    """

    renewed = renew(session)
    if renewed is not None:
        cookie, session = renewed
        _set_session_cookie(response, cookie, session)
        if session.role == "user" and session.user_id is not None:
            webui_devices.check_session(
                session.user_id,
                session.session_id,
                expires_at=session.expires_at,
                remember=session.remember,
            )
    return ok(_session_payload(session))
