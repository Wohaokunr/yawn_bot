# ruff: noqa: E501, TRY003, PLR0911, PLR0912, C901, TID252, PLR2004, PLR0913, PLR0917
"""私聊工具 ``schedules``：私聊提醒和他在各群的定时任务一起查、一起管。

- 编号统一：私聊提醒 ``p<号>``，群定时任务 ``g<号>``（``schedule_hub.parse_ref``）；
- 私聊提醒的增删改走 ``private_organizer.reminders``（同一份个人数据与规则引擎）；
- 给群设任务 / 改群任务要发到群里的内容：先返回 ``needs_confirm``，他下一条同意后
  ``confirm``（``schedule_hub`` 的待确认，和群管代办、私聊转告同一套防循环做法）；
- 停、恢复、取消群任务不往群里发东西，直接做；只能动自己设的，群主/管理员实时
  确认后能动本群任何人的。

结果里 ``done: True`` + ``action`` 才算办成（声明核验据此放行“设好了/取消了”）。
"""

from __future__ import annotations

from typing import Any

from . import schedule_hub as hub
from .log import dbg
from .schedule_store import (
    OPEN_STATUSES,
    STATUS_ACTIVE,
    STATUS_PAUSED,
    ScheduleRuleError,
    ScheduleUnavailableError,
    TaskDraft,
    apply_draft,
    cancel_task,
    check_mention_members,
    pause_task,
    resume_task,
    task_mentions,
    task_public_dict,
    validate_draft,
)
from .tool_args import (
    ERROR_INVALID_ARGS,
    ERROR_NOT_FOUND,
    ERROR_PERMISSION,
    ERROR_QUOTA,
    ERROR_UNSUPPORTED,
    ToolError,
)

ACTIONS = ("list", "add", "preview", "edit", "pause", "resume", "cancel", "history", "confirm", "drop")
_CODES = {
    "quota": ERROR_QUOTA,
    "permission": ERROR_PERMISSION,
    "not_found": ERROR_NOT_FOUND,
    "invalid_args": ERROR_INVALID_ARGS,
    "needs_reply": ERROR_PERMISSION,
}
_RULE_KEYS = (
    "kind",
    "at",
    "every_minutes",
    "times",
    "weekdays",
    "month_days",
    "months",
    "cron",
    "start_at",
    "end_at",
    "skip_dates",
    "window_start",
    "window_end",
)
_CLOCK = {"type": "string", "pattern": r"^([01]?\d|2[0-3])[:：][0-5]\d$"}
TOOL_PROPERTIES: dict[str, Any] = {
    "action": {"type": "string", "enum": list(ACTIONS)},
    "ref": {"type": "string", "maxLength": 12, "description": "编号：私聊提醒 p3、群任务 g12（list 返回的 ref）"},
    "group": {"type": "string", "minLength": 1, "maxLength": 60, "description": "add 时给群设：群名（可以只写一部分）或群号；不写就是私聊提醒"},
    "content": {"type": "string", "minLength": 1, "maxLength": 600, "description": "到点要发的提醒原话（mode=notify）或到点要我去办的事（mode=agent）"},
    "title": {"type": "string", "maxLength": 40},
    "mode": {"type": "string", "enum": ["notify", "agent"], "description": "notify 到点直接发这句（默认）；agent 到点由我去办（查资料、整理、发图…）"},
    "in_minutes": {"type": "integer", "minimum": 1, "maximum": 527040, "description": "单次：多少分钟后"},
    "at": {"type": "string", "maxLength": 32, "description": "单次：YYYY-MM-DD HH:MM（北京时间）"},
    "kind": {"type": "string", "enum": ["once", "interval", "daily", "weekly", "monthly", "cron"], "description": "循环规则；daily/weekly/monthly 配 times"},
    "times": {"type": "array", "items": _CLOCK, "maxItems": 8, "description": '钟点，如 ["08:00"]'},
    "weekdays": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 7}, "maxItems": 7, "description": "weekly：1=周一…7=周日"},
    "month_days": {"type": "array", "items": {"type": "integer", "minimum": -1, "maximum": 31}, "maxItems": 31, "description": "monthly：几号，-1=最后一天"},
    "months": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 12}, "maxItems": 12, "description": "只在这些月（配 monthly 就是每年某天）"},
    "every_minutes": {"type": "integer", "minimum": 1, "maximum": 44640, "description": "interval：间隔分钟"},
    "cron": {"type": "string", "maxLength": 64},
    "window_start": _CLOCK,
    "window_end": _CLOCK,
    "start_at": {"type": "string", "maxLength": 32},
    "end_at": {"type": "string", "maxLength": 32},
    "skip_dates": {"type": "array", "items": {"type": "string"}, "maxItems": 60},
    "max_runs": {"type": "integer", "minimum": 1, "maximum": 10000, "description": "循环总共几次后结束"},
    "mention_me": {"type": "boolean", "description": "群任务到点要不要 @ 他（默认要）"},
    "mention_all": {"type": "boolean", "description": "群任务到点 @全体成员（只有他是那个群的群主/管理员才行）；edit 时传 false 取消"},
    "mention_user_ids": {"type": "array", "items": {"type": "integer", "minimum": 1}, "maxItems": 50, "description": "群任务到点还要 @ 的群成员 QQ 号；edit 时传了就整体替换，[] 清空"},
    "scope": {"type": "string", "enum": ["mine", "managed"], "description": "list：mine 他自己的（默认）；managed 再加上他当群主/管理员的群里别人设的"},
    "channel": {"type": "string", "enum": ["all", "private", "group"]},
    "include_finished": {"type": "boolean"},
    "confirm_id": {"type": "string", "maxLength": 12},
}
TOOL_DESCRIPTION = (
    "他的全部定时事项一起管：私聊提醒（编号 p…）和他在各群设的群定时任务（编号 g…），群里设的私聊里也能查能改。"
    "list 列出（scope=managed 连他管的群里别人设的也看）；add 新建：不写 group 是私聊提醒，写 group 是给那个群设（到点在群里发/办）；"
    "preview 只看规则和接下来几次；edit 改内容或时间（改时间就给新的 at/in_minutes 或 kind+字段）；pause/resume/cancel 停、恢复、取消；history 看最近几次跑得怎么样。"
    "给群设任务或改群任务的内容会先返回 needs_confirm：把群名、时间和原话问他，然后这一轮就结束；他下一条同意后再 confirm（confirm_id），不同意就 drop。"
    "只在他明确要求时用；群里只看得到他让发到群里的内容，不要把你们私聊的其他内容写进群任务"
)


