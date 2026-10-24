"""Agent Tool 的回合级执行上下文与内部结果元数据。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from .tool_args import ToolCallLedger

if TYPE_CHECKING:
    from .capabilities import BotGroupCapabilities


@dataclass(slots=True)
class ToolExecutionContext:
    """同一 Agent 回合内可复用的权限/能力快照。

    普通工具复用 ``capabilities`` 与 ``actor_can_manage``；privileged/critical
    仍由执行器在真正副作用前刷新这两个安全事实。
    """

    bot: Any
    group_id: int
    actor_user_id: int | None
    session: Any
    capabilities: BotGroupCapabilities
    actor_can_manage: bool | None = None
    privileged_allowlist: frozenset[str] | None = None
    # 本轮触发消息中调用者本人写的正文；critical 工具据此校验显式动作意图。
    # 主动/后台任务保持 None，因此永远无法执行 critical 工具。
    current_intent_text: str | None = None
    # 本回合写操作去重/次数账本；with_capabilities() 复制上下文时共享同一个账本。
    ledger: ToolCallLedger = field(default_factory=ToolCallLedger)
    # 本轮触发消息里的附件（``sandbox_inbox.Attachment``，含原始媒体引用，只在
    # 进程内）；入箱工具与 run_python 首次运行据此把附件放进工作区。
    attachments: tuple[Any, ...] = ()
    # 附件 id → 已入箱的工作区相对路径；与 ledger 一样在复制上下文时共享。
    inbox: dict[str, str] = field(default_factory=dict)
    inbox_attempted: bool = False

    def with_capabilities(
        self, capabilities: BotGroupCapabilities
    ) -> ToolExecutionContext:
        return replace(self, capabilities=capabilities)


@dataclass(frozen=True, slots=True)
class ToolHandlerResult:
    """handler 对执行器返回的内部元数据，不直接暴露给模型。"""

    result: Any
    mutated_db: bool = False
    needs_commit: bool = False
    immediate_commit: bool = False
    ends_turn: bool = False
    # 副作用其实没有发生（例如同名文件夹已存在），执行器据此退还预扣配额。
    refund_quota: bool = False


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    """工具执行后的模型 payload 与事务/收尾元数据。"""

    payload: dict[str, Any]
    mutated_db: bool = False
    needs_commit: bool = False
    immediate_commit: bool = False
    ends_turn: bool = False


__all__ = ["ToolExecutionContext", "ToolExecutionResult", "ToolHandlerResult"]
