"""WebUI 个人账号的登录设备与登录记录。

和 ``webui_accounts`` 一样不依赖 FastAPI / NoneBot，私聊命令、Agent 工具和 WebUI
路由共用：

- 每个登录会话（签名 Cookie 里的 ``sid``）登记成一台“设备”：设备描述（从 User-Agent
  粗略认出系统和浏览器）、IP 段、登录时间、最近活跃、是否“自动登录”、到期时间。
  只存 ``sid`` 的哈希，拿到这个文件也冒充不了任何会话。
- 单台设备可以单独下线：从名单里删掉并留一个到期即清的墓碑，这个 Cookie 立刻失效。
- 本功能上线前就已登录的会话（名单里没有、也没有墓碑）第一次出现时自动补登记，
  升级不会把所有人踢下线。
- 每个账号保留最近 ``MAX_LOGIN_HISTORY`` 次成功登录；新登录的 IP 段不在以前的登录
  和在线设备里时标记为“新地点”，由调用方决定要不要私聊提醒本人。
- 登录提醒偏好（``new_location`` / ``always`` / ``off``）也存在这里。

数据在 ``data/webui/user_devices.json``（权限 600），不进数据库、不需要迁移。
“最近活跃”最多每 ``TOUCH_INTERVAL_SECONDS`` 写一次盘，避免每个请求都写文件。
"""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import json
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_BEIJING = timezone(timedelta(hours=8))

DEVICES_PATH = Path(
    os.environ.get("YAWNBOT_WEBUI_DEVICES_FILE")
    or Path(__file__).resolve().parents[3] / "data" / "webui" / "user_devices.json"
)

MAX_SESSIONS_PER_USER = 20
MAX_LOGIN_HISTORY = 20
TOUCH_INTERVAL_SECONDS = 300
ALERT_MODES = ("new_location", "always", "off")
DEFAULT_ALERT_MODE = "new_location"
ALERT_MODE_LABELS = {
    "new_location": "仅新地点登录时提醒",
    "always": "每次登录都提醒",
    "off": "不提醒",
}
METHOD_LABELS = {
    "password": "密码登录",
    "setup_code": "设置码设密码后登录",
    "password_change": "修改密码后续登",
    "adopted": "早先的登录",
}

_LOCK = threading.RLock()
_cache: tuple[float, dict[str, Any]] | None = None


# ── 小工具 ───────────────────────────────────────────────────────────────


def _iso(epoch: float | None) -> str | None:
    if not epoch:
        return None
    return (
        datetime.fromtimestamp(float(epoch), _BEIJING)
        .replace(tzinfo=None)
        .isoformat(timespec="seconds")
    )


def session_key(session_id: str) -> str:
    """名单里只存 sid 的哈希。"""

    return hashlib.sha256(
        b"YawnBot WebUI device\0" + str(session_id).encode()
    ).hexdigest()[:32]


def public_id(key: str) -> str:
    """给网页和私聊看的设备编号（哈希前缀，不可反推 sid）。"""

    return key[:10]


# 只把真正的局域网 / 运营商 NAT 段算“内网”（is_private 还会把文档示例段算进去）。
_LAN = tuple(
    ipaddress.ip_network(net)
    for net in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "fc00::/7",
    )
)


def location_key(ip: str | None) -> str:
    """把 IP 归到“地点”：IPv4 取 /24，IPv6 取 /48；内网与本机统一记“内网”。"""

    raw = (ip or "").strip()
    if not raw:
        return "未知"
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return raw[:40]
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if addr.is_loopback or addr.is_link_local or any(addr in n for n in _LAN):
        return "内网"
    if isinstance(addr, ipaddress.IPv4Address):
        a, b, c, _d = str(addr).split(".")
        return f"{a}.{b}.{c}.*"
    network = ipaddress.ip_network(f"{addr}/48", strict=False)
    return f"{network.network_address}/48"


_OS_RULES: tuple[tuple[str, str], ...] = (
    (r"HarmonyOS|OpenHarmony", "鸿蒙"),
    (r"iPhone", "iPhone"),
    (r"iPad", "iPad"),
    (r"Android", "Android"),
    (r"Windows", "Windows"),
    (r"Mac OS X|Macintosh", "macOS"),
    (r"CrOS", "ChromeOS"),
    (r"Linux", "Linux"),
)
_BROWSER_RULES: tuple[tuple[str, str], ...] = (
    (r"MicroMessenger", "微信"),
    (r"\bQQ/\d", "QQ"),
    (r"MQQBrowser|QQBrowser", "QQ 浏览器"),
    (r"UCBrowser|UCWEB", "UC 浏览器"),
    (r"Quark", "夸克"),
    (r"Edg(e|A|iOS)?/", "Edge"),
    (r"OPR/|Opera", "Opera"),
    (r"Firefox/|FxiOS", "Firefox"),
    (r"Chrome/|CriOS", "Chrome"),
    (r"Safari/", "Safari"),
)


