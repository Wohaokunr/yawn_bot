"""进程内运行指标。

P1-6 的指标故意不依赖额外的监控 SDK：游戏引擎可以在没有网络或
Prometheus 客户端的环境中继续运行，管理面板/状态适配器按需调用
``snapshot_metrics`` 或 ``render_prometheus`` 读取当前进程的聚合值。

所有公开标签都经过白名单限制，游戏 id 只用于进程内阶段计时账本，
绝不会成为指标标签。指标在进程重启时清零，不承担事件日志或回放存储职责。
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

_METRIC_NAME_RE = re.compile(r"^yawnbot_[a-z][a-z0-9_]*$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/+\-]{0,63}$")
_ALLOWED_LABEL_KEYS = frozenset(
    {
        "component",
        "ending",
        "game_kind",
        "operation",
        "outcome",
        "overflow",
        "phase",
        "reason",
        "source",
        "winner",
    }
)
_HIGH_CARDINALITY_LABEL_KEYS = frozenset(
    {
        "actor_id",
        "group_id",
        "game_id",
        "qq",
        "user_id",
    }
)

_HISTOGRAM_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
)

_METRIC_HELP = {
    "yawnbot_ai_degradations_total": "AI fallback or degradation decisions.",
    "yawnbot_ai_request_duration_seconds": "AI request latency in seconds.",
    "yawnbot_ai_requests_total": "AI requests grouped by operation and outcome.",
    "yawnbot_event_log_write_failures_total": "Event log writes that failed.",
    "yawnbot_game_endings_total": "Game endings grouped by low-cardinality outcome.",
    "yawnbot_game_phase_duration_seconds": "Completed game phase duration in seconds.",
    "yawnbot_queue_rejections_total": "Rejected action or event-log queue submissions.",
}

_Labels = tuple[tuple[str, str], ...]
_PhaseKey = tuple[str, str]


@dataclass
class _Histogram:
    """固定 bucket 的累积前计数。"""

    bucket_counts: list[int] = field(
        default_factory=lambda: [0 for _ in _HISTOGRAM_BUCKETS]
    )
    count: int = 0
    total: float = 0.0

    def observe(self, value: float) -> None:
        value = max(float(value), 0.0)
        self.count += 1
        self.total += value
        for index, bound in enumerate(_HISTOGRAM_BUCKETS):
            if value <= bound:
                self.bucket_counts[index] += 1
                return


_lock = threading.RLock()
_counters: dict[str, dict[_Labels, int]] = {}
_histograms: dict[str, dict[_Labels, _Histogram]] = {}
# key 只在内存中保存，避免把 game_id 放入公开指标标签。
_phase_starts: dict[_PhaseKey, tuple[str, float]] = {}


def _enum_value(value: object) -> object:
    return value.value if isinstance(value, Enum) else value


def _token(value: object) -> str | None:
    value = _enum_value(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _TOKEN_RE.fullmatch(value) is not None else None


def _labels(labels: Mapping[str, object] | None) -> _Labels | None:
    if labels is None:
        return ()
    normalized: list[tuple[str, str]] = []
    for key, raw_value in labels.items():
        if (
            not isinstance(key, str)
            or key in _HIGH_CARDINALITY_LABEL_KEYS
            or key not in _ALLOWED_LABEL_KEYS
        ):
            return None
        value = _token(raw_value)
        if value is None:
            return None
        normalized.append((key, value))
    return tuple(sorted(normalized))


def _metric_name(name: str) -> str | None:
    return name if _METRIC_NAME_RE.fullmatch(name) is not None else None


def _increment_locked(name: str, labels: _Labels, value: int) -> None:
    series = _counters.setdefault(name, {})
    series[labels] = series.get(labels, 0) + value


def increment_counter(
    name: str,
    *,
    labels: Mapping[str, object] | None = None,
    value: int = 1,
) -> None:
    """增加一个白名单标签的 counter；指标错误不会影响业务路径。"""

    try:
        metric_name = _metric_name(name)
        normalized = _labels(labels)
        if metric_name is None or normalized is None or value <= 0:
            return
        with _lock:
            _increment_locked(metric_name, normalized, value)
    except Exception:  # noqa: BLE001
        return


def observe_histogram(
    name: str,
    value: float,
    *,
    labels: Mapping[str, object] | None = None,
) -> None:
    """记录一个固定 bucket 的观测值。"""

    try:
        metric_name = _metric_name(name)
        normalized = _labels(labels)
        if metric_name is None or normalized is None:
            return
        numeric_value = float(value)
    except Exception:  # noqa: BLE001
        return
    try:
        with _lock:
            series = _histograms.setdefault(metric_name, {})
            histogram = series.setdefault(normalized, _Histogram())
            histogram.observe(numeric_value)
    except Exception:  # noqa: BLE001
        return


def record_queue_rejection(component: str, game_kind: str, reason: str) -> None:
    """记录动作队列或事件日志队列拒绝。"""

    increment_counter(
        "yawnbot_queue_rejections_total",
        labels={
            "component": component,
            "game_kind": game_kind,
            "reason": reason,
        },
    )


def record_event_log_write_failure(game_kind: str) -> None:
    """记录旁路事件日志写入失败。"""

    increment_counter(
        "yawnbot_event_log_write_failures_total",
        labels={"game_kind": game_kind},
    )


def record_ai_request(operation: str, outcome: str, elapsed_seconds: float) -> None:
    """记录一次共享 LLM 调用的结果和耗时。"""

    labels = {"operation": operation, "outcome": outcome}
    increment_counter("yawnbot_ai_requests_total", labels=labels)
    observe_histogram(
        "yawnbot_ai_request_duration_seconds",
        elapsed_seconds,
        labels={"operation": operation},
    )


def record_ai_degradation(component: str, reason: str) -> None:
    """记录调用方采用固定兜底或降级路径。

    ``component`` 只能是低基数的功能名（例如 ``rpg``、``werewolf``、
    ``chat``）；局 id、群号和用户号不进入标签。
    """

    increment_counter(
        "yawnbot_ai_degradations_total",
        labels={"component": component, "reason": reason},
    )


def record_game_ending(
    game_kind: str,
    *,
    outcome: str | None = None,
    ending: str | None = None,
    winner: str | None = None,
) -> None:
    """记录结局分布；ending/winner 仅接受结构化短标识符。"""

    labels: dict[str, object] = {"game_kind": game_kind}
    if outcome:
        labels["outcome"] = outcome
    if ending:
        labels["ending"] = ending
    if winner:
        labels["winner"] = winner
    increment_counter("yawnbot_game_endings_total", labels=labels)


def start_game_phase(
    game_kind: str,
    game_id: str,
    phase: object,
    *,
    now: float | None = None,
) -> None:
    """开始记录一局的当前阶段；game_id 只留在内存账本。"""

    kind = _token(game_kind)
    phase_value = _token(phase)
    if (
        kind is None
        or not isinstance(game_id, str)
        or not game_id
        or phase_value is None
    ):
        return
    timestamp = time.perf_counter() if now is None else float(now)
    with _lock:
        _phase_starts.setdefault((kind, game_id), (phase_value, timestamp))


def record_phase_change(
    game_kind: str,
    game_id: str,
    _previous_phase: object,
    new_phase: object,
    *,
    now: float | None = None,
) -> None:
    """结束旧阶段并开始新阶段；ENDED 不再创建活动账本。"""

    kind = _token(game_kind)
    new_value = _token(new_phase)
    if kind is None or not isinstance(game_id, str) or not game_id or new_value is None:
        return
    timestamp = time.perf_counter() if now is None else float(now)
    key = (kind, game_id)
    with _lock:
        current = _phase_starts.pop(key, None)
        if current is not None:
            phase_value, started = current
            _observe_phase_locked(kind, phase_value, timestamp - started)
        if new_value != "ENDED":
            _phase_starts[key] = (new_value, timestamp)


def finish_game_phase(
    game_kind: str,
    game_id: str,
    *,
    now: float | None = None,
) -> None:
    """清理未经过 ENDED 阶段切换的异常/取消对局账本。"""

    kind = _token(game_kind)
    if kind is None or not isinstance(game_id, str) or not game_id:
        return
    timestamp = time.perf_counter() if now is None else float(now)
    with _lock:
        current = _phase_starts.pop((kind, game_id), None)
        if current is not None:
            phase_value, started = current
            _observe_phase_locked(kind, phase_value, timestamp - started)


def _observe_phase_locked(game_kind: str, phase: str, elapsed: float) -> None:
    labels = {"game_kind": game_kind, "phase": phase}
    normalized = _labels(labels)
    if normalized is None:
        return
    histogram = _histograms.setdefault(
        "yawnbot_game_phase_duration_seconds", {}
    ).setdefault(normalized, _Histogram())
    histogram.observe(elapsed)


def _labels_dict(labels: _Labels) -> dict[str, str]:
    return dict(labels)


def snapshot_metrics() -> dict[str, object]:
    """返回 JSON 可序列化的聚合快照。"""

    with _lock:
        counters = [
            {
                "name": name,
                "labels": _labels_dict(labels),
                "value": value,
            }
            for name, series in sorted(_counters.items())
            for labels, value in sorted(series.items())
        ]
        histograms: list[dict[str, object]] = []
        for name, series in sorted(_histograms.items()):
            for labels, histogram in sorted(series.items()):
                cumulative = 0
                buckets: dict[str, int] = {}
                for bound, count in zip(
                    _HISTOGRAM_BUCKETS,
                    histogram.bucket_counts,
                    strict=True,
                ):
                    cumulative += count
                    buckets[_format_number(bound)] = cumulative
                buckets["+Inf"] = histogram.count
                histograms.append(
                    {
                        "name": name,
                        "labels": _labels_dict(labels),
                        "buckets": buckets,
                        "count": histogram.count,
                        "sum": histogram.total,
                    }
                )
    return {"counters": counters, "histograms": histograms}


def _format_number(value: object) -> str:
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.12g}"
    return "0"


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_labels(labels: Mapping[str, object]) -> str:
    if not labels:
        return ""
    parts = [
        f'{key}="{_escape_label(str(value))}"'
        for key, value in sorted(labels.items())
    ]
    return "{" + ",".join(parts) + "}"


def render_prometheus() -> str:
    """以 Prometheus text exposition 格式返回当前聚合指标。"""

    snapshot = snapshot_metrics()
    counters = cast("list[dict[str, object]]", snapshot["counters"])
    histograms = cast("list[dict[str, object]]", snapshot["histograms"])
    lines: list[str] = []
    declared: set[tuple[str, str]] = set()

    for item in counters:
        name = str(item["name"])
        if (name, "counter") not in declared:
            lines.extend(
                [
                    f"# HELP {name} {_METRIC_HELP.get(name, name)}",
                    f"# TYPE {name} counter",
                ]
            )
            declared.add((name, "counter"))
        labels = cast("dict[str, str]", item["labels"])
        lines.append(
            f"{name}{_format_labels(labels)} {_format_number(item['value'])}"
        )

    for item in histograms:
        name = str(item["name"])
        if (name, "histogram") not in declared:
            lines.extend(
                [
                    f"# HELP {name} {_METRIC_HELP.get(name, name)}",
                    f"# TYPE {name} histogram",
                ]
            )
            declared.add((name, "histogram"))
        labels = cast("dict[str, str]", item["labels"])
        buckets = cast("dict[str, int]", item["buckets"])
        for bound, count in buckets.items():
            bucket_labels = dict(labels)
            bucket_labels["le"] = bound
            lines.append(
                f"{name}_bucket{_format_labels(bucket_labels)} "
                f"{_format_number(count)}"
            )
        lines.append(
            f"{name}_sum{_format_labels(labels)} "
            f"{_format_number(cast('float', item['sum']))}"
        )
        lines.append(
            f"{name}_count{_format_labels(labels)} "
            f"{_format_number(item['count'])}"
        )
    return "\n".join(lines) + ("\n" if lines else "")


def reset_metrics_for_tests() -> None:
    """清空聚合值和阶段账本；只供测试隔离使用。"""

    with _lock:
        _counters.clear()
        _histograms.clear()
        _phase_starts.clear()


__all__ = [
    "finish_game_phase",
    "increment_counter",
    "observe_histogram",
    "record_ai_degradation",
    "record_ai_request",
    "record_event_log_write_failure",
    "record_game_ending",
    "record_phase_change",
    "record_queue_rejection",
    "render_prometheus",
    "reset_metrics_for_tests",
    "snapshot_metrics",
    "start_game_phase",
]
