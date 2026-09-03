# ruff: noqa: E501,I001,PLR2004
from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import nonebot
import pytest

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_modules() -> tuple[Any, Any]:
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
    from src.plugins.yawn_core.yawn_agent import context_history, media_context

    return context_history, media_context


def _image_message(message_id: int, user_id: int, minutes_ago: int = 0) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "user_id": user_id,
        "text": "[图片]",
        "media_types": ["image"],
        "minutes_ago": minutes_ago,
    }


def _text_message(
    message_id: int, user_id: int, text: str, minutes_ago: int = 0
) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "user_id": user_id,
        "text": text,
        "minutes_ago": minutes_ago,
    }


def test_effective_turn_reconstructs_image_question_then_at_trigger() -> None:
    context_history, _media_context = _load_modules()
    messages = [
        _image_message(101, 20001),
        _text_message(102, 20001, "这张图片怎么样"),
    ]

    effective = context_history.effective_turn_query(
        messages,
        focus_user_ids=[20001],
        query_text="[非文本消息]",
    )

    assert effective.trigger_only is True
    assert effective.used_history is True
    assert effective.text == "这张图片怎么样"
    assert effective.message_ids == (101, 102)
    assert effective.media_message_ids == (101,)
    assert effective.media_requested is True


def test_effective_turn_does_not_cross_another_speaker() -> None:
    context_history, _media_context = _load_modules()
    messages = [
        _image_message(101, 20001),
        _text_message(102, 20001, "这张图片怎么样"),
        _text_message(103, 30001, "我插一句"),
    ]

    effective = context_history.effective_turn_query(
        messages,
        focus_user_ids=[20001],
        query_text="",
    )

    assert effective.used_history is False
    assert effective.media_requested is False
    assert effective.message_ids == ()


def test_effective_turn_does_not_recover_stale_split_turn() -> None:
    context_history, _media_context = _load_modules()
    messages = [
        _image_message(101, 20001, minutes_ago=5),
        _text_message(102, 20001, "这张图片怎么样", minutes_ago=5),
    ]

    effective = context_history.effective_turn_query(
        messages,
        focus_user_ids=[20001],
        query_text="",
    )

    assert effective.used_history is False
    assert effective.media_requested is False


def test_context_selection_marks_effective_turn_media() -> None:
    context_history, _media_context = _load_modules()
    selection = context_history.select_context_messages(
        [
            _text_message(99, 30001, "别人之前说的话", minutes_ago=1),
            _image_message(101, 20001),
            _text_message(102, 20001, "这张图片怎么样"),
        ],
        focus_user_ids=[20001],
        query_text="[非文本消息]",
    )

    assert selection.effective_query == "这张图片怎么样"
    assert selection.media_message_ids == (101,)
    media_trace = next(item for item in selection.trace if item["message_id"] == 101)
    assert media_trace["selected"] is True
    assert media_trace["reason"] == "effective_turn_media"


def test_direct_media_query_keeps_existing_history_behavior() -> None:
    context_history, _media_context = _load_modules()
    selection = context_history.select_context_messages(
        [
            _image_message(101, 30001, minutes_ago=1),
            _text_message(102, 20001, "普通聊天", minutes_ago=0),
        ],
        focus_user_ids=[20001],
        query_text="刚才那张图还有什么细节？",
    )

    media_trace = next(item for item in selection.trace if item["message_id"] == 101)
    assert media_trace["selected"] is True
    assert media_trace["reason"] == "media_reference"


def test_prompt_promotes_split_question_into_current_turn() -> None:
    _context_history, _media_context = _load_modules()
    from src.plugins.yawn_core.yawn_agent import prompt

    context = {
        "messages": [
            _image_message(101, 20001),
            _text_message(102, 20001, "这张图片怎么样"),
        ]
    }
    turn = {
        "message_id": 103,
        "user_id": 20001,
        "name": "用户",
        "role": "member",
        "title": None,
        "content": "[非文本消息]",
        "mentions": (),
        "reply_to": None,
        "trigger": "at",
        "received_at": None,
        "media_types": (),
        "media": (),
        "forward_nodes": 0,
        "truncated": False,
    }

    rebuilt = prompt.reconstruct_effective_current_turn(turn, context)

    assert rebuilt is not None
    assert rebuilt["content"] == "这张图片怎么样"


