# ruff: noqa: E501,F401,I001,TID252,PLR0912,PLR0913,PLR0917,C901,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100,PLR0915,PLR2004
"""群聊 Agent 的严格工具 schema 与 OneBot 执行器。"""

from __future__ import annotations

import contextlib
import json
import time
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
from .execution_trace import current_execution_trace
from .log import dbg, dbg_exc
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
    allowlist_covers,
    tool_argument_summary,
)
from .tool_args import (
    ToolArgumentError,
    audit_safe_value,
    classify_tool_error,
    sanitize_error_text,
    tool_error_payload,
    validate_tool_arguments,
)
from .tool_execution import ToolExecutionContext, ToolExecutionResult, ToolHandlerResult
from .tool_handlers import dispatch_tool
from .tool_support import _compact_onebot_message, _jsonable, _tool_result_limit
from .tool_router import (
    MAX_TOOL_ROUNDS,
    dialogue_tool_round_limit,
    discovery_segment_types,
    has_explicit_critical_intent,
    normalize_tool_family,
    rank_discoverable_tools,
    select_critical_tool_names,
    select_dialogue_message_segment_types,
    select_dialogue_tool_names,
)

# 这些工具族只写调用者自己的东西（本群沙箱工作区），不需要 UserGroup 里已有
# 这位成员：新入群、还没同步过成员表的人也能跑代码。真实 actor 仍然必需。
_ACTOR_SCOPED_FAMILIES = frozenset({"sandbox"})
FAMILY_LOAD_LIMIT = 12
# 同参数也可能有不同结果（随机数、读到更新后的文件），不做同回合去重。
_NON_IDEMPOTENT_TOOLS = frozenset({"run_python", "run_shell"})

# 发现阶段可以向模型说明的“存在但当前不可用”原因；其余原因（权限等级上限）
# 属于内部策略，不回报。
_BLOCKED_TOOL_REASONS: dict[str, str] = {
    "actor_not_admin": "需要群主或管理员本人发起",
    "not_allowlisted": "本群还没在 WebUI 的 Agent 工具白名单里开放它",
    "bot_not_admin": "机器人在本群不是管理员",
    "bot_not_owner": "机器人在本群不是群主",
    "onebot_action": "当前 QQ 协议端不支持这个动作",
    "sandbox_closed": "管理员没有在本群开放代码沙箱",
    "sandbox_offline": "代码沙箱服务当前离线",
    "sandbox_unsafe": "代码沙箱隔离自检未通过，已暂停使用",
}


async def _sandbox_exposure(group_id: int, *, actor_is_admin: bool) -> str | None:
    """沙箱工具族的发现前置检查；健康状态走 15 秒缓存，不会每轮都打 socket。"""

    from .sandbox_client import cached_health
    from .sandbox_policy import exposure_reason

    health, error = await cached_health()
    return exposure_reason(
        group_id, actor_is_admin=actor_is_admin, health=health, health_error=error
    )


