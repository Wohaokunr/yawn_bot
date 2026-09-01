# ruff: noqa: E501
"""Scene and Persona policy for Agent speech.

This module is intentionally deterministic and zero-AI-cost.  It tells the
model how to express an already-authorized turn; it never decides permissions
or executes OneBot actions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from .context import CurrentTurn

_REACTION_AUTO_MIN = 2

_SCENE_RULES: dict[str, str] = {
    SPEECH_SCENE_DIRECT_REPLY: (
        "这是明确呼叫。先完整解决当前问题；角色再安静也不能省掉必要事实、步骤或风险说明。"
        "简单问题直接答，复杂问题可以展开，但不要先复述问题。"
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

_SOFT_TARGETS: dict[str, tuple[int | None, ...]] = {
    # 明确回答只给软目标，不做硬截断；复杂问题必须允许完整回答。
    SPEECH_SCENE_DIRECT_REPLY: (160, 260, 420, 700, 1000),
    SPEECH_SCENE_REPLY_THREAD: (140, 240, 380, 620, 900),
    SPEECH_SCENE_CONVERSATION: (100, 180, 300, 480, 720),
    SPEECH_SCENE_ACTIVE_INTERJECT: (36, 56, 80, 120, 160),
    SPEECH_SCENE_WARMUP: (40, 64, 90, 130, 180),
    SPEECH_SCENE_FOLLOWUP: (40, 68, 100, 150, 200),
    SPEECH_SCENE_TOOL_RESULT: (50, 90, 140, 220, 320),
    SPEECH_SCENE_REACTION: (12, 20, 32, 48, 64),
    SPEECH_SCENE_FALLBACK: (50, 90, 140, 220, 300),
}


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


def resolve_speech_style(
    persona: dict[str, str] | None,
    *,
    scene: str,
) -> SpeechStyle:
    resolved = persona or {}
    normalized_scene = normalize_speech_scene(scene)
    warmth = _persona_level(resolved, "warmth", 2)
    humor = _persona_level(resolved, "humor", 1)
    directness = _persona_level(resolved, "directness", 2)
    verbosity = _persona_level(resolved, "verbosity", 1)
    expressiveness = _persona_level(resolved, "expressiveness", 1)
    reaction = _persona_level(resolved, "reaction_tendency", 2)
    targets = _SOFT_TARGETS[normalized_scene]
    return SpeechStyle(
        warmth=warmth,
        humor=humor,
        directness=directness,
        verbosity=verbosity,
        expressiveness=expressiveness,
        soft_target_chars=targets[verbosity],
        allow_spontaneous_reaction=reaction >= _REACTION_AUTO_MIN,
    )


def build_speech_instruction(
    persona: dict[str, str] | None,
    current_turn: CurrentTurn | dict[str, Any] | None = None,
    *,
    source: str | None = None,
) -> str:
    scene = resolve_speech_scene(current_turn, source=source)
    style = resolve_speech_style(persona, scene=scene)
    target = (
        f"本场景建议控制在约 {style.soft_target_chars} 字内；复杂问题以完整正确为先，字数只是软目标。"
        if style.soft_target_chars
        else "按问题复杂度决定长度。"
    )
    style_rule = (
        f"角色表达参数：温暖度 {style.warmth}/4、幽默 {style.humor}/4、"
        f"直接度 {style.directness}/4、详略 {style.verbosity}/4、"
        f"表现力 {style.expressiveness}/4。{target}"
    )
    quality_rule = (
        "发言质量：不要用“好的/当然可以/没问题/我来帮你”作为无意义开场；"
        "不要先复述用户刚说的话；不要用“还需要我帮你吗/如果还有问题可以继续问”之类"
        "通用结尾强行续聊。只有真正缺少关键信息时才反问。"
    )
    return f"发言场景={scene}。{_SCENE_RULES[scene]}{style_rule}{quality_rule}"


__all__ = [
    "build_speech_instruction",
    "resolve_speech_scene",
    "resolve_speech_style",
]
