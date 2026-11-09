# ruff: noqa: E501, TRY003, PLR0913, TID252
"""私聊提醒 ↔ 群定时任务的统一视图与跨端操作。

两边的存储不变：群定时任务在数据表 ``AgentScheduledTask``（``schedule_store``），私聊
提醒在每人的个人数据 JSON（``private_store`` 的 ``reminders``，规则见 ``private_schedule``）。
两边用的是同一个规则引擎（``schedule_spec``），这里把它们投影成同一种条目：

- 统一编号 ``ref``：群任务 ``g<任务号>``，私聊提醒 ``p<提醒号>``（只在本人名下有意义）；
- 统一字段：在哪（私聊/某个群）、做什么、规则描述、下一次、状态、运行次数、在哪设的。

跨端操作与权限（硬规则）：

- 私聊里给群设任务：他必须在那个群、群开着 Agent、定时任务对本群开放、策略
  ``cross_create`` 开着；“仅管理员可建”时实时查他的身份。任务内容会到点发到群里，
  所以**必须他下一句确认**（和群管代办、私聊转告同一套“问过之后他又发来消息才算”）。
- 私聊里改/停/取消群任务：只能动自己设的；群主/管理员（实时确认）能动本群任何人的。
- 群里只看得到“在本群设的私聊提醒”（内容本来就是他在群里说的），看不到他私聊里的其他提醒。
- 私聊内容永远不进群聊提示词：私聊设的群任务只在唤醒时标“他在私聊里让我给本群设的”。
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from ..data_models.agent_scheduled_task import AgentScheduledTask
from ..data_models.bot_group import BotGroup
from . import private_schedule as ps
from . import private_store
from .log import dbg, dbg_exc
from .schedule_policy import load_settings
from .schedule_spec import format_datetime
from .schedule_store import (
    OPEN_STATUSES,
    STATUS_ACTIVE,
    ScheduleRuleError,
    ScheduleUnavailableError,
    TaskDraft,
    clip,
    create_task,
    get_task,
    list_tasks,
    validate_draft,
)
from .schedule_wake import SOURCE_LABELS

REF_RE = re.compile(r"^\s*([gGpP])\s*#?(\d{1,9})\s*$")
CHANNEL_LABELS = {"group": "群里", "private": "私聊"}
STATUS_LABELS = {
    "active": "进行中",
    "paused": "已暂停",
    "completed": "已结束",
    "cancelled": "已取消",
    "failed": "已失败",
}
MAX_PENDING_PER_USER = 3
MAX_LIST = 40


class HubError(Exception):
    def __init__(self, message: str, *, code: str = "invalid_args", hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint


def parse_ref(raw: Any) -> tuple[str, int]:
    """``g12`` / ``p3``（大小写、``#`` 都行）→ (channel, 编号)。"""

    match = REF_RE.match(str(raw or ""))
    if not match:
        raise HubError("编号要写成 g12（群任务）或 p3（私聊提醒）", hint="先用 list 看编号")
    return ("group" if match.group(1).lower() == "g" else "private"), int(match.group(2))


# ── 投影 ────────────────────────────────────────────────────────────────


def group_item(task: AgentScheduledTask, *, group_name: str | None = None, viewer: int | None = None) -> dict[str, Any]:
    status = str(task.status or "active")
    item: dict[str, Any] = {
        "ref": f"g{int(task.id)}",
        "channel": "group",
        "group_id": int(task.group_id),
        "where": f"群「{group_name or task.group_id}」",
        "title": task.title,
        "what": clip(task.instruction, 120),
        "mode": "agent" if task.mode == "agent" else "notify",
        "rule": task.description,
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "next": format_datetime(task.next_run_at) if task.next_run_at is not None and status == STATUS_ACTIVE else None,
        "runs": int(task.run_count or 0),
        "set_from": SOURCE_LABELS.get(str(task.source_kind or "agent"), "群里"),
        "creator_user_id": int(task.creator_user_id),
        "creator": task.creator_name or str(task.creator_user_id),
    }
    if task.max_runs and task.schedule_kind != "once":
        item["max_runs"] = int(task.max_runs)
    if task.last_result:
        item["last_result"] = task.last_result
    if task.status_reason:
        item["status_reason"] = task.status_reason
    if viewer is not None:
        item["mine"] = int(task.creator_user_id) == int(viewer)
    return item


