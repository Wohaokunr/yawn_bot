"""番茄小说交互命令与任务管理权限。"""

# 交互式 matcher 的参数和中文提示会触发少量项目未采用的风格规则。
# 保留 F/未定义名称等正确性检查。
# ruff: noqa: E501, TRY003, TC002

from __future__ import annotations

import re
from typing import cast

from nonebot import get_driver, get_plugin_config, logger, on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageEvent
from nonebot.exception import RejectedException
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

_FANQIE_CHOICE_KEY = "fanqie_choice"
_FANQIE_STEP_INPUT = "input"
_FANQIE_STEP_BOOK = "book"
_FANQIE_STEP_RANGE = "range"
_FANQIE_STEP_CONFIRM = "confirm"


def _plain(value: Message) -> str:
    return str(value).strip()


def _debug_text(value: object, limit: int = 160) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


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
    user_id = _user_id(event)
    group_id = _group_id(event)
    allowed = await check_feature_permission(
        user_id,
        group_id,
        "fanqie",
        session,
    )
    logger.debug(
        f"番茄权限检查：user_id={user_id} group_id={group_id} allowed={allowed}"
    )
    return allowed


async def _set_book_and_ask_range(
    matcher: Matcher,
    book: BookSummary,
    chapters: list[ChapterRef],
) -> None:
    matcher.state["book"] = book
    matcher.state["chapters"] = chapters
    matcher.state["fanqie_step"] = _FANQIE_STEP_RANGE
    logger.debug(
        f"番茄交互状态转移：选择书籍 book_id={book.book_id} "
        f"chapter_count={len(chapters)}"
    )
    await fanqie_cmd.reject_arg(
        _FANQIE_CHOICE_KEY,
        f"已选择《{book.title}》（作者：{book.author}），共 {len(chapters)} 章。\n"
        "请输入章节范围：全书，或 起始章-结束章（例如 1-20）。",
    )


@fanqie_cmd.handle()
async def handle_fanqie_entry(
    matcher: Matcher,
    arg: Message = CommandArg(),
    _perm: None = require_feature("fanqie"),  # pyright: ignore[reportArgumentType]
) -> None:
    matcher.state["fanqie_step"] = _FANQIE_STEP_INPUT
    value = _plain(arg)
    if value:
        # 将命令同行参数交给统一 got handler，避免入口 handler 在 reject_arg
        # 后被 NoneBot 重新执行，导致下一条消息再次走搜索分支。
        matcher.set_arg(_FANQIE_CHOICE_KEY, arg)


async def _begin_fanqie_input(matcher: Matcher, value: str) -> None:
    try:
        source_kind, _source_id = parse_source(value)
    except ValueError:
        source_kind = "search"
    logger.debug(
        f"番茄查询开始：source_kind={source_kind} value={_debug_text(value)!r}"
    )
    if source_kind in {"page", "reader"}:
        async with FanqieProvider(config) as provider:
            try:
                book = await provider.resolve_book_reference(value)
                chapters = await provider.list_chapters(book.book_id)
            except RejectedException:
                logger.debug("番茄查询交互异常继续向 NoneBot 状态机传播")
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    f"番茄书籍链接查询失败：source_kind={source_kind} "
                    f"value={_debug_text(value)!r} error_type={type(exc).__name__}"
                )
                matcher.state["fanqie_step"] = _FANQIE_STEP_INPUT
                await fanqie_cmd.reject_arg(_FANQIE_CHOICE_KEY, f"查询失败：{exc}")
                return
        logger.debug(
            f"番茄书籍链接查询完成：book_id={book.book_id} "
            f"chapter_count={len(chapters)}"
        )
        await _set_book_and_ask_range(matcher, book, chapters)
        return

    async with FanqieProvider(config) as provider:
        try:
            results = await provider.search(value)
        except RejectedException:
            logger.debug("番茄搜索交互异常继续向 NoneBot 状态机传播")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                f"番茄书名查询失败：value={_debug_text(value)!r} "
                f"error_type={type(exc).__name__}"
            )
            matcher.state["fanqie_step"] = _FANQIE_STEP_INPUT
            await fanqie_cmd.reject_arg(_FANQIE_CHOICE_KEY, f"查询失败：{exc}")
            return
    if not results:
        logger.debug(f"番茄搜索完成但无结果：value={_debug_text(value)!r}")
        matcher.state["fanqie_step"] = _FANQIE_STEP_INPUT
        await fanqie_cmd.reject_arg(
            _FANQIE_CHOICE_KEY, "没有找到公开书籍，请换个书名或直接输入链接。"
        )
        return
    matcher.state["search_results"] = results
    matcher.state["fanqie_step"] = _FANQIE_STEP_BOOK
    lines = ["请选择小说编号："]
    lines.extend(
        f"{index}. 《{book.title}》｜作者：{book.author}｜ID：{book.book_id}"
        for index, book in enumerate(results, 1)
    )
    lines.append("发送“取消”退出。")
    logger.debug(
        f"番茄搜索完成：value={_debug_text(value)!r} result_count={len(results)} "
        f"book_ids={[book.book_id for book in results]}"
    )
    await fanqie_cmd.reject_arg(_FANQIE_CHOICE_KEY, "\n".join(lines))


