"""Zero-AI-cost structured topic state derived from recent group context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_TOPIC_STALE_MINUTES = 30
_TOPIC_FRESH_MINUTES = 10
_ACTIVE_CLUSTER_MIN_MESSAGES = 4
_ACTIVE_CLUSTER_MIN_PARTICIPANTS = 2


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


__all__ = ["TopicState", "build_topic_state"]
