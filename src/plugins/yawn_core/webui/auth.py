"""运维 Token / 访客 / QQ 个人账号登录、签名会话、CSRF 与登录限速。"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal, cast

from fastapi import HTTPException, Request, Response, WebSocket, status

from .config import BASE_PATH, COOKIE_NAME, DIST_DIR, config

# 会话签名密钥 = 服务器本地随机密钥 + 运维 Token：
# Token 偏短时，拿到一份 Cookie 也无法离线穷举出 Token；改 Token 仍会让旧会话失效。
SESSION_KEY_PATH = Path(
    os.environ.get("YAWNBOT_WEBUI_SESSION_KEY_FILE")
    or DIST_DIR.parents[1] / "data" / "webui" / "session.key"
)

_LOGIN_WINDOW_SECONDS = 300
_LOGIN_MAX_FAILURES = 5
_failures: dict[str, deque[float]] = defaultdict(deque)

SessionRole = Literal["admin", "guest", "user"]
_ROLES = frozenset({"admin", "guest", "user"})


@dataclass(frozen=True, slots=True)
class Session:
    session_id: str
    csrf_token: str
    issued_at: int
    expires_at: int
    role: SessionRole
    credential_version: int | None = None
    # 个人账号会话（role="user"）对应的 QQ 号；其余角色为 None
    user_id: int | None = None
    # 登录时勾了“自动登录”：长效 Cookie（WEBUI_REMEMBER_DAYS），否则浏览器关掉就失效
    remember: bool = False

    @property
    def lifetime_seconds(self) -> int:
        return session_lifetime_seconds(remember=self.remember)

    @property
    def actor_fingerprint(self) -> str:
        return hashlib.sha256(self.session_id.encode()).hexdigest()[:16]


_SERVER_KEY_BYTES = 32


@lru_cache(maxsize=1)
def _server_key() -> bytes:
    """读取或生成持久化的本地签名密钥；写不进磁盘时退化为进程内随机值。"""

    with contextlib.suppress(OSError):
        existing = SESSION_KEY_PATH.read_bytes()
        if len(existing) >= _SERVER_KEY_BYTES:
            return existing[:64]
    key = secrets.token_bytes(_SERVER_KEY_BYTES)
    with contextlib.suppress(OSError):
        SESSION_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(SESSION_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
    with contextlib.suppress(OSError):
        # 并发启动时另一个进程可能先写好了，以磁盘上的为准
        stored = SESSION_KEY_PATH.read_bytes()
        if len(stored) >= _SERVER_KEY_BYTES:
            return stored[:64]
    return key


def _secret() -> bytes:
    token = config.webui_admin_token.get_secret_value().encode()
    return hashlib.sha256(
        b"YawnBot WebUI session v2\0" + _server_key() + b"\0" + token
    ).digest()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def session_lifetime_seconds(*, remember: bool) -> int:
    if remember:
        return config.webui_remember_days * 86400
    return config.webui_session_ttl_hours * 3600


def create_session(  # noqa: PLR0913
    *,
    role: SessionRole = "admin",
    credential_version: int | None = None,
    user_id: int | None = None,
    remember: bool = False,
    session_id: str | None = None,
    csrf_token: str | None = None,
    now: int | None = None,
) -> tuple[str, Session]:
    """签发会话。

    续期时传入原 ``session_id`` / ``csrf_token``，设备身份和其他标签页不受影响。
    """

    if (role == "user") != (user_id is not None):
        msg = "个人账号会话必须且只能带 user_id"
        raise ValueError(msg)
    issued_at = int(time.time() if now is None else now)
    session = Session(
        session_id=session_id or secrets.token_hex(16),
        csrf_token=csrf_token or secrets.token_urlsafe(24),
        issued_at=issued_at,
        expires_at=issued_at + session_lifetime_seconds(remember=remember),
        role=role,
        credential_version=credential_version,
        user_id=user_id,
        remember=bool(remember),
    )
    claims: dict[str, object] = {
        "sid": session.session_id,
        "csrf": session.csrf_token,
        "iat": session.issued_at,
        "exp": session.expires_at,
        "role": session.role,
        "cv": session.credential_version,
    }
    if user_id is not None:
        claims["uid"] = int(user_id)
    if remember:
        claims["rem"] = 1
    payload = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
    encoded = _b64encode(payload)
    signature = _b64encode(hmac.digest(_secret(), encoded.encode(), "sha256"))
    return f"{encoded}.{signature}", session


def verify_session(  # noqa: PLR0911
    value: str | None, *, now: int | None = None
) -> Session | None:
    if not value:
        return None
    try:
        encoded, supplied_signature = value.split(".", 1)
        expected = _b64encode(hmac.digest(_secret(), encoded.encode(), "sha256"))
        if not hmac.compare_digest(supplied_signature, expected):
            return None
        payload = json.loads(_b64decode(encoded))
        role = str(payload.get("role", "admin"))
        if role not in _ROLES:
            return None
        raw_uid = payload.get("uid")
        user_id = int(raw_uid) if raw_uid is not None else None
        if (role == "user") != (user_id is not None):
            return None
        session = Session(
            session_id=str(payload["sid"]),
            csrf_token=str(payload["csrf"]),
            issued_at=int(payload["iat"]),
            expires_at=int(payload["exp"]),
            role=cast("SessionRole", role),
            credential_version=(
                int(payload["cv"]) if payload.get("cv") is not None else None
            ),
            user_id=user_id,
            remember=bool(payload.get("rem")),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    current = int(time.time() if now is None else now)
    if session.issued_at > current + 60 or session.expires_at <= current:
        return None
    return session


def set_session_cookie(response: Response, cookie: str, session: Session) -> None:
    """“自动登录”给长效 Cookie；否则是浏览器会话 Cookie，关掉浏览器就要重新登录。"""

    max_age = (
        max(1, session.expires_at - int(time.time())) if session.remember else None
    )
    response.set_cookie(
        COOKIE_NAME,
        cookie,
        max_age=max_age,
        httponly=True,
        secure=config.webui_cookie_secure,
        samesite="strict",
        path=BASE_PATH,
    )


def renew(session: Session, *, now: int | None = None) -> tuple[str, Session] | None:
    """剩余有效期不到一半时用同一个 sid / CSRF 续签；不需要续期返回 None。

    打开着的页面和“自动登录”的浏览器因此不会在使用中途突然掉线。
    """

    current = int(time.time() if now is None else now)
    if session.expires_at - current >= session.lifetime_seconds // 2:
        return None
    return create_session(
        role=session.role,
        credential_version=session.credential_version,
        user_id=session.user_id,
        remember=session.remember,
        session_id=session.session_id,
        csrf_token=session.csrf_token,
        now=current,
    )


def _trusted_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    return _parse_networks(config.webui_trusted_proxies)


# Cloudflare 公布的回源 IP 段（https://www.cloudflare.com/ips/）。
# WEBUI_TRUSTED_PROXIES 里写 cloudflare 即展开为这些段；
# 它们多年未变，若官方更新可再手写追加。
CLOUDFLARE_RANGES = (
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)


@lru_cache(maxsize=8)
def _parse_networks(raw: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    networks = []
    for part in str(raw or "").replace(";", ",").split(","):
        item = part.strip()
        if not item:
            continue
        items = CLOUDFLARE_RANGES if item.lower() == "cloudflare" else (item,)
        for value in items:
            with contextlib.suppress(ValueError):
                networks.append(ipaddress.ip_network(value, strict=False))
    return networks


def _header_ip(value: str | None) -> str | None:
    """转发头里的单个 IP；不是合法 IP 一律不认。"""

    candidate = (value or "").strip()
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def _is_trusted(host: str) -> bool:
    networks = _trusted_networks()
    if not networks:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(addr in network for network in networks)


def client_ip(request: Request | WebSocket) -> str:
    """真实来源 IP。只有直连方在 WEBUI_TRUSTED_PROXIES 里才看转发头，防止伪造。

    X-Forwarded-For 从右往左跳过可信代理，第一个不可信的就是客户端。整条链都可信
    （如 Caddy 记下的上一跳是 Cloudflare 节点）时，才读 CF-Connecting-IP / X-Real-IP：
    绕过 Cloudflare 直连源站的请求，上一跳不在可信段里，伪造这两个头没用。
    """

    host = request.client.host if request.client else "unknown"
    if not _is_trusted(host):
        return host
    forwarded = request.headers.get("x-forwarded-for", "")
    for hop in reversed([h.strip() for h in forwarded.split(",") if h.strip()]):
        if not _is_trusted(hop):
            return hop
    for header in ("cf-connecting-ip", "x-real-ip"):
        found = _header_ip(request.headers.get(header))
        if found:
            return found
    return host


def client_key(request: Request) -> str:
    return client_ip(request)


def login_allowed(
    key: str,
    *,
    now: float | None = None,
    max_failures: int = _LOGIN_MAX_FAILURES,
    window: float = _LOGIN_WINDOW_SECONDS,
) -> bool:
    current = time.monotonic() if now is None else now
    failures = _failures[key]
    while failures and current - failures[0] >= window:
        failures.popleft()
    if not failures:
        _failures.pop(key, None)
    return len(failures) < max_failures


def record_login_failure(
    key: str, *, now: float | None = None, window: float = _LOGIN_WINDOW_SECONDS
) -> None:
    current = time.monotonic() if now is None else now
    failures = _failures[key]
    while failures and current - failures[0] >= window:
        failures.popleft()
    failures.append(current)


def login_retry_after(key: str, *, now: float | None = None, window: float) -> int:
    """被限速时还要等几秒（取最早一次失败过期的时间）。"""

    current = time.monotonic() if now is None else now
    failures = _failures.get(key)
    if not failures:
        return 0
    return max(1, int(window - (current - failures[0])) + 1)


def clear_login_failures(key: str) -> None:
    _failures.pop(key, None)


def check_admin_token(key: str, supplied: str, *, now: float | None = None) -> bool:
    current = time.monotonic() if now is None else now
    if not login_allowed(key, now=current):
        return False
    expected = config.webui_admin_token.get_secret_value()
    # 空 Token 永远不算登录成功，即便启用检查被绕过
    valid = bool(expected) and hmac.compare_digest(supplied.encode(), expected.encode())
    if valid:
        clear_login_failures(key)
    else:
        record_login_failure(key, now=current)
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