def _error(exc: hub.HubError) -> ToolError:
    return ToolError(str(exc), code=_CODES.get(exc.code, ERROR_INVALID_ARGS), hint=exc.hint or None)


def _unavailable() -> ToolError:
    return ToolError("群定时任务还没启用（数据库迁移未执行）", code=ERROR_UNSUPPORTED, hint="照实告诉他；私聊提醒照常能用")


def _rule_args(args: dict[str, Any]) -> dict[str, Any]:
    return {key: args[key] for key in (*_RULE_KEYS, "in_minutes", "max_runs") if args.get(key) not in (None, "", [])}


def group_schedule(args: dict[str, Any]) -> dict[str, Any]:
    """私聊工具参数 → 群任务规则 JSON（in_minutes 换成 delay_minutes）。"""

    from .tool_handlers.schedule import schedule_from_args

    data = dict(args)
    if not data.get("kind"):
        if data.get("in_minutes") is not None:
            data = {**data, "kind": "once", "delay_minutes": data["in_minutes"]}
        elif data.get("at"):
            data = {**data, "kind": "once"}
    return schedule_from_args(data)


def _pick_group(ctx: Any, raw: str) -> Any:
    from .private_tools import _single_group  # pyright: ignore[reportPrivateUsage]

    return _single_group(ctx, raw)


async def _managed_group_ids(ctx: Any) -> set[int]:
    """他在数据库里是群主/管理员、且和我共同在的群（只是列表的粗筛；动别人的任务时实时确认）。"""

    from sqlalchemy import select

    from ..data_models.user_group import UserGroup

    shared = {int(group.group_id) for group in getattr(ctx, "groups", []) or []}
    if not shared or ctx.session is None:
        return set()
    rows = await ctx.session.execute(
        select(UserGroup.group_id).where(UserGroup.user_id == int(ctx.user_id), UserGroup.role.in_(("owner", "admin")))
    )
    return {int(group_id) for (group_id,) in rows} & shared


