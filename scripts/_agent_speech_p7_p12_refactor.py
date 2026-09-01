from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def path(name: str) -> Path:
    return ROOT / name


def read(name: str) -> str:
    return path(name).read_text(encoding="utf-8")


def write(name: str, content: str) -> None:
    target = path(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content.rstrip() + "\n", encoding="utf-8")


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, got {count}")
    return text.replace(old, new, 1)


def function_source(text: str, name: str) -> str:
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            start = min([node.lineno, *(d.lineno for d in node.decorator_list)]) - 1
            end = node.end_lineno or node.lineno
            return "".join(lines[start:end]).rstrip() + "\n"
    raise RuntimeError(f"function not found: {name}")


def remove_functions(text: str, names: set[str]) -> str:
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    spans: list[tuple[int, int, str]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            start = min([node.lineno, *(d.lineno for d in node.decorator_list)]) - 1
            end = node.end_lineno or node.lineno
            # absorb following blank lines so we do not leave huge gaps
            while end < len(lines) and not lines[end].strip():
                end += 1
            spans.append((start, end, node.name))
    found = {name for _start, _end, name in spans}
    missing = names - found
    if missing:
        raise RuntimeError(f"missing functions: {sorted(missing)}")
    for start, end, _name in sorted(spans, reverse=True):
        del lines[start:end]
    return "".join(lines)


# ---------------------------------------------------------------------------
# P7 · semantic topic transition
# ---------------------------------------------------------------------------
write(
    "src/plugins/yawn_core/yawn_agent/topic_state.py",
    r'''"""Bounded topic state and deterministic topic transitions for Agent speech."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_TOPIC_STALE_MINUTES = 30
_TOPIC_FRESH_MINUTES = 10
_ACTIVE_CLUSTER_MIN_MESSAGES = 4
_ACTIVE_CLUSTER_MIN_PARTICIPANTS = 2
_TOPIC_LABEL_LIMIT = 80
_TOPIC_SIMILARITY_CONTINUE = 0.42

TOPIC_ACTION_CONTINUE = "continue"
TOPIC_ACTION_SHIFT = "shift"
TOPIC_ACTION_CLOSE = "close"


@dataclass(frozen=True, slots=True)
class TopicState:
    label: str | None
    status: str
    continuity: str
    age_minutes: int | None
    message_count: int
    participant_count: int
    anchor_message_ids: tuple[int, ...] = ()

    def prompt_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "continuity": self.continuity,
            "message_count": self.message_count,
            "participant_count": self.participant_count,
        }
        if self.label:
            payload["label"] = self.label
        if self.age_minutes is not None:
            payload["age_minutes"] = self.age_minutes
        if self.anchor_message_ids:
            payload["anchor_message_ids"] = list(self.anchor_message_ids)
        return payload


@dataclass(frozen=True, slots=True)
class TopicTransition:
    action: str
    label: str | None
    reason: str

    def prompt_dict(self) -> dict[str, Any]:
        return {"action": self.action, "label": self.label, "reason": self.reason}


def _bounded_int(value: object, *, minimum: int = 0) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return max(parsed, minimum)


def _message_cluster(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not messages:
        return []
    boundary = 0
    for index, item in enumerate(messages):
        if index > 0 and bool(item.get("topic_break_before")):
            boundary = index
    return messages[boundary:]


def _topic_status(
    *,
    label: str | None,
    cluster: list[dict[str, Any]],
    latest_age: int | None,
) -> str:
    if not label and not cluster:
        return "empty"
    if latest_age is None:
        return "unknown_age"
    if latest_age >= _TOPIC_STALE_MINUTES:
        return "stale"
    if latest_age <= _TOPIC_FRESH_MINUTES:
        return "fresh"
    return "cooling"


def _topic_continuity(message_count: int, participant_count: int) -> str:
    if message_count == 0:
        return "none"
    if message_count == 1:
        return "new"
    if (
        message_count >= _ACTIVE_CLUSTER_MIN_MESSAGES
        and participant_count >= _ACTIVE_CLUSTER_MIN_PARTICIPANTS
    ):
        return "active_cluster"
    return "continuing"


def build_topic_state(
    active_topic: str | None,
    messages: list[dict[str, Any]],
) -> TopicState:
    """Build a bounded state that is more informative than one raw topic string."""

    cluster = _message_cluster(messages)
    label = str(active_topic or "").strip()[:240] or None
    latest_age = _bounded_int(cluster[-1].get("minutes_ago")) if cluster else None

    participant_ids: set[int] = set()
    for item in cluster:
        if item.get("role") == "bot":
            continue
        user_id = _bounded_int(item.get("user_id"), minimum=1)
        if user_id is not None:
            participant_ids.add(user_id)

    anchors: list[int] = []
    for item in cluster[-3:]:
        message_id = _bounded_int(item.get("message_id"), minimum=1)
        if message_id is not None and message_id not in anchors:
            anchors.append(message_id)

    status = _topic_status(label=label, cluster=cluster, latest_age=latest_age)
    continuity = _topic_continuity(len(cluster), len(participant_ids))
    return TopicState(
        label=label,
        status=status,
        continuity=continuity,
        age_minutes=latest_age,
        message_count=len(cluster),
        participant_count=len(participant_ids),
        anchor_message_ids=tuple(anchors),
    )


def topic_state_from_prompt(value: object) -> TopicState:
    payload = value if isinstance(value, dict) else {}
    anchors = tuple(
        parsed
        for item in list(payload.get("anchor_message_ids") or [])[:3]
        if (parsed := _bounded_int(item, minimum=1)) is not None
    )
    return TopicState(
        label=str(payload.get("label") or "").strip()[:240] or None,
        status=str(payload.get("status") or "empty"),
        continuity=str(payload.get("continuity") or "none"),
        age_minutes=_bounded_int(payload.get("age_minutes")),
        message_count=_bounded_int(payload.get("message_count")) or 0,
        participant_count=_bounded_int(payload.get("participant_count")) or 0,
        anchor_message_ids=anchors,
    )


def _compact_topic_label(text: object) -> str | None:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    value = re.sub(r"^(?:@\S+\s*)+", "", value)
    value = re.sub(r"^(?:但是|不过|然后|所以|那|这个|就是|话说|对了)[，,：:\s]*", "", value)
    if not value:
        return None
    first = re.split(r"[。！？!?；;\n]", value, maxsplit=1)[0].strip(" ，,：:")
    candidate = first or value
    if len(candidate) <= 2:
        return None
    return candidate[:_TOPIC_LABEL_LIMIT]


def _bigrams(text: str) -> set[str]:
    normalized = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", text.casefold())
    if len(normalized) < 2:
        return {normalized} if normalized else set()
    return {normalized[index : index + 2] for index in range(len(normalized) - 1)}


def topic_similarity(left: object, right: object) -> float:
    a = str(left or "").strip()
    b = str(right or "").strip()
    if not a or not b:
        return 0.0
    a_fold = a.casefold()
    b_fold = b.casefold()
    if a_fold == b_fold:
        return 1.0
    if min(len(a_fold), len(b_fold)) >= 4 and (a_fold in b_fold or b_fold in a_fold):
        return 0.9
    a_parts = _bigrams(a)
    b_parts = _bigrams(b)
    if not a_parts or not b_parts:
        return 0.0
    return len(a_parts & b_parts) / len(a_parts | b_parts)


def resolve_topic_transition(
    state: TopicState,
    *,
    current_text: object = "",
    suggested_topic: object = None,
    close: bool = False,
) -> TopicTransition:
    """Resolve continue/shift/close without another model call.

    A model-provided topic from an existing proactive decision is preferred when
    available. Otherwise a fresh active cluster keeps its semantic label instead
    of replacing it with every new raw user sentence.
    """

    if close:
        return TopicTransition(TOPIC_ACTION_CLOSE, None, "conversation_closed")

    suggestion = _compact_topic_label(suggested_topic)
    if suggestion:
        if not state.label:
            return TopicTransition(TOPIC_ACTION_SHIFT, suggestion, "model_topic_without_anchor")
        if topic_similarity(state.label, suggestion) >= _TOPIC_SIMILARITY_CONTINUE:
            return TopicTransition(TOPIC_ACTION_CONTINUE, state.label, "model_topic_matches")
        return TopicTransition(TOPIC_ACTION_SHIFT, suggestion, "model_topic_changed")

    derived = _compact_topic_label(current_text)
    if state.label and state.status not in {"stale", "empty"} and state.continuity in {
        "continuing",
        "active_cluster",
    }:
        return TopicTransition(TOPIC_ACTION_CONTINUE, state.label, "recent_cluster_continues")
    if state.label and derived and topic_similarity(state.label, derived) >= _TOPIC_SIMILARITY_CONTINUE:
        return TopicTransition(TOPIC_ACTION_CONTINUE, state.label, "current_turn_matches")
    if derived and (not state.label or state.status == "stale" or state.continuity in {"none", "new"}):
        return TopicTransition(TOPIC_ACTION_SHIFT, derived, "new_or_stale_topic")
    if state.label:
        return TopicTransition(TOPIC_ACTION_CONTINUE, state.label, "keep_existing_topic")
    return TopicTransition(TOPIC_ACTION_CONTINUE, None, "no_topic_signal")


__all__ = [
    "TOPIC_ACTION_CLOSE",
    "TOPIC_ACTION_CONTINUE",
    "TOPIC_ACTION_SHIFT",
    "TopicState",
    "TopicTransition",
    "build_topic_state",
    "resolve_topic_transition",
    "topic_similarity",
    "topic_state_from_prompt",
]
''',
)


# ---------------------------------------------------------------------------
# SpeechPlan metadata needed by P7/P10 and the common runtime builder.
# ---------------------------------------------------------------------------
speech_path = "src/plugins/yawn_core/yawn_agent/speech.py"
speech = read(speech_path)
speech = replace_once(
    speech,
    '    reason: str = ""\n    confidence: float = 1.0\n    issues: tuple[SpeechQualityIssue, ...] = ()\n',
    '    reason: str = ""\n    confidence: float = 1.0\n    act: str = "continue"\n    turn_pressure: str = "low"\n    topic: str | None = None\n    topic_action: str = "continue"\n    issues: tuple[SpeechQualityIssue, ...] = ()\n',
    label="SpeechPlan metadata",
)
speech = replace_once(
    speech,
    '            "style": self.style.as_dict(),\n            "quality": [item.as_dict() for item in self.issues],\n            "confidence": max(0.0, min(float(self.confidence), 1.0)),\n',
    '            "style": self.style.as_dict(),\n            "act": self.act,\n            "turn_pressure": self.turn_pressure,\n            "topic": self.topic,\n            "topic_action": self.topic_action,\n            "reason": self.reason,\n            "quality": [item.as_dict() for item in self.issues],\n            "confidence": max(0.0, min(float(self.confidence), 1.0)),\n',
    label="SpeechPlan trace metadata",
)
for function_name in ("speech_plan_from_text", "speech_plan_from_segments"):
    marker = '    reason: str = "",\n    confidence: float = 1.0,\n) -> SpeechPlan:\n'
    replacement = (
        '    reason: str = "",\n'
        '    confidence: float = 1.0,\n'
        '    action: str = "speak",\n'
        '    act: str = "continue",\n'
        '    turn_pressure: str = "low",\n'
        '    topic: str | None = None,\n'
        '    topic_action: str = "continue",\n'
        ') -> SpeechPlan:\n'
    )
    before_count = speech.count(marker)
    if before_count < 1:
        raise RuntimeError(f"{function_name}: signature marker missing")
    speech = speech.replace(marker, replacement, 1)
    return_marker = (
        '        reason=str(reason or "")[:240],\n'
        '        confidence=max(0.0, min(float(confidence), 1.0)),\n'
    )
    return_replacement = (
        '        reason=str(reason or "")[:240],\n'
        '        confidence=max(0.0, min(float(confidence), 1.0)),\n'
        '        action=str(action or "speak").strip().lower() or "speak",\n'
        '        act=str(act or "continue").strip().lower() or "continue",\n'
        '        turn_pressure=str(turn_pressure or "low").strip().lower() or "low",\n'
        '        topic=str(topic or "").strip()[:240] or None,\n'
        '        topic_action=str(topic_action or "continue").strip().lower() or "continue",\n'
    )
    speech = replace_once(
        speech,
        return_marker,
        return_replacement,
        label=f"{function_name} return metadata",
    )
write(speech_path, speech)


write(
    "src/plugins/yawn_core/yawn_agent/speech_runtime.py",
    r'''"""Common speech runtime shared by dialogue, proactive and WebUI dry-runs."""

from __future__ import annotations

from typing import Any

from .execution_trace import trace_event
from .speech import (
    SPEECH_SCENE_TOOL_RESULT,
    SpeechPlan,
    SpeechTarget,
    speech_plan_from_segments,
    speech_plan_from_text,
)
from .speech_act import plan_speech_act
from .speech_policy import resolve_speech_scene, resolve_speech_style
from .speech_quality import finalize_speech_plan
from .topic_state import resolve_topic_transition, topic_state_from_prompt
from .turn_taking import plan_turn_taking


def _turn_text(current_turn: object, fallback: str) -> str:
    if isinstance(current_turn, dict):
        return str(current_turn.get("content") or fallback)
    prompt_dict = getattr(current_turn, "prompt_dict", None)
    if callable(prompt_dict):
        payload = prompt_dict()
        if isinstance(payload, dict):
            return str(payload.get("content") or fallback)
    return fallback


def build_runtime_speech_plan(
    *,
    text: object = "",
    segments: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    persona: dict[str, str] | None,
    current_turn: object = None,
    context: dict[str, Any] | None = None,
    source: str | None = None,
    after_tool: bool = False,
    action: str = "speak",
    target_user_id: int | None = None,
    reply_to_message_id: int | None = None,
    suggested_topic: object = None,
    reason: str = "",
    confidence: float = 1.0,
) -> SpeechPlan:
    resolved_context = context or {}
    scene = (
        SPEECH_SCENE_TOOL_RESULT
        if after_tool
        else resolve_speech_scene(current_turn, source=source)
    )
    style = resolve_speech_style(persona, scene=scene)
    act_plan = plan_speech_act(current_turn, scene=scene)
    turn_plan = plan_turn_taking(current_turn, scene=scene, context=resolved_context)
    topic_state = topic_state_from_prompt(resolved_context.get("topic_state"))
    transition = resolve_topic_transition(
        topic_state,
        current_text=_turn_text(current_turn, str(text or "")),
        suggested_topic=suggested_topic,
        close=str(action).lower() == "close" or act_plan.act == "close",
    )
    target = SpeechTarget(
        user_id=target_user_id,
        reply_to_message_id=reply_to_message_id,
    )
    common = {
        "scene": scene,
        "style": style,
        "target": target,
        "reason": reason,
        "confidence": confidence,
        "action": action,
        "act": act_plan.act,
        "turn_pressure": turn_plan.pressure,
        "topic": transition.label,
        "topic_action": transition.action,
    }
    if segments:
        return speech_plan_from_segments(segments, **common)
    return speech_plan_from_text(text, **common)


def trace_speech_decision(
    plan: SpeechPlan,
    *,
    emotion_state: object = None,
    participation_action: str | None = None,
    status: str | None = None,
    trace: object = None,
) -> SpeechPlan:
    resolved = finalize_speech_plan(plan, autofix=False)
    output = resolved.trace_payload()
    if emotion_state not in (None, {}, ""):
        output["emotion"] = emotion_state
    if participation_action:
        output["participation_action"] = participation_action
    trace_event(
        "speech",
        "发言决策",
        status=status or ("planned" if resolved.should_speak else "skipped"),
        output=output,
        detail=(
            "SpeechPlan 已确定，但尚未执行 OneBot 发送。"
            if resolved.should_speak
            else "策略决定本轮不产生用户可见发言。"
        ),
        trace=trace,
    )
    return resolved


def speech_simulation_payload(
    plan: SpeechPlan,
    *,
    emotion_state: object = None,
    should_speak: bool | None = None,
    preview_only: bool = False,
    user_text: str = "",
    recent_texts: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    resolved = finalize_speech_plan(
        plan,
        user_text=user_text,
        recent_texts=recent_texts,
        autofix=not preview_only,
    )
    payload = resolved.trace_payload()
    payload.update(
        {
            "status": "policy_only" if preview_only else "final",
            "should_speak": resolved.should_speak if should_speak is None else should_speak,
            "text": resolved.visible_text,
            "segments": [dict(item) for item in resolved.segments],
            "emotion": emotion_state,
        }
    )
    return payload


__all__ = [
    "build_runtime_speech_plan",
    "speech_simulation_payload",
    "trace_speech_decision",
]
''',
)


# ---------------------------------------------------------------------------
# P8 · Tool result evidence used by the actual tool loop.
# ---------------------------------------------------------------------------
write(
    "src/plugins/yawn_core/yawn_agent/tool_result_speech.py",
    r'''"""Compact speech evidence derived from already-projected tool results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

TOOL_RESULT_SPEECH_INSTRUCTION = (
    "工具返回是后台事实，不是可以原样发送给群友的话。"
    "看到 role=tool 后先判断 ok；成功只提用户真正关心的结果，"
    "失败只说明可公开的失败原因和必要下一步。"
    "不要照抄 JSON、字段名、布尔值、内部 outcome/delivery_state、"
    "权限级别、trace、路径或协议细节。"
    "列表结果先概括数量，再按用户问题挑最相关项；"
    "除非用户明确要求，不要机械倾倒全部记录。"
    "写操作只有工具明确成功后才能用完成时；"
    "ok=false、超时或不确定状态不能说“已经完成”。"
)


@dataclass(frozen=True, slots=True)
class SpeechEvidence:
    tool_name: str
    ok: bool
    summary: str
    delivery_state: str | None = None
    item_count: int | None = None

    def prompt_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tool": self.tool_name,
            "ok": self.ok,
            "summary": self.summary,
        }
        if self.delivery_state:
            payload["delivery_state"] = self.delivery_state
        if self.item_count is not None:
            payload["item_count"] = self.item_count
        return payload


def build_speech_evidence(tool_name: str, payload: dict[str, Any]) -> SpeechEvidence:
    name = str(tool_name or "工具").strip()[:64] or "工具"
    ok = bool(payload.get("ok"))
    if not ok:
        error = str(payload.get("error") or "执行失败").strip()[:160]
        return SpeechEvidence(name, False, f"未成功：{error}")

    result = payload.get("result")
    delivery_state: str | None = None
    item_count: int | None = None
    if isinstance(result, dict):
        delivery_state = str(result.get("delivery_state") or "").strip()[:32] or None
        items = result.get("items")
        if isinstance(items, list):
            item_count = len(items)
        elif isinstance(result.get("count"), int):
            item_count = max(int(result["count"]), 0)
    elif isinstance(result, list):
        item_count = len(result)

    if delivery_state in {"unknown", "delivery_unknown"}:
        summary = "回执不确定；不能重复执行，也不能断言一定失败"
    elif item_count is not None:
        summary = f"成功，返回 {item_count} 项；只挑与当前问题有关的信息"
    else:
        summary = "成功；只说明与当前请求相关的结果"
    return SpeechEvidence(name, True, summary, delivery_state, item_count)


def tool_result_speech_hint(tool_name: str, payload: dict[str, Any]) -> str:
    evidence = build_speech_evidence(tool_name, payload)
    return f"{evidence.tool_name} {evidence.summary}。"


__all__ = [
    "SpeechEvidence",
    "TOOL_RESULT_SPEECH_INSTRUCTION",
    "build_speech_evidence",
    "tool_result_speech_hint",
]
''',
)


# ---------------------------------------------------------------------------
# outbound: make SpeechPlan the common protocol boundary and emit P10 trace.
# ---------------------------------------------------------------------------
outbound_path = "src/plugins/yawn_core/yawn_agent/outbound.py"
outbound = read(outbound_path)
outbound = replace_once(
    outbound,
    "from dataclasses import dataclass\n",
    "from dataclasses import dataclass, replace\n",
    label="outbound dataclasses import",
)
insert_marker = "\ndef prepare_text_message(\n"
prepare_speech = r'''
async def prepare_speech_plan(
    plan: SpeechPlan,
    *,
    session: Any,
    group_id: int,
    actor_user_id: int | None = None,
    allowed_segment_types: frozenset[str] | None = None,
    speech_user_text: str = "",
    recent_speech: tuple[str, ...] | list[str] = (),
    speech_autofix: bool = True,
    trace_context: dict[str, Any] | None = None,
) -> PreparedOutboundMessage:
    """Finalize one SpeechPlan, trace the decision, then enter OneBot validation."""

    resolved = finalize_speech_plan(
        plan,
        user_text=speech_user_text,
        recent_texts=recent_speech,
        autofix=speech_autofix,
    )
    trace_payload = resolved.trace_payload()
    if trace_context:
        trace_payload.update(trace_context)
    trace_event(
        "speech",
        "发言决策",
        status="planned" if resolved.should_speak else "skipped",
        output=trace_payload,
        detail=(
            "SpeechPlan 已通过表达质量层，准备进入 OneBot 发送校验。"
            if resolved.should_speak
            else "SpeechPlan 决定不产生用户可见消息。"
        ),
    )
    if not resolved.should_speak:
        raise ValueError("SpeechPlan 当前动作不应发送消息")
    if resolved.segments:
        prepared = await prepare_outbound_message(
            list(resolved.segments),
            session=session,
            group_id=group_id,
            actor_user_id=actor_user_id,
            allowed_segment_types=allowed_segment_types,
            speech_scene=resolved.scene,
            speech_style=resolved.style,
            speech_user_text=speech_user_text,
            recent_speech=recent_speech,
            speech_autofix=False,
            trace_speech=False,
        )
    else:
        prepared = prepare_text_message(
            resolved.text,
            speech_scene=resolved.scene,
            speech_style=resolved.style,
            speech_user_text=speech_user_text,
            recent_speech=recent_speech,
            speech_autofix=False,
            trace_speech=False,
        )
    return replace(
        prepared,
        speech_scene=resolved.scene,
        quality_issues=_quality_codes(resolved),
    )

'''
if insert_marker not in outbound:
    raise RuntimeError("outbound prepare_text marker missing")
outbound = outbound.replace(insert_marker, "\n" + prepare_speech + "def prepare_text_message(\n", 1)
outbound = replace_once(
    outbound,
    "    speech_autofix: bool = True,\n) -> PreparedOutboundMessage:\n",
    "    speech_autofix: bool = True,\n    trace_speech: bool = True,\n) -> PreparedOutboundMessage:\n",
    label="prepare_text trace flag",
)
outbound = replace_once(
    outbound,
    "    bounded = str(plan.text)\n",
    '    if trace_speech:\n        trace_event(\n            "speech",\n            "发言决策",\n            status="planned" if plan.should_speak else "skipped",\n            output=plan.trace_payload(),\n        )\n    bounded = str(plan.text)\n',
    label="prepare_text speech trace",
)
outbound = replace_once(
    outbound,
    "    speech_autofix: bool = False,\n) -> PreparedOutboundMessage:\n",
    "    speech_autofix: bool = False,\n    trace_speech: bool = True,\n) -> PreparedOutboundMessage:\n",
    label="prepare_segments trace flag",
)
outbound = replace_once(
    outbound,
    "    raw_segments = list(plan.segments)\n\n    reply_count = sum",
    '    if trace_speech:\n        trace_event(\n            "speech",\n            "发言决策",\n            status="planned" if plan.should_speak else "skipped",\n            output=plan.trace_payload(),\n        )\n    raw_segments = list(plan.segments)\n\n    reply_count = sum',
    label="prepare_segments speech trace",
)
write(outbound_path, outbound)


# ---------------------------------------------------------------------------
# P12 · pull reusable dialogue state/context out of dialogue.py.
# ---------------------------------------------------------------------------
dialogue_path = "src/plugins/yawn_core/yawn_agent/dialogue.py"
dialogue = read(dialogue_path)
activity_src = function_source(dialogue, "_activity_window_counts").replace(
    "async def _activity_window_counts", "async def activity_window_counts", 1
)
context_src = function_source(dialogue, "_load_context").replace(
    "async def _load_context", "async def load_context", 1
)

write(
    "src/plugins/yawn_core/yawn_agent/activity.py",
    '''"""Shared bounded group activity aggregates for dialogue/proactive paths."""\n\nfrom __future__ import annotations\n\nfrom datetime import datetime, timedelta\nfrom typing import Any\n\nfrom sqlalchemy import case, exists, func, select\n\nfrom ..data_models.agent_memory import AgentPrivacy\nfrom ..data_models.group_agent_message import GroupAgentMessage\n\n'''
    + activity_src
    + '\n\n__all__ = ["activity_window_counts"]\n',
)

write(
    "src/plugins/yawn_core/yawn_agent/context_loader.py",
    '''"""Prompt context loading; kept outside dialogue orchestration by design."""\n\nfrom __future__ import annotations\n\nimport json\nfrom collections.abc import Sequence\nfrom datetime import datetime\nfrom typing import Any\n\nfrom sqlalchemy import case, or_, select\n\nfrom ..data_models.agent_memory import AgentMemory, AgentPrivacy, AgentRelation\nfrom ..data_models.bot_group import BotGroup\nfrom ..data_models.group_agent_config import GroupAgentConfig\nfrom ..data_models.group_agent_message import GroupAgentMessage\nfrom ..data_models.user_group import UserGroup\nfrom .activity import activity_window_counts as _activity_window_counts\nfrom .context import ActivitySnapshot, build_context, now_beijing, trim_context_messages\nfrom .context_budget import pack_context\nfrom .context_history import history_message_payload as _history_message_payload, select_context_messages\nfrom .emotion import emotion_context_state\nfrom .log import dbg\nfrom .memory import effective_relation_confidence, rank_memories\nfrom .persona import persona_editor_profile\n\n_MEMORY_CONTEXT_CHAR_BUDGET = 6_000\n_MEMORY_CONTEXT_LIMIT = 24\n\n'''
    + context_src
    + '\n\n__all__ = ["load_context"]\n',
)

write(
    "src/plugins/yawn_core/yawn_agent/dialogue_support.py",
    r'''"""Small compatibility-safe helpers shared by dialogue/proactive paths."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot
from sqlalchemy import select

from ..data_models.group_agent_message import GroupAgentMessage
from .collector import is_pending_trigger_expired
from .context import now_beijing
from .execution_trace import trace_event
from .log import dbg, dbg_exc
from .message_parser import NormalizedMessage
from .outbound import (
    DELIVERY_CONFIRMED_FAILURE,
    PreparedOutboundMessage,
    SendResult,
    prepare_text_message,
    send_prepared_outbound,
)

_GREETING_WORDS = ("你好", "嗨", "hello", "hi", "早上好", "晚上好", "在吗", "在不在")


def contains_word(text: str, word: str) -> bool:
    if not word:
        return False
    if re.fullmatch(r"[a-z0-9 ]+", word):
        return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) is not None
    return word in text


def current_turn_focus_ids(
    actor_user_id: int,
    normalized: NormalizedMessage,
    *,
    bot_id: int | None = None,
) -> list[int]:
    focus = [int(actor_user_id)]
    focus.extend(
        int(user_id)
        for user_id in normalized.mentions
        if bot_id is None or int(user_id) != bot_id
    )
    if normalized.reply_chain:
        raw_user_id = normalized.reply_chain[0].get("user_id")
        try:
            reply_user_id = int(str(raw_user_id))
        except (TypeError, ValueError):
            reply_user_id = 0
        if reply_user_id > 0 and reply_user_id != bot_id:
            focus.append(reply_user_id)
    return list(dict.fromkeys(focus))


def is_recent_duplicate(
    item: object,
    input_fingerprint: str,
    response_fingerprint: str,
    now: datetime,
) -> bool:
    if (
        not isinstance(item, dict)
        or item.get("input") != input_fingerprint
        or item.get("response") != response_fingerprint
    ):
        return False
    raw_at = item.get("at")
    if not raw_at:
        return True
    try:
        return now - datetime.fromisoformat(str(raw_at)) < timedelta(minutes=10)
    except (TypeError, ValueError):
        return False


def deterministic_reply(text: str) -> str | None:
    normalized = " ".join(text.lower().split())
    if any(contains_word(normalized, word) for word in _GREETING_WORDS):
        return "我在呀，有事直接说～"
    if "agent状态" in normalized or "群聊agent" in normalized:
        return "群聊 Agent 在线；复杂对话需要配置 AI_API_KEY。"
    return None


async def send_group_text(
    bot: Bot, group_id: int, text: str
) -> tuple[bool, int | None]:
    try:
        prepared = prepare_text_message(text)
        result = await send_prepared_outbound(bot, group_id, prepared)
    except Exception:  # noqa: BLE001
        logger.warning("群聊 Agent 发送群消息失败: %s", group_id)
        dbg_exc(f"群 {group_id} 发送群消息失败 text={text!r}")
        return False, None
    dbg(f"群 {group_id} 发送群消息成功 text={text!r}")
    return result.ends_turn, result.message_id


async def send_unless_expired(
    bot: Bot,
    group_id: int,
    message: str | PreparedOutboundMessage,
    enqueued_at: float | None,
    *,
    label: str,
    message_id: Any = None,
    session: Any = None,
    actor_user_id: int | None = None,
    source: str = "dialogue",
) -> SendResult:
    if enqueued_at is not None and is_pending_trigger_expired(enqueued_at):
        trace_event(
            "outbound",
            label,
            status="skipped",
            output={"sent": False, "reason": "trigger_expired"},
            detail="触发消息在队列/群锁等待期间过期，取消用户可见发送",
        )
        dbg(f"群 {group_id} {label}前触发已过期,跳过发送: message_id={message_id}")
        return SendResult(
            sent=False,
            message_id=None,
            normalized_text="",
            segment_types=(),
            outcome="expired",
            delivery_state=DELIVERY_CONFIRMED_FAILURE,
        )
    prepared = prepare_text_message(message) if isinstance(message, str) else message
    try:
        return await send_prepared_outbound(
            bot,
            group_id,
            prepared,
            session=session,
            actor_user_id=actor_user_id,
            source=source,
        )
    except Exception:  # noqa: BLE001
        logger.warning("群聊 Agent 发送群消息失败: %s", group_id)
        dbg_exc(f"群 {group_id} {label}失败")
        return SendResult(
            sent=False,
            message_id=None,
            normalized_text=prepared.normalized_text,
            segment_types=(),
            outcome="send_failed",
            delivery_state=DELIVERY_CONFIRMED_FAILURE,
        )


async def persist_bot_reply(
    session: Any,
    bot_id: int,
    group_id: int,
    message_id: int | None,
    text: str,
    retention_days: int,
    *,
    segments: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    reply_chain: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    forward_tree: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    media_refs: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
) -> None:
    if not message_id:
        dbg(f"群 {group_id} bot 发言缺少 message_id,跳过自言落库")
        return
    duplicate = await session.scalar(
        select(GroupAgentMessage).where(
            GroupAgentMessage.bot_id == bot_id,
            GroupAgentMessage.group_id == group_id,
            GroupAgentMessage.message_id == message_id,
        )
    )
    if duplicate is not None:
        dbg(f"群 {group_id} bot 发言 {message_id} 已落库过,去重跳过")
        return
    now = now_beijing()
    retention = max(1, min(int(retention_days), 365))
    session.add(
        GroupAgentMessage(
            bot_id=bot_id,
            message_id=message_id,
            group_id=group_id,
            user_id=bot_id,
            sender_name=None,
            role="bot",
            title=None,
            normalized_text=text,
            segments=list(segments or []),
            reply_chain=list(reply_chain or []),
            forward_tree=list(forward_tree or []),
            media_refs=list(media_refs or []),
            received_at=now,
            expires_at=now + timedelta(days=retention),
        )
    )
    dbg(f"群 {group_id} bot 发言 {message_id} 已加入自言落库(role=bot)")


__all__ = [
    "contains_word",
    "current_turn_focus_ids",
    "deterministic_reply",
    "is_recent_duplicate",
    "persist_bot_reply",
    "send_group_text",
    "send_unless_expired",
]
''',
)

write(
    "src/plugins/yawn_core/yawn_agent/speech_finalize.py",
    r'''"""SpeechPlan finalization and post-send state updates for dialogue."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from nonebot.adapters.onebot.v11 import Bot

from ..data_models.group_agent_config import GroupAgentConfig
from .context import now_beijing
from .execution_trace import trace_event
from .log import dbg, dbg_exc
from .message_parser import NormalizedMessage
from .outbound import PreparedOutboundMessage, SendResult, prepare_speech_plan
from .persona import persona_behavior
from .speech import SpeechPlan
from .topic_state import TOPIC_ACTION_CLOSE, TOPIC_ACTION_SHIFT

SendFunc = Callable[..., Awaitable[SendResult]]
PersistFunc = Callable[..., Awaitable[None]]
MarkFunc = Callable[..., Any]
DuplicateFunc = Callable[[object, str, str, Any], bool]


def apply_speech_topic(config: GroupAgentConfig, plan: SpeechPlan) -> str | None:
    current = str(config.active_topic or "").strip() or None
    next_topic = current
    if plan.topic_action == TOPIC_ACTION_CLOSE:
        next_topic = None
    elif plan.topic_action == TOPIC_ACTION_SHIFT and plan.topic:
        next_topic = plan.topic[:240]
    elif plan.topic and not current:
        next_topic = plan.topic[:240]
    if next_topic != current:
        config.context_epoch += 1
        config.active_topic = next_topic
        dbg(
            f"群 {config.group_id} 话题状态变更: epoch={config.context_epoch} "
            f"action={plan.topic_action} topic={next_topic!r}"
        )
    return next_topic


async def finalize_reply(  # noqa: PLR0913
    bot: Bot,
    group_id: int,
    config: GroupAgentConfig,
    session: Any,
    normalized: NormalizedMessage,
    content: SpeechPlan | PreparedOutboundMessage,
    user_prompt: str,
    enqueued_at: float | None,
    message_id: Any,
    *,
    send_func: SendFunc,
    persist_func: PersistFunc,
    mark_func: MarkFunc,
    duplicate_func: DuplicateFunc,
    emotion_state: object = None,
) -> None:
    if isinstance(content, SpeechPlan):
        recent_speech = tuple(
            str(item.get("text") or "")
            for item in (config.recent_response_fingerprints or [])
            if isinstance(item, dict) and item.get("text")
        )[-4:]
        prepared = await prepare_speech_plan(
            content,
            session=session,
            group_id=group_id,
            actor_user_id=None,
            speech_user_text=normalized.plain_text,
            recent_speech=recent_speech,
            trace_context={"emotion": emotion_state},
        )
        speech_plan = content
    else:
        prepared = content
        speech_plan = None
    reply_text = prepared.normalized_text
    short_conversation_enabled = bool(config.short_conversation_enabled)
    max_followup_bot_turns = (
        persona_behavior(config).max_followup_bot_turns
        if short_conversation_enabled
        else 1
    )
    fingerprint_source = reply_text or json.dumps(
        list(prepared.segment_records), ensure_ascii=False, sort_keys=True
    )
    input_fingerprint = hashlib.sha256(user_prompt.casefold().encode("utf-8")).hexdigest()
    response_fingerprint = hashlib.sha256(
        fingerprint_source.casefold().encode("utf-8")
    ).hexdigest()
    now = now_beijing()
    recent = list(config.recent_response_fingerprints or [])
    duplicate = any(
        duplicate_func(item, input_fingerprint, response_fingerprint, now)
        for item in recent
    )
    if duplicate:
        trace_event(
            "outbound",
            "重复回复抑制",
            status="skipped",
            output={"sent": False},
            detail="与近 10 分钟同一输入/回复指纹重复",
        )
        dbg(f"群 {group_id} 回复与近 10 分钟内重复,抑制发送: {reply_text!r}")
        return
    sent = await send_func(
        bot,
        group_id,
        prepared,
        enqueued_at,
        label="正文发送",
        message_id=message_id,
        session=session,
        actor_user_id=None,
        source="dialogue",
    )
    if not sent.ends_turn:
        dbg(f"群 {group_id} 回复确认未发送(触发过期或明确失败),放弃本轮状态更新")
        return
    next_active_topic = (
        apply_speech_topic(config, speech_plan)
        if speech_plan is not None
        else str(config.active_topic or "").strip() or None
    )

    try:
        if sent.sent:
            await persist_func(
                session,
                int(bot.self_id),
                group_id,
                sent.message_id,
                sent.normalized_text,
                int(config.raw_retention_days),
                segments=sent.segments,
                reply_chain=sent.reply_chain,
                forward_tree=sent.forward_tree,
                media_refs=sent.media_refs,
            )
        else:
            dbg(f"群 {group_id} 回复投递状态未知,按可能已送达推进冷却/去重但不写消息历史")
        recent.append(
            {
                "input": input_fingerprint,
                "response": response_fingerprint,
                "text": reply_text[:500],
                "at": now.isoformat(),
            }
        )
        config.recent_response_fingerprints = recent[-8:]
        config.last_response_fingerprint = response_fingerprint
        config.last_response_input_fingerprint = input_fingerprint
        config.last_response_at = now
        config.last_agent_at = now
        await session.commit()
    except Exception as exc:  # noqa: BLE001
        trace_event(
            "state",
            "回复后状态提交",
            status="degraded",
            output={
                "rolled_back": True,
                "delivery_state": sent.delivery_state,
                "error_type": type(exc).__name__,
            },
            detail="消息已结束投递流程，但本地消息历史/去重/冷却状态写入失败",
        )
        dbg_exc(f"群 {group_id} 回复后状态提交失败,已回滚")
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            dbg_exc(f"群 {group_id} 回复后状态回滚失败(忽略)")
    else:
        trace_event(
            "state",
            "回复后状态提交",
            output={
                "recent_fingerprints": len(recent[-8:]),
                "context_epoch": config.context_epoch,
                "delivery_state": sent.delivery_state,
                "topic": next_active_topic,
                "topic_action": speech_plan.topic_action if speech_plan else "compat",
            },
        )
        dbg(f"群 {group_id} 回复后状态已提交(指纹记录 {len(recent[-8:])} 条)")

    if short_conversation_enabled:
        try:
            mark_func(
                int(bot.self_id),
                group_id,
                topic=next_active_topic,
                source="dialogue",
                max_bot_turns=max_followup_bot_turns,
            )
        except Exception as exc:  # noqa: BLE001
            trace_event(
                "state",
                "短会话状态推进",
                status="degraded",
                output={"error_type": type(exc).__name__},
                detail="正文已经结束投递流程，但短会话内存状态推进失败",
            )
            dbg_exc(f"群 {group_id} 短会话状态推进失败(忽略)")


__all__ = ["apply_speech_topic", "finalize_reply"]
''',
)

# Remove moved function bodies and install thin compatibility aliases/wrapper.
dialogue = remove_functions(
    dialogue,
    {
        "_send_group_text",
        "contains_word",
        "_current_turn_focus_ids",
        "_is_recent_duplicate",
        "_deterministic_reply",
        "_send_unless_expired",
        "persist_bot_reply",
        "_activity_window_counts",
        "_load_context",
        "_finalize_reply",
    },
)
dialogue = dialogue.replace("import re\n", "")
dialogue = re.sub(r'^_GREETING_WORDS = .*?\n', '', dialogue, flags=re.M)
dialogue = re.sub(r'^_MEMORY_CONTEXT_CHAR_BUDGET = .*?\n', '', dialogue, flags=re.M)
dialogue = re.sub(r'^# 条目上限.*?\n_MEMORY_CONTEXT_LIMIT = .*?\n', '', dialogue, flags=re.M)

insert_after = "from .conversation import mark_bot_reply\n"
new_imports = '''from .activity import activity_window_counts as _activity_window_counts\nfrom .context_loader import load_context as _load_context\nfrom .dialogue_support import (\n    contains_word,\n    current_turn_focus_ids as _current_turn_focus_ids,\n    deterministic_reply as _deterministic_reply,\n    is_recent_duplicate as _is_recent_duplicate,\n    persist_bot_reply,\n    send_group_text as _send_group_text,\n    send_unless_expired as _send_unless_expired,\n)\n'''
dialogue = replace_once(
    dialogue,
    insert_after,
    insert_after + new_imports,
    label="dialogue shared imports",
)
dialogue = replace_once(
    dialogue,
    "from .persona import persona_behavior, persona_editor_profile, resolve_persona\n",
    "from .persona import persona_behavior, persona_editor_profile, resolve_persona\nfrom .speech import SpeechPlan\nfrom .speech_finalize import apply_speech_topic, finalize_reply as _speech_finalize_reply\nfrom .speech_runtime import build_runtime_speech_plan\nfrom .tool_result_speech import build_speech_evidence\n",
    label="dialogue speech imports",
)
wrapper = r'''
async def _finalize_reply(
    bot: Bot,
    group_id: int,
    config: GroupAgentConfig,
    session: Any,
    normalized: NormalizedMessage,
    content: str | PreparedOutboundMessage | SpeechPlan,
    user_prompt: str,
    enqueued_at: float | None,
    message_id: Any,
    *,
    context: dict[str, Any] | None = None,
    current_turn: CurrentTurn | None = None,
    after_tool: bool = False,
) -> None:
    """Compatibility wrapper; stateful finalization now lives in speech_finalize.py."""

    plan_or_prepared: PreparedOutboundMessage | SpeechPlan
    if isinstance(content, str):
        plan_or_prepared = build_runtime_speech_plan(
            text=content,
            persona=resolve_persona(config),
            current_turn=current_turn,
            context=context or {},
            source="dialogue",
            after_tool=after_tool,
        )
    else:
        plan_or_prepared = content
    await _speech_finalize_reply(
        bot,
        group_id,
        config,
        session,
        normalized,
        plan_or_prepared,
        user_prompt,
        enqueued_at,
        message_id,
        send_func=_send_unless_expired,
        persist_func=persist_bot_reply,
        mark_func=mark_bot_reply,
        duplicate_func=_is_recent_duplicate,
        emotion_state=(context or {}).get("emotion_state"),
    )


'''
marker = "async def _process_group_message(\n"
if marker not in dialogue:
    raise RuntimeError("dialogue process marker missing")
dialogue = dialogue.replace(marker, wrapper + marker, 1)
# Track whether the final answer is based on tool evidence.
dialogue = replace_once(
    dialogue,
    "            rounds = 0\n            turn_usage: dict[str, int] = {}\n",
    "            rounds = 0\n            had_tool_results = False\n            turn_usage: dict[str, int] = {}\n",
    label="dialogue tool result flag",
)
# Final text must enter the runtime SpeechPlan; tool-backed replies use tool_result scene.
dialogue = replace_once(
    dialogue,
    "                            enqueued_at,\n                            message_id,\n                        )\n                    return\n",
    "                            enqueued_at,\n                            message_id,\n                            context=context,\n                            current_turn=current_turn,\n                            after_tool=had_tool_results,\n                        )\n                    return\n",
    label="dialogue runtime finalizer call",
)
# Every executed tool contributes compact speech evidence.
dialogue = replace_once(
    dialogue,
    "                    if tool_name == \"discover_tools\" and bool(result.get(\"ok\")):\n",
    "                    had_tool_results = True\n                    if tool_name == \"discover_tools\" and bool(result.get(\"ok\")):\n",
    label="dialogue mark tool result",
)
dialogue = replace_once(
    dialogue,
    '''                    messages.append(\n                        {\n                            "role": "tool",\n                            "tool_call_id": call.id,\n                            "content": json.dumps(result, ensure_ascii=False),\n                        }\n                    )\n''',
    '''                    tool_payload = dict(result)\n                    tool_payload["speech_evidence"] = build_speech_evidence(\n                        tool_name, result\n                    ).prompt_dict()\n                    messages.append(\n                        {\n                            "role": "tool",\n                            "tool_call_id": call.id,\n                            "content": json.dumps(tool_payload, ensure_ascii=False),\n                        }\n                    )\n''',
    label="dialogue tool speech evidence",
)
# Structured send tools update topic state without storing the raw trigger sentence as topic.
dialogue = replace_once(
    dialogue,
    '''                            if config.short_conversation_enabled:\n                                mark_bot_reply(\n                                    int(bot.self_id),\n                                    group_id,\n                                    topic=str(config.active_topic or normalized.plain_text or ""),\n                                    source="dialogue",\n                                    max_bot_turns=persona_behavior(\n                                        config\n                                    ).max_followup_bot_turns,\n                                )\n''',
    '''                            tool_speech_plan = build_runtime_speech_plan(\n                                text=str(payload.get("text") or ""),\n                                persona=resolve_persona(config),\n                                current_turn=current_turn,\n                                context=context,\n                                source="dialogue",\n                            )\n                            tool_topic = apply_speech_topic(config, tool_speech_plan)\n                            if config.short_conversation_enabled:\n                                mark_bot_reply(\n                                    int(bot.self_id),\n                                    group_id,\n                                    topic=tool_topic,\n                                    source="dialogue",\n                                    max_bot_turns=persona_behavior(\n                                        config\n                                    ).max_followup_bot_turns,\n                                )\n''',
    label="dialogue visible tool topic",
)
write(dialogue_path, dialogue)


# ---------------------------------------------------------------------------
# P9 · proactive parser remains compatibility boundary; delivery uses SpeechPlan.
# ---------------------------------------------------------------------------
proactive_path = "src/plugins/yawn_core/yawn_agent/proactive.py"
proactive = read(proactive_path)
proactive = replace_once(
    proactive,
    '''from .dialogue import (\n    _activity_window_counts,\n    _load_context,\n    persist_bot_reply,\n)\n''',
    '''from .activity import activity_window_counts as _activity_window_counts\nfrom .context_loader import load_context as _load_context\nfrom .dialogue_support import persist_bot_reply\n''',
    label="proactive dialogue dependency cleanup",
)
proactive = replace_once(
    proactive,
    '''from .outbound import (\n    PreparedOutboundMessage,\n    SendResult,\n    prepare_outbound_message,\n    prepare_text_message,\n    send_prepared_outbound,\n)\n''',
    '''from .outbound import (\n    PreparedOutboundMessage,\n    SendResult,\n    prepare_speech_plan,\n    send_prepared_outbound,\n)\n''',
    label="proactive outbound imports",
)
proactive = replace_once(
    proactive,
    "from .persona import persona_behavior, resolve_persona\n",
    "from .persona import persona_behavior, resolve_persona\nfrom .speech import SpeechPlan, speech_plan_from_segments, speech_plan_from_text\nfrom .speech_finalize import apply_speech_topic\nfrom .speech_runtime import build_runtime_speech_plan, trace_speech_decision\n",
    label="proactive speech imports",
)
old_prepare = function_source(proactive, "_prepare_proactive_message")
new_prepare = r'''async def _prepare_proactive_message(
    decision: _ProactiveDecision | SpeechPlan,
    *,
    session: Any,
    group_id: int,
    speech_user_text: str = "",
    recent_speech: tuple[str, ...] | list[str] = (),
    emotion_state: object = None,
) -> PreparedOutboundMessage:
    """Compatibility parser boundary -> unified SpeechPlan -> outbound."""

    if isinstance(decision, SpeechPlan):
        plan = decision
    elif decision.segments:
        plan = speech_plan_from_segments(
            list(decision.segments),
            scene="conversation",
            target=None,
            reason=decision.reason,
            confidence=decision.confidence,
        )
    else:
        plan = speech_plan_from_text(
            decision.text,
            scene="conversation",
            reason=decision.reason,
            confidence=decision.confidence,
        )
    return await prepare_speech_plan(
        plan,
        session=session,
        group_id=group_id,
        actor_user_id=None,
        speech_user_text=speech_user_text,
        recent_speech=recent_speech,
        speech_autofix=True,
        trace_context={
            "emotion": emotion_state,
            "participation_action": plan.action,
        },
    )
'''
proactive = proactive.replace(old_prepare, new_prepare + "\n", 1)
# Candidate: build plan once after parsing.
proactive = replace_once(
    proactive,
    '''            decision = _apply_persona_behavior_to_decision(\n                config, _decide_proactive_reply(raw)\n            )\n            trace_event(\n''',
    '''            decision = _apply_persona_behavior_to_decision(\n                config, _decide_proactive_reply(raw)\n            )\n            speech_plan = build_runtime_speech_plan(\n                text=decision.text,\n                segments=list(decision.segments),\n                persona=resolve_persona(config),\n                context=context,\n                source=scene,\n                action=decision.action,\n                target_user_id=decision.target_user_id,\n                suggested_topic=decision.topic,\n                reason=decision.reason,\n                confidence=decision.confidence,\n            )\n            trace_event(\n''',
    label="proactive candidate speech plan",
)
# No-speak decisions get the same explicit speech trace.
proactive = replace_once(
    proactive,
    '''            if not decision.should_speak:\n                # 内容门拦截：模型读懂对话后判定此刻不适合开口。\n''',
    '''            if not decision.should_speak:\n                trace_speech_decision(\n                    speech_plan,\n                    emotion_state=context.get("emotion_state"),\n                    participation_action=decision.action,\n                )\n                # 内容门拦截：模型读懂对话后判定此刻不适合开口。\n''',
    label="proactive skipped speech trace",
)
proactive = replace_once(
    proactive,
    "            history_text = decision.history_text\n",
    "            history_text = speech_plan.visible_text or decision.history_text\n",
    label="proactive history from plan",
)
proactive = replace_once(
    proactive,
    '''                prepared = await _prepare_proactive_message(\n                    decision, session=session, group_id=group_id\n                )\n''',
    '''                prepared = await _prepare_proactive_message(\n                    speech_plan,\n                    session=session,\n                    group_id=group_id,\n                    recent_speech=_recent_proactive_lines(config),\n                    emotion_state=context.get("emotion_state"),\n                )\n''',
    label="proactive prepare speech plan",
)
proactive = replace_once(
    proactive,
    '''            if decision.topic:\n                # 用模型提炼的真实话题更新 active_topic，让后续对话路径的\n                # 上下文不再停留在"上次触发消息的原文"。\n                config.active_topic = decision.topic[:240]\n''',
    '''            apply_speech_topic(config, speech_plan)\n''',
    label="proactive topic transition",
)
proactive = replace_once(
    proactive,
    "                    topic=decision.topic or str(config.active_topic or \"\"),\n",
    "                    topic=speech_plan.topic or str(config.active_topic or \"\"),\n",
    label="proactive conversation topic",
)
# Follow-up gets the same plan conversion. The marker occurs only in followup after first was changed.
proactive = replace_once(
    proactive,
    '''            decision = _apply_persona_behavior_to_decision(\n                config, _decide_proactive_reply(raw or "")\n            )\n            trace_event(\n''',
    '''            decision = _apply_persona_behavior_to_decision(\n                config, _decide_proactive_reply(raw or "")\n            )\n            speech_plan = build_runtime_speech_plan(\n                text=decision.text,\n                segments=list(decision.segments),\n                persona=resolve_persona(config),\n                context=context,\n                source="followup",\n                action=decision.action,\n                target_user_id=decision.target_user_id,\n                suggested_topic=decision.topic or batch.topic,\n                reason=decision.reason,\n                confidence=decision.confidence,\n            )\n            trace_event(\n''',
    label="followup speech plan",
)
proactive = replace_once(
    proactive,
    '''            if action != "speak":\n                session.add(\n''',
    '''            if action != "speak":\n                trace_speech_decision(\n                    speech_plan,\n                    emotion_state=context.get("emotion_state"),\n                    participation_action=action,\n                )\n                session.add(\n''',
    label="followup skipped speech trace",
)
proactive = replace_once(
    proactive,
    '''                prepared = await _prepare_proactive_message(\n                    decision, session=session, group_id=group_id\n                )\n''',
    '''                prepared = await _prepare_proactive_message(\n                    speech_plan,\n                    session=session,\n                    group_id=group_id,\n                    recent_speech=_recent_proactive_lines(config),\n                    emotion_state=context.get("emotion_state"),\n                )\n''',
    label="followup prepare speech plan",
)
proactive = replace_once(
    proactive,
    '''            if decision.topic:\n                config.active_topic = decision.topic[:240]\n''',
    '''            apply_speech_topic(config, speech_plan)\n''',
    label="followup topic transition",
)
proactive = replace_once(
    proactive,
    "                topic=decision.topic or batch.topic,\n",
    "                topic=speech_plan.topic or batch.topic,\n",
    label="followup conversation topic",
)
write(proactive_path, proactive)


# ---------------------------------------------------------------------------
# P11 + P10 backend: the existing dry-run becomes an explicit speech simulator.
# ---------------------------------------------------------------------------
web_backend_path = "src/plugins/yawn_core/webui/agent.py"
web_backend = read(web_backend_path)
web_backend = replace_once(
    web_backend,
    "from ..yawn_agent.dialogue import _history_message_meta, _load_context\n",
    "from ..yawn_agent.context_history import history_message_meta as _history_message_meta\nfrom ..yawn_agent.context_loader import load_context as _load_context\n",
    label="webui dialogue dependency cleanup",
)
# Insert speech runtime imports after prompt import line if present.
anchor = "from ..yawn_agent.prompt import "
index = web_backend.find(anchor)
if index < 0:
    raise RuntimeError("webui prompt import anchor missing")
line_end = web_backend.find("\n", index)
web_backend = web_backend[: line_end + 1] + (
    "from ..yawn_agent.speech_runtime import (\n"
    "    build_runtime_speech_plan,\n"
    "    speech_simulation_payload,\n"
    "    trace_speech_decision,\n"
    ")\n"
) + web_backend[line_end + 1 :]
# Add simulation variable after result_payload declaration.
web_backend = replace_once(
    web_backend,
    "        result_payload: dict[str, Any] | None = None\n        if body.run_model:\n",
    '''        result_payload: dict[str, Any] | None = None\n        preview_plan = build_runtime_speech_plan(\n            text="",\n            persona=applied_persona,\n            current_turn=current_turn,\n            context=context,\n            source=body.mode,\n            action="speak",\n        )\n        speech_simulation = speech_simulation_payload(\n            preview_plan,\n            emotion_state=applied_emotion,\n            should_speak=None,\n            preview_only=True,\n            user_text=current_turn.content,\n        )\n        if body.run_model:\n''',
    label="webui speech simulation preview",
)
# After model payload, derive final SpeechPlan before LLM trace event.
web_backend = replace_once(
    web_backend,
    '''                result_payload = _debug_model_payload(result, body.mode)\n                trace_event(\n                    "llm",\n''',
    '''                result_payload = _debug_model_payload(result, body.mode)\n                if body.mode == "dialogue":\n                    if not result_payload.get("toolCalls") and result_payload.get("text"):\n                        simulated_plan = build_runtime_speech_plan(\n                            text=result_payload.get("text") or "",\n                            persona=applied_persona,\n                            current_turn=current_turn,\n                            context=context,\n                            source="dialogue",\n                        )\n                        speech_simulation = speech_simulation_payload(\n                            simulated_plan,\n                            emotion_state=applied_emotion,\n                            should_speak=True,\n                            user_text=current_turn.content,\n                        )\n                        trace_speech_decision(\n                            simulated_plan,\n                            emotion_state=applied_emotion,\n                            participation_action="speak",\n                            trace=debug_trace,\n                        )\n                else:\n                    simulated_decision = _decide_proactive_reply(\n                        str(result_payload.get("text") or "")\n                    )\n                    simulated_plan = build_runtime_speech_plan(\n                        text=simulated_decision.text,\n                        segments=list(simulated_decision.segments),\n                        persona=applied_persona,\n                        current_turn=current_turn,\n                        context=context,\n                        source=body.mode,\n                        action=simulated_decision.action,\n                        target_user_id=simulated_decision.target_user_id,\n                        suggested_topic=simulated_decision.topic,\n                        reason=simulated_decision.reason,\n                        confidence=simulated_decision.confidence,\n                    )\n                    speech_simulation = speech_simulation_payload(\n                        simulated_plan,\n                        emotion_state=applied_emotion,\n                        should_speak=simulated_decision.should_speak,\n                        user_text=current_turn.content,\n                    )\n                    trace_speech_decision(\n                        simulated_plan,\n                        emotion_state=applied_emotion,\n                        participation_action=simulated_decision.action,\n                        trace=debug_trace,\n                    )\n                trace_event(\n                    "llm",\n''',
    label="webui final speech simulation",
)
# Expose field in response.
web_backend = replace_once(
    web_backend,
    '            "result": result_payload,\n            "executionTrace": debug_trace.as_dict(),\n',
    '            "result": result_payload,\n            "speechSimulation": speech_simulation,\n            "executionTrace": debug_trace.as_dict(),\n',
    label="webui simulation payload field",
)
write(web_backend_path, web_backend)


# Frontend types.
types_path = "webui/src/types.ts"
types = read(types_path)
interface_marker = "export interface AgentDebugResponse {\n"
speech_interface = r'''export interface AgentSpeechSimulation {
  status: "policy_only" | "final" | string;
  should_speak: boolean | null;
  action: string;
  scene: string;
  act: string;
  turn_pressure: string;
  target_user_id: string | number | null;
  reply_to_message_id: string | number | null;
  topic: string | null;
  topic_action: string;
  reason: string;
  confidence: number;
  text: string;
  segments: Array<Record<string, unknown>>;
  style: Record<string, unknown>;
  quality: Array<Record<string, unknown>>;
  emotion: unknown;
}

'''
if interface_marker not in types:
    raise RuntimeError("AgentDebugResponse interface marker missing")
types = types.replace(interface_marker, speech_interface + interface_marker, 1)
types = replace_once(
    types,
    "  warnings: string[];\n  executionTrace: AgentExecutionTrace;\n",
    "  warnings: string[];\n  speechSimulation: AgentSpeechSimulation;\n  executionTrace: AgentExecutionTrace;\n",
    label="AgentDebugResponse speechSimulation",
)
write(types_path, types)


# Frontend speech card and trace phase.
agent_tsx_path = "webui/src/agent.tsx"
agent_tsx = read(agent_tsx_path)
# Types are already imported in the file's existing grouped import via AgentDebugResponse, so
# the component can use AgentDebugResponse["speechSimulation"] without another import.
component_marker = "function DebugModelView({ result }: { result: AgentDebugResponse[\"result\"] }): React.JSX.Element {\n"
speech_component = r'''function DebugSpeechSimulation({ value }: { value: AgentDebugResponse["speechSimulation"] }): React.JSX.Element {
  const style = debugRecord(value.style);
  const quality = Array.isArray(value.quality) ? value.quality.map(debugRecord) : [];
  const segments = Array.isArray(value.segments) ? value.segments.map(debugRecord) : [];
  return <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
    <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 4 }} items={[
      { key: "speak", label: "是否发言", children: value.should_speak == null ? <Tag>仅策略预览</Tag> : <Tag color={value.should_speak ? "green" : "default"}>{value.should_speak ? "发言" : "保持沉默"}</Tag> },
      { key: "action", label: "参与动作", children: <Tag>{value.action}</Tag> },
      { key: "scene", label: "Speech Scene", children: <Tag color="blue">{value.scene}</Tag> },
      { key: "act", label: "话语动作", children: value.act },
      { key: "turn", label: "话轮压力", children: <Tag color={value.turn_pressure === "high" ? "orange" : undefined}>{value.turn_pressure}</Tag> },
      { key: "target", label: "目标成员", children: debugDisplay(value.target_user_id) },
      { key: "topic", label: "话题", children: debugDisplay(value.topic) },
      { key: "topicAction", label: "话题动作", children: value.topic_action },
    ]} />
    <Space wrap>
      <Tag>温暖 {debugDisplay(style.warmth)}</Tag>
      <Tag>幽默 {debugDisplay(style.humor)}</Tag>
      <Tag>直接 {debugDisplay(style.directness)}</Tag>
      <Tag>详略 {debugDisplay(style.verbosity)}</Tag>
      <Tag>表现力 {debugDisplay(style.expressiveness)}</Tag>
    </Space>
    {value.text ? <Card size="small" title="最终文本"><Paragraph style={{ marginBottom: 0 }}>{value.text}</Paragraph></Card> : <Text type="secondary">{value.status === "policy_only" ? "未调用模型；这里只预览发言策略，最终文本尚未生成。" : "本轮没有纯文本。"}</Text>}
    {segments.length > 0 && <Card size="small" title={`最终消息段 ${segments.length}`}><DebugRawBlock value={segments} /></Card>}
    {quality.length > 0 && <Space wrap><Text type="secondary">质量检查：</Text>{quality.map((item, index) => <Tag key={`${String(item.code ?? "quality")}-${index}`} color={item.autofixed ? "green" : "orange"}>{String(item.code ?? "quality")}{item.autofixed ? " · 已修正" : ""}</Tag>)}</Space>}
    {value.reason ? <Text type="secondary">决策理由：{value.reason}</Text> : null}
  </Space>;
}

'''
if component_marker not in agent_tsx:
    raise RuntimeError("DebugModelView marker missing")
agent_tsx = agent_tsx.replace(component_marker, speech_component + component_marker, 1)
# Human-readable trace summary for P10.
speech_trace_marker = "  if (event.phase === \"outbound\") {\n"
speech_trace_block = r'''  if (event.phase === "speech") {
    const quality = Array.isArray(output.quality) ? output.quality.map(debugRecord) : [];
    const style = debugRecord(output.style);
    return <Space orientation="vertical" size={4} style={{ width: "100%" }}>
      <Text>
        发言动作 <Text strong>{String(output.action ?? "speak")}</Text>，场景 <Text code>{String(output.scene ?? "conversation")}</Text>，
        话语动作 {String(output.act ?? "continue")}，话轮压力 {String(output.turn_pressure ?? "low")}。
      </Text>
      <Space wrap size={[6, 6]}>
        {output.target_user_id ? <Tag>目标 {String(output.target_user_id)}</Tag> : null}
        {output.topic ? <Tag color="blue">话题 {String(output.topic)}</Tag> : null}
        {output.topic_action ? <Tag>{String(output.topic_action)}</Tag> : null}
        {Object.keys(style).length > 0 ? <Tag>Persona style 已应用</Tag> : null}
        {output.emotion ? <Tag color="purple">Emotion 已应用</Tag> : null}
      </Space>
      {quality.length > 0 && <Text type="secondary">质量检查：{quality.map((item) => String(item.code ?? "quality")).join(" / ")}</Text>}
    </Space>;
  }

'''
if speech_trace_marker not in agent_tsx:
    raise RuntimeError("trace outbound marker missing")
agent_tsx = agent_tsx.replace(speech_trace_marker, speech_trace_block + speech_trace_marker, 1)
agent_tsx = replace_once(
    agent_tsx,
    '  tool: "工具",\n  outbound: "发送",\n',
    '  tool: "工具",\n  speech: "发言",\n  outbound: "发送",\n',
    label="trace speech phase label",
)
# Dedicated simulator card before debug details.
agent_tsx = replace_once(
    agent_tsx,
    '      <Card title="调试详情" className="agent-debug-detail-card">\n',
    '      <Card title="发言模拟器" extra={<Tag color="blue">Dry-run · 不发送</Tag>}>\n        <DebugSpeechSimulation value={result.speechSimulation} />\n      </Card>\n\n      <Card title="调试详情" className="agent-debug-detail-card">\n',
    label="speech simulator card",
)
agent_tsx = agent_tsx.replace(
    'message="执行追踪器"\n      description="这里同时提供无副作用调试 Trace',
    'message="执行追踪器 + 发言模拟器"\n      description="这里同时提供无副作用发言模拟、调试 Trace',
    1,
)
write(agent_tsx_path, agent_tsx)


# ---------------------------------------------------------------------------
# Tests + docs; these are the acceptance contract for the original P7-P12.
# ---------------------------------------------------------------------------
write(
    "tests/test_agent_speech_p7_p12_completion.py",
    r'''from __future__ import annotations

import sys
from pathlib import Path

import nonebot

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_agent_modules() -> None:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()
    if (
        nonebot.get_plugin("yawn_core") is None
        and nonebot.get_plugin("src.plugins.yawn_core") is None
    ):
        nonebot.load_from_toml("pyproject.toml")


def test_p7_fresh_topic_is_not_replaced_by_every_raw_followup() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.topic_state import (
        TOPIC_ACTION_CONTINUE,
        TopicState,
        resolve_topic_transition,
    )

    transition = resolve_topic_transition(
        TopicState(
            label="Docker 镜像拉取性能",
            status="fresh",
            continuity="continuing",
            age_minutes=1,
            message_count=4,
            participant_count=2,
        ),
        current_text="但是我已经换腾讯云了",
    )
    assert transition.action == TOPIC_ACTION_CONTINUE
    assert transition.label == "Docker 镜像拉取性能"


def test_p7_stale_topic_can_shift_without_storing_full_raw_message() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.topic_state import (
        TOPIC_ACTION_SHIFT,
        TopicState,
        resolve_topic_transition,
    )

    transition = resolve_topic_transition(
        TopicState("旧话题", "stale", "new", 45, 1, 1),
        current_text="对了，Windows 11 为什么一直有提示音？后面这段不应该全塞进 topic。",
    )
    assert transition.action == TOPIC_ACTION_SHIFT
    assert transition.label == "Windows 11 为什么一直有提示音"


def test_p8_tool_backed_reply_enters_tool_result_scene_with_evidence() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.speech_runtime import build_runtime_speech_plan
    from src.plugins.yawn_core.yawn_agent.tool_result_speech import build_speech_evidence

    evidence = build_speech_evidence(
        "list_group_members", {"ok": True, "result": {"items": [{}, {}]}}
    )
    assert evidence.item_count == 2  # noqa: PLR2004
    plan = build_runtime_speech_plan(
        text="群里现在有 2 个相关成员。",
        persona={"name": "Yawn"},
        current_turn={"content": "查一下群成员", "trigger": "mention"},
        context={"topic_state": {"status": "fresh", "continuity": "continuing"}},
        after_tool=True,
    )
    assert plan.scene == "tool_result"
    assert plan.act == "tool_report"


def test_p9_proactive_scene_builds_the_same_speech_plan_model() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.speech import SpeechPlan
    from src.plugins.yawn_core.yawn_agent.speech_runtime import build_runtime_speech_plan

    plan = build_runtime_speech_plan(
        text="这个确实有点离谱。",
        persona={"name": "Yawn"},
        context={"topic_state": {"label": "部署", "status": "fresh", "continuity": "continuing"}},
        source="active",
        action="speak",
        suggested_topic="部署",
    )
    assert isinstance(plan, SpeechPlan)
    assert plan.scene == "active_interject"
    assert plan.topic == "部署"


def test_p10_speech_trace_is_a_distinct_stage() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.execution_trace import (
        begin_execution_trace,
        bind_execution_trace,
        reset_execution_trace,
    )
    from src.plugins.yawn_core.yawn_agent.speech_runtime import (
        build_runtime_speech_plan,
        trace_speech_decision,
    )

    trace = begin_execution_trace(100, mode="dialogue", source="debug")
    token = bind_execution_trace(trace)
    try:
        plan = build_runtime_speech_plan(
            text="直接回答。",
            persona={"name": "Yawn"},
            current_turn={"content": "回答我", "trigger": "mention"},
            context={},
        )
        trace_speech_decision(plan, emotion_state={"label": "calm"})
    finally:
        reset_execution_trace(token)
    speech_events = [event for event in trace.events if event.phase == "speech"]
    assert len(speech_events) == 1
    assert speech_events[0].label == "发言决策"
    assert speech_events[0].output["scene"] == "direct_reply"


def test_p12_dialogue_no_longer_owns_context_activity_or_persistence() -> None:
    dialogue = (PROJECT_ROOT / "src/plugins/yawn_core/yawn_agent/dialogue.py").read_text(encoding="utf-8")
    proactive = (PROJECT_ROOT / "src/plugins/yawn_core/yawn_agent/proactive.py").read_text(encoding="utf-8")
    webui = (PROJECT_ROOT / "src/plugins/yawn_core/webui/agent.py").read_text(encoding="utf-8")

    assert "async def _load_context" not in dialogue
    assert "async def _activity_window_counts" not in dialogue
    assert "async def persist_bot_reply" not in dialogue
    assert "from .dialogue import (" not in proactive
    assert "yawn_agent.dialogue import _history_message_meta, _load_context" not in webui
    assert len(dialogue.splitlines()) < 1500  # final orchestration file must stay materially smaller
''',
)

write(
    "docs/agent-speech-p7-p12-completion.md",
    r'''# Agent Speech P7-P12 completion

This pass audits the original Speech Pipeline roadmap rather than later experimental numbering.

- **P7 topic judgement**: `topic_state.py` now owns `continue / shift / close`. A fresh active cluster keeps its semantic label instead of replacing `active_topic` with every raw user sentence. Existing proactive model topic output is reused when available; no extra model call is introduced.
- **P8 tool results**: each executed tool contributes bounded `SpeechEvidence`, and the final reply after any tool round is compiled as `scene=tool_result` before outbound delivery.
- **P9 proactive merge**: proactive/warmup/followup keep `SpeakDecision` / compatibility parsing, but user-visible content is converted to the same `SpeechPlan` and `prepare_speech_plan()` path as normal dialogue.
- **P10 trace**: `speech` is now a first-class execution phase showing action, scene, target, speech act, turn pressure, topic transition, Persona style, Emotion and quality checks.
- **P11 WebUI simulator**: the existing no-side-effect debug runner now exposes a dedicated `speechSimulation` result and a visible **发言模拟器** card. Model-off mode previews policy only; model-on mode shows final text/segments/quality without executing tools or sending QQ messages.
- **P12 cleanup**: reusable activity aggregation, context loading, send/persistence helpers and speech finalization leave `dialogue.py`. Thin compatibility names remain where existing tests/internal callers need them; new code imports the new modules directly.

No database migration or additional LLM request is added by this pass. OneBot validation and permission boundaries remain in `outbound.py` / tools.
''',
)

# Self-delete the temporary patch machinery so it cannot enter the PR.
for name in (
    "scripts/_agent_speech_p7_p12_refactor.py",
    ".github/workflows/_agent-speech-p7-p12-refactor.yml",
):
    target = path(name)
    if target.exists():
        target.unlink()
