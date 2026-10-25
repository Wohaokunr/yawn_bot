# ruff: noqa: TID252, PLR0911, BLE001, E501
"""识图取字节：把群里的图片可靠地变成字节交给模型。

旧实现只认消息段里的 QQ 图片链接：链接一过期、TLS 握手失败或者重定向到别的
QQ 域名，图片就只剩一个 ``[图片]`` 占位符。这里把取法补齐，并给每一种失败
记一个原因码，方便模型如实说明、WebUI 告诉管理员该改哪里：

1. 消息段 ``file`` 自带的 ``base64://``；
2. 消息段链接（``AGENT_MEDIA_ALLOWED_HOSTS``，支持 ``*.qpic.cn`` 这类后缀
   规则），手动跟随重定向并逐跳校验（``safe_download``）；QQ 图床偶发
   HTTPS 握手失败时按 ``AGENT_MEDIA_HTTP_FALLBACK`` 改用 http 再试一次；
3. OneBot ``get_image`` / ``get_file``：返回里的 ``base64``（NapCat 需开
   ``enableLocalFile2Url``）、新链接或共享目录里的路径；
4. 历史/引用里的图片链接过期时，用 ``get_msg`` 重新拉一次消息拿新链接。

URL、OneBot ``file`` 标识和本机路径只在本模块内部使用，不进 Prompt、Trace、
WebUI 统计或长期存储；统计只记原因码和取法。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import os
import re
import time
from collections import Counter, deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from ..llm import ai_config
from .log import dbg

API_TIMEOUT_SECONDS = 10.0
DOWNLOAD_TIMEOUT_SECONDS = 12.0
STATS_LIMIT = 200

# 原因码 → (给模型/管理员看的说明, 建议)。新增原因码时同步 WebUI 的识图诊断卡片。
REASON_TEXT: dict[str, tuple[str, str]] = {
    "host_blocked": (
        "图片链接的域名不在识图白名单里",
        "把该 QQ 图床域名加进 AGENT_MEDIA_ALLOWED_HOSTS（支持 *.qpic.cn 写法）",
    ),
    "expired": ("图片链接已经过期", "请对方重新发一次图片"),
    "tls_failed": (
        "下载图片时 HTTPS 握手失败",
        "保持 AGENT_MEDIA_HTTP_FALLBACK=true，或检查服务器到 QQ 图床的网络",
    ),
    "network": ("下载图片时网络出错", "稍后再试；一直失败请检查服务器出网"),
    "too_large": ("图片超过大小上限", "调大 AGENT_REMOTE_MEDIA_MAX_BYTES 或发小一点的图"),
    "not_image": ("取到的内容不是模型能读的图片格式", "支持 PNG/JPEG/GIF/WebP"),
    "protocol_local_only": (
        "协议端只返回了它自己机器上的文件路径，Bot 读不到",
        "在 NapCat 的 OneBot 配置里打开 enableLocalFile2Url 并重启 NapCat",
    ),
    "api_failed": ("协议端取图接口调用失败", "检查 NapCat 是否在线"),
    "no_source": ("消息里没有可用的图片来源", "请对方重新发一次图片"),
}

_ATTEMPTS: ContextVar[FetchAttempts | None] = ContextVar(
    "agent_media_fetch_attempts", default=None
)
_STATS: deque[dict[str, Any]] = deque(maxlen=STATS_LIMIT)
_TLS_MARKERS = ("ssl", "tls", "handshake", "certificate")
_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")


@dataclass(slots=True)
class FetchAttempts:
    """一次取图过程中遇到的失败；最终原因按严重程度挑一个。"""

    reasons: list[str] = field(default_factory=list)

    def add(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)

    def primary(self) -> str:
        for reason in (
            "too_large",
            "expired",
            "protocol_local_only",
            "tls_failed",
            "host_blocked",
            "not_image",
            "network",
            "api_failed",
        ):
            if reason in self.reasons:
                return reason
        return "no_source"


@contextlib.contextmanager
def tracking(attempts: FetchAttempts):  # noqa: ANN201
    """让 ``download`` 等底层函数把失败原因记到 ``attempts``。"""

    token = _ATTEMPTS.set(attempts)
    try:
        yield attempts
    finally:
        _ATTEMPTS.reset(token)


def note(reason: str) -> None:
    attempts = _ATTEMPTS.get()
    if attempts is not None:
        attempts.add(reason)


# ── 域名白名单 ────────────────────────────────────────────────────────────


def host_patterns() -> tuple[str, ...]:
    raw = str(getattr(ai_config, "agent_media_allowed_hosts", "") or "")
    return tuple(item.strip().lower() for item in raw.split(",") if item.strip())


def host_allowed(host: str, patterns: tuple[str, ...] | None = None) -> bool:
    """``*.qpic.cn`` 匹配子域名和 qpic.cn 本身；其余精确匹配，空白名单全部拒绝。"""

    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    for pattern in patterns if patterns is not None else host_patterns():
        if pattern.startswith("*."):
            base = pattern[2:]
            if base and (host == base or host.endswith(f".{base}")):
                return True
        elif host == pattern:
            return True
    return False


def url_allowed(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and host_allowed(parsed.hostname or "")


def _http_fallback_enabled() -> bool:
    return bool(getattr(ai_config, "agent_media_http_fallback", True))


# ── 下载与解码 ────────────────────────────────────────────────────────────


def _looks_like_tls_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in _TLS_MARKERS)


async def _download_once(url: str, max_bytes: int) -> bytes | None:
    import httpx

    from .safe_download import stream_download

    temporary: Path | None = None
    try:
        temporary, _content_type = await stream_download(
            url,
            allowed_hosts=frozenset(),
            host_check=host_allowed,
            max_bytes=max_bytes,
            suffix=".media",
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        )
        return temporary.read_bytes()
    except PermissionError:
        note("host_blocked")
    except ValueError as exc:
        if "大小" in str(exc) or "限制" in str(exc):
            note("too_large")
        else:
            note("network")
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        note("expired" if status in {400, 403, 404, 410} else "network")
        dbg(f"识图下载失败 HTTP {status}")
    except (httpx.HTTPError, OSError) as exc:
        if _looks_like_tls_error(exc):
            note("tls_failed")
            raise _TLSFailedError from exc
        note("network")
        dbg(f"识图下载失败 {type(exc).__name__}")
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink()
    return None


class _TLSFailedError(Exception):
    pass


async def download(url: str, max_bytes: int) -> bytes | None:
    """白名单内下载；QQ 图床 HTTPS 握手失败时可改用 http 重试一次。"""

    if not url_allowed(url):
        note("host_blocked")
        return None
    try:
        return await _download_once(url, max_bytes)
    except _TLSFailedError:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not _http_fallback_enabled():
            return None
        dbg(f"识图下载 HTTPS 握手失败,改用 http 重试 host={parsed.hostname!r}")
        try:
            return await _download_once(urlunparse(parsed._replace(scheme="http")), max_bytes)
        except _TLSFailedError:
            return None


def decode_base64(text: str, max_bytes: int) -> bytes | None:
    payload = text.split("base64://", 1)[-1].strip()
    if payload.startswith("data:") and "," in payload:
        payload = payload.split(",", 1)[1]
    if len(payload) * 3 // 4 > max_bytes + 4:
        note("too_large")
        return None
    try:
        data = base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError):
        return None
    if len(data) > max_bytes:
        note("too_large")
        return None
    return data or None


def _looks_like_path(text: str) -> bool:
    return text.startswith(("/", "\\", "file://")) or bool(_WINDOWS_PATH.match(text))


def extra_local_roots() -> tuple[Path, ...]:
    """和协议端共享的目录（与群附件入箱同一个配置）。"""

    roots: list[Path] = []
    for item in os.environ.get("AGENT_SANDBOX_INBOX_LOCAL_ROOTS", "").split(","):
        text = item.strip()
        if text:
            roots.append(Path(os.path.realpath(text)))
    return tuple(roots)


def read_local(candidate: str, roots: tuple[Path, ...], max_bytes: int) -> bytes | None:
    text = candidate.removeprefix("file://")
    if not text or not roots:
        return None
    path = Path(os.path.realpath(text))
    if not any(path == root or root in path.parents for root in roots):
        if _looks_like_path(candidate):
            note("protocol_local_only")
        return None
    try:
        if not path.is_file():
            if _looks_like_path(candidate):
                note("protocol_local_only")
            return None
        if path.stat().st_size > max_bytes:
            note("too_large")
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > max_bytes:
        note("too_large")
        return None
    return data


async def call_api(bot: Any, action: str, **params: Any) -> Any:
    """带超时的 OneBot 调用；失败返回 None，链接过期类报错记成 expired。"""

    try:
        return await asyncio.wait_for(bot.call_api(action, **params), timeout=API_TIMEOUT_SECONDS)
    except Exception as exc:
        text = str(exc)
        if any(word in text for word in ("过期", "expired", "不存在", "not found", "已失效")):
            note("expired")
        else:
            note("api_failed")
        dbg(f"识图取图: {action} 失败 ({type(exc).__name__})")
        return None


async def refresh_ref(bot: Any, ref: dict[str, Any]) -> dict[str, Any] | None:
    """历史/引用图片：``get_msg`` 重新拉一次原消息，按序号拿新鲜的图片段。"""

    raw_id = ref.get("source_message_id") or ref.get("message_id")
    try:
        message_id = int(raw_id) if raw_id is not None else 0
    except (TypeError, ValueError):
        message_id = 0
    if bot is None or message_id <= 0:
        return None
    from .message_parser import _message_from_api, normalize_message

    payload = await call_api(bot, "get_msg", message_id=message_id)
    if not isinstance(payload, dict):
        return None
    try:
        fresh_refs = normalize_message(_message_from_api(payload)).media_refs
    except Exception:
        return None
    images = [item for item in fresh_refs if str(item.get("type") or "") == "image"]
    if not images:
        return None
    try:
        index = int(ref.get("image_index") or 0)
    except (TypeError, ValueError):
        index = 0
    if 0 <= index < len(images):
        return dict(images[index])
    old_file = str(ref.get("file") or "")
    for item in images:
        if old_file and str(item.get("file") or "") == old_file:
            return dict(item)
    return dict(images[0]) if len(images) == 1 else None


# ── 统计（只记原因码，不记任何标识） ────────────────────────────────────────


def record(outcome: str, *, reason: str | None = None, via: str | None = None, source: str = "current") -> None:
    _STATS.append(
        {
            "at": time.time(),
            "outcome": outcome,
            "reason": reason,
            "via": via,
            "source": source,
        }
    )


def stats(window_seconds: float = 86400.0) -> dict[str, Any]:
    cutoff = time.time() - window_seconds
    rows = [row for row in _STATS if float(row.get("at") or 0) >= cutoff]
    reasons = Counter(str(row["reason"]) for row in rows if row.get("outcome") == "failed" and row.get("reason"))
    vias = Counter(str(row["via"]) for row in rows if row.get("outcome") == "loaded" and row.get("via"))
    return {
        "total": len(rows),
        "loaded": sum(1 for row in rows if row.get("outcome") == "loaded"),
        "failed": sum(1 for row in rows if row.get("outcome") == "failed"),
        "reasons": dict(reasons),
        "via": dict(vias),
    }


def reason_text(reason: str | None) -> tuple[str, str]:
    return REASON_TEXT.get(str(reason or ""), REASON_TEXT["no_source"])


def reset_for_tests() -> None:
    _STATS.clear()


__all__ = [
    "REASON_TEXT",
    "FetchAttempts",
    "call_api",
    "decode_base64",
    "download",
    "extra_local_roots",
    "host_allowed",
    "host_patterns",
    "note",
    "read_local",
    "reason_text",
    "record",
    "refresh_ref",
    "reset_for_tests",
    "stats",
    "tracking",
    "url_allowed",
]
