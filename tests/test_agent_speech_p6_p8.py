# ruff: noqa: PLR2004
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import nonebot

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_agent_modules() -> None:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()
    if (
        nonebot.get_plugin("yawn_core") is None
        and nonebot.get_plugin("src.plugins.yawn_core") is None
    ):
        nonebot.load_from_toml("pyproject.toml")


def test_p6_native_expression_uses_only_current_turn_targets() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.speech import SpeechStyle
    from src.plugins.yawn_core.yawn_agent.speech_native import (
        native_expression_instruction,
        plan_native_expression,
    )

    plan = plan_native_expression(
        {
            "user_id": 200,
            "trigger": "reply",
            "reply_to": {"message_id": 77, "user_id": 300},
            "mentions": [200, 999, 999],
        },
        scene="reply_thread",
        style=SpeechStyle(allow_spontaneous_reaction=True),
    )
    assert plan.reply_message_id == 77
    assert plan.mention_candidates == (999,)
    assert plan.preferred_modes == (
        "reply",
        "at_if_targeted",
        "reaction_if_sufficient",
        "text",
    )
    rendered = native_expression_instruction(plan)
    assert "被引用消息 77" in rendered
    assert "999" in rendered
    assert "不能只发表情" in rendered


def test_p6_direct_explicit_reply_never_replaces_answer_with_reaction() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.speech import SpeechStyle
    from src.plugins.yawn_core.yawn_agent.speech_native import plan_native_expression

    plan = plan_native_expression(
        {"user_id": 1, "trigger": "mention"},
        scene="direct_reply",
        style=SpeechStyle(allow_spontaneous_reaction=True),
    )
    assert plan.allow_reaction is False
    assert "reaction_if_sufficient" not in plan.preferred_modes


def test_p6_reply_router_opens_real_reaction_lookup_and_segment() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.tool_router import (
        select_dialogue_message_segment_types,
        select_dialogue_tool_names,
    )

    tools = select_dialogue_tool_names("这个呢", has_reply=True)
    assert {"send_message", "search_reactions", "react_to_message"} <= tools
    segments = select_dialogue_message_segment_types("这个呢", has_reply=True)
    assert {"text", "reply", "reaction"} <= segments


def test_p7_topic_state_keeps_only_latest_cluster_and_freshness() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.topic_state import build_topic_state

    state = build_topic_state(
        "Docker 部署",
        [
            {
                "message_id": 1,
                "user_id": 10,
                "text": "很早以前的话题",
                "minutes_ago": 90,
            },
            {
                "message_id": 2,
                "user_id": 20,
                "text": "镜像怎么这么慢",
                "minutes_ago": 4,
                "topic_break_before": True,
            },
            {
                "message_id": 3,
                "user_id": 30,
                "text": "新加坡源也慢吗",
                "minutes_ago": 1,
            },
        ],
    )
    payload = state.prompt_dict()
    assert payload["label"] == "Docker 部署"
    assert payload["status"] == "fresh"
    assert payload["continuity"] == "continuing"
    assert payload["message_count"] == 2
    assert payload["participant_count"] == 2
    assert payload["anchor_message_ids"] == [2, 3]


def test_p7_build_context_exposes_topic_state_as_authoritative_realtime_fact() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.context import ActivitySnapshot, build_context
    from src.plugins.yawn_core.yawn_agent.prompt import build_messages

    context = build_context(
        group_id=1,
        group_name="测试群",
        messages=[
            {
                "message_id": 12,
                "user_id": 2,
                "text": "继续说 Docker",
                "minutes_ago": 2,
            }
        ],
        members=[],
        memories=[],
        relations=[],
        activity=ActivitySnapshot(last_message_at=datetime(2026, 9, 1, 19, 0)),
        active_topic="Docker",
        reference_at=datetime(2026, 9, 1, 19, 2),
    )
    assert context["active_topic"] == "Docker"
    assert context["topic_state"]["status"] == "fresh"
    messages, _ = build_messages(
        persona={"name": "Yawn"},
        tools=[],
        context=context,
        user_prompt="继续",
        current_turn={"user_id": 2, "name": "A", "content": "继续", "trigger": "mention"},
    )
    system_text = "\n".join(str(item.get("content") or "") for item in messages if item["role"] == "system")
    assert '"topic_state"' in system_text
    assert "topic_state 是当前话题的权威结构化状态" in system_text


def test_p8_tool_guidance_requires_natural_language_projection() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.prompt import build_tool_guidance

    guidance = build_tool_guidance(
        [{"type": "function", "function": {"name": "get_group_info"}}]
    )
    assert "工具返回是后台事实" in guidance
    assert "不要照抄 JSON" in guidance
    assert "ok=false" in guidance


def test_p8_tool_result_hint_distinguishes_success_failure_and_unknown_delivery() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.tool_result_speech import tool_result_speech_hint

    assert "返回 2 项" in tool_result_speech_hint(
        "list_group_members",
        {"ok": True, "result": {"items": [{"id": 1}, {"id": 2}]}},
    )
    assert "未成功" in tool_result_speech_hint(
        "get_group_info", {"ok": False, "error": "当前 OneBot 不支持该操作"}
    )
    assert "回执不确定" in tool_result_speech_hint(
        "send_message",
        {"ok": True, "result": {"delivery_state": "unknown"}},
    )
