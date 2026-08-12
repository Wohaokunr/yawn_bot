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


def test_interactive_schedule_formats(reminder_module: Any) -> None:
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc).replace(
        tzinfo=None
    )
    every_day = reminder_module._parse_schedule("每天 07:00", now=now)
    assert every_day.schedule_type == "recurring"
    assert every_day.cron_expression == "0 7 * * *"

    once = reminder_module._parse_schedule(
        "一次 2026-08-20 15:30",
        now=now,
    )
    assert once.schedule_type == "once"
    assert once.run_at == datetime(
        2026,
        8,
        20,
        15,
        30,
        tzinfo=timezone.utc,
    ).replace(tzinfo=None)


def test_interactive_action_parser_only_accepts_menu_actions(
    reminder_module: Any,
) -> None:
    assert reminder_module._parse_action("编辑 12") == ("edit", 12)
    assert reminder_module._parse_action("删除 12") == ("delete", 12)
    assert reminder_module._parse_action("delete 12") == (None, 12)
    assert reminder_module._parse_action("编辑") == (None, None)


def test_scheduler_job_uses_stable_id_and_apscheduler_options(
    reminder_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def add_job(*args: Any, **kwargs: Any) -> None:
        captured["args"] = args
        captured.update(kwargs)

    monkeypatch.setattr(reminder_module.scheduler, "add_job", add_job)
    reminder_module._schedule_reminder_job(
        42,
        reminder_module.ScheduleSpec(
            schedule_type="recurring",
            cron_expression="0 7 * * *",
        ),
    )

    assert captured["id"] == "yawn_core_reminder:42"
    assert captured["args"] == [42]
    assert captured["coalesce"] is True
    assert captured["max_instances"] == 1
    assert captured["misfire_grace_time"] == reminder_module._MISFIRE_GRACE_TIME
    assert captured["trigger"].timezone.key == "Asia/Shanghai"


@pytest.mark.asyncio
async def test_create_reminder_rolls_back_when_job_registration_fails(
    reminder_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSession:
        def __init__(self) -> None:
            self.added: Any = None
            self.deleted: Any = None
            self.commits = 0

        async def scalar(self, _statement: Any) -> int:
            return 0

        def add(self, value: Any) -> None:
            self.added = value

        async def flush(self) -> None:
            self.added.id = 123

        async def commit(self) -> None:
            self.commits += 1

        async def refresh(self, _value: Any) -> None:
            return

        async def delete(self, value: Any) -> None:
            self.deleted = value

    session = FakeSession()
    removed: list[int] = []

    def fail_schedule(_reminder_id: int, _schedule: Any) -> None:
        raise RuntimeError

    def record_removal(reminder_id: int) -> None:
        removed.append(reminder_id)

    monkeypatch.setattr(reminder_module, "_schedule_reminder_job", fail_schedule)
    monkeypatch.setattr(
        reminder_module,
        "_remove_reminder_job",
        record_removal,
    )

    with pytest.raises(ValueError, match="调度失败"):
        await reminder_module._create_reminder(
            session,
            group_id=456,
            creator_user_id=789,
            name="test",
            schedule=reminder_module.ScheduleSpec(
                schedule_type="recurring",
                cron_expression="0 7 * * *",
            ),
            target_type="group",
            target_user_id=None,
            message=Message("hello"),
        )

    assert removed == [123]
    assert session.deleted is session.added
    expected_commits = 2
    assert session.commits == expected_commits
