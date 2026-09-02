# ruff: noqa: E501,PLR0913,SIM105,TC001,TID252
"""Persistent MediaAsset store for Agent media lifecycle and provider reuse."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from ..data_models.agent_media_asset import AgentMediaAsset
from .context import now_beijing
from .log import dbg_exc
from .media_provider import MediaInput

_LOCAL_PROVIDER = "local"
_LOCAL_SCOPE = "local"


def _expires_at(ttl_seconds: int) -> datetime:
    return now_beijing() + timedelta(seconds=max(int(ttl_seconds), 60))


async def _find_asset_record(
    session: Any,
    *,
    group_id: int,
    content_hash: str,
    provider: str,
    provider_scope: str,
) -> AgentMediaAsset | None:
    """Find the unique DB record even when it is expired and can be revived."""

    if session is None:
        return None
    return await session.scalar(
        select(AgentMediaAsset)
        .where(
            AgentMediaAsset.group_id == group_id,
            AgentMediaAsset.content_hash == content_hash,
            AgentMediaAsset.provider == provider,
            AgentMediaAsset.provider_scope == provider_scope,
        )
        .order_by(AgentMediaAsset.id.desc())
    )


async def find_asset(
    session: Any,
    *,
    group_id: int,
    content_hash: str,
    provider: str,
    provider_scope: str,
    now: datetime | None = None,
) -> AgentMediaAsset | None:
    if session is None:
        return None
    now = now or now_beijing()
    return await session.scalar(
        select(AgentMediaAsset)
        .where(
            AgentMediaAsset.group_id == group_id,
            AgentMediaAsset.content_hash == content_hash,
            AgentMediaAsset.provider == provider,
            AgentMediaAsset.provider_scope == provider_scope,
            AgentMediaAsset.expires_at >= now,
            AgentMediaAsset.status != "deleted",
        )
        .order_by(AgentMediaAsset.id.desc())
    )


async def find_local_asset(
    session: Any, *, group_id: int, content_hash: str
) -> AgentMediaAsset | None:
    return await find_asset(
        session,
        group_id=group_id,
        content_hash=content_hash,
        provider=_LOCAL_PROVIDER,
        provider_scope=_LOCAL_SCOPE,
    )


async def find_local_asset_by_id(
    session: Any, *, group_id: int, asset_id: int
) -> AgentMediaAsset | None:
    if session is None:
        return None
    return await session.scalar(
        select(AgentMediaAsset).where(
            AgentMediaAsset.id == asset_id,
            AgentMediaAsset.group_id == group_id,
            AgentMediaAsset.provider == _LOCAL_PROVIDER,
            AgentMediaAsset.provider_scope == _LOCAL_SCOPE,
            AgentMediaAsset.expires_at >= now_beijing(),
            AgentMediaAsset.status != "deleted",
        )
    )


async def find_reusable_remote_asset(
    session: Any,
    *,
    content_hash: str,
    provider: str,
    provider_scope: str,
) -> AgentMediaAsset | None:
    """Find one reusable provider object regardless of which group uploaded it."""

    if session is None:
        return None
    now = now_beijing()
    return await session.scalar(
        select(AgentMediaAsset)
        .where(
            AgentMediaAsset.content_hash == content_hash,
            AgentMediaAsset.provider == provider,
            AgentMediaAsset.provider_scope == provider_scope,
            AgentMediaAsset.remote_file_id.is_not(None),
            AgentMediaAsset.expires_at >= now,
            AgentMediaAsset.status != "deleted",
        )
        .order_by(AgentMediaAsset.last_used_at.desc(), AgentMediaAsset.id.desc())
    )


async def has_local_cache_path(session: Any, *, group_id: int, cache_path: str) -> bool:
    if session is None:
        return False
    row = await session.scalar(
        select(AgentMediaAsset.id).where(
            AgentMediaAsset.group_id == group_id,
            AgentMediaAsset.provider == _LOCAL_PROVIDER,
            AgentMediaAsset.provider_scope == _LOCAL_SCOPE,
            AgentMediaAsset.cache_path == cache_path,
            AgentMediaAsset.media_type == "image",
            AgentMediaAsset.expires_at >= now_beijing(),
            AgentMediaAsset.status != "deleted",
        )
    )
    return row is not None


async def has_active_cache_path(session: Any, *, cache_path: str) -> bool:
    """Whether any non-terminal MediaAsset still owns a local cache file."""

    if session is None or not cache_path or not hasattr(session, "scalar"):
        return False
    try:
        row = await session.scalar(
            select(AgentMediaAsset.id).where(
                AgentMediaAsset.cache_path == cache_path,
                AgentMediaAsset.status.not_in(("expired", "deleted")),
            )
        )
    except SQLAlchemyError:
        # Fail closed: if ownership cannot be proven absent, keep the file.
        dbg_exc("MediaAsset cache_path 引用检查失败,保留本地文件")
        return True
    return row is not None


async def save_local_asset(
    session: Any,
    *,
    group_id: int,
    media: MediaInput,
    size_bytes: int,
    source_ref: dict[str, Any],
    ttl_seconds: int,
) -> AgentMediaAsset | None:
    """Upsert the group-local materialized asset without storing a second media list."""

    if session is None:
        return None
    row = await find_local_asset(
        session, group_id=group_id, content_hash=media.content_hash
    )
    if row is None:
        row = await _find_asset_record(
            session,
            group_id=group_id,
            content_hash=media.content_hash,
            provider=_LOCAL_PROVIDER,
            provider_scope=_LOCAL_SCOPE,
        )
    raw_source_message_id = source_ref.get("source_message_id") or source_ref.get(
        "message_id"
    )
    source_message_id: int | None = None
    if raw_source_message_id is not None:
        try:
            source_message_id = int(raw_source_message_id)
        except (TypeError, ValueError):
            source_message_id = None
    try:
        async with session.begin_nested():
            if row is None:
                row = AgentMediaAsset(
                    group_id=group_id,
                    content_hash=media.content_hash,
                    media_type=media.kind,
                    mime_type=media.mime_type,
                    size_bytes=size_bytes,
                    source_type=str(source_ref.get("source") or "current")[:32],
                    source_message_id=source_message_id,
                    source_file=(str(source_ref.get("file"))[:512] or None),
                    source_url=(str(source_ref.get("url")) or None),
                    cache_path=str(media.local_path) if media.local_path else None,
                    provider=_LOCAL_PROVIDER,
                    provider_scope=_LOCAL_SCOPE,
                    expires_at=_expires_at(ttl_seconds),
                    status="ready",
                )
                session.add(row)
            else:
                row.mime_type = media.mime_type
                row.size_bytes = size_bytes
                row.cache_path = (
                    str(media.local_path) if media.local_path else row.cache_path
                )
                row.last_used_at = now_beijing()
                row.expires_at = _expires_at(ttl_seconds)
                row.status = "ready"
            await session.flush()
    except (SQLAlchemyError, TypeError, ValueError):
        dbg_exc(
            f"群 {group_id} MediaAsset 本地索引写入失败(已隔离) "
            f"digest={media.content_hash[:16]}…"
        )
        return None
    return row


async def reusable_remote_media(
    session: Any,
    *,
    group_id: int,
    media: MediaInput,
    provider: str,
    provider_scope: str,
) -> MediaInput | None:
    row = await find_reusable_remote_asset(
        session,
        content_hash=media.content_hash,
        provider=provider,
        provider_scope=provider_scope,
    )
    if row is None or not row.remote_file_id:
        return None
    now = now_beijing()
    if row.remote_expires_at is not None and row.remote_expires_at <= now + timedelta(
        seconds=60
    ):
        return None
    row.last_used_at = now
    try:
        await session.flush()
    except SQLAlchemyError:
        dbg_exc(
            f"群 {group_id} MediaAsset touch 失败(忽略) digest={media.content_hash[:16]}…"
        )
    return media.bind_provider(
        provider=provider,
        provider_scope=provider_scope,
        remote_file_id=row.remote_file_id,
        remote_created_at=row.remote_created_at,
        remote_expires_at=row.remote_expires_at,
    )


async def save_remote_media(
    session: Any,
    *,
    group_id: int,
    media: MediaInput,
    size_bytes: int,
    ttl_seconds: int,
) -> None:
    if session is None:
        return
    row = await find_asset(
        session,
        group_id=group_id,
        content_hash=media.content_hash,
        provider=media.provider,
        provider_scope=media.provider_scope,
    )
    if row is None:
        row = await _find_asset_record(
            session,
            group_id=group_id,
            content_hash=media.content_hash,
            provider=media.provider,
            provider_scope=media.provider_scope,
        )
    try:
        async with session.begin_nested():
            if row is None:
                row = AgentMediaAsset(
                    group_id=group_id,
                    content_hash=media.content_hash,
                    media_type=media.kind,
                    mime_type=media.mime_type,
                    size_bytes=size_bytes,
                    cache_path=str(media.local_path) if media.local_path else None,
                    provider=media.provider,
                    provider_scope=media.provider_scope,
                    remote_file_id=media.remote_file_id,
                    remote_created_at=media.remote_created_at,
                    remote_expires_at=media.remote_expires_at,
                    expires_at=_expires_at(ttl_seconds),
                    status="uploaded" if media.remote_file_id else "ready",
                )
                session.add(row)
            else:
                row.mime_type = media.mime_type
                row.size_bytes = size_bytes
                row.cache_path = (
                    str(media.local_path) if media.local_path else row.cache_path
                )
                row.remote_file_id = media.remote_file_id
                row.remote_created_at = media.remote_created_at
                row.remote_expires_at = media.remote_expires_at
                row.last_used_at = now_beijing()
                row.expires_at = _expires_at(ttl_seconds)
                row.status = "uploaded" if media.remote_file_id else "ready"
            await session.flush()
    except SQLAlchemyError:
        dbg_exc(
            f"群 {group_id} Provider MediaAsset 写入失败(已隔离) "
            f"provider={media.provider} digest={media.content_hash[:16]}…"
        )


async def get_cached_caption(
    session: Any,
    *,
    group_id: int,
    content_hash: str,
    model_name: str,
) -> str | None:
    row = await find_local_asset(session, group_id=group_id, content_hash=content_hash)
    if row is None or not row.caption or row.caption_model != model_name:
        return None
    row.last_used_at = now_beijing()
    try:
        await session.flush()
    except SQLAlchemyError:
        pass
    return row.caption


async def store_caption(
    session: Any,
    *,
    group_id: int,
    content_hash: str,
    caption: str,
    model_name: str,
    ttl_seconds: int,
) -> bool:
    if session is None or not caption.strip():
        return False
    row = await find_local_asset(session, group_id=group_id, content_hash=content_hash)
    if row is None:
        return False
    try:
        async with session.begin_nested():
            row.caption = caption.strip()[:2000]
            row.caption_model = model_name[:128]
            row.last_used_at = now_beijing()
            row.expires_at = _expires_at(ttl_seconds)
            row.status = "captioned"
            await session.flush()
    except SQLAlchemyError:
        dbg_exc(
            f"群 {group_id} MediaAsset caption 写入失败(已隔离) "
            f"digest={content_hash[:16]}…"
        )
        return False
    return True


async def prepare_expired_assets_cleanup(
    session: Any,
    *,
    now: datetime | None = None,
) -> tuple[
    list[int],
    dict[tuple[str, str, str], list[int]],
    dict[int, str],
]:
    """Mark expired assets cleanup_pending while preserving retry information.

    Returns ``(finalizable_ids, remote_ref_to_asset_ids, cache_path_by_asset_id)``.
    A row is immediately finalizable when it has no remote file, the provider TTL
    has already elapsed, or another live row still owns the same remote file.
    """

    if session is None:
        return [], {}, {}
    now = now or now_beijing()
    rows = (
        (
            await session.execute(
                select(AgentMediaAsset).where(
                    AgentMediaAsset.expires_at < now,
                    AgentMediaAsset.status.not_in(("expired", "deleted")),
                )
            )
        )
        .scalars()
        .all()
    )
    finalizable_ids: list[int] = []
    remote_asset_ids: dict[tuple[str, str, str], list[int]] = {}
    cache_paths: dict[int, str] = {}
    for row in rows:
        asset_id = int(row.id)
        row.status = "cleanup_pending"
        if row.cache_path:
            cache_paths[asset_id] = str(row.cache_path)
        remote_file_id = str(row.remote_file_id or "")
        if not remote_file_id:
            finalizable_ids.append(asset_id)
            continue
        if row.remote_expires_at is not None and row.remote_expires_at <= now:
            # Provider-side expiry is authoritative; a DELETE is unnecessary now.
            finalizable_ids.append(asset_id)
            continue
        still_referenced = await session.scalar(
            select(AgentMediaAsset.id).where(
                AgentMediaAsset.id != row.id,
                AgentMediaAsset.provider == row.provider,
                AgentMediaAsset.provider_scope == row.provider_scope,
                AgentMediaAsset.remote_file_id == remote_file_id,
                AgentMediaAsset.expires_at >= now,
                AgentMediaAsset.status.not_in(("expired", "deleted")),
            )
        )
        if still_referenced is not None:
            # This row can expire without deleting a remote file still used elsewhere.
            finalizable_ids.append(asset_id)
            continue
        ref = (str(row.provider), str(row.provider_scope), remote_file_id)
        remote_asset_ids.setdefault(ref, []).append(asset_id)
    await session.flush()
    return finalizable_ids, remote_asset_ids, cache_paths


async def finalize_expired_assets(session: Any, asset_ids: list[int]) -> int:
    """Persist the terminal expired state after remote cleanup has succeeded."""

    ids = list(dict.fromkeys(int(item) for item in asset_ids if int(item) > 0))
    if session is None or not ids:
        return 0
    result = await session.execute(
        update(AgentMediaAsset)
        .where(AgentMediaAsset.id.in_(ids))
        .values(
            status="expired",
            cache_path=None,
            source_file=None,
            source_url=None,
            remote_file_id=None,
            remote_created_at=None,
            remote_expires_at=None,
            caption=None,
            caption_model=None,
        )
    )
    await session.flush()
    return int(result.rowcount or 0)


async def cleanup_expired_assets(
    session: Any, *, now: datetime | None = None
) -> tuple[int, list[str], list[tuple[str, str, str]]]:
    """Compatibility view of the first cleanup phase; rows are not deleted."""

    finalizable, remote_map, cache_paths = await prepare_expired_assets_cleanup(
        session, now=now
    )
    pending_count = len(set(finalizable) | {i for ids in remote_map.values() for i in ids})
    return pending_count, list(dict.fromkeys(cache_paths.values())), list(remote_map)


async def unreferenced_remote_refs(
    session: Any,
    remote_refs: list[tuple[str, str, str]],
    *,
    now: datetime | None = None,
) -> list[tuple[str, str, str]]:
    """Return remote refs that no live AgentMediaAsset row still owns."""

    if session is None:
        return []
    now = now or now_beijing()
    output: list[tuple[str, str, str]] = []
    for provider, provider_scope, remote_file_id in dict.fromkeys(remote_refs):
        live = await session.scalar(
            select(AgentMediaAsset.id).where(
                AgentMediaAsset.provider == provider,
                AgentMediaAsset.provider_scope == provider_scope,
                AgentMediaAsset.remote_file_id == remote_file_id,
                AgentMediaAsset.expires_at >= now,
                AgentMediaAsset.status != "deleted",
            )
        )
        if live is None:
            output.append((provider, provider_scope, remote_file_id))
    return output


__all__ = [
    "cleanup_expired_assets",
    "finalize_expired_assets",
    "find_asset",
    "find_local_asset",
    "find_local_asset_by_id",
    "find_reusable_remote_asset",
    "get_cached_caption",
    "has_active_cache_path",
    "has_local_cache_path",
    "prepare_expired_assets_cleanup",
    "reusable_remote_media",
    "save_local_asset",
    "save_remote_media",
    "store_caption",
    "unreferenced_remote_refs",
]
