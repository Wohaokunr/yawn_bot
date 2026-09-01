# ruff: noqa: E501,PLR2004
from __future__ import annotations

import sys
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


def test_p7_fresh_topic_is_not_replaced_by_every_raw_followup() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.topic_state import (
        TOPIC_ACTION_CONTINUE,
        TopicState,
        resolve_topic_transition,
    )

    transition = resolve_topic_transition(
        TopicState(
            label="Docker 镜像拉取性能",
            status="fresh",
            continuity="continuing",
            age_minutes=1,
            message_count=4,
            participant_count=2,
        ),
        current_text="但是我已经换腾讯云了",
    )
    assert transition.action == TOPIC_ACTION_CONTINUE
    assert transition.label == "Docker 镜像拉取性能"


def test_p7_stale_topic_can_shift_without_storing_full_raw_message() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.topic_state import (
        TOPIC_ACTION_SHIFT,
        TopicState,
        resolve_topic_transition,
    )

    transition = resolve_topic_transition(
        TopicState("旧话题", "stale", "new", 45, 1, 1),
        current_text="对了，Windows 11 为什么一直有提示音？后面这段不应该全塞进 topic。",
    )
    assert transition.action == TOPIC_ACTION_SHIFT
    assert transition.label == "Windows 11 为什么一直有提示音"


def test_p8_tool_backed_reply_enters_tool_result_scene_with_evidence() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.speech_runtime import (
        build_runtime_speech_plan,
    )
    from src.plugins.yawn_core.yawn_agent.tool_result_speech import (
        build_speech_evidence,
    )

    evidence = build_speech_evidence(
        "list_group_members", {"ok": True, "result": {"items": [{}, {}]}}
    )
    assert evidence.item_count == 2
    plan = build_runtime_speech_plan(
        text="群里现在有 2 个相关成员。",
        persona={"name": "Yawn"},
        current_turn={"content": "查一下群成员", "trigger": "mention"},
        context={"topic_state": {"status": "fresh", "continuity": "continuing"}},
        after_tool=True,
    )
    assert plan.scene == "tool_result"
    assert plan.act == "tool_report"


def test_p9_proactive_scene_builds_the_same_speech_plan_model() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.speech import SpeechPlan
    from src.plugins.yawn_core.yawn_agent.speech_runtime import (
        build_runtime_speech_plan,
    )

    plan = build_runtime_speech_plan(
        text="这个确实有点离谱。",
        persona={"name": "Yawn"},
        context={
            "topic_state": {
                "label": "部署",
                "status": "fresh",
                "continuity": "continuing",
            }
        },
        source="active",
        action="speak",
        suggested_topic="部署",
    )
    assert isinstance(plan, SpeechPlan)
    assert plan.scene == "active_interject"
    assert plan.topic == "部署"


def test_p10_speech_trace_is_a_distinct_stage() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.execution_trace import (
        begin_execution_trace,
        bind_execution_trace,
        reset_execution_trace,
    )
    from src.plugins.yawn_core.yawn_agent.speech_runtime import (
        build_runtime_speech_plan,
        trace_speech_decision,
    )

    trace = begin_execution_trace(100, mode="dialogue", source="debug")
    token = bind_execution_trace(trace)
    try:
        plan = build_runtime_speech_plan(
            text="直接回答。",
            persona={"name": "Yawn"},
            current_turn={"content": "回答我", "trigger": "mention"},
            context={},
        )
        trace_speech_decision(plan, emotion_state={"label": "calm"})
    finally:
        reset_execution_trace(token)
    speech_events = [event for event in trace.events if event.phase == "speech"]
    assert len(speech_events) == 1
    assert speech_events[0].label == "发言决策"
    assert speech_events[0].output["scene"] == "direct_reply"


def test_p12_dialogue_no_longer_owns_context_activity_or_persistence() -> None:
    dialogue = (
        PROJECT_ROOT / "src/plugins/yawn_core/yawn_agent/dialogue.py"
    ).read_text(encoding="utf-8")
    proactive = (
        PROJECT_ROOT / "src/plugins/yawn_core/yawn_agent/proactive.py"
    ).read_text(encoding="utf-8")
    webui = (PROJECT_ROOT / "src/plugins/yawn_core/webui/agent.py").read_text(
        encoding="utf-8"
    )

    assert "async def _load_context" not in dialogue
    assert "async def _activity_window_counts" not in dialogue
    assert "async def persist_bot_reply" not in dialogue
    assert "from .dialogue import (" not in proactive
    assert (
        "yawn_agent.dialogue import _history_message_meta, _load_context" not in webui
    )
    assert (
        len(dialogue.splitlines()) < 1500
    )  # final orchestration file must stay materially smaller
