# ruff: noqa: E501, TRY003, TRY004, TRY300, TID252,PLR2004
"""Tool handler 共用的紧凑投影、参数校验与文件安全 helper。"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select

from ..data_models.agent_memory import AgentPrivacy
from ..data_models.group_agent_message import GroupAgentMessage
from .context import now_beijing
from .log import dbg
from .safe_download import stream_download
from .tool_args import ToolArgumentError

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

_CST = timezone(timedelta(hours=8))
_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
# QQ 群文件/文件夹名里不允许的字符；同时拦截路径穿越。
_FILE_NAME_FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
# 不允许机器人上传到群文件的可执行/脚本类扩展名。
BLOCKED_FILE_EXTENSIONS = frozenset(
    {
        ".exe", ".bat", ".cmd", ".com", ".scr", ".pif", ".msi", ".msp",
        ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh",
        ".hta", ".cpl", ".reg", ".lnk", ".jar", ".apk", ".sh", ".dll",
    }
)


def readable_time(value: Any) -> str | None:
    """OneBot 时间戳（秒/毫秒）或 datetime → 北京时间 ``YYYY-MM-DD HH:MM``。"""

    if value in (None, "", 0):
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=_CST)
    else:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return str(value)[:32]
        if number <= 0:
            return None
        if number > 10_000_000_000:
            number /= 1000
        try:
            moment = datetime.fromtimestamp(number, tz=_CST)
        except (OverflowError, OSError, ValueError):
            return None
    return moment.astimezone(_CST).strftime("%Y-%m-%d %H:%M")


def readable_size(value: Any) -> str | None:
    try:
        size = int(value)
    except (TypeError, ValueError):
        return None
    if size < 0:
        return None
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / 1024 / 1024:.1f}MB"
    return f"{size / 1024 / 1024 / 1024:.2f}GB"


def weekday_label(moment: datetime) -> str:
    return _WEEKDAYS[moment.weekday()]


def clean_single_line(value: Any, *, limit: int) -> str:
    """去掉换行和控制字符，用于群名、群名片、头衔这类单行文本。"""

    text = "".join(
        " " if char in "\r\n\t" else char
        for char in str(value or "")
        if char in "\r\n\t" or unicodedata.category(char)[0] != "C"
    )
    return re.sub(r"\s+", " ", text).strip()[:limit]


def validate_file_component(value: Any, *, field_name: str, limit: int = 120) -> str:
    """群文件/文件夹名：去首尾空白，拒绝路径分隔符和非法字符。"""

    name = str(value or "").strip()
    if not name or name in {".", ".."}:
        raise ToolArgumentError(f"{field_name} 不能为空")
    if _FILE_NAME_FORBIDDEN.search(name):
        raise ToolArgumentError(
            f"{field_name} 含有非法字符",
            hint='名字里不能有 \\ / : * ? " < > | 和换行',
        )
    if len(name) > limit:
        raise ToolArgumentError(f"{field_name} 超过 {limit} 字符")
    return name


def sanitize_upload_name(value: Any, *, source_name: str) -> str:
    """群文件显示名：剥路径、去非法字符，没有扩展名时补源文件扩展名。"""

    raw = str(value or "").replace("\\", "/").split("/")[-1]
    cleaned = _FILE_NAME_FORBIDDEN.sub("", raw).strip().strip(".")
    if not cleaned:
        cleaned = Path(source_name).name
    source_suffix = Path(source_name).suffix
    if source_suffix and not Path(cleaned).suffix:
        cleaned += source_suffix
    if len(cleaned) > 128:
        suffix = Path(cleaned).suffix[:16]
        cleaned = cleaned[: 128 - len(suffix)] + suffix
    return cleaned


def check_file_extension(*names: str) -> None:
    for name in names:
        suffix = Path(str(name or "")).suffix.lower()
        if suffix in BLOCKED_FILE_EXTENSIONS:
            raise PermissionError(f"不允许发送 {suffix} 类型的可执行/脚本文件")


async def _opted_out_user_ids(session: Any, group_id: int) -> frozenset[int]:
    """读取当前群已退出 Agent 记忆的成员；与 Prompt 历史的隐私过滤保持一致。"""

    if session is None or not callable(getattr(session, "execute", None)):
        return frozenset()
    rows = (
        await session.execute(
            select(AgentPrivacy.user_id).where(
                AgentPrivacy.group_id == group_id,
                AgentPrivacy.opted_out.is_(True),
            )
        )
    ).scalars()
    return frozenset(int(user_id) for user_id in rows if user_id is not None)


def _compact_group_info(raw: Any) -> dict[str, Any]:
    """Project OneBot group metadata to fields useful to the model."""

    if not isinstance(raw, dict):
        raise ValueError("群信息响应格式错误")
    return {
        key: raw[key]
        for key in ("group_id", "group_name", "member_count", "max_member_count")
        if raw.get(key) is not None
    }


def _compact_group_member_detail(raw: Any) -> dict[str, Any]:
    """单个成员详情：在基础投影上补等级、入群和最后发言时间。"""

    compact = _compact_group_member(raw)
    nickname = str(raw.get("nickname") or "").strip()
    if nickname and nickname != compact.get("name"):
        compact["nickname"] = nickname[:64]
    level = str(raw.get("level") or "").strip()
    if level and level != "0":
        compact["level"] = level[:16]
    joined = readable_time(raw.get("join_time"))
    if joined:
        compact["join_time"] = joined
    last_sent = readable_time(raw.get("last_sent_time"))
    if last_sent:
        compact["last_sent_time"] = last_sent
    return compact


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


def _reply_target(raw: dict[str, Any]) -> int | None:
    message = raw.get("message")
    if not isinstance(message, list):
        return None
    for segment in message[:4]:
        if isinstance(segment, dict) and str(segment.get("type") or "") == "reply":
            raw_data = segment.get("data")
            data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
            try:
                return int(data.get("id") or 0) or None
            except (TypeError, ValueError):
                return None
    return None


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
        compact["time"] = readable_time(raw.get("time")) or raw.get("time")
    reply_to = _reply_target(raw)
    if reply_to is not None:
        compact["reply_to_message_id"] = reply_to
    media_refs = _message_media_refs(raw)
    if media_refs:
        compact["media_types"] = ["image"] * len(media_refs)
        compact["_agent_media_refs"] = media_refs
    return {key: value for key, value in compact.items() if value not in (None, "")}


def _compact_notice(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("群公告响应格式错误")
    sender = raw.get("sender_id") or raw.get("user_id")
    content: Any = raw.get("message") or raw.get("content") or raw.get("text") or ""
    if isinstance(content, dict):
        # NapCat: {"text": "...", "images": [...]}；只保留正文，图片记数量。
        images = content.get("images") or content.get("image") or []
        content = str(content.get("text") or "")
        if isinstance(images, list) and images:
            content = f"{content}[图片x{len(images)}]"
    raw_time = raw.get("publish_time") or raw.get("time")
    result = {
        "notice_id": raw.get("notice_id") or raw.get("id"),
        "sender_id": sender,
        "publish_time": readable_time(raw_time),
        "_sort_time": raw_time,
        "content": str(content).replace("&#10;", "\n")[:1000],
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
        "operator_time": readable_time(raw.get("operator_time")),
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
    raw_size = raw.get("file_size") or raw.get("size")
    result = {
        "file_id": raw.get("file_id") or raw.get("id"),
        "name": str(raw.get("file_name") or raw.get("name") or "")[:160],
        "busid": raw.get("busid") or raw.get("bus_id"),
        "size": readable_size(raw_size) or raw_size,
        "uploader_id": raw.get("uploader") or raw.get("uploader_id"),
        "uploader_name": str(raw.get("uploader_name") or "")[:64],
        "upload_time": readable_time(raw.get("upload_time")),
        "_sort_time": raw.get("upload_time"),
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


def sort_by_time_desc(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按内部 ``_sort_time`` 倒序并移除该内部字段。"""

    def _key(item: dict[str, Any]) -> float:
        try:
            return float(item.get("_sort_time") or 0)
        except (TypeError, ValueError):
            return 0.0

    ordered = sorted(items, key=_key, reverse=True)
    return [strip_sort_key(item) for item in ordered]


