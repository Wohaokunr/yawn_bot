# ruff: noqa: E501, PLR2004, TID252
"""DeepSeek 官方 Search / Fetch API 客户端（网页搜索工具族的唯一出站通道）。

接口（DeepSeek 2026-10 发布，计费 ¥5 / 千次 *成功* 请求，失败和无结果不计费，
每个账号每个接口 5 QPS）：

- ``POST {base}/v1/tools/search``：``{"query", "count"(1-50), "need_content"}`` →
  ``{"results": [{title, url, snippet, summary, content, site_name, date,
  rerank_score(0-3), authority(0-4)}]}``；query 1-128 个字符，支持 ``site:``、
  ``-site:``、引号短语等运算符。
- ``POST {base}/v1/tools/fetch``：``{"url"}`` → ``{"title", "content"(Markdown)}``；
  URL 不超过 8192 字符、只能 http(s)、不能带账号密码。站点拒绝时 403，取不到
  正文时 404。

密钥按顺序取：``AGENT_WEB_SEARCH_API_KEY`` → ``DEEPSEEK_API_KEY`` → 已配置的、
Base URL 指向 ``*.deepseek.com`` 的模型提供商密钥（默认提供商优先）。这样已经
用 DeepSeek 做对话模型的部署不需要再填一次密钥。

只有 408/429/5xx 和网络超时会重试（最多 3 次，尊重 ``Retry-After``，单次等待
不超过 5 秒）；其余 4xx 立刻失败并给出可读原因。本模块不做配额与缓存——那是
``web_search_policy`` / ``web_search_cache`` 的事。
"""

from __future__ import annotations

import asyncio
import ipaddress
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

DEFAULT_BASE_URL = "https://api.deepseek.com"
SEARCH_PATH = "/v1/tools/search"
FETCH_PATH = "/v1/tools/fetch"
SEARCH_TIMEOUT_SECONDS = 30.0
FETCH_TIMEOUT_SECONDS = 60.0
MAX_ATTEMPTS = 3
MAX_RETRY_DELAY_SECONDS = 5.0
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
MAX_QUERY_CHARS = 128
MAX_URL_CHARS = 8192
MAX_COUNT = 50
# 价目表：每个接口每千次成功请求 ¥5。
PRICE_PER_CALL_YUAN = 0.005
_DEEPSEEK_HOST_SUFFIX = "deepseek.com"
_BLOCKED_HOST_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home.arpa")


class WebSearchError(Exception):
    """搜索/抓取失败。``code`` 是机器可读原因，``status`` 是上游 HTTP 状态码。"""

    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status

    @property
    def billed(self) -> bool:
        """DeepSeek 只对成功请求计费；任何报错都不计费。"""

        return False


@dataclass(frozen=True, slots=True)
class ApiKeyInfo:
    key: str
    source: str

    @property
    def masked(self) -> str:
        if len(self.key) <= 8:
            return "*" * len(self.key)
        return f"{self.key[:3]}…{self.key[-4:]}"


@dataclass(frozen=True, slots=True)
class SearchHit:
    title: str
    url: str
    snippet: str = ""
    summary: str = ""
    content: str = ""
    site_name: str = ""
    date: str = ""
    score: float | None = None
    authority: float | None = None


@dataclass(frozen=True, slots=True)
class SearchResponse:
    query: str
    hits: tuple[SearchHit, ...]
    raw_count: int
    duration_ms: int
    attempts: int = 1

    @property
    def billed(self) -> bool:
        return bool(self.raw_count)


@dataclass(frozen=True, slots=True)
class FetchedPage:
    url: str
    title: str
    content: str
    duration_ms: int
    attempts: int = 1


