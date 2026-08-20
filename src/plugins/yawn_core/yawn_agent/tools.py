# ruff: noqa: E501,F401,I001,TID252,PLR0912,PLR0913,PLR0917,C901,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100,PLR0915,PLR2004
"""群聊 Agent 的严格工具 schema 与 OneBot 执行器。"""

from __future__ import annotations

import json
import mimetypes
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from nonebot.adapters.onebot.v11 import MessageSegment
from sqlalchemy import select

from ..data_models.agent_audit import AgentAudit
from ..data_models.agent_memory import AgentMemory
from ..data_models.group_agent_config import GroupAgentConfig
from ..data_models.group_agent_message import GroupAgentMessage
from .capabilities import (
    BotGroupCapabilities,
    probe_group_capabilities,
    target_can_be_muted,
    user_can_manage_group,
)

MAX_TOOL_ROUNDS = 4
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_FORWARD_SEND = 20
MAX_FORWARD_BYTES = 128 * 1024
_FILE_ROOT = Path(os.environ.get("AGENT_FILE_ROOT", "data/agent_files")).resolve()
_ALLOWED_FILE_HOSTS = frozenset(
    host.strip().lower()
    for host in os.environ.get("AGENT_FILE_ALLOWED_HOSTS", "").split(",")
    if host.strip()
)


def build_tool_schemas(
    capabilities: BotGroupCapabilities, *, allow_admin_tools: bool = False
) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    definitions = [
        ("get_group_info", "读取当前群信息", {}),
        ("get_group_member", "读取群成员角色和头衔", {"user_id": {"type": "integer"}}),
        ("list_group_members", "读取群成员列表", {}),
        (
            "search_group_memory",
            "搜索当前群已沉淀的记忆",
            {"query": {"type": "string", "minLength": 1, "maxLength": 120}},
        ),
        ("get_person_profile", "读取群内人物画像", {"user_id": {"type": "integer"}}),
        (
            "get_recent_messages",
            "读取最近群聊消息摘要",
            {"limit": {"type": "integer", "minimum": 1, "maximum": 40}},
        ),
        ("get_group_activity", "读取群聊活跃度", {}),
    ]
    action_for_tool = {
        "get_group_info": "get_group_info",
        "get_group_member": "get_group_member_info",
        "list_group_members": "get_group_member_list",
    }
    for name, description, properties in definitions:
        action = action_for_tool.get(name)
        if action and not capabilities.has(action):
            continue
        required = (
            list(properties)
            if name in {"get_group_member", "search_group_memory", "get_person_profile"}
            else []
        )
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        **({"required": required} if required else {}),
                        "additionalProperties": False,
                    },
                },
            }
        )
    if capabilities.has("send_group_msg"):
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "send_text",
                    "description": "发送一条群文本消息",
                    "parameters": {
                        "type": "object",
                        "properties": {"text": {"type": "string", "maxLength": 1500}},
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                },
            }
        )
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "send_image",
                    "description": "发送群图片",
                    "parameters": {
                        "type": "object",
                        "properties": {"file": {"type": "string"}},
                        "required": ["file"],
                        "additionalProperties": False,
                    },
                },
            }
        )
    if capabilities.has("send_group_forward_msg"):
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "send_forward",
                    "description": "发送合并转发消息",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "messages": {
                                "type": "array",
                                "maxItems": 20,
                                "items": {"type": "object"},
                            }
                        },
                        "required": ["messages"],
                        "additionalProperties": False,
                    },
                },
            }
        )
    if capabilities.can_manage and allow_admin_tools:
        if capabilities.has("set_group_ban"):
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "mute_member",
                        "description": "禁言群成员",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "user_id": {"type": "integer"},
                                "duration": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 2592000,
                                },
                            },
                            "required": ["user_id", "duration"],
                            "additionalProperties": False,
                        },
                    },
                }
            )
        if capabilities.has("send_group_notice") or capabilities.has(
            "_send_group_notice"
        ):
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "create_group_announcement",
                        "description": "创建群公告",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string", "maxLength": 1000}
                            },
                            "required": ["content"],
                            "additionalProperties": False,
                        },
                    },
                }
            )
    if capabilities.has("upload_group_file"):
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "send_file",
                    "description": "发送群文件或文档",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string"},
                            "name": {"type": "string", "maxLength": 128},
                        },
                        "required": ["file", "name"],
                        "additionalProperties": False,
                    },
                },
            }
        )
    return tools


def _jsonable(value: object) -> object:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


async def _audit(
    session: Any,
    group_id: int,
    actor_user_id: int | None,
    name: str,
    args: dict[str, Any],
    result: str,
    detail: str = "",
) -> None:
    if session is None:
        return
    session.add(
        AgentAudit(
            group_id=group_id,
            actor_user_id=actor_user_id,
            tool_name=name,
            arguments={
                k: _jsonable(v) for k, v in args.items() if k not in {"file", "url"}
            },
            result=result,
            detail=detail[:2000],
        )
    )
    await session.flush()


