# ruff: noqa: TID252,RET504,C901,ASYNC240,PLR0912,PLR0913,PLR0915,SIM105
"""Short-lived media materialization and vision-caption cache for Agent."""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError

from ..data_models.agent_media_cache import AgentMediaCache
from ..llm import ai_config, resolve_llm_request
from .context import now_beijing
from .log import dbg, dbg_exc

MAX_MEDIA_BYTES = 8 * 1024 * 1024
_IMAGE_MIME_PREFIXES = ("image/jpeg", "image/png", "image/gif", "image/webp")


def _allowed_hosts() -> frozenset[str]:
    raw = str(getattr(ai_config, "agent_media_allowed_hosts", "") or "")
    return frozenset(item.strip().lower() for item in raw.split(",") if item.strip())


def _safe_roots() -> tuple[Path, ...]:
    # realpath（而非 Path.resolve）在 Windows 上会解析 junction，
    # 防止缓存目录内的 junction 指向目录外。
    roots = [
        Path(
            os.path.realpath(
                str(getattr(ai_config, "agent_media_cache_dir", "data/agent_media"))
            )
        ),
        Path(os.path.realpath(os.environ.get("AGENT_FILE_ROOT", "data/agent_files"))),
    ]
    return tuple(dict.fromkeys(roots))


def _media_cache_root() -> Path:
    return Path(
        os.path.realpath(
            str(getattr(ai_config, "agent_media_cache_dir", "data/agent_media"))
        )
    )


def _agent_file_root() -> Path:
    return Path(os.path.realpath(os.environ.get("AGENT_FILE_ROOT", "data/agent_files")))


