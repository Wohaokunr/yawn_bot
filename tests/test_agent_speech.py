# ruff: noqa: PLR2004
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import nonebot
import pytest

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


def _persona(style_traits: str, social_style: str = "自然反应") -> dict[str, str]:
    return {
        "name": "Yawn",
        "style_traits": style_traits,
        "social_style": social_style,
    }


def test_speech_scene_resolves_direct_reply_reply_thread_and_conversation() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.speech_policy import resolve_speech_scene

    assert resolve_speech_scene({"trigger": "mention"}) == "direct_reply"
    assert (
        resolve_speech_scene(
            {
                "trigger": "mention",
                "reply_to": {"message_id": 12, "user_id": 3},
            }
        )
        == "reply_thread"
    )
    assert resolve_speech_scene({"trigger": "ambient"}) == "conversation"
    assert resolve_speech_scene(source="active") == "active_interject"
    assert resolve_speech_scene(source="warmup") == "warmup"
    assert resolve_speech_scene(source="followup") == "followup"


def test_quiet_persona_never_turns_explicit_reply_into_low_information_answer() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.speech_policy import (
        build_speech_instruction,
        resolve_speech_style,
    )

    quiet = _persona(
        "自然；偶尔轻幽默；直接度适中；极简短；情绪表达很淡",
        "很少参与；不主动续聊；少量反应",
    )
    style = resolve_speech_style(quiet, scene="direct_reply")
    instruction = build_speech_instruction(quiet, {"trigger": "mention"})

    assert style.verbosity == 0
    assert style.expressiveness == 0
    assert "角色再安静也不能省掉必要事实" in instruction
    assert "复杂问题以完整正确为先" in instruction


def test_lively_persona_compiles_voice_traits_into_scene_style() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.speech_policy import resolve_speech_style

    lively = _persona(
        "温和；很会接梗；比较直接；简洁；表现力很强",
        "很活跃；较愿意延展；很爱接反应",
    )
    style = resolve_speech_style(lively, scene="active_interject")

    assert style.warmth == 3
    assert style.humor == 4
    assert style.directness == 3
    assert style.verbosity == 1
    assert style.expressiveness == 4
    assert style.allow_spontaneous_reaction is True
    assert style.soft_target_chars == 56


def test_plain_speech_autofix_removes_assistant_boilerplate_and_generic_cta() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.speech import speech_plan_from_text
    from src.plugins.yawn_core.yawn_agent.speech_quality import finalize_speech_plan

    plan = speech_plan_from_text(
        "好的！主要原因是 DNS 解析失败。如果你还有其他问题可以继续问我。",
        scene="direct_reply",
    )
    final = finalize_speech_plan(plan)

    assert final.text == "主要原因是 DNS 解析失败。"
    assert {item.code for item in final.issues} == {
        "boilerplate_opening",
        "generic_followup_cta",
    }
    assert all(item.autofixed for item in final.issues)


def test_speech_linter_does_not_remove_substantive_question() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.speech import speech_plan_from_text
    from src.plugins.yawn_core.yawn_agent.speech_quality import finalize_speech_plan

    final = finalize_speech_plan(
        speech_plan_from_text(
            "这个错误通常是端口冲突。你现在 8080 端口被哪个进程占用？",
            scene="direct_reply",
        )
    )
    assert final.text.endswith("被哪个进程占用？")
    assert "generic_followup_cta" not in {item.code for item in final.issues}


def test_speech_linter_detects_recent_semantic_repeat() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.speech import speech_plan_from_text
    from src.plugins.yawn_core.yawn_agent.speech_quality import finalize_speech_plan

    final = finalize_speech_plan(
        speech_plan_from_text("这个确实比较麻烦，主要还是网络太慢。"),
        recent_texts=("这个确实挺麻烦的，主要还是网络太慢。",),
    )
    issues = {item.code: item for item in final.issues}
    assert "recent_repeat" in issues
    assert issues["recent_repeat"].autofixed is False