def describe_device(user_agent: str | None) -> str:
    """User-Agent → “Windows · Chrome”这样的短描述；认不出就给“未知设备”。"""

    ua = (user_agent or "").strip()
    if not ua:
        return "未知设备"
    os_name = next((name for pat, name in _OS_RULES if re.search(pat, ua)), "")
    browser = next((name for pat, name in _BROWSER_RULES if re.search(pat, ua)), "")
    parts = [part for part in (os_name, browser) if part]
    return " · ".join(parts) if parts else "未知设备"


# ── 存储 ─────────────────────────────────────────────────────────────────


def _empty() -> dict[str, Any]:
    return {"version": 1, "users": {}}


def _read_all() -> dict[str, Any]:
    global _cache  # noqa: PLW0603
    try:
        mtime = DEVICES_PATH.stat().st_mtime
    except OSError:
        _cache = None
        return _empty()
    if _cache is not None and _cache[0] == mtime:
        return _cache[1]
    try:
        data = json.loads(DEVICES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict) or not isinstance(data.get("users"), dict):
        data = _empty()
    _cache = (mtime, data)
    return data


def _write_all(data: dict[str, Any]) -> None:
    global _cache  # noqa: PLW0603
    DEVICES_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".user_devices.", suffix=".tmp", dir=str(DEVICES_PATH.parent)
    )
    try:
        with contextlib.suppress(OSError):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=1, sort_keys=True)
        Path(tmp).replace(DEVICES_PATH)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(tmp).unlink()
        raise
    _cache = None


def _user(data: dict[str, Any], user_id: int, *, create: bool) -> dict[str, Any] | None:
    key = str(int(user_id))
    raw = data["users"].get(key)
    if not isinstance(raw, dict):
        if not create:
            return None
        raw = {}
        data["users"][key] = raw
    for name, default in (("sessions", {}), ("revoked", {}), ("logins", [])):
        if not isinstance(raw.get(name), type(default)):
            raw[name] = default
    return raw


def _prune(raw: dict[str, Any], now: float) -> bool:
    """去掉过期的会话与墓碑，返回有没有改动。"""

    changed = False
    sessions: dict[str, Any] = raw["sessions"]
    for key in [k for k, v in sessions.items() if float(v.get("exp") or 0) <= now]:
        sessions.pop(key, None)
        changed = True
    revoked: dict[str, Any] = raw["revoked"]
    for key in [k for k, exp in revoked.items() if float(exp or 0) <= now]:
        revoked.pop(key, None)
        changed = True
    return changed


def _known_locations(raw: dict[str, Any]) -> set[str]:
    known = {str(item.get("loc")) for item in raw["logins"] if isinstance(item, dict)}
    known |= {str(item.get("loc")) for item in raw["sessions"].values()}
    return known


def _evict_overflow(raw: dict[str, Any]) -> None:
    sessions: dict[str, Any] = raw["sessions"]
    while len(sessions) > MAX_SESSIONS_PER_USER:
        oldest = min(sessions, key=lambda k: float(sessions[k].get("seen") or 0))
        exp = float(sessions[oldest].get("exp") or 0)
        sessions.pop(oldest, None)
        raw["revoked"][oldest] = exp


# ── 写入 ─────────────────────────────────────────────────────────────────


def register_session(  # noqa: PLR0913
    user_id: int,
    session_id: str,
    *,
    ip: str | None,
    user_agent: str | None,
    remember: bool,
    expires_at: float,
    method: str = "password",
    record_login: bool = True,
    now: float | None = None,
) -> dict[str, Any]:
    """登记一次新登录，返回这次登录的概况（含是否新地点、上一次登录）。"""

    current = time.time() if now is None else now
    loc = location_key(ip)
    device = describe_device(user_agent)
    key = session_key(session_id)
    with _LOCK:
        data = _read_all()
        raw = _user(data, user_id, create=True)
        assert raw is not None
        _prune(raw, current)
        logins: list[dict[str, Any]] = raw["logins"]
        previous = logins[0] if logins else None
        had_history = bool(logins) or bool(raw["sessions"])
        new_location = had_history and loc not in _known_locations(raw)
        raw["sessions"][key] = {
            "created": current,
            "seen": current,
            "exp": float(expires_at),
            "loc": loc,
            "device": device,
            "remember": bool(remember),
            "method": method,
        }
        raw["revoked"].pop(key, None)
        _evict_overflow(raw)
        event = {
            "at": current,
            "loc": loc,
            "device": device,
            "method": method,
            "new_location": bool(new_location),
            "remember": bool(remember),
        }
        if record_login:
            logins.insert(0, event)
            del logins[MAX_LOGIN_HISTORY:]
        _write_all(data)
        active = len(raw["sessions"])
        mode = alert_mode_of(raw)
    return {
        "device": device,
        "location": loc,
        "newLocation": bool(new_location),
        "firstLogin": not had_history,
        "activeCount": active,
        "alertMode": mode,
        "at": _iso(current),
        "previous": _login_view(previous) if previous else None,
    }


