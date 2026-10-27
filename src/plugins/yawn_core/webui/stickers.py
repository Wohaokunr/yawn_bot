# ruff: noqa: TID252,FAST002,E501,PERF203
"""「表情包」管理页的后端：表情包库（上传、改名打标签、审核、启停、删除）、
发送策略与频率、今日用量、本群提示词菜单预览。

库本体在 ``yawn_agent.sticker_library``（图片 + index.json），策略与用量在
``yawn_agent.sticker_policy``（JSON）。这里只做 snake_case → camelCase、群名标注
和输入校验；全部接口仅管理员可用。图片按 id 取，永远不回显本机路径。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from nonebot_plugin_orm import get_session
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel
from sqlalchemy import select

from ..data_models.bot_group import BotGroup
from ..yawn_agent.sticker_library import (
    STATUS_ACTIVE,
    STATUS_PENDING,
    Sticker,
    StickerError,
    add_sticker,
    all_stickers,
    delete_sticker,
    get_sticker,
    library_root,
    sticker_path,
    tag_counts,
    update_sticker,
)
from ..yawn_agent.sticker_policy import (
    FREQUENCY_LABELS,
    REASON_TEXT,
    StickerSettings,
    cooldown_remaining,
    exposure,
    forget_stats,
    load_settings,
    save_settings,
    stats_snapshot,
    usage_snapshot,
)
from ..yawn_agent.sticker_send import sticker_turn_instruction
from .config import API_PATH
from .deps import AdminReadSession, AdminWriteSession, ok
from .hub import hub

router = APIRouter(prefix=f"{API_PATH}/stickers")
_UPLOAD_LIMIT = 10 * 1024 * 1024
_ID_PATTERN = r"^[A-Za-z0-9._-]{1,64}$"


def _camel(key: str) -> str:
    head, *rest = key.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def _settings_payload(settings: StickerSettings) -> dict[str, Any]:
    data = {_camel(key): value for key, value in settings.model_dump().items()}
    data["allowedGroups"] = [str(item) for item in settings.allowed_groups]
    data["blockedGroups"] = [str(item) for item in settings.blocked_groups]
    return data


async def _group_names(group_ids: set[int]) -> dict[str, str]:
    if not group_ids:
        return {}
    try:
        async with get_session() as session:
            rows = await session.execute(
                select(BotGroup.group_id, BotGroup.group_name).where(BotGroup.group_id.in_(group_ids))
            )
            return {str(group_id): name for group_id, name in rows if name}
    except Exception:  # noqa: BLE001
        return {}


def _sticker_payload(item: Sticker, stats: dict[str, Any]) -> dict[str, Any]:
    row = stats.get(item.sticker_id) if isinstance(stats.get(item.sticker_id), dict) else {}
    file_ok = (library_root() / item.file_name).is_file()
    return {
        "id": item.sticker_id,
        "name": item.name,
        "tags": list(item.tags),
        "description": item.description,
        "enabled": item.enabled,
        "status": item.status,
        "groups": [str(group) for group in item.groups],
        "source": item.source,
        "addedBy": str(item.added_by) if item.added_by else None,
        "addedGroup": str(item.added_group) if item.added_group else None,
        "size": item.size,
        "mime": item.mime,
        "createdAt": item.created_at or None,
        "updatedAt": item.updated_at or None,
        "uses": int(row.get("uses", 0)) if row else 0,
        "lastUsed": row.get("last_used") if row else None,
        "fileMissing": not file_ok,
    }


def _usage_payload(settings: StickerSettings) -> dict[str, Any]:
    usage = usage_snapshot()
    groups = sorted(
        ({"groupId": key, "sent": int(value)} for key, value in usage.get("groups", {}).items() if int(value) > 0),
        key=lambda row: -row["sent"],
    )
    return {
        "day": usage.get("day"),
        "totals": {key: int(value) for key, value in (usage.get("totals") or {}).items()},
        "groups": groups[:50],
        "groupLimit": settings.group_daily_limit,
    }


@router.get("")
async def stickers_overview(_session: AdminReadSession) -> dict[str, Any]:
    """整页数据：策略、今日用量、整个库（含待审核）与每张的使用次数。"""

    settings = load_settings()
    stats = stats_snapshot()
    stickers = [_sticker_payload(item, stats) for item in all_stickers()]
    usage = _usage_payload(settings)
    ids = {int(row["addedGroup"]) for row in stickers if row["addedGroup"]}
    ids |= {int(row["groupId"]) for row in usage["groups"] if str(row["groupId"]).isdigit()}
    ids |= {int(group) for row in stickers for group in row["groups"]}
    names = await _group_names(ids)
    for row in usage["groups"]:
        row["groupName"] = names.get(str(row["groupId"]))
    counts = {
        "total": len(stickers),
        "active": sum(1 for row in stickers if row["status"] == STATUS_ACTIVE and row["enabled"]),
        "pending": sum(1 for row in stickers if row["status"] == STATUS_PENDING),
        "disabled": sum(1 for row in stickers if not row["enabled"]),
    }
    return ok(
        {
            "settings": _settings_payload(settings),
            "defaults": _settings_payload(StickerSettings()),
            "frequencyLabels": FREQUENCY_LABELS,
            "usage": usage,
            "counts": counts,
            "stickers": stickers,
            "tags": [{"tag": tag, "count": count} for tag, count in tag_counts(None)],
            "groupNames": names,
        }
    )


@router.get("/{sticker_id}/image")
async def sticker_image(
    sticker_id: str, _session: AdminReadSession
) -> Response:
    item = get_sticker(sticker_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "表情包不存在")
    try:
        path = sticker_path(item)
    except StickerError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    mime = item.mime if item.mime.startswith("image/") else "image/png"
    return Response(
        content=path.read_bytes(),
        headers={
            "Content-Type": mime,
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self' data:",
            "Cache-Control": "private, max-age=300",
        },
    )


@router.post("/upload")
async def sticker_upload(
    request: Request,
    _session: AdminWriteSession,
    name: str = Query(min_length=1, max_length=40),
    tags: str = Query(default="", max_length=200),
    description: str = Query(default="", max_length=300),
) -> dict[str, Any]:
    """原始请求体上传（不依赖 multipart），一次一张；同一张图重复上传返回已有记录。"""

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > _UPLOAD_LIMIT:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "图片不能超过 10MB")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _UPLOAD_LIMIT:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "图片不能超过 10MB")
    settings = load_settings()
    try:
        item, created = add_sticker(
            bytes(body),
            name=name,
            tags=tags,
            description=description,
            source="upload",
            status=STATUS_ACTIVE,
            max_bytes=settings.max_bytes,
        )
    except StickerError as exc:
        code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE if exc.reason == "too_large" else status.HTTP_422_UNPROCESSABLE_ENTITY
        raise HTTPException(code, str(exc)) from exc
    await hub.notify_change("stickers")
    return ok({"sticker": _sticker_payload(item, stats_snapshot()), "created": created})


class StickerPatch(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    name: str | None = Field(default=None, max_length=40)
    tags: list[str] | None = Field(default=None, max_length=20)
    description: str | None = Field(default=None, max_length=300)
    enabled: bool | None = None
    status: Literal["active", "pending"] | None = None
    groups: list[str] | None = Field(default=None, max_length=200)


@router.patch("/{sticker_id}")
async def sticker_patch(
    sticker_id: str, body: StickerPatch, _session: AdminWriteSession
) -> dict[str, Any]:
    fields = body.model_dump(exclude_unset=True)
    if "groups" in fields and fields["groups"] is not None:
        fields["groups"] = _group_list(fields["groups"], "适用的群")
    try:
        item = update_sticker(sticker_id, **fields)
    except StickerError as exc:
        code = status.HTTP_404_NOT_FOUND if exc.reason == "not_found" else status.HTTP_422_UNPROCESSABLE_ENTITY
        raise HTTPException(code, str(exc)) from exc
    await hub.notify_change("stickers")
    return ok({"sticker": _sticker_payload(item, stats_snapshot())})


@router.delete("/{sticker_id}")
async def sticker_delete(sticker_id: str, _session: AdminWriteSession) -> dict[str, Any]:
    try:
        item = delete_sticker(sticker_id)
    except StickerError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    forget_stats(item.sticker_id)
    await hub.notify_change("stickers")
    return ok({"deleted": item.sticker_id})


class BulkBody(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    ids: list[str] = Field(min_length=1, max_length=500)
    action: Literal["approve", "enable", "disable", "delete"]


@router.post("/bulk")
async def sticker_bulk(body: BulkBody, _session: AdminWriteSession) -> dict[str, Any]:
    done: list[str] = []
    missing: list[str] = []
    for sticker_id in dict.fromkeys(body.ids):
        try:
            if body.action == "delete":
                delete_sticker(sticker_id)
                forget_stats(sticker_id)
            elif body.action == "approve":
                update_sticker(sticker_id, status=STATUS_ACTIVE, enabled=True)
            else:
                update_sticker(sticker_id, enabled=body.action == "enable")
            done.append(sticker_id)
        except StickerError:
            missing.append(sticker_id)
    await hub.notify_change("stickers")
    return ok({"done": done, "missing": missing})


class SettingsBody(BaseModel):
    """前端提交 camelCase；群号用字符串避免 JS 精度问题。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    enabled: bool
    group_mode: Literal["all", "allowlist"]
    allowed_groups: list[str] = Field(default_factory=list, max_length=500)
    blocked_groups: list[str] = Field(default_factory=list, max_length=500)
    frequency: Literal["rare", "normal", "often"]
    group_cooldown_seconds: int
    group_daily_limit: int
    max_per_message: int
    menu_size: int
    allow_proactive: bool
    collect_mode: Literal["off", "review", "auto"]
    collect_admin_only: bool
    member_daily_collect: int
    max_bytes: int