def test_prompt_preserves_media_fallback_and_prepends_split_question() -> None:
    _context_history, _media_context = _load_modules()
    from src.plugins.yawn_core.yawn_agent import prompt

    context = {
        "messages": [
            _image_message(101, 20001),
            _text_message(102, 20001, "这张图片怎么样"),
        ]
    }
    turn = {
        "message_id": 103,
        "user_id": 20001,
        "name": "用户",
        "role": "member",
        "title": None,
        "content": "[图片转述] 一张黑白插画",
        "mentions": (),
        "reply_to": None,
        "trigger": "at",
        "received_at": None,
        "media_types": (),
        "media": (),
        "forward_nodes": 0,
        "truncated": False,
    }

    rebuilt = prompt.reconstruct_effective_current_turn(turn, context)

    assert rebuilt is not None
    assert rebuilt["content"] == "这张图片怎么样\n[图片转述] 一张黑白插画"


@pytest.mark.asyncio
async def test_media_resolver_prefers_effective_turn_image_for_at_only_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _context_history, media_context = _load_modules()
    loaded_ids: list[int] = []
    prepared_refs: list[dict[str, Any]] = []

    async def fake_load_rows(
        _session: Any,
        *,
        group_id: int,
        bot_id: int | None,
        message_ids: list[int],
    ) -> dict[int, Any]:
        del group_id, bot_id
        loaded_ids.extend(message_ids)
        return {
            101: SimpleNamespace(
                media_refs=[
                    {
                        "type": "image",
                        "asset_id": 77,
                        "content_hash": "a" * 64,
                    }
                ]
            )
        }

    async def fake_prepare(
        _bot: Any,
        _group_id: int,
        refs: list[dict[str, Any]],
        **_kwargs: Any,
    ) -> tuple[list[Any], list[Any], list[Any]]:
        prepared_refs.extend(dict(item) for item in refs)
        return (
            [
                SimpleNamespace(
                    content_hash="a" * 64,
                    asset_id=77,
                    source="history",
                    source_message_id=101,
                )
            ],
            [],
            [],
        )

    monkeypatch.setattr(media_context, "_load_message_rows", fake_load_rows)
    monkeypatch.setattr(media_context, "prepare_media_inputs", fake_prepare)

    result = await media_context.resolve_media_context(
        SimpleNamespace(self_id=50001),
        None,
        60001,
        selected_history=[
            _image_message(101, 20001),
            _text_message(102, 20001, "这张图片怎么样"),
        ],
        query_text="[非文本消息]",
    )

    assert loaded_ids == [101]
    assert len(result) == 1
    assert prepared_refs[0]["source"] == "history"
    assert prepared_refs[0]["source_message_id"] == 101


def test_mention_only_semantic_query_is_not_nontext_placeholder() -> None:
    _context_history, _media_context = _load_modules()
    from src.plugins.yawn_core.yawn_agent.message_parser import NormalizedMessage

    normalized = NormalizedMessage(plain_text="", segments=[])
    normalized.trigger_source = "mention"
    normalized.trigger_signals = {"mention": True}

    assert normalized.semantic_query_text() == "[用户仅@机器人，没有附加正文]"
    assert normalized.semantic_query_text() != "[非文本消息]"


def test_real_media_keeps_nontext_media_semantics_even_when_mentioned() -> None:
    _context_history, _media_context = _load_modules()
    from src.plugins.yawn_core.yawn_agent.message_parser import NormalizedMessage

    normalized = NormalizedMessage(
        plain_text="",
        segments=[],
        media_refs=[{"type": "image", "asset_id": 77}],
    )
    normalized.trigger_source = "mention"
    normalized.trigger_signals = {"mention": True}

    assert normalized.semantic_query_text() != "[用户仅@机器人，没有附加正文]"
    assert "媒体" in normalized.semantic_query_text()


