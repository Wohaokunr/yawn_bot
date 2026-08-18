"""番茄下载任务的持久化队列与单 worker。"""

# 状态机集中处理恢复、原子文件合并和投递失败分支；保持这些分支在同一
# worker 中可审计。正确性相关的 F/未定义名称检查仍然开启。
# ruff: noqa: E501, PLW0603, PLR0913, PLR0917, PLR0911, PLR0912, C901, PLR0915, TRY301, TRY003, SIM105

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

from nonebot import get_bot, get_driver, get_plugin_config, logger
from nonebot_plugin_localstore import get_plugin_data_dir
from nonebot_plugin_orm import async_scoped_session, get_session
from sqlalchemy import func, select

from ..permission import check_feature_permission  # noqa: TID252
from .config import Config
from .delivery import notify_group, send_file_to_user
from .models import FanqieBook, FanqieJob, FanqieJobChapter, _now_bj
from .provider import (
    BookSummary,
    ChapterRef,
    ChapterUnavailable,
    FanqieProvider,
)

config = get_plugin_config(Config)

ACTIVE_STATUSES = {"queued", "running"}
TERMINAL_CHAPTER_STATUSES = {"completed", "unavailable"}
_MAX_ERROR_LENGTH = 1000
_RETRYABLE_UNAVAILABLE_MARKERS = (
    "第三方全文接口不可用",
    "第三方全文接口失败",
)
_BJ_TZ = timezone(timedelta(hours=8))
_queue: asyncio.Queue[int] | None = None
_worker_task: asyncio.Task[None] | None = None
_queued_ids: set[int] = set()
_queue_lock = asyncio.Lock()
_submit_lock = asyncio.Lock()


@dataclass(frozen=True, slots=True)
class _ChapterSnapshot:
    id: int
    index: int


def safe_filename(name: str, *, fallback: str = "番茄小说") -> str:
    """清理文件名并限制长度，拒绝路径分隔符和控制字符。"""

    clean = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "_", name).strip(" .")
    return (clean or fallback)[:180]


def _data_root() -> Path:
    try:
        root = Path(get_plugin_data_dir()) / "fanqie"
    except Exception:  # noqa: BLE001
        root = Path("data") / "yawn_fanqie"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _owned_path(raw: str | Path) -> Path | None:
    path = Path(raw).resolve()
    root = _data_root()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path


def _has_owned_file(raw: str | None) -> bool:
    path = _owned_path(raw) if raw else None
    return path is not None and path.is_file()


def _is_retryable_unavailable(error: str | None) -> bool:
    """Recognize old terminal rows caused by a remote full-text outage."""

    return bool(error) and any(
        marker in error for marker in _RETRYABLE_UNAVAILABLE_MARKERS
    )


def _chapter_temp_path(job_id: int, chapter_index: int) -> Path:
    return _data_root() / f"job-{job_id}-chapter-{chapter_index}.txt"


def _output_path(job_id: int, title: str) -> tuple[Path, str]:
    filename = f"{safe_filename(title)}-任务{job_id}.txt"
    return _data_root() / filename, filename


async def _ensure_worker() -> None:
    global _queue, _worker_task
    async with _queue_lock:
        if _queue is None:
            _queue = asyncio.Queue(maxsize=config.fanqie_queue_max)
            logger.debug(
                f"番茄任务队列初始化：maxsize={config.fanqie_queue_max}"
            )
        if _worker_task is None or _worker_task.done():
            _worker_task = asyncio.create_task(_worker_loop(), name="fanqie-worker")
            logger.debug("番茄任务 worker 已启动")


async def _enqueue(job_id: int) -> bool:
    await _ensure_worker()
    assert _queue is not None
    async with _queue_lock:
        if job_id in _queued_ids:
            logger.debug(f"番茄任务重复入队请求：job_id={job_id}")
            return True
        try:
            _queue.put_nowait(job_id)
        except asyncio.QueueFull:
            logger.debug(
                f"番茄任务入队失败：job_id={job_id} reason=queue_full "
                f"maxsize={_queue.maxsize}"
            )
            return False
        _queued_ids.add(job_id)
        logger.debug(
            f"番茄任务已入队：job_id={job_id} queue_size={_queue.qsize()}"
        )
        return True


