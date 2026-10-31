# ruff: noqa: E501, C901, PLR0912, TID252, TC003
"""私聊 Agent 的工具：全部只作用于**当前私聊的这位用户本人**。

没有任何工具接受 user_id；群相关的工具只能选用户本人活跃所在的群，读 Agent 数据
（摘要、画像、关系）还要求该群开着 Agent、用户没有隐私退出。参数一律先过
``tool_args.validate_tool_arguments``，失败统一返回 ``tool_error_payload``。

签到、好感、记忆都调用已有服务（``checkin_service`` / ``affinity`` /
``memory_self`` / ``private_memory``），这里不另写业务逻辑。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from random import randint
from typing import Any

from sqlalchemy import select

from .. import affinity
from ..checkin_service import (
    MAX_BATCH_GROUPS,
    POINTS_MAX,
    POINTS_MIN,
    checkin_overview,
    perform_checkin,
)
from ..command_catalog import get_registered_command_groups
from ..data_models.agent_memory import AgentMemory
from ..permission import check_feature_permission
from . import private_memory
from .memory_hygiene import contains_pii, looks_like_instruction, scrub_pii
from .memory_self import SELF_FACT_KEYS, forget_member_facts, remember_member_fact
from .private_context import SharedGroup, group_bonds, group_profile_facts
from .tool_args import (
    ERROR_NOT_FOUND,
    ERROR_PERMISSION,
    ToolError,
    tool_error_payload,
    validate_tool_arguments,
)

_FACT_KEY_SCHEMA = {
    "type": "string",
    "enum": list(SELF_FACT_KEYS),
    "description": "display_name昵称/preferred_address希望的称呼/hobby爱好/preference喜好/dislike讨厌的/skill擅长/current_status近期状态",
}
_GROUP_SCHEMA = {
    "type": "string",
    "minLength": 1,
    "maxLength": 60,
    "description": "群名（可以只写一部分）或群号；省略表示全部",
}
_SUMMARY_DIGITS_RE = re.compile(r"(?<!\d)\d{5,12}(?!\d)")
_MAX_SUMMARIES = 3
_SUMMARY_CHARS = 400
_MAX_COMMANDS = 30


@dataclass(frozen=True, slots=True)
class PrivateTool:
    name: str
    description: str
    properties: dict[str, Any] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    writes: bool = False

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.properties,
                    "required": list(self.required),
                    "additionalProperties": False,
                },
            },
        }


PRIVATE_TOOLS: tuple[PrivateTool, ...] = (
    PrivateTool(
        "my_overview",
        "查这位用户的概况：和你的关系等级、在几个群、各群签到/连签/积分合计、你什么时候认识他",
    ),
    PrivateTool(
        "checkin",
        "替这位用户签到（等同他在群里发 /签到）。不指定群时在他所在的所有群里签；今天签过的返回 already",
        {"group": _GROUP_SCHEMA},
        writes=True,
    ),
    PrivateTool(
        "checkin_status",
        "查这位用户在各群的签到情况：今天签没签、连签、累计天数、积分",
        {"group": _GROUP_SCHEMA},
    ),
    PrivateTool(
        "list_my_groups",
        "列出你和这位用户共同在的群，以及每个群里你的 Agent 有没有开",
    ),
    PrivateTool(
        "what_you_remember",
        "查你记得关于这位用户的事：私聊里他告诉你的、你在各群里了解到的画像",
    ),
    PrivateTool(
        "remember_about_me",
        "按这位用户的明确要求记住关于他自己的一件事。also_in_groups 只有他明确说“群里也这样”时才设为 true，会同步到他所在且开了 Agent 的群",
        {
            "key": _FACT_KEY_SCHEMA,
            "content": {"type": "string", "minLength": 1, "maxLength": 100},
            "also_in_groups": {"type": "boolean"},
        },
        ("key", "content"),
        writes=True,
    ),
    PrivateTool(
        "forget_about_me",
        "按这位用户的要求忘掉关于他的记忆；可按类别或关键词只删一部分。include_groups=true 时连他在各群的画像也一起删",
        {
            "key": _FACT_KEY_SCHEMA,
            "keyword": {"type": "string", "minLength": 1, "maxLength": 40},
            "include_groups": {"type": "boolean"},
        },
        writes=True,
    ),
    PrivateTool(
        "our_relationship",
        "查你和这位用户的关系：整体好感等级、各群里的熟悉程度、他怎么叫你、你们的梗",
    ),
    PrivateTool(
        "group_recent",
        "看这位用户所在的某个群最近在聊什么（读你整理的群聊日摘要）",
        {"group": {**_GROUP_SCHEMA, "description": "群名（可以只写一部分）或群号"}},
        ("group",),
    ),
    PrivateTool(
        "list_commands",
        "查现在私聊/群里有哪些命令可用、各自做什么；query 可按关键词筛选",
        {
            "query": {"type": "string", "minLength": 1, "maxLength": 20},
            "scene": {"type": "string", "enum": ["private", "group", "all"]},
        },
    ),
)
TOOLS_BY_NAME: dict[str, PrivateTool] = {tool.name: tool for tool in PRIVATE_TOOLS}
WRITE_TOOLS = frozenset(tool.name for tool in PRIVATE_TOOLS if tool.writes)


def tool_schemas() -> list[dict[str, Any]]:
    return [tool.schema() for tool in PRIVATE_TOOLS]


@dataclass(slots=True)
class PrivateToolContext:
    session: Any
    user_id: int
    bot_id: int | None
    groups: list[SharedGroup]
    now: datetime
    nickname: str | None = None
    is_superuser: bool = False
    # 本轮已经成功的写工具，用于去重与“已完成”声明
    succeeded: set[str] = field(default_factory=set)
    mutated: bool = False


def _match_groups(ctx: PrivateToolContext, raw: str | None) -> list[SharedGroup]:
    if not raw:
        return list(ctx.groups)
    needle = raw.strip().casefold()
    if needle.isdigit():
        hits = [g for g in ctx.groups if str(g.group_id) == needle]
    else:
        exact = [g for g in ctx.groups if g.name.casefold() == needle]
        hits = exact or [g for g in ctx.groups if needle in g.name.casefold()]
    if not hits:
        raise ToolError(
            f"没找到你所在的群「{raw}」",
            code=ERROR_NOT_FOUND,
            hint="先用 list_my_groups 看看他在哪些群",
        )
    return hits


def _single_group(ctx: PrivateToolContext, raw: str) -> SharedGroup:
    hits = _match_groups(ctx, raw)
    if len(hits) > 1:
        names = "、".join(g.name for g in hits[:5])
        raise ToolError(
            f"有多个群匹配「{raw}」：{names}",
            code=ERROR_NOT_FOUND,
            hint="问用户具体是哪个群",
        )
    return hits[0]


async def _overview(ctx: PrivateToolContext) -> dict[str, Any]:
    snap = await affinity.snapshot(ctx.session, ctx.user_id, now=ctx.now)
    statuses = await checkin_overview(ctx.session, ctx.user_id, now=ctx.now)
    from ..data_models.bot_user import BotUser

    user = await ctx.session.get(BotUser, ctx.user_id)
    return {
        "relationship": snap.public_dict(),
        "groups": len(ctx.groups),
        "checked_in_today": sum(1 for s in statuses if s.checked_today),
        "best_streak_days": max((s.streak_days for s in statuses), default=0),
        "total_checkin_days": sum(s.total_days for s in statuses),
        "total_points": sum(s.points for s in statuses),
        "known_since": f"{user.first_interaction_at:%Y-%m-%d}"
        if user is not None and user.first_interaction_at
        else None,
    }


async def _checkin(ctx: PrivateToolContext, group: str | None) -> dict[str, Any]:
    targets = _match_groups(ctx, group)[:MAX_BATCH_GROUPS]
    results: list[dict[str, Any]] = []
    skipped: list[str] = []
    for target in targets:
        if not await check_feature_permission(
            ctx.user_id, target.group_id, "checkin", ctx.session
        ):
            skipped.append(target.name)
            continue
        result = await perform_checkin(
            ctx.session,
            group_id=target.group_id,
            user_id=ctx.user_id,
            nickname=target.nickname,
            now=ctx.now,
            points_reward=randint(POINTS_MIN, POINTS_MAX),
        )
        payload = result.public_dict()
        payload["group_name"] = target.name
        payload.pop("group_id", None)
        results.append(payload)
        if result.ok:
            ctx.mutated = True
    if not results and not skipped:
        raise ToolError(
            "你还没有和我一起在任何群里",
            code=ERROR_NOT_FOUND,
            hint="告诉他先在群里和你说句话",
        )
    response: dict[str, Any] = {"results": results}
    if skipped:
        response["checkin_disabled_in"] = skipped
    return response


async def _checkin_status(ctx: PrivateToolContext, group: str | None) -> dict[str, Any]:
    targets = _match_groups(ctx, group)
    statuses = await checkin_overview(
        ctx.session, ctx.user_id, group_ids=[g.group_id for g in targets], now=ctx.now
    )
    items = []
    for item in statuses:
        payload = item.public_dict()
        payload.pop("group_id", None)
        items.append(payload)
    return {"groups": items}


def _list_groups(ctx: PrivateToolContext) -> dict[str, Any]:
    return {
        "groups": [
            {"name": g.name, "group_id": g.group_id, "agent_on": g.agent_readable}
            for g in ctx.groups
        ]
    }


async def _remembered(ctx: PrivateToolContext) -> dict[str, Any]:
    private = [
        private_memory.fact_view(row)
        for row in await private_memory.list_facts(
            ctx.session, ctx.user_id, now=ctx.now
        )
    ]
    readable = [g for g in ctx.groups if g.agent_readable]
    names = {g.group_id: g.name for g in readable}
    facts = await group_profile_facts(
        ctx.session, ctx.user_id, list(names), now=ctx.now
    )
    return {
        "told_you_in_private": private,
        "learned_in_groups": [
            {
                "key": f["key"],
                "content": f["content"],
                "group": names.get(f["group_id"], ""),
            }
            for f in facts
        ],
    }


async def _remember(ctx: PrivateToolContext, args: dict[str, Any]) -> dict[str, Any]:
    key = str(args["key"])
    try:
        result = await private_memory.remember(
            ctx.session, ctx.user_id, key, str(args["content"]), now=ctx.now
        )
    except ValueError as exc:
        raise ToolError(str(exc), code=ERROR_PERMISSION) from exc
    ctx.mutated = True
    synced: list[str] = []
    if args.get("also_in_groups"):
        for group in ctx.groups:
            if not group.agent_readable:
                continue
            try:
                await remember_member_fact(
                    ctx.session,
                    group.group_id,
                    ctx.user_id,
                    key,
                    result["content"],
                    now=ctx.now,
                )
            except (ValueError, PermissionError):
                continue
            synced.append(group.name)
    payload: dict[str, Any] = {"key": key, "content": result["content"], "saved": True}
    if args.get("also_in_groups"):
        payload["synced_groups"] = synced
    return payload


async def _forget(ctx: PrivateToolContext, args: dict[str, Any]) -> dict[str, Any]:
    key = args.get("key")
    keyword = args.get("keyword")
    result = await private_memory.forget(
        ctx.session, ctx.user_id, key=key, keyword=keyword
    )
    groups_changed: list[str] = []
    if args.get("include_groups"):
        for group in ctx.groups:
            outcome = await forget_member_facts(
                ctx.session, group.group_id, ctx.user_id, raw_key=key, keyword=keyword
            )
            if outcome.get("changed"):
                groups_changed.append(group.name)
    if result["changed"] or groups_changed:
        ctx.mutated = True
    payload: dict[str, Any] = {
        "private_removed": result["removed"],
        "private_trimmed": result["trimmed"],
        "changed": bool(result["changed"] or groups_changed),
    }
    if result["protected"]:
        payload["kept_admin_entries"] = result["protected"]
    if args.get("include_groups"):
        payload["groups_changed"] = groups_changed
    return payload


async def _relationship(ctx: PrivateToolContext) -> dict[str, Any]:
    snap = await affinity.snapshot(ctx.session, ctx.user_id, now=ctx.now)
    bonds = await group_bonds(
        ctx.session, ctx.user_id, ctx.bot_id, ctx.groups, now=ctx.now
    )
    return {"overall": snap.public_dict(), "in_groups": bonds}


def _clean_summary(text: str) -> str | None:
    value = str(text or "").strip()
    if not value or looks_like_instruction(value):
        return None
    value = _SUMMARY_DIGITS_RE.sub(
        "[号码]", scrub_pii(value) if contains_pii(value) else value
    )
    return value[:_SUMMARY_CHARS]


async def _group_recent(ctx: PrivateToolContext, raw: str) -> dict[str, Any]:
    group = _single_group(ctx, raw)
    if not group.agent_readable:
        raise ToolError(
            f"「{group.name}」没开我的群聊功能，或你在那个群关闭了我的记忆，我看不到那里的聊天",
            code=ERROR_PERMISSION,
        )
    rows = (
        (
            await ctx.session.execute(
                select(AgentMemory)
                .where(
                    AgentMemory.group_id == group.group_id,
                    AgentMemory.subject_user_id == 0,
                    AgentMemory.memory_type == "summary",
                    AgentMemory.memory_key.like("daily:%")
                    | AgentMemory.memory_key.like("weekly:%"),
                )
                .order_by(AgentMemory.memory_key.desc())
                .limit(_MAX_SUMMARIES * 2)
            )
        )
        .scalars()
        .all()
    )
    summaries = []
    for row in rows:
        if row.expires_at is not None and row.expires_at < ctx.now:
            continue
        text = _clean_summary(str(row.content or ""))
        if text:
            summaries.append(
                {"period": str(row.memory_key).split(":", 1)[-1], "summary": text}
            )
        if len(summaries) >= _MAX_SUMMARIES:
            break
    return {"group": group.name, "summaries": summaries, "empty": not summaries}


async def _commands(ctx: PrivateToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ..command_access import CommandAccessContext
    from ..permission import get_user_feature_status

    scene = str(args.get("scene") or "all")
    query = str(args.get("query") or "").strip().casefold()
    statuses = await get_user_feature_status(ctx.user_id, None, ctx.session)
    enabled = frozenset(key for key, _d, on, _s in statuses if on)
    access = CommandAccessContext(
        user_id=ctx.user_id,
        group_id=None,
        enabled_features=enabled,
        is_superuser=ctx.is_superuser,
    )
    items: list[dict[str, Any]] = []
    for group in get_registered_command_groups():
        for spec in group.commands:
            if spec.permission != "everyone" and not ctx.is_superuser:
                continue
            if scene != "all" and spec.scope not in {scene, "all"}:
                continue
            if not access.feature_enabled(spec.feature):
                continue
            text = f"{spec.name} {' '.join(spec.aliases)} {spec.description} {group.display_name}".casefold()
            if query and query not in text:
                continue
            items.append(
                {
                    "command": f"/{spec.name}",
                    "aliases": [f"/{alias}" for alias in spec.aliases[:3]],
                    "what": spec.description,
                    "where": {"group": "群聊", "private": "私聊"}.get(
                        spec.scope, "群聊/私聊"
                    ),
                    "module": group.display_name,
                }
            )
    return {
        "commands": items[:_MAX_COMMANDS],
        "more": max(len(items) - _MAX_COMMANDS, 0),
    }


async def execute_private_tool(
    name: str, raw_args: Any, ctx: PrivateToolContext
) -> dict[str, Any]:
    """执行一个私聊工具，返回给模型的 payload（``ok`` + ``result`` 或错误）。"""

    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return {"ok": False, "error": f"没有工具 {name}", "error_code": "invalid_args"}
    try:
        args = validate_tool_arguments(
            name, tool.properties, tool.required, raw_args or {}
        )
        if name == "my_overview":
            result = await _overview(ctx)
        elif name == "checkin":
            result = await _checkin(ctx, args.get("group"))
        elif name == "checkin_status":
            result = await _checkin_status(ctx, args.get("group"))
        elif name == "list_my_groups":
            result = _list_groups(ctx)
        elif name == "what_you_remember":
            result = await _remembered(ctx)
        elif name == "remember_about_me":
            result = await _remember(ctx, args)
        elif name == "forget_about_me":
            result = await _forget(ctx, args)
        elif name == "our_relationship":
            result = await _relationship(ctx)
        elif name == "group_recent":
            result = await _group_recent(ctx, str(args["group"]))
        else:
            result = await _commands(ctx, args)
    except Exception as exc:  # noqa: BLE001
        return tool_error_payload(exc)
    if tool.writes:
        ctx.succeeded.add(name)
    else:
        ctx.succeeded.add(name)
    return {"ok": True, "result": result}


__all__ = [
    "PRIVATE_TOOLS",
    "TOOLS_BY_NAME",
    "WRITE_TOOLS",
    "PrivateTool",
    "PrivateToolContext",
    "execute_private_tool",
    "tool_schemas",
]