def test_bare_mention_continues_across_recent_bot_reply() -> None:
    context_history, _media_context = _load_modules()
    messages = [
        _text_message(201, 20001, "你AI味太强了", minutes_ago=3),
        _text_message(202, 20001, "怎么不说话了", minutes_ago=2),
        {
            "message_id": 203,
            "user_id": 50001,
            "role": "bot",
            "text": "上一条机器人回复",
            "minutes_ago": 1,
        },
    ]

    effective = context_history.effective_turn_query(
        messages,
        focus_user_ids=[20001],
        query_text="[用户仅@机器人，没有附加正文]",
    )

    assert effective.trigger_only is True
    assert effective.used_history is True
    assert effective.text == "你AI味太强了\n怎么不说话了"
    assert effective.message_ids == (201, 202)


def test_bare_mention_never_crosses_another_human() -> None:
    context_history, _media_context = _load_modules()
    messages = [
        _text_message(201, 20001, "继续刚才的话题", minutes_ago=3),
        _text_message(202, 30001, "别人插话", minutes_ago=1),
    ]

    effective = context_history.effective_turn_query(
        messages,
        focus_user_ids=[20001],
        query_text="[用户仅@机器人，没有附加正文]",
    )

    assert effective.used_history is False
    assert effective.message_ids == ()


def test_trace_shape_recovers_adapter_consumed_bot_mention() -> None:
    _context_history, _media_context = _load_modules()
    from src.plugins.yawn_core.yawn_agent import dialogue_turn_support
    from src.plugins.yawn_core.yawn_agent.message_parser import (
        NormalizedMessage,
        SegmentNode,
    )

    normalized = NormalizedMessage(
        plain_text="",
        segments=[SegmentNode("text", {"text": ""}, "")],
    )
    normalized.trigger_source = "mention"
    normalized.trigger_signals = {"mention": True, "reply": False, "wake_word": False}

    shape = dialogue_turn_support.trace_message_shape(normalized, bot_id=50001)

    assert shape["mention_bot"] is True
    assert shape["original_segment_types"] == []
    assert shape["observed_segment_types"] == []
    assert shape["effective_segment_types"] == ["at"]
    assert shape["observed_mentions"] == []
    assert shape["effective_mentions"] == [50001]
    assert shape["mention_stripped_for_prompt"] is True
    assert shape["mention_recovered_from_trigger"] is True


def test_trace_shape_keeps_real_at_without_marking_recovery() -> None:
    _context_history, _media_context = _load_modules()
    from src.plugins.yawn_core.yawn_agent import dialogue_turn_support
    from src.plugins.yawn_core.yawn_agent.message_parser import (
        NormalizedMessage,
        SegmentNode,
    )

    normalized = NormalizedMessage(
        plain_text="@50001",
        segments=[SegmentNode("at", {"qq": "50001"}, "@50001")],
        mentions=[50001],
    )
    normalized.trigger_source = "mention"
    normalized.trigger_signals = {"mention": True, "reply": False, "wake_word": False}

    shape = dialogue_turn_support.trace_message_shape(normalized, bot_id=50001)

    assert shape["observed_segment_types"] == ["at"]
    assert shape["effective_segment_types"] == ["at"]
    assert shape["observed_mentions"] == [50001]
    assert shape["effective_mentions"] == [50001]
    assert shape["mention_stripped_for_prompt"] is False
    assert shape["mention_recovered_from_trigger"] is False


def test_trace_shape_leaves_plain_text_message_unchanged() -> None:
    _context_history, _media_context = _load_modules()
    from src.plugins.yawn_core.yawn_agent import dialogue
    from src.plugins.yawn_core.yawn_agent.message_parser import (
        NormalizedMessage,
        SegmentNode,
    )

    normalized = NormalizedMessage(
        plain_text="你好",
        segments=[SegmentNode("text", {"text": "你好"}, "你好")],
    )
    normalized.trigger_source = "wake_word"
    normalized.trigger_signals = {"mention": False, "reply": False, "wake_word": True}

    shape = dialogue._trace_message_shape(normalized, bot_id=50001)

    assert shape["observed_segment_types"] == ["text"]
    assert shape["effective_segment_types"] == ["text"]
    assert shape["effective_mentions"] == []
    assert shape["mention_stripped_for_prompt"] is False
    assert shape["mention_recovered_from_trigger"] is False