@fanqie_cmd.got(
    _FANQIE_CHOICE_KEY,
    prompt="请输入书名、fanqienovel.com 书籍/阅读页链接，或 book ID。发送“取消”退出。",
)
async def handle_fanqie_choice(
    event: MessageEvent,
    matcher: Matcher,
    session: async_scoped_session,
    value: Message = Arg(_FANQIE_CHOICE_KEY),
) -> None:
    if not await _feature_ok(event, session):
        await fanqie_cmd.finish("功能「番茄小说」当前未开启哦~")
    text = _plain(value)
    if text in {"取消", "退出"}:
        await fanqie_cmd.finish("已退出番茄小说下载。")

    step = matcher.state.get("fanqie_step", _FANQIE_STEP_INPUT)
    if step == _FANQIE_STEP_INPUT:
        await _begin_fanqie_input(matcher, text)
        return
    if step == _FANQIE_STEP_BOOK:
        await _handle_fanqie_book(matcher, text)
        return
    if step == _FANQIE_STEP_RANGE:
        await _handle_fanqie_range(matcher, text)
        return
    if step == _FANQIE_STEP_CONFIRM:
        await _handle_fanqie_confirm(event, matcher, text)
        return
    await fanqie_cmd.finish("番茄小说会话已过期，请重新发送 /番茄小说。")


async def _handle_fanqie_book(matcher: Matcher, text: str) -> None:
    results = cast("list[BookSummary]", matcher.state.get("search_results", []))
    if not text.isdigit() or not 1 <= int(text) <= len(results):
        matcher.state["fanqie_step"] = _FANQIE_STEP_BOOK
        await fanqie_cmd.reject_arg(
            _FANQIE_CHOICE_KEY, "请输入搜索结果编号，例如 1。"
        )
    book = results[int(text) - 1]
    logger.debug(
        f"番茄搜索结果选择：selection={text} book_id={book.book_id}"
    )
    async with FanqieProvider(config) as provider:
        try:
            chapters = await provider.list_chapters(book.book_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                f"番茄目录读取失败：book_id={book.book_id} "
                f"error_type={type(exc).__name__}"
            )
            matcher.state["fanqie_step"] = _FANQIE_STEP_BOOK
            await fanqie_cmd.reject_arg(_FANQIE_CHOICE_KEY, f"读取目录失败：{exc}")
            return
    await _set_book_and_ask_range(matcher, book, chapters)


async def _handle_fanqie_range(matcher: Matcher, text: str) -> None:
    chapters = cast("list[ChapterRef]", matcher.state.get("chapters", []))
    book = cast("BookSummary", matcher.state.get("book"))
    try:
        start, end = _parse_range(text, len(chapters), config.fanqie_max_chapters)
    except ValueError as exc:
        matcher.state["fanqie_step"] = _FANQIE_STEP_RANGE
        await fanqie_cmd.reject_arg(_FANQIE_CHOICE_KEY, str(exc))
    logger.debug(
        f"番茄章节范围选择：book_id={book.book_id} start={start} end={end}"
    )
    matcher.state["start_chapter"] = start
    matcher.state["end_chapter"] = end
    matcher.state["fanqie_step"] = _FANQIE_STEP_CONFIRM
    await fanqie_cmd.reject_arg(
        _FANQIE_CHOICE_KEY,
        f"将下载《{book.title}》第 {start}-{end} 章，共 {end - start + 1} 章。\n"
        "发送“确认”开始后台下载，发送“取消”退出。",
    )


