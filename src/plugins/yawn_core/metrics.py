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
from typing import TYPE_CHECKING, TypedDict, cast

from nonebot import logger

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
_AGENT_PHASES = frozenset({"context", "capability", "media", "prompt", "llm", "tool"})
_AGENT_CAPABILITY_PROBE_OUTCOMES = frozenset({"success", "degraded"})
_AGENT_OPERATIONS = frozenset({"dialogue", "proactive", "followup"})
_AGENT_DISCOVERY_OUTCOMES = frozenset({"called", "empty", "returned", "used"})

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
    "yawnbot_ai_tokens_total": "AI tokens reported by the configured endpoint.",
    "yawnbot_ai_request_duration_seconds": "AI request latency in seconds.",
    "yawnbot_ai_requests_total": "AI requests grouped by operation and outcome.",
    "yawnbot_event_log_write_failures_total": "Event log writes that failed.",
    "yawnbot_game_endings_total": "Game endings grouped by low-cardinality outcome.",
    "yawnbot_game_phase_duration_seconds": "Completed game phase duration in seconds.",
    "yawnbot_queue_rejections_total": "Rejected action or event-log queue submissions.",
    "yawnbot_rpg_tutorial_total": (
        "RPG tutorial steps grouped by operation and outcome."
    ),
    "yawnbot_rpg_deductions_total": "RPG deduction outcomes.",
    "yawnbot_rpg_terminations_total": "RPG non-story termination reasons.",
    "yawnbot_agent_cache_total": "Agent stable-prefix reuse and media cache signals.",
    "yawnbot_agent_outbound_total": "Agent outbound message attempts and degradations.",
    "yawnbot_agent_turns_total": "Agent turns grouped by operation and outcome.",
    "yawnbot_agent_turn_duration_seconds": "Agent turn duration in seconds.",
    "yawnbot_agent_queue_wait_seconds": "Agent queue wait duration in seconds.",
    "yawnbot_agent_phase_duration_seconds": "Agent phase duration in seconds.",
    "yawnbot_agent_capability_probes_total": (
        "Actual OneBot capability probes grouped by outcome."
    ),
    "yawnbot_agent_context_db_queries": (
        "SQL statements used to assemble one Agent context."
    ),
    "yawnbot_agent_tool_rounds": "Model/tool loop rounds observed per Agent turn.",
    "yawnbot_agent_tool_schema_chars": (
        "Serialized Tool schema characters per Agent turn."
    ),
    "yawnbot_agent_tool_selected_count": (
        "Tools selected by deterministic routing per Agent turn."
    ),
    "yawnbot_agent_tool_exposed_count": (
        "Tools actually exposed to the model per Agent turn."
    ),
    "yawnbot_agent_tool_discovery_total": (
        "discover_tools calls, non-empty results, and subsequently used "
        "discovered tools."
    ),
    "yawnbot_agent_provider_cache_tokens": (
        "Provider-reported cached and cache-miss input tokens per Agent turn."
    ),
}

_Labels = tuple[tuple[str, str], ...]
_PhaseKey = tuple[str, str]


class _AiHealthState(TypedDict):
    consecutiveFailures: int
    lastFailureOutcome: str | None


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
# AI 累计 counter 适合趋势统计，但不能直接表示“当前是否仍故障”。
# 这里额外维护每个低基数 operation 的连续失败状态：一次成功即关闭该 operation
# 的当前故障；历史失败仍完整保留在 yawnbot_ai_requests_total 中。
_AI_ACTIVE_FAILURE_OUTCOMES = frozenset(
    {"error", "timeout", "empty", "unsupported_multimodal"}
)
_ai_health: dict[str, _AiHealthState] = {}
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
        logger.debug(f"指标计数更新失败，已忽略: {name}", exc_info=True)
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
        logger.debug(f"指标观测值解析失败，已忽略: {name}", exc_info=True)
        return
    try:
        with _lock:
            series = _histograms.setdefault(metric_name, {})
            histogram = series.setdefault(normalized, _Histogram())
            histogram.observe(numeric_value)
    except Exception:  # noqa: BLE001
        logger.debug(f"指标观测值记录失败，已忽略: {name}", exc_info=True)
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


def record_rpg_tutorial(operation: str, outcome: str) -> None:
    increment_counter(
        "yawnbot_rpg_tutorial_total",
        labels={"operation": operation, "outcome": outcome},
    )


def record_rpg_deduction(outcome: str) -> None:
    increment_counter(
        "yawnbot_rpg_deductions_total",
        labels={"outcome": outcome},
    )


def record_rpg_termination(reason: str) -> None:
    increment_counter(
        "yawnbot_rpg_terminations_total",
        labels={"reason": reason},
    )


