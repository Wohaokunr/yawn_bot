# ruff: noqa: TID252, FAST002, E501, PLR0913, PLR0917, PLR2004, TC003, PERF401
"""「定时任务」管理页的后端：任务列表/详情、新建与编辑、暂停恢复取消、立即执行、
规则预览、运行记录与策略。

任务与运行逻辑都在 ``yawn_agent.schedule_store`` / ``schedule_runner``，这里只做
camelCase 投影和参数校验。全部接口仅管理员可用：任务内容可能包含群成员信息。
表还没迁移时只读接口返回 ``ready: false`` 的空数据，写接口返回 503，不影响其它页面。
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from nonebot_plugin_orm import get_session
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel
from sqlalchemy import func, select

from ..data_models.agent_scheduled_task import AgentScheduledTask
from ..data_models.bot_group import BotGroup
from ..data_models.user_group import UserGroup
from ..yawn_agent import private_schedule as ps
from ..yawn_agent import private_store
from ..yawn_agent.context import now_beijing
from ..yawn_agent.schedule_policy import (
    ScheduleSettings,
    load_settings,
    save_settings,
    usage_snapshot,
)
from ..yawn_agent.schedule_runner import RESULT_LABELS, execute_task
from ..yawn_agent.schedule_spec import KIND_LABELS
from ..yawn_agent.schedule_store import (
    OPEN_STATUSES,
    STATUSES,
    ScheduleRuleError,
    ScheduleUnavailableError,
    TaskDraft,
    apply_draft,
    cancel_task,
    create_task,
    get_task,
    list_tasks,
    pause_task,
    preview_runs,
    resume_task,
    validate_draft,
)
from ..yawn_agent.schedule_wake import SOURCE_LABELS
from .config import API_PATH
from .deps import AdminReadSession, AdminWriteSession, ok
from .hub import hub

router = APIRouter(prefix=f"{API_PATH}/schedules")
_RUNNING: set[asyncio.Task[Any]] = set()
_STATUS_PATTERN = "^(?:" + "|".join(STATUSES) + "|open)$"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="minutes") if value is not None else None


async def _group_names(group_ids: set[int]) -> dict[int, str]:
    if not group_ids:
        return {}
    with contextlib.suppress(Exception):
        async with get_session() as session:
            rows = await session.execute(
                select(BotGroup.group_id, BotGroup.group_name).where(BotGroup.group_id.in_(group_ids))
            )
            return {int(group_id): str(name) for group_id, name in rows if name}
    return {}


def task_payload(task: AgentScheduledTask, *, group_name: str | None = None, detail: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": int(task.id),
        "groupId": str(task.group_id),
        "groupName": group_name,
        "botId": str(task.bot_id) if task.bot_id else None,
        "creatorUserId": str(task.creator_user_id),
        "creatorName": task.creator_name or None,
        "title": task.title,
        "instruction": task.instruction,
        "mode": task.mode,
        "kind": task.schedule_kind,
        "kindLabel": KIND_LABELS.get(task.schedule_kind, task.schedule_kind),
        "description": task.description,
        "status": task.status,
        "statusReason": task.status_reason or None,
        "mentionCreator": bool(task.mention_creator),
        "maxRuns": task.max_runs,
        "runCount": int(task.run_count or 0),
        "failureCount": int(task.failure_count or 0),
        "skipCount": int(task.skip_count or 0),
        "nextRunAt": _iso(task.next_run_at) if task.status == "active" else None,
        "lastRunAt": _iso(task.last_run_at),
        "lastResult": task.last_result or None,
        "lastResultLabel": RESULT_LABELS.get(task.last_result or "", task.last_result or None),
        "lastError": task.last_error or None,
        "lastOutput": task.last_output or None,
        "lastTraceId": task.last_trace_id,
        "sourceKind": task.source_kind,
        "sourceLabel": SOURCE_LABELS.get(str(task.source_kind or "agent"), task.source_kind),
        "channel": "group",
        "revision": int(task.revision or 0),
        "createdAt": _iso(task.created_at),
        "updatedAt": _iso(task.updated_at),
    }
    if detail:
        data["schedule"] = dict(task.schedule or {})
        data["upcoming"] = [_iso(item) for item in preview_runs(task, count=8)]
        data["history"] = [
            {**item, "label": RESULT_LABELS.get(str(item.get("result") or ""), item.get("result"))}
            for item in list(task.history or [])
            if isinstance(item, dict)
        ]
        data["cancelledAt"] = _iso(task.cancelled_at)
        data["cancelledBy"] = str(task.cancelled_by) if task.cancelled_by else None
    return data


def _unavailable() -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "定时任务表还没有迁移：请先执行 nb orm upgrade")


def _rule_error(exc: ScheduleRuleError) -> HTTPException:
    detail = f"{exc}" + (f"（{exc.hint}）" if exc.hint else "")
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail)


def _group_id(value: str | None) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    if not text.isdigit() or not 4 <= len(text) <= 12:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "群号不合法")
    return int(text)


# ── 私聊提醒（每人的个人数据 JSON）──────────────────────────────────────────


def _private_rows() -> list[tuple[int, dict[str, Any], dict[str, Any]]]:
    """(QQ, 提醒, 整份个人数据)；扫描所有有个人数据的用户。"""

    rows: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for user_id in private_store.known_user_ids():
        record = private_store.load(user_id)
        for item in record.get("reminders", []):
            if isinstance(item, dict) and "id" in item:
                rows.append((user_id, item, record))
    return rows


def _private_counts() -> dict[str, Any]:
    counts = {"active": 0, "paused": 0, "agent": 0, "fromGroups": 0, "users": 0}
    users: set[int] = set()
    with contextlib.suppress(Exception):
        for user_id, item, _record in _private_rows():
            if item.get("done") and not item.get("retry"):
                continue
            users.add(user_id)
            counts["paused" if item.get("paused") else "active"] += 1
            if str(item.get("mode") or "notify") == "agent":
                counts["agent"] += 1
            origin = ps.origin_of(item)
            if origin.get("kind") == "group":
                counts["fromGroups"] += 1
    counts["users"] = len(users)
    return counts


def private_payload(user_id: int, item: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    origin = ps.origin_of(item)
    status_value = ps.status_of(item)
    data: dict[str, Any] = {
        "key": f"{user_id}:{item['id']}",
        "ref": f"p{item['id']}",
        "channel": "private",
        "userId": str(user_id),
        "itemId": int(item["id"]),
        "title": str(item.get("title") or ""),
        "content": str(item.get("content") or ""),
        "mode": str(item.get("mode") or "notify"),
        "kindLabel": ps.kind_label(item),
        "description": ps.describe_item(item),
        "status": status_value,
        "nextRunAt": None if item.get("paused") or item.get("done") else item.get("due"),
        "runCount": int(item.get("runs") or 0),
        "maxRuns": item.get("max_runs"),
        "lastResult": item.get("last_result"),
        "lastResultLabel": ps.RESULT_LABELS.get(str(item.get("last_result") or ""), item.get("last_result")),
        "lastRunAt": item.get("last_run"),
        "retryAt": (item.get("retry") or {}).get("at") if isinstance(item.get("retry"), dict) else None,
        "sourceKind": str(origin.get("kind") or "private"),
        "sourceLabel": ps.origin_label(item),
        "originGroupId": str(origin["group_id"]) if origin.get("group_id") else None,
        "createdAt": item.get("created"),
    }
    if detail:
        data["schedule"] = item.get("schedule") or ps.legacy_schedule(ps.parse(item.get("due")) or now_beijing(), str(item.get("repeat") or "none"))
        data["history"] = [
            {**row, "label": ps.RESULT_LABELS.get(str(row.get("result") or ""), row.get("result"))}
            for row in list(item.get("history") or [])
            if isinstance(row, dict)
        ]
    return data


@router.get("/private")
async def schedule_private_list(
    _session: AdminReadSession,
    user: str | None = Query(default=None, max_length=12),
    status_filter: Literal["active", "paused", "open", "all"] = Query(default="open", alias="status"),
    mode: Literal["agent", "notify"] | None = None,
    source: Literal["private", "group", "webui"] | None = None,
    q: str = Query(default="", max_length=60),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    """所有人的私聊提醒（管理员排查用；内容是他们自己设的提醒原话）。"""

    needle = q.strip().casefold()
    items: list[dict[str, Any]] = []
    for user_id, item, _record in _private_rows():
        if user and user.isdigit() and int(user) != user_id:
            continue
        payload = private_payload(user_id, item)
        if status_filter == "open" and item.get("done") and not item.get("retry"):
            continue
        if status_filter in {"active", "paused"} and payload["status"] != status_filter:
            continue
        if mode and payload["mode"] != mode:
            continue
        if source and payload["sourceKind"] != source:
            continue
        if needle and needle not in f"{payload['content']} {payload['title']} {user_id}".casefold():
            continue
        items.append(payload)
    items.sort(key=lambda row: (row["nextRunAt"] is None, str(row["nextRunAt"] or ""), row["key"]))
    return ok({"items": items[:limit], "total": len(items), "counts": _private_counts()})


@router.get("/private/{user_id}/{item_id}")
async def schedule_private_detail(user_id: int, item_id: int, _session: AdminReadSession) -> dict[str, Any]:
    item = private_store.find(private_store.load(user_id), "reminders", item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "私聊提醒不存在")
    return ok(private_payload(user_id, item, detail=True))


@router.post("/private/{user_id}/{item_id}/{action}")
async def schedule_private_action(
    user_id: int,
    item_id: int,
    action: Literal["pause", "resume", "cancel"],
    _session: AdminWriteSession,
) -> dict[str, Any]:
    """管理员暂停/恢复/删除某人的私聊提醒（例如设错了、刷屏）。"""

    now = now_beijing()

    def run(record: dict[str, Any]) -> dict[str, Any] | None:
        item = private_store.find(record, "reminders", item_id)
        if item is None:
            return None
        if action == "pause":
            ps.pause(item)
        elif action == "resume":
            if ps.resume(item, now) is None:
                record["reminders"].remove(item)
                return {"removed": True}
        else:
            record["reminders"].remove(item)
            return {"removed": True}
        return private_payload(user_id, item, detail=True)

    result = private_store.update(user_id, run)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "私聊提醒不存在")
    await hub.notify_change("schedules")
    return ok({"changed": True, "item": result})


# ── 状态与列表 ──────────────────────────────────────────────────────────────


@router.get("/status")
async def schedule_status(_session: AdminReadSession) -> dict[str, Any]:
    settings = load_settings()
    usage = usage_snapshot()
    now = now_beijing()
    payload: dict[str, Any] = {
        "ready": True,
        "enabled": settings.enabled,
        "now": _iso(now),
        "counts": dict.fromkeys(STATUSES, 0),
        "today": {
            "day": usage.get("day"),
            "runs": sum(int(value) for value in usage.get("groups", {}).values()),
            "results": usage.get("results", {}),
        },
        "upcoming": [],
        "groups": 0,
        "private": _private_counts(),
    }
    try:
        async with get_session() as session:
            rows = await session.execute(
                select(AgentScheduledTask.status, func.count()).group_by(AgentScheduledTask.status)
            )
            for key, value in rows:
                payload["counts"][str(key)] = int(value)
            payload["groups"] = int(
                await session.scalar(
                    select(func.count(func.distinct(AgentScheduledTask.group_id))).where(
                        AgentScheduledTask.status.in_(tuple(OPEN_STATUSES))
                    )
                )
                or 0
            )
            soon = (
                await session.execute(
                    select(AgentScheduledTask)
                    .where(AgentScheduledTask.status == "active", AgentScheduledTask.next_run_at.is_not(None))
                    .order_by(AgentScheduledTask.next_run_at.asc())
                    .limit(6)
                )
            ).scalars().all()
    except Exception as exc:
        from ..yawn_agent.schedule_store import is_missing_table

        if not is_missing_table(exc):
            raise
        payload["ready"] = False
        return ok(payload)
    names = await _group_names({int(task.group_id) for task in soon})
    payload["upcoming"] = [task_payload(task, group_name=names.get(int(task.group_id))) for task in soon]
    return ok(payload)


@router.get("/tasks")
async def schedule_tasks(
    _session: AdminReadSession,
    group: str | None = Query(default=None, max_length=12),
    status_filter: str | None = Query(default=None, alias="status", pattern=_STATUS_PATTERN),
    mode: Literal["agent", "notify"] | None = None,
    creator: str | None = Query(default=None, max_length=12),
    q: str = Query(default="", max_length=60),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100, alias="pageSize"),
) -> dict[str, Any]:
    statuses: tuple[str, ...] | None = None
    if status_filter == "open":
        statuses = tuple(OPEN_STATUSES)
    elif status_filter:
        statuses = (status_filter,)
    creator_id = int(creator) if creator and creator.isdigit() else None
    try:
        async with get_session() as session:
            rows, total = await list_tasks(
                session,
                group_id=_group_id(group),
                statuses=statuses,
                creator_user_id=creator_id,
                query=q,
                mode=mode,
                limit=page_size,
                offset=(page - 1) * page_size,
            )
    except ScheduleUnavailableError:
        return ok({"ready": False, "items": [], "total": 0})
    names = await _group_names({int(row.group_id) for row in rows})
    return ok(
        {
            "ready": True,
            "items": [task_payload(row, group_name=names.get(int(row.group_id))) for row in rows],
            "total": total,
            "page": page,
            "pageSize": page_size,
        }
    )


@router.get("/tasks/{task_id}")
async def schedule_task_detail(task_id: int, _session: AdminReadSession) -> dict[str, Any]:
    try:
        async with get_session() as session:
            task = await get_task(session, task_id)
    except ScheduleUnavailableError as exc:
        raise _unavailable() from exc
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "定时任务不存在")
    names = await _group_names({int(task.group_id)})
    return ok(task_payload(task, group_name=names.get(int(task.group_id)), detail=True))


@router.get("/runs")
async def schedule_runs(
    _session: AdminReadSession,
    group: str | None = Query(default=None, max_length=12),
    result: str | None = Query(default=None, max_length=24),
    channel: Literal["all", "group", "private"] = "all",
    limit: int = Query(default=100, ge=1, le=300),
) -> dict[str, Any]:
    """把各任务的运行历史摊平成一条时间线（新的在前）；私聊提醒的运行也并进来。"""

    private_runs = _private_runs(result) if channel in {"all", "private"} and not group else []
    if channel == "private":
        private_runs.sort(key=lambda item: str(item.get("at") or ""), reverse=True)
        return ok({"ready": True, "items": private_runs[:limit]})
    try:
        async with get_session() as session:
            rows, _total = await list_tasks(session, group_id=_group_id(group), limit=200)
    except ScheduleUnavailableError:
        return ok({"ready": False, "items": private_runs[:limit]})
    names = await _group_names({int(row.group_id) for row in rows})
    items: list[dict[str, Any]] = []
    for row in rows:
        for entry in list(row.history or []):
            if not isinstance(entry, dict):
                continue
            if result and entry.get("result") != result:
                continue
            items.append(
                {
                    **entry,
                    "label": RESULT_LABELS.get(str(entry.get("result") or ""), entry.get("result")),
                    "taskId": int(row.id),
                    "title": row.title,
                    "mode": row.mode,
                    "groupId": str(row.group_id),
                    "groupName": names.get(int(row.group_id)),
                    "channel": "group",
                }
            )
    items.extend(private_runs)
    items.sort(key=lambda item: str(item.get("at") or ""), reverse=True)
    return ok({"ready": True, "items": items[:limit]})


def _private_runs(result: str | None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    with contextlib.suppress(Exception):
        for user_id in private_store.known_user_ids():
            flags = private_store.load(user_id).get("flags") or {}
            for row in list(flags.get("reminder_log") or []):
                if not isinstance(row, dict) or (result and row.get("result") != result):
                    continue
                items.append(
                    {
                        "at": row.get("at"),
                        "result": row.get("result"),
                        "label": ps.RESULT_LABELS.get(str(row.get("result") or ""), row.get("result")),
                        "taskId": None,
                        "ref": f"p{row.get('id')}",
                        "title": str(row.get("content") or ""),
                        "mode": row.get("mode") or "notify",
                        "userId": str(user_id),
                        "channel": "private",
                    }
                )
    return items


# ── 写操作 ──────────────────────────────────────────────────────────────────


class TaskBody(BaseModel):
    """新建/编辑任务；群号与 QQ 号用字符串避免 JS 精度问题。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    group_id: str | None = Field(default=None, max_length=12)
    creator_user_id: str | None = Field(default=None, max_length=12)
    title: str = Field(default="", max_length=40)
    instruction: str = Field(min_length=1, max_length=600)
    mode: Literal["agent", "notify"] = "agent"
    schedule: dict[str, Any]
    max_runs: int | None = Field(default=None, ge=1, le=10000)
    mention_creator: bool = True
    revision: int | None = None


