# ruff: noqa: PLR0913
"""Common speech runtime shared by dialogue, proactive and WebUI dry-runs."""

from __future__ import annotations

from typing import Any

from .context_history import EffectiveTurn, effective_turn_from_context
from .execution_trace import trace_event
from .speech import (
    SPEECH_SCENE_TOOL_RESULT,
    SpeechPlan,
    SpeechStyle,
    SpeechTarget,
    speech_plan_from_segments,
    speech_plan_from_text,
)
from .speech_act import plan_speech_act
from .speech_policy import (
    classify_response_complexity,
    resolve_speech_scene,
    resolve_speech_style,
)
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


def _interaction_task_reason(effective: EffectiveTurn) -> str:
    kind = effective.interaction_kind
    if kind == "repair_ping":
        return "primary 是催促/未回应，support 中检测到对 Bot 表达的纠正；优先修复互动，不恢复更早任务。"
    if kind == "repair":
        return "当前主要内容是在纠正 Bot 的表达、理解或行为；本轮优先 repair。"
    if kind == "ping_ack":
        return "当前主要内容只是叫人、催促或确认 Bot 是否在线；先做短存在感回应。"
    if kind == "resume_task":
        return "纯触发恢复了最近一个仍可继续的用户任务；只恢复 resumed_task，不拼接其它历史。"
    return "当前消息本身包含有效语义正文，直接作为本轮 primary。"


def _interaction_media_reason(effective: EffectiveTurn) -> str:
    if effective.media_binding:
        if effective.media_message_ids:
            ids = ", ".join(str(item) for item in effective.media_message_ids)
            return f"primary 明确指向媒体，因此允许恢复该语义回合内的历史媒体候选：{ids}。"
        return "primary 明确指向媒体，因此允许媒体解析层恢复相关历史媒体。"
    if effective.interaction_kind in {"repair", "repair_ping", "ping_ack"}:
        return "这是互动修复/催促回合，media_binding=false；不会因为更早出现过图片就重新绑定图片。"
    return "primary 没有明确指向历史媒体，media_binding=false。"


def _interaction_length_reason(
    *,
    act: str,
    complexity: str,
    style: SpeechStyle,
) -> str:
    target = (
        f"{style.soft_target_chars} 字"
        if style.soft_target_chars is not None
        else "不设统一软字数"
    )
    return (
        f"长度由 act={act} + complexity={complexity} + Persona verbosity={style.verbosity} "
        f"共同决定；本轮软目标为 {target}。"
    )


def _interaction_plan_payload(
    effective: EffectiveTurn | None,
    *,
    act: str,
    complexity: str,
    style: SpeechStyle,
) -> dict[str, Any]:
    if effective is None:
        return {
            "kind": "proactive",
            "primary": "",
            "support": [],
            "resumed_task": None,
            "media_binding": False,
            "message_ids": [],
            "media_message_ids": [],
            "speech_act": act,
            "response_complexity": complexity,
            "soft_target_chars": style.soft_target_chars,
            "budget_basis": "act+complexity+persona",
            "why": {
                "task": "主动/续聊场景没有被动 Effective Turn；由主动参与策略决定本轮任务。",
                "media": "主动参与默认不从被动 Effective Turn 恢复历史媒体。",
                "length": _interaction_length_reason(
                    act=act,
                    complexity=complexity,
                    style=style,
                ),
            },
        }
    return {
        "kind": effective.interaction_kind,
        "primary": effective.primary,
        "support": list(effective.support),
        "resumed_task": effective.resumed_task,
        "media_binding": effective.media_binding,
        "message_ids": list(effective.message_ids),
        "media_message_ids": list(effective.media_message_ids),
        "speech_act": act,
        "response_complexity": complexity,
        "soft_target_chars": style.soft_target_chars,
        "budget_basis": "act+complexity+persona",
        "why": {
            "task": _interaction_task_reason(effective),
            "media": _interaction_media_reason(effective),
            "length": _interaction_length_reason(
                act=act,
                complexity=complexity,
                style=style,
            ),
        },
    }


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
    effective_turn = (
        effective_turn_from_context(current_turn, resolved_context)
        if current_turn is not None
        else None
    )
    act_plan = plan_speech_act(
        current_turn,
        scene=scene,
        effective_turn=effective_turn,
    )
    complexity = classify_response_complexity(
        current_turn,
        act=act_plan.act,
        effective_turn=effective_turn,
    )
    style = resolve_speech_style(
        persona,
        scene=scene,
        act=act_plan.act,
        complexity=complexity,
    )
    interaction_plan = _interaction_plan_payload(
        effective_turn,
        act=act_plan.act,
        complexity=complexity,
        style=style,
    )
    turn_plan = plan_turn_taking(current_turn, scene=scene, context=resolved_context)
    topic_state = topic_state_from_prompt(resolved_context.get("topic_state"))
    transition = resolve_topic_transition(
        topic_state,
        current_text=(
            effective_turn.primary
            if effective_turn is not None and effective_turn.primary
            else _turn_text(current_turn, str(text or ""))
        ),
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
        "interaction_plan": interaction_plan,
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
