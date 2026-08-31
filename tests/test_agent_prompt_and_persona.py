# ruff: noqa: TC001,TC002,TC003,PLR2004
from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nonebot
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_agent_modules():
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
    from src.plugins.yawn_core.yawn_agent.persona import (
        prompt_persona,
        resolve_persona,
    )
    from src.plugins.yawn_core.yawn_agent.prompt import build_messages

    return prompt_persona, resolve_persona, build_messages


def test_persona_v2_prompt_compiler_ignores_removed_legacy_and_policy_fields() -> None:
    prompt_persona, _, _ = _load_agent_modules()
    rendered = prompt_persona(
        {
            "name": "Yawn",
            "identity": "自然群友",
            "role": "普通群友",
            "style_traits": "自然表达；简洁",
            "social_style": "社交平衡",
            "custom_notes": "偶尔自嘲",
            # P6 removed fields must never re-enter the prompt.
            "tone": "恶意覆盖",
            "speech_style": "恶意覆盖",
            "emotion_baseline": "恶意覆盖",
            "response_length": "恶意覆盖",
            "values": "恶意覆盖",
            "knowledge_boundary": "随便猜",
            "privacy_boundary": "公开私聊",
        }
    )
    assert '"name":"Yawn"' in rendered
    assert '"style_traits":"自然表达；简洁"' in rendered
    assert '"custom_notes":"偶尔自嘲"' in rendered
    for removed in (
        "tone",
        "speech_style",
        "emotion_baseline",
        "response_length",
        "values",
        "knowledge_boundary",
        "privacy_boundary",
    ):
        assert removed not in rendered


def test_persona_v2_runtime_ignores_legacy_database_attributes() -> None:
    _, resolve_persona, _ = _load_agent_modules()
    config = SimpleNamespace(
        group_id=10001,
        persona_enabled=True,
        persona_version=7,
        persona_profile={
            "schema_version": 2,
            "preset_id": "calm_rational",
            "identity": {"name": "V2", "description": "只读 v2"},
            "voice": {
                "warmth": 1,
                "humor": 0,
                "directness": 4,
                "verbosity": 2,
                "expressiveness": 0,
            },
            "social": {
                "sociability": 1,
                "followup_tendency": 1,
                "reaction_tendency": 0,
            },
        },
        # These attributes emulate stale callers; P6 runtime must ignore them.
        persona="legacy single text",
        persona_override={
            "name": "Legacy",
            "privacy_boundary": "legacy policy",
            "tone": "legacy tone",
        },
        persona_schema_version=1,
    )
    resolved = resolve_persona(config)
    assert resolved["name"] == "V2"
    assert resolved["identity"] == "只读 v2"
    assert "Legacy" not in repr(resolved)
    assert "legacy policy" not in repr(resolved)
    assert "legacy tone" not in repr(resolved)


def test_persona_v2_global_default_is_structured_natural_profile() -> None:
    _, resolve_persona, _ = _load_agent_modules()
    resolved = resolve_persona(None)
    assert resolved["profile_v2"] == "structured"
    assert resolved["name"] == "Yawn"
    assert "style_traits" in resolved
    assert "social_style" in resolved
    assert "knowledge_boundary" not in resolved
    assert "privacy_boundary" not in resolved


def test_persona_v2_editor_uses_templates_traits_and_stable_prompt() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.persona import (
        PERSONA_PRESETS,
        apply_persona_editor_profile,
        persona_editor_apply_preset,
        persona_editor_profile,
        prompt_persona,
        resolve_persona,
    )

    config = SimpleNamespace(
        group_id=10003,
        persona="友好、自然、简洁的群友",
        persona_override={},
        persona_enabled=True,
        persona_version=1,
        persona_schema_version=2,
        persona_profile={"schema_version": 2},
    )
    assert set(PERSONA_PRESETS) == {
        "natural",
        "gentle_listener",
        "calm_rational",
        "lively_sidekick",
        "quiet_observer",
    }

    draft = persona_editor_apply_preset(
        persona_editor_profile(config), "lively_sidekick"
    ).model_copy(update={"custom_notes": "偶尔用好困自嘲"})
    mutation = apply_persona_editor_profile(config, draft, enabled=True)
    assert mutation.semantic_changed is True
    assert mutation.storage_changed is True
    assert config.persona_profile["preset_id"] == "lively_sidekick"
    assert config.persona_profile["voice"]["humor"] == 4
    assert config.persona_profile["social"]["sociability"] == 4

    rendered = prompt_persona(resolve_persona(config))
    assert '"style_traits"' in rendered
    assert "很会接梗" in rendered
    assert '"social_style"' in rendered
    assert "很活跃" in rendered
    assert '"custom_notes":"偶尔用好困自嘲"' in rendered
    assert '"tone"' not in rendered
    assert '"response_length"' not in rendered

    second = apply_persona_editor_profile(config, draft, enabled=True)
    assert second.semantic_changed is False
    assert second.storage_changed is False


def test_persona_v2_reset_removes_structured_traits() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.persona import (
        PersonaEditorProfileV2,
        apply_persona_editor_profile,
        reset_persona,
        resolve_persona,
    )

    config = SimpleNamespace(
        group_id=10004,
        persona="友好、自然、简洁的群友",
        persona_override={},
        persona_enabled=True,
        persona_version=1,
        persona_schema_version=2,
        persona_profile={"schema_version": 2},
    )
    draft = PersonaEditorProfileV2(
        presetId="quiet_observer",
        name="Yawn",
        identity="安静群友",
        groupRole="潜水观察者",
        sociability=0,
        customNotes="少说话",
    )
    apply_persona_editor_profile(config, draft, enabled=True)
    assert "social_style" in resolve_persona(config)

    mutation = reset_persona(config)
    assert mutation.semantic_changed is True
    assert config.persona_profile == {"schema_version": 2}
    assert config.persona_enabled is False
    resolved = resolve_persona(config)
    assert resolved["profile_v2"] == "structured"
    assert "social_style" in resolved


def _make_structured_persona_config(
    *,
    preset_id: str,
    sociability: int,
    followup_tendency: int,
    reaction_tendency: int,
    persona_enabled: bool = True,
):
    return SimpleNamespace(
        group_id=19001,
        persona="友好、自然、简洁的群友",
        persona_override={},
        persona_enabled=persona_enabled,
        persona_version=4,
        persona_schema_version=2,
        persona_profile={
            "schema_version": 2,
            "preset_id": preset_id,
            "voice": {
                "warmth": 2,
                "humor": 1,
                "directness": 2,
                "verbosity": 1,
                "expressiveness": 1,
            },
            "social": {
                "sociability": sociability,
                "followup_tendency": followup_tendency,
                "reaction_tendency": reaction_tendency,
            },
        },
    )


def test_persona_p4_compiles_social_traits_into_runtime_behavior() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.persona import persona_behavior

    quiet = persona_behavior(
        _make_structured_persona_config(
            preset_id="quiet_observer",
            sociability=0,
            followup_tendency=0,
            reaction_tendency=1,
        )
    )
    lively = persona_behavior(
        _make_structured_persona_config(
            preset_id="lively_sidekick",
            sociability=4,
            followup_tendency=3,
            reaction_tendency=4,
        )
    )

    assert quiet.source == "persona_v2"
    assert quiet.active_probability_scale == 0.15
    assert quiet.warmup_probability_scale == 0.15
    assert quiet.max_followup_bot_turns == 1
    assert quiet.allow_spontaneous_reaction is False
    assert quiet.reaction_mode == "restrained"

    assert lively.active_probability_scale == 1.0
    assert lively.warmup_probability_scale == 1.0
    assert lively.max_followup_bot_turns == 4
    assert lively.allow_spontaneous_reaction is True
    assert lively.reaction_mode == "high"


