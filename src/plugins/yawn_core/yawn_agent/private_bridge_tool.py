# ruff: noqa: E501, TID252, TRY003, PLR0911, PLR0912, C901, PLR0915, PLR2004
"""私聊工具 ``group_bridge``：替他在群里转告/提问、看回复、看从群里转来的事。

规则在 ``agent_bridge``：发到群里前要他确认（他发来下一条消息后才能确认）；
同一件事沿用同一个待确认，避免“要发吗？”“发”循环；本群策略关掉转告时拒绝。
"""

from __future__ import annotations

from typing import Any

from ..superuser import SUPERUSER_ROLE, superuser_full_access
from . import agent_bridge as bridge
from .log import dbg, dbg_exc
from .tool_args import (
    ERROR_INVALID_ARGS,
    ERROR_NOT_FOUND,
    ERROR_PERMISSION,
    ERROR_QUOTA,
    ERROR_UNSUPPORTED,
    ToolError,
)

_CODES = {
    "quota": ERROR_QUOTA,
    "permission": ERROR_PERMISSION,
    "not_found": ERROR_NOT_FOUND,
    "invalid_args": ERROR_INVALID_ARGS,
}


def _error(exc: bridge.BridgeError) -> ToolError:
    return ToolError(str(exc), code=_CODES.get(exc.code, ERROR_INVALID_ARGS), hint=exc.hint)


async def _superuser_groups(ctx: Any, raw: str | None) -> list[Any]:
    """超级管理员：他不在的群也能转告（Bot 认识、并开着 Agent 的群）。"""

    from .private_context import superuser_tool_groups

    needle = str(raw or "").strip().casefold()
    if not needle:
        return []
    groups = await superuser_tool_groups(ctx.session, ctx.user_id, [])
    if needle.isdigit():
        return [group for group in groups if str(group.group_id) == needle]
    exact = [group for group in groups if group.name.casefold() == needle]
    return exact or [group for group in groups if needle in group.name.casefold()]


async def _pick_group(ctx: Any, raw: str | None) -> Any:
    from .private_tools import _match_groups  # pyright: ignore[reportPrivateUsage]

    try:
        matched = _match_groups(ctx, raw)
    except ToolError:
        if not superuser_full_access(ctx.user_id):
            raise
        matched = []
    if not matched and superuser_full_access(ctx.user_id):
        matched = await _superuser_groups(ctx, raw)
        if not matched:
            raise ToolError(
                f"Bot 不在叫「{raw or ''}」的群里",
                code=ERROR_NOT_FOUND,
                hint="让他给群号，或者先确认 Bot 在那个群",
            )
    groups = [group for group in matched if group.agent_readable]
    if not groups:
        raise ToolError(
            "这个群我没开着，或者他在那个群退出了 Agent 记忆",
            code=ERROR_PERMISSION,
            hint="照实告诉他这个群我帮不上，可以自己去群里说",
        )
    if len(groups) > 1:
        names = "、".join(group.name for group in groups[:6])
        raise ToolError(f"他在好几个群：{names}", code=ERROR_INVALID_ARGS, hint="问他是哪个群，再用 group 指定")
    return groups[0]


def _display_name(ctx: Any, group: Any) -> str:
    return str(getattr(group, "nickname", None) or ctx.nickname or ctx.user_id)[:20]


