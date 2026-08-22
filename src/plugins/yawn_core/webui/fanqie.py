# ruff: noqa: TID252,I001,PLW0603,FAST002
"""番茄小说子插件的 WebUI 管理端点。

选书、目录与榜单读取复用子插件 FanqieProvider 的公开页面逻辑；任务
创建、取消、重试、发送与删除复用 state 模块的持久化队列入口（与群内
命令同一路径），保证配额与状态机不被绕过。子插件未加载时所有端点
优雅降级为 503，不影响 WebUI 其余功能。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from nonebot import get_plugin_config, logger
from nonebot_plugin_orm import get_session
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import String, func, or_, select

from ..data_models.bot_group import BotGroup
from ..permission import check_feature_permission
from .config import API_PATH
from .deps import ReadSession, WriteSession, ok, page_params
from .hub import hub
from .service import iso, page_meta

router = APIRouter(prefix=API_PATH)

_state_module: Any = None
_state_resolved = False
_provider_module: Any = None
_provider_resolved = False
_models_module: Any = None
_models_resolved = False
_config_instance: Any = None
_config_resolved = False

_JOB_STATUS_FILTERS = ("all", "queued", "running", "completed", "failed", "cancelled")


def _fanqie_state() -> Any | None:
    """延迟解析番茄任务状态模块；子插件缺失或损坏时返回 None。

    仅在解析成功后缓存；失败不落缓存，子插件随后加载成功时下次
    调用即可自动恢复（与 games.py 的解析器语义一致）。
    """
    global _state_module, _state_resolved
    if not _state_resolved:
        try:
            from ..yawn_fanqie import state as module  # pyright: ignore[reportMissingImports]
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"番茄小说子插件不可用，任务管理降级：{exc}")
            return None
        _state_module = module
        _state_resolved = True
    return _state_module


def _fanqie_provider() -> Any | None:
    """延迟解析番茄 provider 模块；子插件缺失或损坏时返回 None。"""
    global _provider_module, _provider_resolved
    if not _provider_resolved:
        try:
            from ..yawn_fanqie import provider as module  # pyright: ignore[reportMissingImports]
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"番茄小说子插件不可用，选书降级：{exc}")
            return None
        _provider_module = module
        _provider_resolved = True
    return _provider_module


def _fanqie_models() -> Any | None:
    """延迟解析番茄 ORM 模型模块；子插件缺失或损坏时返回 None。"""
    global _models_module, _models_resolved
    if not _models_resolved:
        try:
            from ..yawn_fanqie import models as module  # pyright: ignore[reportMissingImports]
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"番茄小说子插件不可用，任务查询降级：{exc}")
            return None
        _models_module = module
        _models_resolved = True
    return _models_module


def _fanqie_config() -> Any | None:
    """延迟解析番茄子插件配置；子插件缺失时返回 None。"""
    global _config_instance, _config_resolved
    if not _config_resolved:
        try:
            from ..yawn_fanqie.config import Config  # pyright: ignore[reportMissingImports]

            _config_instance = get_plugin_config(Config)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"番茄小说子插件不可用，配置读取降级：{exc}")
            return None
        _config_resolved = True
    return _config_instance


def _require_state() -> Any:
    module = _fanqie_state()
    if module is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "番茄小说子插件未加载")
    return module


def _require_provider() -> Any:
    module = _fanqie_provider()
    if module is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "番茄小说子插件未加载")
    return module


def _require_models() -> Any:
    module = _fanqie_models()
    if module is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "番茄小说子插件未加载")
    return module


def _require_config() -> Any:
    instance = _fanqie_config()
    if instance is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "番茄小说子插件未加载")
    return instance


def _provider_error(exc: Exception) -> HTTPException:
    """把子插件 provider 的异常映射为带语义的 HTTP 错误。"""
    provider = _fanqie_provider()
    if provider is not None and isinstance(exc, provider.FanqieServiceUnavailable):
        return HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            str(exc) or "番茄服务暂时不可用，请稍后重试",
        )
    if provider is not None and isinstance(exc, provider.FanqieProviderError):
        return HTTPException(
            status.HTTP_502_BAD_GATEWAY, str(exc) or "番茄公开页面不可用"
        )
    if isinstance(exc, ValueError):
        return HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc) or "请求参数无效"
        )
    return HTTPException(
        status.HTTP_500_INTERNAL_SERVER_ERROR, "番茄请求失败，请稍后重试"
    )


async def _group_names(group_ids: set[int]) -> dict[int, str | None]:
    """批量取群名；群不在 presence 表中时映射为 None。"""
    if not group_ids:
        return {}
    async with get_session() as db:
        rows = (
            await db.execute(
                select(BotGroup.group_id, BotGroup.group_name).where(
                    BotGroup.group_id.in_(group_ids)
                )
            )
        ).all()
    return {group_id: name for group_id, name in rows}  # noqa: C416


def _serialize_book(book: Any) -> dict[str, Any]:
    return {
        "bookId": book.book_id,
        "title": book.title,
        "author": book.author,
        "description": book.description,
        "url": book.url,
        "rank": book.rank,
        "readCount": book.read_count,
        "wordCount": book.word_count,
    }


def _serialize_job(job: Any, book: Any, group_name: str | None) -> dict[str, Any]:
    return {
        "id": job.id,
        "bookId": book.book_id if book is not None else None,
        "title": book.title if book is not None else None,
        "author": book.author if book is not None else None,
        "requesterUserId": str(job.requester_user_id),
        "groupId": str(job.group_id) if job.group_id is not None else None,
        "groupName": group_name,
        "startChapter": job.start_chapter,
        "endChapter": job.end_chapter,
        "totalChapters": job.total_chapters,
        "completedChapters": job.completed_chapters,
        "status": job.status,
        "cancelRequested": job.cancel_requested,
        "outputName": job.output_name,
        "sendStatus": job.send_status,
        "lastError": job.last_error,
        "sendError": job.send_error,
        "createdAt": iso(job.created_at),
        "startedAt": iso(job.started_at),
        "completedAt": iso(job.completed_at),
    }


# ── 状态与配置读出 ─────────────────────────────────────────


@router.get("/fanqie/status")
async def fanqie_status(_session: ReadSession) -> dict[str, Any]:
    state = _fanqie_state()
    models = _fanqie_models()
    cfg = _fanqie_config()
    available = state is not None and models is not None and cfg is not None
    data: dict[str, Any] = {"available": available}
    if cfg is not None:
        data["limits"] = {
            "maxChapters": cfg.fanqie_max_chapters,
            "userActiveMax": cfg.fanqie_user_active_max,
            "groupActiveMax": cfg.fanqie_group_active_max,
            "queueMax": cfg.fanqie_queue_max,
            "searchLimit": cfg.fanqie_search_limit,
            "rankLimit": cfg.fanqie_rank_limit,
            "fileRetentionHours": cfg.fanqie_file_retention_hours,
        }
    if models is not None:
        async with get_session() as db:
            rows = (
                await db.execute(
                    select(models.FanqieJob.status, func.count())
                    .where(models.FanqieJob.status.in_(("queued", "running")))
                    .group_by(models.FanqieJob.status)
                )
            ).all()
        counts = {row[0]: int(row[1]) for row in rows}
        data["active"] = {key: counts.get(key, 0) for key in ("queued", "running")}
    else:
        data["active"] = None
    return ok(data)


# ── 任务查询与管理（复用 state 模块入口） ──────────────────


@router.get("/fanqie/jobs")
async def list_fanqie_jobs(
    _session: ReadSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=100),
    status_filter: Literal[
        "all", "queued", "running", "completed", "failed", "cancelled"
    ] = Query(default="all", alias="status"),
    search: str = Query(default="", max_length=120),
) -> dict[str, Any]:
    models = _require_models()
    page, page_size = page_params(page, page_size)
    conditions = []
    if status_filter != "all":
        conditions.append(models.FanqieJob.status == status_filter)
    term = search.strip()
    if term:
        pattern = f"%{term}%"
        conditions.append(
            or_(
                models.FanqieBook.title.like(pattern),
                models.FanqieBook.author.like(pattern),
                models.FanqieJob.requester_user_id.cast(String).like(pattern),
                models.FanqieJob.id.cast(String).like(pattern),
            )
        )
    join_on = models.FanqieJob.book_record_id == models.FanqieBook.id
    count_stmt = (
        select(func.count())
        .select_from(models.FanqieJob)
        .join(models.FanqieBook, join_on)
    )
    stmt = (
        select(models.FanqieJob, models.FanqieBook)
        .join(models.FanqieBook, join_on)
        .order_by(models.FanqieJob.id.desc())
    )
    if conditions:
        count_stmt = count_stmt.where(*conditions)
        stmt = stmt.where(*conditions)
    async with get_session() as db:
        total = int(await db.scalar(count_stmt) or 0)
        result = await db.execute(stmt.offset((page - 1) * page_size).limit(page_size))
        rows = list(result.all())
    group_names = await _group_names(
        {row[0].group_id for row in rows if row[0].group_id is not None}
    )
    data = [
        _serialize_job(job, book, group_names.get(job.group_id)) for job, book in rows
    ]
    return ok(data, page_meta(page, page_size, total))


@router.get("/fanqie/jobs/{job_id}")
async def get_fanqie_job(job_id: int, _session: ReadSession) -> dict[str, Any]:
    models = _require_models()
    async with get_session() as db:
        row = (
            await db.execute(
                select(models.FanqieJob, models.FanqieBook)
                .join(
                    models.FanqieBook,
                    models.FanqieJob.book_record_id == models.FanqieBook.id,
                )
                .where(models.FanqieJob.id == job_id)
            )
        ).first()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
        chapter_rows = list(
            (
                await db.execute(
                    select(models.FanqieJobChapter)
                    .where(models.FanqieJobChapter.job_id == job_id)
                    .order_by(models.FanqieJobChapter.chapter_index)
                )
            )
            .scalars()
            .all()
        )
    job, book = row
    group_names = await _group_names(
        {job.group_id} if job.group_id is not None else set()
    )
    chapters = [
        {
            "chapterIndex": chapter.chapter_index,
            "itemId": chapter.item_id,
            "title": chapter.title,
            "isLocked": chapter.is_locked,
            "status": chapter.status,
            "lastError": chapter.last_error,
            "completedAt": iso(chapter.completed_at),
        }
        for chapter in chapter_rows
    ]
    group_name = group_names.get(job.group_id) if job.group_id is not None else None
    return ok(
        {
            "job": _serialize_job(job, book, group_name),
            "chapters": chapters,
        }
    )


class FanqieSubmitBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    source: str = Field(min_length=1, max_length=512)
    start_chapter: int = Field(ge=1, alias="startChapter")
    end_chapter: int = Field(ge=1, alias="endChapter")
    requester_user_id: int = Field(ge=1, le=10**12, alias="requesterUserId")
    group_id: int | None = Field(default=None, ge=1, le=10**12, alias="groupId")


@router.post("/fanqie/jobs")
async def create_fanqie_job(
    body: FanqieSubmitBody, _session: WriteSession
) -> dict[str, Any]:
    state = _require_state()
    cfg = _require_config()
    provider_mod = _require_provider()
    async with get_session() as db:
        allowed = await check_feature_permission(
            body.requester_user_id, body.group_id, "fanqie", db
        )
    if not allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "功能「番茄小说」对该接收人当前未开启"
        )
    try:
        async with provider_mod.FanqieProvider(cfg) as provider:
            book = await provider.resolve_book_reference(body.source)
            chapters = await provider.list_chapters(book.book_id)
    except Exception as exc:
        logger.warning(
            f"WebUI 番茄选书失败：source={body.source[:80]!r} "
            f"error_type={type(exc).__name__} error={exc}"
        )
        raise _provider_error(exc) from exc
    job_id, error = await state.submit_job(
        body.requester_user_id,
        body.group_id,
        book,
        chapters,
        body.start_chapter,
        body.end_chapter,
    )
    if job_id is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, error or "任务创建失败"
        )
    logger.info(
        f"WebUI 创建番茄任务：job_id={job_id} requester={body.requester_user_id}"
    )
    await hub.notify_change("fanqie_job", str(job_id))
    return ok({"jobId": job_id})


@router.post("/fanqie/jobs/{job_id}/cancel")
async def cancel_fanqie_job(job_id: int, _session: WriteSession) -> dict[str, Any]:
    state = _require_state()
    if not await state.cancel_job(job_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在或当前状态不可取消")
    await hub.notify_change("fanqie_job", str(job_id))
    return ok({"cancelled": True})


@router.post("/fanqie/jobs/{job_id}/retry")
async def retry_fanqie_job(job_id: int, _session: WriteSession) -> dict[str, Any]:
    state = _require_state()
    retried, error = await state.retry_job(job_id)
    if not retried:
        raise HTTPException(status.HTTP_409_CONFLICT, error or "任务当前不可重试")
    await hub.notify_change("fanqie_job", str(job_id))
    return ok({"retried": True})


@router.post("/fanqie/jobs/{job_id}/send")
async def send_fanqie_job(job_id: int, _session: WriteSession) -> dict[str, Any]:
    state = _require_state()
    sent, message = await state.deliver_job(job_id)
    if not sent:
        raise HTTPException(status.HTTP_409_CONFLICT, message or "文件发送失败")
    await hub.notify_change("fanqie_job", str(job_id))
    return ok({"sent": True, "message": message})


@router.delete("/fanqie/jobs/{job_id}")
async def delete_fanqie_job(job_id: int, _session: WriteSession) -> dict[str, Any]:
    state = _require_state()
    if not await state.delete_job(job_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    await hub.notify_change("fanqie_job", str(job_id))
    return ok({"deleted": True})


# ── 选书：搜索 / 榜单 / 书籍与目录（只读公开页面） ─────────


@router.get("/fanqie/search")
async def fanqie_search(
    _session: ReadSession,
    keyword: str = Query(min_length=1, max_length=80),
    order: Literal["related", "new", "hot"] = Query(default="related"),
) -> dict[str, Any]:
    cfg = _require_config()
    provider_mod = _require_provider()
    try:
        async with provider_mod.FanqieProvider(cfg) as provider:
            books = await provider.search(keyword, order=order)
    except Exception as exc:
        logger.warning(
            f"WebUI 番茄搜索失败：keyword={keyword[:40]!r} "
            f"error_type={type(exc).__name__} error={exc}"
        )
        raise _provider_error(exc) from exc
    return ok([_serialize_book(book) for book in books])


@router.get("/fanqie/rank/categories")
async def fanqie_rank_categories(_session: ReadSession) -> dict[str, Any]:
    cfg = _require_config()
    provider_mod = _require_provider()
    try:
        async with provider_mod.FanqieProvider(cfg) as provider:
            categories = await provider.list_rank_categories()
    except Exception as exc:
        logger.warning(
            f"WebUI 番茄榜单分类失败：error_type={type(exc).__name__} error={exc}"
        )
        raise _provider_error(exc) from exc
    return ok(
        [
            {
                "gender": gender,
                "categories": [
                    {"categoryId": item.category_id, "name": item.name}
                    for item in items
                ],
            }
            for gender, items in categories.items()
        ]
    )


@router.get("/fanqie/rank/books")
async def fanqie_rank_books(
    _session: ReadSession,
    gender: Literal["male", "female"] = Query(),
    rank_type: Literal["read", "new"] = Query(alias="rankType"),
    category_id: str = Query(
        alias="categoryId", min_length=1, max_length=12, pattern=r"^\d{1,12}$"
    ),
    limit: int | None = Query(default=None, ge=1, le=10),
) -> dict[str, Any]:
    cfg = _require_config()
    provider_mod = _require_provider()
    try:
        async with provider_mod.FanqieProvider(cfg) as provider:
            books = await provider.list_rank_books(
                gender=gender,
                rank_type=rank_type,
                category_id=category_id,
                limit=limit,
            )
    except Exception as exc:
        logger.warning(
            f"WebUI 番茄榜单读取失败：gender={gender} rank_type={rank_type} "
            f"category_id={category_id} error_type={type(exc).__name__} error={exc}"
        )
        raise _provider_error(exc) from exc
    return ok([_serialize_book(book) for book in books])


@router.get("/fanqie/resolve")
async def fanqie_resolve(
    _session: ReadSession,
    source: str = Query(min_length=1, max_length=512),
) -> dict[str, Any]:
    """解析书籍页/阅读页链接或 book ID;source 放查询参数以免斜杠进路径。"""
    cfg = _require_config()
    provider_mod = _require_provider()
    try:
        async with provider_mod.FanqieProvider(cfg) as provider:
            book = await provider.resolve_book_reference(source)
    except Exception as exc:
        logger.warning(
            f"WebUI 番茄链接解析失败：source={source[:80]!r} "
            f"error_type={type(exc).__name__} error={exc}"
        )
        raise _provider_error(exc) from exc
    return ok(_serialize_book(book))


@router.get("/fanqie/books/{book_id}/chapters")
async def fanqie_book_chapters(book_id: str, _session: ReadSession) -> dict[str, Any]:
    cfg = _require_config()
    provider_mod = _require_provider()
    try:
        async with provider_mod.FanqieProvider(cfg) as provider:
            chapters = await provider.list_chapters(book_id)
    except Exception as exc:
        logger.warning(
            f"WebUI 番茄目录读取失败：book_id={book_id[:40]!r} "
            f"error_type={type(exc).__name__} error={exc}"
        )
        raise _provider_error(exc) from exc
    return ok(
        [
            {
                "index": chapter.index,
                "itemId": chapter.item_id,
                "title": chapter.title,
                "isLocked": chapter.is_locked,
            }
            for chapter in chapters
        ]
    )