def test_persona_p6_runtime_has_no_legacy_behavior_branch() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.persona import persona_behavior

    stale = SimpleNamespace(
        group_id=19002,
        persona_enabled=True,
        persona_profile={
            "schema_version": 2,
            "preset_id": "natural",
            "voice": {
                "warmth": 2,
                "humor": 1,
                "directness": 2,
                "verbosity": 1,
                "expressiveness": 1,
            },
            "social": {
                "sociability": 4,
                "followup_tendency": 4,
                "reaction_tendency": 2,
            },
        },
        persona="legacy text",
        persona_override={"tone": "legacy tone"},
        persona_schema_version=1,
    )
    behavior = persona_behavior(stale)
    assert behavior.source == "persona_v2"
    assert behavior.active_probability_scale == 1.0
    assert behavior.warmup_probability_scale == 1.0
    assert behavior.max_followup_bot_turns == 4


def test_persona_p4_inherit_mode_ignores_paused_group_behavior_draft() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.persona import persona_behavior

    config = _make_structured_persona_config(
        preset_id="quiet_observer",
        sociability=0,
        followup_tendency=0,
        reaction_tendency=0,
        persona_enabled=False,
    )
    behavior = persona_behavior(config)
    assert behavior.source == "global"
    assert behavior.sociability == 2
    assert behavior.max_followup_bot_turns == 2
    assert behavior.allow_spontaneous_reaction is True


def test_persona_p4_filters_spontaneous_reactions_but_keeps_text() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.proactive_policy import (
        apply_persona_behavior_to_decision,
        decide_proactive_reply,
    )

    quiet_config = _make_structured_persona_config(
        preset_id="quiet_observer",
        sociability=0,
        followup_tendency=0,
        reaction_tendency=1,
    )
    reaction_only = decide_proactive_reply(
        '{"action":"speak","message":{"segments":['
        '{"type":"reaction","reaction_id":"happy"}]}}'
    )
    filtered_only = apply_persona_behavior_to_decision(quiet_config, reaction_only)
    assert filtered_only.action == "wait"
    assert filtered_only.should_speak is False

    text_and_reaction = decide_proactive_reply(
        '{"action":"speak","message":{"segments":['
        '{"type":"text","text":"这个好笑"},'
        '{"type":"reaction","reaction_id":"happy"}]}}'
    )
    filtered = apply_persona_behavior_to_decision(quiet_config, text_and_reaction)
    assert filtered.should_speak is True
    assert filtered.text == "这个好笑"
    assert [item["type"] for item in filtered.segments] == ["text"]

    lively_config = _make_structured_persona_config(
        preset_id="lively_sidekick",
        sociability=4,
        followup_tendency=3,
        reaction_tendency=4,
    )
    lively = apply_persona_behavior_to_decision(lively_config, text_and_reaction)
    assert [item["type"] for item in lively.segments] == ["text", "reaction"]


def test_persona_p4_limits_short_conversation_turns() -> None:
    _load_agent_modules()
    import time

    from src.plugins.yawn_core.yawn_agent import conversation

    conversation.reset_for_tests()
    now = time.monotonic()
    conversation.mark_bot_reply(
        9,
        19003,
        topic="安静角色",
        source="test",
        max_bot_turns=1,
        now=now,
    )
    assert conversation.current_conversation(9, 19003) is None

    conversation.mark_bot_reply(
        9,
        19004,
        topic="自然角色",
        source="test",
        max_bot_turns=2,
        now=now,
    )
    assert conversation.current_conversation(9, 19004) is not None
    conversation.mark_bot_reply(
        9,
        19004,
        topic="自然角色",
        source="test",
        max_bot_turns=2,
        now=now + 1,
    )
    assert conversation.current_conversation(9, 19004) is None


def test_prompt_prefix_is_stable_when_only_context_changes() -> None:
    _, _, build_messages = _load_agent_modules()
    tools = [{"type": "function", "function": {"name": "send_text"}}]
    first, first_hash = build_messages(
        persona={"name": "Yawn"},
        tools=tools,
        context={"active_topic": "a"},
        user_prompt="你好",
    )
    second, second_hash = build_messages(
        persona={"name": "Yawn"},
        tools=tools,
        context={"active_topic": "b"},
        user_prompt="在吗",
    )
    assert first_hash == second_hash
    assert first[0] == second[0]
    # 易变内容变化不得波及稳定层：第 2 条（稳定层）字节一致，第 3 条（易变层）变化。
    assert first[1] == second[1]
    assert first[2] != second[2]


def test_build_messages_keeps_image_block_alongside_text_placeholder() -> None:
    _, _, build_messages = _load_agent_modules()
    image_block = {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,ZmFrZS1pbWFnZQ=="},
    }

    messages, _ = build_messages(
        persona={"name": "Yawn"},
        tools=[],
        context={},
        user_prompt="[图片]",
        media_inputs=[image_block],
    )

    user_content = messages[-1]["content"]
    assert isinstance(user_content, list)
    assert user_content[0] == {"type": "text", "text": "[图片]"}
    assert user_content[1] == image_block


def test_prompt_does_not_repeat_tool_schema_or_catalog() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent import prompt

    tools = [
        {
            "type": "function",
            "function": {
                "name": "search_group_memory",
                "description": "SHOULD_NOT_BE_IN_SYSTEM_PROMPT",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            },
        }
    ]
    messages, _ = prompt.build_messages(
        persona={"name": "Yawn"},
        tools=tools,
        context={},
        user_prompt="你好",
    )

    assert prompt.PROMPT_VERSION == "yawn-agent-v13"
    assert "search_group_memory" not in str(messages[0]["content"])
    assert "SHOULD_NOT_BE_IN_SYSTEM_PROMPT" not in str(messages[0]["content"])
    assert '"properties"' not in str(messages[0]["content"])


def test_default_prompt_prefix_has_cost_guard() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.context_budget import (
        build_context_budget,
        estimate_tokens,
    )
    from src.plugins.yawn_core.yawn_agent.persona import resolve_persona
    from src.plugins.yawn_core.yawn_agent.prompt import build_static_prefix

    static = build_static_prefix(resolve_persona(None), [])
    assert estimate_tokens(static) < 450

    from src.plugins.yawn_core.yawn_agent.persona import (
        apply_persona_editor_profile,
        persona_editor_profile,
    )

    structured_config = SimpleNamespace(
        group_id=10005,
        persona="友好、自然、简洁的群友",
        persona_override={},
        persona_enabled=True,
        persona_version=1,
        persona_schema_version=2,
        persona_profile={"schema_version": 2},
    )
    natural_draft = persona_editor_profile(structured_config)
    apply_persona_editor_profile(structured_config, natural_draft, enabled=True)
    structured_static = build_static_prefix(resolve_persona(structured_config), [])
    assert estimate_tokens(structured_static) < 450

    dialogue_budget = build_context_budget(
        model="test",
        completion_reserve=800,
        target_context_limit=2400,
    )
    proactive_budget = build_context_budget(
        model="test",
        completion_reserve=800,
        target_context_limit=1600,
    )
    assert dialogue_budget.context_limit == 2400
    assert proactive_budget.context_limit == 1600


def test_current_turn_prompt_omits_empty_defaults() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.context import build_current_turn
    from src.plugins.yawn_core.yawn_agent.prompt import render_current_turn

    turn = build_current_turn(
        message_id=None,
        user_id=123,
        name="测试成员",
        role="member",
        title=None,
        content="你好",
        trigger="mention",
        received_at=None,
    )
    rendered = render_current_turn(turn)
    assert '"user_id":123' in rendered
    assert '"title"' not in rendered
    assert '"mentions"' not in rendered
    assert '"media"' not in rendered
    assert '"forward_nodes"' not in rendered
    assert '"truncated"' not in rendered


