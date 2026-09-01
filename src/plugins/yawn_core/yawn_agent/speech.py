"""Unified user-visible speech plan shared by dialogue and proactive paths.

A SpeechPlan describes *what the Agent intends to say* before OneBot-specific
validation and delivery.  It deliberately contains no raw CQ/OneBot payload;
`outbound.py` remains the only protocol boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

SPEECH_SCENE_DIRECT_REPLY = "direct_reply"
SPEECH_SCENE_REPLY_THREAD = "reply_thread"
SPEECH_SCENE_CONVERSATION = "conversation"
SPEECH_SCENE_ACTIVE_INTERJECT = "active_interject"
SPEECH_SCENE_WARMUP = "warmup"
SPEECH_SCENE_FOLLOWUP = "followup"
SPEECH_SCENE_TOOL_RESULT = "tool_result"
SPEECH_SCENE_REACTION = "reaction"
SPEECH_SCENE_FALLBACK = "fallback"

SPEECH_SCENES = frozenset(
    {
        SPEECH_SCENE_DIRECT_REPLY,
        SPEECH_SCENE_REPLY_THREAD,
        SPEECH_SCENE_CONVERSATION,
        SPEECH_SCENE_ACTIVE_INTERJECT,
        SPEECH_SCENE_WARMUP,
        SPEECH_SCENE_FOLLOWUP,
        SPEECH_SCENE_TOOL_RESULT,
        SPEECH_SCENE_REACTION,
        SPEECH_SCENE_FALLBACK,
    }
)


def normalize_speech_scene(value: object) -> str:
    scene = str(value or "").strip().lower()
    aliases = {
        "active": SPEECH_SCENE_ACTIVE_INTERJECT,
        "proactive_active": SPEECH_SCENE_ACTIVE_INTERJECT,
        "proactive_interject": SPEECH_SCENE_ACTIVE_INTERJECT,
        "proactive_warmup": SPEECH_SCENE_WARMUP,
        "short_conversation": SPEECH_SCENE_FOLLOWUP,
        "tool": SPEECH_SCENE_TOOL_RESULT,
    }
    scene = aliases.get(scene, scene)
    return scene if scene in SPEECH_SCENES else SPEECH_SCENE_CONVERSATION


@dataclass(frozen=True, slots=True)
class SpeechTarget:
    """Known current-group target facts; never an arbitrary model identifier."""

    user_id: int | None = None
    reply_to_message_id: int | None = None


@dataclass(frozen=True, slots=True)
class SpeechStyle:
    """Bounded Persona-derived voice controls for one speech scene."""

    warmth: int = 2
    humor: int = 1
    directness: int = 2
    verbosity: int = 1
    expressiveness: int = 1
    soft_target_chars: int | None = None
    allow_spontaneous_reaction: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "warmth": self.warmth,
            "humor": self.humor,
            "directness": self.directness,
            "verbosity": self.verbosity,
            "expressiveness": self.expressiveness,
            "softTargetChars": self.soft_target_chars,
            "allowSpontaneousReaction": self.allow_spontaneous_reaction,
        }


@dataclass(frozen=True, slots=True)
class SpeechQualityIssue:
    code: str
    detail: str
    severity: str = "info"
    autofixed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "detail": self.detail,
            "severity": self.severity,
            "autofixed": self.autofixed,
        }


@dataclass(frozen=True, slots=True)
class SpeechPlan:
    """Protocol-independent plan for one user-visible Agent utterance."""

    action: str = "speak"
    scene: str = SPEECH_SCENE_CONVERSATION
    text: str = ""
    segments: tuple[dict[str, Any], ...] = ()
    target: SpeechTarget = SpeechTarget()
    style: SpeechStyle = SpeechStyle()
    reason: str = ""
    confidence: float = 1.0
    issues: tuple[SpeechQualityIssue, ...] = ()

    @property
    def should_speak(self) -> bool:
        return self.action == "speak" and (bool(self.text.strip()) or bool(self.segments))

    @property
    def visible_text(self) -> str:
        if self.text.strip():
            return self.text.strip()
        return "".join(
            str(item.get("text") or "")
            for item in self.segments
            if str(item.get("type") or "").strip().lower() == "text"
        ).strip()

    def with_content(
        self,
        *,
        text: str | None = None,
        segments: tuple[dict[str, Any], ...] | None = None,
        issues: tuple[SpeechQualityIssue, ...] | None = None,
    ) -> SpeechPlan:
        return replace(
            self,
            text=self.text if text is None else text,
            segments=self.segments if segments is None else segments,
            issues=self.issues if issues is None else issues,
        )

    def trace_payload(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "scene": self.scene,
            "target_user_id": self.target.user_id,
            "reply_to_message_id": self.target.reply_to_message_id,
            "text_chars": len(self.visible_text),
            "segment_types": [
                str(item.get("type") or "") for item in self.segments
            ],
            "style": self.style.as_dict(),
            "quality": [item.as_dict() for item in self.issues],
            "confidence": max(0.0, min(float(self.confidence), 1.0)),
        }


def speech_plan_from_text(
    text: object,
    *,
    scene: object = SPEECH_SCENE_CONVERSATION,
    style: SpeechStyle | None = None,
    target: SpeechTarget | None = None,
    reason: str = "",
    confidence: float = 1.0,
) -> SpeechPlan:
    return SpeechPlan(
        scene=normalize_speech_scene(scene),
        text=str(text or ""),
        style=style or SpeechStyle(),
        target=target or SpeechTarget(),
        reason=str(reason or "")[:240],
        confidence=max(0.0, min(float(confidence), 1.0)),
    )


def speech_plan_from_segments(
    segments: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    scene: object = SPEECH_SCENE_CONVERSATION,
    style: SpeechStyle | None = None,
    target: SpeechTarget | None = None,
    reason: str = "",
    confidence: float = 1.0,
) -> SpeechPlan:
    safe_segments = tuple(dict(item) for item in segments if isinstance(item, dict))
    return SpeechPlan(
        scene=normalize_speech_scene(scene),
        segments=safe_segments,
        style=style or SpeechStyle(),
        target=target or SpeechTarget(),
        reason=str(reason or "")[:240],
        confidence=max(0.0, min(float(confidence), 1.0)),
    )


__all__ = [
    "SPEECH_SCENE_ACTIVE_INTERJECT",
    "SPEECH_SCENE_CONVERSATION",
    "SPEECH_SCENE_DIRECT_REPLY",
    "SPEECH_SCENE_FALLBACK",
    "SPEECH_SCENE_FOLLOWUP",
    "SPEECH_SCENE_REACTION",
    "SPEECH_SCENE_REPLY_THREAD",
    "SPEECH_SCENE_TOOL_RESULT",
    "SPEECH_SCENE_WARMUP",
    "SPEECH_SCENES",
    "SpeechPlan",
    "SpeechQualityIssue",
    "SpeechStyle",
    "SpeechTarget",
    "normalize_speech_scene",
    "speech_plan_from_segments",
    "speech_plan_from_text",
]
