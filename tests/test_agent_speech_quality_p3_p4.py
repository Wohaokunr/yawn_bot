from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import nonebot

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_modules() -> dict[str, Any]:
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
    from src.plugins.yawn_core.yawn_agent import speech_quality, speech_runtime
    from src.plugins.yawn_core.yawn_agent.speech import (
        SpeechStyle,
        speech_plan_from_text,
    )
    from src.plugins.yawn_core.yawn_agent.speech_act import (
        SPEECH_ACT_REPAIR,
    )
    from src.plugins.yawn_core.yawn_agent.speech_scorecard import score_speech_output

    return {
        "speech_quality": speech_quality,
        "speech_runtime": speech_runtime,
        "SpeechStyle": SpeechStyle,
        "speech_plan_from_text": speech_plan_from_text,
        "SPEECH_ACT_REPAIR": SPEECH_ACT_REPAIR,
        "score_speech_output": score_speech_output,
    }


def _text_message(
    message_id: int,
    user_id: int,
    text: str,
    *,
    minutes_ago: int,
    role: str = "member",
) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "user_id": user_id,
        "text": text,
        "role": role,
        "minutes_ago": minutes_ago,
    }


def test_quality_catches_answer_strategy_meta_and_removes_safe_prefix() -> None:
    modules = _load_modules()
    plan = modules["speech_plan_from_text"](
        "我先看一眼这张图，顺便说一句，这个确实有点离谱。",
        scene="direct_reply",
        act=modules["SPEECH_ACT_REPAIR"],
        style=modules["SpeechStyle"](soft_target_chars=55),
    )

    final = modules["speech_quality"].finalize_speech_plan(plan)

    issues = {item.code: item for item in final.issues}
    assert "answer_strategy_meta" in issues
    assert issues["answer_strategy_meta"].autofixed is True
    assert not final.text.startswith("我先看一眼")


def test_quality_removes_forced_binary_followup_from_repair() -> None:
    modules = _load_modules()
    plan = modules["speech_plan_from_text"](
        "在呢，刚才没接上。你就说想让我干嘛吧，是要我夸它离谱，还是想听我吐槽？",
        scene="direct_reply",
        act=modules["SPEECH_ACT_REPAIR"],
        style=modules["SpeechStyle"](soft_target_chars=55),
    )

    final = modules["speech_quality"].finalize_speech_plan(plan)

    issues = {item.code: item for item in final.issues}
    assert "forced_choice_followup" in issues
    assert issues["forced_choice_followup"].autofixed is True
    assert "还是想听我吐槽" not in final.text
    assert final.text == "在呢，刚才没接上。"


def test_quality_flags_and_trims_short_social_monologue() -> None:
    modules = _load_modules()
    content = "在呢，刚才没接上。" + "我知道你是在说刚才那句太端着了。" * 12
    plan = modules["speech_plan_from_text"](
        content,
        scene="direct_reply",
        act=modules["SPEECH_ACT_REPAIR"],
        style=modules["SpeechStyle"](soft_target_chars=55),
    )

    final = modules["speech_quality"].finalize_speech_plan(plan)

    issues = {item.code: item for item in final.issues}
    assert "social_monologue" in issues
    assert issues["social_monologue"].autofixed is True
    assert len(final.text) <= 88


def test_scorecard_penalizes_meta_strategy_in_repair() -> None:
    modules = _load_modules()
    result = modules["score_speech_output"](
        "那我这次就不整那些花里胡哨的描述了，我直接自然点说。",
        scene="direct_reply",
        expected_act=modules["SPEECH_ACT_REPAIR"],
    )

    assert "answer_strategy_meta" in result.quality_codes
    assert result.score < 80


def test_interaction_plan_explains_repair_ping_and_blocks_old_media() -> None:
    modules = _load_modules()
    current_turn = {
        "message_id": 104,
        "user_id": 20001,
        "name": "用户",
        "content": "[用户仅@机器人，没有附加正文]",
        "trigger": "mention",
    }
    context = {
        "messages": [
            {
                "message_id": 100,
                "user_id": 20001,
                "text": "[图片]",
                "media_types": ["image"],
                "minutes_ago": 5,
            },
            _text_message(
                101,
                50001,
                "上一轮已经回答过图片",
                minutes_ago=3,
                role="bot",
            ),
            _text_message(102, 20001, "你这个AI味有点大呀", minutes_ago=2),
            _text_message(103, 20001, "你怎么不说话？", minutes_ago=1),
        ]
    }

    plan = modules["speech_runtime"].build_runtime_speech_plan(
        text="在呢，刚才没接上。",
        persona={},
        current_turn=current_turn,
        context=context,
        source="dialogue",
    )
    interaction = plan.interaction_plan

    assert interaction["kind"] == "repair_ping"
    assert interaction["primary"] == "你怎么不说话？"
    assert interaction["support"] == ["你这个AI味有点大呀"]
    assert interaction["resumed_task"] is None
    assert interaction["media_binding"] is False
    assert interaction["media_message_ids"] == []
    assert interaction["speech_act"] == "repair"
    assert interaction["soft_target_chars"] is not None
    assert "不会因为更早出现过图片" in interaction["why"]["media"]
    assert "act=repair" in interaction["why"]["length"]


def test_interaction_plan_explains_media_resume_and_budget() -> None:
    modules = _load_modules()
    current_turn = {
        "message_id": 103,
        "user_id": 20001,
        "name": "用户",
        "content": "[用户仅@机器人，没有附加正文]",
        "trigger": "mention",
    }
    context = {
        "messages": [
            {
                "message_id": 101,
                "user_id": 20001,
                "text": "[图片]",
                "media_types": ["image"],
                "minutes_ago": 1,
            },
            _text_message(102, 20001, "这张图片怎么样", minutes_ago=1),
        ]
    }

    plan = modules["speech_runtime"].build_runtime_speech_plan(
        text="",
        persona={},
        current_turn=current_turn,
        context=context,
        source="dialogue",
    )
    interaction = plan.interaction_plan

    assert interaction["kind"] == "resume_task"
    assert interaction["primary"] == "这张图片怎么样"
    assert interaction["resumed_task"] == "这张图片怎么样"
    assert interaction["media_binding"] is True
    assert interaction["media_message_ids"] == [101]
    assert interaction["speech_act"] == "answer"
    assert interaction["budget_basis"] == "act+complexity+persona"
    assert "101" in interaction["why"]["media"]
    assert "软目标" in interaction["why"]["length"]


def test_speech_simulation_payload_exposes_interaction_plan() -> None:
    modules = _load_modules()
    current_turn = {
        "user_id": 20001,
        "name": "用户",
        "content": "你怎么不说话？",
        "trigger": "mention",
    }
    plan = modules["speech_runtime"].build_runtime_speech_plan(
        text="在呢。",
        persona={},
        current_turn=current_turn,
        context={},
        source="dialogue",
    )

    payload = modules["speech_runtime"].speech_simulation_payload(
        plan,
        preview_only=True,
        user_text="你怎么不说话？",
    )

    assert payload["interaction_plan"]["kind"] == "ping_ack"
    assert payload["interaction_plan"]["speech_act"] == "ping_ack"
    assert payload["interaction_plan"]["soft_target_chars"] is not None