def test_current_turn_has_priority_over_old_group_topic() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.context import build_current_turn
    from src.plugins.yawn_core.yawn_agent.prompt import build_messages

    received_at = datetime(2026, 8, 25, 12, 0)  # noqa: DTZ001
    current_turn = build_current_turn(
        message_id=9002,
        user_id=300,
        name="当前发言人",
        role="member",
        title=None,
        content="到底有没有一起玩",
        mentions=[400],
        reply_chain=[
            {
                "message_id": 9001,
                "user_id": 500,
                "nickname": "被引用的人",
                "text": "前面问的是 Roblox",
            }
        ],
        trigger="reply",
        received_at=received_at,
    )
    messages, _ = build_messages(
        persona={"name": "Yawn"},
        tools=[],
        context={
            "active_topic": "几小时前的 Roblox 长篇讨论",
            "messages": [
                {
                    "user_id": 200,
                    "name": "上一位发言人",
                    "text": "Brookhaven 和 Doors 都不错",
                    "minutes_ago": 180,
                    "topic_break_before": True,
                }
            ],
        },
        user_prompt="这段不会成为最终 user 消息",
        current_turn=current_turn,
    )

    system = str(messages[0]["content"])
    current = str(messages[-1]["content"])
    assert "current_turn 是本轮最高优先级" in system
    assert "不要误答上一位成员" in system
    assert "默认用 1~2 句" not in system
    assert '"name":"Yawn"' in system
    assert '"user_id":300' in current
    assert '"name":"当前发言人"' in current
    assert '"content":"到底有没有一起玩"' in current
    assert '"mentions":[400]' in current
    assert '"user_id":500' in current
    assert "这段不会成为最终 user 消息" not in current
    assert "Brookhaven" in str(messages[2]["content"])


def test_topic_boundary_and_message_age_mark_old_sessions() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.context import topic_break_before

    current = datetime(2026, 8, 25, 12, 0)  # noqa: DTZ001
    assert not topic_break_before(current - timedelta(minutes=29), current)
    assert topic_break_before(current - timedelta(minutes=30), current)
    assert topic_break_before(current - timedelta(days=3), current)


def test_current_turn_focus_includes_actor_mentions_and_direct_reply() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.dialogue import _current_turn_focus_ids
    from src.plugins.yawn_core.yawn_agent.message_parser import NormalizedMessage

    normalized = NormalizedMessage(
        plain_text="问一下",
        segments=[],
        mentions=[100, 400, 300],
        reply_chain=[{"user_id": 500, "message_id": 12, "text": "原消息"}],
    )
    assert _current_turn_focus_ids(300, normalized, bot_id=100) == [300, 400, 500]


def test_context_message_budget_keeps_newest_content() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.context import trim_context_messages

    messages = [{"user_id": index, "text": str(index) * 900} for index in range(50)]
    trimmed = trim_context_messages(messages)

    assert len(trimmed) <= 40
    assert trimmed[-1]["user_id"] == 49
    assert all(len(str(item["text"])) <= 800 for item in trimmed)
    assert sum(len(str(item["text"])) for item in trimmed) == 6000
    assert trim_context_messages(messages, max_messages=0) == []


def test_history_prompt_payload_omits_default_empty_metadata() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.dialogue import _history_message_payload

    received_at = datetime(2026, 8, 25, 19, 57)  # noqa: DTZ001
    row = SimpleNamespace(
        message_id=159298630,
        user_id=3631683695,
        sender_name=None,
        role="bot",
        title=None,
        normalized_text="网易这爆料是啥新游啊，看着像开放世界类型",
        received_at=received_at,
        segments=[{"type": "text", "data": {"text": "ignored"}}],
        reply_chain=[],
        media_refs=[],
        forward_tree=[],
    )

    payload = _history_message_payload(
        row,  # pyright: ignore[reportArgumentType]
        context_now=received_at + timedelta(minutes=12),
        previous_at=received_at - timedelta(minutes=1),
    )

    assert payload == {
        "message_id": 159298630,
        "user_id": 3631683695,
        "role": "bot",
        "text": "网易这爆料是啥新游啊，看着像开放世界类型",
        "minutes_ago": 12,
    }
    assert "message_meta" not in payload
    assert "title" not in payload
    assert "mentions" not in payload
    assert "reply_to" not in payload
    assert "media_types" not in payload
    assert "forward_nodes" not in payload
    assert "topic_break_before" not in payload


def test_history_selector_does_not_drag_unrelated_old_cluster_into_new_turn() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.dialogue import _select_context_messages

    messages = [
        {
            "message_id": 1,
            "user_id": 10,
            "name": "可恶的网易",
            "text": "网易其他游戏最新爆料",
            "minutes_ago": 50,
        },
        {
            "message_id": 2,
            "user_id": 20,
            "name": "亦，",
            "text": "服务器规格：40人云顶服 模组：丰富经济系统",
            "minutes_ago": 19,
        },
        {
            "message_id": 3,
            "user_id": 30,
            "name": "old•崩•die",
            "text": "[图片]",
            "minutes_ago": 7,
            "media_types": ["image"],
        },
    ]

    assert _select_context_messages(
        messages,
        focus_user_ids=[3856622439],
        query_text="有人玩那个祖国人模组吗",
    ) == []


def test_history_selection_trace_explains_keep_and_drop_without_polluting_prompt(
) -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.context_history import select_context_messages

    messages = [
        {
            "message_id": 1,
            "user_id": 10,
            "text": "嗯",
            "minutes_ago": 1,
        },
        {
            "message_id": 2,
            "user_id": 20,
            "name": "测试群友",
            "text": "祖国人模组刚更新了新版本",
            "minutes_ago": 2,
        },
        {
            "message_id": 3,
            "user_id": 30,
            "text": "昨天完全无关的旧话题",
            "minutes_ago": 70,
        },
    ]

    selection = select_context_messages(
        messages,
        focus_user_ids=[20],
        query_text="祖国人模组更新了吗",
    )

    assert [item["message_id"] for item in selection.messages] == [2]
    trace = {item["message_id"]: item for item in selection.trace}
    assert trace[1]["selected"] is False
    assert trace[1]["reason"] == "low_information"
    assert trace[2]["selected"] is True
    assert trace[2]["reason"] in {"recent_cluster", "query_overlap"}
    assert trace[2]["name"] == "测试群友"
    assert trace[2]["text"] == "祖国人模组刚更新了新版本"
    assert trace[2]["role"] == "member"
    assert trace[2]["text_truncated"] is False
    assert trace[3]["message_id"] == 3
    assert trace[3]["text"] == "昨天完全无关的旧话题"
    assert trace[3]["minutes_ago"] == 70
    assert trace[3]["selected"] is False
    assert trace[3]["reason"] == "stale"
    assert all(
        "reason" not in item and "selected" not in item
        for item in selection.messages
    )


def _build_messages_layered(
    context: dict, tools: list
) -> tuple[list[dict], str]:
    from src.plugins.yawn_core.yawn_agent.prompt import build_messages

    return build_messages(
        persona={"name": "Yawn"}, tools=tools, context=context, user_prompt="在吗"
    )


def test_context_layering_separates_slow_memories_from_volatile_block() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.prompt import (
        split_context,
        stable_context_key,
    )

    tools = [{"type": "function", "function": {"name": "send_text"}}]
    context = {
        "group_id": 100,
        "group_name": "测试群",
        "active_topic": "晚饭",
        "activity": {"messages_5m": 3},
        "members": [{"user_id": 1, "name": "阿眠"}],
        "messages": [{"user_id": 1, "text": "晚饭吃什么"}],
        "memories": [
            {
                "type": "summary",
                "key": "daily:2026-08-22",
                "content": "群里聊了晚饭安排",
                "source_scope": "group_summary",
            },
            {
                "type": "profile",
                "key": "display_name",
                "content": "阿眠",
                "source_scope": "speaker",
            },
            {
                "type": "summary",
                "key": "public_daily:2026-08-21",
                "content": "公共话题",
                "source_scope": "shared_public",
            },
        ],
        "relations": ["阿眠(1) —mentions→ 小李(2)"],
    }
    messages, _ = _build_messages_layered(context, tools)
    assert len(messages) == 5
    stable_json = messages[1]["content"]
    speaker_json = messages[2]["content"]
    volatile_json = messages[3]["content"]
    assert "daily:2026-08-22" in stable_json
    assert "public_daily:2026-08-21" in stable_json
    assert "active_topic" not in stable_json
    assert "晚饭安排" not in volatile_json
    assert "阿眠" in speaker_json and "mentions" in speaker_json
    assert "activity" not in speaker_json
    assert "activity" in volatile_json

    # 同一整理窗口内新消息到达：易变层变化、稳定层字节不变，
    # 服务端前缀缓存可命中到稳定层为止。
    evolved = {
        **context,
        "active_topic": "火锅",
        "messages": [{"user_id": 2, "text": "去吃火锅"}],
        "memories": [
            context["memories"][0],
            context["memories"][2],
            {
                "type": "profile",
                "key": "display_name",
                "content": "别人",
                "source_scope": "topic",
            },
        ],
    }
    again, _ = _build_messages_layered(evolved, tools)
    assert again[1] == messages[1]
    assert again[2] != messages[2]
    assert again[3] != messages[3]
    assert stable_context_key(context) == stable_context_key(evolved)
    assert stable_context_key(context) != stable_context_key(
        {**evolved, "group_name": "改名后的群"}
    )

    # 缺省上下文（无稳定记忆）时稳定层不携带空 memories 噪声。
    stable, volatile = split_context({"active_topic": "a"})
    assert stable == {}
    assert volatile["memories"] == []


