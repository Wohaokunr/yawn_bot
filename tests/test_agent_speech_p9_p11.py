# ruff: noqa: PLR2004
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


def test_p9_explicit_turn_is_answer_but_closure_stays_closed() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.speech_act import (
        SPEECH_ACT_ANSWER,
        SPEECH_ACT_CLOSE,
        plan_speech_act,
        speech_act_instruction,
    )

    answer = plan_speech_act(
        {"content": "为什么镜像这么慢", "trigger": "mention"},
        scene="direct_reply",
    )
    assert answer.act == SPEECH_ACT_ANSWER
    assert answer.must_answer is True
    assert "不能用反问代替回答" in speech_act_instruction(answer)

    closing = plan_speech_act(
        {"content": "好了，解决了，谢谢了", "trigger": "mention"},
        scene="direct_reply",
    )
    assert closing.act == SPEECH_ACT_CLOSE
    assert "不追加新问题" in speech_act_instruction(closing)


def test_p9_short_ack_does_not_expand_into_new_topic() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.speech_act import (
        SPEECH_ACT_ACKNOWLEDGE,
        plan_speech_act,
        speech_act_instruction,
    )

    plan = plan_speech_act({"content": "懂了"}, scene="followup")
    assert plan.act == SPEECH_ACT_ACKNOWLEDGE
    assert "不要把一句确认扩写成解释或新话题" in speech_act_instruction(plan)


def test_p10_turn_pressure_is_high_only_for_non_explicit_busy_group() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.turn_taking import (
        TURN_PRESSURE_HIGH,
        TURN_PRESSURE_LOW,
        plan_turn_taking,
    )

    context = {
        "topic_state": {"participant_count": 3},
        "messages": [
            {"role": "bot", "text": "A"},
            {"role": "user", "user_id": 1, "text": "1"},
            {"role": "bot", "text": "B"},
            {"role": "user", "user_id": 2, "text": "2"},
        ],
    }
    busy = plan_turn_taking(
        {"content": "确实"},
        scene="conversation",
        context=context,
    )
    assert busy.pressure == TURN_PRESSURE_HIGH
    assert busy.prefer_brief is True
    assert busy.avoid_followup_question is True

    explicit = plan_turn_taking(
        {"content": "具体怎么修", "trigger": "mention"},
        scene="direct_reply",
        context=context,
    )
    assert explicit.pressure == TURN_PRESSURE_LOW
    assert explicit.explicit_turn is True
    assert explicit.prefer_brief is False


def test_p10_prompt_compiles_act_and_turn_taking_only_in_volatile_tail() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.prompt import build_messages

    context = {
        "group_id": 1,
        "group_name": "测试群",
        "topic_state": {"participant_count": 3, "status": "fresh"},
        "messages": [
            {"role": "bot", "text": "刚说过一次"},
            {"role": "user", "user_id": 11, "text": "A"},
            {"role": "bot", "text": "又说了一次"},
            {"role": "user", "user_id": 22, "text": "B"},
        ],
        "memories": [],
    }
    messages, fingerprint = build_messages(
        persona={"name": "Yawn"},
        tools=[],
        context=context,
        user_prompt="确实",
        current_turn={"user_id": 22, "content": "确实"},
    )
    assert fingerprint
    assert "话语动作=continue" not in str(messages[0]["content"])
    assert "轮次压力=high" not in str(messages[0]["content"])
    tail = str(messages[-2]["content"])
    assert "话语动作=continue" in tail
    assert "轮次压力=high" in tail


def test_p11_scorecard_penalizes_assistant_boilerplate_and_reopened_close() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.speech_act import SPEECH_ACT_CLOSE
    from src.plugins.yawn_core.yawn_agent.speech_scorecard import score_speech_output

    poor = score_speech_output(
        "好的！问题已经解决。如果你还有其他问题可以继续问我。",
        expected_act=SPEECH_ACT_CLOSE,
    )
    assert poor.score < 80
    assert "quality:boilerplate_opening" in poor.deductions
    assert "quality:generic_followup_cta" in poor.deductions

    reopened = score_speech_output(
        "行，那先这样。还要继续看看别的吗？",
        expected_act=SPEECH_ACT_CLOSE,
    )
    assert "act:close_reopened" in reopened.deductions


def test_p11_scorecard_suite_is_deterministic_and_ci_friendly() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.speech_act import (
        SPEECH_ACT_ACKNOWLEDGE,
        SPEECH_ACT_ANSWER,
    )
    from src.plugins.yawn_core.yawn_agent.speech_scorecard import (
        SpeechScenario,
        run_speech_scorecard,
    )
    from src.plugins.yawn_core.yawn_agent.turn_taking import TURN_PRESSURE_HIGH

    result = run_speech_scorecard(
        [
            SpeechScenario(
                name="direct-answer",
                text="主要是镜像层下载慢，先看拉取阶段的网络吞吐和 registry 连接。",
                expected_act=SPEECH_ACT_ANSWER,
            ),
            SpeechScenario(
                name="short-ack",
                text="嗯，明白。",
                expected_act=SPEECH_ACT_ACKNOWLEDGE,
            ),
            SpeechScenario(
                name="busy-group-brief",
                text="对，这里更像是网络瓶颈。",
                turn_pressure=TURN_PRESSURE_HIGH,
            ),
        ]
    )
    assert result.ok is True
    assert result.total == 3
    assert result.passed == 3
    assert result.average_score >= 80