def _group_list(values: list[str], field: str) -> list[int]:
    result: list[int] = []
    for raw in values:
        text = str(raw).strip()
        if not text:
            continue
        if not text.isdigit() or not 4 <= len(text) <= 12:  # noqa: PLR2004
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{field} 里有不合法的群号：{text[:20]}")
        value = int(text)
        if value not in result:
            result.append(value)
    return result


@router.put("/settings")
async def sticker_update_settings(body: SettingsBody, _session: AdminWriteSession) -> dict[str, Any]:
    try:
        settings = StickerSettings.model_validate(
            {
                **body.model_dump(),
                "allowed_groups": _group_list(body.allowed_groups, "允许的群"),
                "blocked_groups": _group_list(body.blocked_groups, "禁用的群"),
            }
        )
    except ValidationError as exc:
        fields = {_camel(str(item["loc"][0])): item["msg"] for item in exc.errors() if item["loc"]}
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "；".join(f"{key}: {msg}" for key, msg in fields.items()) or "设置不合法",
        ) from exc
    saved = save_settings(settings)
    await hub.notify_change("stickers")
    return ok({"settings": _settings_payload(saved)})


@router.get("/prompt-preview")
async def sticker_prompt_preview(
    _session: AdminReadSession,
    group_id: str = Query(alias="groupId", pattern=r"^\d{4,12}$"),
) -> dict[str, Any]:
    """这个群现在一轮对话里，模型会看到的表情包说明（不含人设与场景的影响）。"""

    gid = int(group_id)
    gate = exposure(gid)
    text = sticker_turn_instruction(gid, scene="conversation")
    return ok(
        {
            "open": gate.open,
            "reason": gate.reason,
            "reasonText": REASON_TEXT.get(gate.reason or "", None),
            "available": len(gate.stickers),
            "leftToday": gate.left_today,
            "cooldownSeconds": round(cooldown_remaining(gid), 1),
            "text": text,
        }
    )


__all__ = ["router"]
