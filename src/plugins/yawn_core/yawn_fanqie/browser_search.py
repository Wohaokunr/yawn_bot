"""Use an isolated real browser for the public Fanqie search page.

The search page is responsible for running Fanqie's own security SDK and
creating the request context required by its search endpoint. This module
only uses the plugin-owned browser context; it never reads, exports, or replays
cookie values or security parameters, and only observes the response produced
by the page itself.

Native deployments may launch a local persistent Chromium context. Container
production can instead connect to the version-pinned Playwright browser server
and persist only Playwright storage state in the plugin data directory.
"""

# The adapter deliberately translates several third-party browser failures into
# one user-facing service error; keep those branches explicit and readable.
# ruff: noqa: BLE001, PLR2004, TRY003, TRY301

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, quote, urlparse

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

_BASE_URL = "https://fanqienovel.com"
_HOSTS = {"fanqienovel.com", "www.fanqienovel.com"}
_SEARCH_PAGE_PREFIX = f"{_BASE_URL}/search/"
_SEARCH_API_PATH = "/api/author/search/search_book/v1"
_SEARCH_QUERY_TYPES = {"0", "1", "2"}
_SEARCH_TAB_SELECTOR = ".search-order-tab"
_CHALLENGE_MARKERS = ("请完成下列验证", "人机验证", "验证码")
_PROFILE_DIR_NAME = "search-browser-profile"
_STORAGE_STATE_NAME = "storage-state.json"
_EMPTY_RESPONSE_RETRIES = 1
_SESSION_COOKIE_NAMES = {"ttwid", "novel_web_id"}
_SESSION_READY_WAIT_SECONDS = 10.0
_SESSION_SETTLE_SECONDS = 2.0
_SEARCH_LOCK = asyncio.Lock()