_READ_ACTIONS = {
    "get_group_info": "get_group_info",
    "get_group_member": "get_group_member_info",
    "list_group_members": "get_group_member_list",
}
_ADMIN_TOOLS = {"mute_member", "create_group_announcement"}
_DEFAULT_ADMIN_ALLOWLIST = frozenset(_ADMIN_TOOLS)


async def _check_tool_policy(
    session: Any,
    group_id: int,
    name: str,
    capabilities: BotGroupCapabilities,
    *,
    bot: Any,
    actor_user_id: int | None,
) -> None:
    required = _READ_ACTIONS.get(name)
    if required and not capabilities.has(required):
        raise PermissionError("当前 OneBot 不支持该读取操作")
    if name in {"send_text", "send_forward", "send_image"} and not capabilities.has(
        "send_group_msg"
    ):
        raise PermissionError("当前 OneBot 不支持群消息发送")
    if name == "send_file" and not capabilities.has("upload_group_file"):
        raise PermissionError("当前 OneBot 不支持群文件上传")
    if name in _ADMIN_TOOLS and not capabilities.can_manage:
        raise PermissionError("机器人没有群管理权限")
    if name in _ADMIN_TOOLS and (
        actor_user_id is None
        or not await user_can_manage_group(bot, group_id, actor_user_id)
    ):
        raise PermissionError("调用者没有群管理权限")
    if name not in _ADMIN_TOOLS or session is None:
        return
    config = await session.get(GroupAgentConfig, group_id)
    if config is None:
        raise PermissionError("群聊 Agent 配置不存在")
    allowlist = set(config.tool_allowlist or []) or set(_DEFAULT_ADMIN_ALLOWLIST)
    if name not in allowlist:
        raise PermissionError("该管理工具未加入 Agent 白名单")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if config.tool_day != today:
        config.tool_day = today
        config.admin_tool_count = 0
    if config.admin_tool_count >= max(int(config.admin_tool_daily_limit), 1):
        raise PermissionError("Agent 今日管理操作配额已用尽")
    config.admin_tool_count += 1
    await session.flush()


def _check_local_path(path: Path) -> Path:
    path = path.expanduser().resolve()
    if _FILE_ROOT not in path.parents and path != _FILE_ROOT:
        raise PermissionError("本地文件必须位于 Agent 文件目录")
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("文件不存在或超过大小限制")
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
        raise PermissionError("远程文件域名不在白名单")
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:  # type: ignore[reportArgumentType]
        response = await client.get(file_ref)
        response.raise_for_status()
        if len(response.content) > MAX_FILE_BYTES:
            raise ValueError("文件超过大小限制")
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=Path(parsed.path).suffix or ".download"
        ) as handle:
            handle.write(response.content)
            return Path(handle.name), content_type or None