def test_tool_bundle_does_not_change_global_or_group_cache_prefix() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.prompt import build_messages

    context = {
        "group_id": 100,
        "group_name": "测试群",
        "members": [{"user_id": 1, "name": "阿眠"}],
        "activity": {"messages_5m": 1},
        "messages": [{"user_id": 1, "text": "在吗"}],
        "memories": [],
        "relations": [],
    }
    plain, _ = build_messages(
        persona={"name": "Yawn"}, tools=[], context=context, user_prompt="在吗"
    )
    send_tools = [{"type": "function", "function": {"name": "send_message"}}]
    rich, _ = build_messages(
        persona={"name": "Yawn"},
        tools=send_tools,
        context=context,
        user_prompt="回复他",
    )

    assert plain[0] == rich[0]
    assert plain[1] == rich[1]
    assert plain[2] == rich[2]
    assert any("send_message" in str(item.get("content")) for item in rich[3:-1])


def test_activity_changes_only_realtime_layer_for_same_speaker() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.prompt import build_messages

    base = {
        "group_id": 100,
        "group_name": "测试群",
        "members": [{"user_id": 1, "name": "阿眠"}],
        "memories": [
            {
                "type": "profile",
                "key": "display_name",
                "content": "阿眠",
                "source_scope": "speaker",
            }
        ],
        "relations": ["阿眠(1) —friend→ 小李(2)"],
        "messages": [{"user_id": 1, "text": "第一条"}],
        "activity": {"messages_5m": 1},
    }
    first, _ = build_messages(
        persona={"name": "Yawn"}, tools=[], context=base, user_prompt="第一条"
    )
    second, _ = build_messages(
        persona={"name": "Yawn"},
        tools=[],
        context={
            **base,
            "activity": {"messages_5m": 8},
            "messages": [{"user_id": 1, "text": "第二条"}],
        },
        user_prompt="第二条",
    )

    assert first[:3] == second[:3]
    assert first[3] != second[3]


def test_dialogue_turn_usage_accumulates_rounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _load_agent_modules()
    from src.plugins.yawn_core import metrics
    from src.plugins.yawn_core.yawn_agent.dialogue import _accumulate_turn_usage

    recorded: list[tuple[str, str, int]] = []
    monkeypatch.setattr(
        metrics,
        "record_ai_tokens",
        lambda operation, source, value: recorded.append((operation, source, value)),
    )
    total: dict[str, int] = {}
    first = _accumulate_turn_usage(
        total,
        SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=20,
            cached_tokens=60,
            cache_miss_tokens=40,
        ),
    )
    second = _accumulate_turn_usage(
        total,
        SimpleNamespace(
            prompt_tokens=130,
            completion_tokens=10,
            cached_tokens=100,
            cache_miss_tokens=30,
        ),
    )

    assert first["turn"]["rounds"] == 1
    assert second["turn"] == {
        "rounds": 2,
        "prompt_tokens": 230,
        "completion_tokens": 30,
        "cached_tokens": 160,
        "cache_miss_tokens": 70,
    }
    assert ("agent_dialogue_turn", "input", 130) in recorded
    assert ("agent_dialogue_turn", "cached", 100) in recorded


def test_intent_text_excludes_input_placeholders() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.message_parser import (
        NormalizedMessage,
        SegmentNode,
    )

    normalized = NormalizedMessage(
        plain_text="@100帮我看看[图片]",
        segments=[
            SegmentNode("at", {"qq": "100"}, "@100"),
            SegmentNode("text", {"text": "帮我看看"}, "帮我看看"),
            SegmentNode("image", {"file": "x"}, "[图片]"),
        ],
        mentions=[100],
        media_refs=[{"type": "image"}],
    )

    assert normalized.intent_text() == "帮我看看"


def test_trigger_metadata_is_process_only_and_not_persisted() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.message_parser import NormalizedMessage

    normalized = NormalizedMessage(plain_text="hello", segments=[])
    normalized.trigger_source = "reply"
    normalized.trigger_signals = {"mention": False, "reply": True, "wake_word": False}

    assert "trigger_source" not in normalized.as_dict()
    assert "trigger_signals" not in normalized.as_dict()
    assert "trigger_source" not in normalized.storage_dict()
    assert "trigger_signals" not in normalized.storage_dict()


@pytest.mark.parametrize(
    "case",
    [
        SimpleNamespace(
            to_me=True, reply=None, text="hello", reply_on=False,
            wake_on=False, source="mention",
        ),
        # OneBot may set to_me for a reply; reply evidence must win that flag.
        SimpleNamespace(
            to_me=True, reply=100, text="继续", reply_on=True,
            wake_on=False, source="reply",
        ),
        SimpleNamespace(
            to_me=False, reply=100, text="继续", reply_on=True,
            wake_on=False, source="reply",
        ),
        SimpleNamespace(
            to_me=False, reply=100, text="继续", reply_on=False,
            wake_on=False, source=None,
        ),
        SimpleNamespace(
            to_me=False, reply=None, text="yawn 在吗", reply_on=False,
            wake_on=True, source="wake_word",
        ),
        SimpleNamespace(
            to_me=False, reply=None, text="yawn 在吗", reply_on=False,
            wake_on=False, source=None,
        ),
        SimpleNamespace(
            to_me=False, reply=300, text="普通消息", reply_on=True,
            wake_on=True, source=None,
        ),
        SimpleNamespace(
            to_me=False, reply=None, text="ordinary conversation", reply_on=True,
            wake_on=True, source=None,
        ),
    ],
)
def test_explicit_call_trigger_matrix(case: SimpleNamespace) -> None:
    _load_agent_modules()
    from typing import cast

    from nonebot.adapters.onebot.v11 import Bot as OneBotBot
    from nonebot.adapters.onebot.v11 import GroupMessageEvent

    from src.plugins.yawn_core.yawn_agent.agent import resolve_trigger

    class Bot:
        self_id = "100"
        nickname = ""

    class Event:
        group_id = 1
        message: tuple[object, ...] = ()

        def __init__(self) -> None:
            self.to_me = bool(case.to_me)
            reply_user_id = case.reply
            self.reply = (
                SimpleNamespace(sender=SimpleNamespace(user_id=reply_user_id))
                if reply_user_id is not None
                else None
            )

        def get_user_id(self) -> str:
            return "200"

        def get_plaintext(self) -> str:
            return str(case.text)

    decision = resolve_trigger(
        cast("GroupMessageEvent", Event()),
        cast("OneBotBot", Bot()),
        reply_trigger_enabled=bool(case.reply_on),
        explicit_wakeup_enabled=bool(case.wake_on),
    )
    expected_source = case.source
    assert decision.respond is (expected_source is not None)
    assert decision.source == expected_source
    if expected_source == "reply":
        assert decision.replied is True
        assert decision.mentioned is False


