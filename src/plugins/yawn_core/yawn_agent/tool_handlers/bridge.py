# ruff: noqa: TID252, E501, TRY003, TRY301
"""私聊协作工具族（群聊侧）：把调用者本人的事转到私聊、在他的私聊小助手里记事、查进展。

业务和隐私边界都在 ``agent_bridge``；这里只做调用者校验、群名、好友检查和结果投影。
工具只作用于**调用者本人**，不提供 user_id 参数；定时任务/主动发言（没有调用者）里不能用。
"""

from __future__ import annotations

import asyncio
from typing import Any

from .. import agent_bridge as bridge
from ..context import now_beijing
from ..log import dbg, dbg_exc
from ..tool_args import (
    ERROR_INVALID_ARGS,
    ERROR_PERMISSION,
    ERROR_QUOTA,
    ERROR_UNSUPPORTED,
    ToolError,
)
from ..tool_execution import ToolExecutionContext, ToolHandlerResult

FAMILY = "private"
NAMES = frozenset(["move_to_private", "add_to_my_assistant", "my_private_followups"])

_CODES = {"quota": ERROR_QUOTA, "permission": ERROR_PERMISSION, "not_found": ERROR_INVALID_ARGS}
_background: set[asyncio.Task[Any]] = set()


def _actor(context: ToolExecutionContext) -> int:
    actor = int(context.actor_user_id or 0)
    if actor <= 0:
        raise ToolError(
            "私聊协作只能替当前发言的成员本人做",
            code=ERROR_PERMISSION,
            hint="定时任务和主动发言里不能用",
        )
    return actor


async def _group_name(context: ToolExecutionContext) -> str:
    if context.session is not None:
        try:
            from ...data_models.bot_group import BotGroup

            row = await context.session.get(BotGroup, int(context.group_id))
            if row is not None and row.group_name:
                return str(row.group_name)
        except Exception:  # noqa: BLE001
            dbg_exc("读群名失败(忽略)")
    return str(context.group_id)


async def _reachable(context: ToolExecutionContext, actor: int) -> None:
    if not bridge.enabled():
        raise ToolError("私聊协作没开", code=ERROR_UNSUPPORTED, hint="在群里直接说就好")
    reason = bridge.can_message_privately(actor)
    if reason:
        raise ToolError(reason, code=ERROR_UNSUPPORTED, hint="在群里直接说就好，或者请他私聊你")
    if not await bridge.is_friend(context.bot, actor):
        raise ToolError(
            "他还不是好友，私聊发不过去",
            code=ERROR_PERMISSION,
            hint="照实告诉他：加个好友（或直接私聊你）就能私聊接着办；这次先在群里简单说",
        )


def _launch(context: ToolExecutionContext, actor: int, handoff: dict[str, Any]) -> None:
    from .. import private_bridge_turn

    delay = float(getattr(bridge.private_agent_config, "agent_bridge_handoff_delay_seconds", 3.0))

    async def run() -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            await private_bridge_turn.open_handoff(context.bot, actor, handoff)
        except Exception:  # noqa: BLE001
            dbg_exc(f"群 {context.group_id} 转私聊开场失败")
            bridge.update_handoff(actor, str(handoff["id"]), status="failed")

    task = asyncio.create_task(run())
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _move(args: dict[str, Any], context: ToolExecutionContext) -> ToolHandlerResult:
    actor = _actor(context)
    await _reachable(context, actor)
    try:
        handoff, created = bridge.create_handoff(
            actor,
            group_id=context.group_id,
            group_name=await _group_name(context),
            topic=str(args.get("topic") or ""),
            asked=str(context.current_intent_text or ""),
            reason=str(args.get("reason") or "asked"),
        )
    except bridge.BridgeError as exc:
        raise ToolError(str(exc), code=_CODES.get(exc.code, ERROR_INVALID_ARGS), hint=exc.hint) from exc
    if created:
        _launch(context, actor, handoff)
    dbg(f"群 {context.group_id} 成员 {actor} 的事转到私聊: {handoff['id']} 新建={created}")
    return ToolHandlerResult(
        {
            "moved": True,
            "already_moving": not created,
            "topic": handoff["topic"],
            "note": "私聊那边马上会找他。群里只回一句短的（比如“我私聊你啦”），别在群里把内容说完",
        }
    )


async def _assist(args: dict[str, Any], context: ToolExecutionContext) -> ToolHandlerResult:
    from .. import private_organizer as organizer

    actor = _actor(context)
    await _reachable(context, actor)
    kind = str(args.get("kind") or "")
    now = now_beijing()
    payload: dict[str, Any] = {"action": "add", "content": args.get("content")}
    try:
        if kind == "reminder":
            payload.update({key: args[key] for key in ("at", "in_minutes") if args.get(key) is not None})
            origin = {"kind": "group", "group_id": int(context.group_id), "group_name": await _group_name(context)}
            result = organizer.reminders(actor, payload, now, origin=origin)
            view = result.get("scheduled") or {}
            out = {"saved": "reminder", "when": view.get("due") or view.get("when"), "minutes_from_now": result.get("minutes_from_now")}
        elif kind == "todo":
            if args.get("due"):
                payload["due"] = args["due"]
            organizer.todos(actor, payload, now)
            out = {"saved": "todo"}
        elif kind == "note":
            organizer.notes(actor, payload, now)
            out = {"saved": "note"}
        else:
            raise ToolError("kind 只能是 reminder/todo/note", code=ERROR_INVALID_ARGS)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(str(exc) or "没记上", code=ERROR_INVALID_ARGS, hint="照实告诉他没记上") from exc
    out["note"] = "已经记进他的私聊小助手（到点私聊提醒）；群里只说记上了，别把他的私人清单列出来"
    dbg(f"群 {context.group_id} 成员 {actor} 记进私聊小助手: {kind}")
    return ToolHandlerResult(out, mutated_db=False)


async def handle(name: str, args: dict[str, Any], context: ToolExecutionContext) -> ToolHandlerResult:
    if name == "move_to_private":
        return await _move(args, context)
    if name == "add_to_my_assistant":
        return await _assist(args, context)
    if name == "my_private_followups":
        actor = _actor(context)
        return ToolHandlerResult(bridge.group_view(actor, context.group_id))
    raise ValueError(f"未知私聊协作工具: {name}")


__all__ = ["FAMILY", "NAMES", "handle"]
