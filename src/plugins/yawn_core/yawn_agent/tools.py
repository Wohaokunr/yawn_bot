# ruff: noqa: E501,F401,I001,TID252,PLR0912,PLR0913,PLR0917,C901,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100,PLR0915,PLR2004
"""群聊 Agent 的严格工具 schema 与 OneBot 执行器。"""

from __future__ import annotations

import contextlib
import json
import mimetypes
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from nonebot import logger
from nonebot.adapters.onebot.v11 import MessageSegment
from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError

from ..data_models.agent_audit import AgentAudit
from ..data_models.agent_memory import AgentMemory, AgentPrivacy, AgentRelation
from ..data_models.group_agent_config import GroupAgentConfig
from ..data_models.group_agent_message import GroupAgentMessage
from ..data_models.user_group import UserGroup
from .capabilities import (
    BotGroupCapabilities,
    COMMON_MESSAGE_SEGMENTS,
    MessageSegmentCapabilities,
    probe_group_capabilities,
    target_can_be_muted,
    user_can_manage_group,
)
from .context import now_beijing
from .log import dbg
from .memory import effective_relation_confidence, normalize_relation_type, rank_memories
from .outbound import (
    MAX_FORWARD_NODES,
    MAX_OUTBOUND_SEGMENTS,
    send_forward_message,
    send_outbound_message,
)
from .reactions import search_reactions
from .tool_registry import (
    ADMIN_TOOLS as _ADMIN_TOOLS,
    CONTROLLED_TOOLS as _CONTROLLED_TOOLS,
    CRITICAL_TOOLS as _CRITICAL_TOOLS,
    MESSAGE_SEGMENT_FIELDS as _MESSAGE_SEGMENT_FIELDS,
    MESSAGE_SEGMENT_SCHEMA as _MESSAGE_SEGMENT_SCHEMA,
    MESSAGE_SEND_TOOLS as _MESSAGE_SEND_TOOLS,
    PRIVILEGED_TOOLS as _PRIVILEGED_TOOLS,
    TOOL_BY_NAME as _TOOL_BY_NAME,
    TOOL_DEFINITIONS as _TOOL_DEFINITIONS,
    TOOL_PERMISSION_CRITICAL,
    TOOL_PERMISSION_MESSAGE_SEND,
    TOOL_PERMISSION_PRIVILEGED,
    TOOL_PERMISSION_RANK as _TOOL_PERMISSION_RANK,
    TOOL_PERMISSION_READ,
    TOOL_PERMISSION_STATE_WRITE,
    ToolDefinition as _ToolDefinition,
)
from .tool_router import (
    MAX_TOOL_ROUNDS,
    dialogue_tool_round_limit,
    rank_discoverable_tools,
    select_dialogue_message_segment_types,
    select_dialogue_tool_names,
)

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

def _max_tool_permission_level(
    *, allow_admin_tools: bool, max_permission_level: str | None
) -> str:
    if max_permission_level is None:
        return (
            TOOL_PERMISSION_CRITICAL
            if allow_admin_tools
            else TOOL_PERMISSION_MESSAGE_SEND
        )
    normalized = str(max_permission_level).strip().lower()
    if normalized not in _TOOL_PERMISSION_RANK:
        raise ValueError(f"未知工具权限等级: {max_permission_level}")
    return normalized


def _tool_exposure_reason(  # noqa: PLR0911
    definition: _ToolDefinition,
    capabilities: BotGroupCapabilities,
    *,
    allow_admin_tools: bool,
    max_permission_level: str,
    privileged_allowlist: set[str] | None,
) -> tuple[bool, str]:
    if _TOOL_PERMISSION_RANK[definition.permission_level] > _TOOL_PERMISSION_RANK[
        max_permission_level
    ]:
        return False, "permission_level"
    if definition.actions and not any(
        capabilities.has(action) for action in definition.actions
    ):
        return False, "onebot_action"
    if definition.admin and not capabilities.can_manage:
        return False, "bot_not_admin"
    if definition.owner_only and capabilities.role != "owner":
        return False, "bot_not_owner"
    if definition.permission_level in {
        TOOL_PERMISSION_PRIVILEGED,
        TOOL_PERMISSION_CRITICAL,
    } and not allow_admin_tools:
        return False, "actor_not_admin"
    if (
        definition.permission_level
        in {TOOL_PERMISSION_PRIVILEGED, TOOL_PERMISSION_CRITICAL}
        and privileged_allowlist is not None
        and definition.name not in privileged_allowlist
    ):
        return False, "not_allowlisted"
    return True, "exposed"


