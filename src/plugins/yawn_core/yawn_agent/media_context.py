# ruff: noqa: C901,E501,PLR0912,PLR0913,PLR0915,TID252,TC001,TC003
"""Resolve media from current/reply/history/tool sources into one Agent media stream."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from ..data_models.group_agent_message import GroupAgentMessage
from ..llm import LLMTask
from .log import dbg, dbg_exc
from .media import (
    MediaInput,
    MediaResolution,
    build_media_resolutions,
    prepare_media_inputs,
)

_MAX_RESOLVED_MEDIA = 4
_MEDIA_REFERENCE_RE = re.compile(
    r"(?:图片|图里|图上|截图|照片|相片|这张|那张|上面那|前面那|刚才那|刚刚那|"
    r"我刚才发的|我刚发的|第[一二三四五六七八九十\d]+张|这里是不是|还有什么细节)"
)


@dataclass(frozen=True, slots=True)
class MediaContextProjection:
    """Provider-legal media projection for exactly one LLM request."""

    content_blocks: list[dict[str, Any]]
    resolutions: list[MediaResolution]

    @property
    def has_visual_blocks(self) -> bool:
        return any(item.block is not None for item in self.resolutions)


def query_requests_historical_media(query_text: str | None) -> bool:
    """Whether selected history images are worth restoring for this turn."""

    text = " ".join(str(query_text or "").split())
    return bool(text and _MEDIA_REFERENCE_RE.search(text))


def _media_refs_from_current(current_message: Any) -> list[dict[str, Any]]:
    if current_message is None:
        return []
    if isinstance(current_message, dict):
        raw = current_message.get("media_refs") or []
    else:
        raw = getattr(current_message, "media_refs", []) or []
    return [dict(item) for item in raw if isinstance(item, dict)]


def _message_ids(items: Sequence[dict[str, Any]] | None) -> list[int]:
    output: list[int] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        raw = item.get("message_id") or item.get("source_message_id")
        if raw is None:
            continue
        try:
            message_id = int(raw)
        except (TypeError, ValueError):
            continue
        if message_id and message_id not in output:
            output.append(message_id)
    return output


def _walk_tool_media(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        internal = value.get("_agent_media_refs")
        if isinstance(internal, list):
            for item in internal:
                if isinstance(item, dict):
                    yield dict(item)
        for key, child in value.items():
            if key == "_agent_media_refs":
                continue
            yield from _walk_tool_media(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_tool_media(child)


def strip_internal_media_metadata(value: Any) -> Any:
    """Remove resolver-only media refs before tool results are serialized to the LLM."""

    if isinstance(value, dict):
        return {
            key: strip_internal_media_metadata(child)
            for key, child in value.items()
            if key != "_agent_media_refs"
        }
    if isinstance(value, list):
        return [strip_internal_media_metadata(child) for child in value]
    return value


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def project_tool_result_media(
    value: Any,
    media_inputs: Sequence[MediaInput],
) -> Any:
    """Project resolver-only tool media into compact model-visible asset refs.

    The tool payload never exposes OneBot ``file`` handles, signed URLs, local paths,
    base64 data, or provider ``file_id`` values.  The media projection layer owns the
    actual binary/provider binding; the model only sees stable local ``asset_id`` refs.
    """

    by_asset = {
        int(item.asset_id): item
        for item in media_inputs
        if item.asset_id is not None
    }
    by_hash = {item.content_hash: item for item in media_inputs if item.content_hash}
    by_message: dict[int, list[MediaInput]] = {}
    for item in media_inputs:
        if item.source_message_id is None:
            continue
        by_message.setdefault(int(item.source_message_id), []).append(item)

    def media_rows(refs: Sequence[Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        message_cursor: dict[int, int] = {}
        for raw in refs[:8]:
            if not isinstance(raw, dict) or str(raw.get("type") or "") != "image":
                continue
            raw_asset_id = _optional_int(raw.get("asset_id"))
            raw_hash = str(raw.get("content_hash") or "")
            message_id = _optional_int(
                raw.get("source_message_id") or raw.get("message_id")
            )
            matched = by_asset.get(raw_asset_id) if raw_asset_id is not None else None
            if matched is None and raw_hash:
                matched = by_hash.get(raw_hash)
            if matched is None and message_id is not None:
                candidates = by_message.get(message_id, [])
                cursor = message_cursor.get(message_id, 0)
                if cursor < len(candidates):
                    matched = candidates[cursor]
                    message_cursor[message_id] = cursor + 1

            asset_id = (
                int(matched.asset_id)
                if matched is not None and matched.asset_id is not None
                else raw_asset_id
            )
            available = matched is not None
            row: dict[str, Any] = {"type": "image", "available": available}
            if asset_id is not None:
                row["asset_id"] = asset_id
            rows.append(row)
        return rows

    def walk(item: Any) -> Any:
        if isinstance(item, dict):
            internal = item.get("_agent_media_refs")
            projected = {
                key: walk(child)
                for key, child in item.items()
                if key not in {"_agent_media_refs", "media_types"}
            }
            if isinstance(internal, list):
                rows = media_rows(internal)
                if rows:
                    projected["media"] = rows
            return projected
        if isinstance(item, list):
            return [walk(child) for child in item]
        return item

    return walk(value)


async def _load_message_rows(
    session: Any,
    *,
    group_id: int,
    bot_id: int | None,
    message_ids: Sequence[int],
) -> dict[int, GroupAgentMessage]:
    if session is None or not message_ids:
        return {}
    stmt = select(GroupAgentMessage).where(
        GroupAgentMessage.group_id == group_id,
        GroupAgentMessage.message_id.in_(list(message_ids)),
    )
    if bot_id is not None:
        stmt = stmt.where(GroupAgentMessage.bot_id == bot_id)
    rows = (await session.execute(stmt)).scalars().all()
    return {int(row.message_id): row for row in rows}


def _ref_identity(ref: dict[str, Any]) -> tuple[str, str]:
    if ref.get("asset_id") is not None:
        return ("asset", str(ref["asset_id"]))
    if ref.get("content_hash"):
        return ("hash", str(ref["content_hash"]))
    if ref.get("file"):
        return ("file", str(ref["file"]))
    return ("url", str(ref.get("url") or ""))


def _append_unique_refs(
    target: list[dict[str, Any]], refs: Iterable[dict[str, Any]], *, limit: int
) -> None:
    seen = {_ref_identity(item) for item in target}
    for raw in refs:
        if len(target) >= limit:
            return
        ref = dict(raw)
        if str(ref.get("type") or "") != "image":
            continue
        identity = _ref_identity(ref)
        if not identity[1] or identity in seen:
            continue
        seen.add(identity)
        target.append(ref)


async def resolve_media_context(
    bot: Any,
    session: Any,
    group_id: int,
    *,
    current_message: Any = None,
    reply_chain: Sequence[dict[str, Any]] | None = None,
    selected_history: Sequence[dict[str, Any]] | None = None,
    tool_results: Sequence[Any] | None = None,
    query_text: str | None = None,
    max_assets: int = _MAX_RESOLVED_MEDIA,
    asset_ttl_seconds: int | None = None,
    cache_enabled: bool = False,
    diagnostics: list[dict[str, Any]] | None = None,
) -> list[MediaInput]:
    """Resolve all supported media entry points to content-addressed MediaInput values.

    Current/reply/forward media is always eligible. Historical media is intentionally
    lazy and only restored when the current query refers to images. Tool media is
    eligible because the model explicitly chose a history/message retrieval action.
    """

    limit = max(1, min(int(max_assets), 8))
    refs: list[dict[str, Any]] = []
    _append_unique_refs(refs, _media_refs_from_current(current_message), limit=limit)

    bot_id: int | None = None
    try:
        bot_id = int(getattr(bot, "self_id", 0) or 0) or None
    except (TypeError, ValueError):
        bot_id = None

    reply_ids = _message_ids(reply_chain)
    history_ids = (
        _message_ids(selected_history)
        if query_requests_historical_media(query_text)
        else []
    )
    row_ids = [*reply_ids, *(item for item in history_ids if item not in reply_ids)]
    try:
        rows = await _load_message_rows(
            session,
            group_id=group_id,
            bot_id=bot_id,
            message_ids=row_ids,
        )
    except SQLAlchemyError:
        dbg_exc(f"群 {group_id} 历史媒体消息索引查询失败(忽略)")
        rows = {}

    for message_id in row_ids:
        row = rows.get(message_id)
        if row is None:
            continue
        row_refs = [dict(item) for item in list(row.media_refs or []) if isinstance(item, dict)]
        for ref in row_refs:
            ref.setdefault("source_message_id", message_id)
            # A media ref stored when it was received commonly says source=current.
            # Once restored into a later turn it is history/reply media, and that
            # distinction controls the LLM projection label.
            ref["source"] = "reply" if message_id in reply_ids else "history"
        _append_unique_refs(refs, row_refs, limit=limit)

    for tool_result in tool_results or []:
        tool_refs = []
        for raw in _walk_tool_media(tool_result):
            ref = dict(raw)
            ref["source"] = "tool"
            tool_refs.append(ref)
        _append_unique_refs(refs, tool_refs, limit=limit)

    if not refs:
        return []

    media_inputs, _captions, _digests = await prepare_media_inputs(
        bot,
        group_id,
        refs,
        session=session,
        cache_enabled=cache_enabled,
        asset_ttl_seconds=asset_ttl_seconds,
        diagnostics=diagnostics,
    )

    # Lazy materialization may have enriched DB refs that predate MediaAsset support.
    # Reassign the JSON value so SQLAlchemy reliably detects the mutation.
    for message_id, row in rows.items():
        changed = False
        updated: list[dict[str, Any]] = []
        for raw in list(row.media_refs or []):
            item = dict(raw) if isinstance(raw, dict) else {}
            for resolved in refs:
                if int(resolved.get("source_message_id") or 0) != message_id:
                    continue
                same_file = item.get("file") and item.get("file") == resolved.get("file")
                same_hash = item.get("content_hash") and item.get("content_hash") == resolved.get("content_hash")
                if not (same_file or same_hash):
                    continue
                for key in ("asset_id", "content_hash", "mime_type", "size_bytes"):
                    if resolved.get(key) is not None and item.get(key) != resolved.get(key):
                        item[key] = resolved[key]
                        changed = True
                break
            updated.append(item)
        if changed:
            row.media_refs = updated
    if session is not None:
        try:
            await session.flush()
        except SQLAlchemyError:
            dbg_exc(f"群 {group_id} 历史媒体引用回填失败(忽略)")

    deduped: list[MediaInput] = []
    seen_hashes: set[str] = set()
    for media in media_inputs:
        if media.content_hash in seen_hashes:
            continue
        seen_hashes.add(media.content_hash)
        deduped.append(media)
        if len(deduped) >= limit:
            break
    dbg(
        f"群 {group_id} resolve_media_context: refs={len(refs)} "
        f"assets={len(deduped)} history={bool(history_ids)} tools={bool(tool_results)}"
    )
    return deduped


def public_media_diagnostics(
    items: Sequence[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Merge per-stage media diagnostics into Trace-safe per-asset rows."""

    sensitive = {
        "url",
        "file",
        "path",
        "cache_path",
        "source_url",
        "source_file",
        "caption",
        "file_id",
        "load_hint",
    }
    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    order: list[tuple[Any, ...]] = []
    for sequence, raw in enumerate(items or []):
        if not isinstance(raw, dict):
            continue
        digest = str(raw.get("_content_hash") or raw.get("content_hash") or "")
        asset_id = _optional_int(raw.get("asset_id"))
        message_id = _optional_int(raw.get("source_message_id"))
        if digest:
            key: tuple[Any, ...] = ("hash", digest)
        elif asset_id is not None:
            key = ("asset", asset_id)
        elif message_id is not None:
            key = (
                "message",
                message_id,
                str(raw.get("type") or "media"),
                int(raw.get("index") or 0),
            )
        else:
            key = (
                "sequence",
                str(raw.get("source") or "current"),
                int(raw.get("index") or sequence),
            )
        if key not in merged:
            merged[key] = {}
            order.append(key)
        target = merged[key]
        next_status = str(raw.get("status") or "")
        previous_status = str(target.get("status") or "")
        if next_status and previous_status and next_status != previous_status:
            target.setdefault("materialization_status", previous_status)
        for raw_key, value in raw.items():
            name = str(raw_key)
            if name.startswith("_") or name in sensitive or value is None:
                continue
            target[name] = value
    return [merged[key] for key in order]