def test_reply_to_bot_detection_falls_back_to_direct_reply_chain_only() -> None:
    _load_agent_modules()
    from typing import cast

    from nonebot.adapters.onebot.v11 import Bot as OneBotBot
    from nonebot.adapters.onebot.v11 import GroupMessageEvent

    from src.plugins.yawn_core.yawn_agent.agent import resolve_trigger
    from src.plugins.yawn_core.yawn_agent.message_parser import NormalizedMessage

    class Bot:
        self_id = "100"
        nickname = ""

    class Event:
        group_id = 1
        message: tuple[object, ...] = ()
        reply = None
        to_me = False

        def get_user_id(self) -> str:
            return "200"

        def get_plaintext(self) -> str:
            return "没有唤醒词"

    direct = NormalizedMessage(
        plain_text="没有唤醒词",
        segments=[],
        reply_chain=[
            {"message_id": 1, "user_id": 100, "nickname": "Yawn", "text": "机器人的话"}
        ],
    )
    direct_decision = resolve_trigger(
        cast("GroupMessageEvent", Event()),
        cast("OneBotBot", Bot()),
        normalized=direct,
    )
    assert direct_decision.respond is True
    assert direct_decision.source == "reply"

    disabled = resolve_trigger(
        cast("GroupMessageEvent", Event()),
        cast("OneBotBot", Bot()),
        reply_trigger_enabled=False,
        normalized=direct,
    )
    assert disabled.respond is False
    assert disabled.source is None
    assert disabled.replied is True

    deeper_only = NormalizedMessage(
        plain_text="没有唤醒词",
        segments=[],
        reply_chain=[
            {"message_id": 2, "user_id": 300, "nickname": "别人", "text": "别人的话"},
            {"message_id": 1, "user_id": 100, "nickname": "Yawn", "text": "机器人的话"},
        ],
    )
    deeper_decision = resolve_trigger(
        cast("GroupMessageEvent", Event()),
        cast("OneBotBot", Bot()),
        normalized=deeper_only,
    )
    assert deeper_decision.respond is False
    assert deeper_decision.source is None


def test_bot_self_message_never_becomes_an_explicit_call() -> None:
    _load_agent_modules()
    from typing import cast

    from nonebot.adapters.onebot.v11 import Bot as OneBotBot
    from nonebot.adapters.onebot.v11 import GroupMessageEvent

    from src.plugins.yawn_core.yawn_agent.agent import resolve_trigger

    event = SimpleNamespace(
        get_user_id=lambda: "100",
        get_plaintext=lambda: "yawn",
        message=(),
        reply=None,
        to_me=True,
    )
    bot = SimpleNamespace(self_id="100", nickname="Yawn")
    decision = resolve_trigger(
        cast("GroupMessageEvent", event), cast("OneBotBot", bot)
    )
    assert decision.respond is False
    assert decision.source is None


def test_prompt_cache_key_includes_model_and_persona_version() -> None:
    _, _, build_messages = _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.prompt import prompt_cache_key

    tools = [{"type": "function", "function": {"name": "send_text"}}]
    first, _ = build_messages(
        persona={"name": "Yawn"}, tools=tools, context={"messages": []}, user_prompt="a"
    )
    second, _ = build_messages(
        persona={"name": "Yawn"},
        tools=tools,
        context={"messages": [1]},
        user_prompt="b",
    )
    assert first[0] == second[0]
    assert prompt_cache_key(
        persona={"name": "Yawn"}, tools=tools, model="advanced", persona_version=1
    ) != prompt_cache_key(
        persona={"name": "Yawn"}, tools=tools, model="ordinary", persona_version=1
    )
    assert prompt_cache_key(
        persona={"name": "Yawn"}, tools=[], model="ordinary", persona_version=1
    ) == prompt_cache_key(
        persona={"name": "Yawn"},
        tools=[{"type": "function", "function": {"name": "send_message"}}],
        model="ordinary",
        persona_version=1,
    )


class _FixedRoll:
    """可控概率骰子：should_proactively_speak 接受 random.Random 鸭子类型。"""

    def __init__(self, value: float) -> None:
        self.value = value

    def random(self) -> float:
        return self.value


def _make_proactive_config(**overrides: object):
    from types import SimpleNamespace
    from typing import cast

    from src.plugins.yawn_core.data_models.group_agent_config import GroupAgentConfig

    values: dict[str, object] = {
        "group_id": 1,
        "enabled": True,
        "proactive_enabled": True,
        "daily_limit": 30,
        "cooldown_minutes": 8,
        "idle_threshold_minutes": 15,
        "proactive_probability": 0.35,
        "proactive_active_enabled": True,
        "proactive_active_probability": 0.25,
        "proactive_active_window_minutes": 12,
        "recent_response_fingerprints": [],
        # Baseline proactive tests emulate a pre-P4 row after the P6 cleanup
        # migration materializes its historical cadence into explicit v2 traits.
        "persona_enabled": True,
        "persona_profile": {
            "schema_version": 2,
            "preset_id": "natural",
            "voice": {
                "warmth": 2,
                "humor": 1,
                "directness": 2,
                "verbosity": 1,
                "expressiveness": 1,
            },
            "social": {
                "sociability": 4,
                "followup_tendency": 4,
                "reaction_tendency": 2,
            },
        },
    }
    values.update(overrides)
    return cast("GroupAgentConfig", SimpleNamespace(**values))


def test_persona_p4_sociability_narrows_runtime_participation_probability() -> None:
    _load_agent_modules()
    from datetime import timedelta

    from src.plugins.yawn_core.yawn_agent.context import ActivitySnapshot, now_beijing
    from src.plugins.yawn_core.yawn_agent.proactive import should_proactively_speak

    quiet_profile = _make_structured_persona_config(
        preset_id="quiet_observer",
        sociability=0,
        followup_tendency=0,
        reaction_tendency=1,
    ).persona_profile
    lively_profile = _make_structured_persona_config(
        preset_id="lively_sidekick",
        sociability=4,
        followup_tendency=3,
        reaction_tendency=4,
    ).persona_profile
    common = {
        "persona_schema_version": 2,
        "persona_enabled": True,
        "persona_override": {},
        "persona": "友好、自然、简洁的群友",
        "persona_version": 4,
    }
    quiet = _make_proactive_config(persona_profile=quiet_profile, **common)
    lively = _make_proactive_config(persona_profile=lively_profile, **common)
    now = now_beijing()

    active = ActivitySnapshot(
        last_message_at=now - timedelta(minutes=3),
        last_agent_at=now - timedelta(minutes=40),
        member_messages_60m=5,
        last_member_message_at=now - timedelta(minutes=3),
    )
    assert should_proactively_speak(quiet, active, now, rng=_FixedRoll(0.1)) is None
    assert (
        should_proactively_speak(lively, active, now, rng=_FixedRoll(0.1))
        == "active"
    )

    warmup = ActivitySnapshot(
        last_message_at=now - timedelta(minutes=40),
        last_agent_at=now - timedelta(minutes=40),
        member_messages_60m=2,
        last_member_message_at=now - timedelta(minutes=40),
    )
    assert should_proactively_speak(quiet, warmup, now, rng=_FixedRoll(0.1)) is None
    assert (
        should_proactively_speak(lively, warmup, now, rng=_FixedRoll(0.1))
        == "warmup"
    )


def test_persona_p4_followup_prompt_exposes_behavior_without_relaxing_limits() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.proactive import _build_user_prompt

    profile = _make_structured_persona_config(
        preset_id="quiet_observer",
        sociability=0,
        followup_tendency=0,
        reaction_tendency=1,
    ).persona_profile
    config = _make_proactive_config(
        persona_schema_version=2,
        persona_profile=profile,
        persona_enabled=True,
        persona_override={},
        persona="友好、自然、简洁的群友",
        persona_version=4,
    )
    prompt = _build_user_prompt("followup", config, turn=1)
    assert "尽量少抢话" in prompt
    assert "不要主动续聊" in prompt
    assert "reaction 极少使用" in prompt
    assert "当前 Persona 最多 1 条" in prompt


def test_proactive_active_mode_triggers_on_topic_gap() -> None:
    _load_agent_modules()
    from datetime import timedelta

    from src.plugins.yawn_core.yawn_agent.context import ActivitySnapshot, now_beijing
    from src.plugins.yawn_core.yawn_agent.proactive import should_proactively_speak

    make_config = _make_proactive_config
    now = now_beijing()
    snapshot = ActivitySnapshot(
        last_message_at=now - timedelta(minutes=3),
        last_agent_at=now - timedelta(minutes=40),
        member_messages_60m=5,
        last_member_message_at=now - timedelta(minutes=3),
    )
    assert (
        should_proactively_speak(make_config(), snapshot, now, rng=_FixedRoll(0.0))
        == "active"
    )