def private_item(row: dict[str, Any]) -> dict[str, Any]:
    status = ps.status_of(row)
    item: dict[str, Any] = {
        "ref": f"p{int(row['id'])}",
        "channel": "private",
        "where": "私聊",
        "title": str(row.get("title") or ""),
        "what": str(row.get("content") or "")[:120],
        "mode": str(row.get("mode") or "notify"),
        "rule": ps.describe_item(row),
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "next": None if row.get("paused") or row.get("done") else row.get("due"),
        "runs": int(row.get("runs") or 0),
        "set_from": ps.origin_label(row),
        "mine": True,
    }
    if row.get("max_runs"):
        item["max_runs"] = int(row["max_runs"])
    if row.get("last_result"):
        item["last_result"] = row["last_result"]
    if isinstance(row.get("retry"), dict):
        item["retrying_at"] = row["retry"].get("at")
    origin = ps.origin_of(row)
    if origin.get("group_id"):
        item["origin_group_id"] = int(origin["group_id"])
    return item


def _sort_key(item: dict[str, Any]) -> tuple[int, str, str]:
    order = 0 if item.get("next") else (1 if item.get("status") == "paused" else 2)
    return order, str(item.get("next") or "9999"), str(item.get("ref"))


async def group_names(session: Any, group_ids: set[int]) -> dict[int, str]:
    if not group_ids or session is None:
        return {}
    try:
        rows = await session.execute(select(BotGroup.group_id, BotGroup.group_name).where(BotGroup.group_id.in_(group_ids)))
    except Exception:  # noqa: BLE001
        return {}
    return {int(group_id): str(name) for group_id, name in rows if name}


def private_items(user_id: int, *, include_finished: bool = False) -> list[dict[str, Any]]:
    record = private_store.load(user_id)
    rows = [row for row in record.get("reminders", []) if isinstance(row, dict) and "id" in row]
    if not include_finished:
        rows = [row for row in rows if not row.get("done") or row.get("retry")]
    return [private_item(row) for row in rows]


async def user_items(
    session: Any,
    user_id: int,
    *,
    include_finished: bool = False,
    managed_groups: set[int] | None = None,
    channel: str = "all",
    group_id: int | None = None,
) -> dict[str, Any]:
    """他名下的全部定时事项：私聊提醒 + 他在各群设的群任务（+ 他管的群里别人设的）。"""

    items: list[dict[str, Any]] = []
    if channel in {"all", "private"} and group_id is None:
        items.extend(private_items(user_id, include_finished=include_finished))
    group_ready = True
    if channel in {"all", "group"}:
        statuses = None if include_finished else tuple(OPEN_STATUSES)
        try:
            rows, _total = await list_tasks(session, statuses=statuses, creator_user_id=int(user_id), group_id=group_id, limit=200)
            extra: list[AgentScheduledTask] = []
            for managed in sorted(managed_groups or set()):
                if group_id is not None and managed != group_id:
                    continue
                others, _ = await list_tasks(session, group_id=managed, statuses=statuses, limit=100)
                extra.extend(row for row in others if int(row.creator_user_id) != int(user_id))
        except ScheduleUnavailableError:
            rows, extra, group_ready = [], [], False
        names = await group_names(session, {int(row.group_id) for row in [*rows, *extra]})
        items.extend(group_item(row, group_name=names.get(int(row.group_id)), viewer=user_id) for row in rows)
        for row in extra:
            item = group_item(row, group_name=names.get(int(row.group_id)), viewer=user_id)
            item["you_manage_this_group"] = True
            items.append(item)
    items.sort(key=_sort_key)
    counts = {
        "private": sum(1 for item in items if item["channel"] == "private"),
        "group": sum(1 for item in items if item["channel"] == "group"),
        "active": sum(1 for item in items if item["status"] == "active"),
        "paused": sum(1 for item in items if item["status"] == "paused"),
    }
    return {"items": items, "counts": counts, "group_ready": group_ready}