class PreviewBody(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    schedule: dict[str, Any]
    max_runs: int | None = Field(default=None, ge=1, le=10000)
    mode: Literal["agent", "notify"] = "notify"


class BulkBody(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    ids: list[int] = Field(min_length=1, max_length=200)
    action: Literal["pause", "resume", "cancel", "delete"]


@router.post("/preview")
async def schedule_preview(body: PreviewBody, _session: AdminReadSession) -> dict[str, Any]:
    """校验规则并给出描述与接下来 10 次触发；不落库。"""

    now = now_beijing()
    try:
        validated = validate_draft(
            TaskDraft(instruction="预览", schedule=body.schedule, mode=body.mode, max_runs=body.max_runs),
            settings=load_settings(),
            now=now,
        )
    except ScheduleRuleError as exc:
        return ok({"valid": False, "error": str(exc), "code": exc.code, "hint": exc.hint or None})
    from ..yawn_agent.schedule_spec import upcoming

    fires = upcoming(validated.spec, now, count=10, anchor=now, remaining_runs=validated.max_runs)
    return ok(
        {
            "valid": True,
            "description": validated.description,
            "kind": validated.spec.kind,
            "schedule": validated.spec.as_dict(),
            "upcoming": [_iso(item) for item in fires],
        }
    )


@router.post("/tasks")
async def schedule_create(body: TaskBody, _session: AdminWriteSession) -> dict[str, Any]:
    group_id = _group_id(body.group_id)
    creator = _group_id(body.creator_user_id) if body.creator_user_id else None
    if group_id is None or creator is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "需要群号和“以谁的名义”（群成员 QQ 号）")
    now = now_beijing()
    try:
        async with get_session() as session:
            member = await session.get(UserGroup, (group_id, creator))
            if member is None:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "这个 QQ 号不是该群的已知成员")
            task, _reused, _validated = await create_task(
                session,
                group_id=group_id,
                bot_id=None,
                creator_user_id=creator,
                creator_name=str(member.group_nickname or "") or str(creator),
                draft=TaskDraft(
                    instruction=body.instruction,
                    schedule=body.schedule,
                    title=body.title,
                    mode=body.mode,
                    max_runs=body.max_runs,
                    mention_creator=body.mention_creator,
                ),
                settings=load_settings(),
                now=now,
                source_kind="manual",
                enforce_quota=False,
            )
            await session.commit()
            await session.refresh(task)
            payload = task_payload(task, detail=True)
    except ScheduleRuleError as exc:
        raise _rule_error(exc) from exc
    except ScheduleUnavailableError as exc:
        raise _unavailable() from exc
    await hub.notify_change("schedules")
    return ok(payload)


