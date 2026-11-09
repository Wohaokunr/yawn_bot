# ruff: noqa: E501, TC003
"""模型没回应时要不要原样再试一次：群聊、私聊、后台小助手共用同一套判断。

以前三处各写各的：群聊完全不重试（上游抖一下就回“现在有点忙”），私聊和小助手
不分原因一律重试（密钥错、请求非法也白等一次）。现在统一成：

- 只看 ``llm`` 给的失败原因（``LLMToolCompletionResult.error_kind``）：超时、空回复、
  限流、服务端错误、连接问题值得再试；密钥/额度/请求非法/上下文超长不值得。
- 再试之前按原因停一下（限流停久一点）；调用方给了回合期限时，剩余时间不够就不试。
- 最多再试 ``retries`` 次（默认 1）。请求内容不变，前缀缓存照常命中。

这里不发任何提示词，也不碰用户消息。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any

# 没有 error_kind 的旧结果（测试替身、其他实现）按 outcome 判断。
_LEGACY_RETRY_OUTCOMES = frozenset({"timeout", "empty", "error"})
# 再试之前停多久（秒）。限流要多等一会儿，别立刻撞第二次。
RETRY_PAUSE_SECONDS: dict[str, float] = {
    "rate_limited": 3.0,
    "server": 1.5,
    "connection": 1.5,
}
DEFAULT_RETRY_PAUSE_SECONDS = 1.0
# 再试至少要留这么多时间给模型，否则试了也只是再超时一次。
MIN_RETRY_WINDOW_SECONDS = 15.0


def error_kind(result: Any) -> str:
    """失败原因；拿不到细分原因时退回 outcome。"""

    return str(getattr(result, "error_kind", "") or getattr(result, "outcome", "") or "")


def should_retry(result: Any) -> bool:
    if getattr(result, "message", None) is not None:
        return False
    retryable = getattr(result, "retryable", None)
    if isinstance(retryable, bool) and getattr(result, "error_kind", ""):
        return retryable
    return str(getattr(result, "outcome", "")) in _LEGACY_RETRY_OUTCOMES


def retry_pause(result: Any) -> float:
    return RETRY_PAUSE_SECONDS.get(error_kind(result), DEFAULT_RETRY_PAUSE_SECONDS)


async def complete_with_retry(
    call: Callable[[], Awaitable[Any]],
    *,
    retries: int = 1,
    deadline: float | None = None,
    pause: float | None = None,
    on_retry: Callable[[Any, int], None] | None = None,
) -> Any:
    """请求一次模型；没拿到回复且值得再试时，停一下原样再请求。

    ``call`` 每次调用都发起同一个请求。``deadline`` 是 ``time.monotonic()`` 口径的回合
    期限，停顿之后剩余时间不足 ``MIN_RETRY_WINDOW_SECONDS`` 就直接返回第一次的结果。
    ``on_retry(result, attempt)`` 在每次再试之前调用（记日志、记 Trace），抛错不影响重试。
    """

    result = await call()
    for attempt in range(max(retries, 0)):
        if not should_retry(result):
            break
        wait = retry_pause(result) if pause is None else max(pause, 0.0)
        if (
            deadline is not None
            and time.monotonic() + wait + MIN_RETRY_WINDOW_SECONDS > deadline
        ):
            break
        if on_retry is not None:
            with contextlib.suppress(Exception):
                on_retry(result, attempt + 1)
        if wait > 0:
            await asyncio.sleep(wait)
        result = await call()
    return result


__all__ = [
    "DEFAULT_RETRY_PAUSE_SECONDS",
    "MIN_RETRY_WINDOW_SECONDS",
    "RETRY_PAUSE_SECONDS",
    "complete_with_retry",
    "error_kind",
    "retry_pause",
    "should_retry",
]
