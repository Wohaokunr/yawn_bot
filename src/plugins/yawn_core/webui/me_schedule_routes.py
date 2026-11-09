# ruff: noqa: TID252, E501, FBT001, FBT002, C901
"""个人账号「我的定时任务」：私聊提醒 + 他在各群设的群任务一起看、一起管（前缀 ``/me/schedules``）。

权限全在服务端，与私聊工具 ``schedules`` 同一套规则（``yawn_agent.schedule_hub``）：

- “本人”只认会话里的 QQ；私聊提醒只动本人的个人数据；
- 群任务只能动自己设的；别人设的要他此刻是那个群的群主/管理员（Bot 在线时实时确认）；
- 在网页上给群设任务：他必须在那个群、群开着 Agent、定时任务对本群开放；网页上点“确定”
  本身就是确认（前端会再弹窗），所以不走私聊那套“下一句确认”。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, status
from nonebot_plugin_orm import get_session
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from sqlalchemy import select

from ..data_models.bot_group import BotGroup
from ..data_models.user_group import UserGroup
from ..yawn_agent import private_organizer as organizer
from ..yawn_agent import private_schedule as ps
from ..yawn_agent import private_store
from ..yawn_agent import schedule_hub as hub
from ..yawn_agent.context import now_beijing
from ..yawn_agent.schedule_policy import load_settings
from ..yawn_agent.schedule_store import (
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
    preview_runs,
    resume_task,
    task_mentions,
    validate_draft,
)
from ..yawn_agent.tool_args import ToolError
from .config import API_PATH
from .deps import UserReadSession, UserWriteSession, ok
from .hub import hub as change_hub
from .schedules import private_payload, task_payload
from .user_scope import _bot, managed_group_rows  # pyright: ignore[reportPrivateUsage]

router = APIRouter(prefix=f"{API_PATH}/me/schedules")
_HUB_STATUS = {"permission": 403, "not_found": 404, "quota": 429, "invalid_args": 422, "needs_reply": 409}


def _uid(session: Any) -> int:
    return int(session.user_id)


def _hub_error(exc: hub.HubError) -> HTTPException:
    detail = str(exc) + (f"（{exc.hint}）" if exc.hint else "")
    return HTTPException(_HUB_STATUS.get(exc.code, 422), detail)


def _tool_error(exc: ToolError) -> HTTPException:
    hint = getattr(exc, "hint", None)
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc) + (f"（{hint}）" if hint else ""))


def _unavailable() -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "群定时任务还没启用（管理员需要先执行数据库迁移）")


async def _my_groups(db: Any, user_id: int) -> list[dict[str, Any]]:
    """他所在的群（给“设到哪个群”下拉用）：标出定时任务开没开、他是不是管理员。"""

    settings = load_settings()
    rows = (
        await db.execute(
            select(UserGroup.group_id, UserGroup.role, BotGroup.group_name)
            .join(BotGroup, BotGroup.group_id == UserGroup.group_id)
            .where(UserGroup.user_id == user_id, UserGroup.is_active.is_(True))
            .order_by(BotGroup.last_active_at.desc().nullslast(), BotGroup.group_id)
            .limit(100)
        )
    ).all()
    return [
        {
            "groupId": str(group_id),
            "groupName": name or str(group_id),
            "role": role,
            "scheduleOpen": bool(settings.enabled and settings.group_open(int(group_id))),
            "canCreate": bool(settings.enabled and settings.group_open(int(group_id)) and (settings.who_can_create == "members" or role in ("owner", "admin"))),
        }
        for group_id, role, name in rows
    ]


# ── 列表 ────────────────────────────────────────────────────────────────


@router.get("")
async def my_schedules(
    session: UserReadSession,
    channel: Literal["all", "private", "group"] = "all",
    scope: Literal["mine", "managed"] = "mine",
    include_finished: bool = False,
) -> dict[str, Any]:
    user_id = _uid(session)
    settings = load_settings()
    async with get_session() as db:
        managed: set[int] = set()
        if scope == "managed":
            managed = {int(group.group_id) for _membership, group in await managed_group_rows(db, user_id)}
        data = await hub.user_items(db, user_id, include_finished=include_finished, managed_groups=managed, channel=channel)
        groups = await _my_groups(db, user_id)
        group_rows: dict[int, Any] = {}
        group_ids = [int(item["ref"][1:]) for item in data["items"] if item["channel"] == "group"]
        if group_ids:
            from ..data_models.agent_scheduled_task import AgentScheduledTask

            rows = (await db.execute(select(AgentScheduledTask).where(AgentScheduledTask.id.in_(group_ids)))).scalars().all()
            group_rows = {int(row.id): row for row in rows}
    names = {int(row["groupId"]): row["groupName"] for row in groups}
    record = private_store.load(user_id)
    by_id = {int(row["id"]): row for row in record.get("reminders", []) if isinstance(row, dict) and "id" in row}
    items: list[dict[str, Any]] = []
    for item in data["items"]:
        number = int(item["ref"][1:])
        if item["channel"] == "private" and number in by_id:
            items.append(private_payload(user_id, by_id[number]))
        elif item["channel"] == "group" and number in group_rows:
            row = group_rows[number]
            payload = task_payload(row, group_name=names.get(int(row.group_id)) or item["where"][2:-1])
            payload["ref"] = item["ref"]
            payload["mine"] = bool(item.get("mine"))
            payload["youManage"] = bool(item.get("you_manage_this_group"))
            items.append(payload)
    return ok(
        {
            "items": items,
            "counts": data["counts"],
            "groupReady": data["group_ready"],
            "groups": groups,
            "policy": {
                "crossCreate": settings.cross_create,
                "privateAgentMode": settings.private_agent_mode,
                "allowAgentMode": settings.allow_agent_mode,
                "privateMinInterval": settings.private_min_interval_minutes,
                "groupMinInterval": settings.min_interval_minutes,
            },
        }
    )


@router.get("/{ref}")
async def my_schedule_detail(ref: str, session: UserReadSession) -> dict[str, Any]:
    user_id = _uid(session)
    try:
        channel, number = hub.parse_ref(ref)
    except hub.HubError as exc:
        raise _hub_error(exc) from exc
    if channel == "private":
        item = private_store.find(private_store.load(user_id), "reminders", number)
        if item is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "没有这条提醒")
        return ok(private_payload(user_id, item, detail=True))
    try:
        async with get_session() as db:
            task = await _readable_task(db, user_id, number)
            names = await hub.group_names(db, {int(task.group_id)})
            payload = task_payload(task, group_name=names.get(int(task.group_id)), detail=True)
    except ScheduleUnavailableError as exc:
        raise _unavailable() from exc
    payload["ref"] = f"g{number}"
    payload["mine"] = int(task.creator_user_id) == user_id
    return ok(payload)


async def _readable_task(db: Any, user_id: int, task_id: int) -> Any:
    from ..yawn_agent.schedule_store import get_task

    task = await get_task(db, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "没有这个群任务")
    if int(task.creator_user_id) == user_id:
        return task
    membership = await db.get(UserGroup, (int(task.group_id), user_id))
    if membership is None or membership.role not in ("owner", "admin"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "没有这个群任务")
    return task


# ── 新建 / 预览 ─────────────────────────────────────────────────────────


class MyScheduleBody(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    target: Literal["private", "group"] = "private"
    group_id: str | None = Field(default=None, max_length=12)
    content: str = Field(min_length=1, max_length=600)
    title: str = Field(default="", max_length=40)
    mode: Literal["notify", "agent"] = "notify"
    schedule: dict[str, Any]
    max_runs: int | None = Field(default=None, ge=1, le=10000)
    mention_me: bool = True
    # 群任务到点 @全体成员（要他是群主/管理员）/ @这些群成员（QQ 号字符串）。
    mention_all: bool | None = None
    mention_user_ids: list[str] | None = Field(default=None, max_length=50)


def _mention_ids(raw: list[str] | None) -> list[int] | None:
    if raw is None:
        return None
    result: list[int] = []
    for item in raw:
        text = str(item).strip()
        if not text.isdigit() or int(text) <= 0:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"要 @ 的成员必须是 QQ 号：{text}")
        result.append(int(text))
    return result


class PreviewBody(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    target: Literal["private", "group"] = "private"
    schedule: dict[str, Any]
    max_runs: int | None = Field(default=None, ge=1, le=10000)
    mode: Literal["notify", "agent"] = "notify"


def _group_id(raw: str | None) -> int:
    text = str(raw or "").strip()
    if not text.isdigit() or not 4 <= len(text) <= 12:  # noqa: PLR2004
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "要选一个群")
    return int(text)


@router.post("/preview")
async def my_schedule_preview(body: PreviewBody, _session: UserReadSession) -> dict[str, Any]:
    now = now_beijing()
    if body.target == "group":
        try:
            preview = hub.preview_group_draft(TaskDraft(instruction="预览", schedule=body.schedule, mode=body.mode, max_runs=body.max_runs), now)
        except hub.HubError as exc:
            return ok({"valid": False, "error": str(exc), "hint": exc.hint or None})
        return ok({"valid": True, "description": preview["rule"], "upcoming": preview["next_times"]})
    try:
        _schedule, first, fires, rule = organizer._reminder_rule({"schedule": body.schedule, "max_runs": body.max_runs}, now)  # pyright: ignore[reportPrivateUsage]
    except ToolError as exc:
        return ok({"valid": False, "error": str(exc), "hint": getattr(exc, "hint", None)})
    return ok({"valid": True, "description": rule, "upcoming": [ps.fmt(item) for item in (fires or [first])]})


@router.post("")
async def my_schedule_create(body: MyScheduleBody, session: UserWriteSession) -> dict[str, Any]:
    user_id = _uid(session)
    now = now_beijing()
    if body.target == "private":
        args: dict[str, Any] = {"action": "add", "content": body.content, "schedule": body.schedule, "mode": body.mode}
        if body.title:
            args["title"] = body.title
        if body.max_runs:
            args["max_runs"] = body.max_runs
        try:
            result = organizer.reminders(user_id, args, now, origin={"kind": "webui"})
        except ToolError as exc:
            raise _tool_error(exc) from exc
        await change_hub.notify_change("schedules")
        item = private_store.find(private_store.load(user_id), "reminders", int(result["scheduled"]["id"]))
        return ok({"created": not result.get("reused"), "item": private_payload(user_id, item or {"id": result["scheduled"]["id"]}, detail=True) if item else result})
    group_id = _group_id(body.group_id)
    bot = _bot()
    try:
        async with get_session() as db:
            member = await db.get(UserGroup, (group_id, user_id))
            await hub.check_mention_all(bot, user_id, group_id, wanted=body.mention_all)
            task, reused = await hub.create_group_task(
                db,
                bot=bot,
                user_id=user_id,
                group_id=group_id,
                creator_name=str(getattr(member, "group_nickname", "") or "") or str(user_id),
                draft=TaskDraft(
                    instruction=body.content,
                    schedule=body.schedule,
                    title=body.title,
                    mode=body.mode,
                    max_runs=body.max_runs,
                    mention_creator=body.mention_me,
                    mention_all=body.mention_all,
                    mention_user_ids=_mention_ids(body.mention_user_ids),
                ),
                now=now,
                source_kind="webui_user",
            )
            await db.commit()
            await db.refresh(task)
            payload = task_payload(task, detail=True)
    except hub.HubError as exc:
        raise _hub_error(exc) from exc
    except ScheduleUnavailableError as exc:
        raise _unavailable() from exc
    _audit(group_id, user_id, "schedule_create", f"g{payload['id']} {body.content}")
    await change_hub.notify_change("schedules")
    payload["ref"] = f"g{payload['id']}"
    return ok({"created": not reused, "item": payload})


def _audit(group_id: int, user_id: int, action: str, detail: str) -> None:
    try:
        from ..yawn_agent.group_manage import audit

        audit(group_id, actor_id=user_id, actor_role="member", source="webui", action=action, result="success", detail=detail[:200])
    except Exception:  # noqa: BLE001
        pass


# ── 修改 / 停 / 恢复 / 取消 ─────────────────────────────────────────────


class EditBody(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    content: str | None = Field(default=None, min_length=1, max_length=600)
    title: str | None = Field(default=None, max_length=40)
    mode: Literal["notify", "agent"] | None = None
    schedule: dict[str, Any] | None = None
    max_runs: int | None = Field(default=None, ge=1, le=10000)
    mention_me: bool | None = None
    mention_all: bool | None = None
    mention_user_ids: list[str] | None = Field(default=None, max_length=50)
    revision: int | None = None


@router.patch("/{ref}")
async def my_schedule_edit(ref: str, body: EditBody, session: UserWriteSession) -> dict[str, Any]:
    user_id = _uid(session)
    now = now_beijing()
    try:
        channel, number = hub.parse_ref(ref)
    except hub.HubError as exc:
        raise _hub_error(exc) from exc
    if channel == "private":
        args: dict[str, Any] = {"action": "edit", "id": number}
        for key in ("content", "title", "mode", "max_runs"):
            value = getattr(body, key)
            if value:
                args[key] = value
        if body.schedule:
            args["schedule"] = body.schedule
            args["kind"] = body.schedule.get("kind")
        try:
            organizer.reminders(user_id, args, now)
        except ToolError as exc:
            raise _tool_error(exc) from exc
        await change_hub.notify_change("schedules")
        item = private_store.find(private_store.load(user_id), "reminders", number)
        return ok(private_payload(user_id, item, detail=True) if item else {})
    try:
        async with get_session() as db:
            task = await hub.owned_group_task(db, _bot(), user_id, number)
            if body.revision is not None and int(task.revision or 0) != body.revision:
                raise HTTPException(status.HTTP_409_CONFLICT, "任务刚被调度器或别人改过，请刷新后再保存")
            rule_changed = body.schedule is not None and dict(task.schedule or {}) != body.schedule
            had_all, old_members = task_mentions(task)
            await hub.check_mention_all(_bot(), user_id, int(task.group_id), wanted=body.mention_all, had=had_all)
            mentions_given = body.mention_all is not None or body.mention_user_ids is not None
            validated = validate_draft(
                TaskDraft(
                    instruction=body.content or task.instruction,
                    schedule=body.schedule if body.schedule is not None else dict(task.schedule or {}),
                    title=body.title if body.title is not None else task.title,
                    mode=body.mode or task.mode,
                    max_runs=body.max_runs if body.max_runs is not None else task.max_runs,
                    mention_creator=body.mention_me if body.mention_me is not None else bool(task.mention_creator),
                    mention_all=(body.mention_all if body.mention_all is not None else had_all) if mentions_given else None,
                    mention_user_ids=(_mention_ids(body.mention_user_ids) if body.mention_user_ids is not None else old_members) if mentions_given else None,
                ),
                settings=load_settings(),
                now=now,
                anchor=now if rule_changed else task.created_at,
                run_count=0 if rule_changed else int(task.run_count or 0),
            )
            await check_mention_members(db, int(task.group_id), validated.mention_targets)
            if rule_changed:
                task.run_count = 0
                task.created_at = now
            apply_draft(task, validated, now=now, restart=rule_changed)
            await db.commit()
            await db.refresh(task)
            payload = task_payload(task, detail=True)
    except hub.HubError as exc:
        raise _hub_error(exc) from exc
    except ScheduleRuleError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{exc}" + (f"（{exc.hint}）" if exc.hint else "")) from exc
    except ScheduleUnavailableError as exc:
        raise _unavailable() from exc
    _audit(int(payload["groupId"]), user_id, "schedule_edit", f"g{number}")
    await change_hub.notify_change("schedules")
    payload["ref"] = f"g{number}"
    return ok(payload)


@router.post("/{ref}/{action}")
async def my_schedule_action(ref: str, action: Literal["pause", "resume", "cancel"], session: UserWriteSession) -> dict[str, Any]:
    user_id = _uid(session)
    now = now_beijing()
    try:
        channel, number = hub.parse_ref(ref)
    except hub.HubError as exc:
        raise _hub_error(exc) from exc
    if channel == "private":
        try:
            result = organizer.reminders(user_id, {"action": action, "id": number}, now)
        except ToolError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        await change_hub.notify_change("schedules")
        return ok({"changed": True, "result": result})
    try:
        async with get_session() as db:
            task = await hub.owned_group_task(db, _bot(), user_id, number)
            changed = False
            if action == "pause" and task.status == STATUS_ACTIVE:
                pause_task(task, now=now, reason=f"由 {user_id} 在网页上暂停")
                changed = True
            elif action == "resume" and task.status == STATUS_PAUSED:
                resume_task(task, now=now)
                changed = True
            elif action == "cancel" and task.status in OPEN_STATUSES:
                cancel_task(task, now=now, by_user_id=user_id, reason="在网页上取消")
                changed = True
            await db.commit()
            await db.refresh(task)
            payload = task_payload(task, detail=True)
            payload["upcoming"] = [item.isoformat(timespec="minutes") for item in preview_runs(task, count=5)]
    except hub.HubError as exc:
        raise _hub_error(exc) from exc
    except ScheduleUnavailableError as exc:
        raise _unavailable() from exc
    if changed:
        _audit(int(payload["groupId"]), user_id, f"schedule_{action}", f"g{number}")
        await change_hub.notify_change("schedules")
    payload["ref"] = f"g{number}"
    return ok({"changed": changed, "item": payload})


__all__ = ["router"]