@router.patch("/tasks/{task_id}")
async def schedule_update(task_id: int, body: TaskBody, _session: AdminWriteSession) -> dict[str, Any]:
    now = now_beijing()
    try:
        async with get_session() as session:
            task = await get_task(session, task_id)
            if task is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "定时任务不存在")
            if body.revision is not None and int(task.revision or 0) != body.revision:
                raise HTTPException(status.HTTP_409_CONFLICT, "任务刚被调度器或别人改过，请刷新后再保存")
            rule_changed = dict(task.schedule or {}) != body.schedule
            validated = validate_draft(
                TaskDraft(
                    instruction=body.instruction,
                    schedule=body.schedule,
                    title=body.title,
                    mode=body.mode,
                    max_runs=body.max_runs,
                    mention_creator=body.mention_creator,
                ),
                settings=load_settings(),
                now=now,
                anchor=now if rule_changed else task.created_at,
                run_count=0 if rule_changed else int(task.run_count or 0),
            )
            if rule_changed:
                task.run_count = 0
                task.created_at = now
            apply_draft(task, validated, now=now, restart=rule_changed)
            await session.commit()
            await session.refresh(task)
            payload = task_payload(task, detail=True)
    except ScheduleRuleError as exc:
        raise _rule_error(exc) from exc
    except ScheduleUnavailableError as exc:
        raise _unavailable() from exc
    await hub.notify_change("schedules")
    return ok(payload)


