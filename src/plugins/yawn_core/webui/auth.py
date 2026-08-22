"""单运维 Token 登录、签名会话、CSRF 与登录限速。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import HTTPException, Request, WebSocket, status

from .config import COOKIE_NAME, config

_LOGIN_WINDOW_SECONDS = 300
_LOGIN_MAX_FAILURES = 5
_failures: dict[str, deque[float]] = defaultdict(deque)


@dataclass(frozen=True, slots=True)
class Session:
    session_id: str
    csrf_token: str
    issued_at: int
    expires_at: int

    @property
    def actor_fingerprint(self) -> str:
        return hashlib.sha256(self.session_id.encode()).hexdigest()[:16]


def _secret() -> bytes:
    token = config.webui_admin_token.get_secret_value().encode()
    return hashlib.sha256(b"YawnBot WebUI session\0" + token).digest()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def create_session(*, now: int | None = None) -> tuple[str, Session]:
    issued_at = int(time.time() if now is None else now)
    session = Session(
        session_id=secrets.token_hex(16),
        csrf_token=secrets.token_urlsafe(24),
        issued_at=issued_at,
        expires_at=issued_at + config.webui_session_ttl_hours * 3600,
    )
    payload = json.dumps(
        {
            "sid": session.session_id,
            "csrf": session.csrf_token,
            "iat": session.issued_at,
            "exp": session.expires_at,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    encoded = _b64encode(payload)
    signature = _b64encode(hmac.digest(_secret(), encoded.encode(), "sha256"))
    return f"{encoded}.{signature}", session


def verify_session(value: str | None, *, now: int | None = None) -> Session | None:
    if not value:
        return None
    try:
        encoded, supplied_signature = value.split(".", 1)
        expected = _b64encode(hmac.digest(_secret(), encoded.encode(), "sha256"))
        if not hmac.compare_digest(supplied_signature, expected):
            return None
        payload = json.loads(_b64decode(encoded))
        session = Session(
            session_id=str(payload["sid"]),
            csrf_token=str(payload["csrf"]),
            issued_at=int(payload["iat"]),
            expires_at=int(payload["exp"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    current = int(time.time() if now is None else now)
    if session.issued_at > current + 60 or session.expires_at <= current:
        return None
    return session


def client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def login_allowed(key: str, *, now: float | None = None) -> bool:
    current = time.monotonic() if now is None else now
    failures = _failures[key]
    while failures and current - failures[0] >= _LOGIN_WINDOW_SECONDS:
        failures.popleft()
    return len(failures) < _LOGIN_MAX_FAILURES


def check_admin_token(key: str, supplied: str, *, now: float | None = None) -> bool:
    current = time.monotonic() if now is None else now
    if not login_allowed(key, now=current):
        return False
    expected = config.webui_admin_token.get_secret_value()
    valid = hmac.compare_digest(supplied.encode(), expected.encode())
    if valid:
        _failures.pop(key, None)
    else:
        _failures[key].append(current)
    return valid


def require_session(request: Request) -> Session:
    session = verify_session(request.cookies.get(COOKIE_NAME))
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已失效")
    return session


def require_csrf(request: Request, session: Session) -> None:
    supplied = request.headers.get("X-CSRF-Token", "")
    if not hmac.compare_digest(supplied, session.csrf_token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF 校验失败")


def websocket_session(websocket: WebSocket) -> Session | None:
    return verify_session(websocket.cookies.get(COOKIE_NAME))


def reset_login_failures_for_tests() -> None:
    _failures.clear()
