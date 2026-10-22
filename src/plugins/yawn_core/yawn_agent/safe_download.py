# ruff: noqa: TRY301, PLR0913, ASYNC240
"""Agent 出站文件/媒体的白名单流式下载。

旧实现用 ``follow_redirects=True``，白名单域名一个 302 就能把下载带到任意
主机。这里手动跟随重定向，每一跳都重新校验 scheme 与域名白名单，并在
读正文前用 Content-Length 预检大小。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

MAX_REDIRECTS = 3


def _check_url(url: str, allowed_hosts: frozenset[str], host_error: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host or host not in allowed_hosts:
        raise PermissionError(host_error)


def _declared_length(response: object) -> int | None:
    headers = getattr(response, "headers", None) or {}
    raw = headers.get("content-length") if hasattr(headers, "get") else None
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


async def stream_download(
    url: str,
    *,
    allowed_hosts: frozenset[str],
    max_bytes: int,
    suffix: str,
    host_error: str = "远程文件域名不在白名单",
    size_error: str = "文件超过大小限制",
    timeout: float = 15.0,
) -> tuple[Path, str | None]:
    """下载到临时文件，返回 ``(路径, content-type)``；失败时不留下临时文件。"""

    current = url
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for _hop in range(MAX_REDIRECTS + 1):
            _check_url(current, allowed_hosts, host_error)
            async with client.stream("GET", current) as response:
                if getattr(response, "is_redirect", False) is True:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("远程地址重定向缺少目标")
                    current = urljoin(current, str(location))
                    continue
                response.raise_for_status()
                declared = _declared_length(response)
                if declared is not None and declared > max_bytes:
                    raise ValueError(size_error)
                content_type = (
                    response.headers.get("content-type", "").split(";", 1)[0].lower()
                    or None
                )
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
                    temporary = Path(handle.name)
                    total = 0
                    try:
                        async for chunk in response.aiter_bytes(64 * 1024):
                            total += len(chunk)
                            if total > max_bytes:
                                raise ValueError(size_error)
                            handle.write(chunk)
                    except BaseException:
                        handle.close()
                        temporary.unlink(missing_ok=True)
                        raise
                return temporary, content_type
    raise ValueError("远程地址重定向次数过多")


__all__ = ["MAX_REDIRECTS", "stream_download"]
