# ruff: noqa: E501,F401,I001,TID252,PLR0912,PLR0913,PLR0917,C901,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100
"""群消息有界队列和批处理。

每个 (bot, group) 一条 FIFO 队列和一个 worker。worker 取到队首后可以调用
可选的 ``coalesce`` 钩子，把队列里同一成员的后续触发一起吸收成一个回合
（见 ``turn_intake.coalesce_same_actor``）；不同成员之间仍严格按序处理。
"""

from __future__ import annotations

import asyncio
import time
import weakref
from collections import deque
from typing import Any, Awaitable, Callable

from nonebot import logger

from .log import dbg, dbg_exc

MAX_QUEUE_PER_GROUP = 64
DEBOUNCE_SECONDS = 0.8
PENDING_TRIGGER_TTL_SECONDS = 120.0
_MAX_TRACKED_GROUPS = 512
QueueKey = tuple[int, int]
QueueItem = tuple[Any, Any, Any, float]
Coalescer = Callable[
    [QueueItem, Callable[[Callable[[QueueItem], bool], int], list[QueueItem]]],
    QueueItem,
]


class TriggerQueue:
    """有界 FIFO；相比 asyncio.Queue 额外支持按条件摘取待处理项。"""

    def __init__(self, maxsize: int) -> None:
        self.maxsize = maxsize
        self._items: deque[QueueItem] = deque()
        self._ready = asyncio.Event()

    def qsize(self) -> int:
        return len(self._items)

    def empty(self) -> bool:
        return not self._items

    def put_nowait(self, item: QueueItem) -> None:
        if len(self._items) >= self.maxsize:
            raise asyncio.QueueFull
        self._items.append(item)
        self._ready.set()

    async def get(self) -> QueueItem:
        while not self._items:
            self._ready.clear()
            await self._ready.wait()
        return self._items.popleft()

    def take_matching(
        self, predicate: Callable[[QueueItem], bool], limit: int
    ) -> list[QueueItem]:
        """按原顺序摘出最多 ``limit`` 个满足条件的项，其余保持不动。"""

        if limit <= 0:
            return []
        taken: list[QueueItem] = []
        kept: deque[QueueItem] = deque()
        for item in self._items:
            if len(taken) < limit and predicate(item):
                taken.append(item)
            else:
                kept.append(item)
        self._items = kept
        return taken

    def task_done(self) -> None:
        """兼容 asyncio.Queue 接口；本队列不跟踪 join。"""


