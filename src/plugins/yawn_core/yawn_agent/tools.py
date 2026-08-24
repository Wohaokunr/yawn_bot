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
    probe_group_capabilities,
    target_can_be_muted,
    user_can_manage_group,
)
from .context import now_beijing
from .log import dbg
from .memory import effective_relation_confidence, normalize_relation_type, rank_memories

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


@dataclass(frozen=True, slots=True)
class _ToolDefinition:
    name: str
    description: str
    properties: dict[str, Any]
    required: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    admin: bool = False


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
        "读取群成员列表（最多返回100人）",
        {},
        actions=("get_group_member_list",),
    ),
    _ToolDefinition(
        "search_group_memory",
        "搜索当前群已沉淀的记忆",
        {"query": {"type": "string", "minLength": 1, "maxLength": 120}},
        required=("query",),
    ),
    _ToolDefinition(
        "get_person_profile",
        "读取群内人物画像",
        {"user_id": {"type": "integer"}},
        required=("user_id",),
    ),
    _ToolDefinition(
        "list_user_relations",
        "查询群内某成员的全部已知关系",
        {"user_id": {"type": "integer"}},
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
    ),
    _ToolDefinition(
        "send_image",
        "发送群图片",
        {"file": {"type": "string"}},
        required=("file",),
        actions=("send_group_msg",),
    ),
    _ToolDefinition(
        "send_forward",
        "发送合并转发消息",
        {
            "messages": {
                "type": "array",
                "maxItems": 20,
                "items": {"type": "object"},
            }
        },
        required=("messages",),
        actions=("send_group_forward_msg",),
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
    ),
    _ToolDefinition(
        "create_group_announcement",
        "创建群公告",
        {"content": {"type": "string", "maxLength": 1000}},
        required=("content",),
        actions=("send_group_notice", "_send_group_notice"),
        admin=True,
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
    ),
)
_TOOL_BY_NAME = {item.name: item for item in _TOOL_DEFINITIONS}
_ADMIN_TOOLS = frozenset(item.name for item in _TOOL_DEFINITIONS if item.admin)


def build_tool_schemas(
    capabilities: BotGroupCapabilities, *, allow_admin_tools: bool = False
) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for definition in _TOOL_DEFINITIONS:
        if definition.actions and not any(
            capabilities.has(action) for action in definition.actions
        ):
            dbg(
                f"工具 schema: {definition.name} 因 OneBot 不支持 "
                f"{definition.actions} 而跳过"
            )
            continue
        if definition.admin and not (
            capabilities.can_manage and allow_admin_tools
        ):
            continue
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": definition.name,
                    "description": definition.description,
                    "parameters": {
                        "type": "object",
                        "properties": definition.properties,
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
    if name in _ADMIN_TOOLS and (
        actor_user_id is None
        or not await user_can_manage_group(bot, group_id, actor_user_id)
    ):
        dbg(f"群 {group_id} 工具策略拒绝 {name}: 调用者 {actor_user_id} 没有群管理权限")
        raise PermissionError("调用者没有群管理权限")
    if name not in _ADMIN_TOOLS:
        return
    if session is None:
        # 没有会话就无法校验白名单和配额，管理工具必须拒绝。
        dbg(f"群 {group_id} 工具策略拒绝 {name}: 缺少数据库会话")
        raise PermissionError("管理工具需要数据库会话")
    config = await session.get(GroupAgentConfig, group_id)
    if config is None:
        dbg(f"群 {group_id} 工具策略拒绝 {name}: Agent 配置不存在")
        raise PermissionError("群聊 Agent 配置不存在")
    # 空白名单即全部禁用；列默认值已是全量管理工具。
    allowlist = set(config.tool_allowlist or [])
    if name not in allowlist:
        dbg(f"群 {group_id} 工具策略拒绝 {name}: 不在白名单 {sorted(allowlist)}")
        raise PermissionError("该管理工具未加入 Agent 白名单")
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
        raise PermissionError("Agent 今日管理操作配额已用尽")
    dbg(f"群 {group_id} 工具策略通过: {name}")


async def _consume_admin_quota(session: Any, group_id: int, name: str) -> None:
    """配额只在工具成功后消耗，失败的尝试不计入。"""

    if name not in _ADMIN_TOOLS or session is None:
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
        f"群 {group_id} 消耗管理工具配额: {name} "
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
            result = await bot.call_api("get_group_info", group_id=group_id)
        elif name == "get_group_member":
            result = await bot.call_api(
                "get_group_member_info", group_id=group_id, user_id=int(args["user_id"])
            )
        elif name == "list_group_members":
            members = await bot.call_api("get_group_member_list", group_id=group_id)
            if not isinstance(members, list):
                raise ValueError("群成员列表响应格式错误")
            result = {
                "items": members[:100],
                "total": len(members),
                "truncated": len(members) > 100,
            }
        elif name == "get_person_profile":
            subject_id = int(args["user_id"])
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
        elif name == "search_group_memory":
            query = str(args.get("query", "")).strip()
            if not query:
                raise ValueError("query 不能为空")
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
            rows = rank_memories(rows, [query], None, now, limit=10)
            result = [
                {
                    "type": row.memory_type,
                    "key": row.memory_key,
                    "content": row.content,
                    "confidence": row.confidence,
                }
                for row in rows
            ]
        elif name == "list_user_relations":
            if session is None:
                raise PermissionError("关系查询需要数据库会话")
            subject_id = int(args["user_id"])
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
                        .limit(30)
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
        return {"ok": True, "result": result}
    except Exception as exc:  # noqa: BLE001
        # 工具失败（含 DB 错误）先回滚，避免待回滚事务毒化后续 flush。
        logger.exception("群聊 Agent 工具执行失败: %s", name)
        if session is not None:
            with contextlib.suppress(SQLAlchemyError):
                await session.rollback()
        await _audit(session, group_id, actor_user_id, name, args, "error", str(exc))
        return {"ok": False, "error": str(exc)}


__all__ = ["MAX_FORWARD_SEND", "MAX_TOOL_ROUNDS", "build_tool_schemas", "execute_tool"]
