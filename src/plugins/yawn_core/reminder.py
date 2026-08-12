"""群聊定时提醒。

提醒配置持久化在 ORM 中，具体的触发由
nonebot_plugin_apscheduler 提供的全局调度器负责。
调度任务只保存提醒 ID；执行时重新读取数据库并通过 OneBot V11
Bot API 发送消息，避免把数据库会话或事件对象放入长期任务。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
from typing import Any, cast
from zoneinfo import ZoneInfo

from apscheduler.jobstores.base import JobLookupError
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from nonebot import get_bot, get_driver, logger
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.matcher import Matcher  # noqa: TC002
from nonebot.params import Arg, CommandArg
from nonebot.plugin import PluginMetadata, on_command
from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_orm import async_scoped_session, get_session
from sqlalchemy import func, select

from .data_models.bot_group import BotGroup
from .data_models.scheduled_reminder import ScheduledReminder
from .permission import (
    check_feature_permission,
    is_group_admin,
    require_feature,
)

__plugin_meta__ = PluginMetadata(
    name="定时提醒",
    description="用交互向导管理群聊和私聊的循环或一次性提醒",
    usage=(
        "群主/管理员发送 /定时提醒 打开当前群的管理向导；"
        "超级用户可私聊发送 /定时提醒 选择要管理的群"
    ),
    extra={
        "commands": [
            {
                "name": "定时提醒",
                "aliases": ["提醒"],
                "description": "交互式管理群聊定时提醒",
                "feature": "reminder",
                "scope": "all",
                "superuser": False,
                "admin": True,
            },
        ],
    },
)

logger.info("定时提醒模块已加载")

_CST = timezone(timedelta(hours=8))
_TIMEZONE = ZoneInfo("Asia/Shanghai")
_JOB_ID_PREFIX = "yawn_core_reminder:"
_SCHEDULE_RECURRING = "recurring"
_SCHEDULE_ONCE = "once"
_MAX_GROUP_REMINDERS = 50
_MAX_CREATOR_REMINDERS = 10
_MAX_NAME_LENGTH = 64
_MAX_CRON_LENGTH = 128
_MAX_ERROR_LENGTH = 2000
_MISFIRE_GRACE_TIME = 60
_MAX_CLOCK_HOUR = 23
_MAX_CLOCK_MINUTE = 59
_MAX_MONTH_DAY = 31
_CRON_FIELD_COUNT = 5
_TWO_PARTS = 2
_MAX_ERROR_PREVIEW = 90
_ERROR_PREVIEW_LENGTH = 87
_COUNTDOWN_PATTERN = re.compile(r"\{\{倒计时:(?P<date>[^{}]+)\}\}")
_ONCE_ISO_PATTERN = re.compile(
    r"^(?:一次(?:性)?\s*)?"
    r"(?P<date>\d{4}[-/.]\d{1,2}[-/.]\d{1,2})\s+"
    r"(?P<time>.+)$"
)
_ONCE_CN_PATTERN = re.compile(
    r"^(?:一次(?:性)?\s*)?"
    r"(?P<year>\d{4})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日\s+"
    r"(?P<time>.+)$"
)
_CLOCK_PATTERN = re.compile(
    r"^(?P<hour>\d{1,2})(?:(?::|：)(?P<minute>\d{1,2})|"
    r"点(?:(?P<minute_cn>\d{1,2})分?)?)$"
)
_ALLOWED_SEGMENT_TYPES = {
    "text",
    "at",
    "image",
    "record",
    "video",
    "face",
    "share",
    "music",
    "location",
}
_TEMPORARY_SEGMENT_TYPES = {
    "anonymous",
    "forward",
    "node",
    "poke",
    "reply",
}
_WEEKDAY_CRON = {
    "一": "mon",
    "二": "tue",
    "三": "wed",
    "四": "thu",
    "五": "fri",
    "六": "sat",
    "日": "sun",
    "天": "sun",
}
_WEEKDAY_DISPLAY = {
    "mon": "一",
    "tue": "二",
    "wed": "三",
    "thu": "四",
    "fri": "五",
    "sat": "六",
    "sun": "日",
}

reminder_cmd = on_command(
    "定时提醒",
    aliases={"提醒"},
    priority=3,
    block=True,
)


@dataclass(frozen=True, slots=True)
class ScheduleSpec:
    """交互层使用的已校验时间规则。"""

    schedule_type: str
    cron_expression: str | None = None
    run_at: datetime | None = None
    display: str = ""


@dataclass(slots=True)
class ReminderDraft:
    """尚未确认写入数据库的提醒草稿。"""

    name: str = ""
    schedule: ScheduleSpec | None = None
    target_type: str | None = None
    target_user_id: int | None = None
    message: Message | None = None
    message_payload: list[dict[str, Any]] | None = None


def _now_bj() -> datetime:
    """返回项目约定的无时区北京时间。"""
    return datetime.now(_CST).replace(tzinfo=None)


def _job_id(reminder_id: int) -> str:
    return f"{_JOB_ID_PREFIX}{reminder_id}"


def _is_superuser(user_id: int) -> bool:
    return str(user_id) in get_driver().config.superusers


def _plain_text(message: Message) -> str:
    return message.extract_plain_text().strip()


def _plain_field(message: Message, field_name: str) -> str:
    if any(not segment.is_text() for segment in message):
        raise ValueError(f"{field_name}只能包含文字")
    value = _plain_text(message)
    if not value:
        raise ValueError(f"{field_name}不能为空")
    return value


def _copy_segment(segment: MessageSegment) -> MessageSegment:
    return MessageSegment(segment.type, dict(segment.data))


def _copy_message(message: Message) -> Message:
    return Message(_copy_segment(segment) for segment in message)


def _trim_message(message: Message) -> Message:
    """去掉命令输入带来的首尾空白，保留其它消息段。"""
    copied = _copy_message(message)

    while copied and copied[0].is_text():
        text = copied[0].data.get("text", "")
        stripped = text.lstrip()
        if stripped:
            copied[0].data["text"] = stripped
            break
        copied.pop(0)

    while copied and copied[-1].is_text():
        text = copied[-1].data.get("text", "")
        stripped = text.rstrip()
        if stripped:
            copied[-1].data["text"] = stripped
            break
        copied.pop()

    return copied


def _consume_group_prefix(message: Message) -> tuple[int | None, Message]:
    """从私聊参数开头读取群号，并保留后续消息。"""
    copied = _copy_message(message)
    if not copied or not copied[0].is_text():
        return None, copied

    text = copied[0].data.get("text", "")
    matched = re.match(r"\s*(\d+)(?:\s+|$)", text)
    if matched is None:
        return None, copied

    group_id = int(matched.group(1))
    rest = text[matched.end() :]
    if rest:
        copied[0].data["text"] = rest
    else:
        copied.pop(0)
    return group_id, copied


def _normalize_cron(expression: str) -> str:
    expression = " ".join(expression.split())
    if len(expression) > _MAX_CRON_LENGTH:
        raise ValueError("Cron 表达式过长")  # noqa: TRY003
    try:
        CronTrigger.from_crontab(expression, timezone=_TIMEZONE)
    except (TypeError, ValueError) as exc:
        raise ValueError("旧提醒规则无效，请编辑为新的时间格式") from exc
    return expression


def _parse_clock(text: str) -> dt_time:
    normalized = text.strip().replace("时", "点")
    matched = _CLOCK_PATTERN.fullmatch(normalized)
    if matched is None:
        raise ValueError(  # noqa: TRY003
            "时间格式应为 7:00、07:00 或 7点30分"
        )

    hour = int(matched.group("hour"))
    minute_text = matched.group("minute") or matched.group("minute_cn")
    minute = int(minute_text) if minute_text else 0
    if hour > _MAX_CLOCK_HOUR or minute > _MAX_CLOCK_MINUTE:
        raise ValueError(  # noqa: TRY003
            "时间必须在 00:00 到 23:59 之间"
        )
    return dt_time(hour, minute)


def _format_clock(value: dt_time) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


def _parse_once_schedule(text: str, now: datetime) -> ScheduleSpec | None:
    matched = _ONCE_ISO_PATTERN.fullmatch(text)
    if matched is not None:
        raw_date = matched.group("date").replace("/", "-").replace(".", "-")
        try:
            target_date = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise ValueError(  # noqa: TRY003
                "日期无效，请使用 YYYY-MM-DD"
            ) from exc
        clock = _parse_clock(matched.group("time"))
    else:
        matched_cn = _ONCE_CN_PATTERN.fullmatch(text)
        if matched_cn is None:
            return None
        try:
            target_date = date(
                int(matched_cn.group("year")),
                int(matched_cn.group("month")),
                int(matched_cn.group("day")),
            )
        except ValueError as exc:
            raise ValueError("日期无效，请检查年月日") from exc
        clock = _parse_clock(matched_cn.group("time"))

    run_at = datetime.combine(target_date, clock)
    if run_at <= now:
        raise ValueError("一次性事件必须设置为未来时间")
    display = f"一次 {_format_datetime(run_at)}"
    return ScheduleSpec(
        schedule_type=_SCHEDULE_ONCE,
        run_at=run_at,
        display=display,
    )


def _parse_weekday_prefix(prefix: str) -> list[str] | None:
    if not prefix.startswith("每周"):
        return None
    body = prefix[2:].strip()
    body = body.replace("星期", "").replace("周", "")
    body = re.sub(r"[\s、,，/和]+", "", body)
    if not body or any(char not in _WEEKDAY_CRON for char in body):
        raise ValueError("星期格式应为每周一，或每周一、三、五")
    days: list[str] = []
    for char in body:
        cron_day = _WEEKDAY_CRON[char]
        if cron_day not in days:
            days.append(cron_day)
    return days


def _parse_schedule(text: str, *, now: datetime | None = None) -> ScheduleSpec:
    """解析交互向导中的循环或一次性时间。"""
    normalized = " ".join(text.strip().replace("：", ":").replace("　", " ").split())
    if not normalized:
        raise ValueError("时间不能为空")

    current = now or _now_bj()
    once = _parse_once_schedule(normalized, current)
    if once is not None:
        return once

    parts = normalized.rsplit(" ", 1)
    if len(parts) != _TWO_PARTS:
        raise ValueError(  # noqa: TRY003
            "无法识别时间，请使用“每天 07:00”或“一次 2026-08-20 15:30”"
        )
    prefix, clock_text = parts
    clock = _parse_clock(clock_text)
    minute = clock.minute
    hour = clock.hour

    if prefix == "每天":
        cron = f"{minute} {hour} * * *"
        display = f"每天 {_format_clock(clock)}"
    elif prefix == "工作日":
        cron = f"{minute} {hour} * * mon-fri"
        display = f"工作日 {_format_clock(clock)}"
    elif prefix.startswith("每月"):
        match = re.fullmatch(r"每月\s*(\d{1,2})\s*[日号]?", prefix)
        if match is None:
            raise ValueError(  # noqa: TRY003
                "月份格式应为每月 1 日 09:00"
            )
        day_of_month = int(match.group(1))
        if not 1 <= day_of_month <= _MAX_MONTH_DAY:
            raise ValueError(  # noqa: TRY003
                "每月日期必须在 1 到 31 之间"
            )
        cron = f"{minute} {hour} {day_of_month} * *"
        display = f"每月 {day_of_month} 日 {_format_clock(clock)}"
    else:
        weekdays = _parse_weekday_prefix(prefix)
        if weekdays is None:
            raise ValueError("无法识别时间，请使用每天、工作日、每周、每月或一次性格式")
        cron = f"{minute} {hour} * * {','.join(weekdays)}"
        display_days = "、".join(_WEEKDAY_DISPLAY[day] for day in weekdays)
        display = f"每周{display_days} {_format_clock(clock)}"

    return ScheduleSpec(
        schedule_type=_SCHEDULE_RECURRING,
        cron_expression=_normalize_cron(cron),
        display=display,
    )


def _format_datetime(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M")


def _humanize_cron(expression: str) -> str:  # noqa: PLR0911
    """为旧的 Cron 规则生成可读标题；不支持的规则保留原文。"""
    fields = expression.split()
    if len(fields) != _CRON_FIELD_COUNT:
        return f"自定义规则：{expression}"
    minute, hour, day, month, weekday = fields
    if not minute.isdigit() or not hour.isdigit():
        return f"自定义规则：{expression}"
    clock = f"{int(hour):02d}:{int(minute):02d}"
    if day == "*" and month == "*" and weekday == "*":
        return f"每天 {clock}"
    if day == "*" and month == "*" and weekday in {"mon-fri", "1-5"}:
        return f"工作日 {clock}"
    if day == "*" and month == "*" and weekday not in {"*", "?"}:
        day_names = []
        for item in weekday.split(","):
            if item in _WEEKDAY_DISPLAY:
                day_names.append(_WEEKDAY_DISPLAY[item])
            else:
                day_names = []
                break
        if day_names:
            return f"每周{'、'.join(day_names)} {clock}"
    if day.isdigit() and month == "*" and weekday == "*":
        return f"每月 {int(day)} 日 {clock}"
    return f"自定义规则：{expression}"


def _coerce_bj_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(_TIMEZONE).replace(tzinfo=None)


def _schedule_from_reminder(reminder: ScheduledReminder) -> ScheduleSpec:
    schedule_type = getattr(reminder, "schedule_type", _SCHEDULE_RECURRING)
    if schedule_type == _SCHEDULE_ONCE:
        run_at = getattr(reminder, "run_at", None)
        if not isinstance(run_at, datetime):
            raise ValueError("一次性提醒缺少执行时间，请重新编辑")
        run_at = _coerce_bj_datetime(run_at)
        return ScheduleSpec(
            schedule_type=_SCHEDULE_ONCE,
            run_at=run_at,
            display=f"一次 {_format_datetime(run_at)}",
        )

    expression = getattr(reminder, "cron_expression", None)
    if not isinstance(expression, str) or not expression:
        raise ValueError(  # noqa: TRY003
            "循环提醒缺少 Cron 规则，请重新编辑"
        )
    expression = _normalize_cron(expression)
    return ScheduleSpec(
        schedule_type=_SCHEDULE_RECURRING,
        cron_expression=expression,
        display=_humanize_cron(expression),
    )


def _validate_schedule_spec(schedule: ScheduleSpec) -> None:
    if schedule.schedule_type == _SCHEDULE_RECURRING:
        if not schedule.cron_expression:
            raise ValueError("循环提醒缺少时间规则")
        _normalize_cron(schedule.cron_expression)
        return
    if schedule.schedule_type == _SCHEDULE_ONCE:
        if schedule.run_at is None:
            raise ValueError("一次性提醒缺少执行时间")
        if schedule.run_at <= _now_bj():
            raise ValueError("一次性事件必须设置为未来时间")
        return
    raise ValueError("未知的提醒时间类型")


def _validate_countdown_text(text: str) -> None:
    for match in _COUNTDOWN_PATTERN.finditer(text):
        try:
            date.fromisoformat(match.group("date"))
        except ValueError as exc:  # noqa: PERF203
            raise ValueError(f"倒计时日期无效：{match.group('date')}") from exc


def _message_to_payload(message: Message) -> list[dict[str, Any]]:
    """校验并转换可持久化的 OneBot V11 消息段。"""
    message = _trim_message(message)
    if not message:
        raise ValueError("提醒消息不能为空")

    payload: list[dict[str, Any]] = []
    for segment in message:
        if segment.type in _TEMPORARY_SEGMENT_TYPES:
            raise ValueError(
                f"不支持保存临时消息段「{segment.type}」，"
                "请使用文本、At、图片、语音等可复用消息段"
            )
        if segment.type not in _ALLOWED_SEGMENT_TYPES:
            raise ValueError(f"不支持保存消息段「{segment.type}」")

        data = dict(segment.data)
        if segment.type == "text":
            text = data.get("text")
            if not isinstance(text, str):
                raise ValueError("文本消息段格式无效")
            _validate_countdown_text(text)

        try:
            normalized_data = json.loads(json.dumps(data, ensure_ascii=False))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"消息段「{segment.type}」包含不可持久化的数据") from exc
        payload.append({"type": segment.type, "data": normalized_data})

    return payload


def _render_countdown(text: str, now: datetime) -> str:
    def replace(match: re.Match[str]) -> str:
        target = date.fromisoformat(match.group("date"))
        remaining = max((target - now.date()).days, 0)
        return str(remaining)

    return _COUNTDOWN_PATTERN.sub(replace, text)


def _payload_to_message(
    payload: list[dict[str, Any]],
    now: datetime | None = None,
    *,
    render_countdown: bool = True,
) -> Message:
    segments: list[MessageSegment] = []
    for item in payload:
        segment_type = item.get("type")
        data = item.get("data")
        if not isinstance(segment_type, str) or not isinstance(data, dict):
            raise TypeError("提醒消息段数据格式无效")
        if segment_type in _TEMPORARY_SEGMENT_TYPES:
            raise ValueError(f"提醒中不支持临时消息段「{segment_type}」")
        if segment_type not in _ALLOWED_SEGMENT_TYPES:
            raise ValueError(f"提醒中不支持消息段「{segment_type}」")
        segment_data = dict(data)
        if segment_type == "text":
            text = segment_data.get("text")
            if not isinstance(text, str):
                raise ValueError("提醒文本消息段数据格式无效")
            if render_countdown:
                segment_data["text"] = _render_countdown(
                    text,
                    now or _now_bj(),
                )
        segments.append(MessageSegment(segment_type, segment_data))
    if not segments:
        raise ValueError("提醒消息不能为空")
    return Message(segments)


def _message_preview(message: Message | None) -> str:
    if message is None:
        return "（未填写）"
    parts: list[str] = []
    for segment in message:
        if segment.type == "text":
            parts.append(str(segment.data.get("text", "")))
        elif segment.type == "at":
            parts.append(f"[艾特 {segment.data.get('qq', '')}]")
        else:
            parts.append(f"[{segment.type}]")
    preview = "".join(parts).strip()
    return preview or "（消息段）"


def _parse_target(text: str) -> tuple[str, int | None]:
    normalized = " ".join(text.split())
    if normalized in {"群聊", "群", "本群"}:
        return "group", None

    parts = normalized.split()
    if (
        len(parts) == _TWO_PARTS
        and parts[0] in {"私聊", "私信"}
        and parts[1].isdigit()
        and int(parts[1]) > 0
    ):
        return "private", int(parts[1])
    raise ValueError(  # noqa: TRY003
        "发送目标应为“本群”，或“私聊 QQ号”"
    )


def _format_target(reminder: ScheduledReminder) -> str:
    if reminder.target_type == "group":
        return "本群"
    return f"私聊 {reminder.target_user_id}"


def _format_next_run(reminder_id: int) -> str:
    try:
        job = scheduler.get_job(_job_id(reminder_id))
    except Exception:  # noqa: BLE001
        job = None
    if job is None or job.next_run_time is None:
        return "未安排"
    return job.next_run_time.astimezone(_TIMEZONE).strftime("%Y-%m-%d %H:%M")


def _format_status(reminder: ScheduledReminder) -> str:
    schedule_type = getattr(reminder, "schedule_type", _SCHEDULE_RECURRING)
    if schedule_type == _SCHEDULE_ONCE:
        if reminder.last_success_at is not None and not reminder.enabled:
            return "已完成"
        if reminder.last_error and not reminder.enabled:
            return "发送失败"
        run_at = getattr(reminder, "run_at", None)
        if (
            reminder.enabled
            and isinstance(run_at, datetime)
            and _coerce_bj_datetime(run_at) <= _now_bj()
        ):
            return "待处理"
        return "启用" if reminder.enabled else "停用"
    return "启用" if reminder.enabled else "停用"


def _format_last_result(reminder: ScheduledReminder) -> str:
    if reminder.last_error:
        error = " ".join(reminder.last_error.split())
        if len(error) > _MAX_ERROR_PREVIEW:
            error = f"{error[:_ERROR_PREVIEW_LENGTH]}..."
        return f"失败：{error}"
    if reminder.last_success_at is not None:
        return f"成功：{_format_datetime(reminder.last_success_at)}"
    if reminder.last_run_at is not None:
        return f"执行：{_format_datetime(reminder.last_run_at)}"
    return "尚未执行"


def _build_home_text(
    group_id: int,
    group_name: str | None,
    reminder_count: int,
) -> str:
    title = group_name or f"群 {group_id}"
    return "\n".join(
        [
            f"═══ 定时提醒 · {title} ═══",
            f"群号：{group_id}　当前 {reminder_count} 条提醒",
            "",
            "1. 新建提醒",
            "2. 查看列表",
            "3. 帮助",
            "0. 退出管理",
            "",
            "输入序号或“新建/列表”，也可以输入“取消”退出。",
        ]
    )


def _build_group_selector(groups: list[BotGroup]) -> str:
    lines = ["═══ 定时提醒 · 选择群 ═══"]
    if not groups:
        lines.append("暂时没有已记录的群，请直接输入群号。")
    else:
        for index, group in enumerate(groups, 1):
            name = group.group_name or "未命名群"
            lines.append(f"{index}. {name}（{group.group_id}）")
    lines.extend(
        [
            "",
            "输入序号进入管理，或直接输入群号。",
            "输入“取消”退出。",
        ]
    )
    return "\n".join(lines)


def _build_reminder_list(
    reminders: list[ScheduledReminder],
    group_id: int,
    group_name: str | None = None,
) -> str:
    title = group_name or f"群 {group_id}"
    lines = [f"═══ 定时提醒列表 · {title} ═══"]
    if not reminders:
        lines.append("当前没有提醒，输入“新建”创建第一条。")
    else:
        for reminder in reminders:
            schedule = _schedule_from_reminder(reminder)
            lines.extend(
                [
                    f"[{reminder.id}] {reminder.name}",
                    f"  {_format_status(reminder)} · {schedule.display}",
                    f"  目标：{_format_target(reminder)}",
                    f"  下次：{_format_next_run(reminder.id)} · "
                    f"最近：{_format_last_result(reminder)}",
                ]
            )

    lines.extend(
        [
            "──────────────",
            "查看 <ID>　编辑 <ID>　复制 <ID>",
            "启用 <ID>　停用 <ID>　立即发送 <ID>",
            "删除 <ID>　返回",
        ]
    )
    return "\n".join(lines)


def _build_reminder_detail(
    reminder: ScheduledReminder,
    *,
    prefix: str | None = None,
) -> str:
    schedule = _schedule_from_reminder(reminder)
    message = _payload_to_message(
        list(reminder.message_segments),
        render_countdown=False,
    )
    lines = [f"═══ 提醒 [{reminder.id}] ═══"]
    if prefix:
        lines.extend([prefix, ""])
    lines.extend(
        [
            f"名称：{reminder.name}",
            f"状态：{_format_status(reminder)}",
            f"时间：{schedule.display}",
            f"目标：{_format_target(reminder)}",
            f"下次：{_format_next_run(reminder.id)}",
            f"最近：{_format_last_result(reminder)}",
            f"消息：{_message_preview(message)}",
        ]
    )
    if reminder.last_error:
        lines.append(f"失败详情：{reminder.last_error[:500]}")
    lines.extend(
        [
            "──────────────",
            "编辑　复制　启用/停用　立即发送　删除",
            "输入“返回”回列表，输入“取消”退出。",
        ]
    )
    return "\n".join(lines)


def _build_help_text() -> str:
    return "\n".join(
        [
            "═══ 定时提醒帮助 ═══",
            "在群聊中发送 /定时提醒，按菜单操作。",
            "超级用户私聊发送 /定时提醒，可先选择群。",
            "",
            "循环时间示例：",
            "  每天 07:00",
            "  工作日 18:30",
            "  每周一、三、五 09:00",
            "  每月 1 日 09:00",
            "一次性时间示例：",
            "  一次 2026-08-20 15:30",
            "  2026年8月20日 15点30分",
            "",
            "目标填写“本群”，或“私聊 QQ号”。",
            "文本支持 {{倒计时:2027-06-07}}，消息也可带常用 OneBot 消息段。",
            "输入“取消”退出当前管理会话。",
        ]
    )


async def _list_group_reminders(
    session: async_scoped_session,
    group_id: int,
) -> list[ScheduledReminder]:
    result = await session.execute(
        select(ScheduledReminder)
        .where(ScheduledReminder.group_id == group_id)
        .order_by(ScheduledReminder.id.asc())
    )
    return list(result.scalars().all())


async def _list_known_groups(
    session: async_scoped_session,
) -> list[BotGroup]:
    result = await session.execute(
        select(BotGroup).order_by(
            BotGroup.last_active_at.desc(),
            BotGroup.group_id.asc(),
        )
    )
    return list(result.scalars().all())


async def _ensure_group(
    bot: Bot,
    session: async_scoped_session,
    group_id: int,
) -> BotGroup:
    group = await session.get(BotGroup, group_id)
    if group is not None:
        return group

    try:
        info = await bot.get_group_info(group_id=group_id)
    except Exception as exc:
        raise ValueError(  # noqa: TRY003
            f"机器人无法获取群 {group_id} 的信息，请确认机器人在该群"
        ) from exc

    group = BotGroup(
        group_id=group_id,
        group_name=info.get("group_name"),
        last_active_at=_now_bj(),
    )
    session.add(group)
    await session.commit()
    return group


async def _check_private_target(
    bot: Bot,
    group_id: int,
    user_id: int,
) -> None:
    try:
        await bot.get_group_member_info(
            group_id=group_id,
            user_id=user_id,
            no_cache=False,
        )
    except Exception as exc:
        raise ValueError(  # noqa: TRY003
            f"QQ {user_id} 不是群 {group_id} 的成员，或机器人无法查询该成员"
        ) from exc


async def _check_management_permission(
    event: MessageEvent,
    session: async_scoped_session,
    group_id: int,
) -> None:
    user_id = int(event.get_user_id())
    is_su = _is_superuser(user_id)

    if isinstance(event, GroupMessageEvent):
        if event.group_id != group_id:
            raise ValueError("不能管理其它群的定时提醒")
        if not is_su and not is_group_admin(event):
            raise PermissionError("仅群主、群管理员或超级用户可管理定时提醒")
    elif not is_su:
        raise PermissionError("私聊中仅超级用户可以指定群管理定时提醒")

    if not await check_feature_permission(
        user_id,
        group_id,
        "reminder",
        session,
    ):
        raise PermissionError("功能「定时提醒」当前未开启哦~")


async def _create_reminder(  # noqa: PLR0913
    session: async_scoped_session,
    *,
    group_id: int,
    creator_user_id: int,
    name: str,
    schedule: ScheduleSpec,
    target_type: str,
    target_user_id: int | None,
    message: Message,
) -> ScheduledReminder:
    _validate_schedule_spec(schedule)
    group_count = await session.scalar(
        select(func.count())
        .select_from(ScheduledReminder)
        .where(ScheduledReminder.group_id == group_id)
    )
    if (group_count or 0) >= _MAX_GROUP_REMINDERS:
        raise ValueError(  # noqa: TRY003
            f"该群最多只能保存 {_MAX_GROUP_REMINDERS} 条定时提醒"
        )

    creator_count = await session.scalar(
        select(func.count())
        .select_from(ScheduledReminder)
        .where(
            ScheduledReminder.group_id == group_id,
            ScheduledReminder.creator_user_id == creator_user_id,
        )
    )
    if (creator_count or 0) >= _MAX_CREATOR_REMINDERS:
        raise ValueError(  # noqa: TRY003
            f"每位创建者在同一群最多只能创建 {_MAX_CREATOR_REMINDERS} 条定时提醒"
        )

    payload = _message_to_payload(message)
    reminder = ScheduledReminder(
        group_id=group_id,
        creator_user_id=creator_user_id,
        name=name,
        schedule_type=schedule.schedule_type,
        cron_expression=schedule.cron_expression,
        run_at=schedule.run_at,
        target_type=target_type,
        target_user_id=target_user_id,
        message_segments=payload,
        enabled=True,
    )
    session.add(reminder)
    await session.flush()
    reminder_id = reminder.id
    await session.commit()
    await session.refresh(reminder)

    try:
        _schedule_reminder_job(reminder_id, schedule)
    except Exception as exc:
        logger.error(f"注册定时提醒任务失败: reminder_id={reminder_id}, error={exc!r}")
        _remove_reminder_job(reminder_id)
        await session.delete(reminder)
        await session.commit()
        raise ValueError("定时提醒调度失败，请检查时间后重试") from exc
    return reminder


def _schedule_reminder_job(
    reminder_id: int,
    schedule: ScheduleSpec,
) -> None:
    _validate_schedule_spec(schedule)
    if schedule.schedule_type == _SCHEDULE_ONCE:
        trigger = DateTrigger(
            run_date=schedule.run_at,
            timezone=_TIMEZONE,
        )
    else:
        trigger = CronTrigger.from_crontab(
            cast("str", schedule.cron_expression),
            timezone=_TIMEZONE,
        )
    scheduler.add_job(
        _run_reminder_job,
        trigger=trigger,
        args=[reminder_id],
        id=_job_id(reminder_id),
        name=f"定时提醒 #{reminder_id}",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=_MISFIRE_GRACE_TIME,
    )


def _remove_reminder_job(reminder_id: int) -> bool:
    try:
        scheduler.remove_job(_job_id(reminder_id))
    except JobLookupError:
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"移除定时提醒任务失败: reminder_id={reminder_id}, error={exc!r}"
        )
        return False
    return True


async def _set_reminder_error(
    reminder_id: int,
    *,
    run_at: datetime,
    error: str | None,
) -> None:
    async with get_session() as session:
        reminder = await session.get(ScheduledReminder, reminder_id)
        if reminder is None:
            return
        reminder.last_run_at = run_at
        if error is None:
            reminder.last_success_at = run_at
            reminder.last_error = None
        else:
            reminder.last_error = error[:_MAX_ERROR_LENGTH]
        await session.commit()


async def _deliver_reminder(
    reminder_id: int,
    *,
    force: bool,
) -> str:
    """发送一条提醒，返回供交互反馈使用的状态码。"""
    async with get_session() as session:
        reminder = await session.get(ScheduledReminder, reminder_id)
        if reminder is None:
            return "missing"
        if not reminder.enabled and not force:
            return "disabled"

        permission_user_id = reminder.creator_user_id
        permission_group_id: int | None = reminder.group_id
        if (
            reminder.target_type == "private"
            and reminder.target_user_id is not None
        ):
            permission_user_id = reminder.target_user_id
            permission_group_id = None
        if not await check_feature_permission(
            permission_user_id,
            permission_group_id,
            "reminder",
            cast("async_scoped_session", session),
        ):
            return "feature_disabled"

        group_id = reminder.group_id
        target_type = reminder.target_type
        target_user_id = reminder.target_user_id
        payload = list(reminder.message_segments)

    run_at = _now_bj()
    try:
        message = _payload_to_message(payload, run_at)
        bot = get_bot()
        if target_type == "group":
            await bot.send_group_msg(group_id=group_id, message=message)
        elif target_type == "private" and target_user_id is not None:
            await bot.send_private_msg(user_id=target_user_id, message=message)
        else:
            raise ValueError("提醒发送目标数据无效")  # noqa: TRY301
    except Exception as exc:  # noqa: BLE001
        error = str(exc) or repr(exc)
        await _set_reminder_error(reminder_id, run_at=run_at, error=error)
        logger.warning(f"定时提醒发送失败: reminder_id={reminder_id}, error={exc!r}")
        return "error"

    await _set_reminder_error(reminder_id, run_at=run_at, error=None)
    logger.info(f"定时提醒发送成功: reminder_id={reminder_id}")
    return "success"


async def _finalize_once(reminder_id: int, status: str) -> None:
    if status not in {"success", "error"}:
        return
    async with get_session() as session:
        reminder = await session.get(ScheduledReminder, reminder_id)
        if (
            reminder is not None
            and getattr(
                reminder,
                "schedule_type",
                _SCHEDULE_RECURRING,
            )
            == _SCHEDULE_ONCE
        ):
            reminder.enabled = False
            await session.commit()
            _remove_reminder_job(reminder_id)


async def _run_reminder_job(reminder_id: int) -> None:
    async with get_session() as session:
        reminder = await session.get(ScheduledReminder, reminder_id)
        if reminder is None:
            _remove_reminder_job(reminder_id)
            return
        is_once = (
            getattr(reminder, "schedule_type", _SCHEDULE_RECURRING)
            == _SCHEDULE_ONCE
        )
    status = await _deliver_reminder(reminder_id, force=False)
    if status == "missing":
        _remove_reminder_job(reminder_id)
        return
    if is_once and status in {"success", "error"}:
        await _finalize_once(reminder_id, status)
    elif status == "feature_disabled":
        logger.info(f"定时提醒因功能开关关闭而跳过: reminder_id={reminder_id}")


@get_driver().on_startup
async def _restore_reminder_jobs() -> None:
    """从 ORM 恢复启用中的提醒；调度器生命周期由 apscheduler 插件管理。"""
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ScheduledReminder).where(ScheduledReminder.enabled.is_(True))
            )
            reminders = list(result.scalars().all())

        restored = 0
        for reminder in reminders:
            try:
                schedule = _schedule_from_reminder(reminder)
                if (
                    schedule.schedule_type == _SCHEDULE_ONCE
                    and schedule.run_at is not None
                    and schedule.run_at <= _now_bj()
                ):
                    async with get_session() as update_session:
                        stale = await update_session.get(
                            ScheduledReminder,
                            reminder.id,
                        )
                        if stale is not None:
                            stale.enabled = False
                            stale.last_error = "一次性提醒已过期，未恢复调度"
                            await update_session.commit()
                    logger.warning(f"停用已过期的一次性提醒: reminder_id={reminder.id}")
                    continue
                _schedule_reminder_job(reminder.id, schedule)
            except Exception as exc:  # noqa: BLE001
                async with get_session() as update_session:
                    failed = await update_session.get(
                        ScheduledReminder,
                        reminder.id,
                    )
                    if failed is not None:
                        failed.enabled = False
                        failed.last_error = f"恢复调度失败：{exc}"[:_MAX_ERROR_LENGTH]
                        await update_session.commit()
                logger.error(
                    f"恢复定时提醒失败: reminder_id={reminder.id}, error={exc!r}"
                )
            else:
                restored += 1
        logger.info(f"已恢复 {restored} 条定时提醒")
    except Exception:  # noqa: BLE001
        # 数据库迁移尚未应用时不阻止机器人其它插件启动。
        logger.exception("恢复定时提醒失败")


def _draft_complete(draft: ReminderDraft) -> bool:
    return (
        bool(draft.name)
        and draft.schedule is not None
        and draft.target_type is not None
        and draft.message is not None
        and draft.message_payload is not None
    )


def _draft_target(draft: ReminderDraft) -> str:
    if draft.target_type == "group":
        return "本群"
    if draft.target_type == "private":
        return f"私聊 {draft.target_user_id}"
    return "（未填写）"


def _build_draft_preview(
    draft: ReminderDraft,
    *,
    title: str = "═══ 请确认提醒 ═══",
) -> str:
    schedule = draft.schedule.display if draft.schedule else "（未填写）"
    return "\n".join(
        [
            title,
            f"名称：{draft.name or '（未填写）'}",
            f"时间：{schedule}",
            f"目标：{_draft_target(draft)}",
            f"消息：{_message_preview(draft.message)}",
            "",
            "输入“确认”保存，或输入“修改 名称/时间/目标/消息”。",
            "输入“返回”回上一步，输入“取消”退出。",
        ]
    )


def _create_step_prompt(step: str, draft: ReminderDraft) -> str:
    prompts = {
        "name": ("═══ 新建提醒 · 1/4 ═══\n请输入提醒名称（例如：早安、会议提醒）："),
        "schedule": (
            "═══ 新建提醒 · 2/4 ═══\n"
            "请输入时间，例如：每天 07:00、工作日 18:30、"
            "每周一、三、五 09:00、一次 2026-08-20 15:30："
        ),
        "target": ("═══ 新建提醒 · 3/4 ═══\n请输入发送目标：“本群”，或“私聊 QQ号”："),
        "message": (
            "═══ 新建提醒 · 4/4 ═══\n请发送提醒消息，可发送文本或常用 OneBot 消息段："
        ),
    }
    if step == "name" and draft.name:
        return f"{prompts[step]}\n当前：{draft.name}"
    if step == "schedule" and draft.schedule:
        return f"{prompts[step]}\n当前：{draft.schedule.display}"
    if step == "target" and draft.target_type:
        return f"{prompts[step]}\n当前：{_draft_target(draft)}"
    return prompts[step]


def _edit_field_prompt(field: str, draft: ReminderDraft) -> str:
    prompts = {
        "name": "请输入新的名称：",
        "schedule": "请输入新的时间规则，例如“每天 07:00”：",
        "target": "请输入新的发送目标：“本群”，或“私聊 QQ号”：",
        "message": "请发送新的提醒消息：",
    }
    current = {
        "name": draft.name or "（未填写）",
        "schedule": draft.schedule.display if draft.schedule else "（未填写）",
        "target": _draft_target(draft),
        "message": _message_preview(draft.message),
    }
    return f"{prompts[field]}\n当前：{current[field]}\n输入“返回”取消本次修改。"


def _build_edit_menu(draft: ReminderDraft) -> str:
    return "\n".join(
        [
            "═══ 编辑提醒 ═══",
            f"名称：{draft.name}",
            f"时间：{draft.schedule.display if draft.schedule else '（未填写）'}",
            f"目标：{_draft_target(draft)}",
            f"消息：{_message_preview(draft.message)}",
            "",
            "输入 1/名称、2/时间、3/目标、4/消息 修改字段。",
            "输入“预览”查看并保存，输入“返回”回详情。",
        ]
    )


def _field_name(text: str) -> str | None:
    aliases = {
        "1": "name",
        "名称": "name",
        "名字": "name",
        "2": "schedule",
        "时间": "schedule",
        "规则": "schedule",
        "3": "target",
        "目标": "target",
        "4": "message",
        "消息": "message",
    }
    return aliases.get(text.strip().lower())


def _action_name(text: str) -> str | None:
    aliases = {
        "查看": "view",
        "详情": "view",
        "编辑": "edit",
        "修改": "edit",
        "复制": "copy",
        "启用": "enable",
        "开启": "enable",
        "停用": "disable",
        "暂停": "disable",
        "立即发送": "send",
        "发送": "send",
        "删除": "delete",
    }
    return aliases.get(text.strip().lower())


def _parse_action(text: str) -> tuple[str | None, int | None]:
    parts = text.split(maxsplit=1)
    if len(parts) != _TWO_PARTS or not parts[1].strip().isdigit():
        return None, None
    return _action_name(parts[0]), int(parts[1].strip())


async def _get_group_reminder(
    session: async_scoped_session,
    group_id: int,
    reminder_id: int,
) -> ScheduledReminder | None:
    reminder = await session.get(ScheduledReminder, reminder_id)
    if reminder is None or reminder.group_id != group_id:
        return None
    return reminder


def _draft_from_reminder(reminder: ScheduledReminder) -> ReminderDraft:
    schedule = _schedule_from_reminder(reminder)
    message = _payload_to_message(
        list(reminder.message_segments),
        render_countdown=False,
    )
    return ReminderDraft(
        name=reminder.name,
        schedule=schedule,
        target_type=reminder.target_type,
        target_user_id=reminder.target_user_id,
        message=message,
        message_payload=list(reminder.message_segments),
    )


def _schedule_equal(first: ScheduleSpec, second: ScheduleSpec) -> bool:
    return (
        first.schedule_type == second.schedule_type
        and first.cron_expression == second.cron_expression
        and first.run_at == second.run_at
    )


async def _update_reminder(
    session: async_scoped_session,
    reminder: ScheduledReminder,
    draft: ReminderDraft,
) -> ScheduledReminder:
    if not _draft_complete(draft):
        raise ValueError("提醒资料未填写完整")
    schedule = cast("ScheduleSpec", draft.schedule)
    payload = cast("list[dict[str, Any]]", draft.message_payload)
    _validate_schedule_spec(schedule)

    old_schedule = _schedule_from_reminder(reminder)
    old_values = {
        "name": reminder.name,
        "schedule_type": getattr(
            reminder,
            "schedule_type",
            _SCHEDULE_RECURRING,
        ),
        "cron_expression": reminder.cron_expression,
        "run_at": getattr(reminder, "run_at", None),
        "target_type": reminder.target_type,
        "target_user_id": reminder.target_user_id,
        "message_segments": list(reminder.message_segments),
        "enabled": reminder.enabled,
    }
    schedule_changed = not _schedule_equal(old_schedule, schedule)
    new_enabled = reminder.enabled or schedule_changed

    reminder.name = draft.name
    reminder.schedule_type = schedule.schedule_type
    reminder.cron_expression = schedule.cron_expression
    reminder.run_at = schedule.run_at
    reminder.target_type = cast("str", draft.target_type)
    reminder.target_user_id = draft.target_user_id
    reminder.message_segments = payload
    reminder.enabled = new_enabled
    await session.commit()

    try:
        if new_enabled:
            _schedule_reminder_job(reminder.id, schedule)
        else:
            _remove_reminder_job(reminder.id)
    except Exception as exc:
        reminder.name = old_values["name"]
        reminder.schedule_type = old_values["schedule_type"]
        reminder.cron_expression = old_values["cron_expression"]
        reminder.run_at = old_values["run_at"]
        reminder.target_type = old_values["target_type"]
        reminder.target_user_id = old_values["target_user_id"]
        reminder.message_segments = old_values["message_segments"]
        reminder.enabled = old_values["enabled"]
        await session.commit()
        _remove_reminder_job(reminder.id)
        if old_values["enabled"]:
            try:
                _schedule_reminder_job(reminder.id, old_schedule)
            except Exception:  # noqa: BLE001
                logger.exception(f"恢复旧定时提醒任务失败: reminder_id={reminder.id}")
        raise ValueError("保存提醒失败，已恢复原配置") from exc
    return reminder


async def _set_enabled(
    session: async_scoped_session,
    reminder: ScheduledReminder,
    *,
    enabled: bool,
) -> None:
    if enabled:
        schedule = _schedule_from_reminder(reminder)
        if (
            schedule.schedule_type == _SCHEDULE_ONCE
            and schedule.run_at is not None
            and schedule.run_at <= _now_bj()
        ):
            raise ValueError("一次性事件时间已过去，请编辑为未来时间")
        _schedule_reminder_job(reminder.id, schedule)
        reminder.enabled = True
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            _remove_reminder_job(reminder.id)
            raise
    else:
        if not _remove_reminder_job(reminder.id):
            raise ValueError("停用提醒失败，请稍后重试")
        reminder.enabled = False
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            try:
                _schedule_reminder_job(reminder.id, _schedule_from_reminder(reminder))
            except Exception:  # noqa: BLE001
                logger.exception("停用提醒回滚调度失败")
            raise


async def _show_home(
    session: async_scoped_session,
    matcher: Matcher,
    *,
    reject: bool,
) -> None:
    group_id = cast("int", matcher.state["group_id"])
    reminders = await _list_group_reminders(session, group_id)
    text = _build_home_text(
        group_id,
        matcher.state.get("group_name"),
        len(reminders),
    )
    matcher.state["view"] = "home"
    matcher.state.pop("selected_id", None)
    matcher.state.pop("flow", None)
    if reject:
        await reminder_cmd.reject_arg("reminder_choice", text)
    await reminder_cmd.send(text)


async def _show_list(
    session: async_scoped_session,
    matcher: Matcher,
    *,
    reject: bool,
) -> None:
    group_id = cast("int", matcher.state["group_id"])
    reminders = await _list_group_reminders(session, group_id)
    text = _build_reminder_list(
        reminders,
        group_id,
        matcher.state.get("group_name"),
    )
    notice = matcher.state.pop("last_notice", None)
    if notice:
        text = f"{notice}\n\n{text}"
    matcher.state["view"] = "list"
    matcher.state.pop("flow", None)
    if reject:
        await reminder_cmd.reject_arg("reminder_choice", text)
    await reminder_cmd.send(text)


async def _show_detail(
    session: async_scoped_session,
    matcher: Matcher,
    reminder_id: int,
    *,
    prefix: str | None = None,
) -> None:
    group_id = cast("int", matcher.state["group_id"])
    reminder = await _get_group_reminder(session, group_id, reminder_id)
    if reminder is None:
        await reminder_cmd.reject_arg(
            "reminder_choice",
            "未找到该群中的提醒 ID，请重新输入或发送“返回”。",
        )
    matcher.state["view"] = "detail"
    matcher.state["selected_id"] = reminder_id
    matcher.state.pop("flow", None)
    await reminder_cmd.reject_arg(
        "reminder_choice",
        _build_reminder_detail(reminder, prefix=prefix),
    )


async def _begin_create(
    matcher: Matcher,
    draft: ReminderDraft | None = None,
    *,
    first_step: str = "name",
) -> None:
    matcher.state["view"] = "create"
    matcher.state["flow"] = "create"
    matcher.state["draft"] = draft or ReminderDraft()
    matcher.state["create_step"] = first_step
    await reminder_cmd.reject_arg(
        "reminder_choice",
        _build_draft_preview(cast("ReminderDraft", matcher.state["draft"]))
        if first_step == "confirm"
        else _create_step_prompt(
            first_step,
            cast("ReminderDraft", matcher.state["draft"]),
        ),
    )


async def _begin_edit(
    session: async_scoped_session,
    matcher: Matcher,
    reminder_id: int,
) -> None:
    group_id = cast("int", matcher.state["group_id"])
    reminder = await _get_group_reminder(session, group_id, reminder_id)
    if reminder is None:
        await reminder_cmd.reject_arg("reminder_choice", "未找到该群中的提醒 ID")
        return
    matcher.state["view"] = "edit"
    matcher.state["flow"] = "edit"
    matcher.state["selected_id"] = reminder_id
    matcher.state["draft"] = _draft_from_reminder(reminder)
    matcher.state["edit_step"] = "select"
    await reminder_cmd.reject_arg(
        "reminder_choice",
        _build_edit_menu(cast("ReminderDraft", matcher.state["draft"])),
    )


async def _handle_draft_confirmation(  # noqa: C901, PLR0911
    event: MessageEvent,
    matcher: Matcher,
    session: async_scoped_session,
    choice: Message,
) -> None:
    text = _plain_text(choice)
    draft = cast("ReminderDraft", matcher.state["draft"])
    if text in {"确认", "确定", "保存", "是", "yes"}:
        if not _draft_complete(draft):
            await reminder_cmd.reject_arg(
                "reminder_choice",
                "提醒资料还没填写完整，请输入“返回”继续填写。",
            )
            return
        try:
            if matcher.state.get("flow") == "create":
                reminder = await _create_reminder(
                    session,
                    group_id=cast("int", matcher.state["group_id"]),
                    creator_user_id=int(event.get_user_id()),
                    name=draft.name,
                    schedule=cast("ScheduleSpec", draft.schedule),
                    target_type=cast("str", draft.target_type),
                    target_user_id=draft.target_user_id,
                    message=cast("Message", draft.message),
                )
                prefix = f"已创建提醒 [{reminder.id}]。"
            else:
                reminder_id = cast("int", matcher.state["selected_id"])
                reminder = await _get_group_reminder(
                    session,
                    cast("int", matcher.state["group_id"]),
                    reminder_id,
                )
                if reminder is None:
                    await reminder_cmd.reject_arg(
                        "reminder_choice",
                        "提醒已不存在，请返回列表。",
                    )
                    return
                reminder = await _update_reminder(session, reminder, draft)
                prefix = f"已保存提醒 [{reminder.id}]。"
        except ValueError as exc:
            await reminder_cmd.reject_arg(
                "reminder_choice",
                f"{exc}\n\n{_build_draft_preview(draft)}",
            )
            return
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            logger.exception("保存交互式定时提醒失败")
            await reminder_cmd.reject_arg(
                "reminder_choice",
                f"保存失败：{exc}\n\n{_build_draft_preview(draft)}",
            )
            return

        matcher.state["view"] = "detail"
        matcher.state["selected_id"] = reminder.id
        matcher.state.pop("flow", None)
        await reminder_cmd.reject_arg(
            "reminder_choice",
            _build_reminder_detail(reminder, prefix=prefix),
        )
        return

    if text in {"返回", "上一步"}:
        if matcher.state.get("flow") == "create":
            await _prompt_create_step(matcher, "message")
        matcher.state["edit_step"] = "select"
        await reminder_cmd.reject_arg(
            "reminder_choice",
            _build_edit_menu(draft),
        )
        return

    field_text = text
    if text.startswith("修改"):
        field_text = text[2:].strip()
    field = _field_name(field_text)
    if field is not None:
        if matcher.state.get("flow") == "create":
            await _prompt_create_step(matcher, field)
        matcher.state["edit_step"] = field
        await reminder_cmd.reject_arg(
            "reminder_choice",
            _edit_field_prompt(field, draft),
        )
        return

    await reminder_cmd.reject_arg(
        "reminder_choice",
        "请输入“确认”、 “修改 名称/时间/目标/消息”，或“取消”。",
    )


async def _prompt_create_step(matcher: Matcher, step: str) -> None:
    draft = cast("ReminderDraft", matcher.state["draft"])
    matcher.state["create_step"] = step
    await reminder_cmd.reject_arg(
        "reminder_choice",
        _create_step_prompt(step, draft),
    )


async def _handle_create_choice(
    bot: Bot,
    matcher: Matcher,
    session: async_scoped_session,
    choice: Message,
) -> None:
    text = _plain_text(choice)
    draft = cast("ReminderDraft", matcher.state["draft"])
    step = cast("str", matcher.state.get("create_step", "name"))

    if step == "confirm":
        await _handle_draft_confirmation(
            cast("MessageEvent", matcher.state["event"]),
            matcher,
            session,
            choice,
        )
        return

    if text in {"返回", "上一步"}:
        previous = {
            "name": "name",
            "schedule": "name",
            "target": "schedule",
            "message": "target",
        }
        await _prompt_create_step(matcher, previous[step])
        return

    try:
        if step == "name":
            name = _plain_field(choice, "名称")
            if len(name) > _MAX_NAME_LENGTH:
                raise ValueError(  # noqa: TRY003, TRY301
                    f"名称不能超过 {_MAX_NAME_LENGTH} 个字符"
                )
            draft.name = name
            await _prompt_create_step(matcher, "schedule")

        if step == "schedule":
            draft.schedule = _parse_schedule(text)
            await _prompt_create_step(matcher, "target")

        if step == "target":
            target_type, target_user_id = _parse_target(
                _plain_field(choice, "发送目标")
            )
            if target_type == "private" and target_user_id is not None:
                await _check_private_target(
                    bot,
                    cast("int", matcher.state["group_id"]),
                    target_user_id,
                )
            draft.target_type = target_type
            draft.target_user_id = target_user_id
            await _prompt_create_step(matcher, "message")

        if step == "message":
            message = _trim_message(choice)
            draft.message_payload = _message_to_payload(message)
            draft.message = message
            matcher.state["create_step"] = "confirm"
            await reminder_cmd.reject_arg(
                "reminder_choice",
                _build_draft_preview(draft),
            )
            return
    except ValueError as exc:
        await reminder_cmd.reject_arg(
            "reminder_choice",
            f"输入有误：{exc}\n\n{_create_step_prompt(step, draft)}",
        )
        return


async def _handle_edit_choice(  # noqa: C901, PLR0911, PLR0912
    bot: Bot,
    matcher: Matcher,
    session: async_scoped_session,
    choice: Message,
) -> None:
    text = _plain_text(choice)
    draft = cast("ReminderDraft", matcher.state["draft"])
    step = cast("str", matcher.state.get("edit_step", "select"))

    if step == "select":
        if text in {"返回", "上一步"}:
            await _show_detail(
                session,
                matcher,
                cast("int", matcher.state["selected_id"]),
            )
            return
        if text in {"预览", "确认", "保存"}:
            matcher.state["edit_step"] = "confirm"
            await reminder_cmd.reject_arg(
                "reminder_choice",
                _build_draft_preview(draft, title="═══ 编辑预览 ═══"),
            )
            return
        field = _field_name(text)
        if field is None:
            await reminder_cmd.reject_arg(
                "reminder_choice",
                _build_edit_menu(draft),
            )
            return
        matcher.state["edit_step"] = field
        await reminder_cmd.reject_arg(
            "reminder_choice",
            _edit_field_prompt(field, draft),
        )
        return

    if step == "confirm":
        await _handle_draft_confirmation(
            cast("MessageEvent", matcher.state["event"]),
            matcher,
            session,
            choice,
        )
        return

    if text in {"返回", "上一步"}:
        matcher.state["edit_step"] = "select"
        await reminder_cmd.reject_arg(
            "reminder_choice",
            _build_edit_menu(draft),
        )
        return

    try:
        if step == "name":
            name = _plain_field(choice, "名称")
            if len(name) > _MAX_NAME_LENGTH:
                raise ValueError(  # noqa: TRY003, TRY301
                    f"名称不能超过 {_MAX_NAME_LENGTH} 个字符"
                )
            draft.name = name
        elif step == "schedule":
            draft.schedule = _parse_schedule(text)
        elif step == "target":
            target_type, target_user_id = _parse_target(
                _plain_field(choice, "发送目标")
            )
            if target_type == "private" and target_user_id is not None:
                await _check_private_target(
                    bot,
                    cast("int", matcher.state["group_id"]),
                    target_user_id,
                )
            draft.target_type = target_type
            draft.target_user_id = target_user_id
        elif step == "message":
            message = _trim_message(choice)
            draft.message_payload = _message_to_payload(message)
            draft.message = message
        else:
            await reminder_cmd.reject_arg(
                "reminder_choice",
                _build_edit_menu(draft),
            )
    except ValueError as exc:
        await reminder_cmd.reject_arg(
            "reminder_choice",
            f"输入有误：{exc}\n\n{_edit_field_prompt(step, draft)}",
        )
        return
    matcher.state["edit_step"] = "select"
    await reminder_cmd.reject_arg(
        "reminder_choice",
        _build_edit_menu(draft),
    )


async def _copy_reminder(
    session: async_scoped_session,
    matcher: Matcher,
    reminder_id: int,
) -> None:
    reminder = await _get_group_reminder(
        session,
        cast("int", matcher.state["group_id"]),
        reminder_id,
    )
    if reminder is None:
        await reminder_cmd.reject_arg("reminder_choice", "未找到该群中的提醒 ID")
        return
    draft = _draft_from_reminder(reminder)
    draft.name = f"{draft.name}（副本）"[:_MAX_NAME_LENGTH]
    if (
        draft.schedule is not None
        and draft.schedule.schedule_type == _SCHEDULE_ONCE
        and draft.schedule.run_at is not None
        and draft.schedule.run_at <= _now_bj()
    ):
        draft.schedule = None
        await _begin_create(matcher, draft, first_step="schedule")
        return
    await _begin_create(matcher, draft, first_step="confirm")


async def _send_action_result(
    session: async_scoped_session,
    matcher: Matcher,
    reminder: ScheduledReminder,
    action: str,
) -> None:
    if action == "enable":
        await _set_enabled(session, reminder, enabled=True)
        await _show_detail(session, matcher, reminder.id, prefix="已启用。")
        return
    if action == "disable":
        await _set_enabled(session, reminder, enabled=False)
        await _show_detail(session, matcher, reminder.id, prefix="已停用。")
        return
    if action == "send":
        status = await _deliver_reminder(reminder.id, force=True)
        try:
            await session.refresh(reminder)
        except Exception:  # noqa: BLE001
            await session.rollback()
        messages = {
            "success": "已立即发送。",
            "missing": "提醒不存在。",
            "feature_disabled": "功能「定时提醒」当前未开启哦~",
            "error": "发送失败，已记录失败详情。",
            "disabled": "提醒已停用。",
        }
        await _show_detail(
            session,
            matcher,
            reminder.id,
            prefix=messages.get(status, "发送失败。"),
        )
        return
    if action == "delete":
        matcher.state["view"] = "confirm_delete"
        matcher.state["selected_id"] = reminder.id
        matcher.state["delete_name"] = reminder.name
        await reminder_cmd.reject_arg(
            "reminder_choice",
            f"确定删除“{reminder.name}”（ID {reminder.id}）吗？\n"
            "删除后无法恢复，输入“确认删除”继续，或“返回”取消。",
        )


async def _handle_action(
    matcher: Matcher,
    session: async_scoped_session,
    action: str,
    reminder_id: int,
) -> None:
    reminder = await _get_group_reminder(
        session,
        cast("int", matcher.state["group_id"]),
        reminder_id,
    )
    if reminder is None:
        await reminder_cmd.reject_arg(
            "reminder_choice",
            "未找到该群中的提醒 ID，请重新输入。",
        )
        return
    if action == "view":
        await _show_detail(session, matcher, reminder_id)
        return
    if action == "edit":
        await _begin_edit(session, matcher, reminder_id)
        return
    if action == "copy":
        await _copy_reminder(session, matcher, reminder_id)
        return
    if action in {"enable", "disable", "send", "delete"}:
        try:
            await _send_action_result(session, matcher, reminder, action)
        except ValueError as exc:
            await reminder_cmd.reject_arg("reminder_choice", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            logger.exception("处理定时提醒操作失败")
            await reminder_cmd.reject_arg(
                "reminder_choice",
                f"操作失败：{exc}",
            )
            return


async def _handle_home_choice(
    matcher: Matcher,
    session: async_scoped_session,
    text: str,
) -> None:
    if text in {"0", "退出", "关闭", "结束"}:
        await reminder_cmd.finish("已退出定时提醒管理。")
    if text in {"1", "新建", "添加", "新增"}:
        await _begin_create(matcher)
        return
    if text in {"2", "列表", "查看"}:
        await _show_list(session, matcher, reject=True)
        return
    if text in {"3", "帮助"}:
        await reminder_cmd.reject_arg("reminder_choice", _build_help_text())
        return
    action, reminder_id = _parse_action(text)
    if action is not None and reminder_id is not None:
        await _handle_action(
            matcher,
            session,
            action,
            reminder_id,
        )
        return
    await reminder_cmd.reject_arg(
        "reminder_choice",
        "请输入 1 新建、2 列表、3 帮助，或输入“取消”退出。",
    )


async def _handle_list_choice(
    matcher: Matcher,
    session: async_scoped_session,
    text: str,
) -> None:
    if text in {"返回", "上一步"}:
        await _show_home(session, matcher, reject=True)
        return
    if text in {"新建", "添加", "新增"}:
        await _begin_create(matcher)
        return
    if text == "列表":
        await _show_list(session, matcher, reject=True)
        return
    if text.isdigit():
        await _show_detail(session, matcher, int(text))
        return
    action, reminder_id = _parse_action(text)
    if action is not None and reminder_id is not None:
        await _handle_action(
            matcher,
            session,
            action,
            reminder_id,
        )
        return
    await reminder_cmd.reject_arg(
        "reminder_choice",
        "请输入提醒 ID，或使用“查看/编辑/复制/启用/停用/发送/删除 <ID>”。",
    )


async def _handle_detail_choice(  # noqa: PLR0911
    matcher: Matcher,
    session: async_scoped_session,
    text: str,
) -> None:
    reminder_id = cast("int", matcher.state["selected_id"])
    if text in {"返回", "上一步"}:
        await _show_list(session, matcher, reject=True)
        return
    if text in {"编辑", "修改"}:
        await _begin_edit(session, matcher, reminder_id)
        return
    if text == "复制":
        await _copy_reminder(session, matcher, reminder_id)
        return
    if text in {"启用", "开启"}:
        await _handle_action(
            matcher,
            session,
            "enable",
            reminder_id,
        )
        return
    if text in {"停用", "暂停"}:
        await _handle_action(
            matcher,
            session,
            "disable",
            reminder_id,
        )
        return
    if text in {"立即发送", "发送"}:
        await _handle_action(
            matcher,
            session,
            "send",
            reminder_id,
        )
        return
    if text == "删除":
        await _handle_action(
            matcher,
            session,
            "delete",
            reminder_id,
        )
        return
    await reminder_cmd.reject_arg(
        "reminder_choice",
        "请输入编辑、复制、启用、停用、立即发送或删除。",
    )


async def _handle_delete_choice(
    session: async_scoped_session,
    matcher: Matcher,
    text: str,
) -> None:
    if text in {"返回", "取消", "否"}:
        await _show_detail(
            session,
            matcher,
            cast("int", matcher.state["selected_id"]),
        )
        return
    if text not in {"确认删除", "确认"}:
        await reminder_cmd.reject_arg(
            "reminder_choice",
            "请输入“确认删除”继续，或输入“返回”取消。",
        )
        return
    reminder_id = cast("int", matcher.state["selected_id"])
    group_id = cast("int", matcher.state["group_id"])
    reminder = await _get_group_reminder(session, group_id, reminder_id)
    if reminder is None:
        await _show_list(session, matcher, reject=True)
        return
    name = reminder.name
    await session.delete(reminder)
    await session.commit()
    _remove_reminder_job(reminder_id)
    matcher.state["last_notice"] = f"已删除提醒 [{reminder_id}] {name}。"
    await _show_list(
        session,
        matcher,
        reject=True,
    )


async def _handle_group_selection(
    bot: Bot,
    event: MessageEvent,
    matcher: Matcher,
    session: async_scoped_session,
    text: str,
) -> None:
    if text in {"返回", "取消", "退出"}:
        await reminder_cmd.finish("已退出定时提醒管理。")
    groups = cast("list[BotGroup]", matcher.state.get("groups", []))
    group_id: int | None = None
    if text.isdigit():
        index = int(text)
        group_id = (
            groups[index - 1].group_id
            if 1 <= index <= len(groups)
            else index
        )
    if group_id is None:
        await reminder_cmd.reject_arg(
            "reminder_choice",
            "请输入群列表中的序号，或直接输入群号。",
        )
        return
    try:
        group = await _ensure_group(cast("Bot", bot), session, group_id)
        await _check_management_permission(event, session, group_id)
    except (PermissionError, ValueError) as exc:
        await reminder_cmd.reject_arg("reminder_choice", str(exc))
        return
    matcher.state["group_id"] = group_id
    matcher.state["group_name"] = group.group_name or f"群 {group_id}"
    await _show_home(session, matcher, reject=True)


async def _dispatch_choice(
    bot: Bot,
    event: MessageEvent,
    matcher: Matcher,
    session: async_scoped_session,
    choice: Message,
) -> None:
    text = _plain_text(choice)
    view = matcher.state.get("view")
    if view == "groups":
        await _handle_group_selection(bot, event, matcher, session, text)
    elif view == "home":
        await _handle_home_choice(matcher, session, text)
    elif view == "list":
        await _handle_list_choice(matcher, session, text)
    elif view == "detail":
        await _handle_detail_choice(matcher, session, text)
    elif view == "confirm_delete":
        await _handle_delete_choice(session, matcher, text)
    elif matcher.state.get("flow") == "create":
        await _handle_create_choice(bot, matcher, session, choice)
    elif matcher.state.get("flow") == "edit":
        await _handle_edit_choice(bot, matcher, session, choice)
    else:
        await reminder_cmd.finish("定时提醒会话已过期，请重新发送 /定时提醒。")


async def _start_reminder_context(
    bot: Bot,
    event: MessageEvent,
    session: async_scoped_session,
    matcher: Matcher,
    args: Message,
) -> None:
    user_id = int(event.get_user_id())
    is_su = _is_superuser(user_id)
    matcher.state["event"] = event
    matcher.state["actor_user_id"] = user_id

    if isinstance(event, GroupMessageEvent):
        group_id = event.group_id
        if not is_su and not is_group_admin(event):
            await reminder_cmd.finish("仅群主、群管理员或超级用户可管理定时提醒")
        group = await _ensure_group(bot, session, group_id)
        await _check_management_permission(event, session, group_id)
        matcher.state["group_id"] = group_id
        matcher.state["group_name"] = group.group_name or f"群 {group_id}"
        matcher.state["view"] = "home"
        await _show_home(session, matcher, reject=False)
        return

    if not is_su:
        await reminder_cmd.finish("私聊中仅超级用户可以管理定时提醒")
    group_id, _remaining = _consume_group_prefix(args)
    if group_id is not None:
        group = await _ensure_group(bot, session, group_id)
        await _check_management_permission(event, session, group_id)
        matcher.state["group_id"] = group_id
        matcher.state["group_name"] = group.group_name or f"群 {group_id}"
        matcher.state["view"] = "home"
        await _show_home(session, matcher, reject=False)
        return

    groups = await _list_known_groups(session)
    matcher.state["view"] = "groups"
    matcher.state["groups"] = groups
    await reminder_cmd.send(_build_group_selector(groups))


@reminder_cmd.handle()
async def handle_reminder_entry(
    bot: Bot,
    event: MessageEvent,
    matcher: Matcher,
    session: async_scoped_session,
    args: Message = CommandArg(),
    _perm: None = require_feature("reminder"),  # pyright: ignore[reportArgumentType]
) -> None:
    await _start_reminder_context(bot, event, session, matcher, args)


@reminder_cmd.got(
    "reminder_choice",
    prompt="请输入菜单选项，或发送“取消”退出",
)
async def handle_reminder_choice(
    bot: Bot,
    event: MessageEvent,
    matcher: Matcher,
    session: async_scoped_session,
    choice: Message = Arg("reminder_choice"),
) -> None:
    if _plain_text(choice) in {"取消", "退出"}:
        await reminder_cmd.finish("已退出定时提醒管理。")

    group_id = matcher.state.get("group_id")
    if isinstance(group_id, int):
        try:
            await _check_management_permission(event, session, group_id)
        except (PermissionError, ValueError) as exc:
            await reminder_cmd.finish(str(exc))

    await _dispatch_choice(bot, event, matcher, session, choice)


__all__ = [
    "ScheduleSpec",
    "handle_reminder_choice",
    "handle_reminder_entry",
    "reminder_cmd",
]
