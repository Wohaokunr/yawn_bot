# ruff: noqa: TID252, TRY003, E501, C901
"""定时任务工具族：让成员通过 Agent 设、查、改、取消定时任务。

规则校验、限额和落库都在 ``schedule_store``；到点执行在 ``schedule_runner``。
这里只做权限（自己的任务 / 群管理可管全部）、把工具参数整理成规则 JSON、把
结果投影成模型需要的最小信息。工具本身不往群里发任何消息。
"""

from __future__ import annotations

from typing import Any

from ...data_models.bot_user import BotUser
from ...data_models.user_group import UserGroup
from ..capabilities import user_can_manage_group
from ..context import now_beijing
from ..log import dbg
from ..schedule_policy import load_settings
from ..schedule_spec import format_datetime
from ..schedule_store import (
    OPEN_STATUSES,
    STATUS_ACTIVE,
    STATUS_PAUSED,
    ScheduleRuleError,
    ScheduleUnavailableError,
    TaskDraft,
    apply_draft,
    cancel_task,
    clip,
    create_task,
    get_task,
    list_tasks,
    pause_task,
    resume_task,
    task_public_dict,
    validate_draft,
)
from ..schedule_wake import WAKE_BLOCKED_TOOLS, in_scheduled_wake
from ..tool_args import (
    ERROR_INVALID_ARGS,
    ERROR_NOT_FOUND,
    ERROR_PERMISSION,
    ERROR_QUOTA,
    ERROR_UNSUPPORTED,
    ToolError,
)
from ..tool_execution import ToolExecutionContext, ToolHandlerResult

FAMILY = "schedule"
NAMES = frozenset(
    [
        "schedule_task",
        "list_scheduled_tasks",
        "update_scheduled_task",
        "cancel_scheduled_task",
    ]
)
_SCHEDULE_KEYS = (
    "kind",
    "at",
    "delay_minutes",
    "every_minutes",
    "times",
    "weekdays",
    "month_days",
    "months",
    "cron",
    "start_at",
    "end_at",
    "skip_dates",
)
_QUOTA_CODES = frozenset({"group_limit", "member_limit"})
_PERMISSION_CODES = frozenset({"agent_mode_closed", "unsafe_instruction"})
_RECENT_HISTORY = 3


def schedule_from_args(args: dict[str, Any]) -> dict[str, Any]:
    """工具参数 → 规则 JSON（``schedule_spec.normalize_spec`` 的输入）。"""

    schedule = {key: args[key] for key in _SCHEDULE_KEYS if args.get(key) not in (None, "", [])}
    if args.get("window_start") or args.get("window_end"):
        schedule["window"] = {"start": args.get("window_start"), "end": args.get("window_end")}
    return schedule


def _rule_error(exc: ScheduleRuleError) -> ToolError:
    if exc.code in _QUOTA_CODES:
        code = ERROR_QUOTA
    elif exc.code in _PERMISSION_CODES:
        code = ERROR_PERMISSION
    else:
        code = ERROR_INVALID_ARGS
    return ToolError(str(exc), code=code, hint=exc.hint or None)


def _unavailable() -> ToolError:
    return ToolError(
        "定时任务功能还没有启用（数据库迁移未执行）",
        code=ERROR_UNSUPPORTED,
        hint="如实告诉成员需要管理员先执行数据库迁移，不要假装已经设好",
    )


async def _member_name(session: Any, group_id: int, user_id: int) -> str:
    if session is None:
        return str(user_id)
    row = await session.get(UserGroup, (group_id, user_id))
    name = str(getattr(row, "group_nickname", "") or "").strip()
    if not name:
        user = await session.get(BotUser, user_id)
        name = str(getattr(user, "nickname", "") or "").strip()
    return name[:64] or str(user_id)


async def _actor_is_admin(context: ToolExecutionContext) -> bool:
    if context.actor_can_manage is not None:
        return bool(context.actor_can_manage)
    if context.actor_user_id is None:
        return False
    return bool(await user_can_manage_group(context.bot, context.group_id, context.actor_user_id))


async def _owned_task(context: ToolExecutionContext, task_id: int) -> Any:
    task = await get_task(context.session, task_id, group_id=context.group_id)
    if task is None:
        raise ToolError(
            f"本群没有编号 {task_id} 的定时任务",
            code=ERROR_NOT_FOUND,
            hint="先用 list_scheduled_tasks 查看真实的 task_id",
        )
    if int(task.creator_user_id) != int(context.actor_user_id or 0) and not await _actor_is_admin(context):
        raise ToolError(
            "这个定时任务不是调用者设的，只有设任务的人或群主/管理员能改",
            code=ERROR_PERMISSION,
            hint="如实说明，可以请设任务的人或管理员来改",
        )
    return task