def strip_sort_key(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key != "_sort_time"}


def agent_file_root() -> Path:
    return _FILE_ROOT


def resolve_agent_file(file_ref: str) -> Path:
    """相对名按 Agent 文件目录解析；绝对路径仍必须落在该目录内。"""

    candidate = Path(file_ref).expanduser()
    if not candidate.is_absolute():
        candidate = _FILE_ROOT / candidate
    return _check_local_path(candidate)


async def _download_allowed_file(file_ref: str) -> tuple[Path, str | None]:
    parsed = urlparse(file_ref)
    if parsed.hostname is None or parsed.hostname.lower() not in _ALLOWED_FILE_HOSTS:
        dbg(
            f"远程文件下载拒绝: 主机 {parsed.hostname!r} 不在白名单 "
            f"{sorted(_ALLOWED_FILE_HOSTS)}"
        )
        raise PermissionError("远程文件域名不在白名单")
    dbg(f"远程文件开始下载: host={parsed.hostname!r}")
    # 流式下载并边下边校验大小；重定向逐跳校验白名单，不能跳出允许的域名。
    return await stream_download(
        file_ref,
        allowed_hosts=_ALLOWED_FILE_HOSTS,
        max_bytes=MAX_FILE_BYTES,
        suffix=Path(parsed.path).suffix or ".download",
        host_error="远程文件域名不在白名单",
        size_error="文件超过大小限制",
    )