def build_tool_schemas(
    capabilities: BotGroupCapabilities,
    *,
    allow_admin_tools: bool = False,
    segment_capabilities: MessageSegmentCapabilities | None = None,
    max_permission_level: str | None = None,
    privileged_allowlist: set[str] | None = None,
    include_names: frozenset[str] | set[str] | None = None,
    message_segment_types: frozenset[str] | set[str] | None = None,
) -> list[dict[str, Any]]:
    resolved_level = _max_tool_permission_level(
        allow_admin_tools=allow_admin_tools,
        max_permission_level=max_permission_level,
    )
    tools: list[dict[str, Any]] = []
    for definition in _TOOL_DEFINITIONS:
        if include_names is not None and definition.name not in include_names:
            continue
        exposed, reason = _tool_exposure_reason(
            definition,
            capabilities,
            allow_admin_tools=allow_admin_tools,
            max_permission_level=resolved_level,
            privileged_allowlist=privileged_allowlist,
        )
        if not exposed:
            dbg(f"工具 schema: {definition.name} 未暴露 reason={reason}")
            continue
        properties = definition.properties
        if definition.name == "send_message":
            properties = json.loads(json.dumps(definition.properties))
            exposed = (
                segment_capabilities.exposed_types
                if segment_capabilities is not None
                else COMMON_MESSAGE_SEGMENTS
            )
            if message_segment_types is not None:
                exposed = frozenset(exposed) & frozenset(message_segment_types)
            allowed_types = [
                item
                for item in _MESSAGE_SEGMENT_SCHEMA["properties"]["type"]["enum"]
                if item in exposed
            ]
            item_properties = properties["segments"]["items"]["properties"]
            item_properties["type"]["enum"] = allowed_types
            allowed_fields = {"type"}
            for segment_type in allowed_types:
                allowed_fields.update(_MESSAGE_SEGMENT_FIELDS.get(segment_type, ()))
            properties["segments"]["items"]["properties"] = {
                key: value
                for key, value in item_properties.items()
                if key in allowed_fields
            }
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": definition.name,
                    "description": definition.description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        **(
                            {"required": list(definition.required)}
                            if definition.required
                            else {}
                        ),
                        "additionalProperties": False,
                    },
                },
            }
        )
    return tools


def tool_permission_snapshot(
    capabilities: BotGroupCapabilities,
    *,
    allow_admin_tools: bool = False,
    max_permission_level: str | None = None,
    privileged_allowlist: set[str] | None = None,
) -> list[dict[str, Any]]:
    """返回不含 schema 参数的最小权限矩阵，供调试/WebUI 使用。"""

    resolved_level = _max_tool_permission_level(
        allow_admin_tools=allow_admin_tools,
        max_permission_level=max_permission_level,
    )
    rows: list[dict[str, Any]] = []
    for definition in _TOOL_DEFINITIONS:
        exposed, reason = _tool_exposure_reason(
            definition,
            capabilities,
            allow_admin_tools=allow_admin_tools,
            max_permission_level=resolved_level,
            privileged_allowlist=privileged_allowlist,
        )
        rows.append(
            {
                "name": definition.name,
                "permissionLevel": definition.permission_level,
                "exposed": exposed,
                "reason": reason,
                "actions": list(definition.actions),
            }
        )
    return rows


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
    """Keep image handles as resolver-only metadata, never as prompt-visible URLs."""

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
        # file is preferred because it can be resolved through OneBot without exposing
        # a signed CDN URL to the language model. URL remains internal fallback only.
        file_handle = data.get("file") or data.get("file_id")
        if file_handle is not None:
            ref["file"] = file_handle
        elif data.get("url") is not None:
            ref["url"] = data["url"]
        if len(ref) > 2:
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


async def _discover_tools_for_actor(
    *,
    query: str,
    family: str | None,
    limit: int,
    bot: Any,
    group_id: int,
    actor_user_id: int | None,
    session: Any,
    capabilities: BotGroupCapabilities,
) -> dict[str, Any]:
    """Discover eligible tools without preloading their schemas.

    Privileged discovery is resolved lazily here: ordinary dialogue never pays a
    role-probe cost, while an administrator asking for a controlled capability
    gets a live bot-role probe before those tools become discoverable.
    """

    allow_admin_tools = bool(
        actor_user_id is not None
        and await user_can_manage_group(bot, group_id, actor_user_id)
    )
    effective_capabilities = capabilities
    if allow_admin_tools:
        effective_capabilities = await probe_group_capabilities(bot, group_id)

    allowlist: set[str] = set()
    if session is not None:
        config = await session.get(GroupAgentConfig, group_id)
        if config is not None:
            allowlist = set(config.tool_allowlist or [])
    resolved_level = _max_tool_permission_level(
        allow_admin_tools=allow_admin_tools,
        max_permission_level=None,
    )
    candidates: list[_ToolDefinition] = []
    for definition in _TOOL_DEFINITIONS:
        exposed, _reason = _tool_exposure_reason(
            definition,
            effective_capabilities,
            allow_admin_tools=allow_admin_tools,
            max_permission_level=resolved_level,
            privileged_allowlist=allowlist,
        )
        if exposed and definition.discoverable:
            candidates.append(definition)

    normalized_query = str(query or "").strip().casefold()
    catalog_request = not family and normalized_query in {
        "*",
        "全部",
        "全部工具",
        "所有工具",
        "工具目录",
        "工具包",
        "全部工具包",
    }
    matches = (
        []
        if catalog_request
        else rank_discoverable_tools(
            query,
            candidates,
            family=family,
            limit=limit,
        )
    )
    matched_families = (
        sorted({item.family for item in candidates})
        if catalog_request
        else sorted({item.family for item in matches})
    )
    toolpacks: list[dict[str, Any]] = []
    for pack_name in matched_families:
        members = [item for item in candidates if item.family == pack_name]
        if not members:
            continue
        toolpacks.append(
            {
                "name": pack_name,
                "count": len(members),
                "summary": "；".join(item.description for item in members[:2])[:240],
                "load_with": {"family": pack_name},
            }
        )
    return {
        "mode": "catalog" if catalog_request else ("toolpack" if family else "search"),
        "tools": [
            {
                "name": item.name,
                "description": item.description,
                "family": item.family,
                "permission": item.permission_level,
            }
            for item in matches
        ],
        "toolpacks": toolpacks,
        "count": len(matches),
    }


