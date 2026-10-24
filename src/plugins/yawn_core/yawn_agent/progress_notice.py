# ruff: noqa: RUF001, RUF002, PLR0913, TC003, E501, C901
"""慢回合的“我在处理”反馈：先给触发消息贴个表情，真慢了才说一句贴合进度的话。

旧实现在任何回合超过 6 秒时都固定发“收到啦，我正在想，稍等一下～”。只要调用
工具（尤其是代码沙箱）几乎每轮都会触发，同一句话反复刷屏很出戏。现在：

1. 超过 ``delay`` 秒先给触发消息贴一个表情回应（不占消息、不打断聊天）；
2. 贴表情不可用（协议不支持/调用失败/没有 message_id）时才退回一句文字；
3. 已经贴过表情的回合，只有正在跑慢工具（代码沙箱等）且超过 ``long_delay``
   仍没结束时，才补一句说明进度的话；
4. 文字优先用模型这一轮自己说的开场白（“我算一下”），否则按当前阶段从几句
   人设化的说法里轮换，同一个群不会连续两次用同一句。

整个回合最多一个表情 + 一句话；正文发送前一律取消。``AGENT_AI_WAIT_NOTICE_MODE``
可设为 ``reaction``（默认）、``text``（只发文字）或 ``off``。
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

WAIT_NOTICE_DEFAULT_DELAY = 6.0
WAIT_NOTICE_LONG_DELAY = 25.0
WAIT_NOTICE_MODES = ("reaction", "text", "off")
# 👀：表示“看到了，在弄”，比文字更不打扰。
WAIT_NOTICE_EMOJI_ID = "128064"
# 这些工具一跑就可能十几秒，值得在表情之外补一句话。
SLOW_TOOLS = frozenset({"run_python", "run_shell"})
_PREAMBLE_MAX_CHARS = 40
_ROTATION_LIMIT = 256

# 每个阶段几种说法，{self} 换成人设自称（没有就是“我”）。
_PHASE_LINES: dict[str, tuple[str, ...]] = {
    "sandbox": (
        "代码在跑了，马上出结果",
        "{self}在算，稍等一会儿",
        "跑着呢，别急～",
    ),
    "lookup": (
        "{self}翻一下记录，稍等",
        "在查了，马上",
        "找找看，等一下下",
    ),
    "admin": (
        "在处理了，稍等",
        "{self}去弄一下，马上好",
    ),
    "thinking": (
        "等一下，{self}想想怎么说",
        "这个得想一下，稍等",
        "嗯…给{self}一点时间",
    ),
}
_LOOKUP_FAMILIES = frozenset({"history", "memory", "profile", "member", "group", "essence", "file", "notice"})
_ADMIN_HINT = re.compile(r"mute|kick|recall|notice|essence|card|title|admin|folder|file", re.IGNORECASE)
_ROTATION: dict[int, int] = {}
# 只保留像人话的开场白：去掉 Markdown/代码/链接，太长或像在提问的都不用。
_PREAMBLE_REJECT = re.compile(r"```|https?://|[{}<>\[\]`]|^\s*[-*#]")


@dataclass(slots=True)
class TurnProgress:
    """一个回合的进度快照；提示任务和对话循环共享同一个对象。"""

    started_at: float = field(default_factory=time.monotonic)
    phase: str = "thinking"
    tool: str | None = None
    tool_family: str | None = None
    tool_started_at: float | None = None
    preamble: str | None = None
    self_name: str = ""
    reacted: bool = False
    text_sent: bool = False
    done: bool = False

    def enter_tool(self, name: str, *, family: str | None = None) -> None:
        self.tool = name
        self.tool_family = family
        self.tool_started_at = time.monotonic()
        if name in SLOW_TOOLS or family == "sandbox":
            self.phase = "sandbox"
        elif family in _LOOKUP_FAMILIES and not _ADMIN_HINT.search(name):
            self.phase = "lookup"
        elif _ADMIN_HINT.search(name):
            self.phase = "admin"
        else:
            self.phase = "lookup"

    def leave_tool(self) -> None:
        self.tool = None
        self.tool_family = None
        self.tool_started_at = None
        self.phase = "thinking"

    @property
    def running_slow_tool(self) -> bool:
        return self.phase == "sandbox" and self.tool is not None


WAIT_NOTICE_TASK: ContextVar[asyncio.Task[None] | None] = ContextVar(
    "agent_dialogue_wait_notice", default=None
)
TURN_PROGRESS: ContextVar[TurnProgress | None] = ContextVar(
    "agent_dialogue_turn_progress", default=None
)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def wait_notice_delay() -> float:
    return _env_float("AGENT_AI_WAIT_NOTICE_DELAY", WAIT_NOTICE_DEFAULT_DELAY)


def wait_notice_long_delay() -> float:
    return max(
        _env_float("AGENT_AI_WAIT_NOTICE_LONG_DELAY", WAIT_NOTICE_LONG_DELAY),
        wait_notice_delay(),
    )


def wait_notice_mode() -> str:
    raw = os.environ.get("AGENT_AI_WAIT_NOTICE_MODE", "").strip().lower()
    return raw if raw in WAIT_NOTICE_MODES else "reaction"


def clean_preamble(text: str | None) -> str | None:
    """模型随工具调用一起给的开场白，能当进度提示用就返回清洗后的版本。"""

    value = " ".join(str(text or "").split())
    if not value or len(value) > _PREAMBLE_MAX_CHARS or _PREAMBLE_REJECT.search(value):
        return None
    if value.endswith(("?", "？")):
        return None
    return value


def progress_text(progress: TurnProgress | None, group_id: int) -> str:
    """按当前阶段挑一句提示；同一个群轮换，不连着用同一句。"""

    if progress is not None and progress.preamble:
        return progress.preamble
    phase = progress.phase if progress is not None else "thinking"
    lines = _PHASE_LINES.get(phase) or _PHASE_LINES["thinking"]
    if len(_ROTATION) >= _ROTATION_LIMIT:
        _ROTATION.clear()
    index = _ROTATION.get(group_id, -1) + 1
    _ROTATION[group_id] = index
    self_name = (progress.self_name if progress is not None else "") or "我"
    return lines[index % len(lines)].format(self=self_name)


def current_progress() -> TurnProgress | None:
    return TURN_PROGRESS.get()


def begin_progress(*, self_name: str = "") -> TurnProgress:
    progress = TurnProgress(self_name=self_name.strip()[:8])
    TURN_PROGRESS.set(progress)
    return progress


def cancel_wait_notice() -> None:
    progress = TURN_PROGRESS.get()
    if progress is not None:
        progress.done = True
    task = WAIT_NOTICE_TASK.get()
    if task is None:
        return
    WAIT_NOTICE_TASK.set(None)
    if not task.done():
        task.cancel()


async def _react(bot: Any, message_id: Any) -> bool:
    """给触发消息贴表情；任何不支持或失败都返回 False，由调用方改发文字。"""

    try:
        target = int(message_id)
    except (TypeError, ValueError):
        return False
    call_api = getattr(bot, "call_api", None)
    if target == 0 or not callable(call_api):
        return False
    try:
        from .capabilities import local_group_capabilities

        if not local_group_capabilities(bot).has("set_msg_emoji_like"):
            return False
    except Exception:  # noqa: BLE001
        return False
    try:
        await asyncio.wait_for(
            call_api("set_msg_emoji_like", message_id=target, emoji_id=WAIT_NOTICE_EMOJI_ID),  # pyright: ignore[reportArgumentType]
            timeout=5.0,
        )
    except Exception:  # noqa: BLE001
        return False
    return True


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
    """回合开始时调用；超时后先贴表情，必要时补一句话。副作用通过回调注入。"""

    delay = wait_notice_delay()
    mode = wait_notice_mode()
    if delay <= 0 or mode == "off":
        return
    progress = TURN_PROGRESS.get()
    if progress is None:
        progress = begin_progress()
    long_delay = wait_notice_long_delay()

    async def _say() -> None:
        if progress.done or progress.text_sent:
            return
        progress.text_sent = True
        await send_unless_expired(
            bot,
            group_id,
            progress_text(progress, group_id),
            enqueued_at,
            label="等待提示",
            message_id=message_id,
            cancel_wait_notice=False,
        )

    async def _notice() -> None:
        try:
            await asyncio.sleep(delay)
            if progress.done:
                return
            if mode == "reaction" and await _react(bot, message_id):
                progress.reacted = True
                debug(f"群 {group_id} 处理超过 {delay:.1f}s,已给触发消息贴表情")
                await asyncio.sleep(max(long_delay - delay, 0.0))
                if progress.done or not progress.running_slow_tool:
                    return
            await _say()
            debug(f"群 {group_id} 处理较慢,已发送一次进度提示(phase={progress.phase})")
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            debug_exception(f"群 {group_id} 等待提示发送失败(忽略)")

    WAIT_NOTICE_TASK.set(asyncio.create_task(_notice()))


__all__ = [
    "SLOW_TOOLS",
    "TURN_PROGRESS",
    "WAIT_NOTICE_DEFAULT_DELAY",
    "WAIT_NOTICE_EMOJI_ID",
    "WAIT_NOTICE_LONG_DELAY",
    "WAIT_NOTICE_TASK",
    "TurnProgress",
    "begin_progress",
    "cancel_wait_notice",
    "clean_preamble",
    "current_progress",
    "progress_text",
    "start_wait_notice",
    "wait_notice_delay",
    "wait_notice_long_delay",
    "wait_notice_mode",
]
