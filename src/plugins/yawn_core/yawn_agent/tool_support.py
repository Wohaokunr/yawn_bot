# ruff: noqa: E501,TRY003,TRY004,TRY300,TRY301,ASYNC240,TID252
"""Tool handler 共用的紧凑投影、参数校验与文件安全 helper。"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from ..data_models.group_agent_message import GroupAgentMessage
from .context import now_beijing
from .log import dbg

MAX_FILE_BYTES = 32 * 1024 * 1024
DEFAULT_MEMBER_TOOL_LIMIT = 30
MAX_MEMBER_TOOL_LIMIT = 50
DEFAULT_PROFILE_TOOL_LIMIT = 6
MAX_PROFILE_TOOL_LIMIT = 10
DEFAULT_MEMORY_TOOL_LIMIT = 6
MAX_MEMORY_TOOL_LIMIT = 10
DEFAULT_RELATION_TOOL_LIMIT = 12
MAX_RELATION_TOOL_LIMIT = 20
_FILE_ROOT = Path(os.environ.get("AGENT_FILE_ROOT", "data/agent_files")).resolve()
_ALLOWED_FILE_HOSTS = frozenset(
    host.strip().lower()
    for host in os.environ.get("AGENT_FILE_ALLOWED_HOSTS", "").split(",")
    if host.strip()
)

def _compact_group_info(raw: Any) -> dict[str, Any]:
    """Project OneBot group metadata to fields useful to the model."""

    if not isinstance(raw, dict):
        raise ValueError("群信息响应格式错误")
    return {
        key: raw[key]
        for key in ("group_id", "group_name", "member_count", "max_member_count")
        if raw.get(key) is not None
    }


def _compact_group_member(raw: Any) -> dict[str, Any]:
    """Drop protocol/account metadata that should not enter the next prompt."""

    if not isinstance(raw, dict):
        raise ValueError("群成员信息响应格式错误")
    user_id = raw.get("user_id")
    name = str(raw.get("card") or raw.get("nickname") or user_id or "未知成员")[:64]
    compact: dict[str, Any] = {"user_id": user_id, "name": name}
    role = str(raw.get("role") or "").strip()
    title = str(raw.get("title") or "").strip()
    if role and role != "member":
        compact["role"] = role
    if title:
        compact["title"] = title[:64]
    return compact


def _compact_message_text(raw: Any, *, maximum: int = 800) -> str:
    if not isinstance(raw, dict):
        return ""
    message = raw.get("message")
    if isinstance(message, str):
        return message[:maximum]
    parts: list[str] = []
    if isinstance(message, list):
        for segment in message[:24]:
            if not isinstance(segment, dict):
                continue
            segment_type = str(segment.get("type") or "").strip().lower()
            raw_data = segment.get("data")
            data = raw_data if isinstance(raw_data, dict) else {}
            if segment_type == "text":
                parts.append(str(data.get("text") or ""))
            elif segment_type in {"image", "record", "video", "file", "face"}:
                labels = {
                    "image": "图片",
                    "record": "语音",
                    "video": "视频",
                    "file": "文件",
                    "face": "表情",
                }
                parts.append(f"[{labels[segment_type]}]")
    text = "".join(parts).strip()
    if not text:
        text = str(raw.get("raw_message") or "").strip()
        text = re.sub(r"\[CQ:([a-zA-Z0-9_-]+)[^\]]*\]", r"[\1]", text)
    return text[:maximum]


def _message_media_refs(raw: Any) -> list[dict[str, Any]]:
    """Keep image handles as resolver-only metadata, never prompt-visible URLs."""

    if not isinstance(raw, dict) or not isinstance(raw.get("message"), list):
        return []
    output: list[dict[str, Any]] = []
    source_message_id = raw.get("message_id")
    for segment in raw["message"][:24]:
        if not isinstance(segment, dict) or str(segment.get("type") or "") != "image":
            continue
        raw_data = segment.get("data")
        data = raw_data if isinstance(raw_data, dict) else {}
        ref: dict[str, Any] = {"type": "image", "source": "tool"}
        if source_message_id is not None:
            ref["source_message_id"] = source_message_id
        file_handle = data.get("file") or data.get("file_id")
        if file_handle is not None:
            ref["file"] = file_handle
        elif data.get("url") is not None:
            ref["url"] = data["url"]
        if "file" in ref or "url" in ref:
            output.append(ref)
    return output


def _compact_onebot_message(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("消息响应格式错误")
    raw_sender = raw.get("sender")
    sender = raw_sender if isinstance(raw_sender, dict) else {}
    user_id = raw.get("user_id") or sender.get("user_id")
    compact: dict[str, Any] = {
        "message_id": raw.get("message_id"),
        "user_id": user_id,
        "name": str(sender.get("card") or sender.get("nickname") or user_id or "未知成员")[:64],
        "text": _compact_message_text(raw),
    }
    if raw.get("time") is not None:
        compact["time"] = raw.get("time")
    media_refs = _message_media_refs(raw)
    if media_refs:
        compact["media_types"] = ["image"] * len(media_refs)
        compact["_agent_media_refs"] = media_refs
    return {key: value for key, value in compact.items() if value not in (None, "")}


def _compact_notice(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("群公告响应格式错误")
    sender = raw.get("sender_id") or raw.get("user_id")
    content = raw.get("message") or raw.get("content") or raw.get("text") or ""
    result = {
        "notice_id": raw.get("notice_id") or raw.get("id"),
        "sender_id": sender,
        "publish_time": raw.get("publish_time") or raw.get("time"),
        "content": str(content)[:1000],
    }
    return {key: value for key, value in result.items() if value not in (None, "")}


def _compact_essence(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("精华消息响应格式错误")
    content = raw.get("content")
    if isinstance(content, str):
        compact_content = content[:800]
    else:
        compact_content = _compact_message_text(raw, maximum=800)
    result = {
        "message_id": raw.get("message_id"),
        "sender_id": raw.get("sender_id"),
        "sender_name": str(raw.get("sender_nick") or raw.get("sender_name") or "")[:64],
        "operator_id": raw.get("operator_id"),
        "operator_name": str(raw.get("operator_nick") or raw.get("operator_name") or "")[:64],
        "content": compact_content,
    }
    return {key: value for key, value in result.items() if value not in (None, "")}


def _payload_list(raw: Any, *keys: str) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in keys:
            value = raw.get(key)
            if isinstance(value, list):
                return value
    return []


def _compact_group_file(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("群文件响应格式错误")
    result = {
        "file_id": raw.get("file_id") or raw.get("id"),
        "name": str(raw.get("file_name") or raw.get("name") or "")[:160],
        "busid": raw.get("busid") or raw.get("bus_id"),
        "size": raw.get("file_size") or raw.get("size"),
        "uploader_id": raw.get("uploader") or raw.get("uploader_id"),
        "uploader_name": str(raw.get("uploader_name") or "")[:64],
        "upload_time": raw.get("upload_time"),
    }
    return {key: value for key, value in result.items() if value not in (None, "")}


def _compact_group_folder(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("群文件夹响应格式错误")
    result = {
        "folder_id": raw.get("folder_id") or raw.get("id"),
        "name": str(raw.get("folder_name") or raw.get("name") or "")[:160],
        "file_count": raw.get("total_file_count") or raw.get("file_count"),
    }
    return {key: value for key, value in result.items() if value not in (None, "")}


async def _require_known_message(
    session: Any, group_id: int, message_id: int
) -> GroupAgentMessage:
    if message_id == 0:
        raise ValueError("message_id 不能为 0")
    if session is None:
        raise PermissionError("消息操作需要数据库会话")
    row = await session.scalar(
        select(GroupAgentMessage).where(
            GroupAgentMessage.group_id == group_id,
            GroupAgentMessage.message_id == message_id,
            (
                GroupAgentMessage.expires_at.is_(None)
                | (GroupAgentMessage.expires_at >= now_beijing())
            ),
        )
    )
    if row is None:
        raise PermissionError("message_id 必须来自当前群近期已知消息")
    return row


async def _require_current_group_message_api(
    bot: Any, group_id: int, message_id: int
) -> dict[str, Any]:
    if message_id == 0:
        raise ValueError("message_id 不能为 0")
    raw = await bot.call_api("get_msg", message_id=message_id)
    if not isinstance(raw, dict):
        raise ValueError("无法确认消息所属群")
    raw_group_id = raw.get("group_id")
    if raw_group_id is None or int(raw_group_id) != int(group_id):
        raise PermissionError("message_id 不属于当前群")
    return raw


async def _require_group_member_api(
    bot: Any, group_id: int, user_id: int
) -> dict[str, Any]:
    raw = await bot.call_api(
        "get_group_member_info", group_id=group_id, user_id=user_id
    )
    if not isinstance(raw, dict) or int(raw.get("user_id") or 0) != int(user_id):
        raise PermissionError("目标用户不是当前群成员")
    return raw


def _tool_result_limit(args: dict[str, Any], *, default: int, maximum: int) -> int:
    try:
        limit = int(args.get("limit") or default)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit 必须是整数") from exc
    if limit < 1 or limit > maximum:
        raise ValueError(f"limit 必须在 1~{maximum} 之间")
    return limit


def _jsonable(value: object) -> object:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def _check_local_path(path: Path) -> Path:
    path = path.expanduser().resolve()
    if _FILE_ROOT not in path.parents and path != _FILE_ROOT:
        dbg(f"本地文件校验拒绝: {path} 不在 {_FILE_ROOT} 内")
        raise PermissionError("本地文件必须位于 Agent 文件目录")
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        dbg(f"本地文件校验拒绝: {path} 不存在或超过 {MAX_FILE_BYTES} 字节")
        raise ValueError("文件不存在或超过大小限制")
    dbg(f"本地文件校验通过: {path}")
    return path


def _check_downloaded_path(path: Path) -> Path:
    path = path.expanduser().resolve()
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("下载文件不存在或超过大小限制")
    return path


def _validate_image_path(path: Path) -> Path:
    path = _check_local_path(path)
    mime = mimetypes.guess_type(path.name)[0] or ""
    if not mime.startswith("image/"):
        raise ValueError("文件不是受支持的图片类型")
    return path


async def _download_allowed_file(file_ref: str) -> tuple[Path, str | None]:
    parsed = urlparse(file_ref)
    if parsed.hostname is None or parsed.hostname.lower() not in _ALLOWED_FILE_HOSTS:
        dbg(
            f"远程文件下载拒绝: 主机 {parsed.hostname!r} 不在白名单 "
            f"{sorted(_ALLOWED_FILE_HOSTS)}"
        )
        raise PermissionError("远程文件域名不在白名单")
    dbg(f"远程文件开始下载: {file_ref}")
    # 流式下载并边下边校验大小，防止白名单域名投递超大文件耗尽内存。
    async with (
        httpx.AsyncClient(timeout=15, follow_redirects=True) as client,
        client.stream("GET", file_ref) as response,
    ):
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=Path(parsed.path).suffix or ".download"
        ) as handle:
            total = 0
            try:
                async for chunk in response.aiter_bytes(64 * 1024):
                    total += len(chunk)
                    if total > MAX_FILE_BYTES:
                        raise ValueError("文件超过大小限制")
                    handle.write(chunk)
            except BaseException:
                handle.close()
                Path(handle.name).unlink(missing_ok=True)
                raise
            return Path(handle.name), content_type or None