async def _fill_waiting_queue() -> None:
    """从数据库补入仍排队的任务，避免启动时超过队列容量的任务悬挂。"""

    if _queue is None:
        return
    try:
        async with get_session() as session:
            result = await session.execute(
                select(FanqieJob.id)
                .where(FanqieJob.status == "queued")
                .order_by(FanqieJob.id)
                .limit(config.fanqie_queue_max)
            )
            waiting_ids = [int(item) for item in result.scalars().all()]
    except Exception:  # noqa: BLE001
        logger.exception("补入番茄小说等待任务失败")
        return
    logger.debug(
        f"番茄任务恢复入队扫描：候选数={len(waiting_ids)} queue_size={_queue.qsize()}"
    )
    enqueued = 0
    async with _queue_lock:
        if _queue is None:
            return
        for job_id in waiting_ids:
            if _queue.full():
                break
            if job_id in _queued_ids:
                continue
            _queue.put_nowait(job_id)
            _queued_ids.add(job_id)
            enqueued += 1
    logger.debug(f"番茄任务恢复入队完成：enqueued={enqueued}")


async def _worker_loop() -> None:
    while True:
        assert _queue is not None
        job_id = await _queue.get()
        _queued_ids.discard(job_id)
        logger.debug(
            f"番茄任务 worker 取出任务：job_id={job_id} queue_size={_queue.qsize()}"
        )
        try:
            await _run_job(job_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception(f"番茄小说任务 worker 处理失败：job_id={job_id}")
            await _mark_job_failed(job_id, "任务 worker 异常，请重试")
        finally:
            _queue.task_done()
            logger.debug(f"番茄任务 worker 完成任务槽位：job_id={job_id}")
        await _fill_waiting_queue()


async def submit_job(
    requester_user_id: int,
    group_id: int | None,
    book: BookSummary,
    chapters: list[ChapterRef],
    start_chapter: int,
    end_chapter: int,
) -> tuple[int | None, str | None]:
    """校验配额、持久化任务并入有界队列。"""

    logger.debug(
        f"番茄任务创建开始：user_id={requester_user_id} group_id={group_id} "
        f"book_id={book.book_id} start={start_chapter} end={end_chapter} "
        f"chapter_count={len(chapters)}"
    )
    if not chapters or not 1 <= start_chapter <= end_chapter <= len(chapters):
        logger.debug("番茄任务创建拒绝：reason=invalid_range")
        return None, "章节范围无效"
    if end_chapter - start_chapter + 1 > config.fanqie_max_chapters:
        logger.debug("番茄任务创建拒绝：reason=max_chapters")
        return None, f"单次最多下载 {config.fanqie_max_chapters} 章"

    async with _submit_lock:
        async with get_session() as session:
            user_count = await session.scalar(
                select(func.count(FanqieJob.id)).where(
                    FanqieJob.requester_user_id == requester_user_id,
                    FanqieJob.status.in_(ACTIVE_STATUSES),
                )
            )
            if int(user_count or 0) >= config.fanqie_user_active_max:
                logger.debug(
                    f"番茄任务创建拒绝：reason=user_quota user_id={requester_user_id} "
                    f"active_count={user_count}"
                )
                return None, "你已有活动中的番茄任务，请先完成或取消后再创建"
            if group_id is not None:
                group_count = await session.scalar(
                    select(func.count(FanqieJob.id)).where(
                        FanqieJob.group_id == group_id,
                        FanqieJob.status.in_(ACTIVE_STATUSES),
                    )
                )
                if int(group_count or 0) >= config.fanqie_group_active_max:
                    logger.debug(
                        f"番茄任务创建拒绝：reason=group_quota group_id={group_id} "
                        f"active_count={group_count}"
                    )
                    return None, "本群活动中的番茄任务已达上限，请稍后再试"

            book_row = await session.scalar(
                select(FanqieBook).where(FanqieBook.book_id == book.book_id)
            )
            if book_row is None:
                book_row = FanqieBook(
                    book_id=book.book_id,
                    title=book.title[:256],
                    author=(book.author or None),
                    description=(book.description or None),
                    url=book.url or f"https://fanqienovel.com/page/{book.book_id}",
                )
                session.add(book_row)
                await session.flush()
            else:
                book_row.title = book.title[:256]
                book_row.author = book.author or None
                book_row.description = book.description or None
                book_row.url = book.url or book_row.url

            job = FanqieJob(
                book_record_id=book_row.id,
                requester_user_id=requester_user_id,
                group_id=group_id,
                start_chapter=start_chapter,
                end_chapter=end_chapter,
                total_chapters=end_chapter - start_chapter + 1,
            )
            session.add(job)
            await session.flush()
            job_id = int(job.id)
            chapter_count = job.total_chapters
            for chapter in chapters[start_chapter - 1 : end_chapter]:
                session.add(
                    FanqieJobChapter(
                        job_id=job_id,
                        chapter_index=chapter.index,
                        item_id=chapter.item_id,
                        title=chapter.title[:256],
                        is_locked=chapter.is_locked,
                    )
                )
            await session.commit()
            logger.debug(
                f"番茄任务已持久化：job_id={job_id} book_id={book.book_id} "
                f"chapter_count={chapter_count}"
            )

        if not await _enqueue(job_id):
            await _mark_job_failed(job_id, "任务队列已满，请稍后重试")
            logger.debug(f"番茄任务创建后入队失败：job_id={job_id}")
            return None, "任务队列已满，请稍后再试"
    logger.debug(f"番茄任务创建完成：job_id={job_id}")
    return job_id, None


async def _set_status(job_id: int, status: str, error: str | None = None) -> None:
    async with get_session() as session:
        job = await session.get(FanqieJob, job_id)
        if job is None:
            logger.debug(f"番茄任务状态更新跳过：job_id={job_id} reason=missing")
            return
        previous = job.status
        job.status = status
        job.last_error = error[:_MAX_ERROR_LENGTH] if error else None
        job.updated_at = _now_bj()
        if status == "running" and job.started_at is None:
            job.started_at = _now_bj()
        if status in {"completed", "failed", "cancelled"}:
            job.completed_at = _now_bj()
        await session.commit()
    logger.debug(
        f"番茄任务状态更新：job_id={job_id} previous={previous} status={status} "
        f"has_error={bool(error)}"
    )


async def _mark_job_failed(job_id: int, error: str) -> None:
    await _set_status(job_id, "failed", error)


async def _is_cancelled(job_id: int) -> bool:
    async with get_session() as session:
        job = await session.get(FanqieJob, job_id)
        return job is None or job.cancel_requested or job.status == "cancelled"


async def _refresh_progress(session, job: FanqieJob) -> None:  # noqa: ANN001
    count = await session.scalar(
        select(func.count(FanqieJobChapter.id)).where(
            FanqieJobChapter.job_id == job.id,
            FanqieJobChapter.status.in_(TERMINAL_CHAPTER_STATUSES),
        )
    )
    job.completed_chapters = int(count or 0)
    job.updated_at = _now_bj()


async def _mark_chapter(
    job_id: int,
    chapter_id: int,
    status: str,
    *,
    temp_path: str | None = None,
    error: str | None = None,
) -> None:
    async with get_session() as session:
        job = await session.get(FanqieJob, job_id)
        chapter = await session.get(FanqieJobChapter, chapter_id)
        if job is None or chapter is None:
            logger.debug(
                f"番茄章节状态更新跳过：job_id={job_id} chapter_id={chapter_id} "
                "reason=missing"
            )
            return
        previous = chapter.status
        chapter.status = status
        chapter.temp_path = temp_path
        chapter.last_error = error[:_MAX_ERROR_LENGTH] if error else None
        chapter.completed_at = (
            _now_bj() if status in TERMINAL_CHAPTER_STATUSES else None
        )
        await _refresh_progress(session, job)
        chapter_index = chapter.chapter_index
        progress = f"{job.completed_chapters}/{job.total_chapters}"
        await session.commit()
    logger.debug(
        f"番茄章节状态更新：job_id={job_id} chapter_id={chapter_id} "
        f"chapter_index={chapter_index} previous={previous} status={status} "
        f"progress={progress} has_error={bool(error)}"
    )


async def _run_job(job_id: int) -> None:
    logger.debug(f"番茄任务执行开始：job_id={job_id}")
    async with get_session() as session:
        job = await session.get(FanqieJob, job_id)
        if job is None or job.status not in ACTIVE_STATUSES:
            logger.debug(
                f"番茄任务执行跳过：job_id={job_id} "
                f"reason=missing_or_inactive status={job.status if job else None}"
            )
            return
        if job.cancel_requested:
            job.status = "cancelled"
            job.completed_at = _now_bj()
            await session.commit()
            logger.debug(f"番茄任务执行取消：job_id={job_id} reason=cancel_requested")
            return
        book = await session.get(FanqieBook, job.book_record_id)
        chapter_rows = list(
            (
                await session.execute(
                    select(FanqieJobChapter)
                    .where(FanqieJobChapter.job_id == job_id)
                    .order_by(FanqieJobChapter.chapter_index)
                )
            )
            .scalars()
            .all()
        )
        chapter_snapshots = [
            _ChapterSnapshot(id=int(chapter.id), index=chapter.chapter_index)
            for chapter in chapter_rows
        ]
        if book is None:
            await session.commit()
            logger.debug(f"番茄任务执行失败：job_id={job_id} reason=missing_book")
            await _mark_job_failed(job_id, "任务关联的书籍元数据不存在")
            return
        book_title = book.title
        book_author = book.author or "未知作者"
        book_id = book.book_id
        job.status = "running"
        job.started_at = job.started_at or _now_bj()
        job.last_error = None
        await session.commit()
        logger.debug(
            f"番茄任务进入下载：job_id={job_id} book_id={book_id} "
            f"chapter_count={len(chapter_snapshots)}"
        )

    async with FanqieProvider(config) as provider:
        for position, chapter in enumerate(chapter_snapshots):
            if await _is_cancelled(job_id):
                logger.debug(
                    f"番茄任务下载中取消：job_id={job_id} "
                    f"chapter_index={chapter.index}"
                )
                await _set_status(job_id, "cancelled", "用户取消任务")
                return
            async with get_session() as check_session:
                current_job = await check_session.get(FanqieJob, job_id)
                if current_job is None:
                    return
                allowed = await check_feature_permission(
                    current_job.requester_user_id,
                    current_job.group_id,
                    "fanqie",
                    cast("async_scoped_session", check_session),
                )
                if not allowed:
                    logger.debug(
                        f"番茄任务后台权限拦截：job_id={job_id} "
                        f"user_id={current_job.requester_user_id} group_id={current_job.group_id}"
                    )
                    current_job.status = "failed"
                    current_job.last_error = "功能「番茄小说」已关闭，任务已暂停"
                    await check_session.commit()
                    await notify_group(
                        get_bot(),
                        current_job.group_id,
                        f"番茄任务 #{job_id} 已因功能关闭而暂停",
                    )
                    return
                chapter_row = await check_session.get(FanqieJobChapter, chapter.id)
                if chapter_row is None:
                    continue
                if (
                    chapter_row.status in TERMINAL_CHAPTER_STATUSES
                    and chapter_row.temp_path
                    and _has_owned_file(chapter_row.temp_path)
                ) or chapter_row.status == "unavailable":
                    logger.debug(
                        f"番茄章节复用或跳过：job_id={job_id} "
                        f"chapter_index={chapter_row.chapter_index} status={chapter_row.status}"
                    )
                    continue
                catalog_locked = chapter_row.is_locked
                item_id = chapter_row.item_id
                chapter_title = chapter_row.title
            try:
                logger.debug(
                    f"番茄章节请求开始：job_id={job_id} "
                    f"chapter_index={chapter.index} item_id={item_id} "
                    f"catalog_locked={catalog_locked}"
                )
                content = await provider.fetch_chapter(item_id)
            except ChapterUnavailable as exc:
                logger.debug(
                    f"番茄章节不可用：job_id={job_id} "
                    f"chapter_index={chapter.index} reason={exc}"
                )
                await _mark_chapter(job_id, chapter.id, "unavailable", error=str(exc))
                if position + 1 < len(chapter_snapshots):
                    await asyncio.sleep(config.fanqie_request_delay)
                continue
            except Exception as exc:  # noqa: BLE001
                error = f"第{chapter.index}章请求失败：{exc}"
                logger.exception(
                    f"番茄章节请求失败：job_id={job_id} "
                    f"chapter_index={chapter.index} error_type={type(exc).__name__}"
                )
                await _mark_chapter(job_id, chapter.id, "failed", error=error)
                await _mark_job_failed(
                    job_id,
                    error,
                )
                await notify_group(
                    get_bot(),
                    (await _job_group_id(job_id)),
                    f"番茄任务 #{job_id} 下载失败：第{chapter.index}章，请使用“番茄任务 {job_id} 重试”",
                )
                return

            path = _chapter_temp_path(job_id, chapter.index)
            partial = path.with_suffix(path.suffix + ".part")
            partial.write_text(
                f"第{chapter.index}章 {content.title or chapter_title}\n\n{content.content}\n",
                encoding="utf-8",
            )
            partial.replace(path)
            await _mark_chapter(job_id, chapter.id, "completed", temp_path=str(path))
            logger.debug(
                f"番茄章节写入完成：job_id={job_id} "
                f"chapter_index={chapter.index} temp_path={path} "
                f"content_chars={len(content.content)}"
            )
            if position + 1 < len(chapter_snapshots):
                await asyncio.sleep(config.fanqie_request_delay)

    if await _is_cancelled(job_id):
        logger.debug(f"番茄任务合并前取消：job_id={job_id}")
        await _set_status(job_id, "cancelled", "用户取消任务")
        return
    logger.debug(f"番茄任务开始合并投递：job_id={job_id}")
    await _assemble_and_deliver(job_id, book_title, book_author)


async def _job_group_id(job_id: int) -> int | None:
    async with get_session() as session:
        job = await session.get(FanqieJob, job_id)
        return job.group_id if job else None


async def _assemble_and_deliver(
    job_id: int,
    book_title: str,
    book_author: str,
) -> None:
    logger.debug(
        f"番茄任务合并开始：job_id={job_id} book_title={book_title[:80]!r}"
    )
    async with get_session() as session:
        job = await session.get(FanqieJob, job_id)
        if job is None:
            return
        chapters = list(
            (
                await session.execute(
                    select(FanqieJobChapter)
                    .where(FanqieJobChapter.job_id == job_id)
                    .order_by(FanqieJobChapter.chapter_index)
                )
            )
            .scalars()
            .all()
        )
        requester = job.requester_user_id
        group_id = job.group_id
        if not chapters:
            await session.commit()
            logger.debug(f"番茄任务合并失败：job_id={job_id} reason=no_chapters")
            await _mark_job_failed(job_id, "任务没有可合并的章节")
            return

    output, filename = _output_path(job_id, book_title)
    partial = output.with_suffix(output.suffix + ".part")
    try:
        with partial.open("wb") as stream:
            stream.write(f"书名：{book_title}\n作者：{book_author}\n\n".encode())
            for chapter in chapters:
                if chapter.status == "completed" and chapter.temp_path:
                    path = _owned_path(chapter.temp_path)
                    if path is None or not path.is_file():
                        raise FileNotFoundError(
                            f"第{chapter.chapter_index}章临时文件缺失"
                        )
                    stream.write(path.read_bytes())
                else:
                    stream.write(
                        f"第{chapter.chapter_index}章 {chapter.title}\n\n"
                        f"【本章不可用：{chapter.last_error or '公开页面不可访问'}】\n".encode()
                    )
                if stream.tell() > config.fanqie_max_file_bytes:
                    raise ValueError(
                        f"生成文件超过 {config.fanqie_max_file_bytes // (1024 * 1024)} MiB"
                    )
        partial.replace(output)
        logger.debug(
            f"番茄任务文件合并完成：job_id={job_id} output_path={output} "
            f"bytes={output.stat().st_size}"
        )
    except Exception as exc:  # noqa: BLE001
        partial.unlink(missing_ok=True)
        logger.exception(
            f"番茄任务文件合并失败：job_id={job_id} error_type={type(exc).__name__}"
        )
        await _mark_job_failed(job_id, str(exc))
        await notify_group(get_bot(), group_id, f"番茄任务 #{job_id} 合并失败：{exc}")
        return

    async with get_session() as session:
        job = await session.get(FanqieJob, job_id)
        if job is None:
            return
        job.status = "completed"
        job.output_path = str(output)
        job.output_name = filename
        job.send_status = "pending"
        job.last_error = None
        job.completed_at = _now_bj()
        await session.commit()
    logger.debug(
        f"番茄任务状态已完成：job_id={job_id} output_path={output} send_status=pending"
    )

    async with get_session() as permission_session:
        job = await permission_session.get(FanqieJob, job_id)
        if job is None:
            return
        allowed = await check_feature_permission(
            requester,
            group_id,
            "fanqie",
            cast("async_scoped_session", permission_session),
        )
    if not allowed:
        logger.debug(f"番茄任务完成后发送被权限拦截：job_id={job_id}")
        await _set_send_error(job_id, "功能「番茄小说」已关闭，请开启后使用发送命令")
        await notify_group(
            get_bot(), group_id, f"番茄任务 #{job_id} 已完成，但当前功能开关关闭"
        )
        return
    try:
        logger.debug(
            f"番茄任务私聊发送开始：job_id={job_id} requester={requester} "
            f"filename={filename!r}"
        )
        await send_file_to_user(get_bot(), requester, output, filename)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            f"番茄任务私聊发送失败：job_id={job_id} "
            f"error_type={type(exc).__name__}"
        )
        await _set_send_error(job_id, str(exc))
        await notify_group(
            get_bot(),
            group_id,
            f"番茄任务 #{job_id} 已完成，但私聊文件发送失败；请使用“番茄任务 {job_id} 发送”重试",
        )
    else:
        await _set_send_sent(job_id)
        logger.debug(f"番茄任务私聊发送完成：job_id={job_id} requester={requester}")
        await notify_group(
            get_bot(), group_id, f"番茄任务 #{job_id} 已完成，成品已私发给请求者"
        )


