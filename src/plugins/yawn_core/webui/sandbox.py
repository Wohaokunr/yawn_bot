# ruff: noqa: TID252,FAST002,E501,FBT001
"""「代码沙箱」管理页的后端：沙箱状态、运行历史、工作区文件、控制台与策略。

沙箱 runner 跑在独立容器里，这里只经 ``yawn_agent.sandbox_client`` 转发并把
snake_case 结果转成前端约定的 camelCase。runner 离线时只读接口返回
``online: false`` 的降级数据，写接口返回 503，绝不影响 WebUI 其余页面。

全部接口仅管理员可用：工作区文件和运行代码里可能有群成员的数据，访客不可见。
文件下载一律带 ``nosniff`` 与 CSP sandbox，HTML/SVG 等可执行内容强制下载，
防止群成员生成的文件在管理台源下执行脚本。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from nonebot_plugin_orm import get_session
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel
from sqlalchemy import select

from ..data_models.bot_group import BotGroup
from ..yawn_agent import sandbox_inbox_store as inbox_store
from ..yawn_agent.sandbox_client import (
    ADMIN_WORKSPACE,
    SandboxClient,
    SandboxError,
    cached_health,
    sandbox_endpoint,
)
from ..yawn_agent.sandbox_inbox import FAILURE_HINTS, host_patterns
from ..yawn_agent.sandbox_policy import (
    INBOX_MEDIA_TYPES,
    SandboxSettings,
    load_settings,
    save_settings,
    usage_snapshot,
)
from .config import API_PATH
from .deps import AdminReadSession, AdminWriteSession, ok
from .hub import hub

router = APIRouter(prefix=f"{API_PATH}/sandbox")

_WORKSPACE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_GROUP_WORKSPACE_RE = re.compile(r"^g(\d{4,12})$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
# 这些字段的值是“数据字典”（键是状态名/工作区名），不做驼峰转换。
_OPAQUE_KEYS = frozenset(
    {"by_status", "by_workspace", "all", "by_reason", "by_type", "by_via", "by_group"}
)
# 只有这些类型允许在浏览器里内联预览；其余一律作为附件下载。
_INLINE_MIME = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "audio/wav",
        "audio/x-wav",
        "audio/mpeg",
        "audio/ogg",
        "audio/flac",
        "video/mp4",
        "video/webm",
        "application/pdf",
    }
)
_TEXT_PREVIEW_KINDS = frozenset({"text", "table", "vector"})
_BINARY_TABLE_SUFFIXES = (".xlsx", ".xls", ".parquet")
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024
_RUN_STATUSES = (
    "ok",
    "error",
    "timeout",
    "cpu_limit",
    "memory_limit",
    "disk_limit",
    "output_limit",
    "killed",
    "cancelled",
    "rejected",
)
_RUN_STATUS_PATTERN = "^(?:" + "|".join(_RUN_STATUSES) + ")$"


def _camel(key: str) -> str:
    head, *rest = key.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def camelize(value: Any) -> Any:
    """runner 的 snake_case JSON → 前端 camelCase；``_OPAQUE_KEYS`` 的值原样保留。"""

    if isinstance(value, dict):
        return {
            _camel(str(key)): (item if key in _OPAQUE_KEYS else camelize(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [camelize(item) for item in value]
    return value


def _workspace_id(value: str) -> str:
    if not _WORKSPACE_RE.fullmatch(value):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "工作区名不合法")
    return value


def _run_id(value: str) -> str:
    if not _RUN_ID_RE.fullmatch(value):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "运行 ID 不合法")
    return value


def _http_error(exc: SandboxError) -> HTTPException:
    if exc.code in {"disabled", "offline", "timeout", "misconfigured"}:
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    if exc.code == "not_found":
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if exc.status is not None and 400 <= exc.status < 500:  # noqa: PLR2004
        return HTTPException(exc.status, str(exc))
    return HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))


def workspace_group_id(workspace: str) -> str | None:
    match = _GROUP_WORKSPACE_RE.fullmatch(workspace or "")
    return match.group(1) if match else None


async def _group_names(group_ids: set[int]) -> dict[str, str]:
    if not group_ids:
        return {}
    try:
        async with get_session() as session:
            rows = await session.execute(
                select(BotGroup.group_id, BotGroup.group_name).where(
                    BotGroup.group_id.in_(group_ids)
                )
            )
            return {str(group_id): name for group_id, name in rows if name}
    except Exception:  # noqa: BLE001
        return {}


async def _label_workspaces(rows: list[dict[str, Any]], key: str = "workspace") -> None:
    """给带工作区字段的行补上 groupId / groupName（admin 工作区标成控制台）。"""

    ids = {int(gid) for row in rows if (gid := workspace_group_id(str(row.get(key) or "")))}
    names = await _group_names(ids)
    for row in rows:
        workspace = str(row.get(key) or "")
        group_id = workspace_group_id(workspace)
        row["groupId"] = group_id
        row["groupName"] = (
            names.get(group_id) if group_id else ("管理员控制台" if workspace == ADMIN_WORKSPACE else None)
        )


def _settings_payload(settings: SandboxSettings) -> dict[str, Any]:
    data = camelize(settings.model_dump())
    data["allowedGroups"] = [str(item) for item in settings.allowed_groups]
    data["blockedGroups"] = [str(item) for item in settings.blocked_groups]
    return data


def _usage_payload(settings: SandboxSettings) -> dict[str, Any]:
    usage = usage_snapshot()
    groups = sorted(
        ({"groupId": key, "runs": int(value)} for key, value in usage.get("groups", {}).items()),
        key=lambda row: -row["runs"],
    )
    members = sorted(
        (
            {"groupId": key.split(":", 1)[0], "userId": key.split(":", 1)[-1], "runs": int(value)}
            for key, value in usage.get("members", {}).items()
        ),
        key=lambda row: -row["runs"],
    )
    return {
        "day": usage.get("day"),
        "totalRuns": sum(row["runs"] for row in groups),
        "groups": groups[:50],
        "members": members[:50],
        "groupLimit": settings.group_daily_runs,
        "memberLimit": settings.member_daily_runs,
        "private": sorted(
            (
                {
                    "userId": str(key),
                    "runs": int(value),
                    "inboxBytes": int((usage.get("private_inbox") or {}).get(key, 0)),
                }
                for key, value in (usage.get("private") or {}).items()
                if int(value) > 0
            ),
            key=lambda row: -row["runs"],
        )[:50],
        "privateLimit": settings.private_daily_runs,
    }


# ── 状态 ────────────────────────────────────────────────────────────────────


@router.get("/status")
async def sandbox_status(
    _session: AdminReadSession, refresh: bool = Query(default=False)
) -> dict[str, Any]:
    """状态条：是否配置、是否在线、隔离自检、并发与当前策略。离线时也返回 200。"""

    endpoint = sandbox_endpoint()
    settings = load_settings()
    health, error = await cached_health(refresh=refresh)
    isolation = (health or {}).get("isolation") or {}
    if not endpoint.enabled:
        state = "disabled"
    elif health is None:
        state = "offline"
    elif not isolation.get("isolated"):
        state = "unsafe"
    elif not settings.enabled:
        state = "paused"
    else:
        state = "ready"
    return ok(
        {
            "state": state,
            "online": health is not None,
            "configured": endpoint.enabled,
            "transport": endpoint.transport_label,
            "error": (
                {"code": error.code, "message": str(error)} if error is not None else None
            ),
            "health": camelize(health) if health is not None else None,
            "settings": _settings_payload(settings),
            "usage": _usage_payload(settings),
            "inbox": _inbox_overview(settings),
        }
    )


def _inbox_overview(settings: SandboxSettings) -> dict[str, Any]:
    """状态条/概览用的入箱摘要；读坏文件也只会得到空统计。"""

    try:
        summary = inbox_store.summary()
    except Exception:  # noqa: BLE001
        summary = {"total": 0, "imported": 0, "reused": 0, "failed": 0, "bytes": 0}
    return {
        "enabled": settings.inbox_enabled,
        "autoImport": settings.inbox_auto_import,
        "today": camelize(summary),
    }


@router.get("/stats")
async def sandbox_stats(_session: AdminReadSession) -> dict[str, Any]:
    try:
        stats = await SandboxClient().stats()
    except SandboxError as exc:
        raise _http_error(exc) from exc
    data = camelize(stats)
    active = data.get("active") or []
    await _label_workspaces(active)
    by_workspace = (stats.get("runs") or {}).get("by_workspace") or {}
    rows = [{"workspace": key, "runs": value} for key, value in by_workspace.items()]
    await _label_workspaces(rows)
    data["topWorkspaces"] = rows
    return ok(data)


@router.get("/packages")
async def sandbox_packages(_session: AdminReadSession) -> dict[str, Any]:
    try:
        return ok(camelize(await SandboxClient().packages()))
    except SandboxError as exc:
        raise _http_error(exc) from exc


# ── 运行历史 ────────────────────────────────────────────────────────────────


@router.get("/runs")
async def sandbox_runs(
    _session: AdminReadSession,
    workspace: str | None = Query(default=None, max_length=48),
    run_status: str | None = Query(default=None, alias="status", pattern=_RUN_STATUS_PATTERN),
    q: str | None = Query(default=None, max_length=120),
    before: float | None = Query(default=None, gt=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    if workspace:
        _workspace_id(workspace)
    try:
        listing = await SandboxClient().runs(
            workspace=workspace or None,
            status=run_status,
            q=(q or "").strip() or None,
            before=before,
            limit=limit,
        )
    except SandboxError as exc:
        raise _http_error(exc) from exc
    data = camelize(listing)
    await _label_workspaces(data.get("items") or [])
    await _label_workspaces(data.get("active") or [])
    return ok(data)


@router.get("/runs/{run_id}")
async def sandbox_run_detail(run_id: str, _session: AdminReadSession) -> dict[str, Any]:
    try:
        detail = await SandboxClient().run_detail(_run_id(run_id))
    except SandboxError as exc:
        raise _http_error(exc) from exc
    data = camelize(detail)
    await _label_workspaces([data])
    return ok(data)


@router.post("/runs/{run_id}/cancel")
async def sandbox_cancel_run(run_id: str, _session: AdminWriteSession) -> dict[str, Any]:
    try:
        result = await SandboxClient().cancel(_run_id(run_id))
    except SandboxError as exc:
        raise _http_error(exc) from exc
    await hub.notify_change("sandbox")
    return ok(camelize(result))


# ── 工作区与文件 ────────────────────────────────────────────────────────────


@router.get("/workspaces")
async def sandbox_workspaces(_session: AdminReadSession) -> dict[str, Any]:
    try:
        listing = await SandboxClient().workspaces()
    except SandboxError as exc:
        raise _http_error(exc) from exc
    rows = camelize(listing.get("items") or [])
    await _label_workspaces(rows, key="id")
    rows.sort(key=lambda row: -(row.get("lastModified") or 0))
    return ok({"items": rows})


@router.get("/workspaces/{workspace}/files")
async def sandbox_files(
    workspace: str,
    _session: AdminReadSession,
    prefix: str = Query(default="", max_length=512),
    limit: int = Query(default=500, ge=1, le=1000),
    dirs: bool = Query(default=False),
) -> dict[str, Any]:
    try:
        listing = await SandboxClient().list_files(
            _workspace_id(workspace), prefix=prefix, limit=limit, dirs=dirs
        )
    except SandboxError as exc:
        raise _http_error(exc) from exc
    return ok(camelize(listing))


def _download_headers(path: str, mime: str, *, inline: bool) -> dict[str, str]:
    name = path.rsplit("/", 1)[-1] or "file"
    disposition = "inline" if inline else "attachment"
    return {
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(name)}",
        "X-Content-Type-Options": "nosniff",
        # 即使被直接打开，也不允许执行脚本、发请求或嵌入。
        "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self' data:; media-src 'self'",
        "Cache-Control": "private, no-store",
        "Content-Type": mime,
    }


@router.get("/workspaces/{workspace}/file")
async def sandbox_file(
    workspace: str,
    _session: AdminReadSession,
    path: str = Query(min_length=1, max_length=512),
    download: bool = Query(default=False),
    preview: bool = Query(default=False),
) -> Response:
    """下载/预览一个文件；``preview=1`` 对文本类文件返回 JSON 片段供页面直接展示。"""

    try:
        data, mime, kind = await SandboxClient().read_file(_workspace_id(workspace), path)
    except SandboxError as exc:
        raise _http_error(exc) from exc
    base_mime = mime.split(";", 1)[0].strip().lower()
    if preview:
        if kind not in _TEXT_PREVIEW_KINDS or path.lower().endswith(_BINARY_TABLE_SUFFIXES):
            raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "这个文件不能文本预览")
        limit = 200_000
        text = data[:limit].decode("utf-8", errors="replace")
        return Response(
            content=_json_bytes(ok({"text": text, "truncated": len(data) > limit, "kind": kind})),
            media_type="application/json",
            headers={"Cache-Control": "private, no-store"},
        )
    inline = not download and base_mime in _INLINE_MIME
    served_mime = mime if inline else "application/octet-stream"
    return Response(content=data, headers=_download_headers(path, served_mime, inline=inline))


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


@router.post("/workspaces/{workspace}/upload")
async def sandbox_upload(
    workspace: str,
    request: Request,
    _session: AdminWriteSession,
    path: str = Query(min_length=1, max_length=512),
    overwrite: bool = Query(default=True),
) -> dict[str, Any]:
    """原始请求体上传（不依赖 multipart）：把数据文件放进工作区供代码读取。

    ``overwrite=false`` 时同名文件返回 409，前端据此询问是否覆盖。
    """

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "上传文件不能超过 20MB")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_UPLOAD_BYTES:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "上传文件不能超过 20MB")
    try:
        result = await SandboxClient().write_file(
            _workspace_id(workspace), path, bytes(body), overwrite=overwrite
        )
    except SandboxError as exc:
        raise _http_error(exc) from exc
    await hub.notify_change("sandbox")
    return ok(camelize(result))


@router.delete("/workspaces/{workspace}/file")
async def sandbox_delete_file(
    workspace: str,
    _session: AdminWriteSession,
    path: str = Query(min_length=1, max_length=512),
) -> dict[str, Any]:
    try:
        result = await SandboxClient().delete_file(_workspace_id(workspace), path)
    except SandboxError as exc:
        raise _http_error(exc) from exc
    _forget_paths(workspace, {path})
    await hub.notify_change("sandbox")
    return ok(camelize(result))


def _forget_paths(workspace: str, paths: set[str]) -> None:
    group_id = workspace_group_id(workspace)
    if group_id is None:
        return
    # 记录只是提示用，失败不影响删除结果。
    with contextlib.suppress(Exception):
        inbox_store.forget_paths(int(group_id), paths)


class DeleteManyBody(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=200)


@router.post("/workspaces/{workspace}/delete")
async def sandbox_delete_many(
    workspace: str, body: DeleteManyBody, _session: AdminWriteSession
) -> dict[str, Any]:
    """批量删除：逐个删，单个失败不影响其余，返回每项结果。"""

    workspace = _workspace_id(workspace)
    client = SandboxClient()
    removed = 0
    failed: list[dict[str, str]] = []
    done: set[str] = set()
    for raw in dict.fromkeys(body.paths):
        path = str(raw).strip()
        if not path or len(path) > 512:  # noqa: PLR2004
            failed.append({"path": path[:80], "error": "路径不合法"})
            continue
        try:
            result = await client.delete_file(workspace, path)
        except SandboxError as exc:
            if exc.code in {"disabled", "offline", "timeout", "misconfigured"}:
                raise _http_error(exc) from exc
            failed.append({"path": path, "error": str(exc)})
            continue
        removed += int(result.get("removed") or 0)
        done.add(path)
    _forget_paths(workspace, done)
    await hub.notify_change("sandbox")
    return ok({"removed": removed, "deleted": sorted(done), "failed": failed})


class RenameBody(BaseModel):
    source: str = Field(min_length=1, max_length=512, alias="from")
    target: str = Field(min_length=1, max_length=512, alias="to")
    overwrite: bool = False

    model_config = ConfigDict(populate_by_name=True)


@router.post("/workspaces/{workspace}/rename")
async def sandbox_rename(
    workspace: str, body: RenameBody, _session: AdminWriteSession
) -> dict[str, Any]:
    try:
        result = await SandboxClient().rename_file(
            _workspace_id(workspace), body.source, body.target, overwrite=body.overwrite
        )
    except SandboxError as exc:
        raise _http_error(exc) from exc
    _forget_paths(workspace, {body.source})
    await hub.notify_change("sandbox")
    return ok(camelize(result))


class MkdirBody(BaseModel):
    path: str = Field(min_length=1, max_length=512)


@router.post("/workspaces/{workspace}/mkdir")
async def sandbox_mkdir(
    workspace: str, body: MkdirBody, _session: AdminWriteSession
) -> dict[str, Any]:
    try:
        result = await SandboxClient().make_dir(_workspace_id(workspace), body.path)
    except SandboxError as exc:
        raise _http_error(exc) from exc
    await hub.notify_change("sandbox")
    return ok(camelize(result))


@router.delete("/workspaces/{workspace}")
async def sandbox_reset_workspace(
    workspace: str,
    _session: AdminWriteSession,
    forget_history: bool = Query(default=False, alias="forgetHistory"),
) -> dict[str, Any]:
    try:
        result = await SandboxClient().reset_workspace(
            _workspace_id(workspace), forget_history=forget_history
        )
    except SandboxError as exc:
        raise _http_error(exc) from exc
    group_id = workspace_group_id(workspace)
    if group_id is not None:
        inbox_store.forget_group_files(int(group_id))
    await hub.notify_change("sandbox")
    return ok(camelize(result))


# ── 控制台 ──────────────────────────────────────────────────────────────────


class ConsoleRunBody(BaseModel):
    code: str = Field(min_length=1, max_length=50_000)
    workspace: str = Field(default=ADMIN_WORKSPACE, pattern=r"^[a-z0-9][a-z0-9_-]{0,47}$")
    timeout_seconds: int | None = Field(default=None, ge=1, le=90, alias="timeoutSeconds")

    model_config = ConfigDict(populate_by_name=True)


@router.post("/console/run")
async def sandbox_console_run(body: ConsoleRunBody, _session: AdminWriteSession) -> dict[str, Any]:
    """管理员控制台：不计群配额，但仍受沙箱时限/内存/磁盘上限约束。"""

    settings = load_settings()
    health, error = await cached_health()
    if error is not None or health is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"代码沙箱不可用：{error}")
    limits = {
        "timeout_seconds": body.timeout_seconds or settings.timeout_seconds,
        "memory_mb": settings.memory_mb,
        "disk_quota_mb": settings.disk_quota_mb,
    }
    try:
        result = await SandboxClient().run(
            body.workspace,
            body.code,
            limits=limits,
            meta={"source": "webui", "purpose": "管理台控制台"},
        )
    except SandboxError as exc:
        raise _http_error(exc) from exc
    await hub.notify_change("sandbox")
    return ok(camelize(result))


# ── 策略与维护 ──────────────────────────────────────────────────────────────


class SettingsBody(BaseModel):
    """前端提交 camelCase；群号用字符串避免 JS 精度问题。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    enabled: bool
    group_mode: Literal["all", "allowlist"]
    allowed_groups: list[str] = Field(default_factory=list, max_length=500)
    blocked_groups: list[str] = Field(default_factory=list, max_length=500)
    admin_only: bool
    member_daily_runs: int
    group_daily_runs: int
    member_cooldown_seconds: int
    timeout_seconds: int
    memory_mb: int
    disk_quota_mb: int
    max_output_chars: int
    max_share_mb: int
    allow_unisolated: bool
    # 入箱设置有默认值：旧页面不带这些字段时保持原值语义（默认值）。
    inbox_enabled: bool = True
    inbox_auto_import: Literal["always", "sandbox_turns", "off"] = "sandbox_turns"
    inbox_types: list[Literal["image", "file", "record", "video"]] = Field(
        default_factory=lambda: list(INBOX_MEDIA_TYPES), max_length=4
    )
    inbox_max_file_mb: int = 20
    inbox_max_per_turn: int = 6
    inbox_history_minutes: int = 180
    inbox_group_daily_mb: int = 500
    # 私聊虚拟电脑：不带时沿用当前值（旧页面保存不会把它们重置成默认）。
    private_enabled: bool | None = None
    private_daily_runs: int | None = None
    private_inbox: bool | None = None
    private_inbox_daily_mb: int | None = None
    private_inbox_max_file_mb: int | None = None
    private_disk_quota_mb: int | None = None
    private_file_mb: int | None = None
    private_network: bool | None = None
    group_network: bool | None = None


