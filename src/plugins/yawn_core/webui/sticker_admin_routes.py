# ruff: noqa: TID252, FAST002, E501, FBT001
"""「表情包」管理页 3.0 的后端：统计、没对上的名字、体检、合集、回收站、拉黑名单、
标签整理、导入导出、换图。

和 ``stickers.py`` 共用 ``/stickers`` 前缀；这里没有单段的 ``/{sticker_id}`` 写接口，
所以两边的路径不会互相遮住。全部仅管理员可用，业务都在 ``yawn_agent.sticker_*``。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from ..yawn_agent import sticker_archive, sticker_health, sticker_insights
from ..yawn_agent.sticker_library import (
    StickerError,
    all_packs,
    all_stickers,
    blocked_hashes,
    bulk_update,
    create_pack,
    delete_pack,
    delete_tag,
    get_sticker,
    purge_trash,
    rename_tag,
    replace_image,
    restore_sticker,
    trashed_stickers,
    unblock_hash,
    update_pack,
    update_sticker,
)
from ..yawn_agent.sticker_policy import (
    dismiss_miss,
    forget_stats,
    load_settings,
    stats_snapshot,
)
from .config import API_PATH
from .deps import AdminReadSession, AdminWriteSession, ok
from .hub import hub
from .stickers import _group_list, _group_names, _pack_payload, _sticker_payload

router = APIRouter(prefix=f"{API_PATH}/stickers")
_IMAGE_LIMIT = 10 * 1024 * 1024
_STATUS = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "pack_not_found": status.HTTP_404_NOT_FOUND,
    "too_large": status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    "duplicate": status.HTTP_409_CONFLICT,
    "library_full": status.HTTP_409_CONFLICT,
}


def _fail(exc: StickerError) -> HTTPException:
    return HTTPException(_STATUS.get(exc.reason, status.HTTP_422_UNPROCESSABLE_ENTITY), str(exc))


class _Body(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


async def _read(request: Request, limit: int, message: str) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, message)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, message)
    return bytes(body)


def auto_purge() -> int:
    """回收站里放够了天数的顺手清掉；返回清了几张。"""

    days = load_settings().trash_days
    if days <= 0:
        return 0
    removed = purge_trash(older_than_days=days)
    for item in removed:
        forget_stats(item.sticker_id)
    return len(removed)


# ── 统计 ──


@router.get("/insights")
async def sticker_insights_view(_session: AdminReadSession, days: int = Query(default=30, ge=7, le=120)) -> dict[str, Any]:
    """用量走势、最常用/从没用过、各群用量、情绪覆盖、模型写了但库里没有的名字。"""

    data = sticker_insights.build_insights(days=days)
    ids = {int(row["groupId"]) for row in data.get("groups", []) if str(row.get("groupId", "")).lstrip("-").isdigit()}
    names = await _group_names({group for group in ids if group > 0})
    for row in data.get("groups", []):
        row["groupName"] = names.get(str(row.get("groupId")))
    return ok(data)


class MissBody(_Body):
    name: str = Field(default="", max_length=24)
    action: Literal["alias", "dismiss", "clear"]
    sticker_id: str = Field(default="", max_length=64)


@router.post("/misses/resolve")
async def sticker_miss_resolve(body: MissBody, _session: AdminWriteSession) -> dict[str, Any]:
    """处理一个没对上的名字：加成某张的别名、忽略这一条、或全部清空。"""

    if body.action == "clear":
        return ok({"removed": dismiss_miss(None)})
    if not body.name.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "要给出名字")
    if body.action == "alias":
        item = get_sticker(body.sticker_id)
        if item is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "表情包不存在")
        try:
            updated = update_sticker(item.sticker_id, aliases=[*item.aliases, body.name])
        except StickerError as exc:
            raise _fail(exc) from exc
        if not any(alias.casefold() == body.name.strip().casefold() for alias in updated.aliases):
            raise HTTPException(status.HTTP_409_CONFLICT, "这张的别名已经满了，先删掉一个")
    removed = dismiss_miss(body.name)
    await hub.notify_change("stickers")
    return ok({"removed": removed})


# ── 体检 ──


@router.get("/health")
async def sticker_health_view(_session: AdminReadSession) -> dict[str, Any]:
    return ok(sticker_health.run_check())


class FixBody(_Body):
    kind: str = Field(max_length=32)


@router.post("/health/fix")
async def sticker_health_fix(body: FixBody, _session: AdminWriteSession) -> dict[str, Any]:
    try:
        result = sticker_health.fix(body.kind)
    except StickerError as exc:
        raise _fail(exc) from exc
    await hub.notify_change("stickers")
    return ok({**result, "health": sticker_health.run_check()})


# ── 合集 ──


def _packs() -> list[dict[str, Any]]:
    stats = stats_snapshot()
    stickers = [_sticker_payload(item, stats) for item in all_stickers()]
    return [_pack_payload(pack, stickers) for pack in all_packs()]


class PackBody(_Body):
    name: str | None = Field(default=None, max_length=32)
    description: str | None = Field(default=None, max_length=60)
    enabled: bool | None = None
    groups: list[str] | None = Field(default=None, max_length=200)


@router.get("/packs")
async def sticker_packs(_session: AdminReadSession) -> dict[str, Any]:
    return ok({"packs": _packs()})


@router.post("/packs")
async def sticker_pack_create(body: PackBody, _session: AdminWriteSession) -> dict[str, Any]:
    try:
        pack = create_pack(
            body.name or "",
            description=body.description or "",
            groups=_group_list(body.groups or [], "groups"),
            enabled=body.enabled is not False,
        )
    except StickerError as exc:
        raise _fail(exc) from exc
    await hub.notify_change("stickers")
    return ok({"id": pack.pack_id, "packs": _packs()})


@router.patch("/packs/{pack_id}")
async def sticker_pack_update(pack_id: str, body: PackBody, _session: AdminWriteSession) -> dict[str, Any]:
    fields: dict[str, Any] = body.model_dump(exclude_unset=True)
    if fields.get("groups") is not None:
        fields["groups"] = _group_list(fields["groups"], "groups")
    try:
        update_pack(pack_id, **fields)
    except StickerError as exc:
        raise _fail(exc) from exc
    await hub.notify_change("stickers")
    return ok({"id": pack_id, "packs": _packs()})


@router.delete("/packs/{pack_id}")
async def sticker_pack_delete(pack_id: str, _session: AdminWriteSession) -> dict[str, Any]:
    """删合集：里面的表情包留在库里。"""

    try:
        delete_pack(pack_id)
    except StickerError as exc:
        raise _fail(exc) from exc
    await hub.notify_change("stickers")
    return ok({"deleted": pack_id, "packs": _packs()})


class PackMembersBody(_Body):
    ids: list[str] = Field(min_length=1, max_length=2000)
    op: Literal["add", "remove"] = "add"


@router.post("/packs/{pack_id}/members")
async def sticker_pack_members(pack_id: str, body: PackMembersBody, _session: AdminWriteSession) -> dict[str, Any]:
    if not any(pack.pack_id == pack_id for pack in all_packs()):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "合集不存在")
    try:
        done, missing = bulk_update(body.ids, "add_pack" if body.op == "add" else "remove_pack", pack_id)
    except StickerError as exc:
        raise _fail(exc) from exc
    await hub.notify_change("stickers")
    return ok({"done": done, "missing": missing, "packs": _packs()})


# ── 回收站与拉黑 ──


@router.get("/trash")
async def sticker_trash(_session: AdminReadSession) -> dict[str, Any]:
    purged = auto_purge()
    stats = stats_snapshot()
    items = sorted(trashed_stickers(), key=lambda item: item.deleted_at, reverse=True)
    return ok(
        {
            "stickers": [_sticker_payload(item, stats) for item in items],
            "trashDays": load_settings().trash_days,
            "autoPurged": purged,
            "blocked": list(blocked_hashes()),
        }
    )


@router.post("/trash/empty")
async def sticker_trash_empty(_session: AdminWriteSession) -> dict[str, Any]:
    removed = purge_trash()
    for item in removed:
        forget_stats(item.sticker_id)
    await hub.notify_change("stickers")
    return ok({"removed": len(removed)})


@router.post("/trash/{sticker_id}/restore")
async def sticker_trash_restore(sticker_id: str, _session: AdminWriteSession) -> dict[str, Any]:
    try:
        item = restore_sticker(sticker_id)
    except StickerError as exc:
        raise _fail(exc) from exc
    await hub.notify_change("stickers")
    return ok({"sticker": _sticker_payload(item, stats_snapshot())})


@router.get("/blocked")
async def sticker_blocked(_session: AdminReadSession) -> dict[str, Any]:
    return ok({"blocked": list(blocked_hashes())})


@router.delete("/blocked/{digest}")
async def sticker_unblock(digest: str, _session: AdminWriteSession) -> dict[str, Any]:
    if not unblock_hash(digest):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "拉黑名单里没有这张")
    await hub.notify_change("stickers")
    return ok({"blocked": list(blocked_hashes())})


# ── 标签整理 ──


class TagBody(_Body):
    source: str = Field(min_length=1, max_length=24, alias="from")
    to: str = Field(default="", max_length=24)


@router.post("/tags/rename")
async def sticker_tag_rename(body: TagBody, _session: AdminWriteSession) -> dict[str, Any]:
    """全库改标签名；新名字已经有了就是合并。"""

    try:
        changed = rename_tag(body.source, body.to)
    except StickerError as exc:
        raise _fail(exc) from exc
    await hub.notify_change("stickers")
    return ok({"changed": changed})


@router.post("/tags/delete")
async def sticker_tag_delete(body: TagBody, _session: AdminWriteSession) -> dict[str, Any]:
    try:
        changed = delete_tag(body.source)
    except StickerError as exc:
        raise _fail(exc) from exc
    await hub.notify_change("stickers")
    return ok({"changed": changed})


# ── 导入导出 ──


class ExportBody(_Body):
    ids: list[str] = Field(default_factory=list, max_length=2000)
    pack: str = Field(default="", max_length=16)
    include_private: bool = False


@router.post("/export")
async def sticker_export(body: ExportBody, _session: AdminWriteSession) -> Response:
    """打成一个 zip（图片 + manifest.json）。默认不带来源群、发图人这些私人信息。"""

    try:
        data, info = sticker_archive.export_archive(body.ids or None, pack_id=body.pack, include_private=body.include_private)
    except StickerError as exc:
        raise _fail(exc) from exc
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="yawnbot-stickers.zip"',
            "X-Sticker-Count": str(info["count"]),
            "X-Sticker-Skipped": str(info["skipped"]),
            "Cache-Control": "no-store",
        },
    )


@router.post("/import")
async def sticker_import(
    request: Request,
    _session: AdminWriteSession,
    apply: bool = Query(default=False),
    update_existing: bool = Query(default=False, alias="updateExisting"),
    as_pending: bool = Query(default=False, alias="asPending"),
    keep_private: bool = Query(default=False, alias="keepPrivate"),
) -> dict[str, Any]:
    """导入一个导出的包。不带 ``apply=true`` 时只看包里有什么，不写库。"""

    data = await _read(request, sticker_archive.MAX_ARCHIVE_BYTES, "包不能超过 200MB")
    try:
        if not apply:
            return ok({"applied": False, "info": sticker_archive.inspect_archive(data)})
        result = sticker_archive.import_archive(
            data,
            max_bytes=load_settings().max_bytes,
            update_existing=update_existing,
            as_pending=as_pending,
            keep_private=keep_private,
        )
    except StickerError as exc:
        raise _fail(exc) from exc
    await hub.notify_change("stickers")
    return ok({"applied": True, "result": result})


# ── 换图 ──


@router.post("/{sticker_id}/replace")
async def sticker_replace(sticker_id: str, request: Request, _session: AdminWriteSession) -> dict[str, Any]:
    """换一张图，名字、标签、统计都留着。"""

    data = await _read(request, _IMAGE_LIMIT, "图片不能超过 10MB")
    try:
        item = replace_image(sticker_id, data, max_bytes=load_settings().max_bytes)
    except StickerError as exc:
        raise _fail(exc) from exc
    await hub.notify_change("stickers")
    return ok({"sticker": _sticker_payload(item, stats_snapshot())})


__all__ = ["auto_purge", "router"]
