# ruff: noqa: C901,PLR0912,PLR0913
"""短期群聊会话与消息合批。

状态只在进程内保留：它用于把一次主动/明确回复延伸成有界的自然对话，
不是需要恢复的业务数据。消息先等待静默窗口，持续刷屏时由硬期限兜底。
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .context import now_beijing
from .log import dbg, dbg_exc

if TYPE_CHECKING:
    from datetime import datetime

CONVERSATION_QUIET_SECONDS = 20.0
CONVERSATION_MAX_BATCH_SECONDS = 45.0
CONVERSATION_MAX_SECONDS = 12 * 60.0
CONVERSATION_MAX_BOT_TURNS = 4
CONVERSATION_MAX_EVALUATIONS = 6
CONVERSATION_MAX_CONSECUTIVE_WAITS = 3

ConversationKey = tuple[int, int]


@dataclass(frozen=True, slots=True)
class ConversationBatch:
    key: ConversationKey
    session_id: int
    topic: str | None
    bot_turns: int
    user_ids: tuple[int, ...]
    message_ids: tuple[int, ...]
    cutoff_at: datetime


@dataclass(slots=True)
class ConversationSession:
    session_id: int
    started_at: float
    last_bot_at: float
    bot_turns: int
    topic: str | None
    max_bot_turns: int = CONVERSATION_MAX_BOT_TURNS
    evaluation_count: int = 0
    consecutive_waits: int = 0
    batch_first_at: float | None = None
    batch_last_at: float | None = None
    batch_cutoff_at: datetime | None = None
    batch_user_ids: list[int] = field(default_factory=list)
    batch_message_ids: list[int] = field(default_factory=list)
    wakeup: asyncio.Event = field(default_factory=asyncio.Event)
    evaluating: bool = False


FollowupHandler = Callable[[ConversationBatch], Awaitable[None]]

_sessions: dict[ConversationKey, ConversationSession] = {}
_tasks: dict[ConversationKey, asyncio.Task[None]] = {}
_handler_box: list[FollowupHandler | None] = [None]
_session_ids = itertools.count(1)


def set_followup_handler(handler: FollowupHandler) -> None:
    _handler_box[0] = handler


def _expired(session: ConversationSession, now: float) -> bool:
    return (
        now - session.started_at >= CONVERSATION_MAX_SECONDS
        or session.bot_turns >= min(
            max(int(session.max_bot_turns), 1), CONVERSATION_MAX_BOT_TURNS
        )
    )


def _clear_batch(session: ConversationSession) -> None:
    session.batch_first_at = None
    session.batch_last_at = None
    session.batch_cutoff_at = None
    session.batch_user_ids.clear()
    session.batch_message_ids.clear()
    session.wakeup.clear()


def _cancel_waiting_task(key: ConversationKey) -> None:
    task = _tasks.get(key)
    session = _sessions.get(key)
    if (
        task is not None
        and task is not asyncio.current_task()
        and not task.done()
        and not (session and session.evaluating)
    ):
        task.cancel()


def close_conversation(bot_id: int, group_id: int, *, reason: str) -> None:
    key = (int(bot_id), int(group_id))
    session = _sessions.pop(key, None)
    _cancel_waiting_task(key)
    if session is not None:
        dbg(
            f"群 {group_id} 短会话关闭: session={session.session_id} "
            f"turns={session.bot_turns} reason={reason}"
        )


def mark_bot_reply(
    bot_id: int,
    group_id: int,
    *,
    topic: str | None,
    source: str,
    preserve_pending: bool = False,
    max_bot_turns: int = CONVERSATION_MAX_BOT_TURNS,
    now: float | None = None,
) -> ConversationSession:
    """成功发送一条 Bot 正文后开启或推进短会话。"""

    key = (int(bot_id), int(group_id))
    current = float(time.monotonic() if now is None else now)
    session = _sessions.get(key)
    bounded_max_turns = min(max(int(max_bot_turns), 1), CONVERSATION_MAX_BOT_TURNS)
    if session is None or _expired(session, current):
        session = ConversationSession(
            session_id=next(_session_ids),
            started_at=current,
            last_bot_at=current,
            bot_turns=1,
            topic=(topic or "").strip()[:240] or None,
            max_bot_turns=bounded_max_turns,
        )
        _sessions[key] = session
        dbg(
            f"群 {group_id} 短会话开启: session={session.session_id} "
            f"source={source} topic={session.topic!r}"
        )
    else:
        session.last_bot_at = current
        session.max_bot_turns = bounded_max_turns
        session.bot_turns += 1
        if topic and topic.strip():
            session.topic = topic.strip()[:240]
        dbg(
            f"群 {group_id} 短会话推进: session={session.session_id} "
            f"turn={session.bot_turns}/{session.max_bot_turns} source={source}"
        )
    if not preserve_pending:
        _clear_batch(session)
        _cancel_waiting_task(key)
    if session.bot_turns >= session.max_bot_turns:
        close_conversation(bot_id, group_id, reason="达到单话题发言上限")
    return session


def batch_due_at(session: ConversationSession) -> float | None:
    if session.batch_first_at is None or session.batch_last_at is None:
        return None
    return min(
        session.batch_last_at + CONVERSATION_QUIET_SECONDS,
        session.batch_first_at + CONVERSATION_MAX_BATCH_SECONDS,
    )


def observe_member_message(
    bot_id: int,
    group_id: int,
    *,
    user_id: int,
    message_id: int | None,
    explicit_trigger: bool,
    observed_at: datetime | None = None,
    now: float | None = None,
) -> bool:
    """把免 @ 的后续消息加入当前会话；返回是否成功加入合批。"""

    key = (int(bot_id), int(group_id))
    current = float(time.monotonic() if now is None else now)
    session = _sessions.get(key)
    if session is None:
        return False
    if _expired(session, current):
        close_conversation(bot_id, group_id, reason="会话超时或达到上限")
        return False
    if explicit_trigger:
        # 明确互动由原 FIFO 对话路径接管；尚未入选的自动批次被正文上下文吸收。
        if not session.evaluating:
            _clear_batch(session)
            _cancel_waiting_task(key)
        dbg(
            f"群 {group_id} 短会话收到明确互动,交由普通对话队列: "
            f"session={session.session_id}"
        )
        return False

    if session.batch_first_at is None:
        session.batch_first_at = current
    session.batch_last_at = current
    session.batch_cutoff_at = observed_at or now_beijing()
    member = int(user_id)
    if member in session.batch_user_ids:
        session.batch_user_ids.remove(member)
    session.batch_user_ids.append(member)
    if message_id is not None:
        session.batch_message_ids.append(int(message_id))
        session.batch_message_ids[:] = session.batch_message_ids[-64:]
    session.wakeup.set()
    _ensure_batch_task(key)
    dbg(
        f"群 {group_id} 短会话消息合批: session={session.session_id} "
        f"成员={len(session.batch_user_ids)} 消息={len(session.batch_message_ids)}"
    )
    return True


def _snapshot_batch(
    key: ConversationKey, session: ConversationSession
) -> ConversationBatch | None:
    if session.batch_first_at is None or session.batch_cutoff_at is None:
        return None
    batch = ConversationBatch(
        key=key,
        session_id=session.session_id,
        topic=session.topic,
        bot_turns=session.bot_turns,
        user_ids=tuple(reversed(session.batch_user_ids[-4:])),
        message_ids=tuple(session.batch_message_ids),
        cutoff_at=session.batch_cutoff_at,
    )
    _clear_batch(session)
    session.evaluating = True
    return batch


async def _run_batch(key: ConversationKey) -> None:
    try:
        while True:
            session = _sessions.get(key)
            if session is None:
                return
            due_at = batch_due_at(session)
            if due_at is None:
                return
            delay = max(due_at - time.monotonic(), 0.0)
            session.wakeup.clear()
            try:
                await asyncio.wait_for(session.wakeup.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            else:
                continue
            session = _sessions.get(key)
            if session is None:
                return
            batch = _snapshot_batch(key, session)
            if batch is None:
                return
            handler = _handler_box[0]
            if handler is None:
                dbg(f"群 {key[1]} 短会话缺少 followup handler,关闭")
                close_conversation(key[0], key[1], reason="处理器未注册")
                return
            dbg(
                f"群 {key[1]} 短会话合批到期: session={batch.session_id} "
                f"turn={batch.bot_turns} messages={len(batch.message_ids)}"
            )
            await handler(batch)
            return
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        dbg_exc(f"群 {key[1]} 短会话合批任务异常")
    finally:
        session = _sessions.get(key)
        if session is not None:
            session.evaluating = False
        if _tasks.get(key) is asyncio.current_task():
            _tasks.pop(key, None)
        if session is not None and session.batch_first_at is not None:
            _ensure_batch_task(key)


def _ensure_batch_task(key: ConversationKey) -> None:
    task = _tasks.get(key)
    if task is not None and not task.done():
        return
    task = asyncio.create_task(_run_batch(key))
    _tasks[key] = task


def conversation_is_current(batch: ConversationBatch) -> bool:
    session = _sessions.get(batch.key)
    return bool(
        session
        and session.session_id == batch.session_id
        and not _expired(session, time.monotonic())
    )


def begin_followup_evaluation(batch: ConversationBatch) -> bool:
    """占用一次续聊评估配额；配额耗尽时关闭会话。"""

    session = _sessions.get(batch.key)
    if (
        session is None
        or session.session_id != batch.session_id
        or _expired(session, time.monotonic())
    ):
        return False
    if session.evaluation_count >= CONVERSATION_MAX_EVALUATIONS:
        close_conversation(*batch.key, reason="达到续聊评估上限")
        return False
    session.evaluation_count += 1
    dbg(
        f"群 {batch.key[1]} 短会话评估: session={batch.session_id} "
        f"evaluation={session.evaluation_count}/{CONVERSATION_MAX_EVALUATIONS}"
    )
    return True


def finish_followup_evaluation(batch: ConversationBatch, action: str) -> None:
    """记录续聊决策，并在连续等待或总评估到限时结束会话。"""

    session = _sessions.get(batch.key)
    if session is None or session.session_id != batch.session_id:
        return
    if action == "speak":
        session.consecutive_waits = 0
    elif action == "wait":
        session.consecutive_waits += 1
    else:
        close_conversation(*batch.key, reason=f"续聊决策 {action}")
        return
    if session.consecutive_waits >= CONVERSATION_MAX_CONSECUTIVE_WAITS:
        close_conversation(*batch.key, reason="连续等待达到上限")
    elif session.evaluation_count >= CONVERSATION_MAX_EVALUATIONS:
        close_conversation(*batch.key, reason="达到续聊评估上限")


def current_conversation(bot_id: int, group_id: int) -> ConversationSession | None:
    return _sessions.get((int(bot_id), int(group_id)))


def close_group_conversations(group_id: int, *, reason: str) -> int:
    """关闭指定群当前进程内的全部短会话（可能对应多个 Bot）。"""

    target_group_id = int(group_id)
    keys = [key for key in _sessions if key[1] == target_group_id]
    for bot_id, current_group_id in keys:
        close_conversation(bot_id, current_group_id, reason=reason)
    return len(keys)


def prune_expired_conversations(*, now: float | None = None) -> int:
    current = float(time.monotonic() if now is None else now)
    expired = [key for key, session in _sessions.items() if _expired(session, current)]
    for bot_id, group_id in expired:
        close_conversation(bot_id, group_id, reason="定时清理过期会话")
    return len(expired)


async def shutdown_conversations() -> None:
    tasks = [task for task in _tasks.values() if not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _tasks.clear()
    _sessions.clear()


def reset_for_tests() -> None:
    for task in _tasks.values():
        task.cancel()
    _tasks.clear()
    _sessions.clear()


__all__ = [
    "CONVERSATION_MAX_BATCH_SECONDS",
    "CONVERSATION_MAX_BOT_TURNS",
    "CONVERSATION_MAX_CONSECUTIVE_WAITS",
    "CONVERSATION_MAX_EVALUATIONS",
    "CONVERSATION_MAX_SECONDS",
    "CONVERSATION_QUIET_SECONDS",
    "ConversationBatch",
    "ConversationSession",
    "batch_due_at",
    "begin_followup_evaluation",
    "close_conversation",
    "close_group_conversations",
    "conversation_is_current",
    "current_conversation",
    "finish_followup_evaluation",
    "mark_bot_reply",
    "observe_member_message",
    "prune_expired_conversations",
    "reset_for_tests",
    "set_followup_handler",
    "shutdown_conversations",
]
