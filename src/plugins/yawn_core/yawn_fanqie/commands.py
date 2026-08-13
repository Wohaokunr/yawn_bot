"""番茄小说交互命令与任务管理权限。"""

# 交互式 matcher 的参数和中文提示会触发少量项目未采用的风格规则。
# 保留 F/未定义名称等正确性检查。
# ruff: noqa: E501, TRY003, TC002

from __future__ import annotations

import re
from typing import cast

from nonebot import get_driver, get_plugin_config, on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageEvent
from nonebot.matcher import Matcher
from nonebot.params import Arg, CommandArg
from nonebot_plugin_orm import async_scoped_session
from sqlalchemy import select

from ..permission import (  # noqa: TID252
    check_feature_permission,
    is_group_admin,
    require_feature,
)
from .config import Config
from .models import FanqieBook, FanqieJob
from .provider import BookSummary, ChapterRef, FanqieProvider, parse_source
from .state import cancel_job, delete_job, deliver_job, retry_job, submit_job

fanqie_cmd = on_command(
    "番茄小说",
    aliases={"番茄下载", "下载小说"},
    priority=5,
    block=True,
)
task_cmd = on_command(
    "番茄任务",
    aliases={"小说任务"},
    priority=5,
    block=True,
)
config = get_plugin_config(Config)


def _plain(value: Message) -> str:
    return str(value).strip()


def _user_id(event: MessageEvent) -> int:
    return int(event.get_user_id())


def _group_id(event: MessageEvent) -> int | None:
    group_id = getattr(event, "group_id", None)
    return int(group_id) if group_id is not None else None


def _is_superuser(user_id: int) -> bool:
    return str(user_id) in get_driver().config.superusers


def _parse_range(
    text: str,
    chapter_count: int,
    max_chapters: int = 500,
) -> tuple[int, int]:
    normalized = text.strip().replace("全书", "全本")
    if normalized in {"全本", "全部", "all", "ALL"}:
        if chapter_count > max_chapters:
            raise ValueError(f"全书超过 {max_chapters} 章，请分段下载")
        return 1, chapter_count
    match = re.fullmatch(
        r"第?\s*(\d+)\s*章?\s*(?:[-~到至])\s*第?\s*(\d+)\s*章?",
        normalized,
        flags=re.IGNORECASE,
    )
    if match is None:
        raise ValueError("请输入“全书”或“起始章-结束章”，例如 1-20")
    start, end = int(match.group(1)), int(match.group(2))
    if start < 1 or end < start or end > chapter_count:
        raise ValueError(f"章节范围必须在 1-{chapter_count} 之间")
    if end - start + 1 > max_chapters:
        raise ValueError(f"单次最多下载 {max_chapters} 章，请分段")
    return start, end


async def _feature_ok(event: MessageEvent, session: async_scoped_session) -> bool:
    return await check_feature_permission(
        _user_id(event),
        _group_id(event),
        "fanqie",
        session,
    )


async def _set_book_and_ask_range(
    matcher: Matcher,
    book: BookSummary,
    chapters: list[ChapterRef],
) -> None:
    matcher.state["book"] = book
    matcher.state["chapters"] = chapters
    await fanqie_cmd.reject_arg(
        "fanqie_range",
        f"已选择《{book.title}》（作者：{book.author}），共 {len(chapters)} 章。\n"
        "请输入章节范围：全书，或 起始章-结束章（例如 1-20）。",
    )


@fanqie_cmd.handle()
async def handle_fanqie_entry(
    matcher: Matcher,
    arg: Message = CommandArg(),
    _perm: None = require_feature("fanqie"),  # pyright: ignore[reportArgumentType]
) -> None:
    value = _plain(arg)
    if value:
        await _begin_fanqie_input(matcher, value)
        return
    await fanqie_cmd.reject_arg(
        "fanqie_input",
        "请输入书名、fanqienovel.com 书籍/阅读页链接，或 book ID。发送“取消”退出。",
    )


async def _begin_fanqie_input(matcher: Matcher, value: str) -> None:
    try:
        source_kind, _source_id = parse_source(value)
    except ValueError:
        source_kind = "search"
    async with FanqieProvider(config) as provider:
        try:
            if source_kind in {"page", "reader"}:
                book = await provider.resolve_book_reference(value)
                chapters = await provider.list_chapters(book.book_id)
                await _set_book_and_ask_range(matcher, book, chapters)
                return
            results = await provider.search(value)
        except Exception as exc:  # noqa: BLE001
            await fanqie_cmd.reject_arg("fanqie_input", f"查询失败：{exc}")
            return
    if not results:
        await fanqie_cmd.reject_arg(
            "fanqie_input", "没有找到公开书籍，请换个书名或直接输入链接。"
        )
        return
    matcher.state["search_results"] = results
    lines = ["请选择小说编号："]
    lines.extend(
        f"{index}. 《{book.title}》｜作者：{book.author}｜ID：{book.book_id}"
        for index, book in enumerate(results, 1)
    )
    lines.append("发送“取消”退出。")
    await fanqie_cmd.reject_arg("fanqie_book", "\n".join(lines))