def check_session(  # noqa: PLR0913
    user_id: int,
    session_id: str,
    *,
    expires_at: float,
    remember: bool = False,
    ip: str | None = None,
    user_agent: str | None = None,
    now: float | None = None,
) -> bool:
    """这个会话还在名单上吗（顺手更新最近活跃）。被单独下线的返回 False。"""

    current = time.time() if now is None else now
    key = session_key(session_id)
    with _LOCK:
        data = _read_all()
        raw = _user(data, user_id, create=False)
        if (
            raw is not None
            and key in raw["revoked"]
            and float(raw["revoked"][key] or 0) > current
        ):
            return False
        entry = raw["sessions"].get(key) if raw is not None else None
        if entry is None:
            # 本功能上线前就登录着的会话：补登记，不踢人。
            raw = _user(data, user_id, create=True)
            assert raw is not None
            _prune(raw, current)
            raw["sessions"][key] = {
                "created": current,
                "seen": current,
                "exp": float(expires_at),
                "loc": location_key(ip),
                "device": describe_device(user_agent),
                "remember": bool(remember),
                "method": "adopted",
            }
            _evict_overflow(raw)
            _write_all(data)
            return True
        dirty = False
        if float(expires_at) > float(entry.get("exp") or 0):
            entry["exp"] = float(expires_at)
            dirty = True
        if current - float(entry.get("seen") or 0) >= TOUCH_INTERVAL_SECONDS:
            entry["seen"] = current
            if ip:
                entry["last_loc"] = location_key(ip)
            dirty = True
        if dirty:
            _write_all(data)
        return True


def revoke_device(user_id: int, device_id: str, *, now: float | None = None) -> bool:
    """按设备编号下线一台。"""

    current = time.time() if now is None else now
    with _LOCK:
        data = _read_all()
        raw = _user(data, user_id, create=False)
        if raw is None:
            return False
        _prune(raw, current)
        match = next(
            (k for k in raw["sessions"] if public_id(k) == str(device_id)), None
        )
        if match is None:
            return False
        entry = raw["sessions"].pop(match)
        raw["revoked"][match] = float(entry.get("exp") or current + 86400)
        _write_all(data)
        return True


def forget_session(user_id: int, session_id: str) -> None:
    """正常退出登录：这台设备从名单里去掉（留墓碑，复制走的 Cookie 也用不了）。"""

    key = session_key(session_id)
    with _LOCK:
        data = _read_all()
        raw = _user(data, user_id, create=False)
        if raw is None or key not in raw["sessions"]:
            return
        entry = raw["sessions"].pop(key)
        raw["revoked"][key] = float(entry.get("exp") or time.time() + 86400)
        _write_all(data)


def revoke_others(
    user_id: int, keep_session_id: str, *, now: float | None = None
) -> int:
    """除了当前这台，其余设备全部下线；返回下线了几台。"""

    current = time.time() if now is None else now
    keep = session_key(keep_session_id)
    with _LOCK:
        data = _read_all()
        raw = _user(data, user_id, create=False)
        if raw is None:
            return 0
        _prune(raw, current)
        others = [k for k in raw["sessions"] if k != keep]
        for key in others:
            entry = raw["sessions"].pop(key)
            raw["revoked"][key] = float(entry.get("exp") or current + 86400)
        if others:
            _write_all(data)
        return len(others)


def drop_sessions(user_id: int) -> None:
    """凭据版本加一（改密码、退出所有设备、停用）后清空在线名单；登录记录保留。

    旧 Cookie 已经因为版本号对不上而失效，这里只是让设备列表跟着清空。
    """

    with _LOCK:
        data = _read_all()
        raw = _user(data, user_id, create=False)
        if raw is None or not (raw["sessions"] or raw["revoked"]):
            return
        raw["sessions"] = {}
        raw["revoked"] = {}
        _write_all(data)


def delete_user(user_id: int) -> None:
    """注销账号：设备、登录记录、偏好全删。"""

    with _LOCK:
        data = _read_all()
        if data["users"].pop(str(int(user_id)), None) is not None:
            _write_all(data)