class BrowserSearchError(RuntimeError):
    """The browser search context cannot produce a public search response."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class BrowserSearchSnapshot:
    """搜索接口响应及同一真实页面生成的动态字体样式。"""

    payload: Any
    page: str


@dataclass(slots=True)
class _BrowserSession:
    context: Any
    browser: Any | None = None
    storage_state_path: Path | None = None


def _load_async_playwright() -> Any:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise BrowserSearchError(
            "未安装 Playwright Python 客户端，请重新执行 uv sync --locked"
        ) from exc
    return async_playwright


def _matches_search_response(response: Any, query_type: str) -> bool:
    """Match only the official search response for the requested sort order."""

    try:
        parsed = urlparse(str(response.url))
    except (TypeError, ValueError):
        return False
    if parsed.scheme != "https":
        return False
    if (parsed.hostname or "").lower().rstrip(".") not in _HOSTS:
        return False
    if parsed.path != _SEARCH_API_PATH:
        return False
    return parse_qs(parsed.query).get("query_type") == [query_type]


def _page_url(keyword: str) -> str:
    return f"{_SEARCH_PAGE_PREFIX}{quote(keyword, safe='')}"


def _normalize_ws_endpoint(value: str | None) -> str:
    if value is None:
        value = os.environ.get("FANQIE_BROWSER_WS_ENDPOINT", "")
    endpoint = value.strip()
    if not endpoint:
        return ""
    parsed = urlparse(endpoint)
    if (
        parsed.scheme not in {"ws", "wss"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise BrowserSearchError("番茄 Playwright 服务地址必须是无账号密码的 ws(s) URL")
    return endpoint


async def _read_response_payload(response: Any) -> Any:
    try:
        status = int(response.status)
        body = await response.body()
    except Exception as exc:
        raise BrowserSearchError("读取番茄搜索响应失败") from exc

    logger.debug(
        "fanqie browser search response: status=%d bytes=%d",
        status,
        len(body),
    )
    if status >= 400:
        raise BrowserSearchError(f"番茄搜索响应 HTTP {status}")
    if not body:
        raise BrowserSearchError(
            "番茄搜索返回空响应，可能需要人工验证",
            retryable=True,
        )

    text = body.decode("utf-8", errors="replace")
    if any(marker in text for marker in _CHALLENGE_MARKERS):
        raise BrowserSearchError("番茄搜索页面要求人工验证")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise BrowserSearchError("番茄搜索响应不是有效 JSON") from exc


async def _capture_response(
    page: Any,
    query_type: str,
    action: Callable[[], Awaitable[Any]],
    timeout_ms: float,
) -> Any:
    try:
        async with page.expect_response(
            lambda response: _matches_search_response(response, query_type),
            timeout=timeout_ms,
        ) as response_info:
            await action()
        response = await response_info.value
    except Exception as exc:
        if exc.__class__.__name__ == "TimeoutError":
            raise BrowserSearchError(
                "番茄搜索页面未在期限内返回结果，可能需要人工验证"
            ) from exc
        if isinstance(exc, BrowserSearchError):
            raise
        raise BrowserSearchError("等待番茄搜索响应失败") from exc
    return await _read_response_payload(response)


async def _page_snapshot(page: Any) -> str:
    """读取页面运行时注入的字体样式，不能读取时保留空快照。"""

    try:
        await page.wait_for_timeout(250)
        return await page.content()
    except Exception:
        logger.debug("fanqie browser search page snapshot unavailable", exc_info=True)
        return ""


async def _click_sort_tab(page: Any, tab_index: int, timeout_ms: float) -> None:
    try:
        await page.wait_for_selector(
            _SEARCH_TAB_SELECTOR,
            state="visible",
            timeout=timeout_ms,
        )
        selectors = (
            f"{_SEARCH_TAB_SELECTOR} .byte-tabs-header-title",
            f"{_SEARCH_TAB_SELECTOR} [role='tab']",
            f"{_SEARCH_TAB_SELECTOR} .ant-tabs-tab",
        )
        for selector in selectors:
            tabs = page.locator(selector)
            if await tabs.count() > tab_index:
                await tabs.nth(tab_index).click()
                return
        raise BrowserSearchError("番茄搜索排序控件结构发生变化")
    except BrowserSearchError:
        raise
    except Exception as exc:
        if exc.__class__.__name__ == "TimeoutError":
            raise BrowserSearchError(
                "番茄搜索排序控件未在期限内加载"
            ) from exc
        raise BrowserSearchError("番茄搜索排序控件不可用") from exc


async def _wait_for_session_cookie(context: Any, timeout_ms: float) -> None:
    """Let the page's security SDK finish setting its session cookie."""

    deadline = asyncio.get_running_loop().time() + min(
        timeout_ms / 1000,
        _SESSION_READY_WAIT_SECONDS,
    )
    while True:
        try:
            cookies = await context.cookies(_BASE_URL)
        except Exception:
            return
        cookie_names = {
            cookie.get("name")
            for cookie in cookies
            if isinstance(cookie, dict)
        }
        if _SESSION_COOKIE_NAMES.issubset(cookie_names):
            await asyncio.sleep(_SESSION_SETTLE_SECONDS)
            return
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(0.25, remaining))


async def _open_browser_session(
    playwright: Any,
    *,
    timeout_ms: float,
    headless: bool,
    profile_path: Path,
    ws_endpoint: str | None,
) -> _BrowserSession:
    endpoint = _normalize_ws_endpoint(ws_endpoint)
    if not endpoint:
        try:
            context = await playwright.chromium.launch_persistent_context(
                str(profile_path),
                headless=headless,
                locale="zh-CN",
                viewport={"width": 1280, "height": 720},
            )
        except Exception as exc:
            raise BrowserSearchError(
                "无法启动番茄搜索浏览器会话，请检查 Chromium 和会话目录"
            ) from exc
        return _BrowserSession(context=context)

    browser = None
    try:
        browser = await playwright.chromium.connect(endpoint, timeout=timeout_ms)
        storage_state_path = profile_path / _STORAGE_STATE_NAME
        context = await browser.new_context(
            locale="zh-CN",
            viewport={"width": 1280, "height": 720},
            storage_state=(
                str(storage_state_path) if storage_state_path.is_file() else None
            ),
        )
    except Exception as exc:
        if browser is not None:
            with suppress(Exception):
                await browser.close()
        raise BrowserSearchError(
            "无法连接番茄搜索 Playwright 服务，请检查 sidecar 状态和 Playwright 版本"
        ) from exc
    return _BrowserSession(
        context=context,
        browser=browser,
        storage_state_path=storage_state_path,
    )


