from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import nonebot

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_prompt() -> Any:
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
    from src.plugins.yawn_core.yawn_agent import prompt

    return prompt


def _turn(content: str = "[非文本消息]") -> dict[str, Any]:
    return {
        "message_id": 103,
        "user_id": 20001,
        "name": "群主",
        "role": "member",
        "title": None,
        "content": content,
        "mentions": (),
        "reply_to": None,
        "trigger": "at",
        "received_at": None,
        "media_types": (),
        "media": (),
        "forward_nodes": 0,
        "truncated": False,
    }


def _history() -> list[dict[str, Any]]:
    return [
        {
            "message_id": 90,
            "user_id": 50001,
            "role": "bot",
            "text": "我这边看不到图片内容，没法评价呀",
            "minutes_ago": 1,
        },
        {
            "message_id": 101,
            "user_id": 20001,
            "text": "[图片]",
            "media_types": ["image"],
            "minutes_ago": 0,
        },
        {
            "message_id": 102,
            "user_id": 20001,
            "text": "这张图片怎么样",
            "minutes_ago": 0,
        },
    ]


def test_current_media_overrides_stale_bot_cannot_see_claim() -> None:
    prompt = _load_prompt()
    messages, _fingerprint = prompt.build_messages(
        persona={},
        tools=[],
        context={"messages": _history()},
        user_prompt="[非文本消息]",
        current_turn=_turn(),
        media_inputs=[
            {
                "type": "text",
                "text": "以下是与当前问题相关的历史图片。",
            },
            {"type": "file", "file_id": "file-api-test"},
        ],
    )

    # Keep the static cache prefix unchanged; media authority is a volatile per-turn fact.
    assert prompt.PROMPT_VERSION == "yawn-agent-v14"
    realtime = next(
        str(item["content"])
        for item in messages
        if item["role"] == "system" and "本轮媒体状态" in str(item["content"])
    )
    assert "必须直接检查这些媒体" in realtime
    assert "旧回复只描述过去失败" in realtime
    assert "只有当前媒体内容块本身无法解码" in realtime

    user = messages[-1]
    assert user["role"] == "user"
    assert isinstance(user["content"], list)
    assert "这张图片怎么样" in str(user["content"][0]["text"])
    assert {"type": "file", "file_id": "file-api-test"} in user["content"]


def test_text_only_media_status_does_not_claim_visual_media_is_attached() -> None:
    prompt = _load_prompt()
    messages, _fingerprint = prompt.build_messages(
        persona={},
        tools=[],
        context={},
        user_prompt="普通问题",
        current_turn=_turn("普通问题"),
        media_inputs=[
            {
                "type": "text",
                "text": "[media_context status=unavailable] 图片文件无法读取",
            }
        ],
    )

    assert not any(
        item["role"] == "system" and "本轮媒体状态" in str(item["content"])
        for item in messages
    )