async def _set_send_error(job_id: int, error: str) -> None:
    async with get_session() as session:
        job = await session.get(FanqieJob, job_id)
        if job is None:
            return
        job.send_status = "failed"
        job.send_error = error[:_MAX_ERROR_LENGTH]
        await session.commit()
    logger.debug(f"番茄任务发送状态更新：job_id={job_id} status=failed")


async def _set_send_sent(job_id: int) -> None:
    async with get_session() as session:
        job = await session.get(FanqieJob, job_id)
        if job is None:
            return
        job.send_status = "sent"
        job.send_error = None
        await session.commit()
    logger.debug(f"番茄任务发送状态更新：job_id={job_id} status=sent")


async def cancel_job(job_id: int) -> bool:
    logger.debug(f"番茄任务取消开始：job_id={job_id}")
    async with get_session() as session:
        job = await session.get(FanqieJob, job_id)
        if job is None or job.status not in ACTIVE_STATUSES:
            logger.debug(
                f"番茄任务取消拒绝：job_id={job_id} "
                f"status={job.status if job else None}"
            )
            return False
        job.cancel_requested = True
        if job.status == "queued":
            job.status = "cancelled"
            job.completed_at = _now_bj()
        await session.commit()
    logger.debug(f"番茄任务取消标记完成：job_id={job_id}")
    return True