def test_proactive_active_mode_rejects_rushing_and_stale_gap() -> None:
    _load_agent_modules()
    from datetime import timedelta

    from src.plugins.yawn_core.yawn_agent.context import ActivitySnapshot, now_beijing
    from src.plugins.yawn_core.yawn_agent.proactive import should_proactively_speak

    make_config = _make_proactive_config
    now = now_beijing()

    def snapshot(member_idle_minutes: float):
        return ActivitySnapshot(
            last_message_at=now - timedelta(minutes=member_idle_minutes),
            last_agent_at=now - timedelta(minutes=40),
            member_messages_60m=5,
            last_member_message_at=now - timedelta(minutes=member_idle_minutes),
        )

    # 普通群真人消息刚发 15 秒：先等满 30 秒把零散消息合起来。
    assert (
        should_proactively_speak(
            make_config(), snapshot(0.25), now, rng=_FixedRoll(0.0)
        )
        is None
    )
    # 1 分钟插话窗口不再与 90 秒最小间隔形成永远不可达的死区。
    assert (
        should_proactively_speak(
            make_config(proactive_active_window_minutes=1),
            snapshot(0.75),
            now,
            rng=_FixedRoll(0.0),
        )
        == "active"
    )
    # 真人消息 13 分钟前：插话窗口(12 分钟)已过，也未到暖场阈值(15 分钟)。
    assert (
        should_proactively_speak(make_config(), snapshot(13), now, rng=_FixedRoll(0.0))
        is None
    )
    # 窗口内有真人消息但 60 分钟总量不足：群里并没有在聊。
    quiet = ActivitySnapshot(
        last_message_at=now - timedelta(minutes=3),
        last_agent_at=now - timedelta(minutes=40),
        member_messages_60m=1,
        last_member_message_at=now - timedelta(minutes=3),
    )
    assert (
        should_proactively_speak(make_config(), quiet, now, rng=_FixedRoll(0.0)) is None
    )
    # 持续刷屏群无需等待 30 秒静默，否则高活跃群反而永远无法候选。
    busy = ActivitySnapshot(
        last_message_at=now - timedelta(seconds=2),
        last_agent_at=now - timedelta(minutes=40),
        member_messages_60m=20,
        member_messages_5m=6,
        member_participants_5m=2,
        last_member_message_at=now - timedelta(seconds=2),
    )
    assert (
        should_proactively_speak(make_config(), busy, now, rng=_FixedRoll(0.0))
        == "active"
    )
    # 插话开关关闭后只剩暖场路径。
    assert (
        should_proactively_speak(
            make_config(proactive_active_enabled=False),
            snapshot(3),
            now,
            rng=_FixedRoll(0.0),
        )
        is None
    )


def test_proactive_policy_stops_when_participation_is_disabled() -> None:
    _load_agent_modules()
    from datetime import timedelta

    from src.plugins.yawn_core.yawn_agent.context import ActivitySnapshot, now_beijing
    from src.plugins.yawn_core.yawn_agent.proactive import should_proactively_speak

    now = now_beijing()
    snapshot = ActivitySnapshot(
        last_message_at=now - timedelta(minutes=40),
        last_agent_at=now - timedelta(minutes=40),
        member_messages_60m=5,
        last_member_message_at=now - timedelta(minutes=40),
    )
    assert (
        should_proactively_speak(
            _make_proactive_config(proactive_enabled=False),
            snapshot,
            now,
            rng=_FixedRoll(0.0),
        )
        is None
    )


def test_proactive_warmup_mode_triggers_after_idle_threshold() -> None:
    _load_agent_modules()
    from datetime import timedelta

    from src.plugins.yawn_core.yawn_agent.context import ActivitySnapshot, now_beijing
    from src.plugins.yawn_core.yawn_agent.proactive import should_proactively_speak

    make_config = _make_proactive_config
    now = now_beijing()
    snapshot = ActivitySnapshot(
        last_message_at=now - timedelta(minutes=40),
        last_agent_at=now - timedelta(minutes=40),
        member_messages_60m=2,
        last_member_message_at=now - timedelta(minutes=40),
    )
    assert (
        should_proactively_speak(make_config(), snapshot, now, rng=_FixedRoll(0.0))
        == "warmup"
    )
    # 概率骰子未中时同样场景不触发。
    assert (
        should_proactively_speak(make_config(), snapshot, now, rng=_FixedRoll(0.99))
        is None
    )


def test_proactive_rejects_cooldown_and_daily_limit() -> None:
    _load_agent_modules()
    from datetime import timedelta

    from src.plugins.yawn_core.yawn_agent.context import ActivitySnapshot, now_beijing
    from src.plugins.yawn_core.yawn_agent.proactive import should_proactively_speak

    make_config = _make_proactive_config
    now = now_beijing()
    # 主动→主动冷却看 last_proactive_at：5 分钟前主动过、冷却 8 分钟，拒绝。
    cooling = ActivitySnapshot(
        last_message_at=now - timedelta(minutes=40),
        last_agent_at=now - timedelta(minutes=60),
        last_proactive_at=now - timedelta(minutes=5),
    )
    assert (
        should_proactively_speak(make_config(), cooling, now, rng=_FixedRoll(0.0))
        is None
    )
    exhausted = ActivitySnapshot(
        last_message_at=now - timedelta(minutes=40),
        last_agent_at=now - timedelta(minutes=60),
        proactive_today=30,
    )
    assert (
        should_proactively_speak(make_config(), exhausted, now, rng=_FixedRoll(0.0))
        is None
    )


def test_proactive_post_reply_guard_short_and_decoupled() -> None:
    _load_agent_modules()
    from datetime import timedelta

    from src.plugins.yawn_core.yawn_agent.context import ActivitySnapshot, now_beijing
    from src.plugins.yawn_core.yawn_agent.proactive import should_proactively_speak

    make_config = _make_proactive_config
    now = now_beijing()

    def chatting(last_agent_minutes: float):
        return ActivitySnapshot(
            last_message_at=now - timedelta(minutes=3),
            last_agent_at=now - timedelta(minutes=last_agent_minutes),
            member_messages_60m=5,
            last_member_message_at=now - timedelta(minutes=3),
        )

    # 被@答话在 8 分钟前：已超出 5 分钟短守卫，不再封锁主动插话
    #（旧逻辑会被整个 20 分钟冷却挡掉）。
    assert (
        should_proactively_speak(
            make_config(), chatting(8), now, rng=_FixedRoll(0.0)
        )
        == "active"
    )
    # 被动回复 3 分钟前：短守卫期内，拒绝。
    assert (
        should_proactively_speak(
            make_config(), chatting(3), now, rng=_FixedRoll(0.0)
        )
        is None
    )


def test_warmup_probability_ramps_with_idle_and_caps() -> None:
    _load_agent_modules()
    from datetime import timedelta

    from src.plugins.yawn_core.yawn_agent.context import ActivitySnapshot, now_beijing
    from src.plugins.yawn_core.yawn_agent.proactive import (
        _warmup_probability,
        should_proactively_speak,
    )

    make_config = _make_proactive_config
    config = make_config()
    now = now_beijing()
    threshold = int(config.idle_threshold_minutes) * 60
    # 阈值时为基准值，两倍阈值翻倍到 0.7 后封顶 0.6。
    assert round(_warmup_probability(config, threshold), 6) == 0.35
    assert round(_warmup_probability(config, threshold * 2), 6) == 0.6
    assert round(_warmup_probability(config, threshold * 10), 6) == 0.6

    # 行为验证：同一颗未中的骰子，冷场更久后能命中暖场。
    def frozen(idle_minutes: float):
        return ActivitySnapshot(
            last_message_at=now - timedelta(minutes=idle_minutes),
            last_agent_at=now - timedelta(minutes=60),
        )

    assert (
        should_proactively_speak(
            make_config(), frozen(15), now, rng=_FixedRoll(0.5)
        )
        is None
    )
    assert (
        should_proactively_speak(
            make_config(), frozen(30), now, rng=_FixedRoll(0.5)
        )
        == "warmup"
    )


