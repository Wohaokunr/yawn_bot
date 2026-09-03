"""Agent Tool 的回合级执行上下文与内部结果元数据。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

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


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    """工具执行后的模型 payload 与事务/收尾元数据。"""

    payload: dict[str, Any]
    mutated_db: bool = False
    needs_commit: bool = False
    immediate_commit: bool = False
    ends_turn: bool = False


__all__ = ["ToolExecutionContext", "ToolExecutionResult", "ToolHandlerResult"]