def _sandbox_has_shell() -> bool:
    from .sandbox_client import peek_health, supports_language

    health, _error, _fresh = peek_health()
    return health is None or supports_language(health, "shell")


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
        and not allowlist_covers(definition.name, privileged_allowlist)
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
    privileged_allowlist: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """Discover eligible tools without preloading their schemas.

    Actor permission and allowlist snapshots are reused when the turn already
    computed them. Controlled discovery still upgrades bot capabilities lazily.
    """

    if actor_can_manage is None:
        actor_can_manage = bool(
            actor_user_id is not None
            and await user_can_manage_group(bot, group_id, actor_user_id)
        )
    allow_admin_tools = bool(actor_can_manage)
    effective_capabilities = capabilities
    if allow_admin_tools and not capabilities.can_manage:
        effective_capabilities = await probe_group_capabilities(bot, group_id)

    allowlist = set(privileged_allowlist or ())
    if privileged_allowlist is None and session is not None:
        config = await session.get(GroupAgentConfig, group_id)
        if config is not None:
            allowlist = set(config.tool_allowlist or [])

    resolved_level = _max_tool_permission_level(
        allow_admin_tools=allow_admin_tools,
        max_permission_level=None,
    )
    # 模型常用中文/英文复数称呼工具包；能归一就归一，认不出来就把它当作任务描述
    # 去搜索，避免 family="公告" 这类调用被过滤成空结果、让模型以为没有这个工具。
    requested_family = str(family or "").strip() or None
    family_key = normalize_tool_family(requested_family)
    if requested_family and family_key is None:
        query = " ".join(part for part in (query, requested_family) if part).strip()
        requested_family = None
    elif family_key is not None:
        requested_family = family_key
    candidates: list[_ToolDefinition] = []
    hidden: list[tuple[_ToolDefinition, str]] = []
    # 指定了别的工具包时沙箱工具反正会被过滤掉，不必检查沙箱状态。
    sandbox_reason = (
        await _sandbox_exposure(group_id, actor_is_admin=allow_admin_tools)
        if requested_family in {None, "sandbox"}
        else None
    )
    for definition in _TOOL_DEFINITIONS:
        if (
            not definition.discoverable
            or definition.permission_level == TOOL_PERMISSION_CRITICAL
        ):
            # critical 工具永远不能被发现，只能由当前触发消息的显式动作意图装载。
            continue
        exposed, reason = _tool_exposure_reason(
            definition,
            effective_capabilities,
            allow_admin_tools=allow_admin_tools,
            max_permission_level=resolved_level,
            privileged_allowlist=allowlist,
        )
        if exposed and definition.family == "sandbox" and sandbox_reason is not None:
            exposed, reason = False, sandbox_reason
        if exposed and definition.name == "run_shell" and not _sandbox_has_shell():
            # 旧 runner 不支持终端：直接不暴露，模型会改用 run_python。
            continue
        if exposed:
            candidates.append(definition)
            continue
        if (
            not allow_admin_tools
            and definition.permission_level == TOOL_PERMISSION_PRIVILEGED
        ):
            # 普通成员用的是不带管理 action 的本地能力基线，此时 onebot_action /
            # permission_level 都不是真正原因；对成员来说原因只有“需要管理发起”。
            reason = "actor_not_admin"
        if reason in _BLOCKED_TOOL_REASONS:
            hidden.append((definition, reason))

    normalized_query = str(query or "").strip().casefold()
    catalog_request = not requested_family and normalized_query in {
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
            family=requested_family,
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
    payload: dict[str, Any] = {
        "mode": "catalog" if catalog_request else ("toolpack" if requested_family else "search"),
        "tools": [
            {
                "name": item.name,
                "description": item.description,
                "family": item.family,
                "permission": item.permission_level,
                **(
                    {"args": tool_argument_summary(item)}
                    if item.properties
                    else {}
                ),
                **({"requires": item.prerequisites} if item.prerequisites else {}),
            }
            for item in matches
        ],
        "toolpacks": toolpacks,
        "count": len(matches),
    }
    if not catalog_request:
        # 被权限/白名单挡住的同类工具只回报“存在但不可用 + 原因”，不给参数、
        # 不装载 schema；模型据此如实说明，而不是回答“没有这个功能”。
        matched_names = {item.name for item in matches}
        blocked = [
            definition
            for definition in rank_discoverable_tools(
                query,
                [definition for definition, _reason in hidden],
                family=requested_family,
                limit=3,
            )
            if definition.name not in matched_names
        ]
        if blocked:
            reasons = {definition.name: reason for definition, reason in hidden}
            payload["unavailable"] = [
                {
                    "name": definition.name,
                    "description": definition.description,
                    "reason": _BLOCKED_TOOL_REASONS[reasons[definition.name]],
                }
                for definition in blocked
            ]
            payload["message"] = (
                "unavailable 里的工具当前不能用；如实告诉成员原因，"
                "不要声称已经执行，也不要换别的工具硬做。"
            )
    return payload


async def _audit(
    session: Any,
    group_id: int,
    actor_user_id: int | None,
    name: str,
    args: dict[str, Any],
    result: str,
    detail: str = "",
    *,
    duration_ms: float | None = None,
    error_code: str | None = None,
) -> None:
    """审计尽力而为；真 AsyncSession 用 SAVEPOINT 隔离审计失败。"""

    if session is None:
        return

    trace = current_execution_trace()
    safe_arguments = {
        key: audit_safe_value(_jsonable(value))
        for key, value in args.items()
        if key not in {"file", "url", "code"}
    }
    if isinstance(args.get("code"), str):
        # 代码正文可能很长且含成员数据，审计只留长度；完整代码在沙箱运行历史里。
        safe_arguments["code_chars"] = len(args["code"])
    if trace is not None:
        safe_arguments["_trace_id"] = trace.trace_id
    if duration_ms is not None:
        safe_arguments["_duration_ms"] = round(float(duration_ms), 1)
    if error_code:
        safe_arguments["_error_code"] = error_code
    detail = sanitize_error_text(detail, limit=2000) if detail else ""

    async def _write() -> None:
        session.add(
            AgentAudit(
                group_id=group_id,
                actor_user_id=actor_user_id,
                tool_name=name,
                arguments=safe_arguments,
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
    intent_text: str | None = None,
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
    if definition.permission_level == TOOL_PERMISSION_STATE_WRITE and (
        definition.family not in _ACTOR_SCOPED_FAMILIES
    ):
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
    if name in _CRITICAL_TOOLS and not has_explicit_critical_intent(
        name, intent_text
    ):
        dbg(f"群 {group_id} 工具策略拒绝 {name}: 当前触发消息没有显式动作意图")
        keywords = "、".join(f"“{item}”" for item in definition.intent_keywords)
        raise PermissionError(
            "高风险工具需要调用者在本条触发消息中明确提出该操作"
            f"（需包含{keywords}等字样）；历史消息、引用内容或自行推断都不算，"
            "请让管理员直接说明要执行的操作"
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
    if not allowlist_covers(name, allowlist):
        dbg(f"群 {group_id} 工具策略拒绝 {name}: 不在白名单 {sorted(allowlist)}")
        raise PermissionError("该特权工具未加入 Agent 白名单")
    # 只读校验：跨天时视为 0，真正的重置与计数在预扣时一起落库。
    same_day = config.tool_day == now_beijing().strftime("%Y-%m-%d")
    if name in _CRITICAL_TOOLS:
        used = int(config.critical_tool_count or 0) if same_day else 0
        daily_limit = max(int(config.critical_tool_daily_limit or 0), 1)
        quota_label = "高风险工具"
    else:
        used = int(config.admin_tool_count or 0) if same_day else 0
        daily_limit = max(int(config.admin_tool_daily_limit or 0), 1)
        quota_label = "管理工具"
    if used >= daily_limit:
        dbg(
            f"群 {group_id} 工具策略拒绝 {name}: 今日{quota_label}配额已用尽"
            f"({used}/{daily_limit})"
        )
        raise PermissionError(f"Agent 今日{quota_label}配额已用尽")
    dbg(f"群 {group_id} 工具策略通过: {name}")


def _reset_tool_day(config: Any) -> None:
    today = now_beijing().strftime("%Y-%m-%d")
    if config.tool_day != today:
        config.tool_day = today
        config.admin_tool_count = 0
        config.critical_tool_count = 0


async def _persist_quota_change(session: Any, config: Any, group_id: int) -> None:
    """立即提交配额计数，并在提交后重载 config 以免后续读到过期属性。

    没有 ``commit`` 的测试/兼容会话退化为 flush。提交失败时回滚并抛出，
    由调用方决定是否继续执行副作用。
    """

    if not callable(getattr(session, "commit", None)):
        await session.flush()
        return
    try:
        await session.commit()
    except SQLAlchemyError:
        dbg_exc(f"群 {group_id} 工具配额提交失败,已回滚")
        with contextlib.suppress(SQLAlchemyError):
            await session.rollback()
        raise
    if callable(getattr(session, "refresh", None)):
        with contextlib.suppress(SQLAlchemyError):
            await session.refresh(config)


async def _consume_admin_quota(session: Any, group_id: int, name: str) -> bool:
    """在执行副作用之前预扣并提交特权/高风险工具配额。

    旧实现先踢人/禁言、再 flush 计数，计数提交失败时动作已发生却报失败，
    模型可能重试导致重复副作用，SQLite 锁竞争时还能绕过每日上限。现在
    先把计数落库，再执行动作；动作失败由 ``_refund_admin_quota`` 退还。
    返回是否真的预扣了配额。
    """

    if name not in _CONTROLLED_TOOLS or session is None:
        return False
    config = await session.get(GroupAgentConfig, group_id)
    if config is None:
        return False
    _reset_tool_day(config)
    if name in _CRITICAL_TOOLS:
        config.critical_tool_count = int(config.critical_tool_count or 0) + 1
        dbg(
            f"群 {group_id} 预扣高风险工具配额: {name} "
            f"(今日已用 {config.critical_tool_count}/{config.critical_tool_daily_limit})"
        )
    else:
        config.admin_tool_count = int(config.admin_tool_count or 0) + 1
        dbg(
            f"群 {group_id} 预扣特权工具配额: {name} "
            f"(今日已用 {config.admin_tool_count}/{config.admin_tool_daily_limit})"
        )
    await _persist_quota_change(session, config, group_id)
    return True


async def _refund_admin_quota(session: Any, group_id: int, name: str) -> None:
    """副作用失败时退还预扣的配额；尽力而为，失败不影响主流程。"""

    try:
        config = await session.get(GroupAgentConfig, group_id)
        if config is None:
            return
        if callable(getattr(session, "refresh", None)):
            await session.refresh(config)
        if name in _CRITICAL_TOOLS:
            config.critical_tool_count = max(int(config.critical_tool_count or 0) - 1, 0)
        else:
            config.admin_tool_count = max(int(config.admin_tool_count or 0) - 1, 0)
        await _persist_quota_change(session, config, group_id)
    except SQLAlchemyError:
        dbg_exc(f"群 {group_id} 工具 {name} 失败后退还配额失败(忽略)")
    else:
        dbg(f"群 {group_id} 工具 {name} 执行失败,已退还预扣配额")


async def _authorize_tool(
    name: str,
    context: ToolExecutionContext,
) -> ToolExecutionContext:
    """校验工具策略并返回执行副作用时使用的上下文；本函数没有副作用。"""

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
        intent_text=runtime_context.current_intent_text,
    )
    return runtime_context


async def _dispatch_authorized(
    name: str,
    args: dict[str, Any],
    runtime_context: ToolExecutionContext,
) -> ToolHandlerResult:
    if name == "discover_tools":
        # schema 允许只传 family（按工具包加载）；只有两者都缺时才是无效调用。
        query = str(args.get("query") or "").strip()
        family = str(args.get("family") or "").strip() or None
        if not query and family is None:
            raise ToolArgumentError(
                "query 和 family 至少提供一个",
                hint="用 query 描述任务，或用 family 加载 toolpacks 里的工具包",
            )
        # 按工具包整包加载时默认给满 12 个（沙箱族有 9 个），免得包里排在后面的
        # 工具被截掉；按关键词搜仍然最多 8 个。
        limit = _tool_result_limit(
            args,
            default=FAMILY_LOAD_LIMIT if family and not query else 5,
            maximum=FAMILY_LOAD_LIMIT if family and not query else 8,
        )
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


async def _run_handler_with_policy(
    name: str,
    args: dict[str, Any],
    context: ToolExecutionContext,
) -> ToolHandlerResult:
    """校验后直接执行；不涉及配额的调用方（含测试）使用。"""

    runtime_context = await _authorize_tool(name, context)
    return await _dispatch_authorized(name, args, runtime_context)


async def execute_tool_with_meta(
    name: str,
    args: dict[str, Any],
    *,
    context: ToolExecutionContext,
) -> ToolExecutionResult:
    """执行工具并返回不进入模型 Prompt 的事务/收尾元数据。

    顺序固定为：策略校验 → 预扣并提交配额 → 执行副作用 → 审计。
    副作用失败退还配额；配额无法落库时副作用根本不会发生。
    """

    dbg(
        f"群 {context.group_id} 执行工具: name={name!r} "
        f"args={json.dumps(args, ensure_ascii=False)} actor={context.actor_user_id}"
    )
    session = context.session
    controlled = name in _CONTROLLED_TOOLS
    definition = _TOOL_BY_NAME.get(name)
    reserved = False
    turn_reserved = False
    started = time.monotonic()
    try:
        if definition is None:
            raise ToolArgumentError(f"未知工具: {name}", hint="只能调用当前 schema 中的工具")
        # 1) schema 校验与规范化：不合法的参数不进入权限/配额/副作用流程。
        args = validate_tool_arguments(
            name, definition.properties, definition.required, args
        )
        # 2) 同回合重复写操作直接复用首次结果，不再扣配额、不再执行副作用。
        writes_state = (
            _TOOL_PERMISSION_RANK[definition.permission_level]
            >= _TOOL_PERMISSION_RANK[TOOL_PERMISSION_STATE_WRITE]
            and name not in _MESSAGE_SEND_TOOLS
            and name not in _NON_IDEMPOTENT_TOOLS
        )
        if writes_state:
            duplicate = context.ledger.duplicate_of(name, args)
            if duplicate is not None:
                dbg(f"群 {context.group_id} 工具 {name} 同回合重复调用,复用首次结果")
                return ToolExecutionResult(payload=duplicate)
        runtime_context = await _authorize_tool(name, context)
        if controlled:
            context.ledger.reserve_controlled()
            turn_reserved = True
            reserved = bool(
                await _consume_admin_quota(session, context.group_id, name)
            )
        # AsyncSession 使用 SAVEPOINT 隔离单个 Tool 失败；这样同一模型轮次里
        # 先前成功、尚未批量 commit 的 Tool 不会被后一个失败 Tool 一起回滚。
        if session is not None and callable(getattr(session, "begin_nested", None)):
            async with session.begin_nested():
                handled = await _dispatch_authorized(name, args, runtime_context)
        else:
            handled = await _dispatch_authorized(name, args, runtime_context)

        if reserved and handled.refund_quota:
            # 动作其实没有发生（例如目标已存在），退还预扣配额与回合次数。
            await _refund_admin_quota(session, context.group_id, name)
            reserved = False
        if turn_reserved and handled.refund_quota:
            context.ledger.release_controlled()
        # 代码跑了但报错/超限：handler 返回 result.ok=False。外层也必须是失败，
        # 否则“图画好了”这类声明核验会把一次报错的运行当成成功。
        inner_failed = (
            isinstance(handled.result, dict) and handled.result.get("ok") is False
        )
        await _audit(
            session,
            context.group_id,
            context.actor_user_id,
            name,
            args,
            "error" if inner_failed else "success",
            str(handled.result.get("note") or "") if inner_failed else "",
            duration_ms=(time.monotonic() - started) * 1000,
            error_code=(
                str(handled.result.get("status") or "run_failed")
                if inner_failed
                else None
            ),
        )
        dbg(
            f"群 {context.group_id} 工具 {name} "
            f"{'执行完成但结果失败' if inner_failed else '执行成功'}"
        )
        payload: dict[str, Any] = {"ok": not inner_failed, "result": handled.result}
        if name in _MESSAGE_SEND_TOOLS:
            payload["sent"] = handled.ends_turn
        if writes_state and not inner_failed:
            context.ledger.record_success(name, args, payload)

        # 有 DB 会话时审计本身也是一次待提交写入；state_write 同样只标记
        # needs_commit，由 dialogue 在本轮 Tool 批次末统一提交。特权工具的
        # 配额已在副作用之前提交，这里立即提交的只是审计。
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
        logger.exception(f"群聊 Agent 工具执行失败: {name}")
        # 真 AsyncSession 的 begin_nested 已只回滚当前 SAVEPOINT；兼容没有
        # SAVEPOINT 的测试/旧会话实现时，维持原来的失败后 rollback 语义。
        if session is not None and not callable(getattr(session, "begin_nested", None)):
            with contextlib.suppress(SQLAlchemyError):
                await session.rollback()
        if reserved:
            await _refund_admin_quota(session, context.group_id, name)
        if turn_reserved:
            context.ledger.release_controlled()
        error_code = classify_tool_error(exc)
        await _audit(
            session,
            context.group_id,
            context.actor_user_id,
            name,
            args if isinstance(args, dict) else {},
            "error",
            str(exc),
            duration_ms=(time.monotonic() - started) * 1000,
            error_code=error_code,
        )
        return ToolExecutionResult(
            payload=tool_error_payload(exc),
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
    current_intent_text: str | None = None,
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
        current_intent_text=current_intent_text,
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
    "discovery_segment_types",
    "execute_tool",
    "execute_tool_with_meta",
    "select_critical_tool_names",
    "select_dialogue_message_segment_types",
    "select_dialogue_tool_names",
    "tool_permission_snapshot",
]
