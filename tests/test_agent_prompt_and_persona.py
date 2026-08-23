# ruff: noqa: TC001,TC002,PLR2004
from __future__ import annotations

import sys
from pathlib import Path

import nonebot

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
        canonical_persona,
        parse_persona_assignments,
    )
    from src.plugins.yawn_core.yawn_agent.prompt import build_messages

    return canonical_persona, parse_persona_assignments, build_messages


def test_persona_assignments_are_restricted_and_stable() -> None:
    canonical_persona, parse_persona_assignments, _ = _load_agent_modules()
    persona = parse_persona_assignments(["name=Yawn", "style=短句", "tone=温和"])
    assert persona["speech_style"] == "短句"
    assert canonical_persona(persona).startswith('{"name":"Yawn"')


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
    assert len(messages) == 4
    stable_json = messages[1]["content"]
    volatile_json = messages[2]["content"]
    assert "daily:2026-08-22" in stable_json
    assert "public_daily:2026-08-21" in stable_json
    assert "active_topic" not in stable_json
    assert "晚饭安排" not in volatile_json
    assert "阿眠" in volatile_json and "mentions" in volatile_json

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
    assert stable_context_key(context) == stable_context_key(evolved)
    assert stable_context_key(context) != stable_context_key(
        {**evolved, "group_name": "改名后的群"}
    )

    # 缺省上下文（无稳定记忆）时稳定层不携带空 memories 噪声。
    stable, volatile = split_context({"active_topic": "a"})
    assert stable == {}
    assert volatile["memories"] == []


def test_trigger_modes_distinguish_mentions_replies_and_explicit_wakeup() -> None:
    _load_agent_modules()
    from typing import cast

    from nonebot.adapters.onebot.v11 import Bot as OneBotBot
    from nonebot.adapters.onebot.v11 import GroupMessageEvent

    from src.plugins.yawn_core.yawn_agent.agent import should_respond

    class Bot:
        self_id = "100"

    class Event:
        group_id = 1
        message: tuple[object, ...] = ()
        reply = None

        def get_user_id(self) -> str:
            return "200"

        def get_plaintext(self) -> str:
            return "小助手 在吗"

    # 假对象只实现 should_respond 用到的鸭子类型接口，用 cast 满足静态检查。
    event = cast("GroupMessageEvent", Event())
    bot = cast("OneBotBot", Bot())
    assert should_respond(event, bot, "explicit_wakeup")
    assert not should_respond(event, bot, "mention_only")


def test_reply_to_bot_detection_falls_back_to_reply_chain() -> None:
    _load_agent_modules()
    from typing import cast

    from nonebot.adapters.onebot.v11 import Bot as OneBotBot
    from nonebot.adapters.onebot.v11 import GroupMessageEvent

    from src.plugins.yawn_core.yawn_agent.agent import should_respond
    from src.plugins.yawn_core.yawn_agent.message_parser import NormalizedMessage

    class Bot:
        self_id = "100"

    class Event:
        group_id = 1
        message: tuple[object, ...] = ()
        reply = None

        def get_user_id(self) -> str:
            return "200"

        def get_plaintext(self) -> str:
            return "这句话没有唤醒词"

    direct = NormalizedMessage(
        plain_text="这句话没有唤醒词",
        segments=[],
        reply_chain=[
            {"message_id": 1, "user_id": 100, "nickname": "Yawn", "text": "机器人的话"}
        ],
    )
    assert should_respond(
        cast("GroupMessageEvent", Event()), cast("OneBotBot", Bot()), normalized=direct
    )

    deeper_only = NormalizedMessage(
        plain_text="这句话没有唤醒词",
        segments=[],
        reply_chain=[
            {"message_id": 2, "user_id": 300, "nickname": "别人", "text": "别人的话"},
            {"message_id": 1, "user_id": 100, "nickname": "Yawn", "text": "机器人的话"},
        ],
    )
    assert not should_respond(
        cast("GroupMessageEvent", Event()),
        cast("OneBotBot", Bot()),
        normalized=deeper_only,
    )


def test_mention_detection_includes_adapter_to_me_after_at_segment_removed() -> None:
    _load_agent_modules()
    from typing import cast

    from nonebot.adapters.onebot.v11 import Bot as OneBotBot
    from nonebot.adapters.onebot.v11 import GroupMessageEvent

    from src.plugins.yawn_core.yawn_agent.agent import should_respond

    class Bot:
        self_id = "100"

    class Event:
        # 适配器的 _check_at_me 会把指向机器人的 @ 段移除并置 to_me,
        # event.message 里不再有 at 段。
        group_id = 1
        message: tuple[object, ...] = ()
        reply = None
        to_me = True

        def get_user_id(self) -> str:
            return "200"

        def get_plaintext(self) -> str:
            return "帮我看看这个"

    assert should_respond(
        cast("GroupMessageEvent", Event()), cast("OneBotBot", Bot()), "mention_only"
    )