def _unresolved_media_resolutions(
    diagnostics: Sequence[dict[str, Any]] | None,
    *,
    known_hashes: set[str],
) -> list[MediaResolution]:
    output: list[MediaResolution] = []
    for item in diagnostics or []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "")
        if status not in {"caption_ready", "dropped_unavailable"}:
            continue
        digest = str(item.get("_content_hash") or item.get("content_hash") or "")
        if digest and digest in known_hashes:
            continue
        source = str(item.get("source") or "current")
        if status == "caption_ready" and str(item.get("_caption") or "").strip():
            output.append(
                MediaResolution(
                    status="caption_ready",
                    content_hash=digest or "unresolved",
                    source=source,
                    caption=str(item["_caption"])[:2000],
                    reason=str(item.get("reason") or "binary_transport_unavailable"),
                    asset_id=(
                        int(item["asset_id"])
                        if str(item.get("asset_id") or "").isdigit()
                        else None
                    ),
                    transport="cached_caption",
                )
            )
        elif status == "dropped_unavailable":
            output.append(
                MediaResolution(
                    status="unavailable",
                    content_hash=digest or "unresolved",
                    source=source,
                    reason=str(item.get("reason") or "image_unavailable"),
                    asset_id=(
                        int(item["asset_id"])
                        if str(item.get("asset_id") or "").isdigit()
                        else None
                    ),
                    transport="none",
                )
            )
    return output


