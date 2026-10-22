"""群消息入口的回合整形：同一成员连发合批、只喊一声时等待后续正文。

真实群聊里经常出现两种节奏：

- 先单独发一句 ``@机器人`` 或唤醒词，隔几秒才打出真正的问题；
- 一个问题拆成两三条消息连发。

旧实现把每条明确触发都当成独立回合排队处理，前者会先回一句“怎么了”
再错过真正的问题，后者会对同一个人连回好几次。本模块只负责把这些
消息整理成“一人一回合”，不做授权，也不调用模型。全部状态都在进程内
且有硬上限，不落库。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from .log import dbg, dbg_exc
from .message_parser import MAX_MEDIA_REFS, NormalizedMessage, SegmentNode

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

CALL_CONTINUATION_SECONDS = 6.0
COALESCE_WINDOW_SECONDS = 20.0
MAX_COALESCED_MESSAGES = 4
_MAX_HELD_CALLS = 256
_BARE_CALL_TRIGGERS = frozenset({"mention", "wake_word"})
_PUNCTUATION_ONLY = re.compile(r"[\s\W_]+")

HeldKey = tuple[int, int, int]
TurnItem = tuple[Any, Any, Any]


def _event_user_id(event: Any) -> int | None:
    getter = getattr(event, "get_user_id", None)
    if not callable(getter):
        return None
    try:
        return int(str(getter()))
    except (TypeError, ValueError):
        return None


def _event_message_id(event: Any) -> int | None:
    raw = getattr(event, "message_id", None)
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value or None


def is_bare_call(normalized: NormalizedMessage, *, wake_words: Iterable[str]) -> bool:
    """只有 @/唤醒词、没有任何正文或附件的明确触发。"""

    if normalized.trigger_source not in _BARE_CALL_TRIGGERS:
        return False
    if normalized.media_refs or normalized.reply_chain or normalized.forward_tree:
        return False
    text = " ".join(normalized.intent_text().lower().split())
    for word in sorted({w for w in wake_words if w}, key=len, reverse=True):
        text = text.replace(word.lower(), "")
    return not _PUNCTUATION_ONLY.sub("", text)


def merge_normalized(messages: Sequence[NormalizedMessage]) -> NormalizedMessage:
    """把同一成员按时间顺序的几条消息合成一个当前回合。

    正文按原顺序换行拼接；引用链取最后一条带引用的消息（最近的指向最
    准确）；媒体保持顺序并受同一数量上限约束。触发来源取第一条明确触发，
    触发信号取并集。
    """

    if not messages:
        raise ValueError("至少需要一条消息")
    if len(messages) == 1:
        return messages[0]
    segments: list[SegmentNode] = []
    for index, item in enumerate(messages):
        if index and segments and item.segments:
            segments.append(SegmentNode(type="text", data={"text": "\n"}, text="\n"))
        segments.extend(item.segments)
    media_refs: list[dict[str, Any]] = []
    for item in messages:
        media_refs.extend(item.media_refs)
    reply_chain: list[dict[str, Any]] = []
    for item in messages:
        if item.reply_chain:
            reply_chain = list(item.reply_chain)
    signals: dict[str, bool] = {}
    for item in messages:
        for key, value in item.trigger_signals.items():
            signals[key] = bool(signals.get(key) or value)
    last = messages[-1]
    return replace(
        last,
        plain_text="\n".join(item.plain_text for item in messages if item.plain_text),
        segments=segments,
        media_refs=media_refs[:MAX_MEDIA_REFS],
        reply_chain=reply_chain,
        forward_tree=[node for item in messages for node in item.forward_tree],
        mentions=list(
            dict.fromkeys(user_id for item in messages for user_id in item.mentions)
        ),
        truncated=any(item.truncated for item in messages),
        parse_trace=list(last.parse_trace),
        trigger_source=next(
            (item.trigger_source for item in messages if item.trigger_source),
            None,
        ),
        trigger_signals=signals,
        merged_message_ids=list(
            dict.fromkeys(
                message_id
                for item in messages
                for message_id in item.merged_message_ids
            )
        ),
    )


def merge_turn_items(items: Sequence[TurnItem]) -> TurnItem:
    """合并 (bot, event, normalized)；事件取最后一条，便于引用最新消息。"""

    if len(items) == 1:
        return items[0]
    bot, event, _ = items[-1]
    merged = merge_normalized([item[2] for item in items])
    last_id = _event_message_id(event)
    merged.merged_message_ids = [
        message_id
        for message_id in dict.fromkeys(
            [
                *(_event_message_id(item[1]) for item in items[:-1]),
                *merged.merged_message_ids,
            ]
        )
        if message_id is not None and message_id != last_id
    ]
    return bot, event, merged


def coalesce_same_actor(
    head: tuple[Any, ...],
    take_matching: Callable[[Callable[[tuple[Any, ...]], bool], int], list[Any]],
) -> tuple[Any, ...]:
    """worker 取到队首后，把队列里同一成员、时间相近的触发一起吸收。

    只合并同一说话人：工具权限、隐私和配额都以单个 actor 为准，不同成员
    仍然按 FIFO 各自成回合。``take_matching`` 由 collector 提供，会把命中的
    项从队列里摘掉并按原顺序返回。
    """

    bot, event, normalized, enqueued_at = head
    actor = _event_user_id(event)
    if actor is None or not isinstance(normalized, NormalizedMessage):
        return head

    def _same_actor(item: tuple[Any, ...]) -> bool:
        return (
            _event_user_id(item[1]) == actor
            and isinstance(item[2], NormalizedMessage)
            and abs(float(item[3]) - float(enqueued_at)) <= COALESCE_WINDOW_SECONDS
        )

    extra = take_matching(_same_actor, MAX_COALESCED_MESSAGES - 1)
    if not extra:
        return head
    merged = merge_turn_items([(bot, event, normalized), *(item[:3] for item in extra)])
    dbg(
        f"群 {getattr(event, 'group_id', '?')} 同一成员 {actor} 连发 "
        f"{len(extra) + 1} 条触发,合并为一个回合"
    )
    # 合并后的回合以最新一条为准计算排队时效。
    return (*merged, max(float(enqueued_at), *(float(item[3]) for item in extra)))


@dataclass(slots=True)
class _HeldCall:
    item: TurnItem
    task: asyncio.Task[None] | None = None


_held_calls: dict[HeldKey, _HeldCall] = {}


def _cancel(held: _HeldCall) -> None:
    task = held.task
    if task is not None and task is not asyncio.current_task() and not task.done():
        task.cancel()


def hold_bare_call(
    key: HeldKey,
    item: TurnItem,
    release: Callable[[TurnItem], None],
    *,
    delay: float | None = None,
) -> None:
    """暂存一次“只喊一声”，等待同一成员几秒内补上正文。

    超时没有补充时照常把原触发交给 ``release``，保证喊了一定有回应。
    """

    previous = _held_calls.pop(key, None)
    if previous is not None:
        _cancel(previous)
    if len(_held_calls) >= _MAX_HELD_CALLS:
        # 极端情况下（大量群同时喊）不再暂存，直接交给普通队列。
        release(item)
        return
    held = _HeldCall(item=item)
    wait = CALL_CONTINUATION_SECONDS if delay is None else max(float(delay), 0.0)

    async def _expire() -> None:
        try:
            await asyncio.sleep(wait)
        except asyncio.CancelledError:
            return
        if _held_calls.get(key) is not held:
            return
        _held_calls.pop(key, None)
        dbg(f"群 {key[1]} 成员 {key[2]} 只喊了一声且 {wait:.0f}s 内没有补充,照常回应")
        try:
            release(held.item)
        except Exception:  # noqa: BLE001
            dbg_exc(f"群 {key[1]} 释放暂存的呼叫失败")

    held.task = asyncio.create_task(_expire())
    _held_calls[key] = held
    dbg(f"群 {key[1]} 成员 {key[2]} 只喊了一声,等待 {wait:.0f}s 内的后续正文")


def take_bare_call(key: HeldKey) -> TurnItem | None:
    """同一成员补充正文时取回暂存的呼叫，并取消超时回应。"""

    held = _held_calls.pop(key, None)
    if held is None:
        return None
    _cancel(held)
    return held.item


def drop_bare_call(key: HeldKey) -> bool:
    held = _held_calls.pop(key, None)
    if held is None:
        return False
    _cancel(held)
    return True


def continue_bare_call(held: TurnItem, followup: TurnItem) -> TurnItem:
    """把暂存的呼叫和补充的正文合成一个明确触发回合。"""

    held_normalized = held[2]
    followup_normalized = followup[2]
    followup_normalized.trigger_source = held_normalized.trigger_source
    followup_normalized.trigger_signals = {
        **held_normalized.trigger_signals,
        "continuation": True,
    }
    return merge_turn_items([held, followup])


def held_call_count() -> int:
    return len(_held_calls)


def reset_for_tests() -> None:
    for held in _held_calls.values():
        _cancel(held)
    _held_calls.clear()


__all__ = [
    "CALL_CONTINUATION_SECONDS",
    "COALESCE_WINDOW_SECONDS",
    "MAX_COALESCED_MESSAGES",
    "coalesce_same_actor",
    "continue_bare_call",
    "drop_bare_call",
    "held_call_count",
    "hold_bare_call",
    "is_bare_call",
    "merge_normalized",
    "merge_turn_items",
    "reset_for_tests",
    "take_bare_call",
]