async def retry_job(job_id: int) -> tuple[bool, str | None]:
    logger.debug(f"番茄任务重试开始：job_id={job_id}")
    async with get_session() as session:
        job = await session.get(FanqieJob, job_id)
        if job is None:
            logger.debug(f"番茄任务重试拒绝：job_id={job_id} reason=missing")
            return False, "任务不存在"
        chapters = list(
            (
                await session.execute(
                    select(FanqieJobChapter).where(FanqieJobChapter.job_id == job_id)
                )
            )
            .scalars()
            .all()
        )
        retryable_unavailable = job.status == "completed" and any(
            chapter.status == "unavailable"
            and _is_retryable_unavailable(chapter.last_error)
            for chapter in chapters
        )
        if job.status not in {"failed", "cancelled"} and not retryable_unavailable:
            logger.debug(
                f"番茄任务重试拒绝：job_id={job_id} status={job.status}"
            )
            return False, "只有失败、已取消，或含暂时不可用章节的任务可以重试"
        job.status = "queued"
        job.cancel_requested = False
        job.last_error = None
        job.output_path = None
        job.output_name = None
        job.send_status = "pending"
        job.send_error = None
        for chapter in chapters:
            keep_completed = chapter.status == "completed" and _has_owned_file(
                chapter.temp_path
            )
            keep_unavailable = (
                retryable_unavailable
                and chapter.status == "unavailable"
                and not _is_retryable_unavailable(chapter.last_error)
            )
            if not keep_completed and not keep_unavailable:
                chapter.status = "pending"
                chapter.temp_path = None
                chapter.last_error = None
        await _refresh_progress(session, job)
        await session.commit()
        logger.debug(
            f"番茄任务已重置为排队：job_id={job_id} chapters={len(chapters)}"
        )
    if not await _enqueue(job_id):
        await _mark_job_failed(job_id, "任务队列已满，请稍后重试")
        logger.debug(f"番茄任务重试入队失败：job_id={job_id}")
        return False, "任务队列已满，请稍后再试"
    logger.debug(f"番茄任务重试入队完成：job_id={job_id}")
    return True, None


