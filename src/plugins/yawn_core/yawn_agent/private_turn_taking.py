# ruff: noqa: TID252
"""私聊发言判断：这一轮要不要说话、要不要把话让给下一轮。

用户习惯一句话拆成好几条发（“在吗”“问你个事”“那个 apk……”），以前每条都触发
一轮完整回复，几条消息就换来几段回答，像刷屏。这里给私聊 Agent 三种“不说话”：

1. **模型自己决定**：最终回复只写 ``【不回】``（这句话不需要回应）或 ``【等他说完】``
   （他明显还没说完）。后者会挂一个计时器：``PRIVATE_AGENT_WAIT_SECONDS`` 秒内他没再
   发消息，就由 ``resume_after_wait`` 自己接上话；他发了新消息就交给新消息那一轮。
2. **让给下一轮**：模型想好了回复、还没发出去，他又发来了新消息（对话队列里有排队，
   或到达计数变了）→ 这条不发，下一轮看到全部消息一起回。写操作已经成功的轮次不让，
   以免“签到成功”之类的结果被吞掉；连续让太多次也不再让（``MAX_YIELDS``）。
3. 关掉 ``PRIVATE_AGENT_TURN_TAKING`` 后以上都不生效，恢复每轮必回。

判断“还没说完/不需要回”完全交给模型（不做关键词匹配，见 AGENTS.md）；这里只看结构
事实：标记、队列、计数和时间。
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .log import dbg, dbg_exc

SKIP_MARK = "【不回】"
WAIT_MARK = "【等他说完】"
# 连续让给下一轮最多几次、最长多久；超过就照常回复，防止他一直发、Bot 一直不说话。
MAX_YIELDS = 3
MAX_YIELD_SPAN_SECONDS = 90.0
_RHYTHM_TAG = re.compile(r"^\s*【节奏[:：][^】]{0,12}】\s*")
_MARKS = re.compile(r"【(?:不回|等他说完)】")

ResumeHook = Callable[[Any, int], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Silence:
    kind: str  # "skip" | "wait" | ""
    text: str  # 去掉标记后的正文（kind 为空时照常发送）

    @property
    def silent(self) -> bool:
        return bool(self.kind)


def parse_silence(text: str) -> Silence:
    """解析模型的“不说话”标记。只有标记、没有别的正文时才算不说话。"""

    raw = str(text or "")
    body = _RHYTHM_TAG.sub("", raw, count=1).strip()
    kind = ""
    if body.startswith(WAIT_MARK):
        kind = "wait"
    elif body.startswith(SKIP_MARK):
        kind = "skip"
    if not kind and not _MARKS.search(raw):
        return Silence("", raw)
    rest = _MARKS.sub("", body).replace("|||", "").strip()
    if kind and not rest:
        return Silence(kind, "")
    # 写了标记又写了话：以正文为准，标记删掉。
    return Silence("", _MARKS.sub("", raw).strip())


def enabled() -> bool:
    from ..private_chat_bridge import private_agent_config

    return bool(private_agent_config.private_agent_turn_taking)


# ── 让给下一轮 ────────────────────────────────────────────────────────────


def arrivals(user_id: int) -> int:
    try:
        from ..chat_state import arrival_count

        return arrival_count(user_id)
    except Exception:  # noqa: BLE001
        return 0


def pending(user_id: int) -> int:
    try:
        from ..chat_state import pending_messages

        return pending_messages(user_id)
    except Exception:  # noqa: BLE001
        return 0


_streaks: dict[int, tuple[int, float]] = {}


def newer_message_arrived(user_id: int, since: int) -> bool:
    """这一轮开始后他又发了消息（排队中或到达计数变了）。"""

    return pending(user_id) > 0 or arrivals(user_id) != since


def may_yield(user_id: int, *, now: float | None = None) -> bool:
    count, first = _streaks.get(int(user_id), (0, 0.0))
    moment = time.monotonic() if now is None else now
    if count == 0:
        return True
    return count < MAX_YIELDS and moment - first < MAX_YIELD_SPAN_SECONDS


def record_yield(user_id: int, *, now: float | None = None) -> None:
    moment = time.monotonic() if now is None else now
    count, first = _streaks.get(int(user_id), (0, moment))
    _streaks[int(user_id)] = (count + 1, first if count else moment)


def record_spoke(user_id: int) -> None:
    _streaks.pop(int(user_id), None)


# ── 等他说完 ──────────────────────────────────────────────────────────────

_resume: ResumeHook | None = None
_waiters: dict[int, asyncio.Task[Any]] = {}


def register_resume(hook: ResumeHook | None) -> None:
    global _resume  # noqa: PLW0603
    _resume = hook


def cancel_wait(user_id: int) -> None:
    task = _waiters.pop(int(user_id), None)
    if task is not None and not task.done() and task is not asyncio.current_task():
        task.cancel()


def schedule_wait(bot: Any, user_id: int, *, seconds: float, since: int) -> None:
    """等 ``seconds`` 秒；期间没有新消息就调用登记的 ``resume`` 接上话。"""

    cancel_wait(user_id)

    async def wait() -> None:
        try:
            await asyncio.sleep(seconds)
            if newer_message_arrived(user_id, since) or _resume is None:
                return
            dbg(f"私聊用户 {user_id} 等了 {seconds:.0f} 秒没再说话，接上话")
            await _resume(bot, user_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            dbg_exc(f"私聊用户 {user_id} 等他说完后接话失败(忽略)")
        finally:
            if _waiters.get(int(user_id)) is asyncio.current_task():
                _waiters.pop(int(user_id), None)

    _waiters[int(user_id)] = asyncio.create_task(wait())


def waiting(user_id: int) -> bool:
    task = _waiters.get(int(user_id))
    return task is not None and not task.done()


def reset_state_for_tests() -> None:
    for task in list(_waiters.values()):
        task.cancel()
    _waiters.clear()
    _streaks.clear()


__all__ = [
    "MAX_YIELDS",
    "SKIP_MARK",
    "WAIT_MARK",
    "Silence",
    "arrivals",
    "cancel_wait",
    "enabled",
    "may_yield",
    "newer_message_arrived",
    "parse_silence",
    "pending",
    "record_spoke",
    "record_yield",
    "register_resume",
    "reset_state_for_tests",
    "schedule_wait",
    "waiting",
]