_PRIVATE_FIELDS = (
    "private_enabled",
    "private_daily_runs",
    "private_inbox",
    "private_inbox_daily_mb",
    "private_inbox_max_file_mb",
    "private_disk_quota_mb",
    "private_file_mb",
    "private_network",
    "group_network",
)


def _group_list(values: list[str], field: str) -> list[int]:
    result: list[int] = []
    for raw in values:
        text = str(raw).strip()
        if not text:
            continue
        if not text.isdigit() or not 4 <= len(text) <= 12:  # noqa: PLR2004
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, f"{field} 里有不合法的群号：{text[:20]}"
            )
        value = int(text)
        if value not in result:
            result.append(value)
    return result


@router.get("/settings")
async def sandbox_settings(_session: AdminReadSession) -> dict[str, Any]:
    settings = load_settings()
    return ok({"settings": _settings_payload(settings), "defaults": _settings_payload(SandboxSettings())})


@router.put("/settings")
async def sandbox_update_settings(body: SettingsBody, _session: AdminWriteSession) -> dict[str, Any]:
    current = load_settings()
    values = body.model_dump()
    for key in _PRIVATE_FIELDS:
        if values.get(key) is None:
            values[key] = getattr(current, key)
    try:
        settings = SandboxSettings.model_validate(
            {
                **values,
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
    await hub.notify_change("sandbox")
    return ok({"settings": _settings_payload(saved)})


@router.get("/usage")
async def sandbox_usage(_session: AdminReadSession) -> dict[str, Any]:
    payload = _usage_payload(load_settings())
    rows = [{"workspace": f"g{row['groupId']}", **row} for row in payload["groups"]]
    await _label_workspaces(rows)
    names = {row["groupId"]: row.get("groupName") for row in rows}
    payload["groups"] = [{**row, "groupName": names.get(row["groupId"])} for row in payload["groups"]]
    payload["members"] = [
        {**row, "groupName": names.get(row["groupId"])} for row in payload["members"]
    ]
    return ok(payload)


# ── 群聊附件入箱 ────────────────────────────────────────────────────────────


@router.get("/inbox")
async def sandbox_inbox(
    _session: AdminReadSession,
    group: str | None = Query(default=None, pattern=r"^\d{4,12}$"),
    inbox_status: str | None = Query(
        default=None, alias="status", pattern="^(?:imported|reused|failed|skipped|removed)$"
    ),
    media_type: str | None = Query(default=None, alias="type", pattern="^(?:image|file|record|video)$"),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    """附件收件箱：最近的入箱记录、今日统计、失败原因说明和当前取文件配置。"""

    settings = load_settings()
    items = inbox_store.recent_events(
        group_id=int(group) if group else None,
        status=inbox_status,
        media_type=media_type,
        limit=limit,
    )
    rows = camelize(items)
    for row in rows:
        row["workspace"] = f"g{row.get('groupId')}" if row.get("groupId") else None
        row["groupId"] = str(row["groupId"]) if row.get("groupId") is not None else None
        for key in ("userId", "senderId", "messageId"):
            if row.get(key) is not None:
                row[key] = str(row[key])
    await _label_workspaces(rows)
    summary = camelize(inbox_store.summary())
    return ok(
        {
            "items": rows,
            "summary": summary,
            "reasons": {
                code: {"message": message, "hint": hint} for code, (message, hint) in FAILURE_HINTS.items()
            },
            "hosts": list(host_patterns()),
            "localRoots": bool(_local_roots_configured()),
            "settings": {
                "enabled": settings.inbox_enabled,
                "autoImport": settings.inbox_auto_import,
                "types": list(settings.inbox_types),
                "maxFileMb": settings.inbox_max_file_mb,
                "maxPerTurn": settings.inbox_max_per_turn,
                "historyMinutes": settings.inbox_history_minutes,
                "groupDailyMb": settings.inbox_group_daily_mb,
            },
        }
    )


def _local_roots_configured() -> bool:
    return bool(os.environ.get("AGENT_SANDBOX_INBOX_LOCAL_ROOTS", "").strip())


@router.post("/maintenance/prune")
async def sandbox_prune(_session: AdminWriteSession) -> dict[str, Any]:
    try:
        result = await SandboxClient().prune()
    except SandboxError as exc:
        raise _http_error(exc) from exc
    await hub.notify_change("sandbox")
    return ok(camelize(result))


__all__ = ["camelize", "router", "workspace_group_id"]