def record_ai_request(operation: str, outcome: str, elapsed_seconds: float) -> None:
    """记录一次共享 LLM 调用的结果、耗时与当前连续故障状态。"""

    labels = {"operation": operation, "outcome": outcome}
    increment_counter("yawnbot_ai_requests_total", labels=labels)
    observe_histogram(
        "yawnbot_ai_request_duration_seconds",
        elapsed_seconds,
        labels={"operation": operation},
    )
    operation_token = _token(operation)
    outcome_token = _token(outcome)
    if operation_token is None or outcome_token is None:
        return
    with _lock:
        state = _ai_health.setdefault(
            operation_token,
            {"consecutiveFailures": 0, "lastFailureOutcome": None},
        )
        if outcome_token == "success":
            state["consecutiveFailures"] = 0
            state["lastFailureOutcome"] = None
        elif outcome_token in _AI_ACTIVE_FAILURE_OUTCOMES:
            state["consecutiveFailures"] = int(state["consecutiveFailures"]) + 1
            state["lastFailureOutcome"] = outcome_token


def ai_health_snapshot() -> list[dict[str, object]]:
    """返回当前仍未被成功请求恢复的 AI 连续故障状态。

    与累计 counter 分离，避免 Overview 把数小时前已经恢复的历史错误永久显示为
    当前告警。只暴露低基数 operation/outcome，不携带请求正文或用户标识。
    """

    with _lock:
        return [
            {
                "operation": operation,
                "consecutiveFailures": int(state["consecutiveFailures"]),
                "lastFailureOutcome": state["lastFailureOutcome"],
            }
            for operation, state in sorted(_ai_health.items())
            if int(state["consecutiveFailures"]) > 0
        ]


def record_ai_tokens(operation: str, source: str, value: int) -> None:
    """记录端点实际返回的 token 用量；缺失或非正数时忽略。"""

    increment_counter(
        "yawnbot_ai_tokens_total",
        labels={"operation": operation, "source": source},
        value=value,
    )


def record_agent_turn(
    operation: str,
    outcome: str,
    elapsed_seconds: float,
    *,
    queue_wait_seconds: float | None = None,
) -> None:
    """记录 Agent 一次处理回合及可选的队列等待时间。"""

    increment_counter(
        "yawnbot_agent_turns_total",
        labels={"operation": operation, "outcome": outcome},
    )
    observe_histogram(
        "yawnbot_agent_turn_duration_seconds",
        elapsed_seconds,
        labels={"operation": operation},
    )
    if queue_wait_seconds is not None:
        observe_histogram(
            "yawnbot_agent_queue_wait_seconds",
            queue_wait_seconds,
            labels={"operation": operation},
        )


def record_agent_phase(phase: str, elapsed_seconds: float) -> None:
    """记录 Agent 固定阶段耗时，供长期 p50/p95 分析。"""

    if phase not in _AGENT_PHASES:
        return
    observe_histogram(
        "yawnbot_agent_phase_duration_seconds",
        elapsed_seconds,
        labels={"phase": phase},
    )


def record_agent_capability_probe(outcome: str) -> None:
    """只统计真实发往 OneBot 的能力探测；缓存命中不计入。"""

    if outcome not in _AGENT_CAPABILITY_PROBE_OUTCOMES:
        return
    increment_counter(
        "yawnbot_agent_capability_probes_total",
        labels={"outcome": outcome},
    )


def record_agent_context_db_queries(value: int) -> None:
    """记录一次上下文组装实际发出的 SQL 语句数量。"""

    observe_histogram("yawnbot_agent_context_db_queries", float(value))


def record_agent_tool_rounds(value: int, operation: str = "dialogue") -> None:
    """记录一次 Agent 回合实际经历的模型/Tool 循环轮数。"""

    if operation not in _AGENT_OPERATIONS or value < 0:
        return
    observe_histogram(
        "yawnbot_agent_tool_rounds",
        float(value),
        labels={"operation": operation},
    )


def record_agent_tool_selection(
    *,
    schema_chars: int,
    selected_count: int,
    exposed_count: int,
) -> None:
    """记录每回合 Tool 路由和 schema 体积，不使用 Tool 名称作为标签。"""

    for metric, value in (
        ("yawnbot_agent_tool_schema_chars", schema_chars),
        ("yawnbot_agent_tool_selected_count", selected_count),
        ("yawnbot_agent_tool_exposed_count", exposed_count),
    ):
        if value < 0:
            continue
        observe_histogram(metric, float(value))


def record_agent_tool_discovery(outcome: str, value: int = 1) -> None:
    """记录动态工具发现漏斗：调用、返回、为空、以及发现后实际使用。"""

    if outcome not in _AGENT_DISCOVERY_OUTCOMES or value <= 0:
        return
    increment_counter(
        "yawnbot_agent_tool_discovery_total",
        labels={"outcome": outcome},
        value=value,
    )