async def execute_tool(
    name: str,
    args: dict[str, Any],
    *,
    bot: Any,
    group_id: int,
    actor_user_id: int | None = None,
    session: Any = None,
    capabilities: BotGroupCapabilities,
) -> dict[str, Any]:
    """执行单个工具；每次调用都重新执行能力和配额校验。"""

    try:
        capabilities = await probe_group_capabilities(bot, group_id, refresh=True)
        await _check_tool_policy(
            session,
            group_id,
            name,
            capabilities,
            bot=bot,
            actor_user_id=actor_user_id,
        )
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if name == "get_group_info":
            result = await bot.call_api("get_group_info", group_id=group_id)
        elif name == "get_group_member":
            result = await bot.call_api(
                "get_group_member_info", group_id=group_id, user_id=int(args["user_id"])
            )
        elif name == "list_group_members":
            result = await bot.call_api("get_group_member_list", group_id=group_id)
        elif name == "get_person_profile":
            stmt = (
                select(AgentMemory)
                .where(
                    AgentMemory.group_id == group_id,
                    AgentMemory.subject_user_id == int(args["user_id"]),
                    AgentMemory.memory_type == "profile",
                    AgentMemory.visibility.in_(("group", "public")),
                    (
                        AgentMemory.expires_at.is_(None)
                        | (AgentMemory.expires_at >= now)
                    ),
                )
                .limit(10)
            )
            rows = (
                (await session.execute(stmt)).scalars().all()
                if session is not None
                else []
            )
            result = [
                {
                    "key": row.memory_key,
                    "content": row.content,
                    "confidence": row.confidence,
                }
                for row in rows
            ]
        elif name == "get_recent_messages":
            limit = max(1, min(int(args.get("limit", 20)), 40))
            stmt = (
                select(GroupAgentMessage)
                .where(
                    GroupAgentMessage.group_id == group_id,
                    (
                        GroupAgentMessage.expires_at.is_(None)
                        | (GroupAgentMessage.expires_at >= now)
                    ),
                )
                .order_by(GroupAgentMessage.id.desc())
                .limit(limit)
            )
            rows = (
                (await session.execute(stmt)).scalars().all()
                if session is not None
                else []
            )
            result = [
                {
                    "user_id": row.user_id,
                    "name": row.sender_name,
                    "text": row.normalized_text,
                }
                for row in reversed(rows)
            ]
        elif name == "get_group_activity":
            stmt = (
                select(GroupAgentMessage)
                .where(
                    GroupAgentMessage.group_id == group_id,
                    (
                        GroupAgentMessage.expires_at.is_(None)
                        | (GroupAgentMessage.expires_at >= now)
                    ),
                )
                .order_by(GroupAgentMessage.id.desc())
                .limit(60)
            )
            rows = (
                (await session.execute(stmt)).scalars().all()
                if session is not None
                else []
            )
            result = {
                "messages_60": len(rows),
                "participants_60": len({row.user_id for row in rows}),
            }
        elif name == "search_group_memory":
            query = str(args.get("query", "")).strip()
            if not query:
                raise ValueError("query 不能为空")
            stmt = (
                select(AgentMemory)
                .where(
                    AgentMemory.group_id == group_id,
                    AgentMemory.visibility.in_(("group", "public")),
                    AgentMemory.content.contains(query),
                    (
                        AgentMemory.expires_at.is_(None)
                        | (AgentMemory.expires_at >= now)
                    ),
                )
                .limit(10)
            )
            rows = (
                (await session.execute(stmt)).scalars().all()
                if session is not None
                else []
            )
            result = [
                {
                    "type": row.memory_type,
                    "key": row.memory_key,
                    "content": row.content,
                    "confidence": row.confidence,
                }
                for row in rows
            ]
        elif name == "send_text":
            text = str(args.get("text", "")).strip()
            if not text or len(text) > 1500:
                raise ValueError("文本不能为空且长度不得超过 1500")
            await bot.call_api(
                "send_group_msg", group_id=group_id, message=MessageSegment.text(text)
            )
            result = {"sent": True}
        elif name == "send_forward":
            messages = args.get("messages")
            if (
                not isinstance(messages, list)
                or not messages
                or len(messages) > MAX_FORWARD_SEND
                or any(not isinstance(item, dict) for item in messages)
            ):
                raise ValueError("messages 必须是 1-20 个对象节点")
            if (
                len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
                > MAX_FORWARD_BYTES
            ):
                raise ValueError("转发内容超过大小限制")
            result = await bot.call_api(
                "send_group_forward_msg", group_id=group_id, messages=messages
            )
        elif name == "send_image":
            file_ref = str(args.get("file", "")).strip()
            temporary: Path | None = None
            if file_ref.startswith(("http://", "https://")):
                temporary, content_type = await _download_allowed_file(file_ref)
                try:
                    if content_type and not content_type.startswith("image/"):
                        raise ValueError("远程文件不是图片")
                    path = _check_downloaded_path(temporary)
                    mime = content_type or mimetypes.guess_type(path.name)[0] or ""
                    if not mime.startswith("image/"):
                        raise ValueError("远程文件不是图片")
                except Exception:
                    temporary.unlink(missing_ok=True)
                    raise
            else:
                path = _validate_image_path(Path(file_ref))
            try:
                await bot.call_api(
                    "send_group_msg",
                    group_id=group_id,
                    message=MessageSegment.image(str(path)),
                )
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
            result = {"sent": True}
        elif name == "send_file":
            file_ref = str(args.get("file", ""))
            temporary: Path | None = None
            if file_ref.startswith(("http://", "https://")):
                temporary, _content_type = await _download_allowed_file(file_ref)
                try:
                    path = _check_downloaded_path(temporary)
                except Exception:
                    temporary.unlink(missing_ok=True)
                    raise
            else:
                path = _check_local_path(Path(file_ref))
            try:
                result = await bot.call_api(
                    "upload_group_file",
                    group_id=group_id,
                    file=str(path),
                    name=str(args["name"])[:128],
                )
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
        elif name == "create_group_announcement":
            action = (
                "send_group_notice"
                if capabilities.has("send_group_notice")
                else "_send_group_notice"
            )
            result = await bot.call_api(
                action, group_id=group_id, content=str(args["content"])[:1000]
            )
        elif name == "mute_member":
            user_id = int(args["user_id"])
            if not await target_can_be_muted(bot, group_id, user_id, capabilities.role):
                raise PermissionError("机器人无权禁言该成员")
            result = await bot.call_api(
                "set_group_ban",
                group_id=group_id,
                user_id=user_id,
                duration=max(1, min(int(args["duration"]), 2592000)),
            )
        else:
            raise ValueError(f"未知工具: {name}")
        await _audit(session, group_id, actor_user_id, name, args, "success")
        return {"ok": True, "result": result}
    except Exception as exc:  # noqa: BLE001
        await _audit(session, group_id, actor_user_id, name, args, "error", str(exc))
        return {"ok": False, "error": str(exc)}


__all__ = ["MAX_FORWARD_SEND", "MAX_TOOL_ROUNDS", "build_tool_schemas", "execute_tool"]