async def deliver_job(job_id: int) -> tuple[bool, str]:
    logger.debug(f"番茄任务手动发送开始：job_id={job_id}")
    async with get_session() as session:
        job = await session.get(FanqieJob, job_id)
        if job is None:
            logger.debug(f"番茄任务手动发送拒绝：job_id={job_id} reason=missing")
            return False, "任务不存在"
        if job.status != "completed" or not job.output_path or not job.output_name:
            logger.debug(
                f"番茄任务手动发送拒绝：job_id={job_id} status={job.status} "
                f"has_output={bool(job.output_path)}"
            )
            return False, "任务尚未完成或成品已失效"
        allowed = await check_feature_permission(
            job.requester_user_id,
            job.group_id,
            "fanqie",
            cast("async_scoped_session", session),
        )
        if not allowed:
            logger.debug(f"番茄任务手动发送被权限拦截：job_id={job_id}")
            return False, "功能「番茄小说」当前未开启"
        path = _owned_path(job.output_path)
        if path is None or not path.is_file():
            job.status = "failed"
            job.last_error = "成品文件已过期，请重试任务"
            job.send_status = "failed"
            job.send_error = job.last_error
            await session.commit()
            logger.debug(f"番茄任务手动发送失败：job_id={job_id} reason=file_expired")
            return False, "成品文件已过期，任务已转为失败状态，请重试"
        requester = job.requester_user_id
        filename = job.output_name
    try:
        await send_file_to_user(get_bot(), requester, path, filename)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            f"番茄任务手动发送失败：job_id={job_id} error_type={type(exc).__name__}"
        )
        await _set_send_error(job_id, str(exc))
        return False, "文件发送失败，请稍后重试"
    await _set_send_sent(job_id)
    logger.debug(f"番茄任务手动发送完成：job_id={job_id}")
    return True, "文件已发送到你的私聊"


