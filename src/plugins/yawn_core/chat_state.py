"""对话模式的内存状态管理模块。"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .metrics import record_queue_rejection

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from nonebot.adapters.onebot.v11 import Bot, MessageEvent


@dataclass
class UserChatState:
    """单个用户的对话模式状态。"""

    in_mode: bool = True
    # 队列项：(bot, event, override_text)
    # override_text 非 None 时覆盖事件纯文本（命令事件含命令前缀）
    queue: asyncio.Queue[tuple[Any, Any, str | None]] = field(
        default_factory=lambda: asyncio.Queue(maxsize=_CHAT_QUEUE_MAX)
    )
    worker: asyncio.Task[None] | None = None


_states: dict[int, UserChatState] = {}
_WORKER_IDLE_TIMEOUT = 600  # 10 分钟无消息自动退出
_CHAT_QUEUE_MAX = 8


def enter_mode(user_id: int) -> UserChatState:
    """进入对话模式，返回用户状态。"""
    state = _states.get(user_id)
    if state is not None and state.in_mode:
        return state
    state = UserChatState()
    _states[user_id] = state
    return state


def _drain_queue(state: UserChatState) -> None:
    """非阻塞清空队列。"""
    while not state.queue.empty():
        try:
            state.queue.get_nowait()
            state.queue.task_done()
        except asyncio.QueueEmpty:  # noqa: PERF203
            break


def exit_mode(user_id: int) -> bool:
    """退出对话模式，清空队列并取消 worker。成功返回 True。"""
    state = _states.get(user_id)
    if state is None or not state.in_mode:
        return False
    state.in_mode = False
    _drain_queue(state)
    # 先摘除引用再取消：worker 的 finally 不会误清状态
    task = state.worker
    state.worker = None
    if task is not None and not task.done():
        task.cancel()
    return True


def is_in_mode(user_id: int) -> bool:
    """检查用户是否处于对话模式。"""
    state = _states.get(user_id)
    return state is not None and state.in_mode


def get_state(user_id: int) -> UserChatState | None:
    """获取用户状态，不存在则返回 None。"""
    return _states.get(user_id)


def enqueue(
    state: UserChatState,
    item: tuple[Any, Any, str | None],
) -> bool:
    """非阻塞投递对话消息，队列满时让调用方返回背压提示。"""
    try:
        state.queue.put_nowait(item)
    except asyncio.QueueFull:
        record_queue_rejection("chat_queue", "chat", "queue_full")
        return False
    user_id = _event_user(item[1])
    if user_id is not None:
        _arrivals[user_id] = _arrivals.get(user_id, 0) + 1
    return True


# 每位用户收到过多少条排队消息（只增不减）。私聊 Agent 用它判断“我刚才在想的时候，
# 他是不是又发了新消息”：前后两个读数不同就说明有新消息。
_arrivals: dict[int, int] = {}


def arrival_count(user_id: int) -> int:
    return _arrivals.get(int(user_id), 0)


def pending_messages(user_id: int) -> int:
    """队列里还没处理的普通消息条数（不含命令消息）。"""

    state = _states.get(int(user_id))
    if state is None:
        return 0
    items = list(getattr(state.queue, "_queue", ()))
    return sum(1 for item in items if len(item) > 2 and item[2] is None)  # noqa: PLR2004


def ensure_worker(
    user_id: int,
    process_fn: Callable[[Bot, MessageEvent, int, str | None], Awaitable[None]],
) -> asyncio.Task[None] | None:
    """确保 worker 任务存在，返回 task；用户不在模式中返回 None。"""
    state = _states.get(user_id)
    if state is None:
        return None
    if state.worker is not None and not state.worker.done():
        return state.worker
    task = asyncio.create_task(_worker_loop(user_id, process_fn))
    state.worker = task
    return task


async def stop_worker(user_id: int) -> None:
    """停止 worker 并等待在途任务结束（用于 /新对话 等需要同步的场景）。

    仅停止 worker，不改变 in_mode 与队列：
    调用方可随后 ensure_worker 让剩余消息在新上下文中继续处理。
    """
    state = _states.get(user_id)
    if state is None:
        return
    task = state.worker
    state.worker = None  # 先摘除，避免与并发的 ensure_worker 竞争
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def _merge_window_seconds() -> float:
    try:
        from .private_chat_bridge import private_agent_config

        return max(int(private_agent_config.private_agent_merge_window_ms), 0) / 1000
    except Exception:  # noqa: BLE001
        return 0.0


# 连发合并时，被并掉的前几条消息里的图片/文件段先放这里，处理这一轮时取走。
_burst_media: dict[int, list[dict[str, Any]]] = {}


def take_burst_media(user_id: int) -> list[dict[str, Any]]:
    """取走（并清空）连发合并留下的媒体段。"""

    return _burst_media.pop(int(user_id), [])


def _event_media(event: Any) -> list[dict[str, Any]]:
    try:
        from .private_chat_bridge import event_media, media_accepted

        return event_media(event) if media_accepted() else []
    except Exception:  # noqa: BLE001
        return []


def _plain(event: Any) -> str:
    try:
        return str(event.get_plaintext()).strip()
    except Exception:  # noqa: BLE001
        return ""


async def _merge_burst(state: UserChatState, event: Any) -> tuple[Any, str | None, int]:
    """连发合并：等一个短窗口，把队列里紧跟着的普通消息并成一轮。

    返回 (用于回复的事件, 合并后的文本或 None, 额外取出的条数)。命令消息
    （带 override_text 的）不合并，留在队列里按原顺序处理。
    """

    window = _merge_window_seconds()
    if window <= 0:
        return event, None, 0
    await asyncio.sleep(window)
    texts = [_plain(event)]
    latest = event
    taken = 0
    earlier_media: list[dict[str, Any]] = []
    while not state.queue.empty() and taken < _CHAT_QUEUE_MAX:
        _bot, nxt, override = state.queue._queue[0]  # type: ignore[attr-defined]
        if override is not None:
            break
        state.queue.get_nowait()
        taken += 1
        # 被并掉的上一条里的图片/文件不能丢：留给这一轮取走。
        earlier_media.extend(_event_media(latest))
        latest = nxt
        text = _plain(nxt)
        if text:
            texts.append(text)
    if not taken:
        return event, None, 0
    user_id = _event_user(latest)
    if earlier_media and user_id is not None:
        _burst_media[user_id] = earlier_media
    return latest, "\n".join(t for t in texts if t), taken


def _event_user(event: Any) -> int | None:
    try:
        return int(event.get_user_id())
    except Exception:  # noqa: BLE001
        return None


async def _worker_loop(  # noqa: C901
    user_id: int,
    process_fn: Callable[[Bot, MessageEvent, int, str | None], Awaitable[None]],
) -> None:
    """后台 worker：串行消费消息队列并调用 AI 处理。"""
    from nonebot import logger

    state = _states.get(user_id)
    if state is None:
        return

    logger.info(f"对话 worker 启动: 用户 {user_id}")

    try:
        while True:
            try:
                item = await asyncio.wait_for(
                    state.queue.get(),
                    timeout=_WORKER_IDLE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                # 空闲超时，自动退出
                state.in_mode = False
                logger.info(f"对话 worker 空闲超时，自动退出: 用户 {user_id}")
                break

            # 用户已退出对话模式
            if not state.in_mode:
                state.queue.task_done()
                _drain_queue(state)
                break

            bot_inst, event, override_text = item
            merged = 0
            if override_text is None:
                event, override_text, merged = await _merge_burst(state, event)
            try:
                await process_fn(bot_inst, event, user_id, override_text)
            except Exception as e:  # noqa: BLE001
                # 带完整堆栈：只记 repr 时生产日志看不出是哪一行出的错
                logger.opt(exception=e).error(
                    f"对话 worker 处理消息异常: 用户 {user_id}, 错误 {e!r}"
                )
                # 尝试通知用户
                try:
                    from nonebot.adapters.onebot.v11 import MessageSegment

                    await bot_inst.send(
                        event,
                        MessageSegment.text("处理消息时出了点问题，请再试一次~"),
                    )
                except Exception:  # noqa: BLE001
                    logger.debug(
                        f"向用户 {user_id} 发送处理失败提示时出错", exc_info=True
                    )
            finally:
                for _ in range(merged + 1):
                    state.queue.task_done()
    finally:
        # 仅当槽位仍是本任务时摘除，避免覆盖 ensure_worker 新建的任务
        if state.worker is asyncio.current_task():
            state.worker = None
        # 身份守卫：用户重新进入后的新 state 不可被垂死 worker 驱逐；
        # 仍处模式中（如 /新对话 触发的 stop_worker）则保留状态
        if _states.get(user_id) is state and not state.in_mode:
            _states.pop(user_id, None)
        logger.info(f"对话 worker 停止: 用户 {user_id}")
