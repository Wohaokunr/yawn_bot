"""番茄下载任务的持久化队列与单 worker。"""

# 状态机集中处理恢复、原子文件合并和投递失败分支；保持这些分支在同一
# worker 中可审计。正确性相关的 F/未定义名称检查仍然开启。
# ruff: noqa: E501, PLW0603, PLR0913, PLR0917, PLR0911, PLR0912, C901, PLR0915, TRY301, TRY003, SIM105

from __future__ import annotations

import asyncio
import re
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
_BJ_TZ = timezone(timedelta(hours=8))
_queue: asyncio.Queue[int] | None = None
_worker_task: asyncio.Task[None] | None = None
_queued_ids: set[int] = set()
_queue_lock = asyncio.Lock()
_submit_lock = asyncio.Lock()


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
        if _worker_task is None or _worker_task.done():
            _worker_task = asyncio.create_task(_worker_loop(), name="fanqie-worker")


async def _enqueue(job_id: int) -> bool:
    await _ensure_worker()
    assert _queue is not None
    async with _queue_lock:
        if job_id in _queued_ids:
            return True
        try:
            _queue.put_nowait(job_id)
        except asyncio.QueueFull:
            return False
        _queued_ids.add(job_id)
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


async def _worker_loop() -> None:
    while True:
        assert _queue is not None
        job_id = await _queue.get()
        _queued_ids.discard(job_id)
        try:
            await _run_job(job_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("番茄小说任务 worker 处理失败: job_id=%s", job_id)
            await _mark_job_failed(job_id, "任务 worker 异常，请重试")
        finally:
            _queue.task_done()
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

    if not chapters or not 1 <= start_chapter <= end_chapter <= len(chapters):
        return None, "章节范围无效"
    if end_chapter - start_chapter + 1 > config.fanqie_max_chapters:
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
                return None, "你已有活动中的番茄任务，请先完成或取消后再创建"
            if group_id is not None:
                group_count = await session.scalar(
                    select(func.count(FanqieJob.id)).where(
                        FanqieJob.group_id == group_id,
                        FanqieJob.status.in_(ACTIVE_STATUSES),
                    )
                )
                if int(group_count or 0) >= config.fanqie_group_active_max:
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
            for chapter in chapters[start_chapter - 1 : end_chapter]:
                session.add(
                    FanqieJobChapter(
                        job_id=job.id,
                        chapter_index=chapter.index,
                        item_id=chapter.item_id,
                        title=chapter.title[:256],
                        is_locked=chapter.is_locked,
                    )
                )
            await session.commit()
            job_id = job.id

        if not await _enqueue(job_id):
            await _mark_job_failed(job_id, "任务队列已满，请稍后重试")
            return None, "任务队列已满，请稍后再试"
    return job_id, None


async def _set_status(job_id: int, status: str, error: str | None = None) -> None:
    async with get_session() as session:
        job = await session.get(FanqieJob, job_id)
        if job is None:
            return
        job.status = status
        job.last_error = error[:_MAX_ERROR_LENGTH] if error else None
        job.updated_at = _now_bj()
        if status == "running" and job.started_at is None:
            job.started_at = _now_bj()
        if status in {"completed", "failed", "cancelled"}:
            job.completed_at = _now_bj()
        await session.commit()


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
            return
        chapter.status = status
        chapter.temp_path = temp_path
        chapter.last_error = error[:_MAX_ERROR_LENGTH] if error else None
        chapter.completed_at = (
            _now_bj() if status in TERMINAL_CHAPTER_STATUSES else None
        )
        await _refresh_progress(session, job)
        await session.commit()


async def _run_job(job_id: int) -> None:
    async with get_session() as session:
        job = await session.get(FanqieJob, job_id)
        if job is None or job.status not in ACTIVE_STATUSES:
            return
        if job.cancel_requested:
            job.status = "cancelled"
            job.completed_at = _now_bj()
            await session.commit()
            return
        book = await session.get(FanqieBook, job.book_record_id)
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
        if book is None:
            await session.commit()
            await _mark_job_failed(job_id, "任务关联的书籍元数据不存在")
            return
        job.status = "running"
        job.started_at = job.started_at or _now_bj()
        job.last_error = None
        await session.commit()
        book_title = book.title
        book_author = book.author or "未知作者"

    async with FanqieProvider(config) as provider:
        for position, chapter in enumerate(chapters):
            if await _is_cancelled(job_id):
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
                    continue
                is_locked = chapter_row.is_locked
                item_id = chapter_row.item_id
                chapter_title = chapter_row.title
            if is_locked:
                await _mark_chapter(
                    job_id,
                    chapter.id,
                    "unavailable",
                    error="章节需要付费或访问权限",
                )
                continue
            try:
                content = await provider.fetch_chapter(item_id)
            except ChapterUnavailable as exc:
                await _mark_chapter(job_id, chapter.id, "unavailable", error=str(exc))
                if position + 1 < len(chapters):
                    await asyncio.sleep(config.fanqie_request_delay)
                continue
            except Exception as exc:  # noqa: BLE001
                error = f"第{chapter.chapter_index}章请求失败：{exc}"
                await _mark_chapter(job_id, chapter.id, "failed", error=error)
                await _mark_job_failed(
                    job_id,
                    error,
                )
                await notify_group(
                    get_bot(),
                    (await _job_group_id(job_id)),
                    f"番茄任务 #{job_id} 下载失败：第{chapter.chapter_index}章，请使用“番茄任务 {job_id} 重试”",
                )
                return

            path = _chapter_temp_path(job_id, chapter.chapter_index)
            partial = path.with_suffix(path.suffix + ".part")
            partial.write_text(
                f"第{chapter.chapter_index}章 {content.title or chapter_title}\n\n{content.content}\n",
                encoding="utf-8",
            )
            partial.replace(path)
            await _mark_chapter(job_id, chapter.id, "completed", temp_path=str(path))
            if position + 1 < len(chapters):
                await asyncio.sleep(config.fanqie_request_delay)

    if await _is_cancelled(job_id):
        await _set_status(job_id, "cancelled", "用户取消任务")
        return
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
    except Exception as exc:  # noqa: BLE001
        partial.unlink(missing_ok=True)
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
        await _set_send_error(job_id, "功能「番茄小说」已关闭，请开启后使用发送命令")
        await notify_group(
            get_bot(), group_id, f"番茄任务 #{job_id} 已完成，但当前功能开关关闭"
        )
        return
    try:
        await send_file_to_user(get_bot(), requester, output, filename)
    except Exception as exc:  # noqa: BLE001
        await _set_send_error(job_id, str(exc))
        await notify_group(
            get_bot(),
            group_id,
            f"番茄任务 #{job_id} 已完成，但私聊文件发送失败；请使用“番茄任务 {job_id} 发送”重试",
        )
    else:
        await _set_send_sent(job_id)
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


async def _set_send_sent(job_id: int) -> None:
    async with get_session() as session:
        job = await session.get(FanqieJob, job_id)
        if job is None:
            return
        job.send_status = "sent"
        job.send_error = None
        await session.commit()


async def cancel_job(job_id: int) -> bool:
    async with get_session() as session:
        job = await session.get(FanqieJob, job_id)
        if job is None or job.status not in ACTIVE_STATUSES:
            return False
        job.cancel_requested = True
        if job.status == "queued":
            job.status = "cancelled"
            job.completed_at = _now_bj()
        await session.commit()
    return True


async def retry_job(job_id: int) -> tuple[bool, str | None]:
    async with get_session() as session:
        job = await session.get(FanqieJob, job_id)
        if job is None:
            return False, "任务不存在"
        if job.status not in {"failed", "cancelled"}:
            return False, "只有失败或已取消的任务可以重试"
        job.status = "queued"
        job.cancel_requested = False
        job.last_error = None
        job.output_path = None
        job.output_name = None
        job.send_status = "pending"
        job.send_error = None
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
            if chapter.status != "completed" or not _has_owned_file(chapter.temp_path):
                chapter.status = "pending"
                chapter.temp_path = None
                chapter.last_error = None
        await _refresh_progress(session, job)
        await session.commit()
    if not await _enqueue(job_id):
        await _mark_job_failed(job_id, "任务队列已满，请稍后重试")
        return False, "任务队列已满，请稍后再试"
    return True, None


async def deliver_job(job_id: int) -> tuple[bool, str]:
    async with get_session() as session:
        job = await session.get(FanqieJob, job_id)
        if job is None:
            return False, "任务不存在"
        if job.status != "completed" or not job.output_path or not job.output_name:
            return False, "任务尚未完成或成品已失效"
        allowed = await check_feature_permission(
            job.requester_user_id,
            job.group_id,
            "fanqie",
            cast("async_scoped_session", session),
        )
        if not allowed:
            return False, "功能「番茄小说」当前未开启"
        path = _owned_path(job.output_path)
        if path is None or not path.is_file():
            job.status = "failed"
            job.last_error = "成品文件已过期，请重试任务"
            job.send_status = "failed"
            job.send_error = job.last_error
            await session.commit()
            return False, "成品文件已过期，任务已转为失败状态，请重试"
        requester = job.requester_user_id
        filename = job.output_name
    try:
        await send_file_to_user(get_bot(), requester, path, filename)
    except Exception as exc:  # noqa: BLE001
        await _set_send_error(job_id, str(exc))
        return False, "文件发送失败，请稍后重试"
    await _set_send_sent(job_id)
    return True, "文件已发送到你的私聊"


async def delete_job(job_id: int) -> bool:
    files: list[Path] = []
    async with get_session() as session:
        job = await session.get(FanqieJob, job_id)
        if job is None:
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