def _compact(item: dict[str, Any]) -> dict[str, Any]:
    keep = ("ref", "where", "title", "what", "mode", "rule", "status_label", "next", "runs", "max_runs", "set_from", "retrying_at", "status_reason")
    data = {key: item[key] for key in keep if item.get(key) not in (None, "", 0) or key == "ref"}
    if item.get("channel") == "group" and not item.get("mine", True):
        data["set_by"] = item.get("creator")
    return data


def _group_name(ctx: Any, group_id: int) -> str:
    for group in getattr(ctx, "groups", []) or []:
        if int(group.group_id) == int(group_id):
            return str(group.name)
    return str(group_id)


async def _list(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    managed = await _managed_group_ids(ctx) if args.get("scope") == "managed" else set()
    group_id = None
    if args.get("group"):
        group_id = int(_pick_group(ctx, str(args["group"])).group_id)
    data = await hub.user_items(
        ctx.session,
        ctx.user_id,
        include_finished=bool(args.get("include_finished")),
        managed_groups=managed,
        channel=str(args.get("channel") or "all"),
        group_id=group_id,
    )
    items = data["items"]
    out: dict[str, Any] = {
        "items": [_compact(item) for item in items[:20]],
        "private": data["counts"]["private"],
        "in_groups": data["counts"]["group"],
    }
    if len(items) > 20:
        out["truncated"] = True
    if not data["group_ready"]:
        out["note"] = "群定时任务还没启用，只列了私聊提醒"
    if not items:
        out["note"] = "他现在没有定时提醒或群定时任务"
    waiting = hub.pending_view(ctx.user_id)
    if waiting:
        out["waiting_for_his_ok"] = waiting
    return out


def _private_args(args: dict[str, Any], action: str, item_id: int | None = None) -> dict[str, Any]:
    payload = {"action": action, **_rule_args(args)}
    for key in ("content", "title", "mode"):
        if args.get(key):
            payload[key] = args[key]
    if item_id is not None:
        payload["id"] = item_id
    return payload


def _draft_mentions(args: dict[str, Any], task: Any = None) -> tuple[bool | None, list[int] | None]:
    """参数里的 @ 目标；都没传时返回 (None, None) = 不改。"""

    if "mention_all" not in args and "mention_user_ids" not in args:
        return None, None
    old_all, old_members = task_mentions(task) if task is not None else (False, [])
    mention_all = bool(args["mention_all"]) if "mention_all" in args else old_all
    members = list(args.get("mention_user_ids") or []) if "mention_user_ids" in args else old_members
    return mention_all, members


async def _check_mention_all(ctx: Any, group_id: int, *, wanted: bool | None, had: bool = False) -> None:
    try:
        await hub.check_mention_all(ctx.bot, ctx.user_id, group_id, wanted=wanted, had=had)
    except hub.HubError as exc:
        raise _error(exc) from exc


def _mention_summary(*, mention_all: bool | None, members: list[int] | None) -> str:
    parts = (["@全体成员"] if mention_all else []) + [f"@{item}" for item in (members or [])[:5]]
    if members and len(members) > 5:
        parts.append(f"等 {len(members)} 人")
    return f"，到点 {' '.join(parts)}" if parts else ""


def _done(action: str, result: dict[str, Any]) -> dict[str, Any]:
    return {"done": True, "action": action, **result}


async def _add(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    from . import private_organizer as organizer

    if not args.get("content"):
        raise ToolError("要写到点提醒什么/办什么", code=ERROR_INVALID_ARGS, hint="content 写他的原意")
    if not args.get("group"):
        result = organizer.reminders(ctx.user_id, _private_args(args, "add"), ctx.now, origin={"kind": "private"})
        return _done("add", {"where": "私聊", **result})
    group = _pick_group(ctx, str(args["group"]))
    mention_all, mention_ids = _draft_mentions(args)
    await _check_mention_all(ctx, int(group.group_id), wanted=mention_all)
    draft = TaskDraft(
        instruction=str(args.get("content") or ""),
        schedule=group_schedule(args),
        title=str(args.get("title") or ""),
        mode=str(args.get("mode") or "notify"),
        max_runs=args.get("max_runs"),
        mention_creator=bool(args.get("mention_me", True)),
        mention_all=mention_all,
        mention_user_ids=mention_ids,
    )
    if not draft.schedule:
        raise ToolError("要给时间", code=ERROR_INVALID_ARGS, hint="at、in_minutes 或 kind+times 等")
    try:
        await hub.check_group_create(ctx.session, ctx.bot, ctx.user_id, int(group.group_id))
        preview = hub.preview_group_draft(draft, ctx.now)
    except hub.HubError as exc:
        raise _error(exc) from exc
    except ScheduleUnavailableError as exc:
        raise _unavailable() from exc
    verb = "到点在群里办" if preview["mode"] == "agent" else "到点在群里发"
    what = draft.instruction if len(draft.instruction) <= 60 else draft.instruction[:60] + "…"
    summary = f"给群「{group.name}」设定时任务：{preview['rule']}，{verb}「{what}」{_mention_summary(mention_all=mention_all, members=mention_ids)}"
    draft_dict = {
        "instruction": draft.instruction,
        "schedule": draft.schedule,
        "title": draft.title,
        "mode": draft.mode,
        "max_runs": draft.max_runs,
        "mention_creator": draft.mention_creator,
        "mention_all": draft.mention_all,
        "mention_user_ids": draft.mention_user_ids,
    }
    return _ask(ctx, group.group_id, group.name, "create", draft_dict, summary, preview=preview)


def _ask(
    ctx: Any,
    group_id: int,
    group_name: str,
    kind: str,
    draft: dict[str, Any],
    summary: str,
    *,
    preview: dict[str, Any] | None = None,
    task_id: int | None = None,
) -> dict[str, Any]:
    pending, reused = hub.create_pending(
        ctx.user_id, group_id=group_id, group_name=group_name, kind=kind, draft=draft, summary=summary, task_id=task_id
    )
    if reused and hub.replied_since(pending):
        return {
            "needs_confirm": True,
            "confirm_id": pending.id,
            "summary": summary,
            "already_asked": True,
            "he_replied_since": True,
            "note": "这件事之前已经问过他，他也回话了：他那句是同意就直接 confirm 这个 confirm_id；不同意就 drop；看不出来再问一次",
        }
    if reused:
        ctx.stop_tools = True
        return {
            "needs_confirm": True,
            "confirm_id": pending.id,
            "summary": summary,
            "already_asked": True,
            "note": "已经在等他确认了。本轮别再调 schedules，把 summary 问他",
        }
    out: dict[str, Any] = {
        "needs_confirm": True,
        "confirm_id": pending.id,
        "summary": summary,
        "note": "还没设。把群名、时间和原话问他要不要这样设，然后结束这一轮；他下一条同意后再 confirm，不同意就 drop",
    }
    if preview:
        out["next_times"] = preview.get("next_times", [])[:3]
    return out


async def _confirm(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    try:
        item = hub.take_pending(ctx.user_id, args.get("confirm_id"))
    except hub.HubError as exc:
        waiting = hub.pending_view(ctx.user_id)
        premature = exc.code == "needs_reply" or (
            exc.code == "not_found" and bool(waiting) and not any(row["he_replied_since"] for row in waiting)
        )
        if not premature:
            raise _error(exc) from exc
        ctx.stop_tools = True
        row = waiting[0] if waiting else {}
        return {
            "done": False,
            "waiting_for_reply": True,
            "confirm_id": row.get("confirm_id"),
            "summary": row.get("summary"),
            "note": "还没设：他还没回复，不能替他确认。本轮别再调 schedules，把 summary 问他，等他下一条",
        }
    draft = TaskDraft(
        instruction=str(item.draft.get("instruction") or ""),
        schedule=dict(item.draft.get("schedule") or {}),
        title=str(item.draft.get("title") or ""),
        mode=str(item.draft.get("mode") or "notify"),
        max_runs=item.draft.get("max_runs"),
        mention_creator=bool(item.draft.get("mention_creator", True)),
        mention_all=item.draft.get("mention_all"),
        mention_user_ids=item.draft.get("mention_user_ids"),
    )
    try:
        if item.kind == "edit" and item.task_id is not None:
            task = await hub.owned_group_task(ctx.session, ctx.bot, ctx.user_id, item.task_id)
            await _apply_group_edit(task, draft, ctx, rule_changed=bool(item.draft.get("rule_changed")))
            reused = False
        else:
            task, reused = await hub.create_group_task(
                ctx.session,
                bot=ctx.bot,
                user_id=ctx.user_id,
                group_id=item.group_id,
                creator_name=_creator_name(ctx, item.group_id),
                draft=draft,
                now=ctx.now,
            )
    except hub.HubError as exc:
        raise _error(exc) from exc
    except ScheduleUnavailableError as exc:
        raise _unavailable() from exc
    ctx.mutated = True
    view = task_public_dict(task, now=ctx.now, preview_count=3)
    view["ref"] = f"g{int(task.id)}"
    dbg(f"私聊用户 {ctx.user_id} 确认群 {item.group_id} 定时任务 {item.kind}: g{task.id}")
    _audit(item.group_id, ctx.user_id, f"schedule_{item.kind}", f"g{task.id} {draft.instruction}")
    action = "edit" if item.kind == "edit" else "add"
    return _done(
        action,
        {
            "group": item.group_name,
            "task": view,
            **({"note": "同样的群任务已经有了，没有重复设"} if reused else {"note": "群里的我到点会照着做；群里看得到这是他在私聊里让我设的"}),
        },
    )


def _creator_name(ctx: Any, group_id: int) -> str:
    for group in getattr(ctx, "groups", []) or []:
        if int(group.group_id) == int(group_id) and group.nickname:
            return str(group.nickname)
    return str(ctx.nickname or ctx.user_id)


def _audit(group_id: int, user_id: int, action: str, detail: str) -> None:
    try:
        from .group_manage import audit

        audit(group_id, actor_id=user_id, actor_role="member", source="private", action=action, result="success", detail=detail[:200])
    except Exception:  # noqa: BLE001
        dbg(f"群 {group_id} 定时任务审计写入失败(忽略)")


async def _apply_group_edit(task: Any, draft: TaskDraft, ctx: Any, *, rule_changed: bool) -> None:
    from .schedule_policy import load_settings

    try:
        validated = validate_draft(
            draft,
            settings=load_settings(),
            now=ctx.now,
            anchor=ctx.now if rule_changed else task.created_at,
            run_count=0 if rule_changed else int(task.run_count or 0),
        )
        await check_mention_members(ctx.session, int(task.group_id), validated.mention_targets)
    except ScheduleRuleError as exc:
        raise ToolError(str(exc), code=ERROR_INVALID_ARGS, hint=exc.hint or None) from exc
    if rule_changed:
        task.run_count = 0
        task.created_at = ctx.now
    apply_draft(task, validated, now=ctx.now, restart=rule_changed)
    await ctx.session.flush()


async def _group_op(action: str, task_id: int, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    try:
        task = await hub.owned_group_task(ctx.session, ctx.bot, ctx.user_id, task_id)
    except hub.HubError as exc:
        raise _error(exc) from exc
    except ScheduleUnavailableError as exc:
        raise _unavailable() from exc
    group_name = _group_name(ctx, int(task.group_id))
    if action == "history":
        view = task_public_dict(task, now=ctx.now, preview_count=5)
        view["ref"] = f"g{task_id}"
        view["recent_runs"] = [
            {"at": row.get("at"), "result": row.get("result"), **({"said": str(row.get("preview"))[:80]} if row.get("preview") else {})}
            for row in list(task.history or [])[:3]
            if isinstance(row, dict)
        ]
        return {"task": view, "group": group_name}
    if action in {"pause", "resume", "cancel"} and task.status not in OPEN_STATUSES:
        verb = {"pause": "暂停", "resume": "恢复", "cancel": "取消"}[action]
        return {"done": False, "note": f"这个群任务已经是{hub.STATUS_LABELS.get(task.status, task.status)}，不用再{verb}"}
    if action == "pause":
        if task.status == STATUS_PAUSED:
            return {"done": False, "note": "已经是暂停状态"}
        pause_task(task, now=ctx.now, reason=f"由 {ctx.user_id} 在私聊里暂停")
    elif action == "resume":
        if task.status == STATUS_ACTIVE:
            return {"done": False, "note": "任务本来就在运行"}
        resume_task(task, now=ctx.now)
    elif action == "cancel":
        cancel_task(task, now=ctx.now, by_user_id=ctx.user_id, reason="在私聊里取消")
    elif action == "edit":
        rule_changed = bool(args.get("kind") or args.get("at") or args.get("in_minutes"))
        mention_all, mention_ids = _draft_mentions(args, task)
        await _check_mention_all(ctx, int(task.group_id), wanted=mention_all, had=task_mentions(task)[0])
        draft = TaskDraft(
            instruction=str(args.get("content") or task.instruction),
            schedule=group_schedule(args) if rule_changed else dict(task.schedule or {}),
            title=str(args.get("title") or (task.title if not args.get("content") else "")),
            mode=str(args.get("mode") or task.mode),
            max_runs=args.get("max_runs") if args.get("max_runs") is not None else task.max_runs,
            mention_creator=bool(args["mention_me"]) if "mention_me" in args else bool(task.mention_creator),
            mention_all=mention_all,
            mention_user_ids=mention_ids,
        )
        mentions_changed = mention_all is not None
        if not mentions_changed and not any(args.get(key) not in (None, "", []) for key in ("content", "title", "mode", "max_runs", "mention_me", "kind", "at", "in_minutes")):
            raise ToolError("没有要改的内容", code=ERROR_INVALID_ARGS, hint="传 content 或新的时间")
        if args.get("content") or args.get("mode") or mentions_changed:
            # 改的是到点发到群里的内容或要 @ 的人：和新建一样要他确认。
            preview = hub.preview_group_draft(draft, ctx.now)
            summary = f"把群「{group_name}」的定时任务 g{task_id} 改成：{preview['rule']}，「{draft.instruction[:60]}」{_mention_summary(mention_all=mention_all, members=mention_ids)}"
            draft_dict = {
                "instruction": draft.instruction,
                "schedule": draft.schedule,
                "title": draft.title,
                "mode": draft.mode,
                "max_runs": draft.max_runs,
                "mention_creator": draft.mention_creator,
                "mention_all": draft.mention_all,
                "mention_user_ids": draft.mention_user_ids,
                "rule_changed": rule_changed,
            }
            return _ask(ctx, int(task.group_id), group_name, "edit", draft_dict, summary, preview=preview, task_id=task_id)
        await _apply_group_edit(task, draft, ctx, rule_changed=rule_changed)
    await ctx.session.flush()
    ctx.mutated = True
    _audit(int(task.group_id), ctx.user_id, f"schedule_{action}", f"g{task_id}")
    view = task_public_dict(task, now=ctx.now, preview_count=3 if action in {"resume", "edit"} else 0)
    view["ref"] = f"g{task_id}"
    return _done(action, {"group": group_name, "task": view})


async def schedules_tool(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    from . import private_organizer as organizer

    action = str(args.get("action") or "list")
    if action == "list":
        return await _list(args, ctx)
    if action == "confirm":
        return await _confirm(args, ctx)
    if action == "drop":
        count = hub.cancel_pending(ctx.user_id, str(args.get("confirm_id") or "").strip() or None)
        return {"dropped": count, "note": "已作废，没有设" if count else "没有待确认的群任务"}
    if action == "add":
        return await _add(args, ctx)
    if action == "preview":
        if args.get("group"):
            group = _pick_group(ctx, str(args["group"]))
            draft = TaskDraft(instruction=str(args.get("content") or "预览"), schedule=group_schedule(args), mode=str(args.get("mode") or "notify"), max_runs=args.get("max_runs"))
            try:
                return {"group": group.name, **hub.preview_group_draft(draft, ctx.now), "note": "还没设"}
            except hub.HubError as exc:
                raise _error(exc) from exc
        return organizer.reminders(ctx.user_id, _private_args(args, "preview"), ctx.now)
    if not args.get("ref"):
        raise ToolError("要写编号 ref", code=ERROR_INVALID_ARGS, hint="先用 list 看编号（p3 / g12）")
    try:
        channel, number = hub.parse_ref(args["ref"])
    except hub.HubError as exc:
        raise _error(exc) from exc
    if action not in {"edit", "pause", "resume", "cancel", "history"}:
        raise ToolError("没有这个动作", code=ERROR_INVALID_ARGS)
    if channel == "group":
        return await _group_op(action, number, args, ctx)
    result = organizer.reminders(ctx.user_id, _private_args(args, action, number), ctx.now)
    if action == "history":
        return result
    if action == "resume" and not result.get("resumed"):
        return {"done": False, **result}
    return _done(action, {"where": "私聊", **result})


__all__ = ["ACTIONS", "TOOL_DESCRIPTION", "TOOL_PROPERTIES", "group_schedule", "schedules_tool"]