async def _audit(
    session: Any,
    group_id: int,
    actor_user_id: int | None,
    name: str,
    args: dict[str, Any],
    result: str,
    detail: str = "",
) -> None:
    """审计是尽力而为：失败不能毒化会话，更不能穿透 execute_tool。"""

    if session is None:
        return
    try:
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
    except SQLAlchemyError:
        dbg(f"群 {group_id} 工具审计写入失败(已抑制): tool={name}")
        with contextlib.suppress(SQLAlchemyError):
            await session.rollback()
    else:
        dbg(f"群 {group_id} 工具审计已写入: tool={name} result={result}")


async def _check_tool_policy(
    session: Any,
    group_id: int,
    name: str,
    capabilities: BotGroupCapabilities,
    *,
    bot: Any,
    actor_user_id: int | None,
) -> None:
    definition = _TOOL_BY_NAME.get(name)
    if definition is None:
        raise ValueError(f"未知工具: {name}")
    effective_capabilities = capabilities
    if name in _CONTROLLED_TOOLS:
        # Controlled actions re-check the bot role at execution time as well as
        # discovery time; a stale discovery can never grant a lasting privilege.
        effective_capabilities = await probe_group_capabilities(
            bot, group_id, refresh=True
        )
    if definition.admin and not effective_capabilities.can_manage:
        dbg(f"群 {group_id} 工具策略拒绝 {name}: 机器人没有群管理权限")
        raise PermissionError("机器人没有群管理权限")
    if definition.owner_only and effective_capabilities.role != "owner":
        dbg(f"群 {group_id} 工具策略拒绝 {name}: 机器人不是群主")
        raise PermissionError("该工具需要机器人是群主")
    if definition.actions and not any(
        effective_capabilities.has(action) for action in definition.actions
    ):
        dbg(f"群 {group_id} 工具策略拒绝 {name}: OneBot 不支持 {definition.actions}")
        raise PermissionError("当前 OneBot 不支持该操作")
    if (
        definition.permission_level == TOOL_PERMISSION_STATE_WRITE
        and actor_user_id is None
    ):
        dbg(f"群 {group_id} 工具策略拒绝 {name}: 状态写入缺少真实调用者")
        raise PermissionError("状态写入工具需要明确的群成员调用者")
    if definition.permission_level == TOOL_PERMISSION_STATE_WRITE:
        if session is None:
            raise PermissionError("状态写入工具需要数据库会话")
        if actor_user_id is None:
            raise PermissionError("状态写入工具需要明确的群成员调用者")
        actor_member = await session.scalar(
            select(UserGroup.user_id).where(
                UserGroup.group_id == group_id,
                UserGroup.user_id == actor_user_id,
            )
        )
        if actor_member is None:
            dbg(f"群 {group_id} 工具策略拒绝 {name}: 调用者 {actor_user_id} 不是已知群成员")
            raise PermissionError("状态写入工具仅允许当前群成员触发")
    if name in _CONTROLLED_TOOLS and (
        actor_user_id is None
        or not await user_can_manage_group(bot, group_id, actor_user_id)
    ):
        dbg(f"群 {group_id} 工具策略拒绝 {name}: 调用者 {actor_user_id} 没有群管理权限")
        raise PermissionError(
            "调用者没有群管理权限"
            if name in _ADMIN_TOOLS
            else "特权工具仅允许群主或管理员触发"
        )
    if name not in _CONTROLLED_TOOLS:
        return
    if session is None:
        # 没有会话就无法校验白名单和配额，特权工具必须拒绝。
        dbg(f"群 {group_id} 工具策略拒绝 {name}: 缺少数据库会话")
        raise PermissionError("特权工具需要数据库会话")
    config = await session.get(GroupAgentConfig, group_id)
    if config is None:
        dbg(f"群 {group_id} 工具策略拒绝 {name}: Agent 配置不存在")
        raise PermissionError("群聊 Agent 配置不存在")
    # 空白名单即全部禁用。send_file 在 P5 后也属于显式特权工具，默认配置
    # 不包含它，因此升级不会自动获得群文件发送能力。
    allowlist = set(config.tool_allowlist or [])
    if name not in allowlist:
        dbg(f"群 {group_id} 工具策略拒绝 {name}: 不在白名单 {sorted(allowlist)}")
        raise PermissionError("该特权工具未加入 Agent 白名单")
    today = now_beijing().strftime("%Y-%m-%d")
    if config.tool_day != today:
        config.tool_day = today
        config.admin_tool_count = 0
        config.critical_tool_count = 0
        dbg(f"群 {group_id} 管理工具配额计数按新的一天重置")
    if name in _CRITICAL_TOOLS:
        used = int(config.critical_tool_count or 0)
        daily_limit = max(int(config.critical_tool_daily_limit or 0), 1)
        quota_label = "高风险工具"
    else:
        used = int(config.admin_tool_count or 0)
        daily_limit = max(int(config.admin_tool_daily_limit or 0), 1)
        quota_label = "管理工具"
    if used >= daily_limit:
        dbg(
            f"群 {group_id} 工具策略拒绝 {name}: 今日{quota_label}配额已用尽"
            f"({used}/{daily_limit})"
        )
        raise PermissionError(f"Agent 今日{quota_label}配额已用尽")
    dbg(f"群 {group_id} 工具策略通过: {name}")


