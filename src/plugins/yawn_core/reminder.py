"""群聊定时提醒。

提醒配置持久化在 ORM 中，具体的触发由
nonebot_plugin_apscheduler 提供的全局调度器负责。
调度任务只保存提醒 ID；执行时重新读取数据库并通过 OneBot V11
Bot API 发送消息，避免把数据库会话或事件对象放入长期任务。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, cast
from zoneinfo import ZoneInfo

from apscheduler.jobstores.base import JobLookupError
from apscheduler.triggers.cron import CronTrigger
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
from .data_models.group_feature import GroupFeature
from .data_models.scheduled_reminder import ScheduledReminder
from .permission import (
    check_feature_permission,
    is_group_admin,
    require_feature,
)

__plugin_meta__ = PluginMetadata(
    name="定时提醒",
    description="由机器人按 Cron 计划发送群聊或私聊提醒",
    usage=(
        "群主/管理员发送 /定时提醒 管理当前群；"
        "超级用户可私聊发送 /定时提醒 <群号> 管理指定群"
    ),
    extra={
        "commands": [
            {
                "name": "定时提醒",
                "aliases": ["提醒"],
                "description": "管理群聊定时提醒（需群主/管理员或超级用户）",
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
_MAX_GROUP_REMINDERS = 50
_MAX_CREATOR_REMINDERS = 10
_MAX_NAME_LENGTH = 64
_MAX_CRON_LENGTH = 128
_MAX_ERROR_LENGTH = 2000
_MISFIRE_GRACE_TIME = 60
_TARGET_PART_COUNT = 2
_ADD_FIELD_COUNT = 4
_ACTION_PART_COUNT = 2
_COUNTDOWN_PATTERN = re.compile(
    r"\{\{倒计时:(?P<date>[^{}]+)\}\}"
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

reminder_cmd = on_command(
    "定时提醒",
    aliases={"提醒"},
    priority=3,
    block=True,
)


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


def _trim_message(message: Message) -> Message:
    """去掉命令分隔符带来的首尾空白，保留其它消息段。"""
    copied = Message(_copy_segment(segment) for segment in message)

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


def _split_message_fields(message: Message, count: int = 3) -> list[Message]:
    """按前 count 个竖线切分，同时保留后续 OneBot 消息段。"""
    fields = [Message()]
    separators = 0

    for segment in message:
        if not segment.is_text() or separators >= count:
            fields[-1].append(_copy_segment(segment))
            continue

        buffer: list[str] = []
        for char in segment.data.get("text", ""):
            if char == "|" and separators < count:
                if buffer:
                    fields[-1].append(
                        MessageSegment.text("".join(buffer))
                    )
                    buffer.clear()
                fields.append(Message())
                separators += 1
            else:
                buffer.append(char)
        if buffer:
            fields[-1].append(MessageSegment.text("".join(buffer)))

    return fields


def _consume_group_prefix(message: Message) -> tuple[int | None, Message]:
    """从私聊命令参数中读取开头的群号，并保留后续消息段。"""
    copied = Message(_copy_segment(segment) for segment in message)
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


def _consume_first_word(message: Message) -> tuple[str | None, Message]:
    """读取并移除消息开头的一个纯文本单词。"""
    copied = Message(_copy_segment(segment) for segment in message)
    if not copied or not copied[0].is_text():
        return None, copied

    text = copied[0].data.get("text", "")
    matched = re.match(r"\s*(\S+)(?:\s+|$)", text)
    if matched is None:
        return None, copied

    word = matched.group(1)
    rest = text[matched.end() :]
    if rest:
        copied[0].data["text"] = rest
    else:
        copied.pop(0)
    return word, copied


def _normalize_cron(expression: str) -> str:
    expression = " ".join(expression.split())
    if len(expression) > _MAX_CRON_LENGTH:
        raise ValueError("Cron 表达式过长")  # noqa: TRY003
    try:
        CronTrigger.from_crontab(expression, timezone=_TIMEZONE)
    except (TypeError, ValueError) as exc:
        raise ValueError(  # noqa: TRY003
            "Cron 格式无效，请使用“分 时 日 月 周”五个字段，"
            "例如：0 7 * * *"
        ) from exc
    return expression


def _validate_countdown_text(text: str) -> None:
    for match in _COUNTDOWN_PATTERN.finditer(text):
        try:
            date.fromisoformat(match.group("date"))
        except ValueError as exc:  # noqa: PERF203
            raise ValueError(
                f"倒计时日期无效：{match.group('date')}"
            ) from exc


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
            normalized_data = json.loads(
                json.dumps(data, ensure_ascii=False)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"消息段「{segment.type}」包含不可持久化的数据"
            ) from exc
        payload.append(
            {
                "type": segment.type,
                "data": normalized_data,
            }
        )

    return payload


def _render_countdown(text: str, now: datetime) -> str:
    def replace(match: re.Match[str]) -> str:
        target = date.fromisoformat(match.group("date"))
        remaining = max((target - now.date()).days, 0)
        return str(remaining)

    return _COUNTDOWN_PATTERN.sub(replace, text)


def _payload_to_message(
    payload: list[dict[str, Any]],
    now: datetime,
) -> Message:
    segments: list[MessageSegment] = []
    for item in payload:
        segment_type = item.get("type")
        data = item.get("data")
        if not isinstance(segment_type, str) or not isinstance(data, dict):
            raise TypeError("提醒消息段数据格式无效")
        if segment_type in _TEMPORARY_SEGMENT_TYPES:
            raise ValueError(
                f"提醒中不支持临时消息段「{segment_type}」"
            )
        if segment_type not in _ALLOWED_SEGMENT_TYPES:
            raise ValueError(f"提醒中不支持消息段「{segment_type}」")
        segment_data = dict(data)
        if segment_type == "text":
            text = segment_data.get("text")
            if not isinstance(text, str):
                raise ValueError("提醒文本消息段数据格式无效")
            segment_data["text"] = _render_countdown(text, now)
        segments.append(MessageSegment(segment_type, segment_data))
    if not segments:
        raise ValueError("提醒消息不能为空")
    return Message(segments)


def _parse_target(text: str) -> tuple[str, int | None]:
    text = " ".join(text.split())
    if text in {"群聊", "群", "本群"}:
        return "group", None

    parts = text.split()
    if (
        len(parts) == _TARGET_PART_COUNT
        and parts[0] in {"私聊", "私信"}
        and parts[1].isdigit()
        and int(parts[1]) > 0
    ):
        return "private", int(parts[1])
    raise ValueError(  # noqa: TRY003
        "发送目标格式应为「群聊」或「私聊 <QQ号>」"
    )


def _parse_add_fields(message: Message) -> tuple[str, str, str, Message]:
    fields = _split_message_fields(message)
    if len(fields) != _ADD_FIELD_COUNT:
        raise ValueError(  # noqa: TRY003
            "添加格式：添加 <名称> | <分 时 日 月 周> | "
            "群聊/私聊 <QQ号> | <消息>"
        )

    name = _plain_field(fields[0], "名称")
    if len(name) > _MAX_NAME_LENGTH:
        raise ValueError(  # noqa: TRY003
            f"名称不能超过 {_MAX_NAME_LENGTH} 个字符"
        )
    cron_expression = _normalize_cron(_plain_field(fields[1], "Cron"))
    target_text = _plain_field(fields[2], "发送目标")
    _parse_target(target_text)
    message = _trim_message(fields[3])
    if not message:
        raise ValueError("提醒消息不能为空")
    return name, cron_expression, target_text, message


def _format_target(reminder: ScheduledReminder) -> str:
    if reminder.target_type == "group":
        return "群聊"
    return f"私聊 {reminder.target_user_id}"


def _format_next_run(reminder_id: int) -> str:
    try:
        job = scheduler.get_job(_job_id(reminder_id))
    except Exception:  # noqa: BLE001
        job = None
    if job is None or job.next_run_time is None:
        return "未安排"
    return job.next_run_time.astimezone(_TIMEZONE).strftime(
        "%m-%d %H:%M"
    )


def _build_reminder_list(
    reminders: list[ScheduledReminder],
    group_id: int,
) -> str:
    lines = [
        f"═══ 定时提醒 · 群 {group_id} ═══",
    ]
    if not reminders:
        lines.append("当前没有定时提醒。")
    else:
        for reminder in reminders:
            status = "启用" if reminder.enabled else "停用"
            error = "（最近发送失败）" if reminder.last_error else ""
            lines.append(
                f"[{reminder.id}] {reminder.name} · {status}{error}\n"
                f"  {reminder.cron_expression} · "
                f"{_format_target(reminder)} · 下次 {_format_next_run(reminder.id)}"
            )

    lines.extend(
        [
            "──────────────",
            "添加 <名称> | <分 时 日 月 周> | 群聊/私聊 <QQ号> | <消息>",
            "启用 <ID> / 停用 <ID> / 删除 <ID> / 立即发送 <ID>",
            "输入「取消」退出",
        ]
    )
    return "\n".join(lines)


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
            f"QQ {user_id} 不是群 {group_id} 的成员，"
            "或机器人无法查询该成员"
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
            raise PermissionError(
                "仅群主、群管理员或超级用户可管理定时提醒"
            )
    elif not is_su:
        raise PermissionError(
            "私聊中仅超级用户可以指定群管理定时提醒"
        )

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
    cron_expression: str,
    target_type: str,
    target_user_id: int | None,
    message: Message,
) -> ScheduledReminder:
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
            f"每位创建者在同一群最多只能创建 "
            f"{_MAX_CREATOR_REMINDERS} 条定时提醒"
        )

    payload = _message_to_payload(message)
    reminder = ScheduledReminder(
        group_id=group_id,
        creator_user_id=creator_user_id,
        name=name,
        cron_expression=cron_expression,
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
        _schedule_reminder_job(reminder_id, cron_expression)
    except Exception as exc:
        logger.error(
            f"注册定时提醒任务失败: reminder_id={reminder_id}, "
            f"error={exc!r}"
        )
        _remove_reminder_job(reminder_id)
        await session.delete(reminder)
        await session.commit()
        raise ValueError("定时提醒调度失败，请稍后重试") from exc
    return reminder


def _schedule_reminder_job(
    reminder_id: int,
    cron_expression: str,
) -> None:
    trigger = CronTrigger.from_crontab(
        cron_expression,
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


def _remove_reminder_job(reminder_id: int) -> None:
    try:
        scheduler.remove_job(_job_id(reminder_id))
    except JobLookupError:
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"移除定时提醒任务失败: reminder_id={reminder_id}, "
            f"error={exc!r}"
        )


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
    """发送一条提醒，返回供命令反馈使用的状态码。"""
    async with get_session() as session:
        reminder = await session.get(ScheduledReminder, reminder_id)
        if reminder is None:
            return "missing"
        if not reminder.enabled and not force:
            return "disabled"

        group_feature = await session.get(
            GroupFeature,
            {"group_id": reminder.group_id, "feature": "reminder"},
        )
        if group_feature is not None and not group_feature.enabled:
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
            await bot.send_group_msg(
                group_id=group_id,
                message=message,
            )
        elif target_type == "private" and target_user_id is not None:
            await bot.send_private_msg(
                user_id=target_user_id,
                message=message,
            )
        else:
            raise ValueError("提醒发送目标数据无效")  # noqa: TRY301
    except Exception as exc:  # noqa: BLE001
        error = str(exc) or repr(exc)
        await _set_reminder_error(
            reminder_id,
            run_at=run_at,
            error=error,
        )
        logger.warning(
            f"定时提醒发送失败: reminder_id={reminder_id}, "
            f"error={exc!r}"
        )
        return "error"

    await _set_reminder_error(
        reminder_id,
        run_at=run_at,
        error=None,
    )
    logger.info(f"定时提醒发送成功: reminder_id={reminder_id}")
    return "success"


async def _run_reminder_job(reminder_id: int) -> None:
    status = await _deliver_reminder(reminder_id, force=False)
    if status == "missing":
        _remove_reminder_job(reminder_id)
    elif status == "feature_disabled":
        logger.info(
            f"定时提醒因功能开关关闭而跳过: reminder_id={reminder_id}"
        )


@get_driver().on_startup
async def _restore_reminder_jobs() -> None:
    """从 ORM 恢复启用中的提醒；调度器生命周期由 apscheduler 插件管理。"""
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ScheduledReminder).where(
                    ScheduledReminder.enabled.is_(True)
                )
            )
            reminders = list(result.scalars().all())

        restored = 0
        for reminder in reminders:
            try:
                _schedule_reminder_job(
                    reminder.id,
                    reminder.cron_expression,
                )
            except Exception as exc:  # noqa: BLE001, PERF203
                logger.error(
                    f"恢复定时提醒失败: reminder_id={reminder.id}, "
                    f"error={exc!r}"
                )
            else:
                restored += 1
        logger.info(f"已恢复 {restored} 条定时提醒")
    except Exception:  # noqa: BLE001
        # 数据库迁移尚未应用时不阻止机器人其它插件启动。
        logger.exception("恢复定时提醒失败")


async def _finish_action(  # noqa: C901, PLR0912, PLR0915
    session: async_scoped_session,
    event: MessageEvent,
    matcher: Matcher,
    group_id: int,
    message: Message,
) -> None:
    """处理一条非交互式操作命令。"""
    text = _plain_text(message)
    lowered = text.lower()

    if text in {"列表", "查看", "list"}:
        reminders = await _list_group_reminders(session, group_id)
        await reminder_cmd.finish(
            _build_reminder_list(reminders, group_id)
        )

    if text in {"添加", "新增", "add"}:
        matcher.state["flow"] = "add"
        matcher.state["add_step"] = "name"
        await reminder_cmd.reject_arg(
            "reminder_choice",
            "请输入提醒名称：",
        )

    if text in {"帮助", "help", "?"}:
        await reminder_cmd.finish(
            "定时提醒用法：\n"
            "  添加 <名称> | <分 时 日 月 周> | 群聊/私聊 <QQ号> | <消息>\n"
            "  列表\n"
            "  启用 <ID> / 停用 <ID> / 删除 <ID> / 立即发送 <ID>\n"
            "Cron 使用北京时间五字段格式，例如：0 7 * * *。\n"
            "文本可使用 {{倒计时:2027-06-07}} 占位符。"
        )

    if lowered.startswith(("添加 ", "新增 ", "add ")):
        try:
            name, cron_expression, target_text, reminder_message = (
                _parse_add_fields(
                    _consume_first_word(message)[1]
                )
            )
            target_type, target_user_id = _parse_target(target_text)
            if target_type == "private" and target_user_id is not None:
                await _check_private_target(
                    cast("Bot", get_bot()),
                    group_id,
                    target_user_id,
                )
            reminder = await _create_reminder(
                session,
                group_id=group_id,
                creator_user_id=int(event.get_user_id()),
                name=name,
                cron_expression=cron_expression,
                target_type=target_type,
                target_user_id=target_user_id,
                message=reminder_message,
            )
        except ValueError as exc:
            await reminder_cmd.finish(str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("创建定时提醒失败")
            await reminder_cmd.finish(
                f"创建定时提醒失败：{exc}"
            )
        await reminder_cmd.finish(
            f"已创建定时提醒 [{reminder.id}] {reminder.name}"
        )

    parts = text.split()
    if len(parts) == _ACTION_PART_COUNT and parts[0] in {
        "启用",
        "停用",
        "删除",
        "立即发送",
        "enable",
        "disable",
        "delete",
        "send",
    } and parts[1].isdigit():
        action = parts[0]
        reminder_id = int(parts[1])
        reminder = await session.get(ScheduledReminder, reminder_id)
        if reminder is None or reminder.group_id != group_id:
            await reminder_cmd.finish("未找到该群中的提醒 ID")

        if action in {"启用", "enable"}:
            reminder_id = reminder.id
            reminder_name = reminder.name
            try:
                cron_expression = _normalize_cron(
                    reminder.cron_expression
                )
                _schedule_reminder_job(reminder.id, cron_expression)
            except ValueError as exc:
                await reminder_cmd.finish(f"无法启用：{exc}")
            reminder.cron_expression = cron_expression
            reminder.enabled = True
            await session.commit()
            await reminder_cmd.finish(
                f"已启用定时提醒 [{reminder_id}] {reminder_name}"
            )

        if action in {"停用", "disable"}:
            reminder_id = reminder.id
            reminder_name = reminder.name
            reminder.enabled = False
            await session.commit()
            _remove_reminder_job(reminder_id)
            await reminder_cmd.finish(
                f"已停用定时提醒 [{reminder_id}] {reminder_name}"
            )

        if action in {"删除", "delete"}:
            reminder_name = reminder.name
            await session.delete(reminder)
            await session.commit()
            _remove_reminder_job(reminder_id)
            await reminder_cmd.finish(
                f"已删除定时提醒 [{reminder_id}] {reminder_name}"
            )

        if action in {"立即发送", "send"}:
            status = await _deliver_reminder(reminder_id, force=True)
            messages = {
                "success": "已立即发送",
                "missing": "提醒不存在",
                "feature_disabled": "功能「定时提醒」当前未开启哦~",
                "error": "发送失败，详情请查看机器人日志",
                "disabled": "提醒已停用",
            }
            await reminder_cmd.finish(messages.get(status, "发送失败"))

    await reminder_cmd.finish(
        "无法识别操作。请输入「列表」、"
        "「添加 ...」或「帮助」查看用法。"
    )


async def _start_reminder_context(
    bot: Bot,
    event: MessageEvent,
    session: async_scoped_session,
    matcher: Matcher,
    args: Message,
) -> None:
    user_id = int(event.get_user_id())
    is_su = _is_superuser(user_id)

    if isinstance(event, GroupMessageEvent):
        group_id = event.group_id
        if not is_su and not is_group_admin(event):
            await reminder_cmd.finish(
                "仅群主、群管理员或超级用户可管理定时提醒"
            )
        command_args = args
    else:
        if not is_su:
            await reminder_cmd.finish(
                "私聊中仅超级用户可以指定群管理定时提醒"
            )
        group_id, command_args = _consume_group_prefix(args)
        if group_id is None:
            await reminder_cmd.finish(
                "私聊格式：/定时提醒 <群号> [列表/添加/启用/停用/删除/立即发送]"
            )

    await _ensure_group(bot, session, group_id)
    if not await check_feature_permission(
        user_id,
        group_id,
        "reminder",
        session,
    ):
        await reminder_cmd.finish("功能「定时提醒」当前未开启哦~")

    matcher.state["group_id"] = group_id
    matcher.state["actor_user_id"] = user_id

    if not _plain_text(command_args):
        reminders = await _list_group_reminders(session, group_id)
        await reminder_cmd.send(
            _build_reminder_list(reminders, group_id)
        )
        return

    await _finish_action(
        session,
        event,
        matcher,
        group_id,
        command_args,
    )


@reminder_cmd.handle()
async def handle_reminder_entry(
    bot: Bot,
    event: MessageEvent,
    matcher: Matcher,
    session: async_scoped_session,
    args: Message = CommandArg(),
    _perm: None = require_feature("reminder"),  # pyright: ignore[reportArgumentType]
) -> None:
    await _start_reminder_context(
        bot,
        event,
        session,
        matcher,
        args,
    )


@reminder_cmd.got(
    "reminder_choice",
    prompt="请输入操作，或发送「取消」退出",
)
async def handle_reminder_choice(  # noqa: C901, PLR0912, PLR0915
    bot: Bot,
    event: MessageEvent,
    matcher: Matcher,
    session: async_scoped_session,
    choice: Message = Arg("reminder_choice"),
) -> None:
    if _plain_text(choice) in {"取消", "退出", "q"}:
        await reminder_cmd.finish("已退出定时提醒管理")

    group_id = matcher.state.get("group_id")
    if not isinstance(group_id, int):
        await reminder_cmd.finish("定时提醒会话已过期，请重新发送命令")

    try:
        await _check_management_permission(event, session, group_id)
    except (PermissionError, ValueError) as exc:
        await reminder_cmd.finish(str(exc))

    flow = matcher.state.get("flow")
    step = matcher.state.get("add_step")
    if flow != "add":
        await _finish_action(
            session,
            event,
            matcher,
            group_id,
            choice,
        )

    if step == "name":
        try:
            name = _plain_field(choice, "名称")
        except ValueError as exc:
            await reminder_cmd.reject_arg("reminder_choice", str(exc))
        if len(name) > _MAX_NAME_LENGTH:
            await reminder_cmd.reject_arg(
                "reminder_choice",
                f"名称不能超过 {_MAX_NAME_LENGTH} 个字符",
            )
        matcher.state["add_name"] = name
        matcher.state["add_step"] = "cron"
        await reminder_cmd.reject_arg(
            "reminder_choice",
            "请输入 Cron（分 时 日 月 周），例如：0 7 * * *",
        )

    if step == "cron":
        try:
            cron_expression = _normalize_cron(
                _plain_field(choice, "Cron")
            )
        except ValueError as exc:
            await reminder_cmd.reject_arg("reminder_choice", str(exc))
        matcher.state["add_cron"] = cron_expression
        matcher.state["add_step"] = "target"
        await reminder_cmd.reject_arg(
            "reminder_choice",
            "请输入发送目标：群聊，或私聊 <QQ号>",
        )

    if step == "target":
        try:
            target_type, target_user_id = _parse_target(
                _plain_field(choice, "发送目标")
            )
            if target_type == "private" and target_user_id is not None:
                await _check_private_target(
                    bot,
                    group_id,
                    target_user_id,
                )
        except ValueError as exc:
            await reminder_cmd.reject_arg("reminder_choice", str(exc))
        matcher.state["add_target_type"] = target_type
        matcher.state["add_target_user_id"] = target_user_id
        matcher.state["add_step"] = "message"
        await reminder_cmd.reject_arg(
            "reminder_choice",
            "请输入提醒消息，可发送文本或常用 OneBot 消息段：",
        )

    if step == "message":
        try:
            _message_to_payload(choice)
        except ValueError as exc:
            await reminder_cmd.reject_arg("reminder_choice", str(exc))

        try:
            reminder = await _create_reminder(
                session,
                group_id=group_id,
                creator_user_id=int(event.get_user_id()),
                name=matcher.state["add_name"],
                cron_expression=matcher.state["add_cron"],
                target_type=matcher.state["add_target_type"],
                target_user_id=matcher.state["add_target_user_id"],
                message=choice,
            )
            reminder_id = reminder.id
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            logger.exception("交互式创建定时提醒失败")
            await reminder_cmd.finish(f"创建定时提醒失败：{exc}")

        await reminder_cmd.finish(
            f"已创建定时提醒 [{reminder_id}] {matcher.state['add_name']}"
        )


__all__ = [
    "handle_reminder_choice",
    "handle_reminder_entry",
    "reminder_cmd",
]
