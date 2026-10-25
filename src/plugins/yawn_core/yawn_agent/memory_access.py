# ruff: noqa: TID252, TC003
"""记忆召回计数：读路径只记进程内缓冲，整理任务批量落库。

每轮对话都会注入十几条记忆，若在读路径逐条 UPDATE，会把只读的上下文加载
变成写事务并与整理任务争锁。这里用有界内存缓冲累计"被用到"的次数，由
定时整理 tick 一次性写回，并据此做两件事：

- 排序时常被召回的记忆衰减更慢（见 ``memory.rank_memories``）；
- 自动记忆被召回时顺延过期时间（"用进废退"），冷门记忆照常按 TTL 遗忘。

缓冲丢失（进程重启）只会少算几次召回，不影响正确性。
"""

from __future__ import annotations

import math
import threading
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from ..data_models.agent_memory import AgentMemory

# 缓冲上限：超过后丢弃新 id（已有 id 仍可累加），防止异常热路径撑爆内存。
MAX_PENDING_IDS = 5_000
# 单次 flush 最多写回的行数；剩余留给下一次 tick。
FLUSH_BATCH = 500
# 召回续期：自动记忆被用到时，过期时间至少顺延到 now + 该天数，
# 但不超过类型保留期的两倍，避免"热门"记忆永不过期。
# 共同经历（bond）被想起来一次就多留两个月，最多留一年。
ACCESS_RENEW_DAYS: dict[str, int] = {"summary": 14, "profile": 30, "bond": 60}
ACCESS_RENEW_CAP_DAYS: dict[str, int] = {"summary": 60, "profile": 180, "bond": 365}

_lock = threading.Lock()
_pending: dict[int, int] = {}
_pending_at: dict[int, datetime] = {}


def record_access(memory_ids: Iterable[int], *, now: datetime) -> int:
    """登记一批被注入/检索命中的记忆 id；返回新登记的条数。"""

    added = 0
    with _lock:
        for raw in memory_ids:
            try:
                memory_id = int(raw)
            except (TypeError, ValueError):
                continue
            if memory_id <= 0:
                continue
            if memory_id not in _pending:
                if len(_pending) >= MAX_PENDING_IDS:
                    continue
                _pending[memory_id] = 0
                added += 1
            _pending[memory_id] += 1
            _pending_at[memory_id] = now
    return added


def pending_access_count() -> int:
    with _lock:
        return len(_pending)


def _take_batch() -> list[tuple[int, int, datetime]]:
    with _lock:
        ids = list(_pending)[:FLUSH_BATCH]
        return [
            (memory_id, _pending.pop(memory_id), _pending_at.pop(memory_id))
            for memory_id in ids
        ]


def _restore(batch: list[tuple[int, int, datetime]]) -> None:
    with _lock:
        for memory_id, count, seen_at in batch:
            if memory_id in _pending or len(_pending) < MAX_PENDING_IDS:
                _pending[memory_id] = _pending.get(memory_id, 0) + count
                _pending_at[memory_id] = max(
                    _pending_at.get(memory_id, seen_at), seen_at
                )


def renewed_expiry(
    memory_type: str,
    created_at: datetime | None,
    expires_at: datetime | None,
    now: datetime,
) -> datetime | None:
    """被召回的自动记忆新的过期时间；不需要顺延时返回原值。"""

    if expires_at is None:
        return None
    renew_days = ACCESS_RENEW_DAYS.get(memory_type)
    cap_days = ACCESS_RENEW_CAP_DAYS.get(memory_type)
    if renew_days is None or cap_days is None:
        return expires_at
    target = now + timedelta(days=renew_days)
    cap = (created_at or now) + timedelta(days=cap_days)
    return min(max(expires_at, target), max(cap, expires_at))


def access_half_life_factor(access_count: int | None) -> float:
    """常被召回的记忆排序衰减更慢：半衰期乘数 1 ~ 2。"""

    count = max(0, int(access_count or 0))
    return 1.0 + min(1.0, math.log1p(count) / math.log1p(50))


async def flush_access(session: Any, *, now: datetime) -> int:
    """把缓冲中的召回计数写回数据库；返回更新的行数。调用方负责 commit。"""

    batch = _take_batch()
    if not batch:
        return 0
    by_id = {memory_id: (count, seen_at) for memory_id, count, seen_at in batch}
    try:
        rows = list(
            (
                await session.execute(
                    select(AgentMemory).where(AgentMemory.id.in_(list(by_id)))
                )
            )
            .scalars()
            .all()
        )
    except Exception:
        _restore(batch)
        raise
    for row in rows:
        count, seen_at = by_id[int(row.id)]
        row.access_count = int(row.access_count or 0) + count
        row.last_accessed_at = seen_at
        if row.source_kind == "auto":
            row.expires_at = renewed_expiry(
                str(row.memory_type or ""), row.created_at, row.expires_at, now
            )
    return len(rows)


def reset_access_buffer() -> None:
    """测试辅助：清空缓冲。"""

    with _lock:
        _pending.clear()
        _pending_at.clear()


__all__ = [
    "access_half_life_factor",
    "flush_access",
    "pending_access_count",
    "record_access",
    "renewed_expiry",
    "reset_access_buffer",
]