def test_short_scene_autofix_keeps_reply_shape_and_trims_only_text() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.speech import (
        SpeechStyle,
        speech_plan_from_segments,
    )
    from src.plugins.yawn_core.yawn_agent.speech_quality import finalize_speech_plan

    plan = speech_plan_from_segments(
        [
            {"type": "reply", "message_id": 99},
            {
                "type": "text",
                "text": "好的！" + "这个点确实挺有意思。" * 40,
            },
        ],
        scene="active_interject",
        style=SpeechStyle(verbosity=0, soft_target_chars=36),
    )
    final = finalize_speech_plan(plan, autofix=True)

    assert final.segments[0] == {"type": "reply", "message_id": 99}
    assert str(final.segments[1]["text"]).startswith("这个点")
    assert len(str(final.segments[1]["text"])) <= 72
    codes = {item.code for item in final.issues}
    assert "boilerplate_opening" in codes
    assert "scene_overlong" in codes


def test_direct_reply_is_not_hard_truncated_by_low_verbosity() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.speech import (
        SpeechStyle,
        speech_plan_from_text,
    )
    from src.plugins.yawn_core.yawn_agent.speech_quality import finalize_speech_plan

    content = "这是必要步骤。" * 100
    final = finalize_speech_plan(
        speech_plan_from_text(
            content,
            scene="direct_reply",
            style=SpeechStyle(verbosity=0, soft_target_chars=80),
        )
    )
    assert final.text == content
    assert "scene_overlong" not in {item.code for item in final.issues}


def test_prompt_injects_scene_policy_in_volatile_tail_without_changing_static_prefix() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.prompt import build_messages

    persona = _persona("自然；偶尔轻幽默；直接度适中；简洁；表达克制")
    direct, direct_hash = build_messages(
        persona=persona,
        tools=[],
        context={"active_topic": "Docker"},
        user_prompt="为什么这么慢",
        current_turn={
            "user_id": 1,
            "name": "A",
            "content": "为什么这么慢",
            "trigger": "mention",
        },
    )
    reply, reply_hash = build_messages(
        persona=persona,
        tools=[],
        context={"active_topic": "Docker"},
        user_prompt="这个呢",
        current_turn={
            "user_id": 1,
            "name": "A",
            "content": "这个呢",
            "trigger": "reply",
            "reply_to": {"message_id": 3, "user_id": 2, "text": "旧消息"},
        },
    )

    assert direct_hash == reply_hash
    assert direct[0] == reply[0]
    assert "发言场景=direct_reply" in str(direct[-2]["content"])
    assert "发言场景=reply_thread" in str(reply[-2]["content"])


def test_outbound_plain_and_structured_paths_both_carry_speech_metadata() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent import outbound

    plain = outbound.prepare_text_message(
        "好的！主要原因是网络拥塞。",
        speech_scene="direct_reply",
    )
    assert plain.normalized_text == "主要原因是网络拥塞。"
    assert plain.speech_scene == "direct_reply"
    assert "boilerplate_opening" in plain.quality_issues


@pytest.mark.asyncio
async def test_structured_outbound_defaults_to_lint_only_but_can_opt_into_autofix() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent import outbound

    raw = [{"type": "text", "text": "好的！主要原因是网络拥塞。"}]
    lint_only = await outbound.prepare_outbound_message(
        raw,
        session=None,
        group_id=1,
        allowed_segment_types=frozenset({"text"}),
    )
    assert lint_only.normalized_text == "好的！主要原因是网络拥塞。"
    assert "boilerplate_opening" in lint_only.quality_issues

    fixed = await outbound.prepare_outbound_message(
        raw,
        session=None,
        group_id=1,
        allowed_segment_types=frozenset({"text"}),
        speech_scene="active_interject",
        speech_autofix=True,
    )
    assert fixed.normalized_text == "主要原因是网络拥塞。"
    assert fixed.speech_scene == "active_interject"


def test_speech_plan_trace_payload_has_no_protocol_payload_or_private_fields() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.speech import (
        SpeechTarget,
        speech_plan_from_text,
    )

    plan = speech_plan_from_text(
        "简短回复",
        scene="direct_reply",
        target=SpeechTarget(user_id=123, reply_to_message_id=456),
    )
    payload = plan.trace_payload()
    assert payload["target_user_id"] == 123
    assert payload["reply_to_message_id"] == 456
    assert "file" not in repr(payload)
    assert "url" not in repr(payload)
    assert "raw" not in repr(payload).lower()