async def project_context_for_llm(
    media_inputs: list[MediaInput],
    *,
    task: LLMTask,
    group_id: int,
    session: Any = None,
    cached_captions: dict[str, str] | None = None,
    unresolved_diagnostics: Sequence[dict[str, Any]] | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
) -> MediaContextProjection:
    """Project database/history media into legal Chat Completions user content.

    This is the architectural boundary between persistent chat history and provider
    request messages.  Callers must attach ``content_blocks`` only to a ``user``
    message; no historical ``assistant``/``system`` message is recreated with media.
    """

    resolutions = await build_media_resolutions(
        media_inputs,
        task=task,
        group_id=group_id,
        session=session,
        cached_captions=cached_captions,
        diagnostics=diagnostics,
    )
    known_hashes = {item.content_hash for item in resolutions}
    resolutions.extend(
        _unresolved_media_resolutions(
            unresolved_diagnostics,
            known_hashes=known_hashes,
        )
    )

    current_blocks: list[dict[str, Any]] = []
    related_blocks: list[dict[str, Any]] = []
    status_lines: list[str] = []
    for item in resolutions:
        if item.block is not None:
            if item.source in {"current", "forward"}:
                current_blocks.append(item.block)
            else:
                related_blocks.append(item.block)
            continue
        digest = item.content_hash[:12] if item.content_hash else "unknown"
        if item.status == "caption_ready" and item.caption:
            status_lines.append(
                f"[media_context status=caption_ready source={item.source} digest={digest}] "
                "当前未直接读取图片文件；以下仅是缓存图片转述，不要声称本轮看到了原图："
                f"{item.caption}"
            )
        elif item.status == "unavailable":
            status_lines.append(
                f"[media_context status=unavailable source={item.source} digest={digest} "
                f"reason={item.reason or 'image_unavailable'}] "
                "找到了这张图片的引用，但图片文件现在无法读取。不要声称已经看过该图。"
            )

    blocks = list(current_blocks)
    if related_blocks:
        blocks.append(
            {
                "type": "text",
                "text": (
                    "以下是与当前问题相关的历史、回复或工具查询图片；"
                    "它们只作为本轮媒体上下文，不代表原历史 assistant/system 消息含图片。"
                ),
            }
        )
        blocks.extend(related_blocks)
    if status_lines:
        blocks.append({"type": "text", "text": "\n".join(status_lines)})
    return MediaContextProjection(content_blocks=blocks, resolutions=resolutions)


__all__ = [
    "MediaContextProjection",
    "project_context_for_llm",
    "project_tool_result_media",
    "public_media_diagnostics",
    "query_requests_historical_media",
    "resolve_media_context",
    "strip_internal_media_metadata",
]