def _inside(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def unlink_cache_file(cache_path: str) -> None:
    """仅删除位于媒体缓存目录内的文件；DB 中的路径不可信。"""

    if not cache_path:
        return
    root = Path(
        os.path.realpath(
            str(getattr(ai_config, "agent_media_cache_dir", "data/agent_media"))
        )
    )
    candidate = Path(os.path.realpath(cache_path))
    if candidate != root and root not in candidate.parents:
        return
    try:
        candidate.unlink(missing_ok=True)
    except OSError:
        pass


def _mime_for_bytes(data: bytes, hint: str | None = None) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    guessed = mimetypes.guess_type(hint or "")[0] or "application/octet-stream"
    return guessed


def _valid_image(data: bytes, hint: str | None = None) -> bool:
    mime = _mime_for_bytes(data, hint)
    return mime.startswith(_IMAGE_MIME_PREFIXES)


def received_media_reuse_policy() -> str:
    """收到过的图片默认禁止二次发送；仅显式 same_group 才允许同群缓存复用。"""

    raw = str(os.environ.get("AGENT_RECEIVED_MEDIA_REUSE", "deny") or "deny")
    normalized = raw.strip().lower().replace("-", "_")
    return "same_group" if normalized == "same_group" else "deny"


async def validate_outbound_image_path(
    path: Path,
    *,
    group_id: int,
    session: Any = None,
) -> Path:
    """校验 Agent 主动发送的本地图片，并落实“收到图片是否可复用”策略。

    - ``AGENT_FILE_ROOT`` 内图片始终可用（包括 reactions 库）。
    - ``AGENT_MEDIA_CACHE_DIR`` 代表收到/物化的临时图片，默认禁止复用；只有
      ``AGENT_RECEIVED_MEDIA_REUSE=same_group`` 且数据库确认属于当前群、未过期时
      才允许发送。
    - 其余本地路径一律拒绝。
    """

    candidate = Path(os.path.realpath(str(path)))
    agent_root = _agent_file_root()
    cache_root = _media_cache_root()
    # 缓存目录即使被部署在 AGENT_FILE_ROOT 下面，也必须先套用“收到图片复用”
    # 策略，不能因为父目录可信而绕过隐私边界。
    if _inside(candidate, (cache_root,)):
        if received_media_reuse_policy() != "same_group":
            raise PermissionError("收到过的图片默认禁止复用")
        if session is None:
            raise PermissionError("复用收到的图片需要数据库会话")
        row = await session.scalar(
            select(AgentMediaCache.id).where(
                AgentMediaCache.group_id == group_id,
                AgentMediaCache.cache_path == str(candidate),
                AgentMediaCache.media_type == "image",
                AgentMediaCache.expires_at >= now_beijing(),
            )
        )
        if row is None:
            raise PermissionError("只能复用当前群仍有效的图片缓存")
    elif _inside(candidate, (agent_root,)):
        pass
    else:
        raise PermissionError("图片不在 Agent 安全目录")  # noqa: TRY003

    if not candidate.is_file():
        raise ValueError("图片文件不存在")
    try:
        size = candidate.stat().st_size
    except OSError as exc:
        raise ValueError("无法读取图片文件") from exc
    if size > MAX_MEDIA_BYTES:
        raise ValueError("图片超过媒体大小限制")
    try:
        data = candidate.read_bytes()
    except OSError as exc:
        raise ValueError("无法读取图片文件") from exc
    if len(data) > MAX_MEDIA_BYTES or not _valid_image(data, candidate.name):
        raise ValueError("文件不是受支持的图片类型")
    return candidate


def _data_url(data: bytes, mime: str) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _url_allowed(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme in {"http", "https"} and bool(host) and host in _allowed_hosts()
    )


async def _fetch_url(url: str) -> bytes | None:
    if not _url_allowed(url):
        dbg(f"媒体下载拒绝: URL 主机不在白名单 {sorted(_allowed_hosts())} url={url!r}")
        return None
    try:
        # QQ 图片 CDN 普遍带签名重定向；流式读取并在超限时尽早中断。
        async with (
            httpx.AsyncClient(timeout=10, follow_redirects=True) as client,
            client.stream("GET", url) as response,
        ):
            response.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes(64 * 1024):
                total += len(chunk)
                if total > MAX_MEDIA_BYTES + 1:
                    break
                chunks.append(chunk)
            data = b"".join(chunks)
    except Exception as exc:  # noqa: BLE001
        dbg_exc(f"Agent 媒体下载失败: {url} ({exc})")
        return None
    return data[: MAX_MEDIA_BYTES + 1]


async def _load_bytes(
    bot: Any, ref: dict[str, Any], *, depth: int = 0
) -> tuple[bytes, str] | None:
    if depth > 1:
        dbg(f"媒体加载放弃: get_image 递归深度超过 1 ref={ref}")
        return None
    url = str(ref.get("url") or "").strip()
    if url.startswith(("http://", "https://")):
        data = await _fetch_url(url)
        if (
            data is not None
            and len(data) <= MAX_MEDIA_BYTES
            and _valid_image(data, url)
        ):
            dbg(f"媒体加载成功(URL): {len(data)} 字节 url={url!r}")
            return data, url
        if data is not None:
            dbg(
                f"媒体加载拒绝(URL): 大小={len(data)} "
                f"有效图片={_valid_image(data, url)} url={url!r}"
            )
    candidate_text = str(ref.get("path") or ref.get("file") or "").strip()
    if candidate_text:
        candidate = Path(os.path.realpath(candidate_text))
        if candidate.is_file() and _inside(candidate, _safe_roots()):
            try:
                if candidate.stat().st_size <= MAX_MEDIA_BYTES:
                    data = candidate.read_bytes()
                    # stat 与读取之间文件可能增长；以实际读到的长度复核。
                    if len(data) <= MAX_MEDIA_BYTES and _valid_image(
                        data, candidate.name
                    ):
                        dbg(
                            f"媒体加载成功(本地文件): {len(data)} 字节 path={candidate}"
                        )
                        return data, candidate.name
                    dbg(
                        f"媒体加载拒绝(本地文件): 大小或 MIME 校验未通过 "
                        f"path={candidate}"
                    )
                else:
                    dbg(
                        f"媒体加载拒绝(本地文件): 超过 {MAX_MEDIA_BYTES} 字节 "
                        f"path={candidate}"
                    )
            except OSError:
                dbg_exc(f"媒体加载失败(本地文件读取异常): path={candidate}")
                return None
        elif candidate_text:
            dbg(
                f"媒体加载跳过(本地文件): 不存在或不在安全目录内 "
                f"path={candidate_text!r}"
            )
    if bot is not None and ref.get("file"):
        try:
            payload = await bot.call_api("get_image", file=str(ref["file"]))
        except Exception:  # noqa: BLE001
            dbg_exc(f"媒体加载: get_image 调用失败 file={ref.get('file')!r}")
            payload = None
        if isinstance(payload, dict):
            nested = dict(ref)
            nested.update(
                {
                    key: payload[key]
                    for key in ("url", "file", "path")
                    if payload.get(key)
                }
            )
            dbg(f"媒体加载: get_image 返回补充信息,递归重试 depth={depth + 1}")
            return await _load_bytes(bot, nested, depth=depth + 1)
    dbg(f"媒体加载失败: 所有途径均无法取得图片字节 ref={ref}")
    return None


async def _find_cache(
    session: Any, group_id: int, content_hash: str, *, model_name: str | None = None
) -> AgentMediaCache | None:
    if session is None:
        return None
    now = now_beijing()
    stmt = select(AgentMediaCache).where(
        AgentMediaCache.group_id == group_id,
        AgentMediaCache.content_hash == content_hash,
        AgentMediaCache.media_type == "image",
        AgentMediaCache.expires_at >= now,
    )
    if model_name is not None:
        stmt = stmt.where(AgentMediaCache.model_name == model_name)
    return await session.scalar(stmt.order_by(AgentMediaCache.id.desc()))


async def prepare_image_inputs(
    bot: Any,
    group_id: int,
    media_refs: list[dict[str, Any]],
    *,
    session: Any = None,
    cache_enabled: bool = False,
    diagnostics: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[tuple[str, str]], list[str]]:
    """Return OpenAI image blocks and cached captions keyed by content hash."""

    blocks: list[dict[str, Any]] = []
    captions: list[tuple[str, str]] = []
    digests: list[str] = []
    for index, ref in enumerate(media_refs):
        diagnostic: dict[str, Any] = {
            "index": index,
            "type": str(ref.get("type") or "unknown"),
            "source": str(ref.get("source") or "current"),
        }
        url = str(ref.get("url") or "").strip()
        if url:
            parsed = urlparse(url)
            diagnostic["url"] = url
            diagnostic["url_allowed"] = _url_allowed(url)
            diagnostic["url_host"] = parsed.hostname or None
        if str(ref.get("type")) != "image":
            diagnostic["status"] = "skipped_non_image"
            if diagnostics is not None:
                diagnostics.append(diagnostic)
            dbg(f"群 {group_id} 媒体跳过: 非图片类型 {ref.get('type')!r}")
            continue
        loaded = await _load_bytes(bot, ref)
        if loaded is None:
            # A provider may still be able to fetch the original URL.  It is
            # only passed through when it was explicitly supplied by OneBot.
            if _url_allowed(url):
                diagnostic["status"] = "url_passthrough"
                dbg(f"群 {group_id} 媒体降级: 直接透传原始 URL 给模型 url={url!r}")
                blocks.append({"type": "image_url", "image_url": {"url": url}})
            else:
                diagnostic["status"] = "dropped_unavailable"
                diagnostic["reason"] = "无法取得图片字节，且原始 URL 不允许直接透传"
                dbg(f"群 {group_id} 媒体丢弃: 无法加载且 URL 不可透传 ref={ref}")
            if diagnostics is not None:
                diagnostics.append(diagnostic)
            continue
        data, hint = loaded
        digest = hashlib.sha256(data).hexdigest()
        digests.append(digest)
        mime = _mime_for_bytes(data, hint)
        diagnostic.update(
            {
                "status": "loaded",
                "size_bytes": len(data),
                "mime": mime,
                "content_hash": digest[:12],
                "load_hint": hint,
                "cache_enabled": cache_enabled,
            }
        )
        dbg(
            f"群 {group_id} 媒体就绪: {len(data)} 字节 sha256={digest[:16]}… "
            f"hint={hint!r}"
        )
        if cache_enabled:
            # 字幕按产出它的视觉模型命中，切换模型后不复用旧结果。
            cached_caption = await _find_cache(
                session,
                group_id,
                digest,
                model_name=resolve_llm_request("agent_image").model,
            )
            if cached_caption is not None and cached_caption.caption:
                diagnostic["caption_cache"] = "hit"
                dbg(
                    f"群 {group_id} 媒体缓存命中字幕: digest={digest[:16]}… "
                    f"caption={cached_caption.caption!r}"
                )
                captions.append((digest, cached_caption.caption))
            else:
                diagnostic["caption_cache"] = "miss"
                dbg(f"群 {group_id} 媒体缓存无可用字幕: digest={digest[:16]}…")
            try:
                cache_dir = Path(
                    os.path.realpath(
                        str(
                            getattr(
                                ai_config, "agent_media_cache_dir", "data/agent_media"
                            )
                        )
                    )
                )
                group_dir = cache_dir / str(group_id)
                group_dir.mkdir(parents=True, exist_ok=True)
                suffix = (
                    mimetypes.guess_extension(_mime_for_bytes(data, hint)) or ".bin"
                )
                path = group_dir / f"{digest}{suffix}"
                if not path.exists():
                    # 先写临时文件再原子替换，避免并发写同一缓存文件报错。
                    temporary = path.with_suffix(path.suffix + ".tmp")
                    temporary.write_bytes(data)
                    temporary.replace(path)
            except OSError:
                # 磁盘写失败时没有可复用的缓存文件；不插入 cache_path 为空
                # 的 DB 行，让下一次遇到同一张图还能重试写盘。
                dbg_exc(f"群 {group_id} 媒体缓存磁盘写入失败 digest={digest[:16]}…")
            else:
                dbg(f"群 {group_id} 媒体缓存磁盘写入完成: {path}")
                existing = await _find_cache(session, group_id, digest, model_name="")
                if existing is None and session is not None:
                    try:
                        async with session.begin_nested():
                            session.add(
                                AgentMediaCache(
                                    group_id=group_id,
                                    content_hash=digest,
                                    media_type="image",
                                    cache_path=str(path),
                                    model_name="",
                                    status="ready",
                                    size_bytes=len(data),
                                    expires_at=now_beijing()
                                    + timedelta(
                                        seconds=max(
                                            int(
                                                getattr(
                                                    ai_config,
                                                    "agent_media_cache_ttl",
                                                    86400,
                                                )
                                            ),
                                            60,
                                        )
                                    ),
                                )
                            )
                            await session.flush()
                    except SQLAlchemyError:
                        # 缓存写失败只回滚 SAVEPOINT；不得回滚主对话事务。
                        dbg_exc(
                            f"群 {group_id} 媒体缓存 DB 行写入失败(已隔离),继续本轮对话"
                        )
                    else:
                        dbg(f"群 {group_id} 媒体缓存 DB 行已写入 digest={digest[:16]}…")
        blocks.append(
            {"type": "image_url", "image_url": {"url": _data_url(data, mime)}}
        )
        if diagnostics is not None:
            diagnostics.append(diagnostic)
    dbg(
        f"群 {group_id} 媒体输入准备完成: blocks={len(blocks)} "
        f"缓存字幕={len(captions)} digests={len(digests)} "
        f"cache_enabled={cache_enabled}"
    )
    return blocks, captions, digests


async def get_cached_caption(
    session: Any, group_id: int, content_hash: str, model_name: str
) -> str | None:
    row = await _find_cache(session, group_id, content_hash, model_name=model_name)
    return row.caption if row is not None else None


async def store_caption(
    session: Any,
    group_id: int,
    content_hash: str,
    caption: str,
    model_name: str,
    *,
    cache_enabled: bool,
) -> None:
    if not cache_enabled or session is None or not caption.strip():
        dbg(
            f"群 {group_id} 字幕存储跳过: cache_enabled={cache_enabled} "
            f"session={'有' if session is not None else '无'} "
            f"caption 为空={not caption.strip()}"
        )
        return
    expires_at = now_beijing() + timedelta(
        seconds=max(int(getattr(ai_config, "agent_media_cache_ttl", 86400)), 60)
    )
    row = await _find_cache(session, group_id, content_hash, model_name=model_name)
    try:
        async with session.begin_nested():
            if row is None:
                session.add(
                    AgentMediaCache(
                        group_id=group_id,
                        content_hash=content_hash,
                        media_type="image",
                        model_name=model_name,
                        status="captioned",
                        caption=caption.strip()[:2000],
                        expires_at=expires_at,
                    )
                )
            else:
                row.caption = caption.strip()[:2000]
                row.status = "captioned"
                row.expires_at = expires_at
            await session.flush()
    except SQLAlchemyError:
        dbg_exc(
            f"群 {group_id} 字幕缓存写入失败(已隔离),不影响本轮回复 "
            f"digest={content_hash[:16]}…"
        )
        return
    if row is not None:
        dbg(f"群 {group_id} 字幕缓存已更新 digest={content_hash[:16]}…")


async def cleanup_media_cache(session: Any, *, now: datetime | None = None) -> int:
    """原子删除过期 DB 行，提交成功后才清理对应磁盘文件。"""

    now = now or now_beijing()
    rows = (
        (
            await session.execute(
                select(AgentMediaCache).where(AgentMediaCache.expires_at < now)
            )
        )
        .scalars()
        .all()
    )
    cache_paths = [str(row.cache_path) for row in rows if row.cache_path]
    result = await session.execute(
        delete(AgentMediaCache).where(AgentMediaCache.expires_at < now)
    )
    await session.flush()
    removed = int(result.rowcount or 0)
    try:
        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        dbg_exc("媒体缓存 DB 清理提交失败,保留磁盘文件等待下次重试")
        raise
    for cache_path in cache_paths:
        unlink_cache_file(cache_path)
    dbg(
        f"媒体缓存清理完成: 过期 {removed} 行"
        f"(提交后清理磁盘文件 {len(cache_paths)} 个)"
    )
    return removed


__all__ = [
    "MAX_MEDIA_BYTES",
    "cleanup_media_cache",
    "get_cached_caption",
    "prepare_image_inputs",
    "store_caption",
]