async def _send(ctx: Any, item: bridge.PendingRelay) -> dict[str, Any]:
    """真正发到群里：再查一次群策略/他是否退出/额度，发出后落库成 Bot 发言。"""

    if ctx.bot is None:
        raise ToolError("现在连不上 QQ", code=ERROR_UNSUPPORTED, hint="稍后再试")
    if not bridge.relay_allowed_in_group(item.group_id, ctx.user_id):
        raise ToolError("这个群的群主关掉了私聊转告", code=ERROR_PERMISSION, hint="照实告诉他，可以自己去群里说")
    if await bridge.opted_out(ctx.session, item.group_id, ctx.user_id):
        raise ToolError("他在这个群退出了 Agent 记忆", code=ERROR_PERMISSION, hint="照实告诉他")
    try:
        bridge.check_relay_quota(ctx.user_id, item.group_id)
    except bridge.BridgeError as exc:
        raise _error(exc) from exc
    try:
        raw = await ctx.bot.call_api("send_group_msg", group_id=item.group_id, message=item.text)
    except Exception as exc:
        dbg_exc(f"私聊用户 {ctx.user_id} 转告到群 {item.group_id} 失败")
        raise ToolError(
            f"群里没发出去：{str(exc)[:60] or type(exc).__name__}",
            code="upstream",
            hint="照实告诉他（可能被禁言了），稍后再试或自己去群里说",
        ) from exc
    message_id = 0
    if isinstance(raw, dict):
        try:
            message_id = int(raw.get("message_id") or 0)
        except (TypeError, ValueError):
            message_id = 0
    await _persist(ctx, item.group_id, message_id, item.text)
    relay = bridge.record_relay(
        ctx.user_id,
        group_id=item.group_id,
        group_name=item.group_name,
        kind=item.kind,
        text=item.text,
        message_id=message_id or None,
        handoff_id=item.handoff_id,
    )
    dbg(f"私聊用户 {ctx.user_id} 在群 {item.group_id} {item.kind} 了一句（{relay['id']}）")
    try:
        # 记进本群操作记录：群主/管理员在网页「群管理 → 操作记录」看得到是谁让 Bot 发的。
        from .group_manage import audit

        audit(
            item.group_id,
            actor_id=ctx.user_id,
            actor_role=SUPERUSER_ROLE if superuser_full_access(ctx.user_id) else "member",
            source="private",
            action=f"bridge_{item.kind}",
            result="success",
            detail=item.text,
        )
    except Exception:  # noqa: BLE001
        dbg_exc(f"群 {item.group_id} 转告审计写入失败(忽略)")
    out: dict[str, Any] = {"done": True, "action": item.kind, "group": item.group_name, "sent": item.text, "id": relay["id"]}
    if item.kind == "ask":
        if relay.get("status") == "open":
            out["collecting_until"] = relay.get("until")
            out["note"] = "群友引用回复会攒一会儿私聊转给他；他问起就用 replies 看"
        else:
            out["note"] = "发出去了，但没拿到消息编号，收不了引用回复"
    return out


async def _persist(ctx: Any, group_id: int, message_id: int, text: str) -> None:
    """落库成 Bot 发言：群里的我之后看得到自己替人转告过什么。"""

    if not message_id or ctx.session is None or ctx.bot_id is None:
        return
    try:
        from .config_store import get_or_create_config
        from .dialogue_support import persist_bot_reply

        config = await get_or_create_config(ctx.session, group_id)
        retention = int(getattr(config, "raw_retention_days", 7) or 7)
        await persist_bot_reply(ctx.session, int(ctx.bot_id), int(group_id), message_id, text, retention)
        ctx.mutated = True
    except Exception:  # noqa: BLE001
        dbg_exc(f"群 {group_id} 转告落库失败(忽略)")