@fanqie_cmd.got("fanqie_input")
async def handle_fanqie_input(
    event: MessageEvent,
    matcher: Matcher,
    session: async_scoped_session,
    value: Message = Arg("fanqie_input"),
) -> None:
    if not await _feature_ok(event, session):
        await fanqie_cmd.finish("功能「番茄小说」当前未开启哦~")
    text = _plain(value)
    if text in {"取消", "退出"}:
        await fanqie_cmd.finish("已退出番茄小说下载。")
    await _begin_fanqie_input(matcher, text)


@fanqie_cmd.got("fanqie_book")
async def handle_fanqie_book(
    event: MessageEvent,
    matcher: Matcher,
    session: async_scoped_session,
    value: Message = Arg("fanqie_book"),
) -> None:
    if not await _feature_ok(event, session):
        await fanqie_cmd.finish("功能「番茄小说」当前未开启哦~")
    text = _plain(value)
    if text in {"取消", "退出"}:
        await fanqie_cmd.finish("已退出番茄小说下载。")
    results = cast("list[BookSummary]", matcher.state.get("search_results", []))
    if not text.isdigit() or not 1 <= int(text) <= len(results):
        await fanqie_cmd.reject_arg("fanqie_book", "请输入搜索结果编号，例如 1。")
    book = results[int(text) - 1]
    async with FanqieProvider(config) as provider:
        try:
            chapters = await provider.list_chapters(book.book_id)
        except Exception as exc:  # noqa: BLE001
            await fanqie_cmd.reject_arg("fanqie_book", f"读取目录失败：{exc}")
            return
    await _set_book_and_ask_range(matcher, book, chapters)


@fanqie_cmd.got("fanqie_range")
async def handle_fanqie_range(
    event: MessageEvent,
    matcher: Matcher,
    session: async_scoped_session,
    value: Message = Arg("fanqie_range"),
) -> None:
    if not await _feature_ok(event, session):
        await fanqie_cmd.finish("功能「番茄小说」当前未开启哦~")
    text = _plain(value)
    if text in {"取消", "退出"}:
        await fanqie_cmd.finish("已退出番茄小说下载。")
    chapters = cast("list[ChapterRef]", matcher.state.get("chapters", []))
    book = cast("BookSummary", matcher.state.get("book"))
    try:
        start, end = _parse_range(text, len(chapters), config.fanqie_max_chapters)
    except ValueError as exc:
        await fanqie_cmd.reject_arg("fanqie_range", str(exc))
    matcher.state["start_chapter"] = start
    matcher.state["end_chapter"] = end
    await fanqie_cmd.reject_arg(
        "fanqie_confirm",
        f"将下载《{book.title}》第 {start}-{end} 章，共 {end - start + 1} 章。\n"
        "发送“确认”开始后台下载，发送“取消”退出。",
    )


@fanqie_cmd.got("fanqie_confirm")
async def handle_fanqie_confirm(
    event: MessageEvent,
    matcher: Matcher,
    session: async_scoped_session,
    value: Message = Arg("fanqie_confirm"),
) -> None:
    if not await _feature_ok(event, session):
        await fanqie_cmd.finish("功能「番茄小说」当前未开启哦~")
    text = _plain(value)
    if text in {"取消", "退出"}:
        await fanqie_cmd.finish("已退出番茄小说下载。")
    if text not in {"确认", "确定", "是", "yes", "Y"}:
        await fanqie_cmd.reject_arg(
            "fanqie_confirm", "请发送“确认”开始，或发送“取消”退出。"
        )
    book = cast("BookSummary", matcher.state["book"])
    chapters = cast("list[ChapterRef]", matcher.state["chapters"])
    job_id, error = await submit_job(
        _user_id(event),
        _group_id(event),
        book,
        chapters,
        int(matcher.state["start_chapter"]),
        int(matcher.state["end_chapter"]),
    )
    if error:
        await fanqie_cmd.finish(error)
    await fanqie_cmd.finish(
        f"番茄任务 #{job_id} 已创建，将在后台下载。群聊只播报状态，成品会私发给你。"
    )


_STATUS_LABELS = {
    "queued": "排队中",
    "running": "下载中",
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}


async def _get_job(session: async_scoped_session, job_id: int) -> FanqieJob | None:
    return await session.get(FanqieJob, job_id)


async def _book_title(session: async_scoped_session, job: FanqieJob) -> str:
    book = await session.get(FanqieBook, job.book_record_id)
    return book.title if book else "未知书籍"


