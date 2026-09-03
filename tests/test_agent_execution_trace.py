from __future__ import annotations

import sys
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROJECT_ROOT / "src" / "plugins" / "yawn_core"
PACKAGE = types.ModuleType("yawn_core")
PACKAGE.__path__ = [str(PLUGIN_ROOT)]  # pyright: ignore[reportAttributeAccessIssue]
sys.modules.setdefault("yawn_core", PACKAGE)
AGENT_PACKAGE = types.ModuleType("yawn_core.yawn_agent")
AGENT_PACKAGE.__path__ = [str(PLUGIN_ROOT / "yawn_agent")]  # pyright: ignore[reportAttributeAccessIssue]
sys.modules.setdefault("yawn_core.yawn_agent", AGENT_PACKAGE)

from yawn_core.yawn_agent.execution_trace import (
    begin_execution_trace,
    bind_execution_trace,
    clear_execution_traces,
    execution_trace_by_id,
    finish_execution_trace,
    recent_execution_trace_summaries,
    recent_execution_traces,
    reset_execution_trace,
    trace_event,
)


def test_execution_trace_is_bounded_redacted_and_stored_per_group() -> None:
    group_id = 987654
    clear_execution_traces(group_id)
    trace = begin_execution_trace(
        group_id,
        mode="dialogue",
        source="runtime",
        trigger_source="reply",
        actor_user_id=123,
        message_id=456,
    )
    token = bind_execution_trace(trace)
    try:
        trace_event(
            "media",
            "媒体准备",
            input={
                "url": "https://signed.example/image?token=secret",
                "path": "C:/private/photo.png",
                "file": "opaque-file-id",
                "nested": {"raw_payload": {"secret": "never expose"}},
                "note": (
                    "see https://signed.example/hidden?token=x "
                    "and C:/private/other.png and /var/cache/yawn/private.png"
                ),
                "text": "x" * 900,
            },
            output={"count": 1},
        )
        trace_event(
            "outbound",
            "OneBot 发送",
            status="unknown",
            output={"delivery_state": "unknown"},
            detail="TimeoutError: 回执不确定",
        )
    finally:
        finish_execution_trace(trace, outcome="completed")
        reset_execution_trace(token)

    stored = recent_execution_traces(group_id)
    assert len(stored) == 1
    payload = stored[0]
    assert payload["source"] == "runtime"
    assert payload["triggerSource"] == "reply"
    assert payload["messageId"] == "456"
    assert payload["durationMs"] is not None
    assert [event["phase"] for event in payload["events"]] == ["media", "outbound"]
    media_input = payload["events"][0]["input"]
    assert media_input["url"] == {
        "redacted": True,
        "kind": "url",
        "scheme": "https",
        "host": "signed.example",
        "suffix": None,
        "has_query": True,
        "query_keys": ["token"],
    }
    assert media_input["path"]["redacted"] is True
    assert media_input["path"]["kind"] == "path"
    assert media_input["path"]["platform"] == "windows"
    assert media_input["path"]["suffix"] == ".png"
    assert media_input["file"]["redacted"] is True
    assert media_input["file"]["kind"] == "file_ref"
    assert media_input["nested"]["raw_payload"] == {
        "redacted": True,
        "kind": "payload",
        "value_type": "dict",
        "key_count": 1,
        "keys": ["secret"],
    }
    assert len(str(media_input["text"])) <= 601  # noqa: PLR2004
    serialized = str(payload)
    assert "signed.example/image" not in serialized
    assert "token=secret" not in serialized
    assert "C:/private" not in serialized
    assert "/var/cache/yawn" not in serialized
    assert "never expose" not in serialized
    clear_execution_traces(group_id)


def test_execution_trace_without_active_scope_is_noop() -> None:
    assert trace_event("llm", "no active trace") is None


def test_execution_trace_collection_is_lightweight_and_detail_is_selective() -> None:
    group_id = 987655
    clear_execution_traces(group_id)

    completed = begin_execution_trace(group_id, mode="dialogue", source="runtime")
    trace_event("prompt", "Prompt 构建", trace=completed)
    finish_execution_trace(completed, outcome="completed")
    failed = begin_execution_trace(group_id, mode="dialogue", source="runtime")
    finish_execution_trace(failed, outcome="error")

    summaries = recent_execution_trace_summaries(group_id)
    assert [item["traceId"] for item in summaries] == [
        failed.trace_id,
        completed.trace_id,
    ]
    assert summaries[0]["eventCount"] == 0
    assert "events" not in summaries[0]
    failed_summaries = recent_execution_trace_summaries(group_id, status="failed")
    assert [item["traceId"] for item in failed_summaries] == [failed.trace_id]
    detail = execution_trace_by_id(group_id, completed.trace_id)
    assert detail is not None
    assert detail["events"][0]["phase"] == "prompt"
    assert execution_trace_by_id(group_id, "missing") is None
    clear_execution_traces(group_id)