async def soon_digest(session: Any, user_id: int, now: datetime, *, hours: int = 24, limit: int = 3) -> list[str]:
    """私聊本轮资料用：他在各群设的、接下来 ``hours`` 小时内要跑的群任务（私聊提醒另有摘要）。"""

    try:
        rows = (
            await session.execute(
                select(AgentScheduledTask)
                .where(
                    AgentScheduledTask.creator_user_id == int(user_id),
                    AgentScheduledTask.status == STATUS_ACTIVE,
                    AgentScheduledTask.next_run_at.is_not(None),
                    AgentScheduledTask.next_run_at <= now + timedelta(hours=hours),
                )
                .order_by(AgentScheduledTask.next_run_at.asc())
                .limit(limit)
            )
        ).scalars().all()
    except Exception:  # noqa: BLE001
        return []
    names = await group_names(session, {int(row.group_id) for row in rows})
    return [
        f"{format_datetime(row.next_run_at)[5:]} 群「{names.get(int(row.group_id), row.group_id)}」{row.title}（g{row.id}）"
        for row in rows
        if row.next_run_at is not None
    ]


def group_private_items(user_id: int, group_id: int, *, include_finished: bool = False) -> list[dict[str, Any]]:
    """群里能看到的私聊提醒：只有在**本群**让我设的（内容本来就是他在群里说的）。"""

    return [
        item
        for item in private_items(user_id, include_finished=include_finished)
        if int(item.get("origin_group_id") or 0) == int(group_id)
    ]


# ── 权限 ────────────────────────────────────────────────────────────────


async def is_live_manager(bot: Any, group_id: int, user_id: int) -> bool:
    if bot is None:
        return False
    try:
        from .group_manage import MANAGER_ROLES, live_role

        return (await live_role(bot, group_id, user_id)) in MANAGER_ROLES
    except Exception:  # noqa: BLE001
        dbg_exc(f"群 {group_id} 定时任务：实时确认 {user_id} 的身份失败")
        return False


async def owned_group_task(session: Any, bot: Any, user_id: int, task_id: int, *, allowed_groups: set[int] | None = None) -> AgentScheduledTask:
    """私聊/网页里要动的群任务：自己设的，或他此刻是群主/管理员的群里的。"""

    task = await get_task(session, task_id)
    if task is None or (allowed_groups is not None and int(task.group_id) not in allowed_groups):
        raise HubError(f"没有编号 g{task_id} 的群定时任务", code="not_found", hint="先用 list 看编号")
    if int(task.creator_user_id) == int(user_id):
        return task
    if await is_live_manager(bot, int(task.group_id), int(user_id)):
        return task
    raise HubError(
        "这个群任务不是他设的，只有设任务的人或群主/管理员能改",
        code="permission",
        hint="照实告诉他，可以请设任务的人或管理员来改",
    )


async def check_group_create(session: Any, bot: Any, user_id: int, group_id: int, *, source: str = "private") -> None:
    """私聊/网页给群设任务前的检查（不含限额，限额在 ``create_task``）。"""

    from ..data_models.user_group import UserGroup
    from .config_store import agent_runtime_enabled

    settings = load_settings()
    if not settings.enabled or not settings.group_open(group_id):
        raise HubError("管理员没有在这个群开放定时任务", code="permission", hint="照实告诉他")
    if source == "private" and not settings.cross_create:
        raise HubError("管理员没开放在私聊里给群设定时任务", code="permission", hint="可以去群里直接让我设")
    member = await session.get(UserGroup, (int(group_id), int(user_id)))
    if member is None or not bool(getattr(member, "is_active", True)):
        raise HubError("他不在这个群里", code="permission", hint="只能给自己所在的群设")
    if not await agent_runtime_enabled(session, int(group_id)):
        raise HubError("这个群没开着我的 Agent，到点没法执行", code="permission", hint="照实告诉他")
    if settings.who_can_create == "admins" and not await is_live_manager(bot, int(group_id), int(user_id)):
        raise HubError("这个群的定时任务只允许群主或管理员设", code="permission", hint="照实告诉他，可以请管理员来设")


async def check_mention_all(bot: Any, user_id: int, group_id: int, *, wanted: bool | None, had: bool = False) -> None:
    """私聊/网页给群任务新加 @全体成员 前查权限（原来就有的不再查，别人改时间不被挡）。"""

    if not wanted or had:
        return
    from .schedule_policy import mention_all_denied

    is_manager = await is_live_manager(bot, int(group_id), int(user_id))
    reason = mention_all_denied(load_settings(), actor_is_admin=bool(is_manager))
    if reason:
        raise HubError(reason, code="permission", hint="去掉 @全体成员，或改成 @ 具体的成员")


