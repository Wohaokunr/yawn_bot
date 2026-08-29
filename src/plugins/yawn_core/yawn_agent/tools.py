# ruff: noqa: E501,F401,I001,TID252,PLR0912,PLR0913,PLR0917,C901,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100,PLR0915,PLR2004
"""群聊 Agent 的严格工具 schema 与 OneBot 执行器。"""

from __future__ import annotations

import contextlib
import json
import mimetypes
import os
import tempfile
from dataclasses import dataclass
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

MAX_TOOL_ROUNDS = 4
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

_MESSAGE_SEGMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": [
                "text",
                "reply",
                "at",
                "face",
                "reaction",
                "image",
                "record",
                "video",
                "rps",
                "dice",
                "poke",
                "share",
                "contact",
                "location",
                "music",
            ],
        },
        "text": {"type": "string", "maxLength": 4000},
        "message_id": {"type": "integer"},
        "user_id": {"type": "integer", "minimum": 1},
        "id": {"type": "integer", "minimum": 0, "maximum": 65535},
        "reaction_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "file": {"type": "string", "minLength": 1},
        "poke_type": {"type": "string", "minLength": 1, "maxLength": 32},
        "poke_id": {"type": "string", "minLength": 1, "maxLength": 32},
        "url": {"type": "string", "minLength": 1, "maxLength": 2048},
        "title": {"type": "string", "minLength": 1, "maxLength": 120},
        "content": {"type": "string", "maxLength": 300},
        "contact_type": {"type": "string", "enum": ["qq", "group"]},
        "latitude": {"type": "number", "minimum": -90, "maximum": 90},
        "longitude": {"type": "number", "minimum": -180, "maximum": 180},
        "provider": {"type": "string", "enum": ["qq", "163", "xm"]},
    },
    "required": ["type"],
    "additionalProperties": False,
}

_MESSAGE_SEGMENT_FIELDS: dict[str, frozenset[str]] = {
    "text": frozenset({"text"}),
    "reply": frozenset({"message_id"}),
    "at": frozenset({"user_id"}),
    "face": frozenset({"id"}),
    "reaction": frozenset({"reaction_id"}),
    "image": frozenset({"file"}),
    "record": frozenset({"file"}),
    "video": frozenset({"file"}),
    "rps": frozenset(),
    "dice": frozenset(),
    "poke": frozenset({"poke_type", "poke_id"}),
    "share": frozenset({"url", "title", "content"}),
    "contact": frozenset({"contact_type", "id"}),
    "location": frozenset({"latitude", "longitude", "title", "content"}),
    "music": frozenset({"provider", "id"}),
}


@dataclass(frozen=True, slots=True)
class _ToolDefinition:
    name: str
    description: str
    properties: dict[str, Any]
    required: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    admin: bool = False
    permission_level: str = "read"


TOOL_PERMISSION_READ = "read"
TOOL_PERMISSION_STATE_WRITE = "state_write"
TOOL_PERMISSION_MESSAGE_SEND = "message_send"
TOOL_PERMISSION_PRIVILEGED = "privileged"
_TOOL_PERMISSION_RANK = {
    TOOL_PERMISSION_READ: 0,
    TOOL_PERMISSION_STATE_WRITE: 1,
    TOOL_PERMISSION_MESSAGE_SEND: 2,
    TOOL_PERMISSION_PRIVILEGED: 3,
}


