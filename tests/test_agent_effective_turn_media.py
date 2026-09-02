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
        query_text="",
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
        query_text="",
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
        query_text="",
    )

    assert loaded_ids == [101]
    assert len(result) == 1
    assert prepared_refs[0]["source"] == "history"
    assert prepared_refs[0]["source_message_id"] == 101
