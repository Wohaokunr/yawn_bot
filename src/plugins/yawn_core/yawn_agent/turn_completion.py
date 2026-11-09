# ruff: noqa: E501, TC003, TID252
"""群聊回合里“请求一次模型”的可靠性包装：抖动重试 + 轮次/时限用完时的收尾轮。

从 ``dialogue.py`` 拆出来（那个文件有行数上限）。这里不直接 import 模型调用函数：
``dialogue`` 把自己模块里的 ``complete_with_tools_result`` 传进来，测试替换它时仍然生效。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..llm import NO_OUTPUT_LIMIT
from . import turn_budget
from .execution_trace import trace_event
from .llm_retry import complete_with_retry, error_kind
from .log import dbg


async def request_completion(
    complete: Callable[..., Awaitable[Any]], turn: Any, *, wrap_up: bool = False
) -> Any:
    """请求一次模型。

    上游抖动（超时/空回复/限流/5xx）且回合时限还够时原样再试一次。``wrap_up`` 是收尾轮：
    同一份工具表但 ``tool_choice="none"``，用更短的超时，不重试，也不再触发多模态降级。
    """

    started = time.monotonic()
    extra: dict[str, Any] = {"tool_choice": "none"} if wrap_up and turn.tools else {}

    def on_retry(failed: Any, attempt: int) -> None:
        reason = error_kind(failed)
        dbg(f"群 {turn.group_id} 模型没有回应（{reason}），原样再试一次")
        trace_event(
            "llm",
            "模型调用重试",
            status="degraded",
            output={"model": turn.model, "error_kind": reason, "attempt": attempt},
            detail="上游抖动，停一下后用同样的请求再试一次",
            duration_ms=(time.monotonic() - started) * 1000,
            round_index=turn.rounds + 1,
        )

    return await complete_with_retry(
        lambda: complete(
            turn.messages,
            turn.tools,
            task="agent_dialogue",
            max_tokens=NO_OUTPUT_LIMIT,
            timeout=turn_budget.WRAP_UP_COMPLETION_TIMEOUT
            if wrap_up
            else turn_budget.completion_timeout(turn.tools),
            multimodal=bool(turn.media_blocks),
            raise_on_unsupported=bool(turn.media_blocks)
            and not turn.fallback_attempted
            and not wrap_up,
            **extra,
        ),
        retries=0 if wrap_up else 1,
        deadline=turn.deadline,
        on_retry=on_retry,
    )


async def wrap_up_reply(
    complete: Callable[..., Awaitable[Any]], turn: Any, reason: str
) -> str:
    """轮次/时限用完还没最终回复时，请模型根据已有工具结果收尾；拿不到就返回空串。

    只在本轮确实有工具结果时才值得多花这一次请求；什么都没查到就让调用方发固定提示。
    """

    if not turn.had_tool_results:
        return ""
    turn.messages.extend(turn_budget.wrap_up_messages())
    started = time.monotonic()
    completion = await request_completion(complete, turn, wrap_up=True)
    message = getattr(completion, "message", None)
    content = str(getattr(message, "content", "") or "").strip()
    trace_event(
        "llm",
        "收尾轮",
        status="success" if content else "degraded",
        output={
            "reason": reason,
            "content_chars": len(content),
            "outcome": str(getattr(completion, "outcome", "")),
            "error_kind": error_kind(completion) if not content else "",
        },
        detail="工具轮次或时限用完，禁用工具后请模型根据已有结果作答",
        duration_ms=(time.monotonic() - started) * 1000,
    )
    dbg(f"群 {turn.group_id} {reason}：收尾轮{'给出最终回复' if content else '没有回复'}")
    return content


__all__ = ["error_kind", "request_completion", "wrap_up_reply"]
