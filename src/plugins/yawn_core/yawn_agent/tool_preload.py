# ruff: noqa: PLR0913
"""把工具路由的结论落成本轮第一轮就能用的工具 schema。

路由模型（``tool_planner``）只说“这条消息大概要用沙箱/历史/群文件”，真正
装载什么仍走 ``discover_tools`` 同一套权限检查：发起人权限、Bot 角色、白名单、
沙箱开放范围。路由说要、权限不给的工具照样不会出现；critical 工具永远不在
预装范围内。这样常见请求省掉“先发现、再调用”的一整轮，又不绕过任何校验。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from .capabilities import BotGroupCapabilities, probe_group_capabilities
from .log import dbg, dbg_exc
from .tool_planner import FALLBACK_PLAN, ToolPlan, plan_tools
from .tool_router import discovery_segment_types

# 第一轮最多直接装两包；路由给的第三包只作为 suggested_toolpacks 提示，
# 需要时模型再 discover_tools 加载，免得首轮 schema 过大。
PRELOAD_PACK_LIMIT = 2


@dataclass(slots=True)
class PreloadResult:
    names: frozenset[str] = frozenset()
    segments: frozenset[str] = frozenset()
    requires_admin: bool = False
    packs: tuple[str, ...] = ()
    empty_packs: tuple[str, ...] = ()
    suggested: tuple[str, ...] = ()
    duration_ms: float = 0.0
    errors: list[str] = field(default_factory=list)

    def trace_dict(self) -> dict[str, Any]:
        return {
            "preloaded_tools": sorted(self.names),
            "preloaded_packs": list(self.packs),
            "unavailable_packs": list(self.empty_packs),
            "suggested_packs": list(self.suggested),
            "segments": sorted(self.segments),
            "requires_admin": self.requires_admin,
            "errors": self.errors,
        }


async def preload_planned_tools(
    plan: ToolPlan,
    *,
    bot: Any,
    group_id: int,
    actor_user_id: int | None,
    session: Any,
    capabilities: BotGroupCapabilities,
    actor_can_manage: bool,
    privileged_allowlist: frozenset[str] | set[str],
) -> PreloadResult:
    """按路由结论逐包走发现权限检查，返回可直接装载的工具名与消息段。"""

    started = time.monotonic()
    if not plan.planned or not plan.toolpacks:
        return PreloadResult(segments=frozenset(plan.segments))
    from .tools import _discover_tools_for_actor

    names: set[str] = set()
    packs: list[str] = []
    empty: list[str] = []
    errors: list[str] = []
    requires_admin = False
    for pack in plan.toolpacks[:PRELOAD_PACK_LIMIT]:
        try:
            result = await _discover_tools_for_actor(
                query="",
                family=pack,
                limit=8,
                bot=bot,
                group_id=group_id,
                actor_user_id=actor_user_id,
                session=session,
                capabilities=capabilities,
                actor_can_manage=actor_can_manage,
                privileged_allowlist=set(privileged_allowlist),
            )
        except Exception as exc:  # noqa: BLE001
            dbg_exc(f"群 {group_id} 预装工具包 {pack} 失败(忽略)")
            errors.append(f"{pack}:{type(exc).__name__}")
            continue
        rows = [item for item in result.get("tools") or [] if isinstance(item, dict)]
        pack_names = {str(item["name"]) for item in rows if item.get("name")}
        if not pack_names:
            empty.append(pack)
            continue
        packs.append(pack)
        names |= pack_names
        requires_admin = requires_admin or any(
            str(item.get("permission") or "") in {"privileged", "critical"}
            for item in rows
        )
    return PreloadResult(
        names=frozenset(names),
        segments=frozenset(plan.segments) | discovery_segment_types(names),
        requires_admin=requires_admin,
        packs=tuple(packs),
        empty_packs=tuple(empty),
        suggested=tuple(plan.toolpacks[PRELOAD_PACK_LIMIT:]),
        duration_ms=(time.monotonic() - started) * 1000,
        errors=errors,
    )


def start_tool_plan(
    text: str, *, has_reply: bool, has_media: bool, mentions_others: bool
) -> asyncio.Task[ToolPlan]:
    """回合一开始就并发跑路由，和上下文加载重叠，通常不增加延迟。"""

    return asyncio.create_task(
        plan_tools(
            text,
            signals=plan_signals(
                has_reply=has_reply,
                has_media=has_media,
                mentions_others=mentions_others,
            ),
        )
    )


async def resolve_tool_plan(
    plan_task: asyncio.Task[ToolPlan],
    *,
    bot: Any,
    group_id: int,
    actor_user_id: int | None,
    session: Any,
    capabilities: BotGroupCapabilities,
    actor_can_manage: bool,
    privileged_allowlist: frozenset[str] | set[str],
) -> tuple[ToolPlan, PreloadResult, BotGroupCapabilities]:
    """等路由结果并预装；需要管理能力时重新探测 Bot 能力，让下一步能装载 schema。"""

    try:
        plan = await plan_task
    except Exception:  # noqa: BLE001
        dbg_exc(f"群 {group_id} 工具路由任务异常(退回渐进披露)")
        plan = FALLBACK_PLAN
    preload = await preload_planned_tools(
        plan,
        bot=bot,
        group_id=group_id,
        actor_user_id=actor_user_id,
        session=session,
        capabilities=capabilities,
        actor_can_manage=actor_can_manage,
        privileged_allowlist=privileged_allowlist,
    )
    if preload.requires_admin and not capabilities.can_manage:
        capabilities = await probe_group_capabilities(bot, group_id)
    if preload.names:
        dbg(
            f"群 {group_id} 工具路由预装: packs={list(preload.packs)} "
            f"tools={sorted(preload.names)} ({plan.source}, {plan.duration_ms:.0f}ms)"
        )
    return plan, preload, capabilities


def plan_signals(
    *, has_reply: bool, has_media: bool, mentions_others: bool
) -> dict[str, bool]:
    """给路由模型的结构信号：只有布尔值，不含任何内容。"""

    return {"reply": has_reply, "media": has_media, "mentions_others": mentions_others}


__all__ = [
    "PRELOAD_PACK_LIMIT",
    "PreloadResult",
    "plan_signals",
    "preload_planned_tools",
    "resolve_tool_plan",
    "start_tool_plan",
]
