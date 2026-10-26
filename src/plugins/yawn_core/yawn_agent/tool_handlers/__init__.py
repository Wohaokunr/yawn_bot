# ruff: noqa: TID252, TRY003
"""Agent Tool family handler dispatch。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from ..tool_execution import ToolExecutionContext, ToolHandlerResult
from . import (
    admin,
    files,
    history,
    inbox,
    member,
    memory,
    message,
    notice,
    sandbox,
    schedule,
    web,
)

Handler = Callable[[str, dict, ToolExecutionContext], Awaitable[ToolHandlerResult]]

_HANDLERS: dict[str, Handler] = {}
_MODULES = (
    history,
    member,
    memory,
    message,
    notice,
    files,
    admin,
    sandbox,
    inbox,
    web,
    schedule,
)
for module in _MODULES:
    for tool_name in module.NAMES:
        _HANDLERS[tool_name] = module.handle


async def dispatch_tool(
    name: str, args: dict, context: ToolExecutionContext
) -> ToolHandlerResult:
    handler = _HANDLERS.get(name)
    if handler is None:
        raise ValueError(f"未知工具: {name}")
    return await handler(name, args, context)


HANDLED_TOOL_NAMES = frozenset(_HANDLERS)
__all__ = ["HANDLED_TOOL_NAMES", "dispatch_tool"]
