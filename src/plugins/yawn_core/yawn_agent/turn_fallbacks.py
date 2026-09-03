# ruff: noqa: E501, PLR0911, PLR0913, RUF001, TC003
"""Agent 回合的纯本地 fallback 与等待提示辅助逻辑。"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import datetime
from typing import Any

GREETING_WORDS = ("你好", "嗨", "hello", "hi", "早上好", "晚上好", "在吗", "在不在")
FALLBACK_NOTICES = (
    "现在有点忙，稍后再试～",
    "这会儿没接上思路，等我一下～",
    "暂时答不上来，过一会儿再问我吧～",
)
FALLBACK_CURSOR_LIMIT = 256
WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
WAIT_NOTICE = "收到啦，我正在想，稍等一下～"
WAIT_NOTICE_DEFAULT_DELAY = 6.0
WAIT_NOTICE_TASK: ContextVar[asyncio.Task[None] | None] = ContextVar(
    "agent_dialogue_wait_notice", default=None
)
_FALLBACK_CURSOR: dict[int, int] = {}


def contains_word(text: str, word: str) -> bool:
    if not word:
        return False
    if re.fullmatch(r"[a-z0-9 ]+", word):
        return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) is not None
    return word in text


def deterministic_reply(text: str, *, now: datetime) -> str | None:
    """不查库、不调协议的确定性回复。"""

    normalized = " ".join(text.lower().split())
    if any(contains_word(normalized, word) for word in GREETING_WORDS):
        return "我在呀，有事直接说～"
    if "agent状态" in normalized or "群聊agent" in normalized:
        return "群聊 Agent 在线；复杂对话需要配置 AI_API_KEY。"
    if any(word in normalized for word in ("谢谢", "感谢", "多谢", "thanks", "thx")):
        return "不客气～"
    if any(word in normalized for word in ("几点", "现在时间", "什么时间", "报时", "时间是")):
        return f"现在是 {now:%H:%M}（北京时间）。"
    if any(word in normalized for word in ("今天几号", "今天日期", "现在日期", "星期几", "周几", "今天星期")):
        return f"今天是 {now:%Y年%m月%d日} {WEEKDAY_NAMES[now.weekday()]}。"
    if any(word in normalized for word in ("会什么", "能做什么", "有哪些功能", "帮助")):
        return "发 /help 可以看全部命令；群里直接 @ 我就能聊天。"
    if "记忆" in normalized:
        return "群记忆用 /Agent记忆 查看；想退出记录发 /Agent隐私。"
    if "画像" in normalized:
        return "人物画像用 /Agent画像 查看。"
    return None


def fallback_notice(group_id: int) -> str:
    index = _FALLBACK_CURSOR.get(group_id, -1) + 1
    if len(_FALLBACK_CURSOR) >= FALLBACK_CURSOR_LIMIT:
        _FALLBACK_CURSOR.clear()
    _FALLBACK_CURSOR[group_id] = index
    return FALLBACK_NOTICES[index % len(FALLBACK_NOTICES)]


def wait_notice_delay() -> float:
    raw = os.environ.get("AGENT_AI_WAIT_NOTICE_DELAY", "").strip()
    if not raw:
        return WAIT_NOTICE_DEFAULT_DELAY
    try:
        return float(raw)
    except ValueError:
        return WAIT_NOTICE_DEFAULT_DELAY


def cancel_wait_notice() -> None:
    task = WAIT_NOTICE_TASK.get()
    if task is None:
        return
    WAIT_NOTICE_TASK.set(None)
    if not task.done():
        task.cancel()


def start_wait_notice(
    bot: Any,
    group_id: int,
    enqueued_at: float | None,
    message_id: Any,
    *,
    send_unless_expired: Callable[..., Awaitable[Any]],
    debug: Callable[[str], None],
    debug_exception: Callable[[str], None],
) -> None:
    """超过阈值后补一次界面等待提示；副作用通过回调注入。"""

    delay = wait_notice_delay()
    if delay <= 0:
        return

    async def _notice() -> None:
        try:
            await asyncio.sleep(delay)
            await send_unless_expired(
                bot,
                group_id,
                WAIT_NOTICE,
                enqueued_at,
                label="等待提示",
                message_id=message_id,
                cancel_wait_notice=False,
            )
            debug(f"群 {group_id} 处理超过 {delay:.1f}s,已发送一次等待提示")
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            debug_exception(f"群 {group_id} 等待提示发送失败(忽略)")

    WAIT_NOTICE_TASK.set(asyncio.create_task(_notice()))


__all__ = [
    "WAIT_NOTICE",
    "WAIT_NOTICE_DEFAULT_DELAY",
    "WAIT_NOTICE_TASK",
    "cancel_wait_notice",
    "contains_word",
    "deterministic_reply",
    "fallback_notice",
    "start_wait_notice",
    "wait_notice_delay",
]
