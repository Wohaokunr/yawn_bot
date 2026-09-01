# ruff: noqa: PLR0911, PLR2004
"""Bounded topic state and deterministic topic transitions for Agent speech."""

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
    value = re.sub(
        r"^(?:但是|不过|然后|所以|那|这个|就是|话说|对了)[，,：:\s]*", "", value
    )
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
            return TopicTransition(
                TOPIC_ACTION_SHIFT, suggestion, "model_topic_without_anchor"
            )
        if topic_similarity(state.label, suggestion) >= _TOPIC_SIMILARITY_CONTINUE:
            return TopicTransition(
                TOPIC_ACTION_CONTINUE, state.label, "model_topic_matches"
            )
        return TopicTransition(TOPIC_ACTION_SHIFT, suggestion, "model_topic_changed")

    derived = _compact_topic_label(current_text)
    if (
        state.label
        and state.status not in {"stale", "empty"}
        and state.continuity
        in {
            "continuing",
            "active_cluster",
        }
    ):
        return TopicTransition(
            TOPIC_ACTION_CONTINUE, state.label, "recent_cluster_continues"
        )
    if (
        state.label
        and derived
        and topic_similarity(state.label, derived) >= _TOPIC_SIMILARITY_CONTINUE
    ):
        return TopicTransition(
            TOPIC_ACTION_CONTINUE, state.label, "current_turn_matches"
        )
    if derived and (
        not state.label
        or state.status == "stale"
        or state.continuity in {"none", "new"}
    ):
        return TopicTransition(TOPIC_ACTION_SHIFT, derived, "new_or_stale_topic")
    if state.label:
        return TopicTransition(
            TOPIC_ACTION_CONTINUE, state.label, "keep_existing_topic"
        )
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