async def _consume_admin_quota(session: Any, group_id: int, name: str) -> None:
    """特权工具配额只在成功后消耗，失败尝试不计入。"""

    if name not in _CONTROLLED_TOOLS or session is None:
        return
    config = await session.get(GroupAgentConfig, group_id)
    if config is None:
        return
    today = now_beijing().strftime("%Y-%m-%d")
    if config.tool_day != today:
        config.tool_day = today
        config.admin_tool_count = 0
        config.critical_tool_count = 0
    if name in _CRITICAL_TOOLS:
        config.critical_tool_count += 1
        dbg(
            f"群 {group_id} 消耗高风险工具配额: {name} "
            f"(今日已用 {config.critical_tool_count}/{config.critical_tool_daily_limit})"
        )
    else:
        config.admin_tool_count += 1
        dbg(
            f"群 {group_id} 消耗特权工具配额: {name} "
            f"(今日已用 {config.admin_tool_count}/{config.admin_tool_daily_limit})"
        )
    await session.flush()


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
    """执行单个工具；高权限工具实时重探测，普通工具不依赖角色探测。"""

    dbg(
        f"群 {group_id} 执行工具: name={name!r} args={json.dumps(args, ensure_ascii=False)} "
        f"actor={actor_user_id}"
    )
    try:
        definition = _TOOL_BY_NAME.get(name)
        if definition is not None and (
            definition.admin or definition.owner_only or name in _CONTROLLED_TOOLS
        ):
            # 管理/特权工具必须使用实时 Bot 角色；失败时 probe 自身按 member
            # fail-closed。普通 read/state/message-send 工具不需要为了执行先做
            # get_group_member_info，避免协议探测成为消息处理关键路径。
            capabilities = await probe_group_capabilities(bot, group_id, refresh=True)
        await _check_tool_policy(
            session,
            group_id,
            name,
            capabilities,
            bot=bot,
            actor_user_id=actor_user_id,
        )
        now = now_beijing()
        if name == "discover_tools":
            query = str(args.get("query") or "").strip()
            family = str(args.get("family") or "").strip() or None
            if not query and not family:
                raise ValueError("query 和 family 至少提供一个")
            if not query:
                query = str(family)
            limit = _tool_result_limit(
                args, default=(12 if family else 5), maximum=12
            )
            result = await _discover_tools_for_actor(
                query=query,
                family=family,
                limit=limit,
                bot=bot,
                group_id=group_id,
                actor_user_id=actor_user_id,
                session=session,
                capabilities=capabilities,
            )
        elif name == "get_group_info":
            result = _compact_group_info(
                await bot.call_api("get_group_info", group_id=group_id)
            )
        elif name == "get_message":
            target_message_id = int(args["message_id"])
            await _require_known_message(session, group_id, target_message_id)
            raw_message = await bot.call_api("get_msg", message_id=target_message_id)
            if (
                isinstance(raw_message, dict)
                and raw_message.get("group_id") is not None
                and int(raw_message["group_id"]) != int(group_id)
            ):
                raise PermissionError("消息不属于当前群")
            result = _compact_onebot_message(raw_message)
        elif name == "get_recent_group_messages":
            try:
                count = int(args.get("count") or 10)
            except (TypeError, ValueError) as exc:
                raise ValueError("count 必须是整数") from exc
            if count < 1 or count > 30:
                raise ValueError("count 必须在 1~30 之间")
            raw_history = await bot.call_api(
                "get_group_msg_history", group_id=group_id, count=count
            )
            messages = _payload_list(raw_history, "messages", "message_list", "items")
            if (
                not messages
                and raw_history not in ([], {"messages": []})
                and not isinstance(raw_history, (list, dict))
            ):
                raise ValueError("群历史消息响应格式错误")
            compact_messages = [
                _compact_onebot_message(item)
                for item in messages[-count:]
                if isinstance(item, dict)
            ]
            result = {"items": compact_messages, "count": len(compact_messages)}
        elif name == "list_group_notices":
            raw_notices = await bot.call_api("_get_group_notice", group_id=group_id)
            notices = _payload_list(raw_notices, "notices", "items")
            if isinstance(raw_notices, dict) and not notices and any(
                key in raw_notices for key in ("notice_id", "content", "message")
            ):
                notices = [raw_notices]
            result = [
                _compact_notice(item)
                for item in notices[:20]
                if isinstance(item, dict)
            ]
        elif name == "list_essence_messages":
            raw_essence = await bot.call_api("get_essence_msg_list", group_id=group_id)
            essence_items = _payload_list(raw_essence, "items", "messages", "list")
            result = [
                _compact_essence(item)
                for item in essence_items[:30]
                if isinstance(item, dict)
            ]
        elif name == "list_muted_members":
            limit = _tool_result_limit(args, default=30, maximum=50)
            raw_muted = await bot.call_api("get_group_shut_list", group_id=group_id)
            muted_items = _payload_list(raw_muted, "items", "members", "list")
            compact_muted: list[dict[str, Any]] = []
            for item in muted_items[:limit]:
                if not isinstance(item, dict):
                    continue
                member = _compact_group_member(item)
                if item.get("shut_up_timestamp") is not None:
                    member["shut_up_timestamp"] = item.get("shut_up_timestamp")
                compact_muted.append(member)
            result = {"items": compact_muted, "count": len(compact_muted)}
        elif name == "get_group_honor":
            honor_type = str(args.get("type") or "all")
            raw_honor = await bot.call_api(
                "get_group_honor_info", group_id=group_id, type=honor_type
            )
            if not isinstance(raw_honor, dict):
                raise ValueError("群荣誉响应格式错误")
            honor_result: dict[str, Any] = {"group_id": raw_honor.get("group_id")}
            for key in (
                "current_talkative",
                "talkative_list",
                "performer_list",
                "legend_list",
                "strong_newbie_list",
                "emotion_list",
            ):
                value = raw_honor.get(key)
                if isinstance(value, dict):
                    honor_result[key] = _compact_group_member(value)
                elif isinstance(value, list):
                    honor_result[key] = [
                        _compact_group_member(item)
                        for item in value[:20]
                        if isinstance(item, dict)
                    ]
            result = {
                key: value
                for key, value in honor_result.items()
                if value not in (None, [], {})
            }
        elif name == "list_group_files":
            limit = _tool_result_limit(args, default=20, maximum=30)
            folder_id = str(args.get("folder_id") or "").strip()
            if folder_id:
                raw_files = await bot.call_api(
                    "get_group_files_by_folder",
                    group_id=group_id,
                    folder_id=folder_id,
                )
            else:
                raw_files = await bot.call_api("get_group_root_files", group_id=group_id)
            files = _payload_list(raw_files, "files", "items", "file_list")
            folders = _payload_list(raw_files, "folders", "folder_list")
            result = {
                "files": [
                    _compact_group_file(item)
                    for item in files[:limit]
                    if isinstance(item, dict)
                ],
                "folders": [
                    _compact_group_folder(item)
                    for item in folders[:limit]
                    if isinstance(item, dict)
                ],
            }
        elif name == "get_group_file_link":
            file_id = str(args.get("file_id") or "").strip()
            if not file_id:
                raise ValueError("file_id 不能为空")
            raw_link = await bot.call_api(
                "get_group_file_url",
                group_id=group_id,
                file_id=file_id,
                busid=int(args["busid"]),
            )
            if isinstance(raw_link, str):
                url = raw_link
            elif isinstance(raw_link, dict):
                url = str(raw_link.get("url") or raw_link.get("download_url") or "")
            else:
                url = ""
            if not url:
                raise ValueError("群文件链接响应格式错误")
            result = {"file_id": file_id, "url": url[:2048]}
        elif name == "get_group_member":
            result = _compact_group_member(
                await bot.call_api(
                    "get_group_member_info",
                    group_id=group_id,
                    user_id=int(args["user_id"]),
                )
            )
        elif name == "list_group_members":
            members = await bot.call_api("get_group_member_list", group_id=group_id)
            if not isinstance(members, list):
                raise ValueError("群成员列表响应格式错误")
            limit = _tool_result_limit(
                args, default=DEFAULT_MEMBER_TOOL_LIMIT, maximum=MAX_MEMBER_TOOL_LIMIT
            )
            compact_members = [
                _compact_group_member(member)
                for member in members
                if isinstance(member, dict)
            ]
            result = {
                "items": compact_members[:limit],
                "total": len(members),
                "truncated": len(members) > limit,
            }
        elif name == "get_person_profile":
            subject_id = int(args["user_id"])
            limit = _tool_result_limit(
                args, default=DEFAULT_PROFILE_TOOL_LIMIT, maximum=MAX_PROFILE_TOOL_LIMIT
            )
            privacy = (
                await session.get(AgentPrivacy, (group_id, subject_id))
                if session is not None
                else None
            )
            # 隐私退出在读路径同样生效：不得再输出其画像。
            rows: list[Any] = []
            if privacy is not None and privacy.opted_out:
                dbg(
                    f"群 {group_id} get_person_profile: 用户 {subject_id} 已隐私退出,返回空画像"
                )
            if privacy is None or not privacy.opted_out:
                stmt = (
                    select(AgentMemory)
                    .where(
                        AgentMemory.group_id == group_id,
                        AgentMemory.subject_user_id == subject_id,
                        AgentMemory.memory_type.in_(("core", "profile")),
                        AgentMemory.visibility.in_(("group", "public")),
                        (
                            AgentMemory.expires_at.is_(None)
                            | (AgentMemory.expires_at >= now)
                        ),
                    )
                    .limit(limit)
                )
                rows = (
                    (await session.execute(stmt)).scalars().all()
                    if session is not None
                    else []
                )
            result = [
                {
                    "key": row.memory_key,
                    "content": str(row.content or "")[:600],
                    "confidence": round(float(row.confidence or 0.0), 3),
                }
                for row in rows
            ]
        elif name == "search_group_memory":
            query = str(args.get("query", "")).strip()
            if not query:
                raise ValueError("query 不能为空")
            limit = _tool_result_limit(
                args, default=DEFAULT_MEMORY_TOOL_LIMIT, maximum=MAX_MEMORY_TOOL_LIMIT
            )
            stmt = (
                select(AgentMemory)
                .where(
                    AgentMemory.group_id == group_id,
                    AgentMemory.visibility.in_(("group", "public")),
                    # autoescape：查询来自用户原话，%/_ 不得充当通配符。
                    AgentMemory.content.contains(query, autoescape=True),
                    (
                        AgentMemory.expires_at.is_(None)
                        | (AgentMemory.expires_at >= now)
                    ),
                )
                # 先放宽到 30 条子串候选，再按查询词相关性与显著度重排，
                # 避免"碰巧先入库的低相关匹配"挤掉真正贴合查询的记忆。
                .limit(30)
            )
            rows = (
                (await session.execute(stmt)).scalars().all()
                if session is not None
                else []
            )
            opted_out = (
                set(
                    (
                        await session.execute(
                            select(AgentPrivacy.user_id).where(
                                AgentPrivacy.group_id == group_id,
                                AgentPrivacy.opted_out.is_(True),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if session is not None
                else set()
            )
            rows = [
                row
                for row in rows
                if int(row.subject_user_id or 0) not in opted_out
                and not opted_out.intersection(set(row.related_user_ids or []))
            ]
            rows = rank_memories(rows, [query], None, now, limit=limit)
            evidence_ids = list(
                dict.fromkeys(
                    int(message_id)
                    for row in rows
                    for message_id in list(row.evidence_message_ids or [])
                    if str(message_id).lstrip("-").isdigit()
                )
            )
            evidence_rows = (
                (
                    await session.execute(
                        select(GroupAgentMessage).where(
                            GroupAgentMessage.group_id == group_id,
                            GroupAgentMessage.message_id.in_(evidence_ids),
                        )
                    )
                )
                .scalars()
                .all()
                if session is not None and evidence_ids
                else []
            )
            evidence_by_message = {
                int(message.message_id): [
                    {
                        **dict(ref),
                        "source": "tool",
                        "source_message_id": int(message.message_id),
                    }
                    for ref in list(message.media_refs or [])
                    if isinstance(ref, dict) and str(ref.get("type") or "") == "image"
                ]
                for message in evidence_rows
            }
            result = []
            for row in rows:
                item: dict[str, Any] = {
                    "type": row.memory_type,
                    "key": row.memory_key,
                    "content": str(row.content or "")[:600],
                    "confidence": round(float(row.confidence or 0.0), 3),
                }
                media_refs = [
                    ref
                    for message_id in list(row.evidence_message_ids or [])
                    if str(message_id).lstrip("-").isdigit()
                    for ref in evidence_by_message.get(int(message_id), [])
                ]
                if media_refs:
                    item["media_types"] = ["image"] * len(media_refs)
                    item["_agent_media_refs"] = media_refs[:8]
                result.append(item)
        elif name == "list_user_relations":
            if session is None:
                raise PermissionError("关系查询需要数据库会话")
            subject_id = int(args["user_id"])
            limit = _tool_result_limit(
                args,
                default=DEFAULT_RELATION_TOOL_LIMIT,
                maximum=MAX_RELATION_TOOL_LIMIT,
            )
            member_names = {
                int(row_user_id): str(nickname or row_user_id)
                for row_user_id, nickname in (
                    await session.execute(
                        select(UserGroup.user_id, UserGroup.group_nickname).where(
                            UserGroup.group_id == group_id
                        )
                    )
                ).all()
            }
            opted_out = set(
                (
                    await session.execute(
                        select(AgentPrivacy.user_id).where(
                            AgentPrivacy.group_id == group_id,
                            AgentPrivacy.opted_out.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
            )
            rows = list(
                (
                    await session.execute(
                        select(AgentRelation)
                        .where(
                            AgentRelation.group_id == group_id,
                            AgentRelation.subject_user_id.not_in(opted_out),
                            AgentRelation.object_user_id.not_in(opted_out),
                            or_(
                                AgentRelation.subject_user_id == subject_id,
                                AgentRelation.object_user_id == subject_id,
                            ),
                        )
                        .order_by(AgentRelation.confidence.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            result = [
                {
                    "subject_user_id": int(row.subject_user_id),
                    "subject_name": member_names.get(int(row.subject_user_id)),
                    "object_user_id": int(row.object_user_id),
                    "object_name": member_names.get(int(row.object_user_id)),
                    "type": row.relation_type,
                    "note": row.note,
                    "effective_confidence": round(
                        effective_relation_confidence(
                            float(row.confidence or 0.0), row.last_seen_at, now
                        ),
                        3,
                    ),
                    "last_seen_days": max(
                        0,
                        int(
                            (now - (row.last_seen_at or now)).total_seconds() // 86400
                        ),
                    ),
                }
                for row in rows
            ]
            dbg(
                f"群 {group_id} list_user_relations: 成员 {subject_id} "
                f"隐私退出过滤={sorted(opted_out)} 返回 {len(rows)} 条"
            )
        elif name == "record_user_relation":
            if session is None:
                raise PermissionError("关系记录需要数据库会话")
            subject = int(args["subject_user_id"])
            target = int(args["object_user_id"])
            relation_type = normalize_relation_type(args.get("type"))
            note = str(args.get("note") or "").strip()[:200]
            if not relation_type:
                raise ValueError("关系类型不能为空")
            if subject == target:
                raise ValueError("关系两端不能是同一个人")
            member_ids = set(
                (
                    await session.execute(
                        select(UserGroup.user_id).where(
                            UserGroup.group_id == group_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            # 双方必须是本群真实成员，防止模型把幻觉人物写进关系图。
            if subject not in member_ids or target not in member_ids:
                raise ValueError("关系双方都必须是本群成员")
            opted_out = set(
                (
                    await session.execute(
                        select(AgentPrivacy.user_id).where(
                            AgentPrivacy.group_id == group_id,
                            AgentPrivacy.opted_out.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if subject in opted_out or target in opted_out:
                raise PermissionError("关系一方已隐私退出，不得记录")
            edge = await session.scalar(
                select(AgentRelation).where(
                    AgentRelation.group_id == group_id,
                    AgentRelation.subject_user_id == subject,
                    AgentRelation.object_user_id == target,
                    AgentRelation.relation_type == relation_type,
                )
            )
            if edge is not None and edge.source_kind == "manual":
                raise PermissionError("该关系由管理员维护，Agent 不能修改")
            if edge is None:
                session.add(
                    AgentRelation(
                        group_id=group_id,
                        subject_user_id=subject,
                        object_user_id=target,
                        relation_type=relation_type,
                        source_kind="agent",
                        note=note,
                        confidence=0.6,
                        evidence_count=1,
                        last_seen_at=now,
                    )
                )
                result = {"ok": True, "created": True, "type": relation_type}
            else:
                edge.evidence_count += 1
                if note and not str(edge.note or "").strip():
                    edge.note = note
                edge.last_seen_at = now
                result = {
                    "ok": True,
                    "created": False,
                    "type": relation_type,
                    "note": edge.note,
                }
            dbg(
                f"群 {group_id} record_user_relation: {subject} "
                f"—{relation_type}→ {target} note={note!r}"
            )
        elif name == "search_reactions":
            query = str(args.get("query") or "").strip()
            if not query:
                raise ValueError("query 不能为空")
            result = search_reactions(query, limit=int(args.get("limit") or 5))
        elif name == "react_to_message":
            target_message_id = int(args["message_id"])
            emoji_id = str(args.get("emoji_id") or "").strip()
            if not emoji_id.isdigit():
                raise ValueError("emoji_id 必须是数字字符串")
            await _require_known_message(session, group_id, target_message_id)
            await bot.call_api(
                "set_msg_emoji_like",
                message_id=target_message_id,
                emoji_id=emoji_id,
            )
            result = {
                "message_id": target_message_id,
                "emoji_id": emoji_id,
                "reacted": True,
            }
        elif name == "send_message":
            sent = await send_outbound_message(
                bot,
                group_id,
                args.get("segments"),
                session=session,
                actor_user_id=actor_user_id,
                source="tool",
            )
            result = {
                "sent": sent.sent,
                "message_id": sent.message_id,
                "segment_types": list(sent.segment_types),
                "message_type": sent.message_type,
                "outcome": sent.outcome,
                "delivery_state": sent.delivery_state,
                "degraded_from": sent.degraded_from,
                "text": sent.normalized_text[:500],
                "outbound": sent.storage_payload(),
            }
        elif name == "send_forward":
            sent = await send_forward_message(
                bot,
                group_id,
                args.get("nodes"),
                session=session,
                actor_user_id=actor_user_id,
                source="tool",
            )
            result = {
                "sent": sent.sent,
                "message_id": sent.message_id,
                "segment_types": list(sent.segment_types),
                "message_type": sent.message_type,
                "outcome": sent.outcome,
                "delivery_state": sent.delivery_state,
                "degraded_from": sent.degraded_from,
                "text": sent.normalized_text[:500],
                "outbound": sent.storage_payload(),
            }
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
            # NapCat/go-cqhttp 使用 `_send_group_notice`；只有适配器明确只暴露
            # `send_group_notice` 时才使用别名。不要在失败后自动重试另一个
            # action，避免公告其实已创建但回执失败时产生重复公告。
            action = (
                "_send_group_notice"
                if capabilities.has("_send_group_notice")
                else "send_group_notice"
            )
            result = await bot.call_api(
                action, group_id=group_id, content=str(args["content"])[:1000]
            )
        elif name == "set_essence_message":
            target_message_id = int(args["message_id"])
            await _require_current_group_message_api(
                bot, group_id, target_message_id
            )
            result = await bot.call_api(
                "set_essence_msg", message_id=target_message_id
            )
        elif name == "remove_essence_message":
            target_message_id = int(args["message_id"])
            await _require_current_group_message_api(
                bot, group_id, target_message_id
            )
            result = await bot.call_api(
                "delete_essence_msg", message_id=target_message_id
            )
        elif name == "delete_group_notice":
            notice_id = str(args.get("notice_id") or "").strip()
            if not notice_id:
                raise ValueError("notice_id 不能为空")
            result = await bot.call_api(
                "_del_group_notice",
                group_id=group_id,
                notice_id=notice_id,
            )
        elif name == "set_group_card":
            user_id = int(args["user_id"])
            await _require_group_member_api(bot, group_id, user_id)
            result = await bot.call_api(
                "set_group_card",
                group_id=group_id,
                user_id=user_id,
                card=str(args.get("card") or "")[:80],
            )
        elif name == "set_special_title":
            if capabilities.role != "owner":
                raise PermissionError("设置专属头衔需要机器人是群主")
            user_id = int(args["user_id"])
            await _require_group_member_api(bot, group_id, user_id)
            result = await bot.call_api(
                "set_group_special_title",
                group_id=group_id,
                user_id=user_id,
                special_title=str(args.get("special_title") or "")[:80],
            )
        elif name == "set_group_name":
            group_name = str(args.get("group_name") or "").strip()[:100]
            if not group_name:
                raise ValueError("group_name 不能为空")
            result = await bot.call_api(
                "set_group_name", group_id=group_id, group_name=group_name
            )
        elif name == "create_group_folder":
            folder_name = str(args.get("name") or "").strip()[:120]
            if not folder_name:
                raise ValueError("name 不能为空")
            result = await bot.call_api(
                "create_group_file_folder",
                group_id=group_id,
                name=folder_name,
            )
        elif name == "mute_member":
            user_id = int(args["user_id"])
            if not await target_can_be_muted(bot, group_id, user_id, capabilities.role):
                dbg(f"群 {group_id} mute_member 拒绝: 机器人无权禁言成员 {user_id}")
                raise PermissionError("机器人无权禁言该成员")
            dbg(
                f"群 {group_id} mute_member: 禁言成员 {user_id} {args.get('duration')}s"
            )
            result = await bot.call_api(
                "set_group_ban",
                group_id=group_id,
                user_id=user_id,
                duration=max(1, min(int(args["duration"]), 2592000)),
            )
        elif name == "kick_member":
            user_id = int(args["user_id"])
            if user_id == int(getattr(bot, "self_id", 0) or 0):
                raise PermissionError("机器人不能把自己移出群聊")
            if not await target_can_be_muted(bot, group_id, user_id, capabilities.role):
                raise PermissionError("机器人无权移出该成员")
            result = await bot.call_api(
                "set_group_kick",
                group_id=group_id,
                user_id=user_id,
                reject_add_request=bool(args.get("reject_add_request", False)),
            )
        elif name == "set_whole_group_mute":
            result = await bot.call_api(
                "set_group_whole_ban",
                group_id=group_id,
                enable=bool(args["enable"]),
            )
        elif name == "set_group_admin":
            if capabilities.role != "owner":
                raise PermissionError("设置群管理员需要机器人是群主")
            user_id = int(args["user_id"])
            target = await _require_group_member_api(bot, group_id, user_id)
            if str(target.get("role") or "member") == "owner":
                raise PermissionError("不能修改群主的管理员状态")
            result = await bot.call_api(
                "set_group_admin",
                group_id=group_id,
                user_id=user_id,
                enable=bool(args["enable"]),
            )
        elif name == "delete_group_file":
            result = await bot.call_api(
                "delete_group_file",
                group_id=group_id,
                file_id=str(args["file_id"]),
                busid=int(args["busid"]),
            )
        elif name == "move_group_file":
            result = await bot.call_api(
                "move_group_file",
                group_id=group_id,
                file_id=str(args["file_id"]),
                target_dir=str(args["target_dir"]),
            )
        elif name == "rename_group_file":
            result = await bot.call_api(
                "rename_group_file",
                group_id=group_id,
                file_id=str(args["file_id"]),
                current_parent_directory=str(args["current_parent_directory"]),
                new_name=str(args["new_name"])[:160],
            )
        elif name == "delete_group_folder":
            result = await bot.call_api(
                "delete_group_folder",
                group_id=group_id,
                folder_id=str(args["folder_id"]),
            )
        else:
            raise ValueError(f"未知工具: {name}")
        await _consume_admin_quota(session, group_id, name)
        await _audit(session, group_id, actor_user_id, name, args, "success")
        dbg(f"群 {group_id} 工具 {name} 执行成功")
        response = {"ok": True, "result": result}
        if name in _MESSAGE_SEND_TOOLS:
            # unknown 也必须终止本轮：OneBot 可能已执行发送，只是回执超时。
            response["sent"] = bool(
                isinstance(result, dict)
                and result.get("delivery_state")
                in {"confirmed_success", "degraded_success", "unknown"}
            )
        return response
    except Exception as exc:  # noqa: BLE001
        # 工具失败（含 DB 错误）先回滚，避免待回滚事务毒化后续 flush。
        logger.exception("群聊 Agent 工具执行失败: %s", name)
        if session is not None:
            with contextlib.suppress(SQLAlchemyError):
                await session.rollback()
        await _audit(session, group_id, actor_user_id, name, args, "error", str(exc))
        return {"ok": False, "error": str(exc)}


__all__ = [
    "MAX_FORWARD_NODES",
    "MAX_TOOL_ROUNDS",
    "TOOL_PERMISSION_CRITICAL",
    "TOOL_PERMISSION_MESSAGE_SEND",
    "TOOL_PERMISSION_PRIVILEGED",
    "TOOL_PERMISSION_READ",
    "TOOL_PERMISSION_STATE_WRITE",
    "build_tool_schemas",
    "dialogue_tool_round_limit",
    "execute_tool",
    "select_dialogue_message_segment_types",
    "select_dialogue_tool_names",
    "tool_permission_snapshot",
]