async def delete_job(job_id: int) -> bool:
    logger.debug(f"番茄任务删除开始：job_id={job_id}")
    files: list[Path] = []
    async with get_session() as session:
        job = await session.get(FanqieJob, job_id)
        if job is None:
            logger.debug(f"番茄任务删除跳过：job_id={job_id} reason=missing")
            return False
        if job.output_path:
            path = _owned_path(job.output_path)
            if path:
                files.append(path)
        chapters = list(
            (
                await session.execute(
                    select(FanqieJobChapter).where(FanqieJobChapter.job_id == job_id)
                )
            )
            .scalars()
            .all()
        )
        for chapter in chapters:
            if chapter.temp_path:
                path = _owned_path(chapter.temp_path)
                if path:
                    files.append(path)
            await session.delete(chapter)
        await session.delete(job)
        await session.commit()
    for path in files:
        path.unlink(missing_ok=True)
        path.with_suffix(path.suffix + ".part").unlink(missing_ok=True)
    logger.debug(f"番茄任务删除完成：job_id={job_id} file_count={len(files)}")
    return True


def cleanup_expired_files() -> int:
    """清理超过保留时间的 TXT 与临时章节文件。"""

    root = _data_root()
    cutoff = (
        datetime.now(_BJ_TZ).timestamp() - config.fanqie_file_retention_hours * 3600
    )
    removed = 0
    for path in root.iterdir():
        if not path.is_file() or path.stat().st_mtime >= cutoff:
            continue
        path.unlink(missing_ok=True)
        removed += 1
    logger.debug(
        f"番茄过期文件清理完成：root={root} removed={removed} "
        f"retention_hours={config.fanqie_file_retention_hours}"
    )
    return removed


