"""Small per-turn helpers kept outside the main dialogue orchestrator."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .tool_router import normalize_tool_family

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


def trace_intake(
    normalized: NormalizedMessage,
    *,
    bot_id: int,
    started: float,
    enqueued_at: float | None,
) -> None:
    """把解析阶段与入口整形结果写进执行追踪。"""

    from .execution_trace import trace_event

    trace_shape = trace_message_shape(normalized, bot_id=bot_id)
    mention_fields = {
        "mentions": len(trace_shape["effective_mentions"]),
        "mention_bot": trace_shape["mention_bot"],
        "observed_mentions": trace_shape["observed_mentions"],
        "effective_mentions": trace_shape["effective_mentions"],
        "original_segment_types": trace_shape["original_segment_types"],
        "observed_segment_types": trace_shape["observed_segment_types"],
        "effective_segment_types": trace_shape["effective_segment_types"],
        "mention_stripped_for_prompt": trace_shape["mention_stripped_for_prompt"],
        "mention_recovered_from_trigger": trace_shape["mention_recovered_from_trigger"],
    }
    for stage in normalized.parse_trace:
        if not isinstance(stage, dict):
            continue
        stage_label = str(stage.get("label") or "消息解析")
        raw_stage_output = stage.get("output")
        stage_output: dict[str, Any] = (
            {str(key): value for key, value in raw_stage_output.items()}
            if isinstance(raw_stage_output, dict)
            else {}
        )
        if stage_label == "消息段归一化":
            stage_output.update(mention_fields)
        trace_event(
            "parse",
            stage_label,
            output=stage_output,
            duration_ms=(
                float(stage.get("duration_ms") or 0.0)
                if stage.get("duration_ms") is not None
                else None
            ),
        )
    trace_event(
        "intake",
        "消息归一化完成",
        output={
            "trigger_source": normalized.trigger_source or "explicit_call",
            "trigger_signals": dict(normalized.trigger_signals),
            "trigger": {
                "source": normalized.trigger_source or "explicit_call",
                "mention_bot": trace_shape["mention_bot"],
                "reply": bool(normalized.trigger_signals.get("reply")),
                "wake_word": bool(normalized.trigger_signals.get("wake_word")),
                "continuation": bool(normalized.trigger_signals.get("continuation")),
            },
            "merged_message_ids": list(normalized.merged_message_ids),
            "text_chars": len(normalized.plain_text),
            "text_preview": normalized.plain_text[:240],
            "segment_types": trace_shape["effective_segment_types"],
            "original_segment_types": trace_shape["original_segment_types"],
            "observed_segment_types": trace_shape["observed_segment_types"],
            "media": [
                {"type": item.get("type"), "source": item.get("source", "current")}
                for item in normalized.media_refs
            ],
            "reply_depth": len(normalized.reply_chain),
            "forward_nodes": len(normalized.forward_tree),
            "mentions": trace_shape["effective_mentions"],
            "observed_mentions": trace_shape["observed_mentions"],
            "mention_bot": trace_shape["mention_bot"],
            "mention_stripped_for_prompt": trace_shape["mention_stripped_for_prompt"],
            "mention_recovered_from_trigger": trace_shape[
                "mention_recovered_from_trigger"
            ],
            "truncated": normalized.truncated,
            "queue_wait_ms": (
                round(max(started - enqueued_at, 0.0) * 1000, 1)
                if enqueued_at is not None
                else None
            ),
        },
    )


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
                # “公告”和“notice”是同一个工具包：按归一后的族名判重，
                # 只传 family 的首次调用照常执行。
                "family": normalize_tool_family(args.get("family"))
                or str(args.get("family") or "").strip().casefold(),
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