_queues: dict[QueueKey, TriggerQueue] = {}
_workers: dict[QueueKey, asyncio.Task[None]] = {}
_locks: weakref.WeakValueDictionary[QueueKey, asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


def _prune_idle() -> None:
    """长期运行的多群部署下，回收无 worker 且队列为空的群条目。

    只裁剪到阈值就停：一次清空全部空闲群会把仍在活跃时段、只是此刻队列为空的
    群一并丢掉，下一条消息又要重建条目。``_locks`` 是 WeakValueDictionary，
    没有强引用时自行回收，因此这里只处理 ``_queues`` / ``_workers``。
    """

    excess = len(_queues) - _MAX_TRACKED_GROUPS
    if excess <= 0:
        return
    for key in list(_queues):
        if excess <= 0:
            break
        worker = _workers.get(key)
        queue = _queues.get(key)
        lock = _locks.get(key)
        if (
            queue is not None
            and queue.empty()
            and (worker is None or worker.done())
            and (lock is None or not lock.locked())
        ):
            _queues.pop(key, None)
            _workers.pop(key, None)
            excess -= 1


def _key(
    group_id: int, bot_id: int | None = None, item: tuple[Any, Any, Any] | None = None
) -> QueueKey:
    if bot_id is None and item is not None:
        bot_id = int(getattr(item[0], "self_id", 0) or 0)
    return (int(bot_id or 0), int(group_id))


def _queue(
    group_id: int, bot_id: int | None = None, item: tuple[Any, Any, Any] | None = None
) -> TriggerQueue:
    key = _key(group_id, bot_id, item)
    queue = _queues.get(key)
    if queue is None:
        _prune_idle()
        queue = TriggerQueue(MAX_QUEUE_PER_GROUP)
        _queues[key] = queue
    return queue


def group_lock(group_id: int, bot_id: int | None = None) -> asyncio.Lock:
    key = _key(group_id, bot_id)
    lock = _locks.get(key)
    if lock is None:
        _prune_idle()
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def enqueue(
    group_id: int, item: tuple[Any, Any, Any], bot_id: int | None = None
) -> bool:
    try:
        queue = _queue(group_id, bot_id, item)
        queue.put_nowait((*item, time.monotonic()))
    except asyncio.QueueFull:
        dbg(f"群 {group_id} 入队失败: 队列已满(上限 {MAX_QUEUE_PER_GROUP})")
        return False
    dbg(f"群 {group_id} 入队成功,当前队列长度={queue.qsize()}")
    return True


def queue_size(group_id: int, bot_id: int | None = None) -> int:
    return _queue(group_id, bot_id).qsize()


def pending_trigger_age(enqueued_at: float) -> float:
    """返回触发项从入队到现在的单调时钟年龄。"""

    return max(0.0, time.monotonic() - enqueued_at)


def is_pending_trigger_expired(enqueued_at: float) -> bool:
    """判断触发项是否已经等待超过允许的排队时效。"""

    return pending_trigger_age(enqueued_at) > PENDING_TRIGGER_TTL_SECONDS


def ensure_worker(
    group_id: int,
    process: Callable[..., Awaitable[None]],
    bot_id: int | None = None,
    *,
    coalesce: Coalescer | None = None,
) -> asyncio.Task[None]:
    key = _key(group_id, bot_id)
    task = _workers.get(key)
    if task is not None and not task.done():
        dbg(f"群 {group_id} worker 已存在,复用(key={key})")
        return task

    async def run() -> None:
        queue = _queue(group_id, bot_id)
        try:
            while True:
                bot, event, normalized, enqueued_at = await asyncio.wait_for(
                    queue.get(), timeout=300
                )
                try:
                    if is_pending_trigger_expired(enqueued_at):
                        dbg(
                            f"群 {group_id} worker 丢弃过期触发: "
                            f"message_id={getattr(event, 'message_id', None)} "
                            f"等待 {pending_trigger_age(enqueued_at):.1f}s"
                        )
                        continue
                    # 保留短暂防抖，但不再丢弃防抖窗口内的有效消息；
                    # 这样队列按入队顺序处理，只有超过 TTL 的项目会被跳过。
                    await asyncio.sleep(DEBOUNCE_SECONDS)
                    if is_pending_trigger_expired(enqueued_at):
                        dbg(
                            f"群 {group_id} worker 防抖后丢弃过期触发: "
                            f"message_id={getattr(event, 'message_id', None)} "
                            f"等待 {pending_trigger_age(enqueued_at):.1f}s"
                        )
                        continue
                    if coalesce is not None:
                        try:
                            bot, event, normalized, enqueued_at = coalesce(
                                (bot, event, normalized, enqueued_at),
                                queue.take_matching,
                            )
                        except Exception:  # noqa: BLE001
                            dbg_exc(f"群 {group_id} 触发合批失败,按单条处理")
                    dbg(
                        f"群 {group_id} worker 按序处理消息: "
                        f"message_id={getattr(event, 'message_id', None)} "
                        f"等待 {pending_trigger_age(enqueued_at):.1f}s"
                    )
                    await process(
                        bot,
                        event,
                        normalized,
                        enqueued_at=enqueued_at,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("群聊 Agent 处理消息失败")
                    dbg_exc(f"群 {group_id} worker 处理消息异常(见上方堆栈)")
                finally:
                    queue.task_done()
        except asyncio.TimeoutError:
            dbg(f"群 {group_id} worker 空闲 300s,退出并注销")
            return
        finally:
            if _workers.get(key) is asyncio.current_task():
                _workers.pop(key, None)
                dbg(f"群 {group_id} worker 已从注册表移除(key={key})")

    task = asyncio.create_task(run())
    _workers[key] = task
    dbg(f"群 {group_id} 新建 worker 任务(key={key})")
    return task


def reset_for_tests() -> None:
    for task in _workers.values():
        task.cancel()
    _workers.clear()
    _queues.clear()
    _locks.clear()


__all__ = [
    "DEBOUNCE_SECONDS",
    "MAX_QUEUE_PER_GROUP",
    "PENDING_TRIGGER_TTL_SECONDS",
    "TriggerQueue",
    "enqueue",
    "ensure_worker",
    "group_lock",
    "is_pending_trigger_expired",
    "pending_trigger_age",
    "queue_size",
    "reset_for_tests",
]