@dataclass(slots=True)
class _Stats:
    """进程内调用统计，给 WebUI 看最近一次错误和平均耗时。"""

    calls: int = 0
    failures: int = 0
    total_ms: int = 0
    last_error: str = ""
    last_error_at: float | None = None
    last_ok_at: float | None = None
    by_status: dict[str, int] = field(default_factory=dict)

    def record(self, *, ok: bool, duration_ms: int, status: str, error: str = "") -> None:
        self.calls += 1
        self.total_ms += max(duration_ms, 0)
        self.by_status[status] = self.by_status.get(status, 0) + 1
        if ok:
            self.last_ok_at = time.time()
        else:
            self.failures += 1
            self.last_error = error[:200]
            self.last_error_at = time.time()

    def snapshot(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "failures": self.failures,
            "avg_ms": int(self.total_ms / self.calls) if self.calls else 0,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "last_ok_at": self.last_ok_at,
            "by_status": dict(self.by_status),
        }


_stats = _Stats()


def client_stats() -> dict[str, Any]:
    return _stats.snapshot()


def reset_stats_for_tests() -> None:
    global _stats  # noqa: PLW0603
    _stats = _Stats()


def _env(name: str) -> str:
    """先读进程环境（生产入口会把 config/.env 注入），再读 NoneBot 加载的 .env。"""

    value = os.environ.get(name, "").strip()
    if value:
        return value
    try:
        import nonebot

        raw = getattr(nonebot.get_driver().config, name.lower(), None)
    except Exception:  # noqa: BLE001
        return ""
    if raw is None:
        return ""
    secret = getattr(raw, "get_secret_value", None)
    return str(secret() if callable(secret) else raw).strip()


def base_url() -> str:
    raw = _env("AGENT_WEB_SEARCH_BASE_URL") or DEFAULT_BASE_URL
    return raw.rstrip("/")


def _provider_key() -> ApiKeyInfo | None:
    """已配置的 DeepSeek 模型提供商密钥；默认提供商优先，其次按定义顺序。"""

    try:
        from ..llm import ai_config, resolve_provider
    except Exception:  # noqa: BLE001
        return None
    provider_ids = ["default", *[item.id for item in getattr(ai_config, "ai_providers", [])]]
    for provider_id in provider_ids:
        try:
            url, key = resolve_provider(provider_id)
        except Exception:  # noqa: BLE001
            continue
        host = (urlparse(url).hostname or "").casefold()
        if key and (host == _DEEPSEEK_HOST_SUFFIX or host.endswith(f".{_DEEPSEEK_HOST_SUFFIX}")):
            label = "AI_API_KEY" if provider_id == "default" else f"AI_PROVIDER_API_KEYS[{provider_id}]"
            return ApiKeyInfo(key=key, source=label)
    return None


def find_api_key() -> ApiKeyInfo | None:
    """按优先级找密钥；都没有时返回 None（工具族随之隐藏，原因 ``web_no_key``）。"""

    for name in ("AGENT_WEB_SEARCH_API_KEY", "DEEPSEEK_API_KEY"):
        value = _env(name)
        if value:
            return ApiKeyInfo(key=value, source=name)
    return _provider_key()


def normalize_query(query: str) -> str:
    """压缩空白并校验长度；不合法时抛 ``WebSearchError``（请求前拦下，不花钱）。"""

    normalized = " ".join(str(query or "").split())
    if not normalized:
        raise WebSearchError("invalid_query", "搜索词不能为空")
    length = len(normalized)
    if length > MAX_QUERY_CHARS:
        raise WebSearchError(
            "query_too_long",
            f"搜索词有 {length} 个字符，DeepSeek 最多接受 {MAX_QUERY_CHARS} 个",
        )
    return normalized


