# ruff: noqa: TRY003, PLR0911
"""WebUI 个人账号：QQ 用户用私聊设的密码登录 WebUI 的自助页面。

和运维 Token 登录、访客访问码完全分开：

- 账号只存在 ``data/webui/user_accounts.json``（权限 600），不进数据库，不需要迁移。
- 密码只存 scrypt 哈希（每个账号独立盐），明文不落盘、不进日志、不回显。
- ``credential_version`` 在改密码、退出所有设备、停用账号时加一；会话 Cookie 里带着
  这个版本号，对不上就立刻失效。
- 一次性设置码：私聊里发 ``/网页密码``（不带参数）或让 Agent 帮忙时发给本人，
  10 分钟内有效、只能用一次、最多试 5 次；只存哈希。用设置码可以在登录页设密码，
  这样密码根本不用经过聊天记录。

这个模块不依赖 FastAPI / NoneBot，私聊命令、Agent 工具和 WebUI 路由都用它。
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_BEIJING = timezone(timedelta(hours=8))

ACCOUNTS_PATH = Path(
    os.environ.get("YAWNBOT_WEBUI_ACCOUNTS_FILE")
    or Path(__file__).resolve().parents[3] / "data" / "webui" / "user_accounts.json"
)

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 64
SETUP_CODE_TTL_SECONDS = 600
SETUP_CODE_MAX_ATTEMPTS = 5
SETUP_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
SETUP_CODE_LENGTH = 8

# scrypt：N=2^15、r=8、p=1，约 32MB 内存、几十毫秒；参数写进哈希串，以后可平滑升级。
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024
_HASH_PREFIX = "scrypt"
_SETUP_DOMAIN = b"YawnBot WebUI setup code\0"

# 太常见的密码直接拒绝（只收小写比较）。
_COMMON_PASSWORDS = frozenset(
    {
        "12345678",
        "123456789",
        "1234567890",
        "87654321",
        "11111111",
        "00000000",
        "88888888",
        "66666666",
        "password",
        "password1",
        "passw0rd",
        "qwertyui",
        "qwerty123",
        "qwe123456",
        "abc12345",
        "abcd1234",
        "a1234567",
        "a12345678",
        "aa123456",
        "1qaz2wsx",
        "iloveyou",
        "admin123",
        "yawnbot1",
        "yawnbot123",
        "woaini520",
        "woaini1314",
    }
)

_LOCK = threading.RLock()
_cache: tuple[float, dict[str, Any]] | None = None


class AccountError(ValueError):
    """面向用户的账号错误；``code`` 给接口和测试区分原因。"""

    def __init__(self, message: str, *, code: str = "invalid") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class AccountView:
    user_id: int
    has_password: bool
    disabled: bool
    credential_version: int
    created_at: str | None
    password_changed_at: str | None
    last_login_at: str | None
    setup_pending: bool
    password_source: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "userId": str(self.user_id),
            "hasPassword": self.has_password,
            "disabled": self.disabled,
            "createdAt": self.created_at,
            "passwordChangedAt": self.password_changed_at,
            "lastLoginAt": self.last_login_at,
            "setupPending": self.setup_pending,
            "passwordSource": self.password_source,
        }


def _now_iso() -> str:
    return datetime.now(_BEIJING).replace(tzinfo=None).isoformat(timespec="seconds")


# ── 密码哈希 ─────────────────────────────────────────────────────────────


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )
    params = f"{_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}"
    return f"{_HASH_PREFIX}${params}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str | None) -> bool:
    """常数时间比较；格式坏了一律视为不匹配。"""

    if not encoded:
        return False
    try:
        prefix, n, r, p, salt, digest = encoded.split("$")
        if prefix != _HASH_PREFIX:
            return False
        expected = _unb64(digest)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_unb64(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
            maxmem=_SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


_DUMMY_HASH: str | None = None


def burn_password_check(password: str) -> None:
    """账号不存在时也算一次哈希，让响应时间不暴露“这个 QQ 有没有开通”。"""

    global _DUMMY_HASH  # noqa: PLW0603
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(12))
    verify_password(password, _DUMMY_HASH)


def password_problem(password: str, user_id: int | None = None) -> str | None:
    """不合格时返回原因（不含密码本身），合格返回 None。"""

    if not isinstance(password, str):
        return "密码格式不对"
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"密码至少 {PASSWORD_MIN_LENGTH} 位"
    if len(password) > PASSWORD_MAX_LENGTH:
        return f"密码最多 {PASSWORD_MAX_LENGTH} 位"
    if any(ch.isspace() for ch in password):
        return "密码里不能有空格或换行"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in password):  # noqa: PLR2004
        return "密码里有不可见字符"
    if len(set(password)) < 4:  # noqa: PLR2004
        return "密码太简单了，别全用同几个字符"
    classes = sum(
        (
            any(ch.isascii() and ch.isalpha() for ch in password),
            any(ch.isdigit() for ch in password),
            any(not ch.isascii() or not ch.isalnum() for ch in password),
        )
    )
    if classes < 2:  # noqa: PLR2004
        return "密码至少要混用字母、数字、符号里的两种"
    lowered = password.lower()
    if lowered in _COMMON_PASSWORDS:
        return "这个密码太常见了，换一个"
    if user_id is not None and str(user_id) in password:
        return "密码里别带自己的 QQ 号"
    return None


# ── 存储 ─────────────────────────────────────────────────────────────────


def _read_all() -> dict[str, Any]:
    global _cache  # noqa: PLW0603
    try:
        mtime = ACCOUNTS_PATH.stat().st_mtime
    except OSError:
        _cache = None
        return {"version": 1, "accounts": {}}
    if _cache is not None and _cache[0] == mtime:
        return _cache[1]
    try:
        data = json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), dict):
        data = {"version": 1, "accounts": {}}
    _cache = (mtime, data)
    return data


def _write_all(data: dict[str, Any]) -> None:
    global _cache  # noqa: PLW0603
    ACCOUNTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".user_accounts.", suffix=".tmp", dir=str(ACCOUNTS_PATH.parent)
    )
    try:
        with contextlib.suppress(OSError):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=1, sort_keys=True)
        Path(tmp).replace(ACCOUNTS_PATH)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(tmp).unlink()
        raise
    _cache = None


def _record(data: dict[str, Any], user_id: int) -> dict[str, Any] | None:
    raw = data["accounts"].get(str(int(user_id)))
    return raw if isinstance(raw, dict) else None


def _setup_active(raw: dict[str, Any], now: float) -> bool:
    setup = raw.get("setup")
    return (
        isinstance(setup, dict)
        and float(setup.get("expires_at") or 0) > now
        and int(setup.get("attempts") or 0) < SETUP_CODE_MAX_ATTEMPTS
    )


def _view(user_id: int, raw: dict[str, Any], now: float | None = None) -> AccountView:
    current = time.time() if now is None else now
    return AccountView(
        user_id=int(user_id),
        has_password=bool(raw.get("password_hash")),
        disabled=bool(raw.get("disabled")),
        credential_version=int(raw.get("credential_version") or 0),
        created_at=raw.get("created_at"),
        password_changed_at=raw.get("password_changed_at"),
        last_login_at=raw.get("last_login_at"),
        setup_pending=_setup_active(raw, current),
        password_source=raw.get("password_source"),
    )


def get_account(user_id: int) -> AccountView | None:
    with _LOCK:
        raw = _record(_read_all(), user_id)
        return _view(user_id, raw) if raw is not None else None


def list_accounts() -> list[AccountView]:
    with _LOCK:
        data = _read_all()
        views = [
            _view(int(key), raw)
            for key, raw in data["accounts"].items()
            if isinstance(raw, dict) and str(key).isdigit()
        ]
    views.sort(
        key=lambda item: item.last_login_at or item.created_at or "", reverse=True
    )
    return views


def session_version(user_id: int) -> int | None:
    """可以登录的账号返回当前凭据版本；没开通、没密码或被停用返回 None。"""

    with _LOCK:
        raw = _record(_read_all(), user_id)
    if raw is None or raw.get("disabled") or not raw.get("password_hash"):
        return None
    return int(raw.get("credential_version") or 0)


def _ensure(data: dict[str, Any], user_id: int) -> dict[str, Any]:
    key = str(int(user_id))
    raw = data["accounts"].get(key)
    if not isinstance(raw, dict):
        raw = {"created_at": _now_iso(), "credential_version": 0}
        data["accounts"][key] = raw
    return raw


def set_password(user_id: int, password: str, *, source: str) -> AccountView:
    """设置或修改密码；会让这个账号已登录的所有会话失效。"""

    problem = password_problem(password, user_id)
    if problem:
        raise AccountError(problem, code="weak_password")
    encoded = hash_password(password)
    with _LOCK:
        data = _read_all()
        raw = _ensure(data, user_id)
        if raw.get("disabled"):
            raise AccountError("你的网页账号已被管理员停用", code="disabled")
        raw["password_hash"] = encoded
        raw["password_changed_at"] = _now_iso()
        raw["password_source"] = str(source)[:16]
        raw["credential_version"] = int(raw.get("credential_version") or 0) + 1
        raw.pop("setup", None)
        _write_all(data)
        return _view(user_id, raw)


def issue_setup_code(user_id: int, *, now: float | None = None) -> str:
    """生成一次性设置码，返回明文（只给本人看一次）；旧码立即作废。"""

    current = time.time() if now is None else now
    code = "".join(
        secrets.choice(SETUP_CODE_ALPHABET) for _ in range(SETUP_CODE_LENGTH)
    )
    with _LOCK:
        data = _read_all()
        raw = _ensure(data, user_id)
        if raw.get("disabled"):
            raise AccountError("你的网页账号已被管理员停用", code="disabled")
        raw["setup"] = {
            "hash": _setup_hash(user_id, code),
            "expires_at": current + SETUP_CODE_TTL_SECONDS,
            "attempts": 0,
        }
        _write_all(data)
    return code


def _setup_hash(user_id: int, code: str) -> str:
    normalized = code.strip().upper().replace("-", "").replace(" ", "")
    return hashlib.sha256(
        _SETUP_DOMAIN + str(int(user_id)).encode() + b"\0" + normalized.encode()
    ).hexdigest()


def redeem_setup_code(
    user_id: int, code: str, password: str, *, now: float | None = None
) -> AccountView:
    """用设置码设密码。码错会累计次数，用满或过期即作废。"""

    current = time.time() if now is None else now
    problem = password_problem(password, user_id)
    with _LOCK:
        data = _read_all()
        raw = _record(data, user_id)
        if raw is None or not _setup_active(raw, current):
            raise AccountError(
                "设置码无效或已过期，请在私聊里重新发 /网页密码", code="bad_code"
            )
        setup = raw["setup"]
        if not hmac.compare_digest(
            str(setup.get("hash") or ""), _setup_hash(user_id, code)
        ):
            setup["attempts"] = int(setup.get("attempts") or 0) + 1
            _write_all(data)
            raise AccountError(
                "设置码无效或已过期，请在私聊里重新发 /网页密码", code="bad_code"
            )
        if problem:
            # 码是对的但密码不合格：不消耗设置码，方便直接改了再提交。
            raise AccountError(problem, code="weak_password")
        # 仍在锁内：同一个设置码不可能被并发用两次。
        return set_password(user_id, password, source="setup_code")


def record_login(user_id: int) -> None:
    with _LOCK:
        data = _read_all()
        raw = _record(data, user_id)
        if raw is None:
            return
        raw["last_login_at"] = _now_iso()
        _write_all(data)


def password_hash_for(user_id: int) -> str | None:
    with _LOCK:
        raw = _record(_read_all(), user_id)
    if raw is None or raw.get("disabled"):
        return None
    value = raw.get("password_hash")
    return str(value) if value else None


def revoke_sessions(user_id: int) -> bool:
    """退出所有设备：凭据版本加一。"""

    with _LOCK:
        data = _read_all()
        raw = _record(data, user_id)
        if raw is None:
            return False
        raw["credential_version"] = int(raw.get("credential_version") or 0) + 1
        _write_all(data)
        return True


def set_disabled(user_id: int, disabled: bool) -> AccountView | None:  # noqa: FBT001
    with _LOCK:
        data = _read_all()
        raw = _record(data, user_id)
        if raw is None:
            return None
        if bool(raw.get("disabled")) != bool(disabled):
            raw["disabled"] = bool(disabled)
            raw["credential_version"] = int(raw.get("credential_version") or 0) + 1
            raw.pop("setup", None)
            _write_all(data)
        return _view(user_id, raw)


def delete_account(user_id: int) -> bool:
    """删掉账号（密码、设置码一起没了）；本人之后可以重新设置。"""

    with _LOCK:
        data = _read_all()
        if str(int(user_id)) not in data["accounts"]:
            return False
        del data["accounts"][str(int(user_id))]
        _write_all(data)
        return True


def reset_for_tests() -> None:
    global _cache, _DUMMY_HASH  # noqa: PLW0603
    with _LOCK:
        _cache = None
        _DUMMY_HASH = None


__all__ = [
    "ACCOUNTS_PATH",
    "PASSWORD_MAX_LENGTH",
    "PASSWORD_MIN_LENGTH",
    "SETUP_CODE_TTL_SECONDS",
    "AccountError",
    "AccountView",
    "burn_password_check",
    "delete_account",
    "get_account",
    "hash_password",
    "issue_setup_code",
    "list_accounts",
    "password_hash_for",
    "password_problem",
    "record_login",
    "redeem_setup_code",
    "revoke_sessions",
    "session_version",
    "set_disabled",
    "set_password",
    "verify_password",
]