_TOOL_DEFINITIONS = (
    _ToolDefinition(
        "get_group_info", "读取当前群信息", {}, actions=("get_group_info",)
    ),
    _ToolDefinition(
        "get_group_member",
        "读取群成员角色和头衔",
        {"user_id": {"type": "integer"}},
        required=("user_id",),
        actions=("get_group_member_info",),
    ),
    _ToolDefinition(
        "list_group_members",
        "读取群成员列表（默认30人，最多50人）",
        {"limit": {"type": "integer", "minimum": 1, "maximum": 50}},
        actions=("get_group_member_list",),
    ),
    _ToolDefinition(
        "search_group_memory",
        "搜索当前群已沉淀的记忆（默认6条，最多10条）",
        {
            "query": {"type": "string", "minLength": 1, "maxLength": 120},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        required=("query",),
    ),
    _ToolDefinition(
        "get_person_profile",
        "读取群内人物画像（默认6条，最多10条）",
        {
            "user_id": {"type": "integer"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        required=("user_id",),
    ),
    _ToolDefinition(
        "list_user_relations",
        "查询群内某成员的已知关系（默认12条，最多20条）",
        {
            "user_id": {"type": "integer"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        required=("user_id",),
    ),
    _ToolDefinition(
        "record_user_relation",
        "记录对话中明确观察到的两位成员之间的关系",
        {
            "subject_user_id": {"type": "integer"},
            "object_user_id": {"type": "integer"},
            "type": {
                "type": "string",
                "minLength": 1,
                "maxLength": 32,
                "description": "优先使用：好友/死党/情侣/伴侣/亲属/师徒/同事/同学/搭子/对立",
            },
            "note": {
                "type": "string",
                "maxLength": 200,
                "description": "一句话关系背景，没有可省略",
            },
        },
        required=("subject_user_id", "object_user_id", "type"),
        permission_level=TOOL_PERMISSION_STATE_WRITE,
    ),
    _ToolDefinition(
        "search_reactions",
        (
            "按情绪/场景标签搜索本地表情包库，例如无语、开心、疑惑、吃瓜、震惊。"
            "返回 reaction_id；发送时必须使用 reaction 段，禁止猜测本地图片路径。"
        ),
        {
            "query": {"type": "string", "minLength": 1, "maxLength": 80},
            "limit": {"type": "integer", "minimum": 1, "maximum": 8},
        },
        required=("query",),
    ),
    _ToolDefinition(
        "send_message",
        (
            "发送一条结构化 QQ 群消息，可组合引用、@、文本、QQ 表情、表情包、图片、"
            "语音、视频、猜拳、骰子和 poke。reply.message_id 必须来自当前上下文"
            "已有消息，at.user_id 必须是当前群成员；禁止 CQ 码和 @all。"
            "成功调用后消息已经发出，不要再用最终文本重复发送同样内容。"
        ),
        {
            "segments": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_OUTBOUND_SEGMENTS,
                "items": _MESSAGE_SEGMENT_SCHEMA,
            }
        },
        required=("segments",),
        actions=("send_group_msg",),
        permission_level=TOOL_PERMISSION_MESSAGE_SEND,
    ),
    _ToolDefinition(
        "send_forward",
        (
            "发送受控合并转发。message 节点只能引用当前群近期 message_id；"
            "custom 节点只给 user_id/content，nickname 由 Python 从当前群成员解析。"
        ),
        {
            "nodes": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_FORWARD_NODES,
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["message", "custom"]},
                        "message_id": {"type": "integer"},
                        "user_id": {"type": "integer", "minimum": 1},
                        "content": {"type": "string", "minLength": 1, "maxLength": 4000},
                    },
                    "required": ["type"],
                    "additionalProperties": False,
                },
            }
        },
        required=("nodes",),
        actions=("send_group_forward_msg",),
        permission_level=TOOL_PERMISSION_MESSAGE_SEND,
    ),
    _ToolDefinition(
        "mute_member",
        "禁言群成员",
        {
            "user_id": {"type": "integer"},
            "duration": {"type": "integer", "minimum": 1, "maximum": 2592000},
        },
        required=("user_id", "duration"),
        actions=("set_group_ban",),
        admin=True,
        permission_level=TOOL_PERMISSION_PRIVILEGED,
    ),
    _ToolDefinition(
        "create_group_announcement",
        "创建群公告",
        {"content": {"type": "string", "maxLength": 1000}},
        required=("content",),
        actions=("send_group_notice", "_send_group_notice"),
        admin=True,
        permission_level=TOOL_PERMISSION_PRIVILEGED,
    ),
    _ToolDefinition(
        "send_file",
        "发送群文件或文档",
        {
            "file": {"type": "string"},
            "name": {"type": "string", "maxLength": 128},
        },
        required=("file", "name"),
        actions=("upload_group_file",),
        permission_level=TOOL_PERMISSION_PRIVILEGED,
    ),
)
_TOOL_BY_NAME = {item.name: item for item in _TOOL_DEFINITIONS}
_ADMIN_TOOLS = frozenset(item.name for item in _TOOL_DEFINITIONS if item.admin)
_PRIVILEGED_TOOLS = frozenset(
    item.name
    for item in _TOOL_DEFINITIONS
    if item.permission_level == TOOL_PERMISSION_PRIVILEGED
)
_MESSAGE_SEND_TOOLS = frozenset({"send_message", "send_forward"})

