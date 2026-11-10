# ruff: noqa: TID252, E501, C901, FAST002, PLR0912, PLR0913, PLR0917, PLR2004
"""个人账号「我的空间」3.0：提醒中心、全站搜索、月历、网页签到、成就、数据洞察、
待办批量操作、清空某一类、网页偏好（前缀 ``/me``）。

和 2.0 的两个文件同一套规矩：

- “本人”只认会话里的 QQ，请求里没有可以指定别人的参数。
- 个人数据仍在 ``private_store`` 的 JSON 里，无迁移；网页偏好和成就解锁时间放在记录的
  ``web`` 键下。
- 网页签到走 ``checkin_service.perform_checkin``，和群里 /签到、私聊一键签到同一份实现，
  同样受群的 ``checkin`` 功能开关约束。
- 排名只返回“我排第几 / 一共多少人”，不带别人的 QQ 或昵称。
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from nonebot_plugin_orm import get_session
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from sqlalchemy import func, select

from .. import affinity, affinity_store
from .. import affinity_economy as economy
from .. import affinity_perks as perks
from ..affinity_policy import load_policy
from ..checkin_service import checkin_overview, perform_checkin
from ..data_models.affinity_event import AffinityEvent
from ..data_models.bot_user import BotUser
from ..data_models.chat_message import ChatMessage
from ..data_models.chat_session import ChatSession
from ..data_models.checkin_record import CheckinRecord
from ..data_models.checkin_user import CheckinUser
from ..data_models.user_group import UserGroup
from ..permission import check_feature_permission
from ..private_chat_bridge import auto_paused
from ..yawn_agent import private_memory
from ..yawn_agent import private_organizer as organizer
from ..yawn_agent import private_store as store
from ..yawn_agent.context import now_beijing
from ..yawn_agent.tool_args import ToolError
from .config import API_PATH
from .deps import UserReadSession, UserWriteSession, ok
from .hub import hub
from .me_space_routes import (
    _countdown_row,  # pyright: ignore[reportPrivateUsage]
    _mood_rows,  # pyright: ignore[reportPrivateUsage]
    _run,  # pyright: ignore[reportPrivateUsage]
    _todo_row,  # pyright: ignore[reportPrivateUsage]
    _todos_payload,  # pyright: ignore[reportPrivateUsage]
)

router = APIRouter(prefix=f"{API_PATH}/me")

MAX_WEB_CHECKIN_GROUPS = 30
_SEARCH_PER_KIND = 8
_OPEN_TASK = {"queued", "running", "blocked"}


class _Body(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )


def _uid(session: Any) -> int:
    return int(session.user_id)


async def _changed(user_id: int) -> None:
    await hub.notify_change("user_private_agent", str(user_id))


def _safe(fn: Any, default: Any) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001 - 附加信息拿不到就按没有
        return default


# ── 提醒中心 ────────────────────────────────────────────────────────────


def _notice(
    kind: str, tone: str, title: str, detail: str, to: str, count: int = 1
) -> dict[str, Any]:
    return {
        "id": kind,
        "kind": kind,
        "tone": tone,
        "title": title,
        "detail": detail,
        "to": to,
        "count": count,
    }


def build_inbox(
    *,
    record: dict[str, Any],
    now: datetime,
    checkins: list[Any],
    quests: dict[str, Any] | None,
    bag: dict[str, Any] | None,
    paused: bool,
) -> dict[str, Any]:
    """纯函数：把各处“等你处理”的事拼成一份提醒清单和侧栏角标。"""

    today = now.date()
    items: list[dict[str, Any]] = []
    todos = [
        _todo_row(item, today)
        for item in record.get("todos") or []
        if not item.get("done")
    ]
    overdue = [row for row in todos if row["overdue"]]
    due_today = [row for row in todos if row["dueInDays"] == 0]
    if overdue:
        items.append(
            _notice(
                "todo_overdue",
                "danger",
                f"{len(overdue)} 条待办逾期了",
                "、".join(row["content"] for row in overdue[:3]),
                "/me/assistant",
                len(overdue),
            )
        )
    if due_today:
        items.append(
            _notice(
                "todo_today",
                "warning",
                f"{len(due_today)} 条待办今天到期",
                "、".join(row["content"] for row in due_today[:3]),
                "/me/assistant",
                len(due_today),
            )
        )
    tasks = record.get("tasks") or []
    blocked = [task for task in tasks if task.get("status") == "blocked"]
    if blocked:
        items.append(
            _notice(
                "task_blocked",
                "warning",
                f"{len(blocked)} 个后台任务在等你回答",
                "在私聊里直接回复 Bot 就行",
                "/me/tasks",
                len(blocked),
            )
        )
    countdowns = [
        _countdown_row(item, today) for item in record.get("countdowns") or []
    ]
    for row in countdowns:
        if row["daysLeft"] == 0:
            items.append(
                _notice(
                    f"countdown_{row['id']}",
                    "success",
                    f"今天就是「{row['title']}」",
                    str(row["date"]),
                    "/me/assistant?tab=countdowns",
                )
            )
        elif 0 < row["daysLeft"] <= 3:
            items.append(
                _notice(
                    f"countdown_{row['id']}",
                    "info",
                    f"「{row['title']}」还有 {row['daysLeft']} 天",
                    str(row["date"]),
                    "/me/assistant?tab=countdowns",
                )
            )
    unchecked = [item for item in checkins if not item.checked_today]
    at_risk = [item for item in unchecked if item.streak_days > 0]
    if at_risk:
        best = max(at_risk, key=lambda item: item.streak_days)
        items.append(
            _notice(
                "streak_risk",
                "warning",
                f"{len(at_risk)} 个群的连签今天还没续上",
                f"{best.group_name or best.group_id} 已连签 {best.streak_days} 天，今天不签就断了",
                "/me/affinity",
                len(at_risk),
            )
        )
    elif unchecked:
        items.append(
            _notice(
                "checkin",
                "info",
                f"{len(unchecked)} 个群今天还没签到",
                "可以在这里一键签完",
                "/me/affinity",
                len(unchecked),
            )
        )
    if quests and quests.get("enabled"):
        ready = [
            item
            for item in quests.get("items") or []
            if item.get("done") and not item.get("claimed")
        ]
        if ready:
            items.append(
                _notice(
                    "quest_claim",
                    "success",
                    f"{len(ready)} 个每日任务做完了，奖励还没领",
                    "、".join(str(item.get("title")) for item in ready),
                    "/me/affinity?tab=play",
                    len(ready),
                )
            )
    if bag:
        shield = str(bag.get("shield_until") or "")
        if shield:
            left = _safe(lambda: (date.fromisoformat(shield) - today).days, None)
            if left is not None and 0 <= left <= 3:
                items.append(
                    _notice(
                        "shield",
                        "info",
                        "好感护盾快到期了",
                        f"{shield} 到期，之后长期不来往好感会慢慢掉",
                        "/me/affinity?tab=play",
                    )
                )
        if bag.get("lucky_today") and unchecked:
            items.append(
                _notice(
                    "lucky",
                    "success",
                    "签到暴击卡今天生效",
                    "下一次签到积分翻倍，别忘了签",
                    "/me/affinity",
                )
            )
    if paused:
        items.append(
            _notice(
                "chat_paused",
                "warning",
                "你暂停了 Bot 的私聊自动回复",
                "恢复后它才会回你",
                "/me/chat",
            )
        )
    order = {"danger": 0, "warning": 1, "success": 2, "info": 3}
    items.sort(key=lambda item: order.get(item["tone"], 9))
    reminders_today = sum(
        1
        for item in record.get("reminders") or []
        if not item.get("paused")
        and not item.get("done")
        and str(item.get("due") or "").startswith(today.isoformat())
    )
    badges = {
        "/me/assistant": len(overdue) + len(due_today),
        "/me/tasks": len(blocked),
        "/me/affinity": len(unchecked),
        "/me/schedules": reminders_today,
    }
    return {
        "now": now.strftime(organizer.TIME_FMT),
        "items": items,
        "total": sum(1 for item in items if item["tone"] in {"danger", "warning"}),
        "badges": {key: value for key, value in badges.items() if value},
    }


async def _affinity_extras(
    user_id: int, now: datetime
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """每日任务和背包；好感玩法出问题时不拖垮提醒中心。"""

    try:
        pol = load_policy()
        quests = affinity_store.update(
            user_id, lambda rec: perks.quests_view(rec, user_id, now, pol)
        )
        bag = economy.inventory_view(
            affinity_store.load(user_id), pol, today=now.date()
        )
    except Exception:  # noqa: BLE001
        return None, None
    return quests, bag


@router.get("/inbox")
async def my_inbox(session: UserReadSession) -> dict[str, Any]:
    """等我处理的事：逾期待办、等回答的任务、快断的连签、没领的任务奖励……"""

    user_id = _uid(session)
    now = now_beijing()
    async with get_session() as db:
        checkins = await checkin_overview(db, user_id, now=now)
    quests, bag = await _affinity_extras(user_id, now)
    return ok(
        build_inbox(
            record=store.load(user_id),
            now=now,
            checkins=checkins,
            quests=quests,
            bag=bag,
            paused=auto_paused(user_id),
        )
    )


# ── 全站搜索 ────────────────────────────────────────────────────────────


def _clip(text: str, needle: str, width: int = 70) -> str:
    flat = " ".join(str(text or "").split())
    at = flat.lower().find(needle.lower())
    if at < 0 or len(flat) <= width:
        return flat[:width]
    start = max(0, at - width // 3)
    return ("…" if start else "") + flat[start : start + width]


def search_record(
    record: dict[str, Any], needle: str, today: date
) -> list[dict[str, Any]]:
    """纯函数：在小本本里找（待办/笔记/倒数日/提醒/心情备注）。"""

    low = needle.lower()

    def hit(*texts: Any) -> bool:
        return any(low in str(text or "").lower() for text in texts)

    groups: list[dict[str, Any]] = []
    todos = [
        {
            "id": f"todo-{item['id']}",
            "title": str(item.get("content") or ""),
            "sub": ("已完成" if item.get("done") else "未完成")
            + (f" · 截止 {item['due']}" if item.get("due") else ""),
            "to": f"/me/assistant?find={item['id']}",
        }
        for item in record.get("todos") or []
        if hit(item.get("content"))
    ]
    notes = [
        {
            "id": f"note-{item['id']}",
            "title": _clip(str(item.get("content") or ""), needle),
            "sub": (f"#{item['tag']} · " if item.get("tag") else "")
            + str(item.get("at") or ""),
            "to": "/me/assistant?tab=notes",
        }
        for item in record.get("notes") or []
        if hit(item.get("content"), item.get("tag"))
    ]
    countdowns = [
        {
            "id": f"cd-{item['id']}",
            "title": str(item.get("title") or ""),
            "sub": str(_countdown_row(item, today)["date"]),
            "to": "/me/assistant?tab=countdowns",
        }
        for item in record.get("countdowns") or []
        if hit(item.get("title"))
    ]
    reminders = [
        {
            "id": f"rem-{item['id']}",
            "title": str(item.get("title") or item.get("content") or ""),
            "sub": ("已结束" if item.get("done") else f"下次 {item.get('due') or '—'}"),
            "to": "/me/schedules",
        }
        for item in record.get("reminders") or []
        if hit(item.get("content"), item.get("title"))
    ]
    moods = [
        {
            "id": f"mood-{index}",
            "title": str(item.get("note") or ""),
            "sub": f"{organizer.MOOD_SCORES.get(int(item.get('score') or 3), '一般')} · {item.get('at') or ''}",
            "to": "/me/assistant?tab=moods",
        }
        for index, item in enumerate(record.get("moods") or [])
        if item.get("note") and hit(item.get("note"))
    ]
    for kind, label, rows in (
        ("todos", "待办", todos),
        ("notes", "笔记", notes),
        ("countdowns", "倒数日", countdowns),
        ("reminders", "提醒", reminders),
        ("moods", "心情备注", list(reversed(moods))),
    ):
        if rows:
            groups.append(
                {
                    "kind": kind,
                    "label": label,
                    "total": len(rows),
                    "items": rows[:_SEARCH_PER_KIND],
                }
            )
    return groups


@router.get("/search")
async def my_search(
    session: UserReadSession, q: str = Query(min_length=1, max_length=40)
) -> dict[str, Any]:
    """一次搜遍我自己的东西：小本本、Bot 记得我的事、聊天记录。"""

    user_id = _uid(session)
    needle = q.strip()
    if not needle:
        return ok({"q": "", "total": 0, "groups": []})
    now = now_beijing()
    groups = search_record(store.load(user_id), needle, now.date())
    async with get_session() as db:
        try:
            facts = [
                private_memory.fact_view(row)
                for row in await private_memory.list_facts(db, user_id, now=now)
            ]
        except Exception:  # noqa: BLE001 - 记忆表没迁移
            facts = []
        memory_hits = [
            {
                "id": f"fact-{fact['key']}",
                "title": _clip(str(fact["content"]), needle),
                "sub": str(fact["label"]),
                "to": "/me/memory",
            }
            for fact in facts
            if needle.lower() in f"{fact['content']} {fact['label']}".lower()
        ]
        rows = (
            await db.execute(
                select(ChatMessage, ChatSession.title)
                .join(ChatSession, ChatSession.id == ChatMessage.session_id)
                .where(
                    ChatSession.user_id == user_id,
                    ChatSession.is_deleted.is_(False),
                    ChatMessage.is_deleted.is_(False),
                    ChatMessage.role.in_(("user", "assistant")),
                    ChatMessage.content.contains(needle),
                )
                .order_by(ChatMessage.id.desc())
                .limit(_SEARCH_PER_KIND + 1)
            )
        ).all()
    if memory_hits:
        groups.append(
            {
                "kind": "memory",
                "label": "Bot 记得我",
                "total": len(memory_hits),
                "items": memory_hits[:_SEARCH_PER_KIND],
            }
        )
    if rows:
        groups.append(
            {
                "kind": "chats",
                "label": "聊天记录",
                "total": len(rows),
                "more": len(rows) > _SEARCH_PER_KIND,
                "items": [
                    {
                        "id": f"msg-{message.id}",
                        "title": _clip(str(message.content or ""), needle),
                        "sub": ("我" if message.role == "user" else "Bot")
                        + (f" · {title}" if title else "")
                        + (
                            f" · {message.created_at.strftime('%m-%d %H:%M')}"
                            if message.created_at
                            else ""
                        ),
                        "to": f"/me/chats?chat={message.session_id}&at={message.id}&q={needle}",
                    }
                    for message, title in rows[:_SEARCH_PER_KIND]
                ],
            }
        )
    return ok(
        {
            "q": needle,
            "total": sum(int(group["total"]) for group in groups),
            "groups": groups,
        }
    )


# ── 月历 ────────────────────────────────────────────────────────────────


def _month_range(raw: str | None, today: date) -> tuple[date, date]:
    try:
        year, month = (int(part) for part in str(raw or "").split("-"))
        first = date(year, month, 1)
    except (ValueError, TypeError):
        first = today.replace(day=1)
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    return first, last


def build_calendar(
    record: dict[str, Any],
    first: date,
    last: date,
    today: date,
    *,
    checkins: dict[str, int],
    affinity_by_day: dict[str, int],
) -> dict[str, Any]:
    """纯函数：把一个月里每天的事摆出来。"""

    days: dict[str, dict[str, Any]] = {}
    cursor = first
    while cursor <= last:
        key = cursor.isoformat()
        days[key] = {
            "date": key,
            "todos": [],
            "done": 0,
            "reminders": [],
            "countdowns": [],
            "mood": None,
            "moodCount": 0,
            "checkins": checkins.get(key, 0),
            "affinity": affinity_by_day.get(key, 0),
        }
        cursor += timedelta(days=1)
    for item in record.get("todos") or []:
        due = str(item.get("due") or "")
        if due in days:
            days[due]["todos"].append(
                {
                    "id": int(item["id"]),
                    "content": str(item.get("content") or ""),
                    "done": bool(item.get("done")),
                    "priority": str(item.get("priority") or "normal"),
                }
            )
        done_day = str(item.get("done_at") or "")[:10]
        if item.get("done") and done_day in days:
            days[done_day]["done"] += 1
    for item in record.get("reminders") or []:
        due = str(item.get("due") or "")
        if item.get("done") or item.get("paused") or due[:10] not in days:
            continue
        days[due[:10]]["reminders"].append(
            {
                "ref": f"p{item['id']}",
                "content": str(item.get("title") or item.get("content") or ""),
                "time": due[11:16],
                "repeat": str(item.get("repeat") or "none") != "none",
            }
        )
    for item in record.get("countdowns") or []:
        day = str(item.get("date") or "")
        if day in days:
            days[day]["countdowns"].append(
                {"id": int(item["id"]), "title": str(item.get("title") or "")}
            )
    sums: dict[str, list[int]] = {}
    for row in _mood_rows(record):
        day = str(row["at"] or "")[:10]
        if day in days:
            sums.setdefault(day, []).append(int(row["score"]))
    for day, scores in sums.items():
        days[day]["mood"] = round(sum(scores) / len(scores), 1)
        days[day]["moodCount"] = len(scores)
    rows = list(days.values())
    for row in rows:
        row["reminders"].sort(key=lambda item: item["time"])
    return {
        "month": first.strftime("%Y-%m"),
        "today": today.isoformat(),
        "firstWeekday": first.weekday(),
        "days": rows,
        "totals": {
            "todos": sum(len(row["todos"]) for row in rows),
            "done": sum(row["done"] for row in rows),
            "reminders": sum(len(row["reminders"]) for row in rows),
            "countdowns": sum(len(row["countdowns"]) for row in rows),
            "moodDays": sum(1 for row in rows if row["mood"] is not None),
            "checkinDays": sum(1 for row in rows if row["checkins"]),
            "affinity": sum(row["affinity"] for row in rows),
        },
    }


async def _checkin_days(
    db: Any, user_id: int, first: date, last: date
) -> dict[str, int]:
    rows = (
        await db.execute(
            select(CheckinRecord.checkin_date, func.count())
            .where(
                CheckinRecord.user_id == user_id,
                CheckinRecord.checkin_date >= first,
                CheckinRecord.checkin_date <= last,
            )
            .group_by(CheckinRecord.checkin_date)
        )
    ).all()
    return {day.isoformat(): int(count) for day, count in rows}


async def _affinity_days(
    db: Any, user_id: int, first: date, last: date
) -> dict[str, int]:
    try:
        rows = (
            await db.execute(
                select(AffinityEvent.day, func.sum(AffinityEvent.delta))
                .where(
                    AffinityEvent.user_id == user_id,
                    AffinityEvent.day >= first,
                    AffinityEvent.day <= last,
                )
                .group_by(AffinityEvent.day)
            )
        ).all()
    except Exception:  # noqa: BLE001 - 好感流水表没迁移
        return {}
    return {day.isoformat(): int(total or 0) for day, total in rows}


@router.get("/calendar")
async def my_calendar(
    session: UserReadSession, month: str | None = Query(default=None, max_length=7)
) -> dict[str, Any]:
    """一个月的日历：到期的待办、提醒、倒数日、心情、签到、好感变化。"""

    user_id = _uid(session)
    now = now_beijing()
    first, last = _month_range(month, now.date())
    async with get_session() as db:
        checkins = await _checkin_days(db, user_id, first, last)
        gained = await _affinity_days(db, user_id, first, last)
    return ok(
        build_calendar(
            store.load(user_id),
            first,
            last,
            now.date(),
            checkins=checkins,
            affinity_by_day=gained,
        )
    )


# ── 网页签到 ────────────────────────────────────────────────────────────


class CheckinBody(_Body):
    # 不填 = 把今天没签的群都签了
    group_id: str | None = Field(default=None, max_length=20)


@router.post("/checkin")
async def my_checkin(body: CheckinBody, session: UserWriteSession) -> dict[str, Any]:
    """在网页上签到：一个群，或者把今天没签的都签了。"""

    user_id = _uid(session)
    now = now_beijing()
    target: int | None = None
    if body.group_id:
        try:
            target = int(body.group_id)
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "群号不对"
            ) from exc
    results: list[dict[str, Any]] = []
    async with get_session() as db:
        statuses = await checkin_overview(db, user_id, now=now)
        if target is not None:
            statuses = [item for item in statuses if item.group_id == target]
            if not statuses:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "你不在这个群里")
        user = await db.get(BotUser, user_id)
        nickname = str(user.nickname) if user and user.nickname else None
        for item in statuses[:MAX_WEB_CHECKIN_GROUPS]:
            name = item.group_name or f"群 {item.group_id}"
            if item.checked_today:
                results.append(
                    {
                        "groupId": str(item.group_id),
                        "groupName": name,
                        "status": "already",
                        "streakDays": item.streak_days,
                    }
                )
                continue
            if not await check_feature_permission(
                user_id, item.group_id, "checkin", db
            ):
                results.append(
                    {
                        "groupId": str(item.group_id),
                        "groupName": name,
                        "status": "disabled",
                        "streakDays": item.streak_days,
                    }
                )
                continue
            result = await perform_checkin(
                db, group_id=item.group_id, user_id=user_id, nickname=nickname, now=now
            )
            results.append(
                {
                    "groupId": str(item.group_id),
                    "groupName": name,
                    "status": "ok" if result.ok else "already",
                    "streakDays": result.streak_days,
                    "reward": result.reward
                    + result.milestone_points
                    + result.level_bonus_points
                    + result.lucky_bonus_points,
                    "bonus": result.level_bonus_points,
                    "lucky": result.lucky_bonus_points > 0,
                    "expGain": result.exp_gain,
                    "affinityGain": result.affinity_gain,
                    "milestone": result.milestone,
                    "levelUp": result.leveled_up_to,
                    "rankToday": result.rank_today,
                }
            )
        try:
            settled = await perks.settle_quests(db, user_id, now=now)
        except Exception:  # noqa: BLE001 - 任务结算失败不影响签到
            settled = {"claimed": [], "affinity": 0, "bonus_item": None}
        await db.commit()
    signed = [row for row in results if row["status"] == "ok"]
    await hub.notify_change("user_affinity", str(user_id))
    return ok(
        {
            "results": results,
            "signed": len(signed),
            "already": sum(1 for row in results if row["status"] == "already"),
            "disabled": sum(1 for row in results if row["status"] == "disabled"),
            "reward": sum(int(row.get("reward") or 0) for row in signed),
            "affinityGain": sum(int(row.get("affinityGain") or 0) for row in signed),
            "levelUp": next(
                (row["levelUp"] for row in signed if row.get("levelUp")), None
            ),
            "quests": settled,
        }
    )


@router.get("/checkin/history")
async def my_checkin_history(
    session: UserReadSession, days: int = Query(default=120, ge=7, le=370)
) -> dict[str, Any]:
    """最近 N 天每天签了几个群、拿了多少积分（签到日历用）。"""

    user_id = _uid(session)
    today = now_beijing().date()
    since = today - timedelta(days=days - 1)
    async with get_session() as db:
        rows = (
            await db.execute(
                select(
                    CheckinRecord.checkin_date,
                    func.count(),
                    func.sum(CheckinRecord.reward),
                )
                .where(
                    CheckinRecord.user_id == user_id,
                    CheckinRecord.checkin_date >= since,
                )
                .group_by(CheckinRecord.checkin_date)
            )
        ).all()
    by_day = {
        day.isoformat(): (int(count), int(reward or 0)) for day, count, reward in rows
    }
    # 连续有签到的天数（任意一个群签了就算）
    streak = 0
    cursor = today if today.isoformat() in by_day else today - timedelta(days=1)
    while cursor.isoformat() in by_day:
        streak += 1
        cursor -= timedelta(days=1)
    return ok(
        {
            "today": today.isoformat(),
            "since": since.isoformat(),
            "days": [
                {"day": day, "groups": count, "reward": reward}
                for day, (count, reward) in sorted(by_day.items())
            ],
            "activeDays": len(by_day),
            "anyStreak": streak,
            "reward": sum(reward for _count, reward in by_day.values()),
        }
    )


# ── 成就 ────────────────────────────────────────────────────────────────

# (key, 分类, 名称, 说明, 图标, 指标, 目标值)
ACHIEVEMENTS: tuple[tuple[str, str, str, str, str, str, int], ...] = (
    ("checkin_1", "签到", "初来乍到", "第一次签到", "🌱", "checkinDays", 1),
    ("checkin_30", "签到", "常客", "累计签到 30 天", "📅", "checkinDays", 30),
    ("checkin_100", "签到", "百日打卡", "累计签到 100 天", "💯", "checkinDays", 100),
    ("streak_7", "签到", "一周不落", "某个群连签 7 天", "🔥", "bestStreak", 7),
    ("streak_30", "签到", "月度全勤", "某个群连签 30 天", "🏅", "bestStreak", 30),
    ("streak_100", "签到", "雷打不动", "某个群连签 100 天", "🏆", "bestStreak", 100),
    ("groups_3", "签到", "多群游走", "在 3 个群签到过", "🧭", "checkinGroups", 3),
    ("level_1", "关系", "混个面熟", "好感到「面熟」", "👋", "levelIndex", 1),
    ("level_2", "关系", "老熟人", "好感到「熟人」", "🤝", "levelIndex", 2),
    ("level_3", "关系", "成了朋友", "好感到「朋友」", "💗", "levelIndex", 3),
    ("level_4", "关系", "好朋友", "好感到「好朋友」", "💞", "levelIndex", 4),
    ("level_5", "关系", "挚友", "好感到「挚友」", "👑", "levelIndex", 5),
    ("gift_1", "关系", "礼轻情意重", "送出第一份礼物", "🎁", "gifts", 1),
    ("gift_20", "关系", "出手大方", "累计送礼 20 份", "🎀", "gifts", 20),
    ("interact_10", "关系", "贴贴达人", "互动 10 次", "🫶", "interactions", 10),
    ("todo_1", "小本本", "记下第一件事", "完成第一条待办", "✅", "todosDone", 1),
    ("todo_20", "小本本", "行动派", "完成 20 条待办", "⚡", "todosDone", 20),
    ("todo_100", "小本本", "清单终结者", "完成 100 条待办", "🚀", "todosDone", 100),
    ("note_10", "小本本", "随手记", "写下 10 条笔记", "📝", "notes", 10),
    ("mood_7", "小本本", "认真感受", "连续 7 天记心情", "🌤️", "moodStreak", 7),
    ("mood_30", "小本本", "情绪观察员", "累计记 30 次心情", "🌈", "moods", 30),
    ("countdown_3", "小本本", "有所期待", "同时有 3 个倒数日", "⏳", "countdowns", 3),
    ("chat_100", "聊天", "话匣子", "和 Bot 说了 100 句", "💬", "myMessages", 100),
    ("chat_1000", "聊天", "无话不谈", "和 Bot 说了 1000 句", "🗣️", "myMessages", 1000),
    ("memory_5", "聊天", "被记住的人", "Bot 记住了你 5 件事", "🧠", "facts", 5),
    ("task_5", "聊天", "甩手掌柜", "后台小助手做完 5 件事", "🤖", "tasksDone", 5),
    ("ww_1", "游戏", "初入村庄", "打完第一局狼人杀", "🐺", "wwGames", 1),
    ("ww_win_10", "游戏", "十胜", "狼人杀赢 10 局", "🎯", "wwWins", 10),
    ("ww_streak_3", "游戏", "三连胜", "狼人杀连胜 3 局", "🌟", "wwBestStreak", 3),
    ("ww_sheriff", "游戏", "警徽在手", "当过警长", "⭐", "wwSheriff", 1),
)


def _mood_streak(record: dict[str, Any], today: date) -> int:
    """连续记心情的最长天数（看现存记录）。"""

    days = sorted(
        {str(item.get("at") or "")[:10] for item in record.get("moods") or []}
    )
    best = run = 0
    previous: date | None = None
    for raw in days:
        day = _safe(lambda raw=raw: date.fromisoformat(raw), None)
        if day is None or day > today:
            continue
        run = run + 1 if previous is not None and (day - previous).days == 1 else 1
        best = max(best, run)
        previous = day
    return best


def build_achievements(
    metrics: dict[str, int], unlocked: dict[str, str], today: str
) -> tuple[dict[str, Any], dict[str, str]]:
    """纯函数：按指标算每个成就的进度；新达成的记上今天。返回 (给前端的结构, 新的解锁表)。"""

    known = dict(unlocked)
    groups: dict[str, list[dict[str, Any]]] = {}
    fresh: list[str] = []
    for key, group, title, desc, icon, metric, target in ACHIEVEMENTS:
        value = int(metrics.get(metric, 0))
        if value >= target and key not in known:
            known[key] = today
            fresh.append(key)
        done = key in known
        groups.setdefault(group, []).append(
            {
                "key": key,
                "title": title,
                "description": desc,
                "icon": icon,
                "value": min(value, target) if not done else max(value, target),
                "target": target,
                "done": done,
                "unlockedAt": known.get(key),
                "progress": 1.0 if done else round(min(value / target, 1.0), 3),
            }
        )
    flat = [item for items in groups.values() for item in items]
    pending = sorted(
        (item for item in flat if not item["done"]),
        key=lambda item: -item["progress"],
    )
    return (
        {
            "total": len(flat),
            "unlocked": sum(1 for item in flat if item["done"]),
            "fresh": fresh,
            "groups": [
                {
                    "label": label,
                    "unlocked": sum(1 for item in items if item["done"]),
                    "items": items,
                }
                for label, items in groups.items()
            ],
            "next": pending[:3],
            "metrics": metrics,
        },
        known,
    )


async def _werewolf_metrics(db: Any, user_id: int) -> dict[str, int]:
    try:
        from ..yawn_werewolf.models import WerewolfGame, WerewolfPlayer

        rows = (
            await db.execute(
                select(WerewolfPlayer, WerewolfGame)
                .join(WerewolfGame, WerewolfGame.id == WerewolfPlayer.game_id)
                .where(
                    WerewolfPlayer.user_id == user_id, WerewolfPlayer.is_ai.is_(False)
                )
            )
        ).all()
    except Exception:  # noqa: BLE001 - 没装狼人杀或表没迁移
        return {}
    from .me_insight_routes import personal_werewolf_stats

    stats = personal_werewolf_stats([(p, g) for p, g in rows], {})
    return {
        "wwGames": int(stats["finished"]),
        "wwWins": int(stats["wins"]),
        "wwBestStreak": int(stats["bestStreak"]),
        "wwSheriff": int(stats["sheriff"]),
    }


async def _metrics(db: Any, user_id: int, now: datetime) -> dict[str, int]:
    record = store.load(user_id)
    checkins = await checkin_overview(db, user_id, now=now, active_only=False)
    best_streak = (
        await db.scalar(
            select(func.max(CheckinUser.streak_days)).where(
                CheckinUser.user_id == user_id
            )
        )
        or 0
    )
    snap = await affinity.snapshot(db, user_id)
    my_messages = (
        await db.scalar(
            select(func.count())
            .select_from(ChatMessage)
            .join(ChatSession, ChatSession.id == ChatMessage.session_id)
            .where(ChatSession.user_id == user_id, ChatMessage.role == "user")
        )
        or 0
    )
    try:
        facts = len(await private_memory.list_facts(db, user_id, now=now))
    except Exception:  # noqa: BLE001
        facts = 0
    play = _safe(lambda: affinity_store.load(user_id), {})
    web = record.get("web") or {}
    todos_done_now = sum(1 for item in record.get("todos") or [] if item.get("done"))
    metrics = {
        "checkinDays": sum(item.total_days for item in checkins),
        "bestStreak": max(
            int(best_streak), max((item.streak_days for item in checkins), default=0)
        ),
        "checkinGroups": sum(1 for item in checkins if item.total_days > 0),
        "levelIndex": int(snap.level.index),
        "gifts": sum(int(v or 0) for v in (play.get("gift_totals") or {}).values()),
        "interactions": sum(
            int(v or 0) for v in (play.get("interact_totals") or {}).values()
        ),
        # 清掉的已完成待办记在 web.todos_cleared，免得“清掉已完成”让进度倒退
        "todosDone": todos_done_now + int(web.get("todos_cleared") or 0),
        "notes": len(record.get("notes") or []),
        "moods": len(record.get("moods") or []),
        "moodStreak": _mood_streak(record, now.date()),
        "countdowns": len(record.get("countdowns") or []),
        "myMessages": int(my_messages),
        "facts": facts,
        "tasksDone": sum(
            1 for task in record.get("tasks") or [] if task.get("status") == "done"
        ),
    }
    metrics.update(await _werewolf_metrics(db, user_id))
    return metrics


@router.get("/achievements")
async def my_achievements(session: UserReadSession) -> dict[str, Any]:
    """成就墙：按现有数据算进度；达成过的不会因为之后数据变少而收回。"""

    user_id = _uid(session)
    now = now_beijing()
    async with get_session() as db:
        metrics = await _metrics(db, user_id, now)
    web = store.load(user_id).get("web") or {}
    payload, known = build_achievements(
        metrics, dict(web.get("achievements") or {}), now.date().isoformat()
    )
    if payload["fresh"]:

        def remember(record: dict[str, Any]) -> None:
            record.setdefault("web", {})["achievements"] = known

        store.update(user_id, remember)
    return ok(payload)


# ── 数据洞察 ────────────────────────────────────────────────────────────


def build_series(
    record: dict[str, Any],
    today: date,
    days: int,
    *,
    messages: list[tuple[datetime | None, str]],
    checkins: dict[str, int],
    affinity_by_day: dict[str, int],
) -> dict[str, Any]:
    """纯函数：最近 N 天每天的聊天、待办、心情、签到、好感，加上时段分布和周对比。"""

    start = today - timedelta(days=days - 1)
    series: dict[str, dict[str, Any]] = {}
    for offset in range(days):
        key = (start + timedelta(days=offset)).isoformat()
        series[key] = {
            "day": key,
            "mine": 0,
            "bot": 0,
            "todosAdded": 0,
            "todosDone": 0,
            "mood": None,
            "checkins": checkins.get(key, 0),
            "affinity": affinity_by_day.get(key, 0),
        }
    hours = [0] * 24
    weekdays = [0] * 7
    for at, role in messages:
        if at is None:
            continue
        key = at.date().isoformat()
        if key not in series:
            continue
        if role == "user":
            series[key]["mine"] += 1
            hours[at.hour] += 1
            weekdays[at.weekday()] += 1
        else:
            series[key]["bot"] += 1
    for item in record.get("todos") or []:
        added = str(item.get("at") or "")[:10]
        if added in series:
            series[added]["todosAdded"] += 1
        done = str(item.get("done_at") or "")[:10]
        if item.get("done") and done in series:
            series[done]["todosDone"] += 1
    moods: dict[str, list[int]] = {}
    for row in _mood_rows(record):
        key = str(row["at"] or "")[:10]
        if key in series:
            moods.setdefault(key, []).append(int(row["score"]))
    for key, scores in moods.items():
        series[key]["mood"] = round(sum(scores) / len(scores), 1)
    rows = list(series.values())

    def window(chunk: list[dict[str, Any]]) -> dict[str, Any]:
        scored = [row["mood"] for row in chunk if row["mood"] is not None]
        return {
            "mine": sum(row["mine"] for row in chunk),
            "todosDone": sum(row["todosDone"] for row in chunk),
            "checkins": sum(row["checkins"] for row in chunk),
            "affinity": sum(row["affinity"] for row in chunk),
            "activeDays": sum(
                1 for row in chunk if row["mine"] or row["checkins"] or row["todosDone"]
            ),
            "mood": round(sum(scored) / len(scored), 1) if scored else None,
        }

    busiest = max(range(24), key=lambda hour: hours[hour]) if any(hours) else None
    best_day = max(rows, key=lambda row: row["mine"]) if rows else None
    # 最长连续活跃（聊天、签到、完成待办任一）
    run = best_run = 0
    for row in rows:
        active = bool(row["mine"] or row["checkins"] or row["todosDone"])
        run = run + 1 if active else 0
        best_run = max(best_run, run)
    return {
        "days": days,
        "today": today.isoformat(),
        "series": rows,
        "hours": hours,
        "weekdays": weekdays,
        "totals": window(rows),
        "thisWeek": window(rows[-7:]),
        "lastWeek": window(rows[-14:-7]) if days >= 14 else None,
        "busiestHour": busiest,
        "bestDay": {"day": best_day["day"], "mine": best_day["mine"]}
        if best_day and best_day["mine"]
        else None,
        "activeStreak": best_run,
    }


async def _ranks(db: Any, checkins: list[Any]) -> list[dict[str, Any]]:
    """我在各群的名次：只给名次和总人数。"""

    rows: list[dict[str, Any]] = []
    for item in checkins[:20]:
        members = (
            await db.scalar(
                select(func.count())
                .select_from(UserGroup)
                .where(
                    UserGroup.group_id == item.group_id, UserGroup.is_active.is_(True)
                )
            )
            or 0
        )
        affinity_rank = None
        if item.group_affinity > 0:
            ahead = (
                await db.scalar(
                    select(func.count())
                    .select_from(UserGroup)
                    .where(
                        UserGroup.group_id == item.group_id,
                        UserGroup.is_active.is_(True),
                        UserGroup.group_affinity > item.group_affinity,
                    )
                )
                or 0
            )
            affinity_rank = int(ahead) + 1
        points_rank = None
        if item.points > 0:
            ahead = (
                await db.scalar(
                    select(func.count())
                    .select_from(CheckinUser)
                    .where(
                        CheckinUser.group_id == item.group_id,
                        CheckinUser.points > item.points,
                    )
                )
                or 0
            )
            points_rank = int(ahead) + 1
        rows.append(
            {
                "groupId": str(item.group_id),
                "groupName": item.group_name,
                "members": int(members),
                "groupAffinity": item.group_affinity,
                "affinityRank": affinity_rank,
                "points": item.points,
                "pointsRank": points_rank,
                "streakDays": item.streak_days,
                "totalDays": item.total_days,
            }
        )
    rows.sort(key=lambda row: (row["affinityRank"] is None, row["affinityRank"] or 0))
    return rows


@router.get("/insights")
async def my_insights(
    session: UserReadSession, days: int = Query(default=30, ge=7, le=90)
) -> dict[str, Any]:
    """我的数据洞察：最近 N 天的聊天、待办、心情、签到、好感，和我在各群的名次。"""

    user_id = _uid(session)
    now = now_beijing()
    today = now.date()
    start = today - timedelta(days=days - 1)
    since = datetime.combine(start, datetime.min.time())
    async with get_session() as db:
        messages = (
            await db.execute(
                select(ChatMessage.created_at, ChatMessage.role)
                .join(ChatSession, ChatSession.id == ChatMessage.session_id)
                .where(
                    ChatSession.user_id == user_id,
                    ChatMessage.created_at >= since,
                    ChatMessage.role.in_(("user", "assistant")),
                )
                .limit(20000)
            )
        ).all()
        checkin_days = await _checkin_days(db, user_id, start, today)
        gained = await _affinity_days(db, user_id, start, today)
        statuses = await checkin_overview(db, user_id, now=now)
        ranks = await _ranks(db, statuses)
    payload = build_series(
        store.load(user_id),
        today,
        days,
        messages=[(at, str(role)) for at, role in messages],
        checkins=checkin_days,
        affinity_by_day=gained,
    )
    return ok({**payload, "ranks": ranks})


# ── 待办：批量、一次加多条 ──────────────────────────────────────────────


class TodoBatchBody(_Body):
    ids: list[int] = Field(min_length=1, max_length=100)
    action: Literal["done", "undone", "delete", "postpone", "priority", "due"]
    days: int | None = Field(default=None, ge=1, le=365)
    priority: Literal["high", "normal", "low"] | None = None
    # "" 表示去掉截止日期
    due: str | None = Field(default=None, max_length=10)


@router.post("/assistant/todos/batch")
async def batch_my_todos(
    body: TodoBatchBody, session: UserWriteSession
) -> dict[str, Any]:
    """一次处理好几条待办：完成、撤销完成、删除、往后推几天、改优先级、改截止日期。"""

    user_id = _uid(session)
    now = now_beijing()
    today = now.date()
    wanted = set(body.ids)
    if body.action == "postpone" and body.days is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "要往后推几天？")
    if body.action == "priority" and body.priority is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "要改成什么优先级？")
    new_due: str | None = None
    if body.action == "due":
        if body.due is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "要改到哪天？")
        new_due = _run(organizer.parse_day, body.due).isoformat() if body.due else ""

    def apply(record: dict[str, Any]) -> int:
        touched = 0
        keep: list[dict[str, Any]] = []
        for item in record.get("todos") or []:
            if int(item.get("id") or 0) not in wanted:
                keep.append(item)
                continue
            touched += 1
            if body.action == "delete":
                if item.get("done"):
                    web = record.setdefault("web", {})
                    web["todos_cleared"] = int(web.get("todos_cleared") or 0) + 1
                continue
            if body.action == "done":
                if not item.get("done"):
                    item["done"] = True
                    item["done_at"] = organizer.fmt(now)
            elif body.action == "undone":
                item["done"] = False
                item.pop("done_at", None)
            elif body.action == "priority":
                item["priority"] = body.priority
            elif body.action == "due":
                if new_due:
                    item["due"] = new_due
                else:
                    item.pop("due", None)
            elif body.action == "postpone":
                # 逾期的从今天起算，没设截止的也从今天起算
                base = _safe(
                    lambda item=item: organizer.parse_day(str(item.get("due"))), today
                )
                base = max(base, today)
                item["due"] = (base + timedelta(days=int(body.days or 1))).isoformat()
            keep.append(item)
        record["todos"] = keep
        return touched

    touched = store.update(user_id, apply)
    if touched == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "这些待办都不在了，刷新看看")
    await _changed(user_id)
    return ok({"affected": touched, **_todos_payload(store.load(user_id), today)})


class TodoBulkBody(_Body):
    lines: list[str] = Field(min_length=1, max_length=30)
    priority: Literal["high", "normal", "low"] = "normal"
    due: str | None = Field(default=None, max_length=10)


@router.post("/assistant/todos/bulk")
async def bulk_add_my_todos(
    body: TodoBulkBody, session: UserWriteSession
) -> dict[str, Any]:
    """一次贴进来好几行，每行一条待办；放不下或不合规的行原样告诉他。"""

    user_id = _uid(session)
    now = now_beijing()
    added = 0
    skipped: list[dict[str, str]] = []
    for raw in body.lines:
        line = raw.strip().lstrip("-*•· ").strip()
        if not line:
            continue
        args: dict[str, Any] = {
            "action": "add",
            "content": line[:200],
            "priority": body.priority,
        }
        if body.due:
            args["due"] = body.due
        try:
            organizer.todos(user_id, args, now)
            added += 1
        except (ToolError, ValueError) as exc:
            skipped.append({"line": line[:60], "reason": str(exc)})
            if getattr(exc, "code", "") == "quota":
                break
    if added:
        await _changed(user_id)
    return ok(
        {
            "added": added,
            "skipped": skipped,
            **_todos_payload(store.load(user_id), now.date()),
        }
    )


class ClearBody(_Body):
    section: Literal["todos", "notes", "countdowns", "moods"]
    # 必须原样打出类别名，防止误触
    confirm: str = Field(max_length=20)


_SECTION_LABEL = {
    "todos": "待办",
    "notes": "笔记",
    "countdowns": "倒数日",
    "moods": "心情",
}


@router.post("/assistant/clear")
async def clear_my_section(
    body: ClearBody, session: UserWriteSession
) -> dict[str, Any]:
    """清空小本本里的某一类（危险操作：要打出类别名确认）。"""

    user_id = _uid(session)
    label = _SECTION_LABEL[body.section]
    if body.confirm.strip() != label:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"请输入“{label}”确认"
        )

    def apply(record: dict[str, Any]) -> int:
        count = len(record.get(body.section) or [])
        if body.section == "todos":
            web = record.setdefault("web", {})
            web["todos_cleared"] = int(web.get("todos_cleared") or 0) + sum(
                1 for item in record.get("todos") or [] if item.get("done")
            )
        record[body.section] = []
        return count

    cleared = store.update(user_id, apply)
    await _changed(user_id)
    return ok(
        {
            "section": body.section,
            "cleared": cleared,
            "counts": store.counts(store.load(user_id)),
        }
    )


# ── 网页偏好（跨设备同步） ──────────────────────────────────────────────

_HOME_BLOCKS = ("notices", "onboarding", "today", "quests", "week", "groups", "more")
_LANDINGS = ("/me", "/me/assistant", "/me/calendar", "/me/chats", "/me/affinity")


class UiPrefsBody(_Body):
    home_hidden: list[str] | None = Field(default=None, max_length=len(_HOME_BLOCKS))
    home_order: list[str] | None = Field(default=None, max_length=len(_HOME_BLOCKS))
    landing: str | None = Field(default=None, max_length=40)
    pinned_chats: list[str] | None = Field(default=None, max_length=20)
    starred: list[str] | None = Field(default=None, max_length=12)
    onboarding_dismissed: bool | None = None
    seen_achievements: list[str] | None = Field(
        default=None, max_length=len(ACHIEVEMENTS)
    )
    seen_notices: list[str] | None = Field(default=None, max_length=40)


def _ui_view(record: dict[str, Any]) -> dict[str, Any]:
    ui = (record.get("web") or {}).get("ui") or {}
    return {
        "homeHidden": [
            key for key in ui.get("home_hidden") or [] if key in _HOME_BLOCKS
        ],
        "homeOrder": [key for key in ui.get("home_order") or [] if key in _HOME_BLOCKS],
        "landing": ui.get("landing") if ui.get("landing") in _LANDINGS else "/me",
        "pinnedChats": [str(value) for value in ui.get("pinned_chats") or []][:20],
        "starred": [str(value) for value in ui.get("starred") or []][:12],
        "onboardingDismissed": bool(ui.get("onboarding_dismissed")),
        "seenAchievements": [str(value) for value in ui.get("seen_achievements") or []],
        "seenNotices": [str(value) for value in ui.get("seen_notices") or []][:40],
        "homeBlocks": list(_HOME_BLOCKS),
        "landings": list(_LANDINGS),
    }


@router.get("/ui-prefs")
async def my_ui_prefs(session: UserReadSession) -> dict[str, Any]:
    return ok(_ui_view(store.load(_uid(session))))


@router.put("/ui-prefs")
async def put_my_ui_prefs(
    body: UiPrefsBody, session: UserWriteSession
) -> dict[str, Any]:
    """只改传了的字段；这些偏好跟着账号走，换设备也在。"""

    user_id = _uid(session)
    changes = body.model_dump(exclude_none=True)
    for key in ("home_hidden", "home_order"):
        if key in changes and any(value not in _HOME_BLOCKS for value in changes[key]):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "不认识的首页板块"
            )
    if "landing" in changes and changes["landing"] not in _LANDINGS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "不能把这个页面设成首页"
        )
    if "starred" in changes and any(
        not str(value).startswith("/me") or len(str(value)) > 40
        for value in changes["starred"]
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "只能收藏我的空间里的页面"
        )
    for key in ("pinned_chats", "seen_achievements", "seen_notices"):
        if key in changes:
            changes[key] = [str(value)[:40] for value in changes[key]]

    def apply(record: dict[str, Any]) -> None:
        record.setdefault("web", {}).setdefault("ui", {}).update(changes)

    store.update(user_id, apply)
    return ok(_ui_view(store.load(user_id)))


__all__ = [
    "ACHIEVEMENTS",
    "build_achievements",
    "build_calendar",
    "build_inbox",
    "build_series",
    "router",
    "search_record",
]
