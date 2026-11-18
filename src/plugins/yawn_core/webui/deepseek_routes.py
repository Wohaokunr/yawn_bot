"""Administrator workspace for the first-party DeepSeek application APIs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from src.plugins.yawn_core import deepseek
from src.plugins.yawn_core.yawn_agent.web_search_client import (
    WebSearchError,
    validate_fetch_url,
)

from .config import API_PATH
from .deps import AdminReadSession, AdminWriteSession, ok

if TYPE_CHECKING:
    from .auth import Session

router = APIRouter(prefix=f"{API_PATH}/deepseek")
_SESSION_TTL = 1800
_MAX_SESSIONS = 24
_MAX_TURNS = 12
_MAX_FILES = 12
_MAX_SESSION_AGE = 2700
_MAX_HISTORY_BYTES = 1024 * 1024


@dataclass(slots=True)
class Workspace:
    owner: str
    key_digest: str
    created: float
    updated: float
    messages: list[dict[str, Any]] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)
    uploaded: dict[tuple[str, str], str] = field(default_factory=dict)
    title: str = ""
    turns: int = 0
    history_bytes: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    expiry: asyncio.TimerHandle | None = None


_sessions: dict[str, Workspace] = {}
_cleanup_tasks: set[asyncio.Task[None]] = set()


class ChatBody(BaseModel):
    text: str = Field(default="", max_length=12000)
    files: list[str] = Field(default_factory=list, max_length=3)
    search: bool = False
    read_url: bool = False
    thinking: bool = False
    timezone: str = Field(default="Asia/Shanghai", max_length=64)
    language: str = Field(
        default="zh-CN", pattern=r"^[a-zA-Z]{2,8}(?:-[a-zA-Z0-9]{1,8}){0,4}$"
    )


class SpeechBody(BaseModel):
    text: str = Field(min_length=1, max_length=deepseek.MAX_TTS_CHARS)
    voice: Literal["stella", "tide"] = "stella"


def _error(exc: deepseek.DeepSeekError) -> HTTPException:
    messages = {
        "content_filter": deepseek.REFUSAL,
        "unsupported_file": "此文件格式暂不支持",
        "unsupported_audio": "请使用 16 位 PCM WAV 或 Ogg Opus",
        "upstream_400": "DeepSeek 无法处理此请求，请检查输入格式或内容",
        "upstream_429": "DeepSeek 调用受限，请稍后再试",
        "upstream_timeout": "DeepSeek 请求超时",
        "stream_interrupted": "DeepSeek 回复中断，请重试",
    }
    return HTTPException(exc.status, detail=messages.get(exc.code, "DeepSeek 调用失败"))


def _key() -> str:
    if not deepseek.config.deepseek_webui_enabled:
        raise HTTPException(503, detail="DeepSeek 工作台未启用")
    key = deepseek.api_key()
    if not key:
        raise HTTPException(503, detail="DeepSeek 密钥未配置")
    return key


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def _cleanup(key: str, workspace: Workspace) -> None:
    if workspace.key_digest == _digest(key):
        client = deepseek.DeepSeekClient(key)
        await asyncio.gather(
            *(client.delete(file_id) for file_id in workspace.files.values())
        )


async def _prune(key: str) -> None:
    now = time.monotonic()
    key_digest = _digest(key)
    expired = [
        session_id
        for session_id, value in _sessions.items()
        if not value.lock.locked()
        and (
            now - value.updated >= _SESSION_TTL
            or now - value.created >= _MAX_SESSION_AGE
            or value.key_digest != key_digest
        )
    ]
    workspaces = [_pop_workspace(session_id) for session_id in expired]
    await asyncio.gather(*(_cleanup(key, workspace) for workspace in workspaces))


def _pop_workspace(workspace_id: str) -> Workspace:
    workspace = _sessions.pop(workspace_id)
    if workspace.expiry:
        workspace.expiry.cancel()
    return workspace


def _touch(workspace_id: str, workspace: Workspace) -> None:
    workspace.updated = time.monotonic()
    if workspace.expiry:
        workspace.expiry.cancel()
    remaining = min(
        _SESSION_TTL, _MAX_SESSION_AGE - (workspace.updated - workspace.created)
    )
    workspace.expiry = asyncio.get_running_loop().call_later(
        max(remaining, 0), _expire, workspace_id, workspace
    )


def _expire(workspace_id: str, workspace: Workspace) -> None:
    if _sessions.get(workspace_id) is not workspace:
        return
    if workspace.lock.locked():
        workspace.expiry = asyncio.get_running_loop().call_later(
            5, _expire, workspace_id, workspace
        )
        return
    _pop_workspace(workspace_id)
    if key := deepseek.api_key():
        task = asyncio.create_task(_cleanup(key, workspace))
        _cleanup_tasks.add(task)
        task.add_done_callback(_cleanup_tasks.discard)


def _workspace(workspace_id: str, session: Session, key: str) -> Workspace:
    value = _sessions.get(workspace_id)
    if (
        value is None
        or value.owner != session.session_id
        or value.key_digest != _digest(key)
        or time.monotonic() - value.updated >= _SESSION_TTL
        or time.monotonic() - value.created >= _MAX_SESSION_AGE
    ):
        raise HTTPException(404, detail="会话已过期，请新建会话")
    if value.lock.locked():
        raise HTTPException(409, detail="当前会话正在处理请求")
    return value


async def _upload(request: Request, limit: int) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(413, detail="文件超过大小限制")
        body.extend(chunk)
    if not body:
        raise HTTPException(400, detail="请选择文件")
    return bytes(body)


def _filename(value: str) -> str:
    return Path(value.replace("\\", "/")).name


def _reminder(body: ChatBody) -> dict[str, str]:
    try:
        local = datetime.now(ZoneInfo(body.timezone))
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise HTTPException(422, detail="无效时区") from exc
    return {
        "role": "latest_reminder",
        "content": f"{local:%Y-%m-%d}, {body.timezone}, Web, {body.language}",
    }


def _chat_tools(body: ChatBody) -> list[dict[str, str]]:
    if body.read_url:
        # DeepSeek discovers links in the final message.
        try:
            for value in re.findall(
                r"https?://[^\s<>\]\"']+", body.text, flags=re.IGNORECASE
            ):
                validate_fetch_url(value.rstrip(".,);，。；）"))
        except WebSearchError as exc:
            raise HTTPException(400, detail="链接地址不可读取") from exc
    return [
        {"type": kind}
        for kind, enabled in (
            ("deepseek_web_search", body.search),
            ("deepseek_read_url", body.read_url),
        )
        if enabled
    ]


@router.get("/status")
async def status(_session: AdminReadSession) -> dict[str, Any]:
    return ok(
        {
            "enabled": deepseek.config.deepseek_webui_enabled,
            "configured": bool(deepseek.api_key()),
        }
    )


@router.post("/sessions")
async def create_session(session: AdminWriteSession) -> dict[str, Any]:
    key = _key()
    await _prune(key)
    if len(_sessions) >= _MAX_SESSIONS:
        raise HTTPException(429, detail="工作台会话已满，请结束一个会话后重试")
    workspace_id = secrets.token_urlsafe(18)
    now = time.monotonic()
    workspace = Workspace(session.session_id, _digest(key), now, now)
    _sessions[workspace_id] = workspace
    _touch(workspace_id, workspace)
    return ok({"id": workspace_id})


@router.delete("/sessions/{workspace_id}")
async def delete_session(
    workspace_id: str, session: AdminWriteSession
) -> dict[str, Any]:
    key = _key()
    workspace = _workspace(workspace_id, session, key)
    _pop_workspace(workspace_id)
    await _cleanup(key, workspace)
    return ok({"deleted": True})


@router.post("/sessions/{workspace_id}/files")
async def upload_file(
    workspace_id: str,
    request: Request,
    session: AdminWriteSession,
    name: Annotated[str, Query(min_length=1, max_length=200)],
    mode: deepseek.ParseMode = "text_recognition",
) -> dict[str, Any]:
    key = _key()
    workspace = _workspace(workspace_id, session, key)
    async with workspace.lock:
        if len(workspace.files) >= _MAX_FILES:
            raise HTTPException(409, detail="当前会话附件已满，请新建会话")
        filename = _filename(name)
        data = await _upload(request, deepseek.MAX_FILE_BYTES)
        try:
            purpose = deepseek.file_purpose(filename, mode)
            digest = (purpose, hashlib.sha256(data).hexdigest())
            if existing := workspace.uploaded.get(digest):
                _touch(workspace_id, workspace)
                return ok({"id": existing, "name": filename})
            file_id = await deepseek.DeepSeekClient(key).upload(filename, data, mode)
        except deepseek.DeepSeekError as exc:
            raise _error(exc) from exc
        handle = secrets.token_urlsafe(18)
        workspace.files[handle] = file_id
        workspace.uploaded[digest] = handle
        _touch(workspace_id, workspace)
        return ok({"id": handle, "name": filename})


@router.post("/sessions/{workspace_id}/chat")
async def chat(
    workspace_id: str, body: ChatBody, session: AdminWriteSession
) -> dict[str, Any]:
    key = _key()
    workspace = _workspace(workspace_id, session, key)
    text = body.text.strip()
    if not (text or body.files):
        raise HTTPException(422, detail="请输入消息或添加附件")
    if len(set(body.files)) != len(body.files) or any(
        handle not in workspace.files for handle in body.files
    ):
        raise HTTPException(422, detail="附件不属于此会话")
    tools = _chat_tools(body)
    reminder = _reminder(body)
    async with workspace.lock:
        if workspace.turns >= _MAX_TURNS:
            raise HTTPException(409, detail="对话已达到轮数上限，请新建会话")
        blocks = [
            {"type": "file", "file_id": workspace.files[handle]}
            for handle in body.files
        ]
        if text:
            blocks.append({"type": "text", "text": text})
        user = {"role": "user", "content": blocks if body.files else text}
        try:
            result = await deepseek.DeepSeekClient(key).chat(
                {
                    "messages": [*workspace.messages, reminder, user],
                    "thinking": {"type": "enabled" if body.thinking else "disabled"},
                    "safety_check": {"level": "high"},
                    "title": {"enabled": not workspace.title},
                    **({"tools": tools} if tools else {}),
                }
            )
        except deepseek.DeepSeekError as exc:
            if exc.code != "content_filter":
                raise _error(exc) from exc
            result = deepseek.ChatResult(finish_reason="content_filter")
            result.finish()
        _touch(workspace_id, workspace)
        workspace.turns += 1
        if result.finish_reason != "content_filter":
            records = (reminder, user, result.message)
            size = len(json.dumps(records, ensure_ascii=False).encode())
            if workspace.history_bytes + size > _MAX_HISTORY_BYTES:
                workspace.turns = _MAX_TURNS
                raise HTTPException(409, detail="会话内容超过上限，请新建会话")
            # Preserve the complete server tool calls and their signatures.
            workspace.messages.extend(records)
            workspace.history_bytes += size
            workspace.title = workspace.title or result.title or text[:24] or "文件对话"
        return ok({**result.public(), "title": workspace.title})


@router.post("/ocr")
async def ocr(
    request: Request,
    _session: AdminWriteSession,
    name: Annotated[str, Query(min_length=1, max_length=200)],
    mode: deepseek.ParseMode = "text_recognition",
) -> dict[str, Any]:
    client = deepseek.DeepSeekClient(_key())
    data = await _upload(request, deepseek.MAX_FILE_BYTES)
    try:
        result = await client.ocr(_filename(name), data, mode)
    except deepseek.DeepSeekError as exc:
        raise _error(exc) from exc
    return ok(
        {
            field: result[field]
            for field in ("page_count", "pages", "usage")
            if field in result
        }
    )


@router.post("/asr")
async def asr(request: Request, _session: AdminWriteSession) -> dict[str, Any]:
    client = deepseek.DeepSeekClient(_key())
    data = await _upload(request, deepseek.MAX_AUDIO_BYTES)
    try:
        result = await client.asr(data)
    except deepseek.DeepSeekError as exc:
        raise _error(exc) from exc
    return ok({"text": result.get("text", ""), "durationMs": result.get("duration_ms")})


@router.post("/tts")
async def tts(body: SpeechBody, _session: AdminWriteSession) -> Response:
    client = deepseek.DeepSeekClient(_key())
    try:
        audio = await client.tts(body.text, body.voice)
    except deepseek.DeepSeekError as exc:
        raise _error(exc) from exc
    return Response(
        audio, media_type="audio/wav", headers={"Cache-Control": "no-store"}
    )
