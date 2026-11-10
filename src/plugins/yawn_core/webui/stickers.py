# ruff: noqa: TID252,FAST002,E501,PERF203
"""「表情包」管理页的后端：表情包库（上传、改名打标签、审核、启停、删除）、
发送策略与频率、今日用量、本群提示词菜单预览。

库本体在 ``yawn_agent.sticker_library``（图片 + index.json），策略与用量在
``yawn_agent.sticker_policy``（JSON）。这里只做 snake_case → camelCase、群名标注
和输入校验；全部接口仅管理员可用。图片按 id 取，永远不回显本机路径。

AI 标注（``yawn_agent.sticker_labeler``）：上传前逐张预览、上传时自动补全、单张重新
识别（先预览再应用）、批量任务（``sticker_label_jobs``，进度可轮询、可取消）、撤销。
偷图来源（群、发图的人）只在这里给管理员看，成员要求忘掉时会被抹掉。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from nonebot_plugin_orm import get_session
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel
from sqlalchemy import select

from ..data_models.bot_group import BotGroup
from ..yawn_agent import sticker_label_jobs as label_jobs
from ..yawn_agent.sticker_intake import intake_image
from ..yawn_agent.sticker_labeler import REASON_TEXT as LABEL_REASON_TEXT
from ..yawn_agent.sticker_labeler import label_image, merge_label, vision_status
from ..yawn_agent.sticker_library import (
    SOURCE_LABELS,
    STATUS_ACTIVE,
    STATUS_PENDING,
    Sticker,
    StickerError,
    all_stickers,
    delete_sticker,
    digest_of,
    find_by_sha256,
    get_sticker,
    known_names,
    library_root,
    sticker_path,
    tag_counts,
    undo_label,
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
    note_labeled,
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
    data["stealProtectedUsers"] = [str(item) for item in settings.steal_protected_users]
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
        "ai": _ai_payload(item),
        "origin": _origin_payload(item),
        "canUndoLabel": item.can_undo_label,
        "placeholderName": item.placeholder_name,
    }


def _ai_payload(item: Sticker) -> dict[str, Any] | None:
    ai = item.ai
    if not ai:
        return None
    return {
        "model": ai.get("model") or None,
        "confidence": ai.get("confidence"),
        "kind": ai.get("kind") or None,
        "safe": ai.get("safe") is not False,
        "text": ai.get("text") or "",
        "labeledAt": ai.get("labeled_at") or None,
        "undone": bool(ai.get("undone")),
    }


def _origin_payload(item: Sticker) -> dict[str, Any] | None:
    origin = item.origin
    if not origin:
        return None
    return {
        "via": origin.get("via") or None,
        "groupId": str(origin["group_id"]) if origin.get("group_id") else None,
        "senderId": str(origin["sender_id"]) if origin.get("sender_id") else None,
        "senderName": origin.get("sender_name") or None,
        "messageId": str(origin["message_id"]) if origin.get("message_id") else None,
        "qqSticker": bool(origin.get("qq_sticker")),
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
    ids |= {int(row["origin"]["groupId"]) for row in stickers if row["origin"] and row["origin"]["groupId"]}
    names = await _group_names(ids)
    for row in usage["groups"]:
        row["groupName"] = names.get(str(row["groupId"]))
    counts = {
        "total": len(stickers),
        "active": sum(1 for row in stickers if row["status"] == STATUS_ACTIVE and row["enabled"]),
        "pending": sum(1 for row in stickers if row["status"] == STATUS_PENDING),
        "disabled": sum(1 for row in stickers if not row["enabled"]),
        "stolen": sum(1 for row in stickers if row["source"] == "stolen"),
        "aiLabeled": sum(1 for row in stickers if row["ai"] and not row["ai"]["undone"]),
        "unlabeled": sum(1 for row in stickers if not row["ai"] or row["ai"]["undone"]),
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
            "sourceLabels": SOURCE_LABELS,
            "vision": vision_status(),
            "labelJobs": label_jobs.list_jobs(),
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


async def _read_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > _UPLOAD_LIMIT:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "图片不能超过 10MB")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _UPLOAD_LIMIT:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "图片不能超过 10MB")
    return bytes(body)


def _label_payload(label: Any) -> dict[str, Any]:
    data = label.public()
    data["reasonText"] = LABEL_REASON_TEXT.get(label.reason) if label.reason else None
    return data


@router.post("/upload")
async def sticker_upload(  # noqa: PLR0913, PLR0917
    request: Request,
    _session: AdminWriteSession,
    name: str = Query(default="", max_length=40),
    tags: str = Query(default="", max_length=200),
    description: str = Query(default="", max_length=300),
    auto: bool = Query(default=True),  # noqa: FBT001
    fallback: str = Query(default="", max_length=60),
) -> dict[str, Any]:
    """原始请求体上传（不依赖 multipart），一次一张；同一张图重复上传返回已有记录。

    名字或标签没填、``auto`` 为真且策略开着“上传时 AI 补全”时，交给识图模型补；
    识图模型不可用时名字退回 ``fallback``（前端传文件名）。
    """

    body = await _read_body(request)
    settings = load_settings()
    # 直接调用（测试/内部）时没传的参数是 FieldInfo，按默认值处理
    name, tags, description, fallback = (value if isinstance(value, str) else "" for value in (name, tags, description, fallback))
    use_ai = (auto if isinstance(auto, bool) else True) and settings.ai_label_on_upload
    try:
        result = await intake_image(
            body,
            name=name,
            tags=tags,
            description=description,
            source="upload",
            status=STATUS_ACTIVE,
            fallback_name=fallback,
            hint=f"文件名：{fallback}" if fallback else "",
            use_ai=use_ai,
            settings=settings,
        )
    except StickerError as exc:
        code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE if exc.reason == "too_large" else status.HTTP_422_UNPROCESSABLE_ENTITY
        raise HTTPException(code, str(exc)) from exc
    await hub.notify_change("stickers")
    return ok(
        {
            "sticker": _sticker_payload(result.sticker, stats_snapshot()),
            "created": result.created,
            "namedBy": result.named_by,
            "heldForReview": result.held_for_review,
            "label": _label_payload(result.label) if result.label is not None else None,
        }
    )


@router.post("/label-preview")
async def sticker_label_preview(
    request: Request,
    _session: AdminWriteSession,
    hint: str = Query(default="", max_length=60),
) -> dict[str, Any]:
    """上传前预览：识图模型看这张图会起什么名字、打什么标签（不入库）。库里已有同一张图时一并告诉前端。"""

    body = await _read_body(request)
    hint = hint if isinstance(hint, str) else ""
    existing = find_by_sha256(digest_of(body)) if body else None
    settings = load_settings()
    if not settings.ai_label_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, LABEL_REASON_TEXT["disabled"])
    label = await label_image(body, hint=f"文件名：{hint}" if hint else "", known_names=known_names(), style=settings.ai_label_style)
    if label.ok:
        note_labeled(1)
    return ok(
        {
            "label": _label_payload(label),
            "duplicate": {"id": existing.sticker_id, "name": existing.name} if existing else None,
        }
    )


class LabelBody(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    apply: bool = False
    mode: Literal["fill", "merge", "replace"] = "replace"
    fields: list[Literal["name", "tags", "description"]] = Field(default_factory=lambda: ["name", "tags", "description"], max_length=3)


@router.post("/{sticker_id}/label")
async def sticker_label(sticker_id: str, body: LabelBody, _session: AdminWriteSession) -> dict[str, Any]:
    """单张重新识别：``apply=false`` 只给建议和会改哪些字段；``apply=true`` 写回（可撤销）。"""

    item = get_sticker(sticker_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "表情包不存在")
    settings = load_settings()
    if not settings.ai_label_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, LABEL_REASON_TEXT["disabled"])
    if body.apply:
        row = await label_jobs.label_one(
            sticker_id,
            mode=body.mode,
            fields=tuple(body.fields),
            hold_unsafe=settings.ai_label_review_unsafe,
            style=settings.ai_label_style,
        )
        if not row.get("ok"):
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(row.get("reasonText") or "标注失败"))
        note_labeled(1)
        await hub.notify_change("stickers")
        fresh = get_sticker(sticker_id) or item
        return ok({"result": row, "sticker": _sticker_payload(fresh, stats_snapshot())})
    try:
        data = sticker_path(item).read_bytes()
    except StickerError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    hint = "" if item.placeholder_name else f"现在叫“{item.name}”"
    label = await label_image(data, hint=hint, known_names=[name for name in known_names() if name != item.name], style=settings.ai_label_style)
    if label.ok:
        note_labeled(1)
    current = {"name": item.name, "tags": list(item.tags), "description": item.description, "name_is_placeholder": item.placeholder_name}
    return ok({"label": _label_payload(label), "changes": merge_label(current, label, mode=body.mode, fields=tuple(body.fields))})


@router.post("/{sticker_id}/undo-label")
async def sticker_undo_label(sticker_id: str, _session: AdminWriteSession) -> dict[str, Any]:
    try:
        item = undo_label(sticker_id)
    except StickerError as exc:
        code = status.HTTP_404_NOT_FOUND if exc.reason == "not_found" else status.HTTP_409_CONFLICT
        raise HTTPException(code, str(exc)) from exc
    await hub.notify_change("stickers")
    return ok({"sticker": _sticker_payload(item, stats_snapshot())})


class LabelJobBody(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    scope: Literal["selected", "unlabeled", "all"] = "selected"
    ids: list[str] = Field(default_factory=list, max_length=500)
    mode: Literal["fill", "merge", "replace"] = "fill"
    fields: list[Literal["name", "tags", "description"]] = Field(default_factory=lambda: ["name", "tags", "description"], max_length=3)


@router.get("/label-jobs")
async def sticker_label_jobs(_session: AdminReadSession) -> dict[str, Any]:
    return ok({"jobs": label_jobs.list_jobs(), "vision": vision_status()})


@router.post("/label-jobs")
async def sticker_label_job_start(body: LabelJobBody, _session: AdminWriteSession) -> dict[str, Any]:
    if body.scope == "unlabeled":
        ids = label_jobs.unlabeled_ids()
    elif body.scope == "all":
        ids = [item.sticker_id for item in all_stickers()]
    else:
        ids = body.ids
    try:
        job = label_jobs.start_job(ids, mode=body.mode, fields=tuple(body.fields))
    except label_jobs.LabelJobError as exc:
        code = status.HTTP_409_CONFLICT if exc.reason in {"busy", "disabled", "no_vision"} else status.HTTP_422_UNPROCESSABLE_ENTITY
        raise HTTPException(code, str(exc)) from exc
    return ok({"job": job.summary()})


@router.get("/label-jobs/{job_id}")
async def sticker_label_job(job_id: str, _session: AdminReadSession) -> dict[str, Any]:
    job = label_jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    return ok({"job": job.detail()})


@router.post("/label-jobs/{job_id}/cancel")
async def sticker_label_job_cancel(job_id: str, _session: AdminWriteSession) -> dict[str, Any]:
    try:
        job = label_jobs.cancel_job(job_id)
    except label_jobs.LabelJobError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return ok({"job": job.summary()})


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
    action: Literal["approve", "enable", "disable", "delete", "undo_label"]


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
            elif body.action == "undo_label":
                undo_label(sticker_id)
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
    # 私聊表情包：不带时沿用当前值。
    private_enabled: bool | None = None
    private_daily_limit: int | None = None
    private_cooldown_seconds: int | None = None
    # AI 标注与偷图：不带时沿用当前值（旧前端只提交上面那些字段）。
    ai_label_enabled: bool | None = None
    ai_label_on_upload: bool | None = None
    ai_label_on_collect: bool | None = None
    ai_label_style: str | None = Field(default=None, max_length=300)
    ai_label_review_unsafe: bool | None = None
    steal_mode: Literal["off", "review", "auto"] | None = None
    steal_photos: bool | None = None
    group_daily_steals: int | None = None
    steal_cooldown_seconds: int | None = None
    private_steal_enabled: bool | None = None
    private_daily_steals: int | None = None
    steal_protected_users: list[str] | None = Field(default=None, max_length=500)


_PRIVATE_FIELDS = (
    "private_enabled",
    "private_daily_limit",
    "private_cooldown_seconds",
    "ai_label_enabled",
    "ai_label_on_upload",
    "ai_label_on_collect",
    "ai_label_style",
    "ai_label_review_unsafe",
    "steal_mode",
    "steal_photos",
    "group_daily_steals",
    "steal_cooldown_seconds",
    "private_steal_enabled",
    "private_daily_steals",
)


def _user_list(values: list[str]) -> list[int]:
    result: list[int] = []
    for raw in values:
        text = str(raw).strip()
        if not text:
            continue
        if not text.isdigit() or not 5 <= len(text) <= 12:  # noqa: PLR2004
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"不偷名单里有不合法的 QQ 号：{text[:20]}")
        value = int(text)
        if value not in result:
            result.append(value)
    return result


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
    current = load_settings()
    values = body.model_dump()
    for key in _PRIVATE_FIELDS:
        if values.get(key) is None:
            values[key] = getattr(current, key)
    try:
        settings = StickerSettings.model_validate(
            {
                **values,
                "allowed_groups": _group_list(body.allowed_groups, "允许的群"),
                "blocked_groups": _group_list(body.blocked_groups, "禁用的群"),
                "steal_protected_users": _user_list(body.steal_protected_users)
                if body.steal_protected_users is not None
                else current.steal_protected_users,
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