async def _apply_action(session: Any, task: AgentScheduledTask, action: str, now: datetime) -> bool:
    if action == "pause" and task.status == "active":
        pause_task(task, now=now, reason="管理员在 WebUI 暂停")
        return True
    if action == "resume" and task.status == "paused":
        resume_task(task, now=now)
        return True
    if action == "cancel" and task.status in OPEN_STATUSES:
        cancel_task(task, now=now, by_user_id=None, reason="管理员在 WebUI 取消")
        return True
    if action == "delete":
        await session.delete(task)
        return True
    return False


@router.post("/tasks/{task_id}/{action}")
async def schedule_action(
    task_id: int,
    action: Literal["pause", "resume", "cancel", "run"],
    _session: AdminWriteSession,
) -> dict[str, Any]:
    now = now_beijing()
    if action == "run":
        try:
            async with get_session() as session:
                task = await get_task(session, task_id)
        except ScheduleUnavailableError as exc:
            raise _unavailable() from exc
        if task is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "定时任务不存在")
        # agent 模式一次可能跑几分钟：后台执行，结果写进任务历史后通过 hub 通知刷新。
        async def _run() -> None:
            try:
                await execute_task(task_id, force=True)
            finally:
                await hub.notify_change("schedules")

        job = asyncio.create_task(_run())
        _RUNNING.add(job)
        job.add_done_callback(_RUNNING.discard)
        return ok({"started": True})
    try:
        async with get_session() as session:
            task = await get_task(session, task_id)
            if task is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "定时任务不存在")
            changed = await _apply_action(session, task, action, now)
            await session.commit()
            await session.refresh(task)
            payload = task_payload(task, detail=True)
    except ScheduleUnavailableError as exc:
        raise _unavailable() from exc
    if changed:
        await hub.notify_change("schedules")
    return ok({"changed": changed, "task": payload})


