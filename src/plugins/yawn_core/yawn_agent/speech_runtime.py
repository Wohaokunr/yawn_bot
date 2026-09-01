# ruff: noqa: PLR0913
"""Common speech runtime shared by dialogue, proactive and WebUI dry-runs."""

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
    current_turn: Any = None,
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
    trace: Any = None,
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
            "should_speak": resolved.should_speak
            if should_speak is None
            else should_speak,
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
