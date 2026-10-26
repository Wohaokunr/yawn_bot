from datetime import datetime
from typing import Any, Optional

from nonebot_plugin_orm import Model
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .timeutil import beijing_now


class AgentScheduledTask(Model):
    """Agent 定时任务：到点唤醒群聊 Agent 执行一段任务说明，或直接发一句提醒。

    时间规则存在 ``schedule``（形状见 ``yawn_agent.schedule_spec``），调度器只看
    ``status='active'`` 且 ``next_run_at`` 到期的行；领取时先把 ``next_run_at``
    推进到下一次并写 ``lease_until``，所以进程在执行中途重启也不会重复触发。
    """

    __table_args__ = (
        Index("ix_agent_scheduled_task_due", "status", "next_run_at"),
        Index("ix_agent_scheduled_task_group_status", "group_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("yawn_core_botgroup.group_id", ondelete="CASCADE"),
        index=True,
    )
    # 创建时所在的机器人；执行时优先用它，不在线时换群里最近活跃的那个。
    bot_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    creator_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    creator_name: Mapped[str] = mapped_column(String(64), default="")
    title: Mapped[str] = mapped_column(String(40), default="")
    # 到点时交给 Agent 的任务说明（agent 模式），或原样发出的提醒正文（notify 模式）。
    instruction: Mapped[str] = mapped_column(String(600), default="")
    # agent：唤醒完整对话流程（可用工具）；notify：不调模型，直接发提醒。
    mode: Mapped[str] = mapped_column(String(16), default="agent")
    schedule: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    schedule_kind: Mapped[str] = mapped_column(String(16), default="once")
    description: Mapped[str] = mapped_column(String(200), default="")
    # active / paused / completed / cancelled / failed
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    status_reason: Mapped[str] = mapped_column(String(120), default="")
    mention_creator: Mapped[bool] = mapped_column(Boolean, default=True)
    max_runs: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    run_count: Mapped[int] = mapped_column(Integer, default=0)
    # 连续失败次数；成功一次清零，达到策略上限自动暂停。
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    skip_count: Mapped[int] = mapped_column(Integer, default=0)
    next_run_at: Mapped[Optional[datetime]] = mapped_column(nullable=True, index=True)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    last_result: Mapped[str] = mapped_column(String(24), default="")
    last_error: Mapped[str] = mapped_column(String(240), default="")
    last_output: Mapped[str] = mapped_column(String(300), default="")
    last_trace_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # 最近若干次运行的摘要（时间、结果、耗时、发言预览），新的在前。
    history: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # agent：成员在群里让 Bot 设的；manual：管理员在 WebUI 建的。
    source_kind: Mapped[str] = mapped_column(String(16), default="agent")
    # 执行租约：领取后到这个时间之前其他 tick 不会再领取同一行。
    lease_until: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    # 乐观锁：WebUI 编辑时带上，避免覆盖调度器刚写入的运行结果。
    revision: Mapped[int] = mapped_column(Integer, default=0)
    cancelled_by: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        default=beijing_now, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        default=beijing_now,
        server_default=func.current_timestamp(),
        onupdate=beijing_now,
    )
