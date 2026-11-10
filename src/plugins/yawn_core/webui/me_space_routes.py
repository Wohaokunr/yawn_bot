# ruff: noqa: TID252, FAST002, TRY003
"""个人账号「我的空间」2.0：今日概览、小本本（待办/笔记/倒数日/心情）、私聊设置、
主动提议、后台小助手（前缀 ``/me``）。

和私聊里让 Agent 做是同一份个人数据（``yawn_agent.private_store``）、同一套校验
（``private_organizer.clean_text``：限长、去控制字符、拒绝像指令的内容），所以网页上记的
笔记 Bot 在私聊里立刻看得到，反过来也一样。

- “本人”只认会话里的 QQ，请求里没有可以指定别人的参数。
- 只改他自己的个人数据：不碰群、不碰别人；写完通知 ``user_private_agent``，
  管理台的用户抽屉跟着刷新。
- 主动提议只改他自己的偏好（开关/间隔/暂停/静音某一类）；全站策略仍由管理员定。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from nonebot_plugin_orm import get_session
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from .. import affinity
from ..checkin_service import checkin_overview
from ..private_chat_bridge import (
    auto_paused,
    get_private_handler,
    private_agent_config,
    set_auto_paused,
    turns_today,
)
from ..yawn_agent import private_offers as offers
from ..yawn_agent import private_organizer as organizer
from ..yawn_agent import private_store as store
from ..yawn_agent.context import now_beijing
from ..yawn_agent.private_offer_policy import kind_catalog
from ..yawn_agent.private_offer_policy import load_settings as load_offer_settings
from ..yawn_agent.tool_args import ToolError
from .config import API_PATH
from .deps import UserReadSession, UserWriteSession, ok
from .hub import hub

router = APIRouter(prefix=f"{API_PATH}/me")

_ERROR_STATUS = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "quota": status.HTTP_429_TOO_MANY_REQUESTS,
    "permission": status.HTTP_403_FORBIDDEN,
}

# 主动提议的记录是写给管理员看的第三人称，到了本人页面换成“你”。
_OUTCOME_SELF = {
    "accepted": "你答应了",
    "declined": "你说不用",
    "later": "你说晚点再说",
    "replied": "你回了话",
    "ignored": "你没回",
}
_STATUS_SELF = {
    "sent": "问了你",
    "skipped": "看了一圈没问",
    "user_spoke": "正要问时你先说话了",
    "failed": "没发出去",
    "no_candidates": "没找到值得问的事",
}


class _Body(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )


def _uid(session: Any) -> int:
    return int(session.user_id)


def _tool_error(exc: ToolError) -> HTTPException:
    hint = getattr(exc, "hint", None)
    code = _ERROR_STATUS.get(
        getattr(exc, "code", ""), status.HTTP_422_UNPROCESSABLE_CONTENT
    )
    return HTTPException(code, str(exc) + (f"（{hint}）" if hint else ""))


def _run(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except ToolError as exc:
        raise _tool_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


async def _changed(user_id: int) -> None:
    await hub.notify_change("user_private_agent", str(user_id))


def _parse_day(raw: Any) -> date | None:
    try:
        return organizer.parse_day(str(raw)) if raw else None
    except (ToolError, ValueError):
        return None


# ── 今日概览 ────────────────────────────────────────────────────────────


def _todo_row(item: dict[str, Any], today: date) -> dict[str, Any]:
    due = _parse_day(item.get("due"))
    left = (due - today).days if due is not None and not item.get("done") else None
    return {
        "id": int(item["id"]),
        "content": str(item.get("content") or ""),
        "priority": str(item.get("priority") or "normal"),
        "priorityLabel": organizer.PRIORITY_LABELS.get(
            str(item.get("priority") or "normal"), "普通"
        ),
        "done": bool(item.get("done")),
        "due": item.get("due"),
        "dueInDays": left,
        "overdue": left is not None and left < 0,
        "createdAt": item.get("at"),
        "doneAt": item.get("done_at"),
    }


def _countdown_row(item: dict[str, Any], today: date) -> dict[str, Any]:
    target = _parse_day(item.get("date"))
    days = (target - today).days if target is not None else 0
    return {
        "id": int(item["id"]),
        "title": str(item.get("title") or ""),
        "date": item.get("date"),
        "daysLeft": days,
        "status": "today" if days == 0 else ("past" if days < 0 else "upcoming"),
        "notify": bool(item.get("notify", True)),
    }


def _mood_rows(record: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for index, item in enumerate(record.get("moods") or []):
        score = int(item.get("score") or 3)
        rows.append(
            {
                "index": index,
                "at": item.get("at"),
                "score": score,
                "label": organizer.MOOD_SCORES.get(score, "一般"),
                "note": item.get("note") or "",
            }
        )
    return rows


def _mood_summary(
    rows: list[dict[str, Any]], now: datetime, days: int
) -> dict[str, Any]:
    since = (now - timedelta(days=days)).strftime(organizer.TIME_FMT)
    recent = [row for row in rows if str(row["at"] or "") >= since]
    if not recent:
        return {
            "days": days,
            "count": 0,
            "average": None,
            "averageLabel": None,
            "trend": None,
        }
    avg = sum(row["score"] for row in recent) / len(recent)
    trend = "flat"
    half = len(recent) // 2
    if half:
        first = sum(row["score"] for row in recent[:half]) / half
        second = sum(row["score"] for row in recent[half:]) / (len(recent) - half)
        if second - first >= 0.5:  # noqa: PLR2004
            trend = "up"
        elif first - second >= 0.5:  # noqa: PLR2004
            trend = "down"
    return {
        "days": days,
        "count": len(recent),
        "average": round(avg, 1),
        "averageLabel": organizer.MOOD_SCORES.get(round(avg), "一般"),
        "trend": trend,
    }


def _next_reminders(record: dict[str, Any], limit: int = 3) -> list[dict[str, Any]]:
    rows = [
        item
        for item in record.get("reminders") or []
        if not item.get("paused")
        and (not item.get("done") or item.get("retry"))
        and item.get("due")
    ]
    rows.sort(key=lambda item: str(item.get("due")))
    return [
        {
            "ref": f"p{item['id']}",
            "content": str(item.get("content") or ""),
            "due": item.get("due"),
        }
        for item in rows[:limit]
    ]


@router.get("/dashboard")
async def my_dashboard(session: UserReadSession) -> dict[str, Any]:
    """主页“今天”一栏：待办、提醒、倒数日、心情、好感、签到、私聊状态、后台任务。"""

    user_id = _uid(session)
    now = now_beijing()
    today = now.date()
    record = store.load(user_id)
    todos = [_todo_row(item, today) for item in record.get("todos") or []]
    pending = [row for row in todos if not row["done"]]
    pending.sort(
        key=lambda row: (
            row["due"] or "9999-99-99",
            {"high": 0, "normal": 1, "low": 2}.get(row["priority"], 1),
            row["id"],
        )
    )
    countdowns = sorted(
        (_countdown_row(item, today) for item in record.get("countdowns") or []),
        key=lambda row: (row["daysLeft"] < 0, abs(row["daysLeft"])),
    )
    moods = _mood_rows(record)
    tasks = record.get("tasks") or []
    running = [
        task for task in tasks if task.get("status") in {"queued", "running", "blocked"}
    ]
    async with get_session() as db:
        snap = await affinity.snapshot(db, user_id)
        checkins = await checkin_overview(db, user_id, now=now)
        turns = await turns_today(db, user_id, now)
    level = snap.level
    return ok(
        {
            "now": now.strftime(organizer.TIME_FMT),
            "todos": {
                "pending": len(pending),
                "overdue": sum(1 for row in pending if row["overdue"]),
                "dueToday": sum(1 for row in pending if row["dueInDays"] == 0),
                "top": pending[:4],
            },
            "reminders": _next_reminders(record),
            "countdowns": [row for row in countdowns if row["daysLeft"] >= 0][:3],
            "mood": {
                "last": moods[-1] if moods else None,
                "week": _mood_summary(moods, now, 7),
                "loggedToday": bool(moods)
                and str(moods[-1]["at"] or "").startswith(today.isoformat()),
            },
            "affinity": {
                "available": snap.available,
                "level": level.name,
                "nextLevel": level.next_name,
                "progress": level.progress(snap.points),
                # snapshot.today 是 {来源: 今天已得}，概览只要合计
                "today": sum(snap.today.values()),
            },
            "checkin": {
                "groups": len(checkins),
                "checkedToday": sum(1 for item in checkins if item.checked_today),
                "bestStreak": max((item.streak_days for item in checkins), default=0),
                "pendingGroups": [
                    {
                        "groupId": str(item.group_id),
                        "groupName": item.group_name,
                        "streakDays": item.streak_days,
                    }
                    for item in checkins
                    if not item.checked_today
                ][:5],
            },
            "chat": {
                "enabled": bool(private_agent_config.private_agent_enabled),
                "loaded": get_private_handler() is not None,
                "paused": auto_paused(user_id),
                "turnsToday": turns,
                "dailyTurns": int(private_agent_config.private_agent_daily_turns),
            },
            "tasks": {
                "running": len(running),
                "waitingYou": sum(
                    1 for task in running if task.get("status") == "blocked"
                ),
                "total": len(tasks),
            },
            "counts": store.counts(record),
        }
    )


# ── 小本本：待办 ─────────────────────────────────────────────────────────


class TodoBody(_Body):
    content: str = Field(min_length=1, max_length=200)
    priority: Literal["high", "normal", "low"] = "normal"
    due: str | None = Field(default=None, max_length=10)


class TodoEditBody(_Body):
    content: str | None = Field(default=None, min_length=1, max_length=200)
    priority: Literal["high", "normal", "low"] | None = None
    # "" 表示清掉截止日期
    due: str | None = Field(default=None, max_length=10)
    done: bool | None = None


def _todos_payload(record: dict[str, Any], today: date) -> dict[str, Any]:
    rows = [_todo_row(item, today) for item in record.get("todos") or []]
    rows.sort(
        key=lambda row: (
            row["done"],
            row["due"] or "9999-99-99",
            {"high": 0, "normal": 1, "low": 2}.get(row["priority"], 1),
            -row["id"],
        )
    )
    return {
        "items": rows,
        "pending": sum(1 for row in rows if not row["done"]),
        "done": sum(1 for row in rows if row["done"]),
        "limit": store.LIMITS["todos"],
    }


@router.get("/assistant/todos")
async def my_todos(session: UserReadSession) -> dict[str, Any]:
    now = now_beijing()
    return ok(_todos_payload(store.load(_uid(session)), now.date()))


@router.post("/assistant/todos")
async def add_my_todo(body: TodoBody, session: UserWriteSession) -> dict[str, Any]:
    user_id = _uid(session)
    now = now_beijing()
    args: dict[str, Any] = {
        "action": "add",
        "content": body.content,
        "priority": body.priority,
    }
    if body.due:
        args["due"] = body.due
    result = _run(organizer.todos, user_id, args, now)
    await _changed(user_id)
    return ok(
        {
            "added": result["added"]["id"],
            **_todos_payload(store.load(user_id), now.date()),
        }
    )


@router.patch("/assistant/todos/{todo_id}")
async def edit_my_todo(
    todo_id: int, body: TodoEditBody, session: UserWriteSession
) -> dict[str, Any]:
    user_id = _uid(session)
    now = now_beijing()

    def apply(record: dict[str, Any]) -> None:
        item = store.find(record, "todos", todo_id)
        if item is None:
            raise ToolError(f"没有编号 {todo_id} 的待办", code="not_found")
        if body.content is not None:
            item["content"] = organizer.clean_text(body.content, limit=120)
        if body.priority is not None:
            item["priority"] = body.priority
        if body.due is not None:
            if body.due == "":
                item.pop("due", None)
            else:
                item["due"] = organizer.parse_day(body.due).isoformat()
        if body.done is not None:
            item["done"] = body.done
            if body.done:
                item["done_at"] = organizer.fmt(now)
            else:
                item.pop("done_at", None)

    _run(store.update, user_id, apply)
    await _changed(user_id)
    return ok(_todos_payload(store.load(user_id), now.date()))


@router.delete("/assistant/todos/{todo_id}")
async def delete_my_todo(todo_id: int, session: UserWriteSession) -> dict[str, Any]:
    user_id = _uid(session)
    now = now_beijing()
    _run(organizer.todos, user_id, {"action": "delete", "id": todo_id}, now)
    await _changed(user_id)
    return ok(_todos_payload(store.load(user_id), now.date()))


@router.post("/assistant/todos/clear-done")
async def clear_my_done_todos(session: UserWriteSession) -> dict[str, Any]:
    user_id = _uid(session)
    now = now_beijing()
    result = _run(organizer.todos, user_id, {"action": "clear_done"}, now)
    if result.get("cleared"):
        # 成就里“完成过多少待办”不该因为清理而倒退

        def remember(record: dict[str, Any]) -> None:
            web = record.setdefault("web", {})
            web["todos_cleared"] = int(web.get("todos_cleared") or 0) + int(
                result["cleared"]
            )

        store.update(user_id, remember)
    await _changed(user_id)
    return ok(
        {
            "cleared": result["cleared"],
            **_todos_payload(store.load(user_id), now.date()),
        }
    )


# ── 小本本：笔记 ─────────────────────────────────────────────────────────


class NoteBody(_Body):
    content: str = Field(min_length=1, max_length=400)
    tag: str | None = Field(default=None, max_length=20)


class NoteEditBody(_Body):
    content: str | None = Field(default=None, min_length=1, max_length=400)
    # "" 表示去掉标签
    tag: str | None = Field(default=None, max_length=20)
    pinned: bool | None = None


def _notes_payload(record: dict[str, Any]) -> dict[str, Any]:
    items = sorted(
        record.get("notes") or [], key=lambda n: (not n.get("pinned"), -int(n["id"]))
    )
    return {
        "items": [
            {
                "id": int(item["id"]),
                "content": str(item.get("content") or ""),
                "tag": item.get("tag") or None,
                "pinned": bool(item.get("pinned")),
                "at": item.get("at"),
                "editedAt": item.get("edited_at"),
            }
            for item in items
        ],
        "tags": sorted({str(item["tag"]) for item in items if item.get("tag")}),
        "limit": store.LIMITS["notes"],
    }


@router.get("/assistant/notes")
async def my_notes(session: UserReadSession) -> dict[str, Any]:
    return ok(_notes_payload(store.load(_uid(session))))


@router.post("/assistant/notes")
async def add_my_note(body: NoteBody, session: UserWriteSession) -> dict[str, Any]:
    user_id = _uid(session)
    args: dict[str, Any] = {"action": "add", "content": body.content}
    if body.tag and body.tag.strip():
        args["tag"] = body.tag.strip()
    result = _run(organizer.notes, user_id, args, now_beijing())
    await _changed(user_id)
    return ok({"added": result["saved"]["id"], **_notes_payload(store.load(user_id))})


@router.patch("/assistant/notes/{note_id}")
async def edit_my_note(
    note_id: int, body: NoteEditBody, session: UserWriteSession
) -> dict[str, Any]:
    user_id = _uid(session)
    now = now_beijing()

    def apply(record: dict[str, Any]) -> None:
        item = store.find(record, "notes", note_id)
        if item is None:
            raise ToolError(f"没有编号 {note_id} 的笔记", code="not_found")
        if body.content is not None:
            item["content"] = organizer.clean_text(body.content, limit=300)
            item["edited_at"] = organizer.fmt(now)
        if body.tag is not None:
            if body.tag.strip():
                item["tag"] = organizer.clean_text(body.tag, limit=12, field="标签")
            else:
                item.pop("tag", None)
        if body.pinned is not None:
            item["pinned"] = body.pinned

    _run(store.update, user_id, apply)
    await _changed(user_id)
    return ok(_notes_payload(store.load(user_id)))


@router.delete("/assistant/notes/{note_id}")
async def delete_my_note(note_id: int, session: UserWriteSession) -> dict[str, Any]:
    user_id = _uid(session)
    _run(organizer.notes, user_id, {"action": "delete", "id": note_id}, now_beijing())
    await _changed(user_id)
    return ok(_notes_payload(store.load(user_id)))


# ── 小本本：倒数日 ───────────────────────────────────────────────────────


class CountdownBody(_Body):
    title: str = Field(min_length=1, max_length=60)
    date: str = Field(min_length=8, max_length=10)
    notify: bool = True


class CountdownEditBody(_Body):
    title: str | None = Field(default=None, min_length=1, max_length=60)
    date: str | None = Field(default=None, min_length=8, max_length=10)
    notify: bool | None = None


def _countdowns_payload(record: dict[str, Any], today: date) -> dict[str, Any]:
    rows = [_countdown_row(item, today) for item in record.get("countdowns") or []]
    rows.sort(key=lambda row: (row["daysLeft"] < 0, abs(row["daysLeft"])))
    return {"items": rows, "limit": store.LIMITS["countdowns"]}


@router.get("/assistant/countdowns")
async def my_countdowns(session: UserReadSession) -> dict[str, Any]:
    return ok(_countdowns_payload(store.load(_uid(session)), now_beijing().date()))


@router.post("/assistant/countdowns")
async def add_my_countdown(
    body: CountdownBody, session: UserWriteSession
) -> dict[str, Any]:
    user_id = _uid(session)
    now = now_beijing()
    result = _run(
        organizer.countdowns,
        user_id,
        {
            "action": "add",
            "title": body.title,
            "date": body.date,
            "notify": body.notify,
        },
        now,
    )
    await _changed(user_id)
    return ok(
        {
            "added": result["added"]["id"],
            **_countdowns_payload(store.load(user_id), now.date()),
        }
    )


@router.patch("/assistant/countdowns/{countdown_id}")
async def edit_my_countdown(
    countdown_id: int, body: CountdownEditBody, session: UserWriteSession
) -> dict[str, Any]:
    user_id = _uid(session)
    now = now_beijing()

    def apply(record: dict[str, Any]) -> None:
        item = store.find(record, "countdowns", countdown_id)
        if item is None:
            raise ToolError(f"没有编号 {countdown_id} 的倒数日", code="not_found")
        if body.title is not None:
            item["title"] = organizer.clean_text(body.title, limit=40, field="名称")
        if body.date is not None:
            item["date"] = organizer.parse_day(body.date).isoformat()
        if body.notify is not None:
            item["notify"] = body.notify

    _run(store.update, user_id, apply)
    await _changed(user_id)
    return ok(_countdowns_payload(store.load(user_id), now.date()))


@router.delete("/assistant/countdowns/{countdown_id}")
async def delete_my_countdown(
    countdown_id: int, session: UserWriteSession
) -> dict[str, Any]:
    user_id = _uid(session)
    now = now_beijing()
    _run(organizer.countdowns, user_id, {"action": "delete", "id": countdown_id}, now)
    await _changed(user_id)
    return ok(_countdowns_payload(store.load(user_id), now.date()))


# ── 小本本：心情 ─────────────────────────────────────────────────────────


class MoodBody(_Body):
    score: int = Field(ge=1, le=5)
    note: str | None = Field(default=None, max_length=80)


def _moods_payload(record: dict[str, Any], now: datetime) -> dict[str, Any]:
    rows = _mood_rows(record)
    return {
        "items": list(reversed(rows)),
        "week": _mood_summary(rows, now, 7),
        "month": _mood_summary(rows, now, 30),
        "scores": {str(key): value for key, value in organizer.MOOD_SCORES.items()},
        "limit": store.LIMITS["moods"],
    }


@router.get("/assistant/moods")
async def my_moods(session: UserReadSession) -> dict[str, Any]:
    return ok(_moods_payload(store.load(_uid(session)), now_beijing()))


@router.post("/assistant/moods")
async def log_my_mood(body: MoodBody, session: UserWriteSession) -> dict[str, Any]:
    user_id = _uid(session)
    now = now_beijing()
    args: dict[str, Any] = {"action": "log", "score": body.score}
    if body.note and body.note.strip():
        args["note"] = body.note.strip()
    _run(organizer.mood, user_id, args, now)
    await _changed(user_id)
    return ok(_moods_payload(store.load(user_id), now))


@router.delete("/assistant/moods/{index}")
async def delete_my_mood(
    index: int,
    session: UserWriteSession,
    at: str = Query(min_length=1, max_length=16),
) -> dict[str, Any]:
    """删一条心情记录；``at`` 用来核对，防止列表在别处变了以后删错。"""

    user_id = _uid(session)

    def apply(record: dict[str, Any]) -> None:
        items = record["moods"]
        if not 0 <= index < len(items) or str(items[index].get("at")) != at:
            raise ToolError("这条心情记录已经变了，刷新后再试", code="not_found")
        items.pop(index)

    _run(store.update, user_id, apply)
    await _changed(user_id)
    return ok(_moods_payload(store.load(user_id), now_beijing()))


# ── 私聊设置 ────────────────────────────────────────────────────────────


class ChatPrefsBody(_Body):
    reply_length: Literal["short", "normal", "long"] | None = None
    emoji: Literal["none", "some", "lots"] | None = None
    # 下面几项给 "" 表示关掉/清空
    call_me: str | None = Field(default=None, max_length=20)
    quiet_start: str | None = Field(default=None, max_length=5)
    quiet_end: str | None = Field(default=None, max_length=5)
    morning_greeting: str | None = Field(default=None, max_length=5)
    checkin_nudge: str | None = Field(default=None, max_length=5)
    birthday: str | None = Field(default=None, max_length=5)
    proactive: bool | None = None


class PauseBody(_Body):
    paused: bool


def _prefs_payload(prefs: dict[str, Any]) -> dict[str, Any]:
    return {
        "replyLength": prefs.get("reply_length", "normal"),
        "emoji": prefs.get("emoji", "some"),
        "callMe": prefs.get("call_me") or "",
        "quietStart": prefs.get("quiet_start") or "",
        "quietEnd": prefs.get("quiet_end") or "",
        "morningGreeting": prefs.get("morning_greeting") or "",
        "checkinNudge": prefs.get("checkin_nudge") or "",
        "birthday": prefs.get("birthday") or "",
        "proactive": bool(prefs.get("proactive", True)),
    }


async def _chat_payload(user_id: int) -> dict[str, Any]:
    now = now_beijing()
    prefs = store.load(user_id)["prefs"]
    cfg = private_agent_config
    async with get_session() as db:
        turns = await turns_today(db, user_id, now)
    return {
        "prefs": _prefs_payload(prefs),
        "inQuietHours": organizer.in_quiet_hours(prefs, now),
        "agent": {
            "enabled": bool(cfg.private_agent_enabled),
            "loaded": get_private_handler() is not None,
            "autoReply": bool(cfg.private_agent_auto_reply),
            "friendsOnly": bool(cfg.private_agent_friends_only),
            "paused": auto_paused(user_id),
            "turnsToday": turns,
            "dailyTurns": int(cfg.private_agent_daily_turns),
            "proactiveAllowed": bool(getattr(cfg, "private_agent_proactive", True)),
        },
    }


@router.get("/chat")
async def my_chat_settings(session: UserReadSession) -> dict[str, Any]:
    return ok(await _chat_payload(_uid(session)))


@router.patch("/chat/prefs")
async def patch_my_chat_prefs(
    body: ChatPrefsBody, session: UserWriteSession
) -> dict[str, Any]:
    """和私聊里说“以后叫我 xx / 晚上 11 点后别打扰”是同一份偏好（``chat_prefs``）。"""

    user_id = _uid(session)
    args: dict[str, Any] = {"action": "set"}
    values = body.model_dump(exclude_none=True)
    for key, value in values.items():
        args[key] = "off" if value == "" else value
    if len(args) == 1:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "没有要改的设置")
    _run(organizer.chat_prefs, user_id, args, now_beijing())
    await _changed(user_id)
    return ok(await _chat_payload(user_id))


@router.post("/chat/pause")
async def pause_my_chat(body: PauseBody, session: UserWriteSession) -> dict[str, Any]:
    """暂停/恢复 Bot 在私聊里自动回复你（和私聊发 /退出、/对话 等价）。"""

    user_id = _uid(session)
    set_auto_paused(user_id, paused=body.paused)
    await _changed(user_id)
    return ok(await _chat_payload(user_id))


# ── 主动提议 ────────────────────────────────────────────────────────────


class OffersBody(_Body):
    # True/False = 自己选开或关；None 不动。follow_default=True 回到跟随默认。
    enabled: bool | None = None
    follow_default: bool = False
    every_hours: float | None = Field(default=None, ge=0.5, le=48)
    reset_interval: bool = False
    snooze_hours: float | None = Field(default=None, ge=0, le=24 * 30)
    mute_kind: str | None = Field(default=None, max_length=40)
    mute_days: int | None = Field(default=None, ge=0, le=365)
    unmute_kind: str | None = Field(default=None, max_length=40)


def _offers_payload(user_id: int) -> dict[str, Any]:
    settings = load_offer_settings()
    now = now_beijing()
    record = store.load(user_id)
    view = offers.prefs_view(record, settings, now)
    muted = offers.muted_kinds(record, now)
    log = offers.peek(record)["log"][-20:]
    return {
        "siteEnabled": bool(settings.enabled),
        "siteOpen": settings.user_open(user_id) is None,
        "defaultOn": bool(settings.default_on),
        "on": bool(view["on"]),
        "ownChoice": bool(view["own_choice"]),
        "everyHours": float(view["every_hours"]),
        "defaultEveryHours": float(settings.interval_hours),
        "minEveryHours": float(settings.user_min_interval_hours),
        "pausedUntil": view.get("paused_until"),
        "dailyMax": int(settings.daily_max),
        "siteQuiet": f"{settings.quiet_start}-{settings.quiet_end}"
        if settings.quiet_hours_enabled
        else None,
        "kinds": [
            {
                **item,
                "siteOn": settings.kind_on(item["kind"]),
                "mutedUntil": (
                    None
                    if item["kind"] not in muted
                    else (
                        "forever"
                        if muted[item["kind"]] == "forever"
                        else muted[item["kind"]]
                    )
                ),
            }
            for item in kind_catalog()
        ],
        "history": [
            {
                "id": row.get("id"),
                "at": row.get("at"),
                "status": str(row.get("status") or ""),
                "statusLabel": _STATUS_SELF.get(
                    str(row.get("status")), str(row.get("status") or "")
                ),
                "about": [
                    str(item.get("what") or "")
                    for item in row.get("items", [])
                    if isinstance(item, dict)
                ],
                "outcome": row.get("outcome"),
                "outcomeLabel": _OUTCOME_SELF.get(str(row.get("outcome")))
                if row.get("outcome")
                else None,
            }
            for row in reversed(log)
            if row.get("status") in {"sent", "user_spoke"}
        ],
        "stats": offers.stats(record),
    }


@router.get("/offers")
async def my_offers(session: UserReadSession) -> dict[str, Any]:
    return ok(_offers_payload(_uid(session)))


@router.patch("/offers")
async def patch_my_offers(
    body: OffersBody, session: UserWriteSession
) -> dict[str, Any]:
    """他自己的主动提议偏好；和私聊里说“别主动问我了 / 三天内别问待办的事”一样。"""

    user_id = _uid(session)
    settings = load_offer_settings()
    now = now_beijing()

    def apply(record: dict[str, Any]) -> None:
        if body.follow_default:
            offers.set_enabled(record, None)
        elif body.enabled is not None:
            offers.set_enabled(record, body.enabled)
        if body.reset_interval:
            offers.set_interval(record, None, settings)
        elif body.every_hours is not None:
            offers.set_interval(record, body.every_hours, settings)
        if body.snooze_hours is not None:
            if body.snooze_hours <= 0:
                offers.state(record)["prefs"].pop("snooze_until", None)
            else:
                offers.snooze(record, now, body.snooze_hours)
        if body.mute_kind:
            offers.mute_kind(record, body.mute_kind, now, body.mute_days or None)
        if body.unmute_kind:
            offers.unmute_kind(record, body.unmute_kind)

    _run(store.update, user_id, apply)
    await _changed(user_id)
    return ok(_offers_payload(user_id))


# ── 后台小助手 ──────────────────────────────────────────────────────────


def _tasks_payload(user_id: int) -> dict[str, Any]:
    from .users import _helper_payload  # pyright: ignore[reportPrivateUsage]

    payload = _helper_payload(user_id)
    projects: list[dict[str, Any]] = []
    try:
        from ..yawn_agent.private_tasks import list_projects

        projects = list_projects(user_id)
    except Exception:  # noqa: BLE001 - 项目只是附加信息
        projects = []
    return {**payload, "projects": projects}


@router.get("/tasks")
async def my_tasks(session: UserReadSession) -> dict[str, Any]:
    """后台小助手任务（最近的在前）、多成员项目、我的虚拟电脑今日用量。"""

    return ok(_tasks_payload(_uid(session)))


@router.delete("/tasks/{task_id}")
async def cancel_my_task(task_id: str, session: UserWriteSession) -> dict[str, Any]:
    if not task_id.startswith("t") or not task_id[1:].isdigit() or len(task_id) > 12:  # noqa: PLR2004
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "任务编号不合法")
    try:
        from ..yawn_agent.private_tasks import cancel_task
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "私聊 Agent 没有加载"
        ) from exc
    user_id = _uid(session)
    result = _run(cancel_task, user_id, task_id)
    await _changed(user_id)
    return ok(
        {**_tasks_payload(user_id), "cancelled": result.get("status") == "已取消"}
    )


__all__ = ["router"]