def record_agent_provider_cache_tokens(*, cached: int, cache_miss: int) -> None:
    """记录 provider usage 中真实 cached/cache-miss token，按回合聚合。"""

    for source, value in (("cached", cached), ("cache_miss", cache_miss)):
        if value < 0:
            continue
        observe_histogram(
            "yawnbot_agent_provider_cache_tokens",
            float(value),
            labels={"source": source},
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


def record_agent_cache(kind: str, outcome: str) -> None:
    """记录本地可观测的前缀稳定性复用或媒体缓存，不代表服务商缓存。"""

    increment_counter(
        "yawnbot_agent_cache_total",
        labels={"component": kind, "outcome": outcome},
    )


def record_agent_outbound(message_type: str, outcome: str) -> None:
    """记录 Agent 输出状态机；只允许低基数消息类型/结果进入标签。"""

    increment_counter(
        "yawnbot_agent_outbound_total",
        labels={"operation": message_type, "outcome": outcome},
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


def _percentile_from_buckets(
    buckets: Mapping[str, int], count: int, quantile: float
) -> float | None:
    """从累积 bucket 推导分位数；count 为 0 时返回 None。"""

    if count <= 0:
        return None
    threshold = count * quantile
    for bound in sorted(buckets, key=float):
        if buckets[bound] >= threshold:
            return float(bound)
    return None


def summarize_ai_metrics(snapshot: dict[str, object]) -> dict[str, object]:
    """把 AI 相关指标快照汇总为概览页可直接渲染的健康数据。

    输入是 ``snapshot_metrics()`` 的返回值；进程重启后计数从零开始，
    汇总值只代表当前进程累计口径。
    """

    counters = cast("list[dict[str, object]]", snapshot.get("counters", []))
    histograms = cast("list[dict[str, object]]", snapshot.get("histograms", []))

    by_outcome: dict[str, int] = {}
    total = success = 0
    degradations = 0
    for item in counters:
        name = str(item.get("name", ""))
        value = int(cast("int", item.get("value", 0)))
        if name == "yawnbot_ai_requests_total":
            labels = cast("dict[str, str]", item.get("labels", {}))
            outcome = labels.get("outcome", "unknown")
            by_outcome[outcome] = by_outcome.get(outcome, 0) + value
            total += value
            if outcome == "success":
                success += value
        elif name == "yawnbot_ai_degradations_total":
            degradations += value

    duration_count = 0
    duration_total = 0.0
    merged_buckets: dict[str, int] = {}
    for item in histograms:
        if str(item.get("name", "")) != "yawnbot_ai_request_duration_seconds":
            continue
        histogram_count = int(cast("int", item.get("count", 0)))
        duration_count += histogram_count
        duration_total += float(cast("float", item.get("sum", 0.0)))
        buckets = cast("dict[str, int]", item.get("buckets", {}))
        for bound, bucket_count in buckets.items():
            merged_buckets[bound] = merged_buckets.get(bound, 0) + bucket_count

    avg_ms = duration_total * 1000 / duration_count if duration_count else None
    p95_seconds = _percentile_from_buckets(merged_buckets, duration_count, 0.95)
    return {
        "requestsTotal": total,
        "success": success,
        "failed": total - success,
        "successRate": (success / total) if total else None,
        "byOutcome": sorted(
            (
                {"outcome": outcome, "count": count}
                for outcome, count in by_outcome.items()
            ),
            key=lambda item: -item["count"],
        ),
        "avgDurationMs": avg_ms,
        "p95DurationMs": p95_seconds * 1000 if p95_seconds is not None else None,
        "degradations": degradations,
    }


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
        f'{key}="{_escape_label(str(value))}"' for key, value in sorted(labels.items())
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
        lines.append(f"{name}{_format_labels(labels)} {_format_number(item['value'])}")

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
                f"{name}_bucket{_format_labels(bucket_labels)} {_format_number(count)}"
            )
        lines.append(
            f"{name}_sum{_format_labels(labels)} "
            f"{_format_number(cast('float', item['sum']))}"
        )
        lines.append(
            f"{name}_count{_format_labels(labels)} {_format_number(item['count'])}"
        )
    return "\n".join(lines) + ("\n" if lines else "")


def reset_metrics_for_tests() -> None:
    """清空聚合值和阶段账本；只供测试隔离使用。"""

    with _lock:
        _counters.clear()
        _histograms.clear()
        _ai_health.clear()
        _phase_starts.clear()


__all__ = [
    "ai_health_snapshot",
    "finish_game_phase",
    "increment_counter",
    "observe_histogram",
    "record_agent_capability_probe",
    "record_agent_context_db_queries",
    "record_agent_phase",
    "record_agent_tool_rounds",
    "record_agent_turn",
    "record_ai_degradation",
    "record_ai_request",
    "record_ai_tokens",
    "record_event_log_write_failure",
    "record_game_ending",
    "record_phase_change",
    "record_queue_rejection",
    "record_rpg_deduction",
    "record_rpg_termination",
    "record_rpg_tutorial",
    "render_prometheus",
    "reset_metrics_for_tests",
    "snapshot_metrics",
    "start_game_phase",
    "summarize_ai_metrics",
]