async def create_group_task(
    session: Any,
    *,
    bot: Any,
    user_id: int,
    group_id: int,
    creator_name: str,
    draft: TaskDraft,
    now: datetime,
    source_kind: str = "private",
) -> tuple[AgentScheduledTask, bool]:
    """真正建群任务（确认之后）；只 flush，提交由调用方做。"""

    await check_group_create(session, bot, user_id, group_id, source="private" if source_kind == "private" else "webui")
    bot_id = None
    try:
        bot_id = int(str(getattr(bot, "self_id", "") or 0)) or None
    except (TypeError, ValueError):
        bot_id = None
    try:
        task, reused, _validated = await create_task(
            session,
            group_id=int(group_id),
            bot_id=bot_id,
            creator_user_id=int(user_id),
            creator_name=creator_name,
            draft=draft,
            settings=load_settings(),
            now=now,
            source_kind=source_kind,
        )
    except ScheduleRuleError as exc:
        raise HubError(str(exc), code="quota" if exc.code in {"group_limit", "member_limit"} else "invalid_args", hint=exc.hint) from exc
    return task, reused


def preview_group_draft(draft: TaskDraft, now: datetime) -> dict[str, Any]:
    try:
        validated = validate_draft(draft, settings=load_settings(), now=now)
    except ScheduleRuleError as exc:
        raise HubError(str(exc), code="invalid_args", hint=exc.hint) from exc
    return {
        "rule": validated.description,
        "first": format_datetime(validated.first_run),
        "next_times": [format_datetime(item) for item in validated.upcoming[:5]],
        "mode": validated.mode,
        "title": validated.title,
    }


# ── 私聊给群设任务的待确认 ─────────────────────────────────────────────


@dataclass(slots=True)
class PendingGroupTask:
    id: str
    user_id: int
    group_id: int
    group_name: str
    kind: str  # create / edit
    draft: dict[str, Any]
    summary: str
    arrival: int
    created: float
    expires: float
    task_id: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


_pending: dict[str, PendingGroupTask] = {}


def _arrivals(user_id: int) -> int:
    try:
        from ..chat_state import arrival_count

        return int(arrival_count(user_id))
    except Exception:  # noqa: BLE001
        return 0


def confirm_seconds() -> int:
    try:
        from ..private_chat_bridge import private_agent_config

        return int(getattr(private_agent_config, "group_manage_confirm_seconds", 300) or 300)
    except Exception:  # noqa: BLE001
        return 300


def _gc() -> None:
    now = time.monotonic()
    for key in [key for key, item in _pending.items() if item.expires <= now]:
        _pending.pop(key, None)


def create_pending(
    user_id: int,
    *,
    group_id: int,
    group_name: str,
    kind: str,
    draft: dict[str, Any],
    summary: str,
    task_id: int | None = None,
) -> tuple[PendingGroupTask, bool]:
    """返回（待确认, 是否沿用了同一件事）；同一件事沿用原来的计数，避免“要设吗？”“设”循环。"""

    _gc()
    for item in _pending.values():
        if (
            item.user_id == int(user_id)
            and item.group_id == int(group_id)
            and item.kind == kind
            and item.task_id == task_id
            and item.draft == draft
        ):
            return item, True
    mine = sorted((item for item in _pending.values() if item.user_id == int(user_id)), key=lambda item: item.created)
    for stale in mine[: max(0, len(mine) - MAX_PENDING_PER_USER + 1)]:
        _pending.pop(stale.id, None)
    pending_id = "s" + secrets.token_hex(2)
    while pending_id in _pending:
        pending_id = "s" + secrets.token_hex(2)
    now = time.monotonic()
    item = PendingGroupTask(
        id=pending_id,
        user_id=int(user_id),
        group_id=int(group_id),
        group_name=group_name,
        kind=kind,
        draft=dict(draft),
        summary=summary,
        arrival=_arrivals(user_id),
        created=now,
        expires=now + confirm_seconds(),
        task_id=task_id,
    )
    _pending[pending_id] = item
    return item, False


def replied_since(item: PendingGroupTask) -> bool:
    return _arrivals(item.user_id) > item.arrival


