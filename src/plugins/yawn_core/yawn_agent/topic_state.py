"""Zero-AI-cost structured topic state derived from recent group context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_TOPIC_STALE_MINUTES = 30
_TOPIC_FRESH_MINUTES = 10


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


def _bounded_minutes(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(parsed, 0)


def _message_cluster(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not messages:
        return []
    boundary = 0
    for index, item in enumerate(messages):
        if index > 0 and bool(item.get("topic_break_before")):
            boundary = index
    return messages[boundary:]


def build_topic_state(
    active_topic: str | None,
    messages: list[dict[str, Any]],
) -> TopicState:
    """Build a bounded state that is more informative than one raw topic string."""

    cluster = _message_cluster(messages)
    label = str(active_topic or "").strip()[:240] or None
    latest_age = (
        _bounded_minutes(cluster[-1].get("minutes_ago")) if cluster else None
    )
    participant_ids = {
        int(item.get("user_id") or 0)
        for item in cluster
        if item.get("role") != "bot" and int(item.get("user_id") or 0) > 0
    }
    anchors: list[int] = []
    for item in cluster[-3:]:
        try:
            message_id = int(item.get("message_id") or 0)
        except (TypeError, ValueError):
            continue
        if message_id and message_id not in anchors:
            anchors.append(message_id)

    if not label and not cluster:
        status = "empty"
    elif latest_age is None:
        status = "unknown_age"
    elif latest_age >= _TOPIC_STALE_MINUTES:
        status = "stale"
    elif latest_age <= _TOPIC_FRESH_MINUTES:
        status = "fresh"
    else:
        status = "cooling"

    if not cluster:
        continuity = "none"
    elif len(cluster) == 1:
        continuity = "new"
    elif len(cluster) >= 4 and len(participant_ids) >= 2:
        continuity = "active_cluster"
    else:
        continuity = "continuing"

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