async def _can_manage(
    event: MessageEvent,
    job: FanqieJob,
) -> bool:
    user_id = _user_id(event)
    if _is_superuser(user_id) or user_id == job.requester_user_id:
        return True
    return (
        isinstance(event, GroupMessageEvent)
        and job.group_id == int(event.group_id)
        and is_group_admin(event)
    )


async def _list_text(event: MessageEvent, session: async_scoped_session) -> str:
    user_id = _user_id(event)
    group_id = _group_id(event)
    query = select(FanqieJob).order_by(FanqieJob.id.desc()).limit(20)
    if not _is_superuser(user_id) and not (
        isinstance(event, GroupMessageEvent) and is_group_admin(event)
    ):
        query = query.where(FanqieJob.requester_user_id == user_id)
    elif isinstance(event, GroupMessageEvent) and not _is_superuser(user_id):
        query = query.where(FanqieJob.group_id == group_id)
    result = await session.execute(query)
    jobs = list(result.scalars().all())
    if not jobs:
        return "暂无可见番茄任务。"
    lines = ["番茄任务："]
    for job in jobs:
        title = await _book_title(session, job)
        scope = f"群 {job.group_id}" if job.group_id is not None else "私聊"
        lines.append(
            f"#{job.id} 《{title}》 {job.start_chapter}-{job.end_chapter}章｜"
            f"{_STATUS_LABELS.get(job.status, job.status)} "
            f"({job.completed_chapters}/{job.total_chapters})｜{scope}"
        )
    lines.append("详情：/番茄任务 <id>；操作：取消、重试、发送、删除。")
    return "\n".join(lines)


@task_cmd.handle()
async def handle_task_entry(
    event: MessageEvent,
    matcher: Matcher,
    session: async_scoped_session,
    arg: Message = CommandArg(),
    _perm: None = require_feature("fanqie"),  # pyright: ignore[reportArgumentType]
) -> None:
    text = _plain(arg)
    if not text:
        await task_cmd.finish(await _list_text(event, session))
    parts = text.split(maxsplit=1)
    if not parts[0].isdigit():
        await task_cmd.finish("格式：/番茄任务，或 /番茄任务 <id> 取消|重试|发送|删除")
    job_id = int(parts[0])
    job = await _get_job(session, job_id)
    if job is None or not await _can_manage(event, job):
        await task_cmd.finish("找不到该任务，或你没有管理权限。")
    if len(parts) == 1:
        title = await _book_title(session, job)
        await task_cmd.finish(
            f"任务 #{job.id}\n书籍：{title}\n"
            f"状态：{_STATUS_LABELS.get(job.status, job.status)} "
            f"({job.completed_chapters}/{job.total_chapters})\n"
            f"错误：{job.last_error or '无'}\n发送：{job.send_status}"
        )
    action = parts[1].strip()
    if action in {"删除", "delete"}:
        matcher.state["delete_job_id"] = job_id
        await task_cmd.reject_arg(
            "task_confirm",
            f"删除任务 #{job_id} 及其本地文件后不可恢复。发送“确认删除”继续，或“取消”。",
        )
    if action in {"取消", "cancel"}:
        await task_cmd.finish(
            "任务已取消。" if await cancel_job(job_id) else "任务当前不能取消。"
        )
    if action in {"重试", "retry"}:
        ok, error = await retry_job(job_id)
        await task_cmd.finish("任务已重新排队。" if ok else (error or "任务重试失败。"))
    if action in {"发送", "send"}:
        _ok, message = await deliver_job(job_id)
        await task_cmd.finish(message)
    await task_cmd.finish("未知操作，请使用：取消、重试、发送、删除。")


@task_cmd.got("task_confirm")
async def handle_task_confirm(
    event: MessageEvent,
    matcher: Matcher,
    session: async_scoped_session,
    value: Message = Arg("task_confirm"),
) -> None:
    if not await _feature_ok(event, session):
        await task_cmd.finish("功能「番茄小说」当前未开启哦~")
    text = _plain(value)
    job_id = int(matcher.state.get("delete_job_id", 0))
    if text in {"取消", "退出"}:
        await task_cmd.finish("已取消删除。")
    if text not in {"确认删除", "确认", "是", "yes", "Y"}:
        await task_cmd.reject_arg("task_confirm", "请发送“确认删除”继续，或“取消”。")
    job = await _get_job(session, job_id)
    if job is None or not await _can_manage(event, job):
        await task_cmd.finish("任务不存在，或你已没有管理权限。")
    await task_cmd.finish(
        "任务及本地文件已删除。"
        if await delete_job(job_id)
        else "删除失败，请稍后重试。"
    )


__all__ = [
    "fanqie_cmd",
    "handle_fanqie_entry",
    "handle_task_entry",
    "task_cmd",
]