@router.delete("/tasks/{task_id}")
async def schedule_delete(task_id: int, _session: AdminWriteSession) -> dict[str, Any]:
    try:
        async with get_session() as session:
            task = await get_task(session, task_id)
            if task is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "定时任务不存在")
            await session.delete(task)
            await session.commit()
    except ScheduleUnavailableError as exc:
        raise _unavailable() from exc
    await hub.notify_change("schedules")
    return ok({"deleted": True})


@router.post("/bulk")
async def schedule_bulk(body: BulkBody, _session: AdminWriteSession) -> dict[str, Any]:
    now = now_beijing()
    changed = 0
    try:
        async with get_session() as session:
            for task_id in dict.fromkeys(body.ids):
                task = await get_task(session, task_id)
                if task is not None and await _apply_action(session, task, body.action, now):
                    changed += 1
            await session.commit()
    except ScheduleUnavailableError as exc:
        raise _unavailable() from exc
    if changed:
        await hub.notify_change("schedules")
    return ok({"changed": changed})


# ── 策略 ────────────────────────────────────────────────────────────────────


class SettingsBody(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    enabled: bool
    group_mode: Literal["all", "allowlist"]
    allowed_groups: list[str] = Field(default_factory=list, max_length=500)
    blocked_groups: list[str] = Field(default_factory=list, max_length=500)
    who_can_create: Literal["members", "admins"]
    allow_agent_mode: bool
    max_active_per_group: int
    max_active_per_member: int
    min_interval_minutes: int
    max_horizon_days: int
    group_daily_runs: int
    misfire_grace_minutes: int
    missed_policy: Literal["skip", "run_once"]
    catch_up_max_hours: int
    max_consecutive_failures: int
    quiet_hours_enabled: bool
    quiet_start: str
    quiet_end: str
    quiet_policy: Literal["defer", "skip"]
    concurrency: int
    run_timeout_seconds: int
    notify_prefix: str = ""
    history_limit: int = 20
    cross_create: bool = True
    deliver_to_private: bool = True
    notify_creator_on_pause: bool = True
    private_agent_mode: bool = True
    private_max_active: int = 30
    private_min_interval_minutes: int = 5
    private_retry_limit: int = 3


def _settings_payload(settings: ScheduleSettings) -> dict[str, Any]:
    data = {to_camel(key): value for key, value in settings.model_dump().items()}
    data["allowedGroups"] = [str(item) for item in settings.allowed_groups]
    data["blockedGroups"] = [str(item) for item in settings.blocked_groups]
    return data


def _group_list(values: list[str], field: str) -> list[int]:
    result: list[int] = []
    for raw in values:
        text = str(raw).strip()
        if not text:
            continue
        if not text.isdigit() or not 4 <= len(text) <= 12:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{field} 里有不合法的群号：{text[:20]}")
        result.append(int(text))
    return result


@router.get("/settings")
async def schedule_settings(_session: AdminReadSession) -> dict[str, Any]:
    return ok({"settings": _settings_payload(load_settings()), "defaults": _settings_payload(ScheduleSettings())})


@router.put("/settings")
async def schedule_update_settings(body: SettingsBody, _session: AdminWriteSession) -> dict[str, Any]:
    try:
        settings = ScheduleSettings.model_validate(
            {
                **body.model_dump(),
                "allowed_groups": _group_list(body.allowed_groups, "允许的群"),
                "blocked_groups": _group_list(body.blocked_groups, "禁用的群"),
            }
        )
    except ValidationError as exc:
        fields = {to_camel(str(item["loc"][0])): item["msg"] for item in exc.errors() if item["loc"]}
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "；".join(f"{key}: {msg}" for key, msg in fields.items()) or "设置不合法",
        ) from exc
    saved = save_settings(settings)
    await hub.notify_change("schedules")
    return ok({"settings": _settings_payload(saved)})


__all__ = ["private_payload", "router", "task_payload"]
