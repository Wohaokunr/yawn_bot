# ruff: noqa: TC002
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
    assert first[1] != second[1]


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