def test_explicit_wakeup_does_not_match_embedded_english_syllables() -> None:
    _load_agent_modules()
    from typing import cast

    from nonebot.adapters.onebot.v11 import Bot as OneBotBot
    from nonebot.adapters.onebot.v11 import GroupMessageEvent

    from src.plugins.yawn_core.yawn_agent.agent import should_respond

    class Bot:
        self_id = "100"

    class Event:
        message: tuple[object, ...] = ()
        reply = None

        def get_user_id(self) -> str:
            return "200"

        def get_plaintext(self) -> str:
            return "this is an ordinary sentence"

    assert not should_respond(
        cast("GroupMessageEvent", Event()), cast("OneBotBot", Bot()), "explicit_wakeup"
    )


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
        "daily_limit": 12,
        "cooldown_minutes": 20,
        "idle_threshold_minutes": 30,
        "proactive_probability": 0.15,
        "proactive_active_enabled": True,
        "proactive_active_probability": 0.08,
        "proactive_active_window_minutes": 8,
        "recent_response_fingerprints": [],
    }
    values.update(overrides)
    return cast("GroupAgentConfig", SimpleNamespace(**values))


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

    # 真人消息刚发 1 分钟：话题还在进行中，抢话不像真人。
    assert (
        should_proactively_speak(make_config(), snapshot(1), now, rng=_FixedRoll(0.0))
        is None
    )
    # 真人消息 20 分钟前：话题间隙已过，也达不到暖场阈值。
    assert (
        should_proactively_speak(make_config(), snapshot(20), now, rng=_FixedRoll(0.0))
        is None
    )
    # 窗口内有真人消息但 60 分钟总量不足：群里并没有在聊。
    quiet = ActivitySnapshot(
        last_message_at=now - timedelta(minutes=3),
        last_agent_at=now - timedelta(minutes=40),
        member_messages_60m=2,
        last_member_message_at=now - timedelta(minutes=3),
    )
    assert (
        should_proactively_speak(make_config(), quiet, now, rng=_FixedRoll(0.0)) is None
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
    cooling = ActivitySnapshot(
        last_message_at=now - timedelta(minutes=40),
        last_agent_at=now - timedelta(minutes=5),
    )
    assert (
        should_proactively_speak(make_config(), cooling, now, rng=_FixedRoll(0.0))
        is None
    )
    exhausted = ActivitySnapshot(
        last_message_at=now - timedelta(minutes=40),
        last_agent_at=now - timedelta(minutes=40),
        proactive_today=12,
    )
    assert (
        should_proactively_speak(make_config(), exhausted, now, rng=_FixedRoll(0.0))
        is None
    )


def test_proactive_decision_parses_speak_true() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.proactive import _decide_proactive_reply

    decision = _decide_proactive_reply(
        '{"speak": true, "topic": "周末爬山", "reason": "话题正热", '
        '"text": " 山上现在应该挺凉的 "}'
    )
    assert decision.should_speak is True
    assert decision.text == "山上现在应该挺凉的"
    assert decision.topic == "周末爬山"
    assert decision.reason == "话题正热"


def test_proactive_decision_parses_speak_false_with_reason() -> None:
    _load_agent_modules()
    from src.plugins.yawn_core.yawn_agent.proactive import _decide_proactive_reply

    decision = _decide_proactive_reply(
        '{"speak": false, "topic": "两人争论配置", "reason": "正在争论", "text": ""}'
    )
    assert decision.should_speak is False
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

    for key in ("speak", "topic", "reason", "text"):
        assert key in _JSON_PROTOCOL
    # 插话与暖场都要把"保持沉默"作为合法结果写进指令。
    assert "speak=false" in _ACTIVE_INTERJECT_PROMPT
    assert "speak=false" in _WARMUP_PROMPT
    # 插话必须先理解内容再决策，且禁止泛泛附和。
    assert "先读懂" in _ACTIVE_INTERJECT_PROMPT
    assert "泛泛附和" in _ACTIVE_INTERJECT_PROMPT


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
    llm_module.get_client = lambda: fake_client
    try:
        empty = asyncio.run(
            llm_module.complete_with_tools(
                [{"role": "user", "content": "hi"}],
                [],
                model="fake",
                role="test_proactive",
            )
        )
        assert empty is not None
        assert "tools" not in fake_client.chat.completions.calls[0]

        tools = [{"type": "function", "function": {"name": "t"}}]
        asyncio.run(
            llm_module.complete_with_tools(
                [{"role": "user", "content": "hi"}],
                tools,
                model="fake",
                role="test_proactive",
            )
        )
        assert fake_client.chat.completions.calls[1]["tools"] == tools
    finally:
        llm_module.get_client = original
