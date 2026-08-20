# ruff: noqa: TID252,RET504,C901,PLR0911,ASYNC240,PLR0913,SIM105
"""Short-lived media materialization and vision-caption cache for Agent."""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import delete, select

from ..data_models.agent_media_cache import AgentMediaCache
from ..llm import ai_config

MAX_MEDIA_BYTES = 8 * 1024 * 1024
_IMAGE_MIME_PREFIXES = ("image/jpeg", "image/png", "image/gif", "image/webp")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _allowed_hosts() -> frozenset[str]:
    raw = str(getattr(ai_config, "agent_media_allowed_hosts", "") or "")
    return frozenset(item.strip().lower() for item in raw.split(",") if item.strip())


def _safe_roots() -> tuple[Path, ...]:
    roots = [
        Path(str(getattr(ai_config, "agent_media_cache_dir", "data/agent_media")))
        .expanduser()
        .resolve()
    ]
    configured = os.environ.get("AGENT_FILE_ROOT", "data/agent_files")
    roots.append(Path(configured).expanduser().resolve())
    return tuple(dict.fromkeys(roots))


def _inside(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or root in path.parents for root in roots)


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


def _data_url(data: bytes, mime: str) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _identifier(ref: dict[str, Any]) -> str:
    for key in ("file_id", "file", "url", "path", "name"):
        value = str(ref.get(key) or "").strip()
        if value:
            return value
    return "unknown"


async def _fetch_url(url: str) -> bytes | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or host not in _allowed_hosts()
    ):
        return None
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.content
    except Exception:  # noqa: BLE001
        return None
    return data[: MAX_MEDIA_BYTES + 1]


async def _load_bytes(
    bot: Any, ref: dict[str, Any], *, depth: int = 0
) -> tuple[bytes, str] | None:
    if depth > 1:
        return None
    url = str(ref.get("url") or "").strip()
    if url.startswith(("http://", "https://")):
        data = await _fetch_url(url)
        if (
            data is not None
            and len(data) <= MAX_MEDIA_BYTES
            and _valid_image(data, url)
        ):
            return data, url
        return None
    candidate_text = str(ref.get("path") or ref.get("file") or "").strip()
    if candidate_text:
        candidate = Path(candidate_text).expanduser().resolve()
        if candidate.is_file() and _inside(candidate, _safe_roots()):
            try:
                if candidate.stat().st_size <= MAX_MEDIA_BYTES:
                    data = candidate.read_bytes()
                    if _valid_image(data, candidate.name):
                        return data, candidate.name
            except OSError:
                return None
    if bot is not None and ref.get("file"):
        try:
            payload = await bot.call_api("get_image", file=str(ref["file"]))
        except Exception:  # noqa: BLE001
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
            return await _load_bytes(bot, nested, depth=depth + 1)
    return None


async def _find_cache(
    session: Any, group_id: int, content_hash: str, *, model_name: str | None = None
) -> AgentMediaCache | None:
    if session is None:
        return None
    now = _now()
    stmt = select(AgentMediaCache).where(
        AgentMediaCache.group_id == group_id,
        AgentMediaCache.content_hash == content_hash,
        AgentMediaCache.media_type == "image",
        AgentMediaCache.expires_at >= now,
    )
    if model_name is not None:
        stmt = stmt.where(AgentMediaCache.model_name == model_name)
    row = await session.scalar(stmt.order_by(AgentMediaCache.id.desc()))
    if row is not None:
        row.last_access_at = now
        await session.flush()
    return row


async def prepare_image_inputs(
    bot: Any,
    group_id: int,
    media_refs: list[dict[str, Any]],
    *,
    session: Any = None,
    cache_enabled: bool = False,
) -> tuple[list[dict[str, Any]], list[tuple[str, str]], list[str]]:
    """Return OpenAI image blocks and cached captions keyed by content hash."""

    blocks: list[dict[str, Any]] = []
    captions: list[tuple[str, str]] = []
    digests: list[str] = []
    for ref in media_refs:
        if str(ref.get("type")) != "image":
            continue
        loaded = await _load_bytes(bot, ref)
        if loaded is None:
            # A provider may still be able to fetch the original URL.  It is
            # only passed through when it was explicitly supplied by OneBot.
            url = str(ref.get("url") or "").strip()
            if url.startswith(("http://", "https://")):
                blocks.append({"type": "image_url", "image_url": {"url": url}})
            continue
        data, hint = loaded
        digest = hashlib.sha256(data).hexdigest()
        digests.append(digest)
        if cache_enabled:
            cached_caption = await _find_cache(
                session, group_id, digest, model_name=None
            )
            if cached_caption is not None and cached_caption.caption:
                captions.append((digest, cached_caption.caption))
            cache_dir = (
                Path(
                    str(getattr(ai_config, "agent_media_cache_dir", "data/agent_media"))
                )
                .expanduser()
                .resolve()
            )
            group_dir = cache_dir / str(group_id)
            group_dir.mkdir(parents=True, exist_ok=True)
            suffix = mimetypes.guess_extension(_mime_for_bytes(data, hint)) or ".bin"
            path = group_dir / f"{digest}{suffix}"
            if not path.exists():
                path.write_bytes(data)
            existing = await _find_cache(session, group_id, digest, model_name="")
            if existing is None and session is not None:
                session.add(
                    AgentMediaCache(
                        group_id=group_id,
                        content_hash=digest,
                        media_type="image",
                        cache_path=str(path),
                        model_name="",
                        status="ready",
                        size_bytes=len(data),
                        expires_at=_now()
                        + timedelta(
                            seconds=max(
                                int(getattr(ai_config, "agent_media_cache_ttl", 86400)),
                                60,
                            )
                        ),
                    )
                )
                await session.flush()
        mime = _mime_for_bytes(data, hint)
        blocks.append(
            {"type": "image_url", "image_url": {"url": _data_url(data, mime)}}
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
        return
    row = await _find_cache(session, group_id, content_hash, model_name=model_name)
    if row is None:
        row = AgentMediaCache(
            group_id=group_id,
            content_hash=content_hash,
            media_type="image",
            model_name=model_name,
            status="captioned",
            caption=caption.strip()[:2000],
            expires_at=_now()
            + timedelta(
                seconds=max(int(getattr(ai_config, "agent_media_cache_ttl", 86400)), 60)
            ),
        )
        session.add(row)
    else:
        row.caption = caption.strip()[:2000]
        row.status = "captioned"
        row.expires_at = _now() + timedelta(
            seconds=max(int(getattr(ai_config, "agent_media_cache_ttl", 86400)), 60)
        )
    await session.flush()


async def cleanup_media_cache(session: Any, *, now: datetime | None = None) -> int:
    now = now or _now()
    rows = (
        (
            await session.execute(
                select(AgentMediaCache).where(AgentMediaCache.expires_at < now)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        if row.cache_path:
            try:
                Path(row.cache_path).unlink(missing_ok=True)
            except OSError:
                pass
    result = await session.execute(
        delete(AgentMediaCache).where(AgentMediaCache.expires_at < now)
    )
    await session.flush()
    return int(result.rowcount or 0)


__all__ = [
    "MAX_MEDIA_BYTES",
    "cleanup_media_cache",
    "get_cached_caption",
    "prepare_image_inputs",
    "store_caption",
]