def _history_view(task: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in list(task.history or [])[:_RECENT_HISTORY]:
        if not isinstance(item, dict):
            continue
        row = {"at": item.get("at"), "result": item.get("result")}
        if item.get("preview"):
            row["said"] = clip(item.get("preview"), 80)
        if item.get("error"):
            row["error"] = clip(item.get("error"), 80)
        rows.append(row)
    return rows


async def _create(args: dict[str, Any], context: ToolExecutionContext) -> ToolHandlerResult:
    if in_scheduled_wake():
        raise ToolError(
            "定时任务执行时不能再设新的定时任务",
            code=ERROR_PERMISSION,
            hint="把这次任务做完即可；需要新任务请成员到时候再说",
        )
    if context.actor_user_id is None:
        raise ToolError("定时任务需要明确的群成员调用者", code=ERROR_PERMISSION)
    settings = load_settings()
    if not settings.enabled or not settings.group_open(context.group_id):
        raise ToolError("管理员没有在本群开放定时任务", code=ERROR_PERMISSION, hint="如实说明，不要假装设好了")
    is_admin = await _actor_is_admin(context)
    if settings.who_can_create == "admins" and not is_admin:
        raise ToolError("本群的定时任务只允许群主或管理员设置", code=ERROR_PERMISSION, hint="如实说明，可以请管理员来设")
    now = now_beijing()
    draft = TaskDraft(
        instruction=str(args.get("instruction") or ""),
        schedule=schedule_from_args(args),
        title=str(args.get("title") or ""),
        mode=str(args.get("mode") or "agent"),
        max_runs=args.get("max_runs"),
        mention_creator=bool(args.get("mention_me", True)),
    )
    try:
        if args.get("dry_run"):
            validated = validate_draft(draft, settings=settings, now=now)
            return ToolHandlerResult(
                {
                    "dry_run": True,
                    "schedule": validated.description,
                    "upcoming": [format_datetime(item) for item in validated.upcoming],
                    "mode": validated.mode,
                    "note": "还没有创建；成员确认后去掉 dry_run 再调用一次",
                }
            )
        task, reused, validated = await create_task(
            context.session,
            group_id=context.group_id,
            bot_id=int(str(getattr(context.bot, "self_id", "") or 0)) or None,
            creator_user_id=int(context.actor_user_id),
            creator_name=await _member_name(context.session, context.group_id, int(context.actor_user_id)),
            draft=draft,
            settings=settings,
            now=now,
        )
    except ScheduleRuleError as exc:
        raise _rule_error(exc) from exc
    except ScheduleUnavailableError as exc:
        raise _unavailable() from exc
    view = task_public_dict(task, now=now)
    view["upcoming"] = [format_datetime(item) for item in validated.upcoming[:3]]
    dbg(
        f"群 {context.group_id} 定时任务{'复用' if reused else '已创建'}: id={task.id} "
        f"kind={task.schedule_kind} mode={task.mode} next={view.get('next_run')}"
    )
    result: dict[str, Any] = {"created": not reused, "task": view}
    if reused:
        result["note"] = "同样的任务已经存在，没有重复创建"
    return ToolHandlerResult(result, mutated_db=True, needs_commit=True)


async def _list(args: dict[str, Any], context: ToolExecutionContext) -> ToolHandlerResult:
    now = now_beijing()
    try:
        if args.get("task_id"):
            task = await get_task(context.session, int(args["task_id"]), group_id=context.group_id)
            if task is None:
                raise ToolError(f"本群没有编号 {args['task_id']} 的定时任务", code=ERROR_NOT_FOUND, hint="去掉 task_id 列出全部")
            view = task_public_dict(task, now=now, preview_count=5)
            view["recent_runs"] = _history_view(task)
            return ToolHandlerResult({"task": view})
        scope = str(args.get("scope") or "mine")
        creator = int(context.actor_user_id) if scope == "mine" and context.actor_user_id is not None else None
        statuses = None if args.get("include_finished") else tuple(OPEN_STATUSES)
        limit = int(args.get("limit") or 10)
        rows, total = await list_tasks(
            context.session,
            group_id=context.group_id,
            statuses=statuses,
            creator_user_id=creator,
            limit=limit,
        )
    except ScheduleUnavailableError as exc:
        raise _unavailable() from exc
    payload: dict[str, Any] = {
        "scope": scope,
        "count": total,
        "tasks": [task_public_dict(row, now=now) for row in rows],
    }
    if total > len(rows):
        payload["truncated"] = True
    if not rows:
        payload["note"] = "没有符合条件的定时任务" + ("（只看了调用者自己的，scope=group 可看全群）" if scope == "mine" else "")
    return ToolHandlerResult(payload)


async def _update(args: dict[str, Any], context: ToolExecutionContext) -> ToolHandlerResult:
    now = now_beijing()
    action = str(args.get("action") or "edit")
    try:
        task = await _owned_task(context, int(args["task_id"]))
        if task.status not in OPEN_STATUSES and action != "edit":
            raise ToolError(
                f"任务已经是{task.status}状态，不能{action}",
                code=ERROR_INVALID_ARGS,
                hint="已结束的任务可以用 action=edit 改时间重新启用",
            )
        if action == "pause":
            if task.status == STATUS_PAUSED:
                return ToolHandlerResult({"task": task_public_dict(task, now=now), "note": "已经是暂停状态"})
            pause_task(task, now=now, reason=f"由 {context.actor_user_id} 暂停")
            await context.session.flush()
            return ToolHandlerResult({"paused": True, "task": task_public_dict(task, now=now)}, mutated_db=True, needs_commit=True)
        if action == "resume":
            if task.status == STATUS_ACTIVE:
                return ToolHandlerResult({"task": task_public_dict(task, now=now), "note": "任务本来就在运行"})
            resume_task(task, now=now)
            await context.session.flush()
            return ToolHandlerResult({"resumed": task.status == STATUS_ACTIVE, "task": task_public_dict(task, now=now, preview_count=3)}, mutated_db=True, needs_commit=True)
        settings = load_settings()
        schedule = schedule_from_args(args) if args.get("kind") else dict(task.schedule or {})
        if not args.get("kind") and any(args.get(key) not in (None, "", []) for key in (*_SCHEDULE_KEYS, "window_start", "window_end")):
            raise ToolError("改时间需要同时给出 kind 和完整的新规则", code=ERROR_INVALID_ARGS, hint='例如 kind=daily times=["09:00"]')
        draft = TaskDraft(
            instruction=str(args.get("instruction") or task.instruction),
            schedule=schedule,
            title=str(args.get("title") or (task.title if not args.get("instruction") else "")),
            mode=str(args.get("mode") or task.mode),
            max_runs=args.get("max_runs") if args.get("max_runs") is not None else task.max_runs,
            mention_creator=bool(args["mention_me"]) if "mention_me" in args else bool(task.mention_creator),
        )
        if not any(key in args for key in ("instruction", "title", "mode", "max_runs", "mention_me", "kind")):
            raise ToolError("没有要修改的内容", code=ERROR_INVALID_ARGS, hint="传要改的字段，或 action=pause/resume")
        validated = validate_draft(
            draft,
            settings=settings,
            now=now,
            anchor=task.created_at if not args.get("kind") else now,
            run_count=int(task.run_count or 0) if not args.get("kind") else 0,
        )
        if args.get("kind"):
            # 换了规则就重新计数：新规则的 max_runs 从现在起算。
            task.run_count = 0
            task.created_at = now
        apply_draft(task, validated, now=now)
        await context.session.flush()
    except ScheduleRuleError as exc:
        raise _rule_error(exc) from exc
    except ScheduleUnavailableError as exc:
        raise _unavailable() from exc
    return ToolHandlerResult(
        {"updated": True, "task": task_public_dict(task, now=now, preview_count=3)},
        mutated_db=True,
        needs_commit=True,
    )


async def _cancel(args: dict[str, Any], context: ToolExecutionContext) -> ToolHandlerResult:
    now = now_beijing()
    try:
        if args.get("all_mine"):
            if context.actor_user_id is None:
                raise ToolError("需要明确的调用者", code=ERROR_PERMISSION)
            rows, _total = await list_tasks(
                context.session,
                group_id=context.group_id,
                statuses=tuple(OPEN_STATUSES),
                creator_user_id=int(context.actor_user_id),
                limit=200,
            )
            for row in rows:
                cancel_task(row, now=now, by_user_id=context.actor_user_id, reason="成员取消全部")
            await context.session.flush()
            return ToolHandlerResult(
                {"cancelled": len(rows), "titles": [row.title for row in rows][:10]},
                mutated_db=bool(rows),
                needs_commit=bool(rows),
            )
        if not args.get("task_id"):
            raise ToolError("要给 task_id 或 all_mine=true", code=ERROR_INVALID_ARGS, hint="先用 list_scheduled_tasks 找到 task_id")
        task = await _owned_task(context, int(args["task_id"]))
        if task.status not in OPEN_STATUSES:
            return ToolHandlerResult({"cancelled": 0, "note": f"这个任务已经是 {task.status} 状态，不用再取消"})
        cancel_task(task, now=now, by_user_id=context.actor_user_id, reason="成员取消")
        await context.session.flush()
    except ScheduleUnavailableError as exc:
        raise _unavailable() from exc
    return ToolHandlerResult(
        {"cancelled": 1, "task": {"task_id": int(task.id), "title": task.title}},
        mutated_db=True,
        needs_commit=True,
    )


async def handle(name: str, args: dict[str, Any], context: ToolExecutionContext) -> ToolHandlerResult:
    if context.session is None:
        raise ToolError("定时任务需要数据库会话", code=ERROR_UNSUPPORTED)
    if name in WAKE_BLOCKED_TOOLS and in_scheduled_wake():
        raise ToolError("定时任务执行时不能再设新的定时任务", code=ERROR_PERMISSION, hint="把这次任务做完即可")
    if name == "schedule_task":
        return await _create(args, context)
    if name == "list_scheduled_tasks":
        return await _list(args, context)
    if name == "update_scheduled_task":
        return await _update(args, context)
    if name == "cancel_scheduled_task":
        return await _cancel(args, context)
    raise ValueError(f"未知定时任务工具: {name}")


__all__ = ["FAMILY", "NAMES", "handle", "schedule_from_args"]