async def _handle_fanqie_confirm(
    event: MessageEvent,
    matcher: Matcher,
    text: str,
) -> None:
    if text not in {"确认", "确定", "是", "yes", "Y"}:
        await fanqie_cmd.reject_arg(
            _FANQIE_CHOICE_KEY, "请发送“确认”开始，或发送“取消”退出。"
        )
    book = cast("BookSummary", matcher.state["book"])
    chapters = cast("list[ChapterRef]", matcher.state["chapters"])
    logger.debug(
        f"番茄任务提交：user_id={_user_id(event)} group_id={_group_id(event)} "
        f"book_id={book.book_id} start={matcher.state['start_chapter']} "
        f"end={matcher.state['end_chapter']}"
    )
    job_id, error = await submit_job(
        _user_id(event),
        _group_id(event),
        book,
        chapters,
        int(matcher.state["start_chapter"]),
        int(matcher.state["end_chapter"]),
    )
    if error:
        logger.debug(f"番茄任务提交拒绝：error={error}")
        await fanqie_cmd.finish(error)
    logger.debug(f"番茄任务提交成功：job_id={job_id}")
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
    is_superuser = _is_superuser(user_id)
    is_owner = user_id == job.requester_user_id
    is_group_admin_user = (
        isinstance(event, GroupMessageEvent)
        and job.group_id == int(event.group_id)
        and is_group_admin(event)
    )
    allowed = is_superuser or is_owner or is_group_admin_user
    logger.debug(
        f"番茄任务权限检查：job_id={job.id} user_id={user_id} "
        f"allowed={allowed} superuser={is_superuser} owner={is_owner} "
        f"group_admin={is_group_admin_user}"
    )
    return allowed


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
    logger.debug(
        f"番茄任务列表查询：user_id={user_id} group_id={group_id} count={len(jobs)}"
    )
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
    logger.debug(
        f"番茄任务命令：user_id={_user_id(event)} group_id={_group_id(event)} "
        f"arg={_debug_text(text)!r}"
    )
    if not text:
        await task_cmd.finish(await _list_text(event, session))
    parts = text.split(maxsplit=1)
    if not parts[0].isdigit():
        await task_cmd.finish("格式：/番茄任务，或 /番茄任务 <id> 取消|重试|发送|删除")
    job_id = int(parts[0])
    job = await _get_job(session, job_id)
    if job is None or not await _can_manage(event, job):
        logger.debug(
            f"番茄任务访问拒绝或不存在：job_id={job_id} "
            f"user_id={_user_id(event)}"
        )
        await task_cmd.finish("找不到该任务，或你没有管理权限。")
    if len(parts) == 1:
        logger.debug(f"番茄任务详情查询：job_id={job_id}")
        title = await _book_title(session, job)
        await task_cmd.finish(
            f"任务 #{job.id}\n书籍：{title}\n"
            f"状态：{_STATUS_LABELS.get(job.status, job.status)} "
            f"({job.completed_chapters}/{job.total_chapters})\n"
            f"错误：{job.last_error or '无'}\n发送：{job.send_status}"
        )
    action = parts[1].strip()
    logger.debug(f"番茄任务操作：job_id={job_id} action={action}")
    if action in {"删除", "delete"}:
        matcher.state["delete_job_id"] = job_id
        await task_cmd.reject_arg(
            "task_confirm",
            f"删除任务 #{job_id} 及其本地文件后不可恢复。发送“确认删除”继续，或“取消”。",
        )
    if action in {"取消", "cancel"}:
        logger.debug(f"番茄任务取消请求：job_id={job_id}")
        await task_cmd.finish(
            "任务已取消。" if await cancel_job(job_id) else "任务当前不能取消。"
        )
    if action in {"重试", "retry"}:
        logger.debug(f"番茄任务重试请求：job_id={job_id}")
        ok, error = await retry_job(job_id)
        await task_cmd.finish("任务已重新排队。" if ok else (error or "任务重试失败。"))
    if action in {"发送", "send"}:
        logger.debug(f"番茄任务发送请求：job_id={job_id}")
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
        logger.debug(
            f"番茄任务二次确认后访问拒绝或不存在：job_id={job_id} "
            f"user_id={_user_id(event)}"
        )
        await task_cmd.finish("任务不存在，或你已没有管理权限。")
    logger.debug(f"番茄任务删除请求确认：job_id={job_id}")
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
