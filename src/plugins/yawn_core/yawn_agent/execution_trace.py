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
from pathlib import PurePath
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from .context import now_beijing

_MAX_EVENTS = 96
_MAX_GROUP_TRACES = 12
_MAX_TRACKED_GROUPS = 512
_MAX_STRING = 600
_MAX_LIST = 24
_MAX_DICT = 32
_SUMMARY_PREVIEW_CHARS = 80
_TOOL_LABEL_PREFIXES = ("工具意图 ", "工具 ")
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


def _redacted_metadata(name: str, value: Any) -> dict[str, Any]:
    """Hide sensitive values while retaining non-secret diagnostics.

    Trace is an admin-only, in-memory diagnostic surface.  Replacing every URL/path/file
    with one opaque marker made failures impossible to diagnose, so sensitive fields now
    keep only structural metadata (host, suffix, payload keys, length).  Full values are
    still never retained in the trace buffer.
    """

    lowered = name.lower()
    if lowered == "url" or lowered.endswith("_url"):
        text = str(value or "").strip()
        parts = urlsplit(text)
        query_keys = sorted({key for key, _value in parse_qsl(parts.query, keep_blank_values=True)})
        suffix = PurePath(parts.path).suffix.lower()[:16] if parts.path else ""
        return {
            "redacted": True,
            "kind": "url",
            "scheme": parts.scheme[:12] or None,
            "host": (parts.hostname or "")[:160] or None,
            "suffix": suffix or None,
            "has_query": bool(parts.query),
            "query_keys": query_keys[:12],
        }
    if lowered in {"path", "cache_path"} or lowered.endswith("_path"):
        text = str(value or "").strip()
        suffix = PurePath(text.replace("\\", "/")).suffix.lower()[:16]
        return {
            "redacted": True,
            "kind": "path",
            "platform": "windows" if re.match(r"^[A-Za-z]:[\\/]", text) else "posix",
            "absolute": bool(re.match(r"^[A-Za-z]:[\\/]", text) or text.startswith("/")),
            "suffix": suffix or None,
            "chars": len(text),
        }
    if lowered in {"raw_message", "raw_payload", "payload_raw"}:
        keys = [str(key)[:80] for key in value] if isinstance(value, dict) else []
        return {
            "redacted": True,
            "kind": "payload",
            "value_type": type(value).__name__,
            "key_count": len(keys),
            "keys": keys[:16],
        }
    if lowered in {"file", "file_ref"}:
        text = str(value or "")
        suffix = PurePath(text.replace("\\", "/")).suffix.lower()[:16]
        return {
            "redacted": True,
            "kind": "file_ref",
            "value_type": type(value).__name__,
            "suffix": suffix or None,
            "chars": len(text),
        }
    text = str(value or "")
    return {
        "redacted": True,
        "kind": "secret",
        "present": bool(text),
        "chars": len(text),
    }


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

    def as_summary(self) -> dict[str, Any]:
        """Return the small representation used by the trace collection API."""
        statuses = {event.status for event in self.events}
        phases = {event.phase for event in self.events}
        has_outbound_problem = any(
            event.phase == "outbound"
            and (
                event.status in {"failed", "degraded", "unknown"}
                or str(event.output.get("delivery_state") or "")
                in {"confirmed_failure", "unknown"}
            )
            for event in self.events
        )
        has_failure = (
            self.status == "failed"
            or self.outcome == "error"
            or "failed" in statuses
        )
        has_degradation = (
            self.status == "degraded"
            or self.outcome in {"degraded", "delivery_unknown"}
            or bool(statuses & {"degraded", "unknown"})
        )
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
            "eventCount": len(self.events),
            "hasFailure": has_failure,
            "hasDegradation": has_degradation,
            "hasTool": "tool" in phases,
            "hasMedia": "media" in phases,
            "hasOutboundProblem": has_outbound_problem,
            "preview": self._preview(),
            "toolNames": self._tool_names(),
            "llmRounds": sum(1 for event in self.events if event.phase == "llm"),
            "slowestPhase": self._slowest_event(),
            "truncated": len(self.events) >= _MAX_EVENTS,
        }

    def _preview(self) -> str | None:
        """触发消息的短预览：只取 intake 事件里已脱敏的 text_preview。"""
        for event in self.events:
            if event.phase != "intake":
                continue
            text = event.output.get("text_preview")
            if isinstance(text, str) and text.strip():
                text = text.strip()
                if len(text) > _SUMMARY_PREVIEW_CHARS:
                    return text[:_SUMMARY_PREVIEW_CHARS] + "…"
                return text
        return None

    def _tool_names(self) -> list[str]:
        names: list[str] = []
        for event in self.events:
            if event.phase != "tool":
                continue
            name = event.label
            for prefix in _TOOL_LABEL_PREFIXES:
                if name.startswith(prefix):
                    name = name[len(prefix) :]
                    break
            if name and name not in names:
                names.append(name)
        return names[:_MAX_LIST]

    def _slowest_event(self) -> dict[str, Any] | None:
        timed = [event for event in self.events if event.duration_ms is not None]
        if not timed:
            return None
        slowest = max(timed, key=lambda event: event.duration_ms or 0.0)
        return {
            "phase": slowest.phase,
            "label": slowest.label,
            "durationMs": slowest.duration_ms,
        }

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
                output[name] = _redacted_metadata(name, item)
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


