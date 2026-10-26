# ruff: noqa: E501, TRY003, RUF001, RUF002, C901, PLR0912, PLR0915, PERF203, DTZ001, DTZ007
"""定时任务的时间规则：校验、规范化、计算下一次触发、中文描述（纯函数）。

规则以 JSON 存在 ``AgentScheduledTask.schedule``，形状由 ``kind`` 决定：

- ``once``：``at`` 一个具体时间（工具里的“N 分钟后”在创建时换算成 ``at``）；
- ``interval``：每 ``every_minutes`` 分钟一次，从 ``start_at``（或创建时间）起算，
  可加 ``window`` 只在每天某个时段内触发（例如 09:00–22:00 每小时喝水提醒）；
- ``daily`` / ``weekly`` / ``monthly``：一天里的若干个 ``times``，配合
  ``weekdays``（1=周一…7=周日）、``month_days``（1–31，-1=最后一天）、
  ``months``（只在这些月份，配合 monthly 就是“每年 X 月 X 日”）；
- ``cron``：标准 5 段 cron（分 时 日 月 周，周按 cron 习惯 0/7=周日），
  也认 ``@hourly`` / ``@daily`` / ``@weekly`` / ``@monthly`` / ``@yearly``。

所有规则都可以再加 ``start_at`` / ``end_at`` / ``skip_dates``（跳过的具体日期，
比如节假日）；``max_runs`` 由调用方结合已运行次数判断。

时间一律是 naive 北京时间（与数据表约定一致），内部计算交给 APScheduler 的
trigger，时区固定 Asia/Shanghai。这里不碰数据库、不读配置，方便测试与 WebUI
预览共用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger

TIMEZONE = ZoneInfo("Asia/Shanghai")
KINDS = ("once", "interval", "daily", "weekly", "monthly", "cron")
KIND_LABELS = {
    "once": "单次",
    "interval": "固定间隔",
    "daily": "每天",
    "weekly": "每周",
    "monthly": "每月",
    "cron": "cron 表达式",
}
MAX_TIMES = 8
MAX_SKIP_DATES = 60
MAX_INTERVAL_MINUTES = 60 * 24 * 31
# 下一次触发最远只算到这么远；更远的规则（cron 写成 2 月 30 日）视为永不触发。
MAX_HORIZON_DAYS = 366 * 2
_MAX_ITERATIONS = 4000
_WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_LABELS = ("一", "二", "三", "四", "五", "六", "日")
_CRON_DOW_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat", "sun")
_CRON_ALIASES = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}
_TIME_RE = re.compile(r"^(\d{1,2})[:：](\d{2})$")
_CRON_FIELD_RE = re.compile(r"^[\d*,/\-a-zA-Z?]+$")
_DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y/%m/%d %H:%M",
)


class ScheduleError(ValueError):
    """规则不合法；``code`` 给机器看，``hint`` 告诉模型/管理员怎么改。"""

    def __init__(self, message: str, *, code: str = "invalid_schedule", hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint


@dataclass(frozen=True, slots=True)
class ScheduleSpec:
    """规范化后的规则；``as_dict`` 是写进数据库的形状。"""

    kind: str
    at: datetime | None = None
    every_minutes: int | None = None
    times: tuple[str, ...] = ()
    weekdays: tuple[int, ...] = ()
    month_days: tuple[int, ...] = ()
    months: tuple[int, ...] = ()
    cron: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    skip_dates: tuple[str, ...] = ()

    @property
    def recurring(self) -> bool:
        return self.kind != "once"

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind}
        if self.at is not None:
            data["at"] = format_datetime(self.at)
        if self.every_minutes is not None:
            data["every_minutes"] = self.every_minutes
        if self.times:
            data["times"] = list(self.times)
        if self.weekdays:
            data["weekdays"] = list(self.weekdays)
        if self.month_days:
            data["month_days"] = list(self.month_days)
        if self.months:
            data["months"] = list(self.months)
        if self.cron:
            data["cron"] = self.cron
        if self.window_start and self.window_end:
            data["window"] = {"start": self.window_start, "end": self.window_end}
        if self.start_at is not None:
            data["start_at"] = format_datetime(self.start_at)
        if self.end_at is not None:
            data["end_at"] = format_datetime(self.end_at)
        if self.skip_dates:
            data["skip_dates"] = list(self.skip_dates)
        return data


def format_datetime(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M")


def _strip_seconds(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0, tzinfo=None)


def parse_datetime(value: Any, *, now: datetime, field: str = "at") -> datetime:
    """接受 ``YYYY-MM-DD HH:MM``（含 T、秒、斜杠）或只给 ``HH:MM``（下一次出现）。"""

    if isinstance(value, datetime):
        return _strip_seconds(value)
    text = str(value or "").strip()
    if not text:
        raise ScheduleError(f"{field} 不能为空", code="missing_field", hint=f"{field} 写成 YYYY-MM-DD HH:MM")
    clock = _TIME_RE.match(text)
    if clock:
        hour, minute = _parse_clock(text, field=field)
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    for fmt in _DATETIME_FORMATS:
        try:
            return _strip_seconds(datetime.strptime(text, fmt))
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ScheduleError(
            f"{field} 不是有效时间：{text[:40]}",
            code="bad_datetime",
            hint=f"{field} 写成 YYYY-MM-DD HH:MM（北京时间），或只写 HH:MM 表示下一次到这个钟点",
        ) from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(TIMEZONE)
    return _strip_seconds(parsed)


def _parse_clock(value: Any, *, field: str = "times") -> tuple[int, int]:
    match = _TIME_RE.match(str(value or "").strip())
    if not match:
        raise ScheduleError(f"{field} 里的时间不合法：{str(value)[:20]}", code="bad_time", hint="时间写成 24 小时制 HH:MM，例如 08:30")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour == 24 and minute == 0:  # noqa: PLR2004
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):  # noqa: PLR2004
        raise ScheduleError(f"{field} 里的时间超出范围：{value}", code="bad_time", hint="小时 0–23，分钟 0–59")
    return hour, minute


def _clock_text(value: Any, *, field: str = "times") -> str:
    hour, minute = _parse_clock(value, field=field)
    return f"{hour:02d}:{minute:02d}"


def _int_list(value: Any, *, field: str, low: int, high: int, allow: frozenset[int] = frozenset()) -> tuple[int, ...]:
    if value in (None, "", []):
        return ()
    items = value if isinstance(value, (list, tuple)) else re.split(r"[,，、\s]+", str(value).strip())
    result: list[int] = []
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        try:
            number = int(text)
        except ValueError as exc:
            raise ScheduleError(f"{field} 只能是整数：{text[:12]}", code="bad_number", hint=f"{field} 取值 {low}–{high}") from exc
        if number not in allow and not (low <= number <= high):
            raise ScheduleError(f"{field} 超出范围：{number}", code="bad_number", hint=f"{field} 取值 {low}–{high}")
        if number not in result:
            result.append(number)
    return tuple(sorted(result, key=lambda item: (item < 0, item)))


def _cron_dow_token(token: str) -> str:
    """标准 cron 的周字段（0/7=周日、1=周一）→ APScheduler 的英文缩写。

    APScheduler 3 的数字周是 0=周一，直接 ``from_crontab`` 会把“1-5”错成周二到周六。
    """

    def name(part: str) -> str:
        if part.isdigit():
            number = int(part)
            if not 0 <= number <= 7:  # noqa: PLR2004
                raise ScheduleError(f"cron 周字段超出范围：{part}", code="bad_cron", hint="周字段 0–7（0 和 7 都是周日）或 mon–sun")
            return _CRON_DOW_NAMES[number]
        return part.lower()

    parts: list[str] = []
    for chunk in token.split(","):
        base, _, step = chunk.partition("/")
        if base in {"*", "?"}:
            converted = "*"
        elif "-" in base:
            start, _, end = base.partition("-")
            start_name, end_name = name(start), name(end)
            if start_name == "sun" and start == "0" and end_name != "sun":
                # 0-5 这种从周日开始的区间，拆成 sun + mon-end。
                parts.append("sun")
                converted = f"mon-{end_name}"
            elif end_name == "sun" and end == "7":
                converted = f"{start_name}-sun" if start_name != "sun" else "sun"
            else:
                converted = f"{start_name}-{end_name}"
        else:
            converted = name(base)
        parts.append(f"{converted}/{step}" if step else converted)
    return ",".join(dict.fromkeys(parts))


def normalize_cron(expression: Any) -> str:
    text = " ".join(str(expression or "").strip().split())
    if not text:
        raise ScheduleError("cron 表达式不能为空", code="bad_cron", hint="写成 5 段：分 时 日 月 周，例如 0 9 * * 1-5")
    text = _CRON_ALIASES.get(text.lower(), text)
    fields = text.split(" ")
    if len(fields) != 5:  # noqa: PLR2004
        raise ScheduleError(f"cron 需要 5 段，收到 {len(fields)} 段", code="bad_cron", hint="分 时 日 月 周，例如 30 8 * * 1-5 表示工作日 08:30")
    if any(not _CRON_FIELD_RE.match(item) for item in fields):
        raise ScheduleError("cron 含有非法字符", code="bad_cron", hint="只能用数字、*、逗号、-、/ 和英文缩写")
    _build_cron_trigger(text)
    return text


def _build_cron_trigger(text: str) -> CronTrigger:
    minute, hour, day, month, dow = text.split(" ")
    try:
        return CronTrigger(
            minute=minute,
            hour=hour,
            day=day.replace("?", "*"),
            month=month,
            day_of_week=_cron_dow_token(dow),
            timezone=TIMEZONE,
        )
    except ScheduleError:
        raise
    except (ValueError, TypeError) as exc:
        raise ScheduleError(f"cron 表达式不合法：{exc}", code="bad_cron", hint="分 时 日 月 周，例如 0 9 * * 1-5") from exc


def _window(raw: Any) -> tuple[str | None, str | None]:
    if not raw:
        return None, None
    if isinstance(raw, str):
        start, sep, end = raw.replace("～", "-").replace("~", "-").replace("–", "-").partition("-")
        if not sep:
            raise ScheduleError("window 写成 HH:MM-HH:MM", code="bad_window", hint="例如 09:00-22:00")
        raw = {"start": start, "end": end}
    if not isinstance(raw, dict):
        raise ScheduleError("window 必须是 {start, end}", code="bad_window", hint='例如 {"start": "09:00", "end": "22:00"}')
    start = _clock_text(raw.get("start"), field="window.start")
    end = _clock_text(raw.get("end"), field="window.end")
    if start == end:
        raise ScheduleError("window 的开始和结束不能相同", code="bad_window", hint="全天触发就不要写 window")
    return start, end


def _skip_dates(raw: Any) -> tuple[str, ...]:
    if raw in (None, "", []):
        return ()
    items = raw if isinstance(raw, (list, tuple)) else re.split(r"[,，、\s]+", str(raw))
    result: list[str] = []
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        try:
            parsed = date.fromisoformat(text.replace("/", "-"))
        except ValueError as exc:
            raise ScheduleError(f"skip_dates 里的日期不合法：{text[:20]}", code="bad_date", hint="写成 YYYY-MM-DD") from exc
        result.append(parsed.isoformat())
    if len(result) > MAX_SKIP_DATES:
        raise ScheduleError(f"skip_dates 最多 {MAX_SKIP_DATES} 个", code="too_many_dates")
    return tuple(sorted(set(result)))


def normalize_spec(raw: dict[str, Any], *, now: datetime) -> ScheduleSpec:
    """把模型/WebUI 给的规则规范化；任何不合法输入都抛 :class:`ScheduleError`。

    ``delay_minutes`` 是“N 分钟后”的便捷写法，会换算成 ``once`` + ``at``。
    """

    if not isinstance(raw, dict):
        raise ScheduleError("schedule 必须是对象", code="invalid_schedule")
    data = dict(raw)
    kind = str(data.get("kind") or "").strip().lower()
    if not kind and data.get("delay_minutes") not in (None, ""):
        kind = "once"
    if kind not in KINDS:
        raise ScheduleError(
            f"不认识的规则类型：{kind or '(空)'}",
            code="bad_kind",
            hint="kind 取 once/interval/daily/weekly/monthly/cron",
        )
    now = _strip_seconds(now)
    start_at = parse_datetime(data["start_at"], now=now, field="start_at") if data.get("start_at") else None
    end_at = parse_datetime(data["end_at"], now=now, field="end_at") if data.get("end_at") else None
    if start_at and end_at and end_at <= start_at:
        raise ScheduleError("end_at 必须晚于 start_at", code="bad_range")
    skip = _skip_dates(data.get("skip_dates"))
    times: tuple[str, ...] = ()
    if kind in {"daily", "weekly", "monthly"}:
        raw_times = data.get("times")
        if raw_times in (None, "", []) and data.get("time"):
            raw_times = [data.get("time")]
        if isinstance(raw_times, str):
            raw_times = re.split(r"[,，、\s]+", raw_times.strip())
        times = tuple(sorted({_clock_text(item) for item in (raw_times or []) if str(item).strip()}))
        if not times:
            raise ScheduleError(f"{KIND_LABELS[kind]}规则需要 times", code="missing_field", hint='times 写成 ["08:00"]，可以给多个钟点')
        if len(times) > MAX_TIMES:
            raise ScheduleError(f"times 最多 {MAX_TIMES} 个", code="too_many_times")
    months = _int_list(data.get("months"), field="months", low=1, high=12)
    if kind == "once":
        if data.get("at"):
            at = parse_datetime(data["at"], now=now)
        elif data.get("delay_minutes") not in (None, ""):
            try:
                delay = int(data["delay_minutes"])
            except (TypeError, ValueError) as exc:
                raise ScheduleError("delay_minutes 必须是整数", code="bad_number") from exc
            if not 1 <= delay <= MAX_INTERVAL_MINUTES * 12:
                raise ScheduleError("delay_minutes 超出范围", code="bad_number", hint="1 分钟到一年之内")
            at = now + timedelta(minutes=delay)
        else:
            raise ScheduleError("单次任务需要 at 或 delay_minutes", code="missing_field", hint="at 写成 YYYY-MM-DD HH:MM，或用 delay_minutes 表示 N 分钟后")
        return ScheduleSpec(kind="once", at=at, skip_dates=())
    if kind == "interval":
        try:
            every = int(data.get("every_minutes") or 0)
        except (TypeError, ValueError) as exc:
            raise ScheduleError("every_minutes 必须是整数", code="bad_number") from exc
        if not 1 <= every <= MAX_INTERVAL_MINUTES:
            raise ScheduleError("every_minutes 超出范围", code="bad_number", hint=f"1 到 {MAX_INTERVAL_MINUTES} 分钟")
        window_start, window_end = _window(data.get("window"))
        return ScheduleSpec(
            kind="interval",
            every_minutes=every,
            window_start=window_start,
            window_end=window_end,
            start_at=start_at,
            end_at=end_at,
            skip_dates=skip,
        )
    if kind == "daily":
        return ScheduleSpec(kind="daily", times=times, months=months, start_at=start_at, end_at=end_at, skip_dates=skip)
    if kind == "weekly":
        weekdays = _int_list(data.get("weekdays"), field="weekdays", low=1, high=7)
        if not weekdays:
            raise ScheduleError("每周规则需要 weekdays", code="missing_field", hint="weekdays 用 1–7 表示周一到周日，例如 [1,3,5]")
        return ScheduleSpec(kind="weekly", times=times, weekdays=weekdays, months=months, start_at=start_at, end_at=end_at, skip_dates=skip)
    if kind == "monthly":
        month_days = _int_list(data.get("month_days"), field="month_days", low=1, high=31, allow=frozenset({-1}))
        if not month_days:
            raise ScheduleError("每月规则需要 month_days", code="missing_field", hint="month_days 用 1–31，-1 表示每月最后一天")
        return ScheduleSpec(kind="monthly", times=times, month_days=month_days, months=months, start_at=start_at, end_at=end_at, skip_dates=skip)
    cron = normalize_cron(data.get("cron"))
    return ScheduleSpec(kind="cron", cron=cron, start_at=start_at, end_at=end_at, skip_dates=skip)


def spec_from_dict(data: dict[str, Any] | None) -> ScheduleSpec | None:
    """从数据库 JSON 还原；坏数据返回 None（调用方据此暂停任务而不是崩溃）。"""

    if not isinstance(data, dict):
        return None
    try:
        return normalize_spec(data, now=datetime(2000, 1, 1))
    except ScheduleError:
        return None


def _cron_triggers(spec: ScheduleSpec) -> CronTrigger | OrTrigger:
    month = ",".join(str(item) for item in spec.months) or None
    triggers: list[CronTrigger] = []
    if spec.kind == "cron" and spec.cron:
        return _build_cron_trigger(spec.cron)
    for clock in spec.times:
        hour, minute = (int(part) for part in clock.split(":"))
        kwargs: dict[str, Any] = {"hour": hour, "minute": minute, "timezone": TIMEZONE}
        if month:
            kwargs["month"] = month
        if spec.kind == "weekly":
            kwargs["day_of_week"] = ",".join(_WEEKDAY_NAMES[item - 1] for item in spec.weekdays)
        if spec.kind == "monthly":
            kwargs["day"] = ",".join("last" if item == -1 else str(item) for item in spec.month_days)
        triggers.append(CronTrigger(**kwargs))
    return triggers[0] if len(triggers) == 1 else OrTrigger(triggers)


def _in_window(moment: datetime, spec: ScheduleSpec) -> bool:
    if not (spec.window_start and spec.window_end):
        return True
    clock = moment.strftime("%H:%M")
    if spec.window_start < spec.window_end:
        return spec.window_start <= clock <= spec.window_end
    # 跨午夜的时段，例如 22:00-02:00。
    return clock >= spec.window_start or clock <= spec.window_end


def _raw_next(spec: ScheduleSpec, after: datetime, *, anchor: datetime) -> datetime | None:
    if spec.kind == "once":
        return spec.at if spec.at is not None and spec.at > after else None
    if spec.kind == "interval":
        every = timedelta(minutes=int(spec.every_minutes or 1))
        base = spec.start_at or anchor
        if after < base:
            return base
        steps = int((after - base) / every) + 1
        return base + steps * every
    trigger = _cron_triggers(spec)
    probe = (after + timedelta(seconds=1)).replace(tzinfo=TIMEZONE)
    fire = trigger.get_next_fire_time(None, probe)
    if fire is None:
        return None
    return fire.astimezone(TIMEZONE).replace(tzinfo=None)


def next_fire(spec: ScheduleSpec, after: datetime, *, anchor: datetime | None = None) -> datetime | None:
    """严格晚于 ``after`` 的下一次触发时间；规则已结束或太远时返回 None。

    ``anchor`` 是间隔规则的起算点（没写 start_at 时用任务创建时间）。
    """

    after = _strip_seconds(after) if after.second or after.microsecond else after.replace(tzinfo=None)
    anchor = _strip_seconds(anchor or after)
    if spec.start_at is not None and after < spec.start_at - timedelta(minutes=1) and spec.kind != "interval":
        after = spec.start_at - timedelta(minutes=1)
    horizon = after + timedelta(days=MAX_HORIZON_DAYS)
    skip = set(spec.skip_dates)
    cursor = after
    for _ in range(_MAX_ITERATIONS):
        candidate = _raw_next(spec, cursor, anchor=anchor)
        if candidate is None or candidate > horizon:
            return None
        if spec.end_at is not None and candidate > spec.end_at:
            return None
        if (spec.start_at is None or candidate >= spec.start_at) and candidate.date().isoformat() not in skip and _in_window(candidate, spec):
            return candidate
        cursor = candidate
    return None


def upcoming(spec: ScheduleSpec, after: datetime, *, count: int = 5, anchor: datetime | None = None, remaining_runs: int | None = None) -> list[datetime]:
    """接下来 ``count`` 次触发时间（受剩余次数限制），给预览和最小间隔检查用。"""

    limit = count if remaining_runs is None else max(0, min(count, remaining_runs))
    result: list[datetime] = []
    cursor = after
    while len(result) < limit:
        fire = next_fire(spec, cursor, anchor=anchor)
        if fire is None:
            break
        result.append(fire)
        cursor = fire
    return result


def min_gap_minutes(spec: ScheduleSpec, after: datetime, *, anchor: datetime | None = None, samples: int = 40) -> float | None:
    """前若干次触发之间的最小间隔（分钟）；不足两次时返回 None。"""

    if spec.kind == "interval" and not spec.window_start:
        return float(spec.every_minutes or 0)
    fires = upcoming(spec, after, count=samples, anchor=anchor)
    if len(fires) < 2:  # noqa: PLR2004
        return None
    return min((later - earlier).total_seconds() / 60 for earlier, later in pairwise(fires))


def _join(items: list[str]) -> str:
    return "、".join(items)


def _weekday_text(weekdays: tuple[int, ...]) -> str:
    days = set(weekdays)
    if days == {1, 2, 3, 4, 5}:
        return "工作日（周一到周五）"
    if days == {6, 7}:
        return "周末"
    if days == set(range(1, 8)):
        return "每天"
    return "每周" + _join([WEEKDAY_LABELS[item - 1] for item in weekdays])


def _every_text(minutes: int) -> str:
    if minutes % 1440 == 0:
        return f"每 {minutes // 1440} 天"
    if minutes % 60 == 0:
        return "每小时" if minutes == 60 else f"每 {minutes // 60} 小时"  # noqa: PLR2004
    return f"每 {minutes} 分钟"


def describe(spec: ScheduleSpec, *, max_runs: int | None = None) -> str:
    """给人看的一句话中文描述。"""

    months = f"（仅 {_join([f'{item}月' for item in spec.months])}）" if spec.months else ""
    times = _join(list(spec.times))
    if spec.kind == "once":
        text = f"{format_datetime(spec.at)} 一次" if spec.at else "单次"
    elif spec.kind == "interval":
        text = _every_text(int(spec.every_minutes or 0))
        if spec.window_start and spec.window_end:
            text += f"（每天 {spec.window_start}–{spec.window_end}）"
    elif spec.kind == "daily":
        text = f"每天 {times}{months}"
    elif spec.kind == "weekly":
        text = f"{_weekday_text(spec.weekdays)} {times}{months}"
    elif spec.kind == "monthly":
        days = _join(["最后一天" if item == -1 else f"{item}日" for item in spec.month_days])
        text = (f"每年 {_join([f'{item}月' for item in spec.months])}{days}" if spec.months else f"每月 {days}") + f" {times}"
    else:
        text = f"cron「{spec.cron}」"
    extras: list[str] = []
    if spec.start_at is not None and spec.kind != "once":
        extras.append(f"{format_datetime(spec.start_at)} 起")
    if spec.end_at is not None:
        extras.append(f"到 {format_datetime(spec.end_at)} 为止")
    if max_runs:
        extras.append(f"共 {max_runs} 次")
    if spec.skip_dates:
        extras.append(f"跳过 {len(spec.skip_dates)} 个日期")
    return text + (f"，{'，'.join(extras)}" if extras else "")


__all__ = [
    "KINDS",
    "KIND_LABELS",
    "MAX_HORIZON_DAYS",
    "TIMEZONE",
    "ScheduleError",
    "ScheduleSpec",
    "describe",
    "format_datetime",
    "min_gap_minutes",
    "next_fire",
    "normalize_cron",
    "normalize_spec",
    "parse_datetime",
    "spec_from_dict",
    "upcoming",
]
