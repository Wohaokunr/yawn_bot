# ruff: noqa: E501, C901, PLR0912, TRY003, PLR2004, PERF203
"""私聊提醒（个人助理）的时间规则与运行状态：和群定时任务用同一个规则引擎。

私聊提醒仍然存在每人的个人数据 JSON（``private_store`` 的 ``reminders``），不建表、
无迁移；但规则不再只有“一次/每天/工作日/每周”四种，而是 ``schedule_spec`` 的全部
规则（单次、间隔、每天/每周/每月的几个钟点、每年某天、cron、时段、起止、跳过日期、
总次数）。旧数据只有 ``repeat`` 字段时按原语义换算，读写都兼容，不需要迁移。

一条提醒的字段（新字段都可选）：

- ``id`` / ``content`` / ``due``（下一次，``YYYY-MM-DD HH:MM``）/ ``created``；
- ``schedule``：规范化后的规则 JSON（与 ``AgentScheduledTask.schedule`` 同形）；
  ``anchor``：间隔规则的起算点；``rule``：中文描述；
- ``mode``：``notify`` 到点发这句提醒（默认）、``agent`` 到点由私聊的我去执行；
- ``max_runs`` / ``runs``、``paused``、``base``（被“稍后提醒”推迟时原本的时间，
  下一次从它往后算，避免整条循环被推迟）、``retry``（发送失败后的重试计划）；
- ``origin``：在哪设的（``private``、``group``（带 ``group_id``/``group_name``）、``webui``）；
- ``history``：最近几次运行结果（新的在前）。

这里全是纯函数（原地改传入的记录），读写锁由调用方 ``private_store.update`` 提供。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .schedule_spec import (
    KIND_LABELS,
    ScheduleError,
    ScheduleSpec,
    describe,
    format_datetime,
    min_gap_minutes,
    next_fire,
    normalize_spec,
    spec_from_dict,
    upcoming,
)

TIME_FMT = "%Y-%m-%d %H:%M"
MODES = ("notify", "agent")
MODE_LABELS = {"notify": "到点提醒", "agent": "到点替他去办"}
HISTORY_LIMIT = 6
RETRY_BACKOFF_MINUTES = (2, 5, 15, 30, 60)
# 错过超过这么久的一次性提醒不再补发（与原调度器一致）。
STALE_AFTER = timedelta(hours=12)
ORIGIN_LABELS = {"private": "私聊里设的", "group": "群里设的", "webui": "网页上设的"}
RESULT_LABELS = {
    "sent": "已提醒",
    "done": "已替他办",
    "fallback": "没办成，改发了提醒",
    "failed": "发送失败",
    "retry": "发送失败，稍后重试",
    "skipped_stale": "错过太久已跳过",
}
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
LEGACY_REPEATS = ("none", "daily", "weekdays", "weekly")


class RuleError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_schedule", hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint


def fmt(value: datetime) -> str:
    return value.strftime(TIME_FMT)


def parse(raw: Any) -> datetime | None:
    text = str(raw or "").strip().replace("T", " ").replace("/", "-")
    for pattern in (TIME_FMT, "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, pattern)  # noqa: DTZ007 - 北京时间 naive
        except ValueError:
            continue
    return None


# ── 规则 ────────────────────────────────────────────────────────────────


def legacy_schedule(due: datetime, repeat: str) -> dict[str, Any]:
    """旧的 ``repeat`` 换算成规则：每天/工作日/每周在 ``due`` 的钟点，一次就是单次。"""

    clock = f"{due:%H:%M}"
    if repeat == "daily":
        return {"kind": "daily", "times": [clock]}
    if repeat == "weekdays":
        return {"kind": "weekly", "times": [clock], "weekdays": [1, 2, 3, 4, 5]}
    if repeat == "weekly":
        return {"kind": "weekly", "times": [clock], "weekdays": [due.isoweekday()]}
    return {"kind": "once", "at": fmt(due)}


def schedule_from_args(args: dict[str, Any]) -> dict[str, Any]:
    """工具/网页参数 → 规则 JSON；没有 ``kind`` 时返回空（由调用方走 at/in_minutes）。"""

    data = {key: args[key] for key in _SCHEDULE_KEYS if args.get(key) not in (None, "", [])}
    if args.get("window_start") or args.get("window_end"):
        data["window"] = {"start": args.get("window_start"), "end": args.get("window_end")}
    if isinstance(args.get("schedule"), dict):
        data = {**dict(args["schedule"]), **data}
    return data if data.get("kind") else {}


def item_spec(item: dict[str, Any]) -> ScheduleSpec | None:
    if isinstance(item.get("schedule"), dict):
        return spec_from_dict(item["schedule"])
    due = parse(item.get("base") or item.get("due"))
    if due is None:
        return None
    return spec_from_dict(legacy_schedule(due, str(item.get("repeat") or "none")))


def item_anchor(item: dict[str, Any]) -> datetime | None:
    return parse(item.get("anchor") or item.get("created"))


def remaining(item: dict[str, Any]) -> int | None:
    if not item.get("max_runs"):
        return None
    return max(int(item["max_runs"]) - int(item.get("runs") or 0), 0)


def compute_next(item: dict[str, Any], after: datetime) -> datetime | None:
    """严格晚于 ``after`` 的下一次（考虑剩余次数）；一次性的或跑完了返回 None。"""

    spec = item_spec(item)
    if spec is None or not spec.recurring:
        return None
    left = remaining(item)
    if left is not None and left <= 1:
        return None
    return next_fire(spec, after, anchor=item_anchor(item))


def describe_item(item: dict[str, Any]) -> str:
    spec = item_spec(item)
    if spec is None:
        return "规则损坏"
    if not spec.recurring:
        due = parse(item.get("due"))
        return f"单次 {fmt(due)}" if due else "单次"
    return describe(spec, max_runs=item.get("max_runs"))


def build_rule(
    args: dict[str, Any],
    now: datetime,
    *,
    min_interval: int,
    max_lead: timedelta,
    min_lead: timedelta,
) -> tuple[dict[str, Any], datetime, list[datetime], str]:
    """校验参数里的时间：返回（规则 JSON、第一次、接下来几次、描述）。

    兼容旧参数：``in_minutes`` / ``at`` + ``repeat``；新参数：``kind`` + 规则字段。
    """

    raw = schedule_from_args(args)
    if not raw:
        due: datetime | None = None
        if args.get("in_minutes") is not None:
            due = now + timedelta(minutes=int(args["in_minutes"]))
        elif args.get("at"):
            due = parse(args["at"])
            if due is None:
                try:
                    due = normalize_spec({"kind": "once", "at": args["at"]}, now=now).at
                except ScheduleError as exc:
                    raise RuleError(
                        f"时间「{args['at']}」看不懂",
                        code="bad_datetime",
                        hint="at 用 YYYY-MM-DD HH:MM（北京时间），相对时间用 in_minutes",
                    ) from exc
        else:
            raise RuleError("要给提醒时间", code="missing_field", hint="at（YYYY-MM-DD HH:MM）或 in_minutes 二选一；循环规则用 kind")
        if due is None:
            raise RuleError("时间看不懂", code="bad_datetime", hint="at 用 YYYY-MM-DD HH:MM")
        repeat = str(args.get("repeat") or "none")
        if repeat not in LEGACY_REPEATS:
            raise RuleError("repeat 只能是 none/daily/weekdays/weekly", code="bad_kind")
        if due - now < min_lead - timedelta(seconds=1):
            raise RuleError("提醒时间已经过了", code="never_fires", hint="确认日期，或用 in_minutes")
        raw = legacy_schedule(due, repeat)
        if repeat != "none":
            natural = spec_from_dict(raw)
            first = next_fire(natural, now, anchor=now) if natural is not None else None
            if first is not None and first < due - timedelta(minutes=1):
                # “11 月 10 日起每天 9 点”：从给的那天开始，不提前。
                raw["start_at"] = fmt(due)
        if repeat == "none":
            # 单次：直接用 due（可能带秒数取整）。
            return raw, due, [due], f"单次 {fmt(due)}"
    try:
        spec = normalize_spec(raw, now=now)
    except ScheduleError as exc:
        raise RuleError(str(exc), code=exc.code, hint=exc.hint) from exc
    max_runs = int(args["max_runs"]) if args.get("max_runs") else None
    if not spec.recurring and spec.at is not None:
        if spec.at - now < min_lead - timedelta(seconds=1):
            raise RuleError("提醒时间已经过了", code="never_fires", hint="确认日期，或用 in_minutes")
        if spec.at - now > max_lead:
            raise RuleError("最远只能提醒一年以内的事", code="too_far")
        return spec.as_dict(), spec.at, [spec.at], f"单次 {fmt(spec.at)}"
    fires = upcoming(spec, now, count=5, anchor=now, remaining_runs=max_runs)
    if not fires:
        raise RuleError("按这个规则以后不会再触发", code="never_fires", hint="检查起止时间和日期")
    if fires[0] - now > max_lead:
        raise RuleError("第一次提醒超过一年以后了", code="too_far")
    gap = min_gap_minutes(spec, now, anchor=now)
    if gap is not None and gap < min_interval:
        raise RuleError(
            f"太频繁了：两次之间最短只有 {gap:g} 分钟，私聊提醒至少隔 {min_interval} 分钟",
            code="too_frequent",
            hint=f"把间隔调到 {min_interval} 分钟以上",
        )
    return spec.as_dict(), fires[0], fires, describe(spec, max_runs=max_runs)


# ── 视图 ────────────────────────────────────────────────────────────────


def origin_of(item: dict[str, Any]) -> dict[str, Any]:
    raw = item.get("origin")
    return raw if isinstance(raw, dict) else {}


def origin_label(item: dict[str, Any]) -> str:
    origin = origin_of(item)
    kind = str(origin.get("kind") or "private")
    label = ORIGIN_LABELS.get(kind, ORIGIN_LABELS["private"])
    if kind == "group" and origin.get("group_name"):
        label = f"在「{origin['group_name']}」里设的"
    return label


def status_of(item: dict[str, Any]) -> str:
    if item.get("paused"):
        return "paused"
    if item.get("broken"):
        return "failed"
    return "active"


def view(item: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    """给模型看的紧凑视图（兼容旧字段 id/content/due/repeat）。"""

    data: dict[str, Any] = {
        "id": item["id"],
        "ref": f"p{item['id']}",
        "content": item.get("content"),
        "due": item.get("due"),
        "repeat": describe_item(item),
    }
    mode = str(item.get("mode") or "notify")
    if mode != "notify":
        data["mode"] = MODE_LABELS.get(mode, mode)
    if item.get("paused"):
        data["paused"] = True
    if item.get("max_runs"):
        data["runs"] = f"{int(item.get('runs') or 0)}/{int(item['max_runs'])}"
    elif item.get("runs"):
        data["runs"] = int(item["runs"])
    if origin_of(item).get("kind") not in (None, "private"):
        data["set_from"] = origin_label(item)
    if isinstance(item.get("retry"), dict):
        data["retrying_at"] = item["retry"].get("at")
    if detail:
        spec = item_spec(item)
        if spec is not None and spec.recurring and not item.get("paused"):
            start = parse(item.get("due"))
            if start is not None:
                rest = upcoming(spec, start, count=4, anchor=item_anchor(item), remaining_runs=None if remaining(item) is None else max((remaining(item) or 0) - 1, 0))
                data["upcoming"] = [fmt(start), *(fmt(when) for when in rest)]
        data["recent_runs"] = [
            {"at": row.get("at"), "result": RESULT_LABELS.get(str(row.get("result")), row.get("result"))}
            for row in list(item.get("history") or [])[:3]
            if isinstance(row, dict)
        ]
    return data


def push_history(item: dict[str, Any], entry: dict[str, Any]) -> None:
    rows = [row for row in list(item.get("history") or []) if isinstance(row, dict)]
    item["history"] = [entry, *rows][:HISTORY_LIMIT]


# ── 到点 ────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Firing:
    """一次到点：调度器据此发提醒或唤醒私聊回合，再用 :func:`finish` 写回结果。"""

    item_id: int
    content: str
    mode: str
    planned: datetime
    late: timedelta
    attempt: int = 0
    title: str = ""
    origin: str = ""
    last: bool = False


def late_note(late: timedelta) -> str:
    if late < timedelta(minutes=5):
        return ""
    minutes = int(late.total_seconds() // 60)
    span = f"{minutes // 60} 小时" if minutes >= 60 else f"{minutes} 分钟"
    return f"（抱歉晚了 {span}）"


def notify_text(firing: Firing) -> str:
    return f"⏰ 提醒{late_note(firing.late)}：{firing.content}"


def collect_due(record: dict[str, Any], now: datetime) -> list[Firing]:
    """原地推进到期的提醒，返回这次要执行的；发送结果由 :func:`finish` 写回。

    - 一次性提醒到点后先标 ``done``（发成功再删，失败可以重试）；错过超过
      ``STALE_AFTER`` 的直接丢弃；
    - 循环提醒推进到下一次（从“现在”往后算，停机错过的中间几次不连环补发）；
    - 暂停的不动；有重试计划的到点再试。
    """

    firings: list[Firing] = []
    keep: list[dict[str, Any]] = []
    for item in record.get("reminders", []):
        if not isinstance(item, dict):
            continue
        retry = item.get("retry") if isinstance(item.get("retry"), dict) else None
        if retry is not None:
            at = parse(retry.get("at"))
            if at is not None and at <= now and not item.get("paused"):
                planned = parse(retry.get("planned")) or at
                firings.append(_firing(item, planned, now, attempt=int(retry.get("attempt") or 1), last=bool(item.get("done"))))
                item["retry"] = {**retry, "at": fmt(now + timedelta(minutes=30)), "in_flight": True}
            keep.append(item)
            continue
        if item.get("done") or item.get("paused"):
            keep.append(item)
            continue
        due = parse(item.get("due"))
        if due is None:
            # 坏数据不再悄悄删掉：标出来，网页和列表里看得到。
            item["broken"] = True
            keep.append(item)
            continue
        if due > now:
            keep.append(item)
            continue
        spec = item_spec(item)
        recurring = bool(spec is not None and spec.recurring)
        if not recurring and now - due > STALE_AFTER:
            push_history(item, {"at": fmt(now), "planned": fmt(due), "result": "skipped_stale"})
            _log(record, item, now, "skipped_stale")
            continue
        nxt = compute_next(item, now) if recurring else None
        firings.append(_firing(item, due, now, last=nxt is None))
        item.pop("base", None)
        if nxt is not None:
            item["due"] = fmt(nxt)
        else:
            item["done"] = True
        keep.append(item)
    record["reminders"] = keep
    return firings


def _firing(item: dict[str, Any], planned: datetime, now: datetime, *, attempt: int = 0, last: bool = False) -> Firing:
    origin = origin_of(item)
    return Firing(
        item_id=int(item["id"]),
        content=str(item.get("content") or ""),
        mode=str(item.get("mode") or "notify"),
        planned=planned,
        late=max(now - planned, timedelta(0)),
        attempt=attempt,
        title=str(item.get("title") or ""),
        origin=str(origin.get("kind") or "private"),
        last=last,
    )


def _log(record: dict[str, Any], item: dict[str, Any], now: datetime, result: str) -> None:
    """个人数据里留最近 30 次运行（网页运行记录时间线用，不含别人的东西）。"""

    flags = record.setdefault("flags", {})
    rows = [row for row in list(flags.get("reminder_log") or []) if isinstance(row, dict)]
    content = str(item.get("content") or "")
    flags["reminder_log"] = [
        {"at": fmt(now), "id": item.get("id"), "content": content[:40], "result": result, "mode": item.get("mode") or "notify"},
        *rows,
    ][:30]


def finish(record: dict[str, Any], firing: Firing, result: str, now: datetime, *, retry_limit: int) -> str:
    """写回一次执行结果；返回最终结果（失败且还能重试时是 ``retry``）。"""

    item = next((row for row in record.get("reminders", []) if int(row.get("id") or 0) == firing.item_id), None)
    if item is None:
        return result
    final = result
    if result in {"sent", "done", "fallback"}:
        item["runs"] = int(item.get("runs") or 0) + 1
        item.pop("retry", None)
        item["last_run"] = fmt(now)
    else:
        attempt = firing.attempt + 1
        if attempt <= retry_limit:
            backoff = RETRY_BACKOFF_MINUTES[min(attempt, len(RETRY_BACKOFF_MINUTES)) - 1]
            item["retry"] = {"at": fmt(now + timedelta(minutes=backoff)), "attempt": attempt, "planned": fmt(firing.planned)}
            final = "retry"
        else:
            item.pop("retry", None)
    item["last_result"] = final
    push_history(
        item,
        {"at": fmt(now), "planned": fmt(firing.planned), "result": final, **({"attempt": firing.attempt} if firing.attempt else {})},
    )
    _log(record, item, now, final)
    if item.get("done") and "retry" not in item:
        record["reminders"] = [row for row in record.get("reminders", []) if row is not item]
    return final


# ── 改动 ────────────────────────────────────────────────────────────────


def pause(item: dict[str, Any]) -> None:
    item["paused"] = True
    item.pop("retry", None)


def resume(item: dict[str, Any], now: datetime) -> datetime | None:
    """恢复：从现在起重新算下一次；一次性的且时间已过时返回 None（调用方删掉或提示）。"""

    item.pop("paused", None)
    item.pop("broken", None)
    due = parse(item.get("due"))
    if due is not None and due > now:
        return due
    nxt = compute_next(item, now)
    if nxt is None:
        return None
    item["due"] = fmt(nxt)
    return nxt


def snooze(item: dict[str, Any], minutes: int, now: datetime) -> datetime:
    """推迟这一次：循环提醒记住原本的时间，下一次仍按原来的钟点。"""

    due = parse(item.get("due")) or now
    spec = item_spec(item)
    if spec is not None and spec.recurring and not item.get("base"):
        item["base"] = fmt(due)
    moved = max(due, now) + timedelta(minutes=minutes)
    item["due"] = fmt(moved)
    return moved


def kind_label(item: dict[str, Any]) -> str:
    spec = item_spec(item)
    return KIND_LABELS.get(spec.kind, spec.kind) if spec is not None else "损坏"


def format_moment(value: datetime | None) -> str | None:
    return format_datetime(value) if value is not None else None


__all__ = [
    "HISTORY_LIMIT",
    "MODES",
    "MODE_LABELS",
    "ORIGIN_LABELS",
    "RESULT_LABELS",
    "STALE_AFTER",
    "Firing",
    "RuleError",
    "build_rule",
    "collect_due",
    "compute_next",
    "describe_item",
    "finish",
    "fmt",
    "item_spec",
    "kind_label",
    "late_note",
    "legacy_schedule",
    "notify_text",
    "origin_label",
    "origin_of",
    "parse",
    "pause",
    "push_history",
    "remaining",
    "resume",
    "schedule_from_args",
    "snooze",
    "status_of",
    "view",
]