_MESSAGE_TOOL_HINTS = (
    "回复",
    "引用",
    "艾特",
    "表情",
    "图片",
    "发图",
    "语音",
    "视频",
    "骰子",
    "猜拳",
    "戳一戳",
    "poke",
    "分享",
    "链接",
    "名片",
    "位置",
    "定位",
    "音乐",
    "歌曲",
)
_REACTION_TOOL_HINTS = ("表情包", "reaction", "无语", "吃瓜", "震惊")
_MEMORY_SEARCH_HINTS = ("记得", "记忆", "之前", "以前", "上次")
_PROFILE_TOOL_HINTS = ("画像", "人物资料", "个人资料", "关系", "认识")
_GROUP_TOOL_HINTS = ("群信息", "群资料", "群成员", "成员列表", "管理员", "群主", "头衔")
_RELATION_WRITE_HINTS = (
    "记录关系",
    "记住关系",
    "我们是",
    "是我朋友",
    "是我对象",
    "是我同事",
    "是我同学",
    "是我搭子",
    "是我死党",
    "是我伴侣",
)


def select_dialogue_tool_names(
    text: str,
    *,
    has_reply: bool = False,
    has_mentions: bool = False,
    has_media: bool = False,
    allow_admin_tools: bool = False,
) -> frozenset[str]:
    """Select a small deterministic tool bundle for one dialogue turn.

    Most QQ chat turns only need a direct text response. Sending the complete
    schema catalog on every request costs thousands of input tokens and also
    reduces the cached-prefix share. Tool bundles are intentionally keyword-
    based rather than LLM-classified so selecting them has zero AI cost.
    """

    normalized = str(text or "").strip().casefold()
    selected: set[str] = set()
    if has_mentions or any(
        hint.casefold() in normalized for hint in _MESSAGE_TOOL_HINTS
    ):
        selected.add("send_message")
    if any(hint.casefold() in normalized for hint in _REACTION_TOOL_HINTS):
        selected.update(("search_reactions", "send_message"))
    if "合并转发" in normalized or "转发" in normalized:
        selected.add("send_forward")
    if any(hint in normalized for hint in _MEMORY_SEARCH_HINTS):
        selected.add("search_group_memory")
    if any(hint in normalized for hint in _PROFILE_TOOL_HINTS):
        selected.update(("get_person_profile", "list_user_relations"))
    if any(hint in normalized for hint in _GROUP_TOOL_HINTS):
        selected.update(("get_group_info", "get_group_member", "list_group_members"))
    if any(hint in normalized for hint in _RELATION_WRITE_HINTS):
        selected.add("record_user_relation")
    if allow_admin_tools:
        if "禁言" in normalized:
            selected.add("mute_member")
        if "公告" in normalized:
            selected.add("create_group_announcement")
        if "群文件" in normalized or "发文件" in normalized or "发送文件" in normalized:
            selected.add("send_file")
    return frozenset(selected)


def select_dialogue_message_segment_types(
    text: str,
    *,
    has_target_mentions: bool = False,
) -> frozenset[str]:
    """Select the smallest outbound message vocabulary needed this turn."""

    normalized = str(text or "").strip().casefold()
    selected: set[str] = {"text"}
    if "回复" in normalized or "引用" in normalized:
        selected.add("reply")
    if has_target_mentions or "艾特" in normalized:
        selected.add("at")
    if "表情包" in normalized or "reaction" in normalized or any(
        hint in normalized for hint in ("无语", "吃瓜", "震惊")
    ):
        selected.add("reaction")
    elif "表情" in normalized:
        selected.add("face")
    if "图片" in normalized or "发图" in normalized:
        selected.add("image")
    if "语音" in normalized:
        selected.add("record")
    if "视频" in normalized:
        selected.add("video")
    if "骰子" in normalized:
        selected.add("dice")
    if "猜拳" in normalized:
        selected.add("rps")
    if "戳一戳" in normalized or "poke" in normalized:
        selected.add("poke")
    if "分享" in normalized or "链接" in normalized:
        selected.add("share")
    if "名片" in normalized:
        selected.add("contact")
    if "位置" in normalized or "定位" in normalized:
        selected.add("location")
    if "音乐" in normalized or "歌曲" in normalized:
        selected.add("music")
    return frozenset(selected)


