# ruff: noqa: E501,PLR2004
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
    from src.plugins.yawn_core.yawn_agent import (
        context_history,
        speech_act,
        speech_policy,
        speech_quality,
        speech_runtime,
    )
    from src.plugins.yawn_core.yawn_agent.speech import SpeechStyle, speech_plan_from_text

    return {
        "context_history": context_history,
        "speech_act": speech_act,
        "speech_policy": speech_policy,
        "speech_quality": speech_quality,
        "speech_runtime": speech_runtime,
        "SpeechStyle": SpeechStyle,
        "speech_plan_from_text": speech_plan_from_text,
    }


def _text_message(message_id: int, user_id: int, text: str, minutes_ago: int = 0) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "user_id": user_id,
        "text": text,
        "minutes_ago": minutes_ago,
    }


def _mention_turn(user_id: int = 20001) -> dict[str, Any]:
    return {
        "message_id": 203,
        "user_id": user_id,
        "name": "用户",
        "content": "[用户仅@机器人，没有附加正文]",
        "trigger": "at",
    }


def test_repair_ping_becomes_repair_act_with_small_budget() -> None:
    modules = _load_modules()
    context = {
        "messages": [
            _text_message(201, 20001, "你这个AI味有点大呀", minutes_ago=2),
            _text_message(202, 20001, "你怎么不说话？", minutes_ago=1),
        ]
    }

    plan = modules["speech_runtime"].build_runtime_speech_plan(
        text="在呢，刚才没接上。那句确实有点太端了。",
        persona={},
        current_turn=_mention_turn(),
        context=context,
    )

    assert plan.act == modules["speech_act"].SPEECH_ACT_REPAIR
    assert plan.style.response_complexity == modules["speech_policy"].RESPONSE_COMPLEXITY_SIMPLE
    assert plan.style.soft_target_chars == 55


def test_plain_presence_ping_becomes_ping_ack() -> None:
    modules = _load_modules()
    context = {"messages": [_text_message(202, 20001, "怎么不说话了", minutes_ago=1)]}

    plan = modules["speech_runtime"].build_runtime_speech_plan(
        text="在呢，刚才没接上。",
        persona={},
        current_turn=_mention_turn(),
        context=context,
    )

    assert plan.act == modules["speech_act"].SPEECH_ACT_PING_ACK
    assert plan.style.response_complexity == modules["speech_policy"].RESPONSE_COMPLEXITY_SIMPLE
    assert plan.style.soft_target_chars == 30


def test_simple_answer_uses_answer_plus_simple_complexity_budget() -> None:
    modules = _load_modules()
    current_turn = {
        "message_id": 301,
        "user_id": 20001,
        "name": "用户",
        "content": "1+1等于多少？",
        "trigger": "at",
    }

    plan = modules["speech_runtime"].build_runtime_speech_plan(
        text="2。",
        persona={},
        current_turn=current_turn,
        context={"messages": []},
    )

    assert plan.act == modules["speech_act"].SPEECH_ACT_ANSWER
    assert plan.style.response_complexity == modules["speech_policy"].RESPONSE_COMPLEXITY_SIMPLE
    assert plan.style.soft_target_chars == 100


def test_complex_answer_gets_larger_budget_without_changing_persona() -> None:
    modules = _load_modules()
    current_turn = {
        "message_id": 302,
        "user_id": 20001,
        "name": "用户",
        "content": "请仔细分析这个 Agent 对话系统的根因，给出重构方案、实现步骤和回归测试计划。",
        "trigger": "at",
    }

    plan = modules["speech_runtime"].build_runtime_speech_plan(
        text="这里需要展开分析。",
        persona={},
        current_turn=current_turn,
        context={"messages": []},
    )

    assert plan.act == modules["speech_act"].SPEECH_ACT_ANSWER
    assert plan.style.response_complexity == modules["speech_policy"].RESPONSE_COMPLEXITY_COMPLEX
    assert plan.style.soft_target_chars == 420


def test_prompt_policy_exposes_separated_repair_interaction() -> None:
    modules = _load_modules()
    current_turn = {
        "message_id": 203,
        "user_id": 20001,
        "name": "用户",
        "content": "你怎么不说话？",
        "trigger": "at",
        "interaction": {
            "kind": modules["context_history"].INTERACTION_REPAIR_PING,
            "primary": "你怎么不说话？",
            "support": ["你这个AI味有点大呀"],
            "media_binding": False,
        },
    }

    instruction = modules["speech_policy"].build_speech_instruction(
        {},
        current_turn,
        context={"messages": []},
    )

    assert "话语动作=repair" in instruction
    assert "support 只用于理解 primary" in instruction
    assert "media_binding=false" in instruction
    assert "约 55 字内" in instruction


def test_quality_guard_trims_overlong_direct_repair() -> None:
    modules = _load_modules()
    plan = modules["speech_plan_from_text"](
        "这句话确实太端了，我会收一点。" * 20,
        scene="direct_reply",
        style=modules["SpeechStyle"](soft_target_chars=55),
        act=modules["speech_act"].SPEECH_ACT_REPAIR,
    )

    final = modules["speech_quality"].finalize_speech_plan(plan, autofix=True)

    assert len(final.visible_text) <= 110
    assert any(item.code == "scene_overlong" and item.autofixed for item in final.issues)