@get_driver().on_startup
async def _restore_jobs() -> None:
    """恢复 queued/running 任务；迁移未应用时不阻断其它插件启动。"""

    try:
        removed = cleanup_expired_files()
        async with get_session() as session:
            result = await session.execute(
                select(FanqieJob).where(FanqieJob.status.in_(ACTIVE_STATUSES))
            )
            jobs = list(result.scalars().all())
            for job in jobs:
                if job.status == "running":
                    job.status = "queued"
                    job.updated_at = _now_bj()
            await session.commit()
        logger.debug(
            f"番茄任务恢复扫描完成：active_count={len(jobs)} cleaned_files={removed}"
        )
        await _ensure_worker()
        await _fill_waiting_queue()
        logger.info(
            "番茄任务恢复完成：%d 个任务，清理 %d 个过期文件", len(jobs), removed
        )
    except Exception:  # noqa: BLE001
        logger.exception("恢复番茄小说任务失败")


@get_driver().on_shutdown
async def _stop_worker() -> None:
    global _worker_task
    if _worker_task is None:
        return
    _worker_task.cancel()
    try:
        await _worker_task
    except asyncio.CancelledError:
        pass
    _worker_task = None
    logger.debug("番茄任务 worker 已停止")


__all__ = [
    "ACTIVE_STATUSES",
    "cancel_job",
    "cleanup_expired_files",
    "delete_job",
    "deliver_job",
    "retry_job",
    "safe_filename",
    "submit_job",
]
