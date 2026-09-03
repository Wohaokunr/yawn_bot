# ruff: noqa: E501
"""Scene, interaction and Persona policy for Agent speech.

This module is intentionally deterministic and zero-AI-cost. It tells the
model how to express an already-authorized turn; it never decides permissions
or executes OneBot actions.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from .context_history import (
    INTERACTION_PING_ACK,
    INTERACTION_REPAIR_PING,
    INTERACTION_RESUME_TASK,
    EffectiveTurn,
    effective_turn_from_context,
)
from .persona import persona_trait_label
from .speech import (
    SPEECH_SCENE_ACTIVE_INTERJECT,
    SPEECH_SCENE_CONVERSATION,
    SPEECH_SCENE_DIRECT_REPLY,
    SPEECH_SCENE_FALLBACK,
    SPEECH_SCENE_FOLLOWUP,
    SPEECH_SCENE_REACTION,
    SPEECH_SCENE_REPLY_THREAD,
    SPEECH_SCENE_TOOL_RESULT,
    SPEECH_SCENE_WARMUP,
    SpeechStyle,
    normalize_speech_scene,
)
from .speech_act import (
    SPEECH_ACT_ACKNOWLEDGE,
    SPEECH_ACT_ANSWER,
    SPEECH_ACT_CLOSE,
    SPEECH_ACT_PING_ACK,
    SPEECH_ACT_REACT,
    SPEECH_ACT_REPAIR,
    SPEECH_ACT_TOOL_REPORT,
    plan_speech_act,
    speech_act_instruction,
)
from .speech_native import native_expression_instruction, plan_native_expression
from .turn_taking import plan_turn_taking, turn_taking_instruction

if TYPE_CHECKING:
    from .context import CurrentTurn

_REACTION_AUTO_MIN = 2

RESPONSE_COMPLEXITY_SIMPLE = "simple"
RESPONSE_COMPLEXITY_NORMAL = "normal"
RESPONSE_COMPLEXITY_COMPLEX = "complex"

_SCENE_RULES: dict[str, str] = {
    SPEECH_SCENE_DIRECT_REPLY: (
        "这是明确呼叫。answer 需要完整解决当前问题，角色再安静也不能省掉必要事实、步骤或风险说明；"
        "repair/ping_ack 则以短互动为先。简单问题直接答，复杂问题可以展开，但不要先复述问题。"
    ),
    SPEECH_SCENE_REPLY_THREAD: (
        "这是回复链承接。优先回答被引用消息和当前发言人的真实问题；多人聊天时要保持对象清楚，"
        "不要把更早的话题当成当前问题。"
    ),
    SPEECH_SCENE_CONVERSATION: (
        "这是普通群聊承接。直接进入有信息量的回应；能一句说清就不要扩成小作文，"
        "需要解释时再自然展开。"
    ),
    SPEECH_SCENE_ACTIVE_INTERJECT: (
        "这是热闹聊天中的插话。只有已经决定开口时才输出，通常 1~2 句，回应一个具体观点或梗；"
        "不要总结整段聊天，也不要抢着给每个人答复。"
    ),
    SPEECH_SCENE_WARMUP: (
        "这是冷场暖场。只围绕自然切入点说 1~2 句，不用“大家在吗/在干嘛”之类签到式开场，"
        "不要把暖场写成主持词。"
    ),
    SPEECH_SCENE_FOLLOWUP: (
        "这是短会话续聊。必须承接新消息并带来新信息、明确回应或自然接梗；"
        "不要同义复述上一条，也不要靠结尾反问硬续话题。"
    ),
    SPEECH_SCENE_TOOL_RESULT: (
        "这是工具结果反馈。只依据真实执行结果说人话：成功就简洁确认关键结果，失败就说明失败原因；"
        "不要把工具 JSON、内部字段或后台措辞照搬给群友。"
    ),
    SPEECH_SCENE_REACTION: (
        "这是轻量反应场景。文字要极短；如果一个自然的 reaction/表情已经足够，就不要再补同义长句。"
    ),
    SPEECH_SCENE_FALLBACK: (
        "这是降级回复。只说明当前能确认的事实和下一步，不虚构已经完成的操作，也不要长篇解释内部故障。"
    ),
}

_SCENE_CONTINUE_TARGETS: dict[str, tuple[int | None, ...]] = {
    SPEECH_SCENE_DIRECT_REPLY: (100, 180, 300, 480, 720),
    SPEECH_SCENE_REPLY_THREAD: (90, 160, 260, 420, 640),
    SPEECH_SCENE_CONVERSATION: (70, 120, 200, 320, 480),
    SPEECH_SCENE_ACTIVE_INTERJECT: (36, 56, 80, 120, 160),
    SPEECH_SCENE_WARMUP: (40, 64, 90, 130, 180),
    SPEECH_SCENE_FOLLOWUP: (40, 68, 100, 150, 200),
    SPEECH_SCENE_TOOL_RESULT: (50, 90, 140, 220, 320),
    SPEECH_SCENE_REACTION: (12, 20, 32, 48, 64),
    SPEECH_SCENE_FALLBACK: (50, 90, 140, 220, 300),
}

_ACT_TARGETS: dict[str, tuple[int | None, ...]] = {
    SPEECH_ACT_PING_ACK: (18, 30, 40, 55, 70),
    SPEECH_ACT_REPAIR: (35, 55, 80, 110, 150),
    SPEECH_ACT_ACKNOWLEDGE: (18, 28, 42, 60, 80),
    SPEECH_ACT_REACT: (12, 20, 32, 48, 64),
    SPEECH_ACT_CLOSE: (18, 30, 50, 70, 100),
    SPEECH_ACT_TOOL_REPORT: (50, 90, 140, 220, 320),
}
_ANSWER_TARGETS: dict[str, tuple[int | None, ...]] = {
    RESPONSE_COMPLEXITY_SIMPLE: (60, 100, 160, 240, 360),
    RESPONSE_COMPLEXITY_NORMAL: (100, 180, 300, 480, 720),
    RESPONSE_COMPLEXITY_COMPLEX: (220, 420, 700, 1000, 1400),
}
_COMPLEX_HINT_RE = re.compile(
    r"(?:仔细|深入|详细|全面|系统|研究|分析|原因|根因|方案|计划|设计|架构|比较|对比|"
    r"证明|推导|论证|步骤|流程|实现|修复|排查|审计|优化|重构|为什么.+为什么)",
    re.IGNORECASE,
)
_LIST_HINT_RE = re.compile(r"(?:\n|[；;]|(?:^|\s)[一二三四五六七八九十\d]+[.、)])")
_COMPLEX_TEXT_CHARS = 80
_NORMAL_TEXT_CHARS = 40
_MULTI_SIGNAL_COUNT = 2
_COMPLEX_SCORE_THRESHOLD = 2
_SIMPLE_ANSWER_MAX_CHARS = 24


def _turn_payload(current_turn: CurrentTurn | dict[str, Any] | None) -> dict[str, Any]:
    if current_turn is None:
        return {}
    if isinstance(current_turn, dict):
        return dict(current_turn)
    prompt_dict = getattr(current_turn, "prompt_dict", None)
    if callable(prompt_dict):
        value = prompt_dict()
        return dict(value) if isinstance(value, dict) else {}
    as_dict = getattr(current_turn, "as_dict", None)
    if callable(as_dict):
        value = as_dict()
        return dict(value) if isinstance(value, dict) else {}
    return {}


def _effective_for_speech(
    current_turn: CurrentTurn | dict[str, Any] | None,
    context: dict[str, Any] | None,
) -> EffectiveTurn:
    if current_turn is None:
        return EffectiveTurn(primary="")
    payload = _turn_payload(current_turn)
    raw = payload.get("interaction")
    if isinstance(raw, dict):
        support = tuple(
            str(item)[:240]
            for item in list(raw.get("support") or [])[:2]
            if str(item).strip()
        )
        kind = str(raw.get("kind") or "direct").strip() or "direct"
        resumed_task = str(raw.get("resumed_task") or "").strip() or None
        primary = str(raw.get("primary") or payload.get("content") or "").strip()
        trigger_only = kind in {
            INTERACTION_PING_ACK,
            INTERACTION_REPAIR_PING,
            INTERACTION_RESUME_TASK,
        }
        return EffectiveTurn(
            primary=primary,
            support=support,
            resumed_task=resumed_task,
            media_binding=bool(raw.get("media_binding")),
            interaction_kind=kind,
            trigger_only=trigger_only,
            used_history=bool(support or resumed_task),
        )
    return effective_turn_from_context(current_turn, context)


def resolve_speech_scene(
    current_turn: CurrentTurn | dict[str, Any] | None = None,
    *,
    source: str | None = None,
) -> str:
    """Resolve an internal speech scene without adding new user-facing modes."""

    if source:
        normalized_source = normalize_speech_scene(source)
        if normalized_source != SPEECH_SCENE_CONVERSATION or source == "conversation":
            return normalized_source

    payload = _turn_payload(current_turn)
    if payload.get("reply_to"):
        return SPEECH_SCENE_REPLY_THREAD
    trigger = str(payload.get("trigger") or "").strip().lower()
    if "reply" in trigger:
        return SPEECH_SCENE_REPLY_THREAD
    if trigger in {
        "mention",
        "explicit_call",
        "explicit_wakeup",
        "at",
        "to_me",
        "debug_replay",
    }:
        return SPEECH_SCENE_DIRECT_REPLY
    return SPEECH_SCENE_CONVERSATION


def _persona_level(persona: dict[str, str], field: str, fallback: int) -> int:
    haystack = "；".join(
        str(persona.get(key) or "")
        for key in ("style_traits", "social_style")
    )
    for value in range(5):
        try:
            label = persona_trait_label(field, value)
        except ValueError:
            return fallback
        if label and label in haystack:
            return value
    return fallback


def classify_response_complexity(  # noqa: C901
    current_turn: CurrentTurn | dict[str, Any] | None,
    *,
    act: str,
    effective_turn: EffectiveTurn | None = None,
) -> str:
    """Estimate how much information the current conversational job actually needs."""

    if act in {
        SPEECH_ACT_PING_ACK,
        SPEECH_ACT_REPAIR,
        SPEECH_ACT_ACKNOWLEDGE,
        SPEECH_ACT_REACT,
        SPEECH_ACT_CLOSE,
    }:
        return RESPONSE_COMPLEXITY_SIMPLE
    if act == SPEECH_ACT_TOOL_REPORT:
        return RESPONSE_COMPLEXITY_NORMAL

    payload = _turn_payload(current_turn)
    text = (
        effective_turn.primary
        if effective_turn is not None and effective_turn.primary
        else str(payload.get("content") or "")
    )
    compact = " ".join(text.split())
    if not compact:
        return RESPONSE_COMPLEXITY_SIMPLE

    score = 0
    if len(compact) >= _COMPLEX_TEXT_CHARS:
        score += _COMPLEX_SCORE_THRESHOLD
    elif len(compact) >= _NORMAL_TEXT_CHARS:
        score += 1
    if _COMPLEX_HINT_RE.search(compact):
        score += _COMPLEX_SCORE_THRESHOLD
    if len(_LIST_HINT_RE.findall(text)) >= _MULTI_SIGNAL_COUNT:
        score += 1
    if compact.count("？") + compact.count("?") >= _MULTI_SIGNAL_COUNT:
        score += 1

    if score >= _COMPLEX_SCORE_THRESHOLD:
        return RESPONSE_COMPLEXITY_COMPLEX
    if act == SPEECH_ACT_ANSWER and len(compact) <= _SIMPLE_ANSWER_MAX_CHARS:
        return RESPONSE_COMPLEXITY_SIMPLE
    return RESPONSE_COMPLEXITY_NORMAL


def _soft_target_chars(
    *,
    scene: str,
    act: str,
    complexity: str,
    verbosity: int,
) -> int | None:
    index = max(0, min(4, int(verbosity)))
    if act == SPEECH_ACT_ANSWER:
        targets = _ANSWER_TARGETS.get(complexity, _ANSWER_TARGETS[RESPONSE_COMPLEXITY_NORMAL])
        return targets[index]
    if act in _ACT_TARGETS:
        return _ACT_TARGETS[act][index]
    return _SCENE_CONTINUE_TARGETS[normalize_speech_scene(scene)][index]


def resolve_speech_style(
    persona: dict[str, str] | None,
    *,
    scene: str,
    act: str = "continue",
    complexity: str = RESPONSE_COMPLEXITY_NORMAL,
) -> SpeechStyle:
    resolved = persona or {}
    normalized_scene = normalize_speech_scene(scene)
    warmth = _persona_level(resolved, "warmth", 2)
    humor = _persona_level(resolved, "humor", 1)
    directness = _persona_level(resolved, "directness", 2)
    verbosity = _persona_level(resolved, "verbosity", 1)
    expressiveness = _persona_level(resolved, "expressiveness", 1)
    reaction = _persona_level(resolved, "reaction_tendency", 2)
    return SpeechStyle(
        warmth=warmth,
        humor=humor,
        directness=directness,
        verbosity=verbosity,
        expressiveness=expressiveness,
        soft_target_chars=_soft_target_chars(
            scene=normalized_scene,
            act=act,
            complexity=complexity,
            verbosity=verbosity,
        ),
        response_complexity=complexity,
        allow_spontaneous_reaction=reaction >= _REACTION_AUTO_MIN,
    )


def _interaction_instruction(effective: EffectiveTurn) -> str:
    parts = [f"Effective Turn 类型={effective.interaction_kind}。"]
    if effective.support:
        parts.append("support 只用于理解 primary，不是第二个待回答任务。")
    if effective.resumed_task:
        parts.append("本轮已明确恢复一个未完成任务，可以继续完成 resumed_task。")
    elif effective.trigger_only:
        parts.append("本轮没有 resumed_task；不得因为历史里曾有问题就自行重新执行旧任务。")
    if effective.media_binding:
        parts.append("media_binding=true：仅本轮允许按媒体投影层恢复匹配的历史媒体。")
    else:
        parts.append("media_binding=false：不要把历史图片/截图重新解释成当前任务。")
    return "".join(parts)


def build_speech_instruction(
    persona: dict[str, str] | None,
    current_turn: CurrentTurn | dict[str, Any] | None = None,
    *,
    source: str | None = None,
    context: dict[str, Any] | None = None,
) -> str:
    scene = resolve_speech_scene(current_turn, source=source)
    effective = _effective_for_speech(current_turn, context)
    act_plan = plan_speech_act(current_turn, scene=scene, effective_turn=effective)
    complexity = classify_response_complexity(
        current_turn,
        act=act_plan.act,
        effective_turn=effective,
    )
    style = resolve_speech_style(
        persona,
        scene=scene,
        act=act_plan.act,
        complexity=complexity,
    )
    target = (
        f"本轮按话语动作与信息复杂度建议控制在约 {style.soft_target_chars} 字内；复杂问题以完整正确为先，字数只是软目标。"
        if style.soft_target_chars
        else "按话语动作与问题复杂度决定长度。"
    )
    style_rule = (
        f"角色表达参数：温暖度 {style.warmth}/4、幽默 {style.humor}/4、"
        f"直接度 {style.directness}/4、详略 {style.verbosity}/4、"
        f"表现力 {style.expressiveness}/4；响应复杂度={complexity}。{target}"
    )
    quality_rule = (
        "发言质量：不要用“好的/当然可以/没问题/我来帮你”作为无意义开场；"
        "不要先复述用户刚说的话；不要用“还需要我帮你吗/如果还有问题可以继续问”之类"
        "通用结尾强行续聊。只有真正缺少关键信息时才反问。"
    )
    act_rule = speech_act_instruction(act_plan)
    turn_rule = turn_taking_instruction(
        plan_turn_taking(current_turn, scene=scene, context=context)
    )
    native_rule = native_expression_instruction(
        plan_native_expression(current_turn, scene=scene, style=style)
    )
    interaction_rule = _interaction_instruction(effective)
    return (
        f"发言场景={scene}。{_SCENE_RULES[scene]}{interaction_rule}{act_rule}{turn_rule}"
        f"{style_rule}{quality_rule}{native_rule}"
    )


__all__ = [
    "RESPONSE_COMPLEXITY_COMPLEX",
    "RESPONSE_COMPLEXITY_NORMAL",
    "RESPONSE_COMPLEXITY_SIMPLE",
    "build_speech_instruction",
    "classify_response_complexity",
    "resolve_speech_scene",
    "resolve_speech_style",
]
