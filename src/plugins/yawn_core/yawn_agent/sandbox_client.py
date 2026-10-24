# ruff: noqa: E501,PLR0913
"""代码沙箱 runner 的异步客户端（Bot 与 WebUI 共用）。

runner 跑在独立的无网络容器里，默认经 Unix socket 通信
（``AGENT_SANDBOX_URL=unix:///run/yawnbot-sandbox/runner.sock``）；开发时也可以
指向带 token 的 ``http://127.0.0.1:8790``。地址为空表示不启用沙箱。

这里只做传输与错误归一：沙箱离线、超时、返回错误都变成 ``SandboxError``，
调用方据 ``code`` 决定怎么告诉模型或管理员，绝不让异常冒到监听器里。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

import httpx

DEFAULT_SANDBOX_URL = "unix:///run/yawnbot-sandbox/runner.sock"
_HEALTH_TTL_SECONDS = 15.0
# 执行请求的 HTTP 超时 = 代码时限 + 排队等待 + 余量。
_RUN_TIMEOUT_GRACE = 45.0
_DEFAULT_TIMEOUT = 10.0


class SandboxError(Exception):
    """沙箱不可用或拒绝请求。``code`` 是 runner 的错误码或本地分类。"""

    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True, slots=True)
class SandboxEndpoint:
    url: str
    token: str

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    @property
    def transport_label(self) -> str:
        if not self.url:
            return "disabled"
        return "unix" if self.url.startswith("unix://") else "tcp"


def sandbox_endpoint() -> SandboxEndpoint:
    raw = os.environ.get("AGENT_SANDBOX_URL")
    url = DEFAULT_SANDBOX_URL if raw is None else raw.strip()
    return SandboxEndpoint(url=url, token=os.environ.get("AGENT_SANDBOX_TOKEN", "").strip())


def _client(endpoint: SandboxEndpoint, timeout: float) -> httpx.AsyncClient:
    headers = {"User-Agent": "yawnbot-sandbox-client"}
    if endpoint.token:
        headers["Authorization"] = f"Bearer {endpoint.token}"
    if endpoint.url.startswith("unix://"):
        socket_path = endpoint.url[len("unix://") :]
        transport = httpx.AsyncHTTPTransport(uds=socket_path)
        return httpx.AsyncClient(
            transport=transport,
            base_url="http://sandbox",
            headers=headers,
            timeout=timeout,
        )
    parsed = urlparse(endpoint.url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SandboxError("misconfigured", "AGENT_SANDBOX_URL 必须是 unix:// 或 http(s):// 地址")
    return httpx.AsyncClient(
        base_url=endpoint.url.rstrip("/"),
        headers=headers,
        timeout=timeout,
        # 沙箱地址由运维配置，不走系统代理，也不跟随重定向。
        trust_env=False,
        follow_redirects=False,
    )


class SandboxClient:
    def __init__(self, endpoint: SandboxEndpoint | None = None) -> None:
        self.endpoint = endpoint or sandbox_endpoint()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        content: bytes | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        raw: bool = False,
    ) -> Any:
        if not self.endpoint.enabled:
            raise SandboxError("disabled", "代码沙箱未配置（AGENT_SANDBOX_URL 为空）")
        try:
            async with _client(self.endpoint, timeout) as client:
                response = await client.request(
                    method,
                    f"/v1{path}",
                    json=json_body,
                    content=content,
                    params={key: value for key, value in (params or {}).items() if value is not None},
                )
        except SandboxError:
            raise
        except httpx.TimeoutException as exc:
            raise SandboxError("timeout", "代码沙箱响应超时") from exc
        except (httpx.HTTPError, OSError) as exc:
            raise SandboxError("offline", "代码沙箱未运行或无法连接") from exc
        if response.status_code >= 400:  # noqa: PLR2004
            code = "http_error"
            message = f"代码沙箱返回错误（HTTP {response.status_code}）"
            try:
                error = response.json().get("error") or {}
                code = str(error.get("code") or code)
                message = str(error.get("message") or message)
            except ValueError:
                pass
            raise SandboxError(code, message, status=response.status_code)
        if raw:
            return response
        try:
            body = response.json()
        except ValueError as exc:
            raise SandboxError("bad_response", "代码沙箱返回了无法解析的数据") from exc
        return body.get("data") if isinstance(body, dict) else body

    # ── 状态 ────────────────────────────────────────────────────────────────

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/health", timeout=5.0)

    async def stats(self) -> dict[str, Any]:
        return await self._request("GET", "/stats", timeout=10.0)

    async def packages(self) -> dict[str, Any]:
        return await self._request("GET", "/packages")

    # ── 执行 ────────────────────────────────────────────────────────────────

    async def run(
        self,
        workspace: str,
        code: str,
        *,
        limits: dict[str, int] | None = None,
        meta: dict[str, Any] | None = None,
        language: str = "python",
    ) -> dict[str, Any]:
        timeout = float((limits or {}).get("timeout_seconds") or 30) + _RUN_TIMEOUT_GRACE
        body: dict[str, Any] = {"workspace": workspace, "code": code, "limits": limits or {}, "meta": meta or {}}
        if language != "python":
            # 旧 runner 不认识 language 字段；Python 运行不带它，保持向后兼容。
            body["language"] = language
        return await self._request(
            "POST",
            "/run",
            json_body=body,
            timeout=timeout,
        )

    async def runs(self, **filters: Any) -> dict[str, Any]:
        return await self._request("GET", "/runs", params=filters)

    async def run_detail(self, run_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/runs/{quote(run_id, safe='')}")

    async def cancel(self, run_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/runs/{quote(run_id, safe='')}/cancel")

    # ── 工作区 ──────────────────────────────────────────────────────────────

    async def workspaces(self) -> dict[str, Any]:
        return await self._request("GET", "/workspaces", timeout=20.0)

    async def list_files(
        self, workspace: str, *, prefix: str = "", limit: int = 200, dirs: bool = False
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/workspaces/{quote(workspace, safe='')}/files",
            params={"prefix": prefix or None, "limit": limit, "dirs": "1" if dirs else None},
        )

    async def read_file(self, workspace: str, path: str, *, max_bytes: int | None = None) -> tuple[bytes, str, str]:
        response = await self._request(
            "GET",
            f"/workspaces/{quote(workspace, safe='')}/file",
            params={"path": path, "max_bytes": max_bytes},
            timeout=30.0,
            raw=True,
        )
        return (
            response.content,
            response.headers.get("Content-Type", "application/octet-stream"),
            response.headers.get("X-File-Kind", "other"),
        )

    async def write_file(
        self, workspace: str, path: str, data: bytes, *, overwrite: bool = True
    ) -> dict[str, Any]:
        # 旧 runner 不认识 overwrite 参数（总是覆盖）；只在需要保护时才带上。
        return await self._request(
            "PUT",
            f"/workspaces/{quote(workspace, safe='')}/file",
            params={"path": path, "overwrite": None if overwrite else "0"},
            content=data,
            timeout=60.0,
        )

    async def rename_file(
        self, workspace: str, source: str, target: str, *, overwrite: bool = False
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/workspaces/{quote(workspace, safe='')}/rename",
            json_body={"from": source, "to": target, "overwrite": overwrite},
        )

    async def make_dir(self, workspace: str, path: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/workspaces/{quote(workspace, safe='')}/mkdir",
            json_body={"path": path},
        )

    async def delete_file(self, workspace: str, path: str) -> dict[str, Any]:
        return await self._request(
            "DELETE",
            f"/workspaces/{quote(workspace, safe='')}/file",
            params={"path": path},
        )

    async def reset_workspace(self, workspace: str, *, forget_history: bool = False) -> dict[str, Any]:
        return await self._request(
            "DELETE",
            f"/workspaces/{quote(workspace, safe='')}",
            params={"forget_history": "1" if forget_history else None},
            timeout=60.0,
        )

    async def prune(self) -> dict[str, Any]:
        return await self._request("POST", "/maintenance/prune", timeout=120.0)


class _HealthCache:
    """进程内的短期健康缓存：工具发现每轮都要知道沙箱在不在，不能每次都打 socket。"""

    def __init__(self) -> None:
        self.value: dict[str, Any] | None = None
        self.error: SandboxError | None = None
        self.checked_at = 0.0

    def fresh(self) -> bool:
        return time.monotonic() - self.checked_at < _HEALTH_TTL_SECONDS

    def remember(self, value: dict[str, Any] | None, error: SandboxError | None) -> None:
        self.value = value
        self.error = error
        self.checked_at = time.monotonic()


_health_cache = _HealthCache()


async def cached_health(*, refresh: bool = False) -> tuple[dict[str, Any] | None, SandboxError | None]:
    """返回 (health, error)；任何失败都只体现在 error 里，不抛异常。"""

    if not refresh and _health_cache.checked_at and _health_cache.fresh():
        return _health_cache.value, _health_cache.error
    try:
        value = await SandboxClient().health()
    except SandboxError as exc:
        _health_cache.remember(None, exc)
        return None, exc
    _health_cache.remember(value, None)
    return value, None


def peek_health() -> tuple[dict[str, Any] | None, SandboxError | None, bool]:
    """同步读取上次健康检查结果（不发请求）；第三项表示是否还新鲜。"""

    return _health_cache.value, _health_cache.error, bool(_health_cache.checked_at and _health_cache.fresh())


def supports_language(health: dict[str, Any] | None, language: str) -> bool:
    """runner 是否支持某种运行语言；旧 runner 的 health 没有 languages，只当作支持 Python。"""

    if language == "python":
        return True
    languages = (health or {}).get("languages")
    return isinstance(languages, list) and language in languages


def supports_feature(health: dict[str, Any] | None, feature: str) -> bool:
    """runner 是否声明了某个可选能力（重命名、上传防覆盖……）；旧 runner 一律没有。"""

    features = (health or {}).get("features")
    return isinstance(features, list) and feature in features


def reset_health_cache() -> None:
    _health_cache.remember(None, None)
    _health_cache.checked_at = 0.0


def workspace_for_group(group_id: int) -> str:
    return f"g{int(group_id)}"


ADMIN_WORKSPACE = "admin"


__all__ = [
    "ADMIN_WORKSPACE",
    "DEFAULT_SANDBOX_URL",
    "SandboxClient",
    "SandboxEndpoint",
    "SandboxError",
    "cached_health",
    "peek_health",
    "reset_health_cache",
    "sandbox_endpoint",
    "supports_feature",
    "supports_language",
    "workspace_for_group",
]