def dialogue_tool_round_limit(tool_names: frozenset[str] | set[str]) -> int:
    """Return a bounded LLM round budget for the selected tool bundle.

    A plain response needs one model call. Direct message tools also need only
    one call because a successful visible send ends the turn. Read/search
    tools need one additional call to turn their result into a reply. Only
    genuinely mixed or privileged bundles may use a third round; four rounds
    remain an absolute compatibility ceiling rather than the normal path.
    """

    names = frozenset(tool_names)
    if not names:
        return 1
    if names <= _MESSAGE_SEND_TOOLS:
        return 2
    if names & _PRIVILEGED_TOOLS:
        return min(MAX_TOOL_ROUNDS, 3)
    non_send = names - _MESSAGE_SEND_TOOLS
    if len(non_send) <= 2:
        return 2
    return min(MAX_TOOL_ROUNDS, 3)


def _max_tool_permission_level(
    *, allow_admin_tools: bool, max_permission_level: str | None
) -> str:
    if max_permission_level is None:
        return (
            TOOL_PERMISSION_PRIVILEGED
            if allow_admin_tools
            else TOOL_PERMISSION_MESSAGE_SEND
        )
    normalized = str(max_permission_level).strip().lower()
    if normalized not in _TOOL_PERMISSION_RANK:
        raise ValueError(f"未知工具权限等级: {max_permission_level}")
    return normalized


def _tool_exposure_reason(
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
    if definition.permission_level == TOOL_PERMISSION_PRIVILEGED and not allow_admin_tools:
        return False, "actor_not_admin"
    if (
        definition.permission_level == TOOL_PERMISSION_PRIVILEGED
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
    if definition.admin and not capabilities.can_manage:
        dbg(f"群 {group_id} 工具策略拒绝 {name}: 机器人没有群管理权限")
        raise PermissionError("机器人没有群管理权限")
    if definition.actions and not any(
        capabilities.has(action) for action in definition.actions
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
    if name in _PRIVILEGED_TOOLS and (
        actor_user_id is None
        or not await user_can_manage_group(bot, group_id, actor_user_id)
    ):
        dbg(f"群 {group_id} 工具策略拒绝 {name}: 调用者 {actor_user_id} 没有群管理权限")
        raise PermissionError(
            "调用者没有群管理权限"
            if name in _ADMIN_TOOLS
            else "特权工具仅允许群主或管理员触发"
        )
    if name not in _PRIVILEGED_TOOLS:
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
        dbg(f"群 {group_id} 管理工具配额计数按新的一天重置")
    if config.admin_tool_count >= max(int(config.admin_tool_daily_limit), 1):
        dbg(
            f"群 {group_id} 工具策略拒绝 {name}: 今日配额已用尽"
            f"({config.admin_tool_count}/{config.admin_tool_daily_limit})"
        )
        raise PermissionError("Agent 今日特权操作配额已用尽")
    dbg(f"群 {group_id} 工具策略通过: {name}")


async def _consume_admin_quota(session: Any, group_id: int, name: str) -> None:
    """特权工具配额只在成功后消耗，失败尝试不计入。"""

    if name not in _PRIVILEGED_TOOLS or session is None:
        return
    config = await session.get(GroupAgentConfig, group_id)
    if config is None:
        return
    today = now_beijing().strftime("%Y-%m-%d")
    if config.tool_day != today:
        config.tool_day = today
        config.admin_tool_count = 0
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
    """执行单个工具；每次调用都重新执行能力和配额校验。"""

    dbg(
        f"群 {group_id} 执行工具: name={name!r} args={json.dumps(args, ensure_ascii=False)} "
        f"actor={actor_user_id}"
    )
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
        now = now_beijing()
        if name == "get_group_info":
            result = _compact_group_info(
                await bot.call_api("get_group_info", group_id=group_id)
            )
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
            result = [
                {
                    "type": row.memory_type,
                    "key": row.memory_key,
                    "content": str(row.content or "")[:600],
                    "confidence": round(float(row.confidence or 0.0), 3),
                }
                for row in rows
            ]
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