def pending_for(user_id: int) -> list[PendingGroupTask]:
    _gc()
    return sorted((item for item in _pending.values() if item.user_id == int(user_id)), key=lambda item: item.created, reverse=True)


def take_pending(user_id: int, pending_id: str | None) -> PendingGroupTask:
    """取出要执行的待确认；没等到他回话时抛 ``needs_reply``（不弹出）。"""

    mine = pending_for(user_id)
    raw = str(pending_id or "").strip()
    item = next((row for row in mine if row.id == raw), None)
    if item is None and not raw and len(mine) == 1:
        item = mine[0]
    if item is None:
        if mine:
            listing = "；".join(f"{row.id} {row.summary}" for row in mine)
            raise HubError("没有这个待确认的群任务", code="not_found", hint=f"还在等他确认的是：{listing}")
        raise HubError("没有待确认的群任务（可能已过期或已设好）", code="not_found", hint="需要的话重新发起")
    if not replied_since(item):
        raise HubError("还没等到他的确认", code="needs_reply", hint="把群名、时间和内容问他，等他回复同意后再 confirm")
    _pending.pop(item.id, None)
    return item


def cancel_pending(user_id: int, pending_id: str | None = None) -> int:
    gone = [row for row in pending_for(user_id) if not pending_id or row.id == pending_id]
    for row in gone:
        _pending.pop(row.id, None)
    return len(gone)


def pending_view(user_id: int) -> list[dict[str, Any]]:
    now = time.monotonic()
    return [
        {
            "confirm_id": item.id,
            "summary": item.summary,
            "asked_seconds_ago": int(max(0.0, now - item.created)),
            "he_replied_since": replied_since(item),
        }
        for item in pending_for(user_id)[:3]
    ]


def reset_for_tests() -> None:
    _pending.clear()


# ── 群任务被自动暂停时私聊告诉设任务的人 ───────────────────────────────


async def notify_creator_paused(group_id: int, task_id: int, title: str, creator_user_id: int, reason: str) -> bool:
    """策略 ``notify_creator_on_pause`` 开着、他是好友且没 /退出 时，私聊说一声。"""

    settings = load_settings()
    if not settings.notify_creator_on_pause:
        return False
    try:
        from nonebot import get_bots

        from . import agent_bridge as bridge

        if bridge.can_message_privately(int(creator_user_id)):
            return False
        bots = [bot for bot in get_bots().values() if bot is not None]
        if not bots or not await bridge.is_friend(bots[0], int(creator_user_id)):
            return False
        group_name = str(group_id)
        try:
            from nonebot_plugin_orm import get_session

            async with get_session() as session:
                row = await session.get(BotGroup, int(group_id))
                if row is not None and row.group_name:
                    group_name = str(row.group_name)
        except Exception:  # noqa: BLE001
            dbg_exc("读群名失败(忽略)")
        text = (
            f"跟你说一声：你在群「{group_name}」设的定时任务「{title}」（g{task_id}）{reason}。"
            "想继续的话跟我说“恢复 g" + str(task_id) + "”，不要了就说“取消 g" + str(task_id) + "”。"
        )
        await bots[0].send_private_msg(user_id=int(creator_user_id), message=text)
        from .context import now_beijing
        from .private_scheduler import record_outgoing

        try:
            await record_outgoing(int(creator_user_id), text, now_beijing())
        except Exception:  # noqa: BLE001
            dbg_exc("暂停通知入库失败(忽略)")
    except Exception:  # noqa: BLE001
        dbg_exc(f"群 {group_id} 定时任务 {task_id} 暂停通知发送失败(忽略)")
        return False
    dbg(f"群 {group_id} 定时任务 {task_id} 自动暂停，已私聊告诉 {creator_user_id}")
    return True


__all__ = [
    "CHANNEL_LABELS",
    "STATUS_LABELS",
    "HubError",
    "PendingGroupTask",
    "cancel_pending",
    "check_group_create",
    "create_group_task",
    "create_pending",
    "group_item",
    "group_names",
    "group_private_items",
    "is_live_manager",
    "notify_creator_paused",
    "owned_group_task",
    "parse_ref",
    "pending_for",
    "pending_view",
    "preview_group_draft",
    "private_item",
    "private_items",
    "replied_since",
    "reset_for_tests",
    "soon_digest",
    "take_pending",
    "user_items",
]
