"""Zero-AI-cost group turn-taking guidance for Agent speech.

This module never decides authorization or proactive participation. It only
reduces monopolizing behavior after a turn has already been selected by the
existing dialogue/proactive control plane.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .speech import (
    SPEECH_SCENE_DIRECT_REPLY,
    SPEECH_SCENE_REPLY_THREAD,
    normalize_speech_scene,
)

if TYPE_CHECKING:
    from .context import CurrentTurn

TURN_PRESSURE_LOW = "low"
TURN_PRESSURE_MEDIUM = "medium"
TURN_PRESSURE_HIGH = "high"

_RECENT_WINDOW = 6
_HIGH_BOT_TURNS = 2
_MIN_BUSY_PARTICIPANTS = 2
_EXPLICIT_TRIGGERS = frozenset(
    {"mention", "explicit_call", "explicit_wakeup", "at", "to_me", "reply"}
)


@dataclass(frozen=True, slots=True)
class TurnTakingPlan:
    pressure: str
    explicit_turn: bool
    recent_bot_turns: int
    participant_count: int
    prefer_brief: bool
    avoid_followup_question: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "pressure": self.pressure,
            "explicit_turn": self.explicit_turn,
            "recent_bot_turns": self.recent_bot_turns,
            "participant_count": self.participant_count,
            "prefer_brief": self.prefer_brief,
            "avoid_followup_question": self.avoid_followup_question,
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
    return {}


def _recent_bot_turns(context: dict[str, Any]) -> int:
    raw_messages = context.get("messages")
    if not isinstance(raw_messages, list):
        return 0
    count = 0
    for item in raw_messages[-_RECENT_WINDOW:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        sender_kind = str(item.get("sender_kind") or "").strip().lower()
        is_bot = (
            role in {"bot", "assistant"}
            or sender_kind == "bot"
            or item.get("is_bot") is True
        )
        if is_bot:
            count += 1
    return count


def _participant_count(context: dict[str, Any]) -> int:
    topic_state = context.get("topic_state")
    if isinstance(topic_state, dict):
        raw = topic_state.get("participant_count")
        if isinstance(raw, int) and not isinstance(raw, bool):
            return max(raw, 0)
    raw_messages = context.get("messages")
    if not isinstance(raw_messages, list):
        return 0
    participants: set[int] = set()
    for item in raw_messages[-_RECENT_WINDOW:]:
        if not isinstance(item, dict):
            continue
        user_id = item.get("user_id")
        if isinstance(user_id, int) and not isinstance(user_id, bool) and user_id > 0:
            participants.add(user_id)
    return len(participants)


def plan_turn_taking(
    current_turn: CurrentTurn | dict[str, Any] | None,
    *,
    scene: str,
    context: dict[str, Any] | None = None,
) -> TurnTakingPlan:
    normalized_scene = normalize_speech_scene(scene)
    payload = _turn_payload(current_turn)
    trigger = str(payload.get("trigger") or "").strip().lower()
    explicit_turn = (
        normalized_scene in {SPEECH_SCENE_DIRECT_REPLY, SPEECH_SCENE_REPLY_THREAD}
        or trigger in _EXPLICIT_TRIGGERS
    )
    resolved_context = context or {}
    recent_bot_turns = _recent_bot_turns(resolved_context)
    participant_count = _participant_count(resolved_context)

    if explicit_turn:
        pressure = TURN_PRESSURE_LOW
    elif (
        recent_bot_turns >= _HIGH_BOT_TURNS
        and participant_count >= _MIN_BUSY_PARTICIPANTS
    ):
        pressure = TURN_PRESSURE_HIGH
    elif recent_bot_turns >= 1:
        pressure = TURN_PRESSURE_MEDIUM
    else:
        pressure = TURN_PRESSURE_LOW

    return TurnTakingPlan(
        pressure=pressure,
        explicit_turn=explicit_turn,
        recent_bot_turns=recent_bot_turns,
        participant_count=participant_count,
        prefer_brief=pressure == TURN_PRESSURE_HIGH,
        avoid_followup_question=pressure != TURN_PRESSURE_LOW and not explicit_turn,
    )


def turn_taking_instruction(plan: TurnTakingPlan) -> str:
    if plan.explicit_turn:
        return (
            "轮次=明确交给 Bot：正常完整回答当前发言人，"
            "不因群里之前 Bot 说过话而故意省略必要信息。"
        )
    if plan.pressure == TURN_PRESSURE_HIGH:
        return (
            "轮次压力=high：Bot 近期已参与较多且多人正在交谈；"
            "只回应一个最相关点，优先短句/轻反应，"
            "不要逐人点名作答，也不要用新问题继续占住话轮。"
        )
    if plan.pressure == TURN_PRESSURE_MEDIUM:
        return (
            "轮次压力=medium：保持克制，只在当前点上补充有价值内容；没有必要就不要追加反问。"
        )
    return "轮次压力=low：按当前问题自然回应，但仍不要把群聊写成连续独白。"


__all__ = [
    "TURN_PRESSURE_HIGH",
    "TURN_PRESSURE_LOW",
    "TURN_PRESSURE_MEDIUM",
    "TurnTakingPlan",
    "plan_turn_taking",
    "turn_taking_instruction",
]
