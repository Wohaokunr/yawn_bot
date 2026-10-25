# ruff: noqa: E501,PLR0913,PLR0917,C901,PLR0912,PLR0915,PERF203
"""按节奏把几条消息依次发出去：第一条马上发，后面每条前停一会儿“打字”。

发送本身仍然只走调用方给的 ``send_one``（对话路径是带过期检查的
``send_unless_expired``，主动/续聊是 ``send_prepared_outbound``），本模块只管：

- 按 ``speech_rhythm`` 算好的停顿依次发送，总时长有硬上限；
- 发送期间群里又有人在排队找 Bot（或调用方说“该收尾了”）时，不再慢慢打字，
  把剩下的几条合成一条立刻发完——信息不丢，也不让下一位干等；
- 任何一条确认失败或回执不确定就停下，剩下的不再发，临时文件一并清理；
- 返回逐条结果，供调用方逐条落库（每条都有自己的 message_id，撤回、引用都对得上）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from nonebot.adapters.onebot.v11 import Message, MessageSegment

from .execution_trace import trace_event
from .log import dbg, dbg_exc
from .outbound import (
    DELIVERY_CONFIRMED_FAILURE,
    DELIVERY_UNKNOWN,
    PreparedOutboundMessage,
    PreparedSpeechParts,
    SendResult,
)
from .speech_rhythm import rhythm_settings

SendOne = Callable[[PreparedOutboundMessage, int], Awaitable[SendResult]]
HurryCheck = Callable[[], bool]
Sleep = Callable[[float], Awaitable[Any]]

STOP_SEND_FAILED = "send_failed"
STOP_DELIVERY_UNKNOWN = "delivery_unknown"
STOP_EXCEPTION = "exception"
HURRY_QUEUE = "queued_trigger"
HURRY_SPREAD = "spread_limit"


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    """一次分条发送的结果；``results`` 与真正尝试过的消息一一对应。"""

    results: tuple[SendResult, ...]
    planned: int
    hurried: str | None = None
    flushed: int = 0
    stopped: str | None = None
    waited_seconds: float = 0.0
    part_texts: tuple[str, ...] = field(default=())

    @property
    def first(self) -> SendResult | None:
        return self.results[0] if self.results else None

    @property
    def ends_turn(self) -> bool:
        first = self.first
        return bool(first is not None and first.ends_turn)

    @property
    def delivered(self) -> tuple[SendResult, ...]:
        return tuple(item for item in self.results if item.sent)

    @property
    def visible_text(self) -> str:
        """所有可能已送达的条目拼起来，作为“这一轮 Bot 说了什么”。"""

        return "\n".join(
            item.normalized_text
            for item in self.results
            if item.may_have_delivered and item.normalized_text
        )

    def combined(self) -> SendResult:
        """给只认一条 SendResult 的老调用方：首条的投递状态 + 全部文本。"""

        first = self.first
        if first is None:
            return SendResult(
                sent=False,
                message_id=None,
                normalized_text="",
                segment_types=(),
                outcome="not_sent",
                delivery_state=DELIVERY_CONFIRMED_FAILURE,
            )
        if len(self.results) == 1:
            return first
        delivered = [item for item in self.results if item.may_have_delivered]
        return replace(
            first,
            normalized_text=self.visible_text,
            segment_types=tuple(
                kind for item in delivered for kind in item.segment_types
            ),
            segments=tuple(seg for item in delivered for seg in item.segments),
            media_refs=tuple(ref for item in delivered for ref in item.media_refs),
            outcome=first.outcome if self.stopped is None else "partial",
        )

    def trace_payload(self) -> dict[str, Any]:
        return {
            "planned": self.planned,
            "attempted": len(self.results),
            "delivered": len(self.delivered),
            "hurried": self.hurried,
            "flushed": self.flushed,
            "stopped": self.stopped,
            "waited_seconds": round(self.waited_seconds, 3),
            "message_ids": [item.message_id for item in self.results],
        }


def merge_prepared(parts: Sequence[PreparedOutboundMessage]) -> PreparedOutboundMessage:
    """把还没发的几条合成一条；中间用换行隔开，引用只保留在最前面。"""

    if len(parts) == 1:
        return parts[0]
    message = Message()
    records: list[dict[str, Any]] = []
    texts: list[str] = []
    for index, part in enumerate(parts):
        if index > 0:
            message += MessageSegment.text("\n")
            records.append({"type": "text", "data": {"text": "\n"}, "text": "\n", "children": [], "depth": 0})
        for segment in part.message:
            if index > 0 and segment.type == "reply":
                continue
            message += segment
        records.extend(
            record
            for record in part.segment_records
            if index == 0 or record.get("type") != "reply"
        )
        if part.normalized_text:
            texts.append(part.normalized_text)
    head = parts[0]
    return PreparedOutboundMessage(
        message=message,
        normalized_text="\n".join(texts),
        segment_records=tuple(records),
        reply_chain=head.reply_chain,
        media_refs=tuple(ref for part in parts for ref in part.media_refs),
        temporary_files=tuple(path for part in parts for path in part.temporary_files),
        speech_scene=head.speech_scene,
        quality_issues=tuple(
            dict.fromkeys(code for part in parts for code in part.quality_issues)
        ),
    )


def discard_prepared(parts: Sequence[PreparedOutboundMessage]) -> None:
    """不再发送的条目：删掉它们的临时文件（沙箱导出等），不留垃圾。"""

    for part in parts:
        for path in part.temporary_files:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                dbg_exc(f"删除未发送分条的临时文件失败(忽略): {path.name}")


async def deliver_parts(
    prepared: PreparedSpeechParts | Sequence[PreparedOutboundMessage],
    *,
    send_one: SendOne,
    delays: Sequence[float] | None = None,
    hurry: HurryCheck | None = None,
    sleep: Sleep = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    max_spread_seconds: float | None = None,
    group_id: int | None = None,
) -> DeliveryOutcome:
    """依次发送；第一条的异常原样抛出（调用方原有的失败处理不变）。"""

    if isinstance(prepared, PreparedSpeechParts):
        messages = list(prepared.messages)
        planned_delays = list(delays if delays is not None else prepared.delays)
    else:
        messages = list(prepared)
        planned_delays = list(delays or ())
    if len(planned_delays) != len(messages):
        planned_delays = [0.0 for _ in messages]
    spread_cap = (
        rhythm_settings().max_spread_seconds
        if max_spread_seconds is None
        else max(0.0, max_spread_seconds)
    )
    results: list[SendResult] = []
    texts: list[str] = []
    hurried: str | None = None
    flushed = 0
    stopped: str | None = None
    waited = 0.0
    index = 0
    while index < len(messages):
        current = messages[index]
        if index > 0:
            reason = _hurry_reason(hurry)
            delay = max(0.0, float(planned_delays[index]))
            if reason is None and waited + delay > spread_cap + 1e-6:
                reason = HURRY_SPREAD
            if reason is None and delay > 0:
                started = clock()
                await sleep(delay)
                waited += max(0.0, clock() - started) or delay
                reason = _hurry_reason(hurry)
            if reason is not None:
                hurried = reason
                rest = messages[index:]
                flushed = len(rest) if len(rest) > 1 else 0
                current = merge_prepared(rest)
                messages = [*messages[:index], current]
        try:
            result = await send_one(current, index)
        except Exception:
            if index == 0:
                discard_prepared(messages[1:])
                raise
            dbg_exc(f"群 {group_id} 分条第 {index + 1} 条发送异常,停止后续")
            stopped = STOP_EXCEPTION
            discard_prepared(messages[index + 1 :])
            break
        results.append(result)
        texts.append(str(getattr(current, "normalized_text", "") or ""))
        if not result.ends_turn:
            stopped = STOP_SEND_FAILED
            discard_prepared(messages[index + 1 :])
            break
        if result.delivery_state == DELIVERY_UNKNOWN and index < len(messages) - 1:
            # 回执不确定说明连接在抖：别再接着刷，宁可少说半句也不重复。
            stopped = STOP_DELIVERY_UNKNOWN
            discard_prepared(messages[index + 1 :])
            break
        index += 1
    outcome = DeliveryOutcome(
        results=tuple(results),
        planned=len(prepared.messages) if isinstance(prepared, PreparedSpeechParts) else len(messages),
        hurried=hurried,
        flushed=flushed,
        stopped=stopped,
        waited_seconds=waited,
        part_texts=tuple(texts),
    )
    if outcome.planned > 1:
        trace_event(
            "outbound",
            "分条发送",
            status=(
                "success"
                if stopped is None
                else ("degraded" if outcome.ends_turn else "failed")
            ),
            output=outcome.trace_payload(),
            detail=_detail(outcome),
        )
        dbg(f"群 {group_id} 分条发送: {outcome.trace_payload()}")
    return outcome


def _hurry_reason(hurry: HurryCheck | None) -> str | None:
    if hurry is None:
        return None
    try:
        return HURRY_QUEUE if hurry() else None
    except Exception:  # noqa: BLE001
        dbg_exc("分条发送的收尾检查失败(按不催处理)")
        return None


def _detail(outcome: DeliveryOutcome) -> str:
    if outcome.stopped == STOP_SEND_FAILED:
        return f"第 {len(outcome.results)} 条发送失败，后面的不再发"
    if outcome.stopped == STOP_DELIVERY_UNKNOWN:
        return "有一条回执不确定，为避免重复刷屏停止后续分条"
    if outcome.stopped == STOP_EXCEPTION:
        return "分条发送中途异常，已停止后续"
    if outcome.hurried == HURRY_QUEUE:
        return "群里有人在排队找 Bot，剩下的分条合成一条立刻发完"
    if outcome.hurried == HURRY_SPREAD:
        return "分条停顿超过总时长上限，剩下的合成一条发完"
    return f"按节奏分 {outcome.planned} 条发完"


async def persist_delivered(
    outcome: DeliveryOutcome,
    persist: Callable[..., Awaitable[None]],
    session: Any,
    bot_id: int,
    group_id: int,
    retention_days: int,
) -> int:
    """逐条落库 Bot 自己的发言；返回落库条数。投递未知的条目不写历史。"""

    count = 0
    for result in outcome.results:
        if not result.sent:
            continue
        await persist(
            session,
            bot_id,
            group_id,
            result.message_id,
            result.normalized_text,
            retention_days,
            segments=result.segments,
            reply_chain=result.reply_chain,
            forward_tree=result.forward_tree,
            media_refs=result.media_refs,
        )
        count += 1
    return count


async def send_outbound_parts(
    bot: Any,
    group_id: int,
    raw_segments: Any,
    *,
    rhythm: object = None,
    session: Any,
    actor_user_id: int | None = None,
    source: str = "agent",
    sleep: Sleep = asyncio.sleep,
) -> DeliveryOutcome:
    """send_message 工具的分条版本：text 里有 ||| 或指定了节奏时依次发多条。

    没有分条时完全走原来的 ``send_outbound_message``（同一套校验、审计和降级）。
    分条准备失败也退回原路径，由它给出校验错误和审计。
    """

    from .capabilities import get_segment_capabilities
    from .outbound import (
        prepare_speech_parts,
        send_outbound_message,
        send_prepared_outbound,
    )
    from .speech import speech_plan_from_segments
    from .speech_rhythm import (
        RHYTHM_AUTO,
        RHYTHM_BLOCK,
        normalize_rhythm,
        strip_break_segments,
    )

    declared = normalize_rhythm(rhythm)
    plan = None
    if isinstance(raw_segments, list) and raw_segments and all(
        isinstance(item, dict) for item in raw_segments
    ):
        plan = speech_plan_from_segments(raw_segments, scene="tool_result", rhythm=declared)
    wants_parts = plan is not None and (
        plan.part_count > 1 or plan.rhythm not in {RHYTHM_AUTO, RHYTHM_BLOCK}
    )
    if plan is None or not wants_parts:
        segments = (
            strip_break_segments(plan.segments) if plan is not None else raw_segments
        )
        single = await send_outbound_message(
            bot,
            group_id,
            segments,
            session=session,
            actor_user_id=actor_user_id,
            source=source,
        )
        return DeliveryOutcome(results=(single,), planned=1, part_texts=(single.normalized_text,))
    caps = get_segment_capabilities(bot, group_id)
    try:
        prepared = await prepare_speech_parts(
            plan,
            session=session,
            group_id=group_id,
            actor_user_id=actor_user_id,
            allowed_segment_types=caps.allowed,
            speech_autofix=False,
        )
    except (ValueError, PermissionError, LookupError):
        single = await send_outbound_message(
            bot,
            group_id,
            strip_break_segments(plan.segments),
            session=session,
            actor_user_id=actor_user_id,
            source=source,
        )
        return DeliveryOutcome(results=(single,), planned=1, part_texts=(single.normalized_text,))

    async def send_one(part: PreparedOutboundMessage, _index: int) -> SendResult:
        return await send_prepared_outbound(
            bot,
            group_id,
            part,
            session=session,
            actor_user_id=actor_user_id,
            source=source,
        )

    return await deliver_parts(
        prepared,
        send_one=send_one,
        hurry=queued_trigger_check(group_id, int(str(getattr(bot, "self_id", "") or 0))),
        sleep=sleep,
        group_id=group_id,
    )


def queued_trigger_check(group_id: int, bot_id: int | None) -> HurryCheck:
    """群队列里有新的触发在等 → 该收尾了。"""

    from .collector import queue_size

    def check() -> bool:
        return queue_size(group_id, bot_id) > 0

    return check


__all__ = [
    "HURRY_QUEUE",
    "HURRY_SPREAD",
    "STOP_DELIVERY_UNKNOWN",
    "STOP_EXCEPTION",
    "STOP_SEND_FAILED",
    "DeliveryOutcome",
    "deliver_parts",
    "discard_prepared",
    "merge_prepared",
    "persist_delivered",
    "queued_trigger_check",
    "send_outbound_parts",
]
