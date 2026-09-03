# ruff: noqa: E501,F401,I001,TID252,PLR0912,PLR0913,PLR0917,C901,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100,PLR0915,PLR2004
"""群聊 Agent 的严格工具 schema 与 OneBot 执行器。"""

from __future__ import annotations

import contextlib
import json
from typing import Any

from nonebot import logger
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from ..data_models.agent_audit import AgentAudit
from ..data_models.group_agent_config import GroupAgentConfig
from ..data_models.user_group import UserGroup
from .capabilities import (
    BotGroupCapabilities,
    COMMON_MESSAGE_SEGMENTS,
    MessageSegmentCapabilities,
    probe_group_capabilities,
    user_can_manage_group,
)
from .context import now_beijing
from .log import dbg
from .outbound import MAX_FORWARD_NODES
from .tool_registry import (
    ADMIN_TOOLS as _ADMIN_TOOLS,
    CONTROLLED_TOOLS as _CONTROLLED_TOOLS,
    CRITICAL_TOOLS as _CRITICAL_TOOLS,
    MESSAGE_SEGMENT_FIELDS as _MESSAGE_SEGMENT_FIELDS,
    MESSAGE_SEGMENT_SCHEMA as _MESSAGE_SEGMENT_SCHEMA,
    MESSAGE_SEND_TOOLS as _MESSAGE_SEND_TOOLS,
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
from .tool_execution import ToolExecutionContext, ToolExecutionResult, ToolHandlerResult
from .tool_handlers import dispatch_tool
from .tool_support import _jsonable, _tool_result_limit
from .tool_router import (
    MAX_TOOL_ROUNDS,
    dialogue_tool_round_limit,
    rank_discoverable_tools,
    select_dialogue_message_segment_types,
    select_dialogue_tool_names,
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
    actor_can_manage: bool | None = None,
    privileged_allowlist: set[str] | None = None,
) -> dict[str, Any]:
    """Return compact summaries for currently eligible discoverable tools."""

    if actor_can_manage is None:
        actor_can_manage = bool(
            actor_user_id is not None
            and await user_can_manage_group(bot, group_id, actor_user_id)
        )
    allow_admin_tools = bool(actor_can_manage)
    allowlist = set(privileged_allowlist or ())
    if privileged_allowlist is None and session is not None:
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
            capabilities,
            allow_admin_tools=allow_admin_tools,
            max_permission_level=resolved_level,
            privileged_allowlist=allowlist,
        )
        if exposed and definition.discoverable:
            candidates.append(definition)
    matches = rank_discoverable_tools(
        query,
        candidates,
        family=family,
        limit=limit,
    )
    return {
        "tools": [
            {
                "name": item.name,
                "description": item.description,
                "family": item.family,
                "permission": item.permission_level,
            }
            for item in matches
        ],
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
    """审计尽力而为；真 AsyncSession 用 SAVEPOINT 隔离审计失败。"""

    if session is None:
        return

    async def _write() -> None:
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

    try:
        if callable(getattr(session, "begin_nested", None)):
            async with session.begin_nested():
                await _write()
        else:
            await _write()
    except SQLAlchemyError:
        dbg(f"群 {group_id} 工具审计写入失败(已抑制): tool={name}")
        if not callable(getattr(session, "begin_nested", None)):
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
    if definition.owner_only and capabilities.role != "owner":
        dbg(f"群 {group_id} 工具策略拒绝 {name}: 机器人不是群主")
        raise PermissionError("该工具需要机器人是群主")
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


async def _run_handler_with_policy(
    name: str,
    args: dict[str, Any],
    context: ToolExecutionContext,
) -> ToolHandlerResult:
    definition = _TOOL_BY_NAME.get(name)
    if definition is None:
        raise ValueError(f"未知工具: {name}")

    runtime_context = context
    if definition.permission_level in {
        TOOL_PERMISSION_PRIVILEGED,
        TOOL_PERMISSION_CRITICAL,
    }:
        # 高权限工具永远不信任回合开始时的 Bot 权限快照：真正执行副作用前
        # 强制刷新；actor 管理权限也由 _check_tool_policy 实时重新读取 OneBot。
        fresh = await probe_group_capabilities(
            context.bot, context.group_id, refresh=True
        )
        runtime_context = context.with_capabilities(fresh)

    await _check_tool_policy(
        runtime_context.session,
        runtime_context.group_id,
        name,
        runtime_context.capabilities,
        bot=runtime_context.bot,
        actor_user_id=runtime_context.actor_user_id,
    )

    if name == "discover_tools":
        query = str(args.get("query") or "").strip()
        if not query:
            raise ValueError("query 不能为空")
        family = str(args.get("family") or "").strip() or None
        limit = _tool_result_limit(args, default=5, maximum=8)
        result = await _discover_tools_for_actor(
            query=query,
            family=family,
            limit=limit,
            bot=runtime_context.bot,
            group_id=runtime_context.group_id,
            actor_user_id=runtime_context.actor_user_id,
            session=runtime_context.session,
            capabilities=runtime_context.capabilities,
            actor_can_manage=runtime_context.actor_can_manage,
            privileged_allowlist=(
                set(runtime_context.privileged_allowlist)
                if runtime_context.privileged_allowlist is not None
                else None
            ),
        )
        return ToolHandlerResult(result)

    return await dispatch_tool(name, args, runtime_context)


async def execute_tool_with_meta(
    name: str,
    args: dict[str, Any],
    *,
    context: ToolExecutionContext,
) -> ToolExecutionResult:
    """执行工具并返回不进入模型 Prompt 的事务/收尾元数据。"""

    dbg(
        f"群 {context.group_id} 执行工具: name={name!r} "
        f"args={json.dumps(args, ensure_ascii=False)} actor={context.actor_user_id}"
    )
    session = context.session
    try:
        # AsyncSession 使用 SAVEPOINT 隔离单个 Tool 失败；这样同一模型轮次里
        # 先前成功、尚未批量 commit 的 Tool 不会被后一个失败 Tool 一起回滚。
        if session is not None and callable(getattr(session, "begin_nested", None)):
            async with session.begin_nested():
                handled = await _run_handler_with_policy(name, args, context)
                await _consume_admin_quota(session, context.group_id, name)
        else:
            handled = await _run_handler_with_policy(name, args, context)
            await _consume_admin_quota(session, context.group_id, name)

        await _audit(
            session,
            context.group_id,
            context.actor_user_id,
            name,
            args,
            "success",
        )
        dbg(f"群 {context.group_id} 工具 {name} 执行成功")
        payload: dict[str, Any] = {"ok": True, "result": handled.result}
        if name in _MESSAGE_SEND_TOOLS:
            payload["sent"] = handled.ends_turn

        controlled = name in _CONTROLLED_TOOLS
        # 有 DB 会话时审计本身也是一次待提交写入；state_write / 配额写入
        # 同样只标记 needs_commit，由 dialogue 在本轮 Tool 批次末统一提交。
        needs_commit = bool(
            session is not None
            or handled.needs_commit
            or handled.mutated_db
            or controlled
        )
        return ToolExecutionResult(
            payload=payload,
            mutated_db=handled.mutated_db or controlled,
            needs_commit=needs_commit,
            immediate_commit=handled.immediate_commit or controlled,
            ends_turn=handled.ends_turn,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("群聊 Agent 工具执行失败: %s", name)
        # 真 AsyncSession 的 begin_nested 已只回滚当前 SAVEPOINT；兼容没有
        # SAVEPOINT 的测试/旧会话实现时，维持原来的失败后 rollback 语义。
        if session is not None and not callable(getattr(session, "begin_nested", None)):
            with contextlib.suppress(SQLAlchemyError):
                await session.rollback()
        await _audit(
            session,
            context.group_id,
            context.actor_user_id,
            name,
            args,
            "error",
            str(exc),
        )
        return ToolExecutionResult(
            payload={"ok": False, "error": str(exc)},
            needs_commit=session is not None,
        )


async def execute_tool(
    name: str,
    args: dict[str, Any],
    *,
    bot: Any,
    group_id: int,
    actor_user_id: int | None = None,
    session: Any = None,
    capabilities: BotGroupCapabilities,
    actor_can_manage: bool | None = None,
    privileged_allowlist: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """兼容旧调用方的单 Tool API；真实对话使用 execute_tool_with_meta。"""

    context = ToolExecutionContext(
        bot=bot,
        group_id=group_id,
        actor_user_id=actor_user_id,
        session=session,
        capabilities=capabilities,
        actor_can_manage=actor_can_manage,
        privileged_allowlist=(
            frozenset(privileged_allowlist)
            if privileged_allowlist is not None
            else None
        ),
    )
    return (await execute_tool_with_meta(name, args, context=context)).payload


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
    "execute_tool_with_meta",
    "select_dialogue_message_segment_types",
    "select_dialogue_tool_names",
    "tool_permission_snapshot",
]
