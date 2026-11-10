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
# 唤醒回合里禁止的工具：再建任务会让一个任务无限繁殖；偷图要有人在场时才偷。
WAKE_BLOCKED_TOOLS = frozenset({"schedule_task", "steal_sticker"})
SOURCE_LABELS = {
    "agent": "群里让我设的",
    "private": "他在私聊里让我给本群设的",
    "manual": "管理员在网页上设的",
    "webui_user": "他在网页上给本群设的",
}


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
    # 在哪设的：agent（群里让我设的）/ private（他在私聊里让我给本群设的）/ manual（网页）。
    source_kind: str = "agent"
    # 到点还要 @ 的其他成员（本轮 at 段会对他们开放）。
    mention_user_ids: tuple[int, ...] = ()
    # 任务要求 @全体成员：调度器已经代发了一句带 @全体 的开场（模型自己不能 @全体）。
    mention_all_announced: bool = False

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
        if self.mention_user_ids:
            # 这些人要在你的回复里用 at 段逐个 @ 到。
            payload["remind_members"] = list(self.mention_user_ids)
        if self.mention_all_announced:
            payload["mention_all_sent"] = "我已经先发了一句 @全体成员 的开场，你接着说正事，不要再 @全体"
        if self.source_kind in SOURCE_LABELS and self.source_kind != "agent":
            # 只说“在哪设的”，不带任何私聊内容（私聊内容永远不进群聊提示词）。
            payload["set_via"] = SOURCE_LABELS[self.source_kind]
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
    "SOURCE_LABELS",
    "WAKE_BLOCKED_TOOLS",
    "WAKE_TRIGGER_SOURCE",
    "ScheduledWake",
    "current_wake",
    "in_scheduled_wake",
    "scheduled_wake",
]