def _host_is_private(host: str) -> bool:
    folded = host.strip("[]").casefold()
    if folded in {"localhost", ""} or folded.endswith(_BLOCKED_HOST_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(folded)
    except ValueError:
        # 没有点的主机名（intranet、nas）只可能是内网名字。
        return "." not in folded
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def validate_fetch_url(url: str) -> str:
    """抓取前的本地校验：http(s)、有主机、不带账号密码、不指向内网、长度上限。"""

    normalized = str(url or "").strip()
    if not normalized:
        raise WebSearchError("invalid_url", "网址不能为空")
    if len(normalized) > MAX_URL_CHARS:
        raise WebSearchError("invalid_url", f"网址太长（上限 {MAX_URL_CHARS} 字符）")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"}:
        raise WebSearchError("invalid_url", "只能抓取 http/https 网址")
    if not parsed.hostname:
        raise WebSearchError("invalid_url", "网址缺少域名")
    if parsed.username or parsed.password:
        raise WebSearchError("invalid_url", "网址里不能带账号密码")
    if _host_is_private(parsed.hostname):
        raise WebSearchError("private_url", "不能抓取内网或本机地址")
    return normalized


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def _as_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def parse_search_payload(data: Any) -> tuple[list[SearchHit], int]:
    """上游 JSON → ``SearchHit`` 列表；没有可用 http(s) 链接的条目丢弃。"""

    entries = data.get("results") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise WebSearchError("bad_response", "搜索接口返回里没有 results 数组")
    hits: list[SearchHit] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        url = _as_str(entry.get("url"))
        if not _is_http_url(url):
            continue
        hits.append(
            SearchHit(
                title=_as_str(entry.get("title")) or url,
                url=url,
                snippet=_as_str(entry.get("snippet")),
                summary=_as_str(entry.get("summary")),
                content=_as_str(entry.get("content")),
                site_name=_as_str(entry.get("site_name")),
                date=_as_str(entry.get("date")),
                score=_as_number(entry.get("rerank_score")),
                authority=_as_number(entry.get("authority")),
            )
        )
    return hits, len(entries)


_STATUS_CODES: dict[int, tuple[str, str]] = {
    400: ("bad_request", "请求参数不被接受"),
    401: ("auth", "DeepSeek API 密钥无效或已失效"),
    402: ("balance", "DeepSeek 账户余额不足"),
    403: ("forbidden", "目标站点或内容被 DeepSeek 的安全策略拒绝"),
    404: ("not_found", "没有取到可用内容"),
    408: ("timeout", "DeepSeek 读取超时"),
    413: ("too_large", "页面内容太大"),
    422: ("bad_request", "请求参数不合法"),
    429: ("rate_limited", "请求太频繁，触发了 DeepSeek 限流"),
}


def _error_detail(response: httpx.Response) -> str:
    try:
        parsed = response.json()
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict) and _as_str(error.get("message")):
            return _as_str(error.get("message"))[:300]
        if _as_str(parsed.get("message")):
            return _as_str(parsed.get("message"))[:300]
        if isinstance(error, str) and error.strip():
            return error.strip()[:300]
    return (response.text or "").strip()[:300] or response.reason_phrase or "无响应内容"


def _status_error(response: httpx.Response) -> WebSearchError:
    status = response.status_code
    code, label = _STATUS_CODES.get(status, ("upstream", f"DeepSeek 返回 HTTP {status}"))
    if status >= 500:
        code, label = "upstream", f"DeepSeek 服务暂时出错（HTTP {status}）"
    detail = _error_detail(response)
    message = label if not detail or detail == label else f"{label}：{detail}"
    return WebSearchError(code, message, status=status)


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    header = response.headers.get("retry-after") if response is not None else None
    delay: float
    try:
        delay = float(header) if header else 0.5 * 2 ** (attempt - 1)
    except ValueError:
        delay = 0.5 * 2 ** (attempt - 1)
    return min(max(delay, 0.0), MAX_RETRY_DELAY_SECONDS)


