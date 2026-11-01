# ruff: noqa: E501, C901, RUF003, PERF203, TRY003
"""私聊个人助理工具：笔记、待办、提醒、倒数日、心情、聊天偏好。

数据都在 ``private_store``（每人一个 JSON），只作用于发消息的本人。
时间一律是北京时间 naive，存成 ``YYYY-MM-DD HH:MM`` 字符串；提醒由
``private_scheduler`` 到点发私聊。所有用户文本先过 ``clean_text``：去控制字符、
限长、拒绝像指令的内容（这些内容之后会作为工具结果回到模型里）。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from . import private_store as store
from .memory_hygiene import looks_like_instruction
from .tool_args import ERROR_INVALID_ARGS, ERROR_NOT_FOUND, ERROR_QUOTA, ToolError

TIME_FMT = "%Y-%m-%d %H:%M"
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_CLOCK_RE = re.compile(r"^([01]?\d|2[0-3])[:：]([0-5]\d)$")
_MMDD_RE = re.compile(r"^(0?[1-9]|1[0-2])-(0?[1-9]|[12]\d|3[01])$")
# 提醒最早 1 分钟后、最远一年内；稍后再提醒 1 分钟～1 天。
MIN_LEAD = timedelta(minutes=1)
MAX_LEAD = timedelta(days=366)
REPEATS = ("none", "daily", "weekdays", "weekly")
REPEAT_LABELS = {"none": "一次", "daily": "每天", "weekdays": "工作日", "weekly": "每周"}
PRIORITIES = ("high", "normal", "low")
PRIORITY_LABELS = {"high": "重要", "normal": "普通", "low": "不急"}
_PRIORITY_ORDER = {"high": 0, "normal": 1, "low": 2}
MOOD_SCORES = {1: "很差", 2: "不太好", 3: "一般", 4: "不错", 5: "很好"}
PREF_FIELDS = (
    "reply_length",
    "emoji",
    "call_me",
    "quiet_start",
    "quiet_end",
    "morning_greeting",
    "checkin_nudge",
    "birthday",
    "proactive",
)


def clean_text(raw: Any, *, limit: int, field: str = "内容") -> str:
    text = _CTRL_RE.sub("", str(raw or "")).strip()
    if not text:
        raise ToolError(f"{field}不能为空", code=ERROR_INVALID_ARGS)
    if looks_like_instruction(text):
        raise ToolError(
            f"这段{field}像是在给我下指令，不能保存",
            code=ERROR_INVALID_ARGS,
            hint="如实告诉用户这句不能存",
        )
    return text[:limit]


def fmt(value: datetime) -> str:
    return value.strftime(TIME_FMT)


def parse_time(raw: str) -> datetime:
    text = str(raw or "").strip().replace("T", " ").replace("/", "-")
    for pattern in (TIME_FMT, "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, pattern)  # noqa: DTZ007 - 北京时间 naive
        except ValueError:
            continue
    raise ToolError(
        f"时间「{raw}」看不懂",
        code=ERROR_INVALID_ARGS,
        hint="at 用 YYYY-MM-DD HH:MM（北京时间），相对时间用 in_minutes",
    )


def parse_day(raw: str) -> date:
    text = str(raw or "").strip().replace("/", "-")
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()  # noqa: DTZ007
    except ValueError as exc:
        raise ToolError(
            f"日期「{raw}」看不懂", code=ERROR_INVALID_ARGS, hint="日期用 YYYY-MM-DD"
        ) from exc


def parse_clock(raw: str, field: str) -> str:
    match = _CLOCK_RE.match(str(raw or "").strip())
    if not match:
        raise ToolError(f"{field} 要写成 HH:MM", code=ERROR_INVALID_ARGS)
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def _require_id(args: dict[str, Any]) -> int:
    item_id = args.get("id")
    if item_id is None:
        raise ToolError("缺少 id", code=ERROR_INVALID_ARGS, hint="先 list 看编号")
    return int(item_id)


def _not_found(what: str, item_id: int) -> ToolError:
    return ToolError(
        f"没有编号 {item_id} 的{what}", code=ERROR_NOT_FOUND, hint="先 list 看编号"
    )


def _check_room(record: dict[str, Any], section: str, what: str) -> None:
    if len(record[section]) >= store.LIMITS[section]:
        raise ToolError(
            f"{what}已经有 {store.LIMITS[section]} 条了，先删掉一些",
            code=ERROR_QUOTA,
        )


# ── 笔记 ─────────────────────────────────────────────────


def _note_view(item: dict[str, Any]) -> dict[str, Any]:
    view = {"id": item["id"], "content": item["content"], "at": item.get("at")}
    if item.get("tag"):
        view["tag"] = item["tag"]
    if item.get("pinned"):
        view["pinned"] = True
    return view


def _sorted_notes(notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(notes, key=lambda n: (not n.get("pinned"), -int(n["id"])))


def notes(user_id: int, args: dict[str, Any], now: datetime) -> dict[str, Any]:
    action = str(args.get("action") or "list")

    def run(record: dict[str, Any]) -> dict[str, Any]:
        items = record["notes"]
        if action == "add":
            _check_room(record, "notes", "笔记")
            item = {
                "id": store.next_id(record),
                "content": clean_text(args.get("content"), limit=300),
                "at": fmt(now),
            }
            if args.get("tag"):
                item["tag"] = clean_text(args["tag"], limit=12, field="标签")
            items.append(item)
            return {"saved": _note_view(item), "total": len(items)}
        if action in {"list", "search"}:
            keyword = str(args.get("keyword") or "").strip().casefold()
            tag = str(args.get("tag") or "").strip()
            hits = [
                n
                for n in items
                if (not keyword or keyword in str(n["content"]).casefold())
                and (not tag or n.get("tag") == tag)
            ]
            limit = int(args.get("limit") or 10)
            ordered = _sorted_notes(hits)
            return {
                "notes": [_note_view(n) for n in ordered[:limit]],
                "matched": len(hits),
                "total": len(items),
                "tags": sorted({str(n["tag"]) for n in items if n.get("tag")}),
            }
        item_id = _require_id(args)
        item = store.find(record, "notes", item_id)
        if item is None:
            raise _not_found("笔记", item_id)
        if action == "delete":
            items.remove(item)
            return {"deleted": item_id, "total": len(items)}
        if action in {"pin", "unpin"}:
            item["pinned"] = action == "pin"
            return {"note": _note_view(item)}
        raise ToolError("未知操作", code=ERROR_INVALID_ARGS)

    return store.update(user_id, run)


# ── 待办 ─────────────────────────────────────────────────


def _todo_view(item: dict[str, Any], today: date) -> dict[str, Any]:
    view: dict[str, Any] = {
        "id": item["id"],
        "content": item["content"],
        "priority": PRIORITY_LABELS.get(item.get("priority", "normal"), "普通"),
        "done": bool(item.get("done")),
    }
    if item.get("due"):
        view["due"] = item["due"]
        if not item.get("done"):
            left = (parse_day(item["due"]) - today).days
            view["due_in_days"] = left
            if left < 0:
                view["overdue"] = True
    if item.get("done_at"):
        view["done_at"] = item["done_at"]
    return view


def _todo_key(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        bool(item.get("done")),
        item.get("due") or "9999-99-99",
        _PRIORITY_ORDER.get(item.get("priority", "normal"), 1),
        int(item["id"]),
    )


def todos(user_id: int, args: dict[str, Any], now: datetime) -> dict[str, Any]:
    action = str(args.get("action") or "list")
    today = now.date()

    def run(record: dict[str, Any]) -> dict[str, Any]:
        items = record["todos"]
        if action == "add":
            _check_room(record, "todos", "待办")
            new_item: dict[str, Any] = {
                "id": store.next_id(record),
                "content": clean_text(args.get("content"), limit=120),
                "priority": str(args.get("priority") or "normal"),
                "done": False,
                "at": fmt(now),
            }
            if args.get("due"):
                new_item["due"] = parse_day(args["due"]).isoformat()
            items.append(new_item)
            pending = sum(1 for t in items if not t.get("done"))
            return {"added": _todo_view(new_item, today), "pending": pending}
        if action == "list":
            include_done = bool(args.get("include_done"))
            shown = [t for t in items if include_done or not t.get("done")]
            shown.sort(key=_todo_key)
            return {
                "todos": [_todo_view(t, today) for t in shown[: int(args.get("limit") or 20)]],
                "pending": sum(1 for t in items if not t.get("done")),
                "done": sum(1 for t in items if t.get("done")),
            }
        if action == "clear_done":
            before = len(items)
            record["todos"] = [t for t in items if not t.get("done")]
            return {"cleared": before - len(record["todos"])}
        item_id = _require_id(args)
        item = store.find(record, "todos", item_id)
        if item is None:
            raise _not_found("待办", item_id)
        if action == "done":
            item["done"] = True
            item["done_at"] = fmt(now)
        elif action == "undo":
            item["done"] = False
            item.pop("done_at", None)
        elif action == "delete":
            items.remove(item)
            return {"deleted": item_id}
        else:
            raise ToolError("未知操作", code=ERROR_INVALID_ARGS)
        return {
            "todo": _todo_view(item, today),
            "pending": sum(1 for t in items if not t.get("done")),
        }

    return store.update(user_id, run)


# ── 提醒 ─────────────────────────────────────────────────


def next_occurrence(due: datetime, repeat: str, after: datetime) -> datetime | None:
    """重复提醒的下一次（严格晚于 ``after``）；一次性的返回 None。"""

    if repeat == "none":
        return None
    step = timedelta(days=7 if repeat == "weekly" else 1)
    nxt = due + step
    while nxt <= after or (repeat == "weekdays" and nxt.weekday() >= 5):  # noqa: PLR2004
        nxt += timedelta(days=1) if repeat == "weekdays" else step
    return nxt


def _reminder_view(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["id"],
        "content": item["content"],
        "due": item["due"],
        "repeat": REPEAT_LABELS.get(item.get("repeat", "none"), "一次"),
    }


def reminders(user_id: int, args: dict[str, Any], now: datetime) -> dict[str, Any]:
    action = str(args.get("action") or "list")

    def run(record: dict[str, Any]) -> dict[str, Any]:
        items = record["reminders"]
        if action == "add":
            _check_room(record, "reminders", "提醒")
            if args.get("in_minutes") is not None:
                due = now + timedelta(minutes=int(args["in_minutes"]))
            elif args.get("at"):
                due = parse_time(args["at"])
            else:
                raise ToolError(
                    "要给提醒时间",
                    code=ERROR_INVALID_ARGS,
                    hint="at（YYYY-MM-DD HH:MM）或 in_minutes 二选一",
                )
            if due - now < MIN_LEAD - timedelta(seconds=1):
                raise ToolError("提醒时间已经过了", code=ERROR_INVALID_ARGS, hint="确认日期，或用 in_minutes")
            if due - now > MAX_LEAD:
                raise ToolError("最远只能提醒一年以内的事", code=ERROR_INVALID_ARGS)
            repeat = str(args.get("repeat") or "none")
            if repeat == "weekdays" and due.weekday() >= 5:  # noqa: PLR2004
                due = next_occurrence(due, "weekdays", due) or due
            item = {
                "id": store.next_id(record),
                "content": clean_text(args.get("content"), limit=120),
                "due": fmt(due),
                "repeat": repeat,
                "created": fmt(now),
            }
            items.append(item)
            return {"scheduled": _reminder_view(item), "minutes_from_now": int((due - now).total_seconds() // 60)}
        if action == "list":
            ordered = sorted(items, key=lambda r: r["due"])
            return {"reminders": [_reminder_view(r) for r in ordered], "total": len(items)}
        item_id = _require_id(args)
        item = store.find(record, "reminders", item_id)
        if item is None:
            raise _not_found("提醒", item_id)
        if action == "cancel":
            items.remove(item)
            return {"cancelled": item_id}
        if action == "snooze":
            minutes = int(args.get("snooze_minutes") or 10)
            item["due"] = fmt(max(parse_time(item["due"]), now) + timedelta(minutes=minutes))
            return {"reminder": _reminder_view(item)}
        raise ToolError("未知操作", code=ERROR_INVALID_ARGS)

    return store.update(user_id, run)


# ── 倒数日 ───────────────────────────────────────────────


def _countdown_view(item: dict[str, Any], today: date) -> dict[str, Any]:
    target = parse_day(item["date"])
    days = (target - today).days
    return {
        "id": item["id"],
        "title": item["title"],
        "date": item["date"],
        "days_left": days,
        "status": "今天" if days == 0 else ("已过去" if days < 0 else "还没到"),
        "notify": bool(item.get("notify", True)),
    }


def countdowns(user_id: int, args: dict[str, Any], now: datetime) -> dict[str, Any]:
    action = str(args.get("action") or "list")
    today = now.date()

    def run(record: dict[str, Any]) -> dict[str, Any]:
        items = record["countdowns"]
        if action == "add":
            _check_room(record, "countdowns", "倒数日")
            item = {
                "id": store.next_id(record),
                "title": clean_text(args.get("title"), limit=40, field="名称"),
                "date": parse_day(args.get("date") or "").isoformat(),
                "notify": bool(args.get("notify", True)),
            }
            items.append(item)
            return {"added": _countdown_view(item, today)}
        if action == "list":
            views = [_countdown_view(c, today) for c in items]
            views.sort(key=lambda v: (v["days_left"] < 0, abs(v["days_left"])))
            return {"countdowns": views}
        item_id = _require_id(args)
        item = store.find(record, "countdowns", item_id)
        if item is None:
            raise _not_found("倒数日", item_id)
        if action == "delete":
            items.remove(item)
            return {"deleted": item_id}
        raise ToolError("未知操作", code=ERROR_INVALID_ARGS)

    return store.update(user_id, run)


# ── 心情 ─────────────────────────────────────────────────


def mood(user_id: int, args: dict[str, Any], now: datetime) -> dict[str, Any]:
    action = str(args.get("action") or "history")

    def run(record: dict[str, Any]) -> dict[str, Any]:
        items = record["moods"]
        if action == "log":
            score = int(args.get("score") or 3)
            entry = {"at": fmt(now), "score": score}
            if args.get("note"):
                entry["note"] = clean_text(args["note"], limit=60, field="备注")
            items.append(entry)
            del items[: max(len(items) - store.LIMITS["moods"], 0)]
            return {"logged": {**entry, "label": MOOD_SCORES.get(score, "一般")}}
        days = int(args.get("days") or 7)
        since = fmt(now - timedelta(days=days))
        recent = [m for m in items if str(m.get("at", "")) >= since]
        if not recent:
            return {"days": days, "entries": [], "empty": True}
        avg = sum(int(m.get("score", 3)) for m in recent) / len(recent)
        half = len(recent) // 2
        trend = "持平"
        if half:
            first = sum(int(m["score"]) for m in recent[:half]) / half
            second = sum(int(m["score"]) for m in recent[half:]) / (len(recent) - half)
            if second - first >= 0.5:  # noqa: PLR2004
                trend = "在变好"
            elif first - second >= 0.5:  # noqa: PLR2004
                trend = "在变差"
        return {
            "days": days,
            "entries": [
                {**m, "label": MOOD_SCORES.get(int(m.get("score", 3)), "一般")}
                for m in recent[-10:]
            ],
            "average": round(avg, 1),
            "average_label": MOOD_SCORES.get(round(avg), "一般"),
            "trend": trend,
        }

    if action == "log":
        return store.update(user_id, run)
    return run(store.load(user_id))


# ── 聊天偏好 ─────────────────────────────────────────────


def prefs_view(prefs: dict[str, Any]) -> dict[str, Any]:
    return {
        "reply_length": prefs.get("reply_length", "normal"),
        "emoji": prefs.get("emoji", "some"),
        "call_me": prefs.get("call_me"),
        "quiet_hours": f"{prefs['quiet_start']}-{prefs['quiet_end']}"
        if prefs.get("quiet_start") and prefs.get("quiet_end")
        else None,
        "morning_greeting": prefs.get("morning_greeting"),
        "checkin_nudge": prefs.get("checkin_nudge"),
        "birthday": prefs.get("birthday"),
        "proactive": prefs.get("proactive", True),
    }


def _apply_pref(prefs: dict[str, Any], key: str, value: Any) -> None:
    off = isinstance(value, str) and value.strip().lower() in {"off", "关", "关闭", "none", ""}
    if key in {"reply_length", "emoji"}:
        prefs[key] = str(value)
    elif key == "proactive":
        prefs[key] = bool(value)
    elif key == "call_me":
        if off:
            prefs.pop(key, None)
        else:
            prefs[key] = clean_text(value, limit=16, field="称呼")
    elif key in {"quiet_start", "quiet_end", "morning_greeting", "checkin_nudge"}:
        if off:
            prefs.pop(key, None)
        else:
            prefs[key] = parse_clock(value, key)
    elif key == "birthday":
        if off:
            prefs.pop(key, None)
            return
        match = _MMDD_RE.match(str(value).strip().replace("/", "-"))
        if not match:
            raise ToolError("生日写成 MM-DD（只记月日）", code=ERROR_INVALID_ARGS)
        prefs[key] = f"{int(match.group(1)):02d}-{int(match.group(2)):02d}"


def chat_prefs(user_id: int, args: dict[str, Any], _now: datetime) -> dict[str, Any]:
    action = str(args.get("action") or "get")
    if action == "get":
        return {"prefs": prefs_view(store.load(user_id)["prefs"])}

    def run(record: dict[str, Any]) -> dict[str, Any]:
        prefs = record["prefs"]
        changed = []
        for key in PREF_FIELDS:
            if key in args:
                _apply_pref(prefs, key, args[key])
                changed.append(key)
        if not changed:
            raise ToolError("没有要改的设置", code=ERROR_INVALID_ARGS, hint="传要改的字段")
        if bool(prefs.get("quiet_start")) != bool(prefs.get("quiet_end")):
            raise ToolError("免打扰要同时给开始和结束时间", code=ERROR_INVALID_ARGS)
        return {"changed": changed, "prefs": prefs_view(prefs)}

    return store.update(user_id, run)


def in_quiet_hours(prefs: dict[str, Any], now: datetime) -> bool:
    start, end = prefs.get("quiet_start"), prefs.get("quiet_end")
    if not start or not end:
        return False
    clock = f"{now:%H:%M}"
    if start <= end:
        return start <= clock < end
    return clock >= start or clock < end


def quiet_until(prefs: dict[str, Any], now: datetime) -> datetime:
    """免打扰结束的时刻（调用方先确认正在免打扰）。"""

    hour, minute = (int(x) for x in str(prefs["quiet_end"]).split(":"))
    end = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if end <= now:
        end += timedelta(days=1)
    return end


# ── 给提示词的摘要 ───────────────────────────────────────


def prompt_digest(record: dict[str, Any], now: datetime) -> dict[str, Any]:
    """私聊上下文里的个人助理摘要：偏好、今天/逾期待办、24 小时内的提醒、近的倒数日、生日。"""

    today = now.date()
    digest: dict[str, Any] = {}
    prefs = record.get("prefs") or {}
    style: dict[str, Any] = {}
    if prefs.get("reply_length") in {"short", "long"}:
        style["reply_length"] = {"short": "尽量简短", "long": "可以多说一些"}[prefs["reply_length"]]
    if prefs.get("emoji") in {"none", "lots"}:
        style["emoji"] = {"none": "不要用表情符号", "lots": "可以多用表情符号"}[prefs["emoji"]]
    if prefs.get("call_me"):
        style["call_me"] = prefs["call_me"]
    if style:
        digest["chat_style"] = style
    if prefs.get("birthday") and prefs["birthday"] == f"{today:%m-%d}":
        digest["today_is_birthday"] = True
    pending = [t for t in record.get("todos", []) if not t.get("done")]
    urgent = [t for t in pending if t.get("due") and t["due"] <= today.isoformat()]
    if pending:
        digest["todos"] = {
            "pending": len(pending),
            "due_today_or_overdue": [t["content"] for t in sorted(urgent, key=_todo_key)[:3]],
        }
    soon = fmt(now + timedelta(hours=24))
    upcoming = sorted(
        (r for r in record.get("reminders", []) if r.get("due", "") <= soon),
        key=lambda r: r["due"],
    )
    if upcoming:
        digest["reminders_next_24h"] = [f"{r['due'][5:]} {r['content']}" for r in upcoming[:3]]
    near = []
    for item in record.get("countdowns", []):
        try:
            days = (parse_day(item["date"]) - today).days
        except ToolError:
            continue
        if 0 <= days <= 7:  # noqa: PLR2004
            near.append(f"{item['title']}：{'就是今天' if days == 0 else f'还有 {days} 天'}")
    if near:
        digest["countdowns_this_week"] = near[:3]
    return digest


__all__ = [
    "MOOD_SCORES",
    "PREF_FIELDS",
    "PRIORITIES",
    "REPEATS",
    "chat_prefs",
    "clean_text",
    "countdowns",
    "fmt",
    "in_quiet_hours",
    "mood",
    "next_occurrence",
    "notes",
    "parse_time",
    "prefs_view",
    "prompt_digest",
    "quiet_until",
    "reminders",
    "todos",
]