async def group_bridge_tool(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    if not bridge.enabled():
        raise ToolError("群聊协作没开", code=ERROR_UNSUPPORTED, hint="照实告诉他")
    action = str(args.get("action") or "handoffs")
    if action == "handoffs":
        rows = bridge.handoffs(ctx.user_id)[-8:][::-1]
        return {
            "from_groups": [
                {
                    "id": row.get("id"),
                    "group": row.get("group_name"),
                    "topic": row.get("topic"),
                    "his_words_in_group": row.get("asked") or None,
                    "when": row.get("created"),
                    "status": bridge.STATUS_LABELS.get(str(row.get("status")), row.get("status")),
                }
                for row in rows
            ]
        }
    if action == "done":
        handoff_id = str(args.get("id") or "").strip()
        if not handoff_id:
            active = bridge.active_handoffs(ctx.user_id, ctx.now)
            if len(active) != 1:
                raise ToolError("要写是哪件事（id）", code=ERROR_INVALID_ARGS, hint="用 handoffs 看编号")
            handoff_id = str(active[0]["id"])
        row = bridge.update_handoff(ctx.user_id, handoff_id, status="done")
        if row is None:
            raise ToolError("没有这件事", code=ERROR_NOT_FOUND, hint="用 handoffs 看编号")
        return {"marked_done": handoff_id, "topic": row.get("topic")}
    if action == "replies":
        relay_id = str(args.get("id") or "").strip()
        rows = [row for row in bridge.relays(ctx.user_id) if row.get("kind") == "ask"]
        if relay_id:
            rows = [row for row in rows if str(row.get("id")) == relay_id]
            if not rows:
                raise ToolError("没有这个提问", code=ERROR_NOT_FOUND, hint="不写 id 看最近的提问")
        return {
            "asks": [
                {
                    "id": row.get("id"),
                    "group": row.get("group_name"),
                    "question": row.get("text"),
                    "open": row.get("status") == "open",
                    "replies": [
                        {"who": reply.get("from_name"), "said": reply.get("text"), "at": reply.get("at")}
                        for reply in (row.get("replies") or [])[-10:]
                    ],
                }
                for row in rows[-5:][::-1]
            ]
        }
    if action == "close":
        count = bridge.close_relay(ctx.user_id, str(args.get("id") or "").strip() or None)
        return {"closed": count, "note": "不再收这些提问的回复了" if count else "没有还在收回复的提问"}
    if action == "cancel":
        count = bridge.cancel_pending(ctx.user_id, str(args.get("confirm_id") or "").strip() or None)
        return {"cancelled": count, "note": "已作废，没有发到群里" if count else "没有待确认的转告"}
    if action == "confirm":
        try:
            item = bridge.take_pending(ctx.user_id, args.get("confirm_id"))
        except bridge.BridgeError as exc:
            waiting = bridge.pending_view(ctx.user_id)
            premature = exc.code == "needs_reply" or (
                exc.code == "not_found" and bool(waiting) and not any(row["he_replied_since"] for row in waiting)
            )
            if not premature:
                raise _error(exc) from exc
            # 同一轮自问自答：不算错，告诉模型去问他并收尾（避免一轮里反复重试）。
            ctx.stop_tools = True
            row = waiting[0] if waiting else {}
            return {
                "done": False,
                "waiting_for_reply": True,
                "confirm_id": row.get("confirm_id"),
                "summary": row.get("summary"),
                "note": "还没发：他还没回复，不能替他确认。本轮别再调 group_bridge，把群名和原话问他，等他下一条",
            }
        return await _send(ctx, item)
    if action not in {"post", "ask"}:
        raise ToolError("没有这个动作", code=ERROR_INVALID_ARGS)
    group = await _pick_group(ctx, args.get("group"))
    if not bridge.relay_allowed_in_group(group.group_id, ctx.user_id):
        raise ToolError("这个群的群主关掉了私聊转告", code=ERROR_PERMISSION, hint="照实告诉他，可以自己去群里说")
    try:
        body = bridge.normalize_relay(action, args.get("text"))
        bridge.check_relay_quota(ctx.user_id, group.group_id)
    except bridge.BridgeError as exc:
        raise _error(exc) from exc
    text = bridge.relay_text(action, _display_name(ctx, group), body)
    verb = "转告" if action == "post" else "问大家"
    summary = f"在「{group.name}」替你{verb}：「{body if len(body) <= 60 else body[:60] + '…'}」"
    active = bridge.active_handoffs(ctx.user_id, ctx.now)
    handoff_id = next((str(row["id"]) for row in active if int(row.get("group_id") or 0) == group.group_id), None)
    if not getattr(bridge.private_agent_config, "agent_bridge_confirm", True):
        item = bridge.PendingRelay(
            id="direct", user_id=ctx.user_id, group_id=group.group_id, group_name=group.name, kind=action,
            text=text, summary=summary, arrival=0, created=0.0, expires=0.0, handoff_id=handoff_id,
        )
        return {**(await _send(ctx, item)), "summary": summary}
    pending, reused = bridge.create_pending(
        ctx.user_id,
        group_id=group.group_id,
        group_name=group.name,
        kind=action,
        text=text,
        summary=summary,
        handoff_id=handoff_id,
    )
    if reused and bridge.replied_since(pending):
        return {
            "needs_confirm": True,
            "confirm_id": pending.id,
            "summary": summary,
            "already_asked": True,
            "he_replied_since": True,
            "note": "这句之前已经问过他，他也回话了：他那句是同意就直接 confirm 这个 confirm_id；不同意就 cancel；看不出来再问一次",
        }
    if reused:
        ctx.stop_tools = True
        return {
            "needs_confirm": True,
            "confirm_id": pending.id,
            "summary": summary,
            "already_asked": True,
            "note": "已经在等他确认了。本轮别再调 group_bridge，把群名和原话问他",
        }
    return {
        "needs_confirm": True,
        "confirm_id": pending.id,
        "summary": summary,
        "will_send": text,
        "note": "还没发。把群名和要发的原话问他要不要发，然后结束这一轮；他下一条同意后再 confirm，不同意就 cancel",
    }


__all__ = ["group_bridge_tool"]
