"""定时提醒的纯逻辑回归测试。"""

from __future__ import annotations

import importlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import nonebot
import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def reminder_module() -> Any:
    """通过 NoneBot 正式插件发现流程加载提醒模块。"""
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    nonebot.init()
    nonebot.load_from_toml("pyproject.toml")
    return importlib.import_module("src.plugins.yawn_core.reminder")


def test_cron_and_target_validation(reminder_module: Any) -> None:
    assert reminder_module._normalize_cron(" 0   7 * * * ") == "0 7 * * *"
    with pytest.raises(ValueError):
        reminder_module._normalize_cron("0 7 * *")

    assert reminder_module._parse_target("群聊") == ("group", None)
    assert reminder_module._parse_target("私聊 123456") == (
        "private",
        123456,
    )
    with pytest.raises(ValueError):
        reminder_module._parse_target("私聊 0")


def test_message_payload_and_temporary_segments(
    reminder_module: Any,
) -> None:
    source = Message(
        [
            MessageSegment.text("提醒 "),
            MessageSegment.at(123456),
            MessageSegment.image("https://example.test/image.png"),
        ]
    )
    payload = reminder_module._message_to_payload(source)
    assert [item["type"] for item in payload] == [
        "text",
        "at",
        "image",
    ]

    restored = reminder_module._payload_to_message(
        payload,
        datetime(2026, 8, 12, tzinfo=timezone.utc),
    )
    assert restored[0].data["text"] == "提醒 "
    assert restored[1].data["qq"] == "123456"

    with pytest.raises(ValueError, match="临时消息段"):
        reminder_module._message_to_payload(
            Message(MessageSegment.reply(100))
        )

    with pytest.raises(ValueError, match="不能为空"):
        reminder_module._message_to_payload(Message("   "))


def test_countdown_placeholder_and_invalid_date(
    reminder_module: Any,
) -> None:
    message = Message("距离高考还有{{倒计时:2027-06-07}}天")
    payload = reminder_module._message_to_payload(message)
    rendered = reminder_module._payload_to_message(
        payload,
        datetime(2027, 6, 6, tzinfo=timezone.utc),
    )
    assert rendered[0].data["text"] == "距离高考还有1天"

    with pytest.raises(ValueError, match="日期无效"):
        reminder_module._message_to_payload(
            Message("{{倒计时:不是日期}}")
        )


def test_single_line_fields_keep_message_segments(
    reminder_module: Any,
) -> None:
    source = Message("高考 | 0 7 * * * | 群聊 | 倒计时：")
    source += MessageSegment.at(123456)
    source += MessageSegment.text(" {{倒计时:2027-06-07}}")

    name, cron, target, message = reminder_module._parse_add_fields(
        source
    )
    assert name == "高考"
    assert cron == "0 7 * * *"
    assert target == "群聊"
    assert message[0].is_text()
    assert message[1].type == "at"
    assert message[2].data["text"].strip() == "{{倒计时:2027-06-07}}"


def test_scheduler_job_uses_stable_id_and_apscheduler_options(
    reminder_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def add_job(*args: Any, **kwargs: Any) -> None:
        captured["args"] = args
        captured.update(kwargs)

    monkeypatch.setattr(reminder_module.scheduler, "add_job", add_job)
    reminder_module._schedule_reminder_job(42, "0 7 * * *")

    assert captured["id"] == "yawn_core_reminder:42"
    assert captured["args"] == [42]
    assert captured["coalesce"] is True
    assert captured["max_instances"] == 1
    assert captured["misfire_grace_time"] == reminder_module._MISFIRE_GRACE_TIME
    assert captured["trigger"].timezone.key == "Asia/Shanghai"