class DeepSeekWebClient:
    """无状态客户端；``transport`` 只给测试注入 ``httpx.MockTransport``。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._base = (base or base_url()).rstrip("/")
        self._transport = transport

    def _key(self) -> str:
        if self._api_key:
            return self._api_key
        info = find_api_key()
        if info is None:
            raise WebSearchError(
                "no_key",
                "没有配置 DeepSeek API 密钥（AGENT_WEB_SEARCH_API_KEY / DEEPSEEK_API_KEY）",
            )
        return info.key

    async def _post(self, path: str, body: dict[str, Any], timeout: float) -> tuple[Any, int, int]:
        key = self._key()
        started = time.monotonic()
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        }
        attempt = 0
        async with httpx.AsyncClient(
            timeout=timeout, transport=self._transport, follow_redirects=False
        ) as client:
            while True:
                attempt += 1
                try:
                    response = await client.post(f"{self._base}{path}", json=body, headers=headers)
                except httpx.TimeoutException as exc:
                    if attempt < MAX_ATTEMPTS:
                        await asyncio.sleep(_retry_delay(None, attempt))
                        continue
                    self._record(started, "timeout", "请求超时", ok=False)
                    raise WebSearchError(
                        "timeout", f"DeepSeek {int(timeout)} 秒内没有响应"
                    ) from exc
                except httpx.HTTPError as exc:
                    self._record(started, "network", str(exc), ok=False)
                    raise WebSearchError("network", f"连不上 DeepSeek：{type(exc).__name__}") from exc
                if response.status_code < 300:
                    try:
                        data = response.json()
                    except ValueError as exc:
                        self._record(started, "bad_response", "非 JSON 响应", ok=False)
                        raise WebSearchError(
                            "bad_response", "DeepSeek 返回的不是 JSON"
                        ) from exc
                    duration = int((time.monotonic() - started) * 1000)
                    self._record(started, str(response.status_code), ok=True)
                    return data, duration, attempt
                if response.status_code in RETRYABLE_STATUS and attempt < MAX_ATTEMPTS:
                    await asyncio.sleep(_retry_delay(response, attempt))
                    continue
                error = _status_error(response)
                self._record(started, str(response.status_code), str(error), ok=False)
                raise error

    @staticmethod
    def _record(started: float, status: str, error: str = "", *, ok: bool) -> None:
        _stats.record(
            ok=ok,
            duration_ms=int((time.monotonic() - started) * 1000),
            status=status,
            error=error,
        )

    async def search(
        self, query: str, *, count: int = 5, need_content: bool = False
    ) -> SearchResponse:
        normalized = normalize_query(query)
        bounded = max(1, min(int(count), MAX_COUNT))
        data, duration, attempts = await self._post(
            SEARCH_PATH,
            {"query": normalized, "count": bounded, "need_content": bool(need_content)},
            SEARCH_TIMEOUT_SECONDS,
        )
        hits, raw_count = parse_search_payload(data)
        if raw_count and not hits:
            raise WebSearchError("bad_response", "搜索结果里没有可用的 http(s) 链接")
        return SearchResponse(
            query=normalized,
            hits=tuple(hits),
            raw_count=raw_count,
            duration_ms=duration,
            attempts=attempts,
        )

    async def fetch(self, url: str) -> FetchedPage:
        normalized = validate_fetch_url(url)
        try:
            data, duration, attempts = await self._post(
                FETCH_PATH, {"url": normalized}, FETCH_TIMEOUT_SECONDS
            )
        except WebSearchError as exc:
            if exc.status == 403:
                raise WebSearchError(
                    "forbidden", "这个站点拒绝了 DeepSeek 的抓取（安全策略）", status=403
                ) from exc
            if exc.status == 404:
                raise WebSearchError(
                    "not_found",
                    "没取到可用正文：页面可能失效、屏蔽抓取、不是网页或没有可提取的文字",
                    status=404,
                ) from exc
            raise
        content = data.get("content") if isinstance(data, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise WebSearchError("not_found", "抓取结果里没有正文")
        return FetchedPage(
            url=normalized,
            title=_as_str(data.get("title")),
            content=content,
            duration_ms=duration,
            attempts=attempts,
        )


__all__ = [
    "DEFAULT_BASE_URL",
    "FETCH_PATH",
    "MAX_COUNT",
    "MAX_QUERY_CHARS",
    "MAX_URL_CHARS",
    "PRICE_PER_CALL_YUAN",
    "SEARCH_PATH",
    "ApiKeyInfo",
    "DeepSeekWebClient",
    "FetchedPage",
    "SearchHit",
    "SearchResponse",
    "WebSearchError",
    "base_url",
    "client_stats",
    "find_api_key",
    "normalize_query",
    "parse_search_payload",
    "validate_fetch_url",
]