def test_skip_backoff_leaves_half_cooldown() -> None:
    _load_agent_modules()
    from datetime import timedelta

    from src.plugins.yawn_core.yawn_agent.context import now_beijing
    from src.plugins.yawn_core.yawn_agent.proactive import _skip_backoff_timestamp

    now = now_beijing()
    # 默认 8 分钟冷却：跳过后剩余 4 分钟，而不是整个冷却期。
    assert _skip_backoff_timestamp(now, 8) == now - timedelta(minutes=4)
    # 冷却配得极短时退避也不低于 2 分钟。
    assert _skip_backoff_timestamp(now, 0) == now + timedelta(minutes=2)


def test_proactive_decision_parses_speak_true() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.proactive import _decide_proactive_reply

    decision = _decide_proactive_reply(
        '{"speak": true, "topic": "周末爬山", "reason": "话题正热", '
        '"text": " 山上现在应该挺凉的 "}'
    )
    assert decision.should_speak is True
    assert decision.action == "speak"
    assert decision.text == "山上现在应该挺凉的"
    assert decision.topic == "周末爬山"
    assert decision.reason == "话题正热"


def test_proactive_decision_accepts_structured_reply_at_face_message() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.proactive import _decide_proactive_reply

    decision = _decide_proactive_reply(
        '{"action":"speak","speak":true,"topic":"接话","reason":"回应刚才的人",'
        '"message":{"segments":['
        '{"type":"reply","message_id":123},'
        '{"type":"at","user_id":456},'
        '{"type":"text","text":"这个确实"},'
        '{"type":"face","id":14}]}}'
    )

    assert decision.should_speak is True
    assert decision.text == "这个确实"
    assert [item["type"] for item in decision.segments] == [
        "reply",
        "at",
        "text",
        "face",
    ]
    assert "message" in __import__(
        "src.plugins.yawn_core.yawn_agent.proactive", fromlist=["_JSON_PROTOCOL"]
    )._JSON_PROTOCOL


def test_proactive_decision_parses_speak_false_with_reason() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.proactive import _decide_proactive_reply

    decision = _decide_proactive_reply(
        '{"speak": false, "topic": "两人争论配置", "reason": "正在争论", "text": ""}'
    )
    assert decision.should_speak is False
    assert decision.action == "wait"
    assert decision.text == ""
    assert decision.topic == "两人争论配置"
    assert decision.reason == "正在争论"


def test_proactive_decision_tolerates_code_fence_json() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.proactive import _decide_proactive_reply

    decision = _decide_proactive_reply(
        '```json\n{"speak": true, "topic": "晚饭", '
        '"reason": "", "text": "我也馋了"}\n```'
    )
    assert decision.should_speak is True
    assert decision.text == "我也馋了"
    assert decision.reason == "模型未说明理由"


def test_proactive_decision_supports_wait_and_close_actions() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.proactive import _decide_proactive_reply

    waiting = _decide_proactive_reply(
        '{"action":"wait","speak":false,"reason":"群友正在互聊","text":""}'
    )
    closing = _decide_proactive_reply(
        '{"action":"close","speak":false,"reason":"话题结束","text":""}'
    )
    assert waiting.action == "wait"
    assert closing.action == "close"
    assert not waiting.should_speak
    assert not closing.should_speak


def test_proactive_decision_speak_true_without_text_is_skipped() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.proactive import _decide_proactive_reply

    decision = _decide_proactive_reply('{"speak": true, "text": "  "}')
    assert decision.should_speak is False
    assert decision.text == ""


def test_proactive_decision_rejects_broken_json_fragment() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.proactive import _decide_proactive_reply

    # 疑似 JSON 的碎片不可发到群里，只能静默跳过。
    assert _decide_proactive_reply('{"speak": tru').should_speak is False
    assert _decide_proactive_reply('```{"text": "hi"').should_speak is False
    assert _decide_proactive_reply("   ").should_speak is False


def test_proactive_decision_falls_back_to_plain_text() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.proactive import _decide_proactive_reply

    decision = _decide_proactive_reply(" 这模型没走 JSON 协议，直接说了一句 ")
    assert decision.should_speak is True
    assert decision.text == "这模型没走 JSON 协议，直接说了一句"
    assert decision.topic is None


def test_proactive_prompts_declare_json_protocol_and_silence() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.proactive import (
        _ACTIVE_INTERJECT_PROMPT,
        _JSON_PROTOCOL,
        _WARMUP_PROMPT,
    )

    for key in ("action", "speak", "topic", "reason", "text"):
        assert key in _JSON_PROTOCOL
    # 插话与暖场都要把"保持沉默"作为合法结果写进指令。
    assert "speak=false" in _ACTIVE_INTERJECT_PROMPT
    assert "speak=false" in _WARMUP_PROMPT
    # 插话必须先理解内容再决策，且禁止泛泛附和。
    assert "先读懂" in _ACTIVE_INTERJECT_PROMPT
    assert "泛泛附和" in _ACTIVE_INTERJECT_PROMPT


def test_followup_prompt_uses_memory_naturally_and_can_wait() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.proactive import _build_user_prompt

    prompt = _build_user_prompt("followup", _make_proactive_config(), turn=2)
    for phrase in ("群总结", "人物画像", "人物关系", "自然隐式", "当前消息"):
        assert phrase in prompt
    assert "wait" in prompt
    assert "close" in prompt
    assert "连续同义复述" in prompt
    assert "不要靠结尾反问" in prompt
    assert "第 2 条候选发言" in prompt


def test_proactive_user_prompt_injects_recent_lines() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.proactive import _build_user_prompt

    config = _make_proactive_config(
        recent_response_fingerprints=[
            {"text": "早期的一条", "at": "t0", "input": "proactive"},
            {"text": "昨天聊游戏时说的", "at": "t1", "input": "proactive"},
            {"text": "对话路径的回复", "at": "t2", "input": "dialogue"},
            {"text": "", "at": "t3", "input": "proactive"},
        ]
    )
    prompt = _build_user_prompt("active", config)
    assert "昨天聊游戏时说的" in prompt
    assert "对话路径的回复" not in prompt
    assert "不要重复相近的说法" in prompt
    # 没有近期主动发言时不注入该段。
    bare = _build_user_prompt("warmup", _make_proactive_config())
    assert "最近主动发言过" not in bare


@pytest.mark.asyncio
async def test_proactive_generation_uses_light_task_and_safe_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent import proactive

    calls: list[dict[str, object]] = []

    async def fake_complete(
        _messages: list[object], **kwargs: object
    ) -> str:
        calls.append(kwargs)
        return "ok"

    monkeypatch.setattr(proactive, "complete", fake_complete)
    monkeypatch.setattr(proactive.ai_config, "ai_max_tokens", 1024)
    assert await proactive._generate_proactive_reply([]) == "ok"
    monkeypatch.setattr(proactive.ai_config, "ai_max_tokens", 4096)
    assert await proactive._generate_proactive_reply([]) == "ok"

    assert [call["max_tokens"] for call in calls] == [2048, 4096]
    assert {call["task"] for call in calls} == {"agent_proactive"}
    assert {call["timeout"] for call in calls} == {25.0}


@pytest.mark.asyncio
async def test_unsupported_dialogue_profile_preflights_images_through_caption_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent import dialogue

    normalized = SimpleNamespace(prompt_text=lambda: "看看这张图")

    async def describe(*_args: object, **_kwargs: object) -> str:
        return "[图片转述] 一只猫"

    monkeypatch.setattr(
        dialogue,
        "resolve_llm_request",
        lambda _task: SimpleNamespace(multimodal="unsupported"),
    )
    monkeypatch.setattr(dialogue, "_describe_images", describe)
    prompt, blocks = await dialogue._prepare_media_prompt(
        1,
        normalized,
        object(),
        object(),
        [{"type": "image_url", "image_url": {"url": "data:image/png;base64,eA=="}}],
        [],
        ["digest"],
    )

    assert "[图片转述] 一只猫" in prompt
    assert blocks == []


