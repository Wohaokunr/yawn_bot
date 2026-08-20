# ruff: noqa: E501,F401,I001,TID252,PLR0912,PLR0913,PLR0917,C901,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100
"""群消息有界队列和批处理。"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any, Awaitable, Callable

from nonebot import logger

MAX_QUEUE_PER_GROUP = 64
DEBOUNCE_SECONDS = 0.8
QueueKey = tuple[int, int]
_queues: dict[QueueKey, asyncio.Queue[tuple[Any, Any, Any]]] = {}
_workers: dict[QueueKey, asyncio.Task[None]] = {}
_locks: dict[QueueKey, asyncio.Lock] = {}


def _key(
    group_id: int, bot_id: int | None = None, item: tuple[Any, Any, Any] | None = None
) -> QueueKey:
    if bot_id is None and item is not None:
        bot_id = int(getattr(item[0], "self_id", 0) or 0)
    return (int(bot_id or 0), int(group_id))


def _queue(
    group_id: int, bot_id: int | None = None, item: tuple[Any, Any, Any] | None = None
) -> asyncio.Queue[tuple[Any, Any, Any]]:
    key = _key(group_id, bot_id, item)
    queue = _queues.get(key)
    if queue is None:
        queue = asyncio.Queue(maxsize=MAX_QUEUE_PER_GROUP)
        _queues[key] = queue
    return queue


def group_lock(group_id: int, bot_id: int | None = None) -> asyncio.Lock:
    key = _key(group_id, bot_id)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def enqueue(
    group_id: int, item: tuple[Any, Any, Any], bot_id: int | None = None
) -> bool:
    try:
        _queue(group_id, bot_id, item).put_nowait(item)
    except asyncio.QueueFull:
        return False
    return True


def queue_size(group_id: int, bot_id: int | None = None) -> int:
    return _queue(group_id, bot_id).qsize()


def ensure_worker(
    group_id: int,
    process: Callable[[Any, Any, Any], Awaitable[None]],
    bot_id: int | None = None,
) -> asyncio.Task[None]:
    key = _key(group_id, bot_id)
    task = _workers.get(key)
    if task is not None and not task.done():
        return task

    async def run() -> None:
        queue = _queue(group_id, bot_id)
        try:
            while True:
                bot, event, normalized = await asyncio.wait_for(
                    queue.get(), timeout=300
                )
                # Coalesce a short burst of mentions/replies.  All messages
                # have already been persisted, so processing the latest item
                # gives the model the complete recent context without issuing
                # one expensive generation per message.
                await asyncio.sleep(DEBOUNCE_SECONDS)
                while True:
                    try:
                        newer = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    queue.task_done()
                    bot, event, normalized = newer
                try:
                    await process(bot, event, normalized)
                except Exception:  # noqa: BLE001
                    logger.exception("群聊 Agent 处理消息失败")
                finally:
                    queue.task_done()
        except asyncio.TimeoutError:
            return
        finally:
            if _workers.get(key) is asyncio.current_task():
                _workers.pop(key, None)

    task = asyncio.create_task(run())
    _workers[key] = task
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
    "enqueue",
    "ensure_worker",
    "group_lock",
    "queue_size",
    "reset_for_tests",
]