async def _close_browser_session(session: _BrowserSession) -> None:
    if session.storage_state_path is not None:
        try:
            await session.context.storage_state(
                path=str(session.storage_state_path),
                indexed_db=True,
            )
            session.storage_state_path.chmod(0o600)
        except Exception:
            logger.debug(
                "fanqie remote browser storage state could not be persisted",
                exc_info=True,
            )
    try:
        await session.context.close()
    finally:
        if session.browser is not None:
            try:
                await session.browser.close()
            except Exception:
                logger.debug(
                    "fanqie remote browser connection close failed",
                    exc_info=True,
                )


async def _search_page_snapshot(  # noqa: PLR0913
    keyword: str,
    *,
    query_type: str,
    timeout: float,
    headless: bool,
    profile_dir: str | None,
    ws_endpoint: str | None,
) -> BrowserSearchSnapshot:
    timeout_ms = timeout * 1000
    async with _load_async_playwright()() as playwright:
        profile_path = _resolve_profile_dir(profile_dir)
        session = await _open_browser_session(
            playwright,
            timeout_ms=timeout_ms,
            headless=headless,
            profile_path=profile_path,
            ws_endpoint=ws_endpoint,
        )
        context = session.context
        try:
            for attempt in range(_EMPTY_RESPONSE_RETRIES + 1):
                page = await context.new_page()
                try:
                    current_page = page
                    payload = await _capture_response(
                        current_page,
                        "0",
                        lambda current_page=current_page: current_page.goto(
                            _page_url(keyword),
                            wait_until="domcontentloaded",
                            timeout=timeout_ms,
                        ),
                        timeout_ms,
                    )

                    if query_type == "0":
                        return BrowserSearchSnapshot(
                            payload=payload,
                            page=await _page_snapshot(current_page),
                        )

                    sorted_payload = await _capture_response(
                        current_page,
                        query_type,
                        lambda current_page=current_page: _click_sort_tab(
                            current_page,
                            int(query_type),
                            timeout_ms,
                        ),
                        timeout_ms,
                    )
                    return BrowserSearchSnapshot(
                        payload=sorted_payload,
                        page=await _page_snapshot(current_page),
                    )
                except BrowserSearchError as exc:
                    if not exc.retryable or attempt >= _EMPTY_RESPONSE_RETRIES:
                        raise
                    await _wait_for_session_cookie(context, timeout_ms)
                    logger.debug(
                        "fanqie browser search empty response; retrying with "
                        "persisted session: attempt=%d/%d",
                        attempt + 1,
                        _EMPTY_RESPONSE_RETRIES + 1,
                    )
                finally:
                    await page.close()
        finally:
            await _close_browser_session(session)
    raise BrowserSearchError("番茄搜索未返回结果")


def _resolve_profile_dir(configured: str | None) -> Path:
    raw = (configured or "").strip()
    if raw:
        path = Path(raw).expanduser()
    else:
        try:
            from nonebot_plugin_localstore import get_plugin_data_dir

            path = get_plugin_data_dir() / _PROFILE_DIR_NAME
        except (ImportError, RuntimeError):
            path = Path("data") / "yawn_fanqie" / _PROFILE_DIR_NAME
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BrowserSearchError("番茄搜索会话目录不可用") from exc
    return path.resolve()


async def search_page_snapshot(  # noqa: PLR0913
    keyword: str,
    *,
    query_type: str,
    timeout: float,
    headless: bool,
    profile_dir: str | None = None,
    ws_endpoint: str | None = None,
) -> BrowserSearchSnapshot:
    """Return the response and runtime font styles produced by the page."""

    normalized = keyword.strip()
    if not normalized:
        return BrowserSearchSnapshot(
            payload={"data": {"search_book_data_list": []}},
            page="",
        )
    if query_type not in _SEARCH_QUERY_TYPES:
        raise ValueError("不支持的番茄搜索排序")
    if timeout <= 0:
        raise ValueError("番茄浏览器搜索超时必须大于 0")

    try:
        await asyncio.wait_for(_SEARCH_LOCK.acquire(), timeout=timeout)
    except TimeoutError as exc:
        raise BrowserSearchError("番茄搜索浏览器正忙，请稍后重试") from exc
    try:
        return await _search_page_snapshot(
            normalized,
            query_type=query_type,
            timeout=timeout,
            headless=headless,
            profile_dir=profile_dir,
            ws_endpoint=ws_endpoint,
        )
    finally:
        _SEARCH_LOCK.release()


__all__ = [
    "BrowserSearchError",
    "BrowserSearchSnapshot",
    "search_page_snapshot",
]