def alert_mode_of(raw: dict[str, Any] | None) -> str:
    mode = (raw or {}).get("alert")
    return mode if mode in ALERT_MODES else DEFAULT_ALERT_MODE


def alert_mode(user_id: int) -> str:
    with _LOCK:
        return alert_mode_of(_user(_read_all(), user_id, create=False))


def set_alert_mode(user_id: int, mode: str) -> str:
    if mode not in ALERT_MODES:
        msg = f"登录提醒只能是 {'/'.join(ALERT_MODES)}"
        raise ValueError(msg)
    with _LOCK:
        data = _read_all()
        raw = _user(data, user_id, create=True)
        assert raw is not None
        raw["alert"] = mode
        _write_all(data)
    return mode


def should_alert(event: dict[str, Any]) -> bool:
    """按本人偏好决定这次登录要不要私聊提醒。第一次登录不提醒（本人刚在私聊里开通）。"""

    mode = event.get("alertMode", DEFAULT_ALERT_MODE)
    if mode == "off" or event.get("firstLogin"):
        return False
    if mode == "always":
        return True
    return bool(event.get("newLocation"))


# ── 读取 ─────────────────────────────────────────────────────────────────


def _login_view(item: dict[str, Any]) -> dict[str, Any]:
    method = str(item.get("method") or "password")
    return {
        "at": _iso(item.get("at")),
        "device": item.get("device") or "未知设备",
        "location": item.get("loc") or "未知",
        "method": method,
        "methodLabel": METHOD_LABELS.get(method, method),
        "newLocation": bool(item.get("new_location")),
        "remember": bool(item.get("remember")),
    }


def list_devices(
    user_id: int, *, current_session_id: str | None = None, now: float | None = None
) -> list[dict[str, Any]]:
    """在线设备，当前这台排最前，其余按最近活跃倒序。"""

    current = time.time() if now is None else now
    current_key = session_key(current_session_id) if current_session_id else None
    with _LOCK:
        raw = _user(_read_all(), user_id, create=False)
        sessions = dict(raw["sessions"]) if raw is not None else {}
    rows = []
    for key, entry in sessions.items():
        if float(entry.get("exp") or 0) <= current:
            continue
        rows.append(
            {
                "id": public_id(key),
                "device": entry.get("device") or "未知设备",
                "location": entry.get("loc") or "未知",
                "lastLocation": entry.get("last_loc") or entry.get("loc") or "未知",
                "createdAt": _iso(entry.get("created")),
                "lastSeenAt": _iso(entry.get("seen")),
                "expiresAt": _iso(entry.get("exp")),
                "remember": bool(entry.get("remember")),
                "method": entry.get("method") or "password",
                "current": key == current_key,
                "_seen": float(entry.get("seen") or 0),
            }
        )
    rows.sort(key=lambda row: (not row["current"], -row["_seen"]))
    for row in rows:
        row.pop("_seen", None)
    return rows


def login_history(
    user_id: int, *, limit: int = MAX_LOGIN_HISTORY
) -> list[dict[str, Any]]:
    with _LOCK:
        raw = _user(_read_all(), user_id, create=False)
        logins = list(raw["logins"]) if raw is not None else []
    return [
        _login_view(item)
        for item in logins[: max(0, int(limit))]
        if isinstance(item, dict)
    ]


def status_summary(
    user_id: int, *, now: float | None = None, recent: int = 5
) -> dict[str, Any]:
    """私聊命令和 Agent 用的登录状态概况（不含 IP 全文、不含 sid）。"""

    devices = list_devices(user_id, now=now)
    logins = login_history(user_id, limit=MAX_LOGIN_HISTORY)
    return {
        "activeCount": len(devices),
        "devices": devices,
        "recentLogins": logins[:recent],
        "newLocationLogins": sum(1 for item in logins if item["newLocation"]),
        "alertMode": alert_mode(user_id),
        "alertModeLabel": ALERT_MODE_LABELS[alert_mode(user_id)],
    }


def reset_for_tests() -> None:
    global _cache  # noqa: PLW0603
    with _LOCK:
        _cache = None


__all__ = [
    "ALERT_MODES",
    "ALERT_MODE_LABELS",
    "DEVICES_PATH",
    "alert_mode",
    "check_session",
    "delete_user",
    "describe_device",
    "drop_sessions",
    "forget_session",
    "list_devices",
    "location_key",
    "login_history",
    "public_id",
    "register_session",
    "revoke_device",
    "revoke_others",
    "session_key",
    "set_alert_mode",
    "should_alert",
    "status_summary",
]
