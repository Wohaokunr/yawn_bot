# ruff: noqa: A002,BLE001,E501,PLR0913,PLR2004,TC003,TRY300
"""Bounded, redacted execution traces for YawnAgent debugging.

The trace buffer is intentionally in-memory and diagnostic-only.  It must never become
an alternate long-lived copy of chat/media payloads.
"""

from __future__ import annotations

import contextvars
import re
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .context import now_beijing

_MAX_EVENTS = 96
_MAX_GROUP_TRACES = 12
_MAX_TRACKED_GROUPS = 512
_MAX_STRING = 600
_MAX_LIST = 24
_MAX_DICT = 32
_REDACT_KEYS = {
    "url",
    "path",
    "file",
    "api_key",
    "authorization",
    "raw_message",
    "raw_payload",
    "payload_raw",
}
_URL_RE = re.compile(r"https?://[^\s\]\[\)\(\}\{<>\"']+", re.IGNORECASE)
_WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:[\\/][^\s\]\[\)\(\}\{<>\"']+")
_POSIX_PATH_RE = re.compile(r"(?<![\w:])/(?:[^/\s]+/)+[^\s\]\[\)\(\}\{<>\"']+")


@dataclass(slots=True)
class TraceEvent:
    id: str
    phase: str
    label: str
    status: str
    offset_ms: float
    duration_ms: float | None = None
    input: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    detail: str | None = None
    round: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "phase": self.phase,
            "label": self.label,
            "status": self.status,
            "offsetMs": self.offset_ms,
            "durationMs": self.duration_ms,
            "input": self.input,
            "output": self.output,
            "detail": self.detail,
            "round": self.round,
        }


@dataclass(slots=True)
class ExecutionTrace:
    trace_id: str
    group_id: int
    mode: str
    source: str
    trigger_source: str | None
    actor_user_id: int | None
    message_id: int | None
    started_at: datetime
    started_monotonic: float
    status: str = "running"
    outcome: str | None = None
    duration_ms: float | None = None
    events: list[TraceEvent] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "traceId": self.trace_id,
            "groupId": str(self.group_id),
            "mode": self.mode,
            "source": self.source,
            "triggerSource": self.trigger_source,
            "actorUserId": (
                str(self.actor_user_id) if self.actor_user_id is not None else None
            ),
            "messageId": str(self.message_id) if self.message_id is not None else None,
            "startedAt": self.started_at.isoformat(),
            "status": self.status,
            "outcome": self.outcome,
            "durationMs": self.duration_ms,
            "events": [event.as_dict() for event in self.events],
        }


_current_trace: contextvars.ContextVar[ExecutionTrace | None] = contextvars.ContextVar(
    "yawn_agent_execution_trace", default=None
)
_recent_traces: OrderedDict[int, deque[ExecutionTrace]] = OrderedDict()


def _trace_bucket(group_id: int) -> deque[ExecutionTrace]:
    group = int(group_id)
    bucket = _recent_traces.get(group)
    if bucket is None:
        bucket = deque(maxlen=_MAX_GROUP_TRACES)
        _recent_traces[group] = bucket
    _recent_traces.move_to_end(group)
    while len(_recent_traces) > _MAX_TRACKED_GROUPS:
        _recent_traces.popitem(last=False)
    return bucket


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 4:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        text = value.replace("\x00", "").strip()
        text = _URL_RE.sub("[redacted-url]", text)
        text = _WINDOWS_PATH_RE.sub("[redacted-path]", text)
        text = _POSIX_PATH_RE.sub("[redacted-path]", text)
        return text[:_MAX_STRING] + ("…" if len(text) > _MAX_STRING else "")
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_DICT:
                output["_truncated"] = True
                break
            name = str(key)[:80]
            lowered = name.lower()
            if (
                lowered in _REDACT_KEYS
                or lowered.endswith(("_url", "_path"))
                or lowered in {"cache_path", "file_ref"}
            ):
                output[name] = "[redacted]"
            else:
                output[name] = _safe_value(item, depth=depth + 1)
        return output
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        list_output = [
            _safe_value(item, depth=depth + 1) for item in items[:_MAX_LIST]
        ]
        if len(items) > _MAX_LIST:
            list_output.append("[truncated]")
        return list_output
    return _safe_value(str(value), depth=depth + 1)


def safe_summary(value: Any) -> dict[str, Any]:
    safe = _safe_value(value)
    return safe if isinstance(safe, dict) else {"value": safe}


def begin_execution_trace(
    group_id: int,
    *,
    mode: str,
    source: str,
    trigger_source: str | None = None,
    actor_user_id: int | None = None,
    message_id: int | None = None,
) -> ExecutionTrace:
    return ExecutionTrace(
        trace_id=uuid.uuid4().hex,
        group_id=int(group_id),
        mode=mode,
        source=source,
        trigger_source=trigger_source,
        actor_user_id=actor_user_id,
        message_id=message_id,
        started_at=now_beijing(),
        started_monotonic=time.monotonic(),
    )


def bind_execution_trace(
    trace: ExecutionTrace,
) -> contextvars.Token[ExecutionTrace | None]:
    return _current_trace.set(trace)


def reset_execution_trace(token: contextvars.Token[ExecutionTrace | None]) -> None:
    _current_trace.reset(token)


def current_execution_trace() -> ExecutionTrace | None:
    return _current_trace.get()


def trace_event(
    phase: str,
    label: str,
    *,
    status: str = "success",
    input: dict[str, Any] | None = None,
    output: dict[str, Any] | None = None,
    detail: str | None = None,
    duration_ms: float | None = None,
    round_index: int | None = None,
    trace: ExecutionTrace | None = None,
) -> TraceEvent | None:
    try:
        active = trace or current_execution_trace()
        if active is None or len(active.events) >= _MAX_EVENTS:
            return None
        event = TraceEvent(
            id=f"e{len(active.events) + 1}",
            phase=str(phase)[:40],
            label=str(label)[:120],
            status=str(status)[:24],
            offset_ms=round(
                max(time.monotonic() - active.started_monotonic, 0.0) * 1000,
                1,
            ),
            duration_ms=(
                round(max(float(duration_ms), 0.0), 1)
                if duration_ms is not None
                else None
            ),
            input=safe_summary(input or {}),
            output=safe_summary(output or {}),
            detail=(_safe_value(detail) if detail else None),
            round=round_index,
        )
        active.events.append(event)
        return event
    except Exception:
        # 追踪是纯诊断旁路；任何意外值/序列化问题都不得改变 Agent 主流程。
        return None


def finish_execution_trace(
    trace: ExecutionTrace,
    *,
    outcome: str,
    status: str | None = None,
    store: bool = True,
) -> ExecutionTrace:
    trace.status = status or ("failed" if outcome == "error" else "completed")
    trace.outcome = outcome
    trace.duration_ms = round(
        max(time.monotonic() - trace.started_monotonic, 0.0) * 1000,
        1,
    )
    if store:
        bucket = _trace_bucket(trace.group_id)
        if not bucket or bucket[-1] is not trace:
            bucket.append(trace)
    return trace


def recent_execution_traces(group_id: int) -> list[dict[str, Any]]:
    return [trace.as_dict() for trace in reversed(_recent_traces.get(int(group_id), ()))]


def clear_execution_traces(group_id: int) -> None:
    _recent_traces.pop(int(group_id), None)


__all__ = [
    "ExecutionTrace",
    "begin_execution_trace",
    "bind_execution_trace",
    "clear_execution_traces",
    "current_execution_trace",
    "finish_execution_trace",
    "recent_execution_traces",
    "reset_execution_trace",
    "safe_summary",
    "trace_event",
]