@pytest.mark.asyncio
async def test_image_caption_uses_configured_image_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent import dialogue

    captured: dict[str, object] = {}

    async def complete(_messages: object, **kwargs: object) -> str:
        captured.update(kwargs)
        return "一只猫"

    monkeypatch.setattr(dialogue, "complete", complete)
    result = await dialogue._caption_single_image(
        1,
        SimpleNamespace(prompt_text=lambda: "这是什么"),
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,eA=="}},
    )

    assert result == "一只猫"
    assert captured["task"] == "agent_image"


def test_conversation_batch_deadline_uses_quiet_or_hard_limit() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent import conversation

    session = conversation.ConversationSession(
        session_id=1,
        started_at=100.0,
        last_bot_at=100.0,
        bot_turns=1,
        topic="测试",
        batch_first_at=110.0,
        batch_last_at=140.0,
    )
    # last+20=160，但 first+45=155，持续刷屏也必须在硬期限评估。
    assert conversation.batch_due_at(session) == 155.0
    session.batch_last_at = 120.0
    assert conversation.batch_due_at(session) == 140.0


def test_conversation_turn_limit_closes_session() -> None:
    _load_agent_modules()
    import time

    from src.plugins.yawn_core.yawn_agent import conversation

    conversation.reset_for_tests()
    now = time.monotonic()
    for turn in range(conversation.CONVERSATION_MAX_BOT_TURNS):
        conversation.mark_bot_reply(
            9, 100, topic="同一话题", source="test", now=now + turn
        )
    assert conversation.current_conversation(9, 100) is None


def test_close_group_conversations_only_closes_target_group() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent import conversation

    conversation.reset_for_tests()
    conversation.mark_bot_reply(9, 100, topic="测试", source="test")
    conversation.mark_bot_reply(10, 100, topic="测试", source="test")
    conversation.mark_bot_reply(9, 200, topic="其他群", source="test")

    assert conversation.close_group_conversations(100, reason="关闭开关") == 2
    assert conversation.current_conversation(9, 100) is None
    assert conversation.current_conversation(10, 100) is None
    assert conversation.current_conversation(9, 200) is not None
    conversation.reset_for_tests()


def test_conversation_evaluation_and_wait_limits() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent import conversation
    from src.plugins.yawn_core.yawn_agent.context import now_beijing

    def batch() -> Any:
        current = conversation.current_conversation(9, 100)
        assert current is not None
        return conversation.ConversationBatch(
            key=(9, 100),
            session_id=current.session_id,
            topic=current.topic,
            bot_turns=current.bot_turns,
            user_ids=(1,),
            message_ids=(1,),
            cutoff_at=now_beijing(),
        )

    conversation.reset_for_tests()
    conversation.mark_bot_reply(9, 100, topic="测试", source="test")
    for _ in range(2):
        current_batch = batch()
        assert conversation.begin_followup_evaluation(current_batch)
        conversation.finish_followup_evaluation(current_batch, "wait")
    current = conversation.current_conversation(9, 100)
    assert current is not None and current.consecutive_waits == 2
    current_batch = batch()
    assert conversation.begin_followup_evaluation(current_batch)
    conversation.finish_followup_evaluation(current_batch, "speak")
    current = conversation.current_conversation(9, 100)
    assert current is not None and current.consecutive_waits == 0
    for action in ("wait", "speak", "wait"):
        current_batch = batch()
        assert conversation.begin_followup_evaluation(current_batch)
        conversation.finish_followup_evaluation(current_batch, action)
    assert conversation.current_conversation(9, 100) is None

    conversation.mark_bot_reply(9, 100, topic="测试", source="test")
    for _ in range(3):
        current_batch = batch()
        assert conversation.begin_followup_evaluation(current_batch)
        conversation.finish_followup_evaluation(current_batch, "wait")
    assert conversation.current_conversation(9, 100) is None


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 0.5) -> None:
    import asyncio
    import time

    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_conversation_batches_messages_and_preserves_next_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _load_agent_modules()
    import asyncio

    from src.plugins.yawn_core.yawn_agent import conversation, proactive
    from src.plugins.yawn_core.yawn_agent.context import now_beijing

    await conversation.shutdown_conversations()
    monkeypatch.setattr(conversation, "CONVERSATION_QUIET_SECONDS", 0.01)
    monkeypatch.setattr(conversation, "CONVERSATION_MAX_BATCH_SECONDS", 0.03)
    batches: list[Any] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def handler(batch: Any) -> None:
        batches.append(batch)
        if len(batches) == 1:
            first_started.set()
            await release_first.wait()

    conversation.set_followup_handler(handler)
    conversation.mark_bot_reply(9, 100, topic="测试话题", source="test")
    conversation.observe_member_message(
        9,
        100,
        user_id=1,
        message_id=11,
        explicit_trigger=False,
        observed_at=now_beijing(),
    )
    conversation.observe_member_message(
        9,
        100,
        user_id=2,
        message_id=12,
        explicit_trigger=False,
        observed_at=now_beijing(),
    )
    await asyncio.wait_for(first_started.wait(), timeout=0.5)
    # 第一批已经入选后到达的新消息不能取消候选，也不能被清掉；进入下一批。
    conversation.observe_member_message(
        9,
        100,
        user_id=3,
        message_id=13,
        explicit_trigger=False,
        observed_at=now_beijing(),
    )
    release_first.set()
    await _wait_until(lambda: len(batches) == 2)
    assert batches[0].message_ids == (11, 12)
    assert batches[1].message_ids == (13,)
    await conversation.shutdown_conversations()
    conversation.set_followup_handler(proactive._process_followup)


@pytest.mark.asyncio
async def test_explicit_trigger_cancels_only_unselected_auto_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _load_agent_modules()
    import asyncio

    from src.plugins.yawn_core.yawn_agent import conversation, proactive
    from src.plugins.yawn_core.yawn_agent.context import now_beijing

    await conversation.shutdown_conversations()
    monkeypatch.setattr(conversation, "CONVERSATION_QUIET_SECONDS", 0.01)
    called: list[Any] = []

    async def handler(batch: Any) -> None:
        called.append(batch)

    conversation.set_followup_handler(handler)
    conversation.mark_bot_reply(9, 100, topic="测试话题", source="test")
    conversation.observe_member_message(
        9,
        100,
        user_id=1,
        message_id=11,
        explicit_trigger=False,
        observed_at=now_beijing(),
    )
    conversation.observe_member_message(
        9,
        100,
        user_id=2,
        message_id=12,
        explicit_trigger=True,
        observed_at=now_beijing(),
    )
    await asyncio.sleep(0.04)
    assert called == []
    assert conversation.current_conversation(9, 100) is not None
    await conversation.shutdown_conversations()
    conversation.set_followup_handler(proactive._process_followup)


def test_complete_with_tools_omits_empty_tools_param() -> None:
    """空 tools 列表不得透传：OpenAI 兼容端点对空数组直接 400。"""

    _load_agent_modules()
    import asyncio
    from types import SimpleNamespace

    import src.plugins.yawn_core.llm as llm_module

    class FakeCompletions:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def create(self, **kwargs: object):
            self.calls.append(kwargs)
            message = SimpleNamespace(content="ok", tool_calls=None)
            choice = SimpleNamespace(message=message, finish_reason="stop")
            return SimpleNamespace(choices=[choice])

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    original = llm_module.get_client
    llm_module.get_client = lambda _provider="default": fake_client
    try:
        empty = asyncio.run(
            llm_module.complete_with_tools(
                [{"role": "user", "content": "hi"}],
                [],
                task="agent_proactive",
            )
        )
        assert empty is not None
        assert "tools" not in fake_client.chat.completions.calls[0]

        tools = [{"type": "function", "function": {"name": "t"}}]
        asyncio.run(
            llm_module.complete_with_tools(
                [{"role": "user", "content": "hi"}],
                tools,  # pyright: ignore[reportArgumentType]
                task="agent_proactive",
            )
        )
        assert fake_client.chat.completions.calls[1]["tools"] == tools
    finally:
        llm_module.get_client = original
