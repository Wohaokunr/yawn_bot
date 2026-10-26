# ruff: noqa: E501
"""定时任务唤醒的回合标记（contextvar）。

调度器把一次 agent 模式的定时任务交给普通对话流程执行。对话流程本身不知道
“这一轮是定时任务”，几个地方据这里的标记收窄行为：

- ``context_loader``：把任务事实（谁、什么时候设的、第几次、计划时间、上次
  说了什么）放进实时状态层 ``scheduled_wake``；
- ``prompt``：追加 ``core.scheduled_wake`` 分段，告诉模型这是到点执行；
- ``tools``：特权/高风险工具一律不暴露、不执行，也不能在唤醒里再建新的定时
  任务（避免任务自我繁殖）——没有人在场确认的回合不能做群管理。

标记只在调度器的 asyncio 任务里设置，不会泄漏到其他群的回合。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime

WAKE_TRIGGER_SOURCE = "scheduled_task"
# 唤醒回合里禁止的工具：再建任务会让一个任务无限繁殖。
WAKE_BLOCKED_TOOLS = frozenset({"schedule_task"})


@dataclass(frozen=True, slots=True)
class ScheduledWake:
    group_id: int
    task_id: int
    title: str
    creator_user_id: int
    creator_name: str
    planned_at: datetime
    run_number: int
    max_runs: int | None
    schedule: str
    created_at: datetime | None
    late_minutes: int = 0
    previous_output: str = ""
    mention_creator: bool = True

    def prompt_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task_id": self.task_id,
            "title": self.title,
            "set_by": self.creator_name or str(self.creator_user_id),
            "set_by_user_id": self.creator_user_id,
            "planned_at": self.planned_at.strftime("%Y-%m-%d %H:%M"),
            "schedule": self.schedule,
            "run": self.run_number if self.max_runs is None else f"{self.run_number}/{self.max_runs}",
        }
        if self.created_at is not None:
            payload["created_at"] = self.created_at.strftime("%Y-%m-%d %H:%M")
        if self.late_minutes > 0:
            payload["late_minutes"] = self.late_minutes
        if self.previous_output:
            payload["previous_output"] = self.previous_output
        if self.mention_creator:
            payload["remind_set_by"] = True
        return payload


_CURRENT: ContextVar[ScheduledWake | None] = ContextVar("agent_scheduled_wake", default=None)


def current_wake() -> ScheduledWake | None:
    return _CURRENT.get()


def in_scheduled_wake() -> bool:
    return _CURRENT.get() is not None


@contextmanager
def scheduled_wake(wake: ScheduledWake) -> Iterator[ScheduledWake]:
    token = _CURRENT.set(wake)
    try:
        yield wake
    finally:
        _CURRENT.reset(token)


__all__ = [
    "WAKE_BLOCKED_TOOLS",
    "WAKE_TRIGGER_SOURCE",
    "ScheduledWake",
    "current_wake",
    "in_scheduled_wake",
    "scheduled_wake",
]
