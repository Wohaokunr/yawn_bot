# ruff: noqa: TID252,RET504,C901,ASYNC240,PLR0912,PLR0913,PLR0915,SIM105,TRY300
"""Short-lived media materialization and vision-caption cache for Agent."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import mimetypes
import os
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError

from ..data_models.agent_media_cache import AgentMediaCache
from ..llm import (
    LLMTask,
    ai_config,
    get_client,
    resolve_llm_request,
    resolve_provider,
)
from . import media_store
from .context import now_beijing
from .log import dbg, dbg_exc
from .media_provider import (
    DeepSeekFileProvider,
    InlineMediaProvider,
    MediaInput,
    MediaProvider,
    is_deepseek_files_endpoint,
    provider_scope,
)

MAX_MEDIA_BYTES = 64 * 1024 * 1024
INLINE_MEDIA_MAX_BYTES = 32 * 1024 * 1024
_HTTP_NOT_FOUND = 404
_FILE_ID_HINT_TAIL = 4
_IMAGE_MIME_PREFIXES = ("image/jpeg", "image/png", "image/gif", "image/webp")
_MEDIA_UPLOAD_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}

MediaResolutionStatus = Literal[
    "remote_file_ready",
    "inline_file_ready",
    "image_url_ready",
    "caption_ready",
    "unavailable",
]


@dataclass(frozen=True, slots=True)
class MediaResolution:
    status: MediaResolutionStatus
    content_hash: str
    source: str
    block: dict[str, Any] | None = None
    caption: str | None = None
    reason: str | None = None
    asset_id: int | None = None
    remote_file_id: str | None = None
    transport: str | None = None


@dataclass(frozen=True, slots=True)
class RemoteDeleteReport:
    cleaned: tuple[tuple[str, str, str], ...]
    failed: tuple[tuple[str, str, str], ...]


def _media_upload_lock(provider_scope_value: str, content_hash: str) -> asyncio.Lock:
    key = (provider_scope_value, content_hash)
    lock = _MEDIA_UPLOAD_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _MEDIA_UPLOAD_LOCKS[key] = lock
    return lock


def _max_media_bytes() -> int:
    configured = int(
        getattr(ai_config, "agent_remote_media_max_bytes", MAX_MEDIA_BYTES)
        or MAX_MEDIA_BYTES
    )
    return min(max(configured, 1), MAX_MEDIA_BYTES)


def _remote_media_enabled() -> bool:
    return bool(getattr(ai_config, "agent_remote_media_enabled", True))


def _remote_media_ttl_seconds() -> int:
    days = int(getattr(ai_config, "agent_remote_media_ttl_days", 7) or 7)
    return min(max(days * 86400, 3600), 2_592_000)


def _remote_media_provider_mode() -> str:
    return (
        str(getattr(ai_config, "agent_remote_media_provider", "auto") or "auto")
        .strip()
        .lower()
    )


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
        asset_owned = False
        try:
            asset_owned = await media_store.has_local_cache_path(
                session, group_id=group_id, cache_path=str(candidate)
            )
        except SQLAlchemyError:
            # 兼容迁移前的旧数据库；legacy AgentMediaCache 仍然可判定权限。
            asset_owned = False
        if row is None and not asset_owned:
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
    max_bytes = _max_media_bytes()
    if size > max_bytes:
        raise ValueError("图片超过媒体大小限制")
    try:
        data = candidate.read_bytes()
    except OSError as exc:
        raise ValueError("无法读取图片文件") from exc
    if len(data) > max_bytes or not _valid_image(data, candidate.name):
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
    host = (urlparse(url).hostname or "unknown").lower()
    if not _url_allowed(url):
        dbg(f"媒体下载拒绝: URL 主机不在白名单 host={host!r}")
        return None
    try:
        # QQ 图片 CDN 普遍带签名重定向；流式读取并在超限时尽早中断。
        max_bytes = _max_media_bytes()
        async with (
            httpx.AsyncClient(timeout=10, follow_redirects=True) as client,
            client.stream("GET", url) as response,
        ):
            response.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes(64 * 1024):
                total += len(chunk)
                if total > max_bytes + 1:
                    break
                chunks.append(chunk)
            data = b"".join(chunks)
    except Exception as exc:  # noqa: BLE001
        dbg_exc(f"Agent 媒体下载失败: host={host!r} ({type(exc).__name__})")
        return None
    return data[: max_bytes + 1]


async def _load_bytes(
    bot: Any,
    ref: dict[str, Any],
    *,
    depth: int = 0,
    diagnostic: dict[str, Any] | None = None,
) -> tuple[bytes, str] | None:
    if diagnostic is not None:
        diagnostic.setdefault("onebot_file", bool(ref.get("file")))
        diagnostic.setdefault("get_image_status", "not_needed")
        diagnostic.setdefault("image_read_status", "pending")
    if depth > 1:
        if diagnostic is not None:
            diagnostic["image_read_status"] = "failed"
            diagnostic["read_source"] = "get_image"
        dbg(
            "媒体加载放弃: get_image 递归深度超过 1 "
            f"source={str(ref.get('source') or 'current')!r}"
        )
        return None
    max_bytes = _max_media_bytes()
    url = str(ref.get("url") or "").strip()
    url_host = (urlparse(url).hostname or "") if url else ""
    if url.startswith(("http://", "https://")):
        data = await _fetch_url(url)
        if (
            data is not None
            and len(data) <= max_bytes
            and _valid_image(data, url)
        ):
            if diagnostic is not None:
                diagnostic["image_read_status"] = "success"
                diagnostic["read_source"] = "url"
            dbg(f"媒体加载成功(URL): {len(data)} 字节 host={url_host!r}")
            return data, url
        if data is not None:
            dbg(
                f"媒体加载拒绝(URL): 大小={len(data)} "
                f"有效图片={_valid_image(data, url)} host={url_host!r}"
            )
    candidate_text = str(ref.get("path") or ref.get("file") or "").strip()
    if candidate_text:
        candidate = Path(os.path.realpath(candidate_text))
        if candidate.is_file() and _inside(candidate, _safe_roots()):
            try:
                if candidate.stat().st_size <= max_bytes:
                    data = candidate.read_bytes()
                    # stat 与读取之间文件可能增长；以实际读到的长度复核。
                    if len(data) <= max_bytes and _valid_image(
                        data, candidate.name
                    ):
                        if diagnostic is not None:
                            diagnostic["image_read_status"] = "success"
                            diagnostic["read_source"] = "local_file"
                        dbg(
                            f"媒体加载成功(本地文件): {len(data)} 字节"
                        )
                        return data, candidate.name
                    dbg(
                        "媒体加载拒绝(本地文件): 大小或 MIME 校验未通过"
                    )
                else:
                    dbg(
                        f"媒体加载拒绝(本地文件): 超过 {max_bytes} 字节"
                    )
            except OSError:
                dbg_exc("媒体加载失败(本地文件读取异常)")
                return None
        elif candidate_text:
            dbg("媒体加载跳过(本地文件): 不存在或不在安全目录内")
    if bot is not None and ref.get("file"):
        if diagnostic is not None:
            diagnostic["get_image_status"] = "attempted"
        try:
            payload = await bot.call_api("get_image", file=str(ref["file"]))
        except Exception:  # noqa: BLE001
            if diagnostic is not None:
                diagnostic["get_image_status"] = "failed"
            dbg_exc("媒体加载: get_image 调用失败")
            payload = None
        if isinstance(payload, dict):
            if diagnostic is not None:
                diagnostic["get_image_status"] = "success"
            nested = dict(ref)
            nested.update(
                {
                    key: payload[key]
                    for key in ("url", "file", "path")
                    if payload.get(key)
                }
            )
            dbg(f"媒体加载: get_image 返回补充信息,递归重试 depth={depth + 1}")
            return await _load_bytes(
                bot,
                nested,
                depth=depth + 1,
                diagnostic=diagnostic,
            )
    if diagnostic is not None:
        diagnostic["image_read_status"] = "failed"
    dbg(
        "媒体加载失败: 所有途径均无法取得图片字节 "
        f"source={str(ref.get('source') or 'current')!r} "
        f"has_file={bool(ref.get('file'))} host={url_host!r}"
    )
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


def _media_ttl_seconds() -> int:
    # 第一版统一使用远端媒体 TTL，避免本地索引先于 DeepSeek file_id 过期，
    # 也避免同一资产在 1 天/7 天两套生命周期之间反复重建。
    return _remote_media_ttl_seconds()


def _materialize_cache_file(
    group_id: int,
    digest: str,
    data: bytes,
    mime_type: str,
) -> Path | None:
    try:
        cache_dir = _media_cache_root()
        group_dir = cache_dir / str(group_id)
        group_dir.mkdir(parents=True, exist_ok=True)
        suffix = mimetypes.guess_extension(mime_type) or ".bin"
        path = group_dir / f"{digest}{suffix}"
        if not path.exists():
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_bytes(data)
            temporary.replace(path)
        return path
    except OSError:
        dbg_exc(f"群 {group_id} 媒体缓存磁盘写入失败 digest={digest[:16]}…")
        return None


def _provider_for_task(task: LLMTask) -> MediaProvider:
    request = resolve_llm_request(task)
    base_url, api_key = resolve_provider(request.provider)
    scope = provider_scope(request.provider, api_key)
    client = get_client(request.provider)
    mode = _remote_media_provider_mode()
    remote_selected = mode in {"auto", "deepseek", request.provider.lower()}
    if (
        _remote_media_enabled()
        and remote_selected
        and client is not None
        and is_deepseek_files_endpoint(base_url)
    ):
        return DeepSeekFileProvider(request.provider, scope, client)
    return InlineMediaProvider(request.provider, scope)


def _exception_status_code(exc: Exception) -> int | None:
    raw = getattr(exc, "status_code", None)
    if raw is None:
        raw = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


async def delete_remote_media_files_detailed(
    remote_refs: list[tuple[str, str, str]],
) -> RemoteDeleteReport:
    """Delete remote files and retain failed refs for a later cleanup retry."""

    cleaned: list[tuple[str, str, str]] = []
    failed: list[tuple[str, str, str]] = []
    for ref in dict.fromkeys(remote_refs):
        provider_id, stored_scope, remote_file_id = ref
        try:
            base_url, api_key = resolve_provider(provider_id)
            current_scope = provider_scope(provider_id, api_key)
            if current_scope != stored_scope:
                # A rotated credential cannot safely address the old file. Keep the
                # row pending until its provider TTL proves the object has expired.
                failed.append(ref)
                continue
            client = get_client(provider_id)
            if client is None or not is_deepseek_files_endpoint(base_url):
                failed.append(ref)
                continue
            provider = DeepSeekFileProvider(provider_id, current_scope, client)
            if await provider.delete(remote_file_id):
                cleaned.append(ref)
            else:
                failed.append(ref)
        except Exception as exc:  # noqa: BLE001
            if _exception_status_code(exc) == _HTTP_NOT_FOUND:
                # DELETE is idempotent for lifecycle purposes: already gone is clean.
                cleaned.append(ref)
                continue
            failed.append(ref)
            dbg_exc(
                f"远端媒体删除失败(保留待重试) provider={provider_id} "
                f"file_id={remote_file_id[:32]!r}"
            )
    return RemoteDeleteReport(tuple(cleaned), tuple(failed))


async def delete_remote_media_files(
    remote_refs: list[tuple[str, str, str]],
) -> int:
    """Compatibility wrapper returning the number of remotely-cleaned files."""

    return len((await delete_remote_media_files_detailed(remote_refs)).cleaned)


def _media_size_bytes(media: MediaInput) -> int:
    if media.data is not None:
        return len(media.data)
    if media.local_path is not None and media.local_path.is_file():
        try:
            return int(media.local_path.stat().st_size)
        except OSError:
            return 0
    return 0


def _remote_file_id_hint(remote_file_id: str | None) -> str | None:
    text = str(remote_file_id or "").strip()
    if not text:
        return None
    prefix = text.split("-", 1)[0][:12]
    tail = (
        text[-_FILE_ID_HINT_TAIL:]
        if len(text) >= _FILE_ID_HINT_TAIL
        else text
    )
    return f"{prefix}-****{tail}"


def _remaining_remote_ttl_seconds(media: MediaInput) -> int | None:
    if media.remote_expires_at is None:
        return None
    return max(int((media.remote_expires_at - now_beijing()).total_seconds()), 0)


def _inline_base64_block(media: MediaInput) -> dict[str, Any] | None:
    size = _media_size_bytes(media)
    if size <= 0 or size > INLINE_MEDIA_MAX_BYTES:
        return None
    data = media.data
    if data is None and media.local_path is not None and media.local_path.is_file():
        try:
            data = media.local_path.read_bytes()
        except OSError:
            return None
    if data is None or len(data) > INLINE_MEDIA_MAX_BYTES:
        return None
    return {
        "type": "image_url",
        "image_url": {"url": _data_url(data, media.mime_type)},
    }


async def build_media_resolutions(
    media_inputs: list[MediaInput],
    *,
    task: LLMTask,
    group_id: int,
    session: Any = None,
    cached_captions: dict[str, str] | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
) -> list[MediaResolution]:
    """Resolve media via file_id/upload/inline/URL/caption/unavailable."""

    request = resolve_llm_request(task)
    provider = _provider_for_task(task)
    captions = cached_captions or {}
    resolutions: list[MediaResolution] = []
    ttl_seconds = _remote_media_ttl_seconds()
    can_query_assets = session is not None and hasattr(session, "scalar")
    remote_capable = isinstance(provider, DeepSeekFileProvider) or not isinstance(
        provider, InlineMediaProvider
    )

    for index, media in enumerate(media_inputs):
        bound: MediaInput | None = None
        transport: str | None = None
        failure_reason: str | None = None
        failure_status_code: int | None = None
        failure_type: str | None = None
        upload_lock = _media_upload_lock(provider.provider_scope, media.content_hash)
        async with upload_lock:
            if can_query_assets:
                try:
                    bound = await media_store.reusable_remote_media(
                        session,
                        group_id=group_id,
                        media=media,
                        provider=provider.provider_id,
                        provider_scope=provider.provider_scope,
                    )
                except SQLAlchemyError:
                    dbg_exc(
                        f"群 {group_id} Provider 媒体复用查询失败(忽略) "
                        f"digest={media.content_hash[:16]}…"
                    )
            if bound is not None and bound.remote_file_id:
                transport = "reused_file"
            elif remote_capable:
                try:
                    bound = await provider.upload(
                        media, expires_after_seconds=ttl_seconds
                    )
                    if bound.remote_file_id:
                        transport = "uploaded_file"
                except Exception as exc:  # noqa: BLE001
                    failure_status_code = _exception_status_code(exc)
                    failure_type = type(exc).__name__
                    failure_reason = (
                        f"files_api_upload_failed:{failure_status_code}"
                        if failure_status_code is not None
                        else f"files_api_upload_failed:{failure_type}"
                    )
                    dbg_exc(
                        f"群 {group_id} Provider 媒体上传失败，进入降级链 "
                        f"task={task} digest={media.content_hash[:16]}…"
                    )
                    bound = None

            if bound is not None and bound.remote_file_id:
                if can_query_assets:
                    try:
                        await media_store.save_remote_media(
                            session,
                            group_id=group_id,
                            media=bound,
                            size_bytes=_media_size_bytes(media),
                            ttl_seconds=ttl_seconds,
                        )
                    except SQLAlchemyError:
                        dbg_exc(
                            f"群 {group_id} Provider MediaAsset 绑定失败(忽略) "
                            f"digest={media.content_hash[:16]}…"
                        )
                try:
                    block = provider.build_content_block(bound)
                except (OSError, ValueError):
                    failure_reason = "remote_file_block_failed"
                else:
                    resolution = MediaResolution(
                        status="remote_file_ready",
                        content_hash=media.content_hash,
                        source=media.source,
                        block=block,
                        asset_id=media.asset_id,
                        remote_file_id=bound.remote_file_id,
                        transport=transport or "remote_file",
                    )
                    resolutions.append(resolution)
                    if diagnostics is not None:
                        diagnostics.append(
                            {
                                "index": index,
                                "task": task,
                                "status": resolution.status,
                                "transport": resolution.transport,
                                "source": media.source,
                                "source_message_id": media.source_message_id,
                                "asset_id": media.asset_id,
                                "content_hash": media.content_hash[:12],
                                "_content_hash": media.content_hash,
                                "provider": request.provider,
                                "model": request.model,
                                "remote_file_status": (
                                    "hit"
                                    if resolution.transport == "reused_file"
                                    else "uploaded"
                                ),
                                "file_id_hint": _remote_file_id_hint(
                                    bound.remote_file_id
                                ),
                                "remote_ttl_seconds": _remaining_remote_ttl_seconds(
                                    bound
                                ),
                                "input_type": "file",
                                "delivered_to_model": True,
                            }
                        )
                    continue

        block: dict[str, Any] | None = None
        inline_transport: str | None = None
        if isinstance(provider, DeepSeekFileProvider):
            size = _media_size_bytes(media)
            if 0 < size <= INLINE_MEDIA_MAX_BYTES:
                try:
                    block = provider.build_file_data_block(media)
                    inline_transport = "file_data"
                except (OSError, ValueError):
                    block = None
        if block is None:
            block = _inline_base64_block(media)
            if block is not None:
                inline_transport = "base64_image_url"
        if block is not None:
            resolution = MediaResolution(
                status="inline_file_ready",
                content_hash=media.content_hash,
                source=media.source,
                block=block,
                reason=failure_reason,
                asset_id=media.asset_id,
                transport=inline_transport,
            )
            resolutions.append(resolution)
            if diagnostics is not None:
                diagnostics.append(
                    {
                        "index": index,
                        "task": task,
                        "status": resolution.status,
                        "transport": resolution.transport,
                        "source": media.source,
                        "source_message_id": media.source_message_id,
                        "asset_id": media.asset_id,
                        "content_hash": media.content_hash[:12],
                        "_content_hash": media.content_hash,
                        "provider": request.provider,
                        "model": request.model,
                        "remote_file_status": (
                            "upload_failed" if failure_reason else "not_used"
                        ),
                        "input_type": (
                            "file_data"
                            if resolution.transport == "file_data"
                            else "image_url"
                        ),
                        "delivered_to_model": True,
                        "fallback_reason": failure_reason,
                    }
                )
            continue

        if media.source_url and _url_allowed(media.source_url):
            resolution = MediaResolution(
                status="image_url_ready",
                content_hash=media.content_hash,
                source=media.source,
                block={"type": "image_url", "image_url": {"url": media.source_url}},
                reason=failure_reason,
                asset_id=media.asset_id,
                transport="trusted_url",
            )
            resolutions.append(resolution)
            if diagnostics is not None:
                diagnostics.append(
                    {
                        "index": index,
                        "task": task,
                        "status": resolution.status,
                        "transport": resolution.transport,
                        "source": media.source,
                        "source_message_id": media.source_message_id,
                        "asset_id": media.asset_id,
                        "content_hash": media.content_hash[:12],
                        "_content_hash": media.content_hash,
                        "provider": request.provider,
                        "model": request.model,
                        "remote_file_status": (
                            "upload_failed" if failure_reason else "not_used"
                        ),
                        "input_type": "image_url",
                        "delivered_to_model": True,
                        "fallback_reason": failure_reason,
                    }
                )
            continue

        caption = str(captions.get(media.content_hash) or "").strip()
        if caption:
            resolution = MediaResolution(
                status="caption_ready",
                content_hash=media.content_hash,
                source=media.source,
                caption=caption[:2000],
                reason=failure_reason or "binary_transport_unavailable",
                asset_id=media.asset_id,
                transport="cached_caption",
            )
        else:
            resolution = MediaResolution(
                status="unavailable",
                content_hash=media.content_hash,
                source=media.source,
                reason=failure_reason or "image_bytes_and_trusted_url_unavailable",
                asset_id=media.asset_id,
                transport="none",
            )
        resolutions.append(resolution)
        if diagnostics is not None:
            diagnostics.append(
                {
                    "index": index,
                    "task": task,
                    "status": resolution.status,
                    "transport": resolution.transport,
                    "source": media.source,
                    "source_message_id": media.source_message_id,
                    "asset_id": media.asset_id,
                    "content_hash": media.content_hash[:12],
                    "_content_hash": media.content_hash,
                    "provider": request.provider,
                    "model": request.model,
                    "remote_file_status": (
                        "upload_failed" if failure_reason else "not_used"
                    ),
                    "input_type": (
                        "caption" if resolution.status == "caption_ready" else "none"
                    ),
                    "delivered_to_model": resolution.status == "caption_ready",
                    "reason": resolution.reason,
                }
            )
    return resolutions


async def build_media_content_blocks(
    media_inputs: list[MediaInput],
    *,
    task: LLMTask,
    group_id: int,
    session: Any = None,
    cache_enabled: bool = False,
    diagnostics: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[MediaInput]]:
    """Backward-compatible wrapper over the structured media resolver."""

    del cache_enabled
    resolutions = await build_media_resolutions(
        media_inputs,
        task=task,
        group_id=group_id,
        session=session,
        diagnostics=diagnostics,
    )
    blocks = [item.block for item in resolutions if item.block is not None]
    ready_hashes = {item.content_hash for item in resolutions if item.block is not None}
    bound_inputs = [item for item in media_inputs if item.content_hash in ready_hashes]
    return blocks, bound_inputs


async def prepare_media_inputs(
    bot: Any,
    group_id: int,
    media_refs: list[dict[str, Any]],
    *,
    session: Any = None,
    cache_enabled: bool = False,
    asset_ttl_seconds: int | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
) -> tuple[list[MediaInput], list[tuple[str, str]], list[str]]:
    """Materialize OneBot media into provider-neutral MediaInput objects."""

    media_inputs: list[MediaInput] = []
    captions: list[tuple[str, str]] = []
    digests: list[str] = []
    vision_model = resolve_llm_request("agent_image").model
    asset_ttl = max(int(asset_ttl_seconds or _media_ttl_seconds()), 60)
    for index, ref in enumerate(media_refs):
        diagnostic: dict[str, Any] = {
            "index": index,
            "type": str(ref.get("type") or "unknown"),
            "source": str(ref.get("source") or "current"),
            "onebot_file": bool(ref.get("file")),
            "get_image_status": "not_needed",
            "image_read_status": "pending",
        }
        raw_source_message_id = ref.get("source_message_id") or ref.get("message_id")
        try:
            source_message_id = (
                int(raw_source_message_id)
                if raw_source_message_id is not None
                else None
            )
        except (TypeError, ValueError):
            source_message_id = None
        if source_message_id is not None:
            diagnostic["source_message_id"] = source_message_id
        url = str(ref.get("url") or "").strip()
        if url:
            parsed = urlparse(url)
            diagnostic["url_allowed"] = _url_allowed(url)
            diagnostic["url_host"] = parsed.hostname or None
        if str(ref.get("type")) != "image":
            diagnostic["status"] = "skipped_non_image"
            if diagnostics is not None:
                diagnostics.append(diagnostic)
            continue
        cached_asset = None
        if session is not None:
            raw_asset_id = ref.get("asset_id")
            try:
                asset_id = int(raw_asset_id) if raw_asset_id is not None else None
            except (TypeError, ValueError):
                asset_id = None
            if asset_id is not None:
                cached_asset = await media_store.find_local_asset_by_id(
                    session, group_id=group_id, asset_id=asset_id
                )
            if cached_asset is None and ref.get("content_hash"):
                cached_asset = await media_store.find_local_asset(
                    session,
                    group_id=group_id,
                    content_hash=str(ref["content_hash"]),
                )
        if cached_asset is not None and cached_asset.cache_path:
            cached_path = Path(str(cached_asset.cache_path))
            if cached_path.is_file():
                media = MediaInput(
                    kind="image",
                    content_hash=str(cached_asset.content_hash),
                    mime_type=str(cached_asset.mime_type),
                    local_path=cached_path,
                    data=None,
                    source_url=(
                        str(cached_asset.source_url)
                        if cached_asset.source_url
                        and _url_allowed(str(cached_asset.source_url))
                        else None
                    ),
                    source=str(ref.get("source") or "current"),
                    source_message_id=source_message_id,
                    asset_id=int(cached_asset.id),
                )
                media_inputs.append(media)
                digests.append(media.content_hash)
                ref["asset_id"] = int(cached_asset.id)
                ref["content_hash"] = media.content_hash
                ref["mime_type"] = media.mime_type
                diagnostic.update(
                    {
                        "status": "asset_reused",
                        "image_read_status": "success",
                        "read_source": "local_cache",
                        "local_cache": "hit",
                        "mime": media.mime_type,
                        "size_bytes": int(cached_asset.size_bytes or 0),
                        "content_hash": media.content_hash[:12],
                        "_content_hash": media.content_hash,
                        "asset_id": int(cached_asset.id),
                    }
                )
                if (
                    cache_enabled
                    and cached_asset.caption_model == vision_model
                    and cached_asset.caption
                ):
                    captions.append((media.content_hash, cached_asset.caption))
                    diagnostic["caption_cache"] = "hit"
                if diagnostics is not None:
                    diagnostics.append(diagnostic)
                continue
        loaded = await _load_bytes(bot, ref, diagnostic=diagnostic)
        if loaded is None:
            if _url_allowed(url):
                digest = hashlib.sha256(f"url:{url}".encode()).hexdigest()
                media = MediaInput(
                    kind="image",
                    content_hash=digest,
                    mime_type="application/octet-stream",
                    local_path=None,
                    source_url=url,
                    source=str(ref.get("source") or "current"),
                    source_message_id=source_message_id,
                    asset_id=(
                        int(ref["asset_id"])
                        if str(ref.get("asset_id") or "").isdigit()
                        else None
                    ),
                )
                if session is not None:
                    asset_row = await media_store.save_local_asset(
                        session,
                        group_id=group_id,
                        media=media,
                        size_bytes=0,
                        source_ref=ref,
                        ttl_seconds=asset_ttl,
                    )
                    if asset_row is not None:
                        media = replace(media, asset_id=int(asset_row.id))
                        ref["asset_id"] = int(asset_row.id)
                        ref["content_hash"] = digest
                        diagnostic["asset_id"] = int(asset_row.id)
                media_inputs.append(media)
                digests.append(digest)
                diagnostic.update(
                    {
                        "status": "url_passthrough",
                        "image_read_status": "failed",
                        "local_cache": "unavailable",
                        "content_hash": digest[:12],
                        "_content_hash": digest,
                    }
                )
            else:
                known_digest = str(
                    ref.get("content_hash")
                    or (cached_asset.content_hash if cached_asset is not None else "")
                    or ""
                )
                cached_caption: str | None = None
                if cache_enabled and known_digest and session is not None:
                    if (
                        cached_asset is not None
                        and cached_asset.caption_model == vision_model
                        and cached_asset.caption
                    ):
                        cached_caption = str(cached_asset.caption)
                    else:
                        cached_caption = await media_store.get_cached_caption(
                            session,
                            group_id=group_id,
                            content_hash=known_digest,
                            model_name=vision_model,
                        )
                        if cached_caption is None:
                            legacy = await _find_cache(
                                session,
                                group_id,
                                known_digest,
                                model_name=vision_model,
                            )
                            cached_caption = (
                                str(legacy.caption)
                                if legacy is not None and legacy.caption
                                else None
                            )
                if cached_caption:
                    captions.append((known_digest, cached_caption))
                    diagnostic.update(
                        {
                            "status": "caption_ready",
                            "content_hash": known_digest[:12],
                            "_content_hash": known_digest,
                            "_caption": cached_caption[:2000],
                            "reason": "image_bytes_unavailable_using_cached_caption",
                        }
                    )
                else:
                    diagnostic["status"] = "dropped_unavailable"
                    diagnostic["reason"] = (
                        "onebot_get_image_failed_or_source_unavailable"
                        if ref.get("file")
                        else "image_bytes_and_trusted_url_unavailable"
                    )
                    if known_digest:
                        diagnostic["content_hash"] = known_digest[:12]
                        diagnostic["_content_hash"] = known_digest
            if diagnostics is not None:
                diagnostics.append(diagnostic)
            continue
        data, hint = loaded
        digest = hashlib.sha256(data).hexdigest()
        mime = _mime_for_bytes(data, hint)
        digests.append(digest)
        local_path = (
            _materialize_cache_file(group_id, digest, data, mime)
            if session is not None
            else None
        )
        media = MediaInput(
            kind="image",
            content_hash=digest,
            mime_type=mime,
            local_path=local_path,
            data=data,
            source_url=url if _url_allowed(url) else None,
            source=str(ref.get("source") or "current"),
            source_message_id=source_message_id,
            asset_id=(
                int(ref["asset_id"])
                if str(ref.get("asset_id") or "").isdigit()
                else None
            ),
        )
        media_inputs.append(media)
        diagnostic.update(
            {
                "status": "loaded",
                "image_read_status": "success",
                "size_bytes": len(data),
                "mime": mime,
                "content_hash": digest[:12],
                "_content_hash": digest,
                "load_hint": hint,
                "cache_enabled": cache_enabled,
                "local_cache": "materialized" if local_path else "unavailable",
            }
        )
        if session is not None:
            asset_row = await media_store.save_local_asset(
                session,
                group_id=group_id,
                media=media,
                size_bytes=len(data),
                source_ref=ref,
                ttl_seconds=asset_ttl,
            )
            if asset_row is not None:
                media = replace(media, asset_id=int(asset_row.id))
                media_inputs[-1] = media
                ref["asset_id"] = int(asset_row.id)
                ref["content_hash"] = digest
                ref["mime_type"] = mime
                ref["size_bytes"] = len(data)
                diagnostic["asset_id"] = int(asset_row.id)
        if cache_enabled and session is not None:
            caption = await media_store.get_cached_caption(
                session,
                group_id=group_id,
                content_hash=digest,
                model_name=vision_model,
            )
            if caption is None:
                # Compatibility with assets cached before AgentMediaAsset existed.
                legacy = await _find_cache(
                    session, group_id, digest, model_name=vision_model
                )
                caption = legacy.caption if legacy is not None else None
            if caption:
                captions.append((digest, caption))
                diagnostic["caption_cache"] = "hit"
            else:
                diagnostic["caption_cache"] = "miss"
        if diagnostics is not None:
            diagnostics.append(diagnostic)
    dbg(
        f"群 {group_id} 媒体输入准备完成: media_inputs={len(media_inputs)} "
        f"缓存字幕={len(captions)} digests={len(digests)} "
        f"cache_enabled={cache_enabled}"
    )
    return media_inputs, captions, digests


async def prepare_image_inputs(
    bot: Any,
    group_id: int,
    media_refs: list[dict[str, Any]],
    *,
    session: Any = None,
    cache_enabled: bool = False,
    diagnostics: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[tuple[str, str]], list[str]]:
    """Backward-compatible wrapper returning content blocks for older callers/tests."""

    media_inputs, captions, digests = await prepare_media_inputs(
        bot,
        group_id,
        media_refs,
        session=session,
        cache_enabled=cache_enabled,
        diagnostics=diagnostics,
    )
    blocks, _bound = await build_media_content_blocks(
        media_inputs,
        task="agent_dialogue",
        group_id=group_id,
        session=session,
        cache_enabled=cache_enabled,
    )
    return blocks, captions, digests


async def get_cached_caption(
    session: Any, group_id: int, content_hash: str, model_name: str
) -> str | None:
    try:
        caption = await media_store.get_cached_caption(
            session,
            group_id=group_id,
            content_hash=content_hash,
            model_name=model_name,
        )
    except SQLAlchemyError:
        caption = None
    if caption:
        return caption
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
    try:
        if await media_store.store_caption(
            session,
            group_id=group_id,
            content_hash=content_hash,
            caption=caption,
            model_name=model_name,
            ttl_seconds=_media_ttl_seconds(),
        ):
            return
    except SQLAlchemyError:
        dbg_exc(
            f"群 {group_id} MediaAsset 字幕缓存不可用,回退旧缓存表 "
            f"digest={content_hash[:16]}…"
        )
    expires_at = now_beijing() + timedelta(seconds=_media_ttl_seconds())
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
    """Two-phase media cleanup with retryable remote deletion.

    Phase 1 commits ``cleanup_pending`` while retaining the remote file id. Phase 2
    finalizes only assets whose remote DELETE succeeded/404ed (or whose remote TTL
    already elapsed), and only then removes their local cache file.
    """

    now = now or now_beijing()
    finalizable_ids, remote_asset_ids, asset_paths = (
        await media_store.prepare_expired_assets_cleanup(
            session,
            now=now,
        )
    )
    legacy_rows = (
        (
            await session.execute(
                select(AgentMediaCache).where(AgentMediaCache.expires_at < now)
            )
        )
        .scalars()
        .all()
    )
    legacy_paths = list(
        dict.fromkeys(str(row.cache_path) for row in legacy_rows if row.cache_path)
    )
    legacy_result = await session.execute(
        delete(AgentMediaCache).where(AgentMediaCache.expires_at < now)
    )
    await session.flush()
    legacy_removed = int(legacy_result.rowcount or 0)
    try:
        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        dbg_exc("媒体缓存 cleanup_pending 提交失败,保留远端与磁盘文件等待重试")
        raise

    unlinked_count = 0
    # Legacy rows have no remote lifecycle. Their DB deletion is durable now, but
    # the same cache path may also be owned by a live MediaAsset.
    for cache_path in legacy_paths:
        if not await media_store.has_active_cache_path(
            session,
            cache_path=cache_path,
        ):
            unlink_cache_file(cache_path)
            unlinked_count += 1

    remote_report = await delete_remote_media_files_detailed(list(remote_asset_ids))
    finalized_ids = list(finalizable_ids)
    for ref in remote_report.cleaned:
        finalized_ids.extend(remote_asset_ids.get(ref, []))
    finalized_ids = list(dict.fromkeys(finalized_ids))

    finalized = 0
    if finalized_ids:
        try:
            finalized = await media_store.finalize_expired_assets(
                session,
                finalized_ids,
            )
            await session.commit()
        except SQLAlchemyError:
            await session.rollback()
            # Remote deletion may already have succeeded. Keeping cleanup_pending
            # makes the next run retry DELETE; a provider 404 then safely finalizes.
            dbg_exc("媒体缓存 expired 状态提交失败,保留磁盘文件等待下次重试")
            raise
        finalized_paths = list(
            dict.fromkeys(
                path
                for asset_id in finalized_ids
                if (path := asset_paths.get(asset_id))
            )
        )
        for cache_path in finalized_paths:
            if not await media_store.has_active_cache_path(
                session,
                cache_path=cache_path,
            ):
                unlink_cache_file(cache_path)
                unlinked_count += 1

    pending_remote_assets = sum(
        len(remote_asset_ids.get(ref, [])) for ref in remote_report.failed
    )
    removed = finalized + legacy_removed
    dbg(
        f"媒体缓存清理完成: finalized={finalized},legacy={legacy_removed},"
        f"remote_cleaned={len(remote_report.cleaned)},"
        f"remote_pending_assets={pending_remote_assets},"
        f"local_unlinked={unlinked_count}"
    )
    return removed


__all__ = [
    "INLINE_MEDIA_MAX_BYTES",
    "MAX_MEDIA_BYTES",
    "MediaInput",
    "MediaResolution",
    "RemoteDeleteReport",
    "build_media_content_blocks",
    "build_media_resolutions",
    "cleanup_media_cache",
    "delete_remote_media_files",
    "delete_remote_media_files_detailed",
    "get_cached_caption",
    "prepare_image_inputs",
    "prepare_media_inputs",
    "store_caption",
]
