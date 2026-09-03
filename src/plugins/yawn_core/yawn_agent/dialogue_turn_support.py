"""Small per-turn helpers kept outside the main dialogue orchestrator."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .message_parser import NormalizedMessage

MAX_DISCOVERY_ATTEMPTS = 2


def semantic_query_text(normalized: Any) -> str:
    semantic = getattr(normalized, "semantic_query_text", None)
    if callable(semantic):
        return str(semantic())
    prompt = getattr(normalized, "prompt_text", None)
    return str(prompt()) if callable(prompt) else ""


def trace_message_shape(
    normalized: "NormalizedMessage",
    *,
    bot_id: int,
) -> dict[str, Any]:
    """Expose raw parser observations and effective @ semantics side by side."""

    observed_types: list[str] = []
    for item in normalized.segments:
        segment_type = str(item.type or "").strip()
        if not segment_type:
            continue
        if segment_type == "text" and not str(
            item.data.get("text") or item.text or ""
        ).strip():
            continue
        if segment_type not in observed_types:
            observed_types.append(segment_type)

    observed_mentions: list[int] = []
    for raw in normalized.mentions:
        try:
            user_id = int(raw)
        except (TypeError, ValueError):
            continue
        if user_id > 0 and user_id not in observed_mentions:
            observed_mentions.append(user_id)

    mention_bot = bool(normalized.trigger_signals.get("mention"))
    effective_mentions = list(observed_mentions)
    if mention_bot and bot_id > 0 and bot_id not in effective_mentions:
        effective_mentions.append(bot_id)

    effective_types = list(observed_types)
    if mention_bot and "at" not in effective_types:
        effective_types.append("at")

    mention_recovered = mention_bot and (
        bot_id not in observed_mentions or "at" not in observed_types
    )
    return {
        "mention_bot": mention_bot,
        "original_segment_types": observed_types,
        "observed_segment_types": observed_types,
        "effective_segment_types": effective_types,
        "observed_mentions": observed_mentions,
        "effective_mentions": effective_mentions,
        "mention_stripped_for_prompt": mention_bot and "at" not in observed_types,
        "mention_recovered_from_trigger": mention_recovered,
    }


@dataclass(slots=True)
class DiscoveryRoundGuard:
    """Bound progressive tool discovery and stop no-op discovery loops."""

    known_names: set[str]
    attempts: int = 0
    fingerprints: set[str] = field(default_factory=set)
    disabled: bool = False

    def preflight(self, args: dict[str, Any]) -> dict[str, Any] | None:
        fingerprint = json.dumps(
            {
                "query": str(args.get("query") or "").strip().casefold(),
                "family": str(args.get("family") or "").strip().casefold(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        if fingerprint in self.fingerprints or self.attempts >= MAX_DISCOVERY_ATTEMPTS:
            self.disabled = True
            return {
                "ok": True,
                "result": {
                    "mode": "guarded",
                    "tools": [],
                    "toolpacks": [],
                    "count": 0,
                    "no_new_tools": True,
                    "message": (
                        "本回合没有新的工具可加载；请使用已加载工具或直接回答，"
                        "不要再次调用 discover_tools。"
                    ),
                },
            }
        self.attempts += 1
        self.fingerprints.add(fingerprint)
        return None

    def observe(self, discovery: Any) -> tuple[set[str], bool]:
        rows = discovery.get("tools", []) if isinstance(discovery, dict) else []
        returned_names: set[str] = set()
        requires_admin = False
        for item in rows:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            returned_names.add(str(item["name"]))
            if str(item.get("permission") or "") in {"privileged", "critical"}:
                requires_admin = True

        new_names = returned_names - self.known_names
        self.known_names.update(returned_names)
        if not new_names or self.attempts >= MAX_DISCOVERY_ATTEMPTS:
            self.disabled = True
            if isinstance(discovery, dict):
                discovery["no_new_tools"] = not bool(new_names)
                discovery["message"] = (
                    "没有发现新的工具；请使用已加载工具或直接回答，"
                    "不要再次调用 discover_tools。"
                    if not new_names
                    else "已达到本回合工具发现上限；请使用已加载工具或直接回答。"
                )
        return new_names, requires_admin