def recent_execution_trace_summaries(
    group_id: int,
    *,
    status: str | None = None,
    mode: str | None = None,
    trigger_source: str | None = None,
    actor_user_id: int | None = None,
    source: str | None = None,
) -> list[dict[str, Any]]:
    """Return recent traces without their event payloads.

    The filters are intentionally applied in the in-memory buffer so the
    collection endpoint never needs to serialize event diagnostics just to filter
    or render the list.
    """
    normalized_status = status.strip() if status else None
    traces = list(reversed(_recent_traces.get(int(group_id), ())))
    if mode:
        traces = [trace for trace in traces if trace.mode == mode]
    if trigger_source:
        traces = [trace for trace in traces if trace.trigger_source == trigger_source]
    if actor_user_id is not None:
        traces = [trace for trace in traces if trace.actor_user_id == actor_user_id]
    if source:
        traces = [trace for trace in traces if trace.source == source]
    summaries = [trace.as_summary() for trace in traces]
    if normalized_status is None:
        return summaries
    flag_by_filter = {
        "failed": "hasFailure",
        "degraded": "hasDegradation",
        "tool": "hasTool",
        "media": "hasMedia",
        "outbound": "hasOutboundProblem",
    }
    flag = flag_by_filter.get(normalized_status)
    if flag is not None:
        return [summary for summary in summaries if bool(summary.get(flag))]
    return [summary for summary in summaries if summary.get("status") == normalized_status]


def execution_trace_by_id(group_id: int, trace_id: str) -> dict[str, Any] | None:
    for trace in _recent_traces.get(int(group_id), ()):
        if trace.trace_id == trace_id:
            return trace.as_dict()
    return None


def clear_execution_traces(group_id: int) -> int:
    """清空一个群的 Trace 缓冲，返回被丢弃的条数。"""
    bucket = _recent_traces.pop(int(group_id), None)
    return len(bucket) if bucket is not None else 0


def _percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(ratio * (len(ordered) - 1))))
    return round(ordered[index], 1)


def _count(values: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def execution_trace_stats(group_id: int) -> dict[str, Any]:
    """汇总当前缓冲内 Trace 的状态分布、耗时分位与阶段耗时。

    只读取内存缓冲，和列表接口一样不触碰数据库，也不返回事件原文。
    """
    traces = list(_recent_traces.get(int(group_id), ()))
    summaries = [trace.as_summary() for trace in traces]
    durations = [
        float(trace.duration_ms) for trace in traces if trace.duration_ms is not None
    ]
    phase_totals: dict[str, list[float]] = {}
    phase_failures: dict[str, int] = {}
    tool_counts: dict[str, int] = {}
    for trace in traces:
        for event in trace.events:
            if event.duration_ms is not None:
                phase_totals.setdefault(event.phase, []).append(float(event.duration_ms))
            if event.status == "failed":
                phase_failures[event.phase] = phase_failures.get(event.phase, 0) + 1
        for name in trace._tool_names():
            tool_counts[name] = tool_counts.get(name, 0) + 1
    phases = [
        {
            "phase": phase,
            "count": len(values),
            "avgMs": round(sum(values) / len(values), 1),
            "maxMs": round(max(values), 1),
            "totalMs": round(sum(values), 1),
            "failures": phase_failures.get(phase, 0),
        }
        for phase, values in phase_totals.items()
    ]
    for phase, failures in phase_failures.items():
        if phase not in phase_totals:
            phases.append(
                {
                    "phase": phase,
                    "count": 0,
                    "avgMs": None,
                    "maxMs": None,
                    "totalMs": 0.0,
                    "failures": failures,
                }
            )
    phases.sort(key=lambda item: -(item["totalMs"] or 0.0))
    return {
        "groupId": str(group_id),
        "capacity": _MAX_GROUP_TRACES,
        "total": len(summaries),
        "failed": sum(1 for item in summaries if item["hasFailure"]),
        "degraded": sum(1 for item in summaries if item["hasDegradation"]),
        "withTools": sum(1 for item in summaries if item["hasTool"]),
        "withMedia": sum(1 for item in summaries if item["hasMedia"]),
        "outboundProblems": sum(1 for item in summaries if item["hasOutboundProblem"]),
        "running": sum(1 for item in summaries if item["status"] == "running"),
        "durationMs": {
            "avg": round(sum(durations) / len(durations), 1) if durations else None,
            "p50": _percentile(durations, 0.5),
            "p95": _percentile(durations, 0.95),
            "max": round(max(durations), 1) if durations else None,
        },
        "byMode": _count([str(item["mode"]) for item in summaries]),
        "byTrigger": _count(
            [str(item["triggerSource"]) for item in summaries if item["triggerSource"]]
        ),
        "byOutcome": _count(
            [str(item["outcome"]) for item in summaries if item["outcome"]]
        ),
        "phases": phases,
        "tools": [
            {"name": name, "count": count}
            for name, count in sorted(tool_counts.items(), key=lambda item: (-item[1], item[0]))
        ],
        "oldestAt": min((item["startedAt"] for item in summaries), default=None),
        "newestAt": max((item["startedAt"] for item in summaries), default=None),
    }


__all__ = [
    "ExecutionTrace",
    "begin_execution_trace",
    "bind_execution_trace",
    "clear_execution_traces",
    "current_execution_trace",
    "execution_trace_by_id",
    "execution_trace_stats",
    "finish_execution_trace",
    "recent_execution_trace_summaries",
    "recent_execution_traces",
    "reset_execution_trace",
    "safe_summary",
    "trace_event",
]
