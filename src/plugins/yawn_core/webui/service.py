# ruff: noqa: FBT001,TC001,TC002,TID252
"""WebUI 查询与写入服务。"""

from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from nonebot import get_bots
from nonebot_plugin_orm import get_session
from sqlalchemy import String, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..data_models.agent_audit import AgentAudit
from ..data_models.agent_memory import AgentMemory, AgentPrivacy, AgentRelation
from ..data_models.bot_group import BotGroup
from ..data_models.bot_user import BotUser
from ..data_models.global_user_feature import GlobalUserFeature
from ..data_models.group_agent_config import GroupAgentConfig
from ..data_models.group_agent_message import GroupAgentMessage
from ..data_models.group_feature import GroupFeature
from ..data_models.guest_access import GuestGroupAccess
from ..data_models.scheduled_reminder import ScheduledReminder
from ..data_models.user_feature import UserFeature
from ..data_models.user_group import UserGroup
from ..data_models.web_admin_audit import WebAdminAudit
from ..llm import resolve_llm_request, resolve_provider
from ..metrics import ai_health_snapshot, snapshot_metrics, summarize_ai_metrics
from ..permission import FEATURE_REGISTRY
from ..yawn_agent.config_store import agent_runtime_enabled
from ..yawn_agent.conversation import current_conversation
from ..yawn_agent.emotion import emotion_public_state
from ..yawn_agent.memory import compacting_group_count, is_memory_compacting
from ..yawn_agent.persona import (
    PERSONA_SCHEMA_VERSION,
    persona_behavior,
    persona_editor_profile,
    persona_preset_payloads,
    persona_summary,
    resolve_persona,
)

ADMIN_TOOLS = frozenset({"mute_member", "create_group_announcement"})


# 全库约定 naive datetime 为北京时间（UTC+8），序列化时必须按此时区标注，
# 否则前端会把库里的北京时间当作 UTC 显示，整体偏移 8 小时。
BEIJING_TZ = timezone(timedelta(hours=8))


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=BEIJING_TZ)
    return value.isoformat()


def version(value: datetime | None) -> str | None:
    return iso(value)


def page_meta(page: int, page_size: int, total: int) -> dict[str, int]:
    return {"page": page, "pageSize": page_size, "total": total}


# 概览统计的进程内状态。hub 每 5 秒轮询一次 overview()，
# DB 聚合走 15 秒 TTL 缓存，把查询压力收敛为每 15 秒一轮；
# bots/plugins/metrics 快照与 uptime 每次现算，保持实时口径。
_LOADED_AT = datetime.now(BEIJING_TZ)
_STATS_TTL_SECONDS = 15.0
_stats_state: dict[str, Any] = {"cache": None, "expires_at": 0.0, "lock": None}

# 子插件状态模块的懒解析结果；只在解析成功后缓存，失败下次重试
# （与 webui/games.py 同模式；此处不能 import games.py，会形成循环导入）。
_live_state_modules: dict[str, Any] = {}


def _llm_runtime_status() -> dict[str, Any]:
    """返回 WebUI 可展示的 LLM 路由状态；绝不暴露 API Key。"""

    tasks = ("agent_dialogue", "agent_proactive", "agent_memory", "agent_image")
    routes: list[dict[str, Any]] = []
    missing: set[str] = set()
    for task in tasks:
        request = resolve_llm_request(task)
        base_url, api_key = resolve_provider(request.provider)
        configured = bool(base_url.strip() and api_key and request.model.strip())
        if not configured:
            missing.add(request.provider)
        routes.append(
            {
                "task": task,
                "profile": request.profile,
                "provider": request.provider,
                "model": request.model,
                "thinking": request.thinking,
                "multimodal": request.multimodal,
                "configured": configured,
            }
        )
    return {"routes": routes, "unconfiguredProviders": sorted(missing)}


def _import_rpg_state() -> Any | None:
    try:
        from ..yawn_rpg import state as module  # pyright: ignore[reportMissingImports]
    except Exception:  # noqa: BLE001
        return None
    return module


def _import_werewolf_state() -> Any | None:
    try:
        from ..yawn_werewolf import (
            state as module,  # pyright: ignore[reportMissingImports]
        )
    except Exception:  # noqa: BLE001
        return None
    return module


def _live_state(kind: str) -> Any | None:
    """按需解析跑团/狼人杀的进程内对局注册表；子插件缺失时返回 None。"""

    if kind not in _live_state_modules:
        module = _import_rpg_state() if kind == "rpg" else _import_werewolf_state()
        if module is None:
            return None
        _live_state_modules[kind] = module
    return _live_state_modules[kind]


def _live_game_count(kind: str) -> dict[str, Any]:
    module = _live_state(kind)
    if module is None:
        return {"available": False, "count": 0}
    try:
        count = len(module.all_games())
    except Exception:  # noqa: BLE001
        count = 0
    return {"available": True, "count": count}


async def _db_stats(now: datetime) -> dict[str, Any]:
    """一轮 DB 聚合统计；由 ``_cached_db_stats`` 负责 TTL 缓存。"""

    cutoff_24h = now - timedelta(hours=24)
    # 北京时间当日零点；库内时间列按约定为 naive 北京时间。
    day_start = datetime(now.year, now.month, now.day)  # noqa: DTZ001
    today = now.strftime("%Y-%m-%d")

    async with get_session() as session:
        messages_24h, active_groups_24h = (
            await session.execute(
                select(
                    func.count(GroupAgentMessage.id),
                    func.count(func.distinct(GroupAgentMessage.group_id)),
                ).where(GroupAgentMessage.received_at >= cutoff_24h)
            )
        ).one()
        response_groups_24h = int(
            await session.scalar(
                select(func.count())
                .select_from(GroupAgentConfig)
                .where(GroupAgentConfig.last_response_at >= cutoff_24h)
            )
            or 0
        )
        proactive_sum = select(
            func.coalesce(func.sum(GroupAgentConfig.proactive_count), 0)
        ).where(GroupAgentConfig.proactive_day == today)
        proactive_today = int(await session.scalar(proactive_sum) or 0)
        admin_tool_sum = select(
            func.coalesce(func.sum(GroupAgentConfig.admin_tool_count), 0)
        ).where(GroupAgentConfig.tool_day == today)
        admin_tool_today = int(await session.scalar(admin_tool_sum) or 0)
        rebuild_required = int(
            await session.scalar(
                select(func.count())
                .select_from(GroupAgentConfig)
                .where(GroupAgentConfig.memory_rebuild_required.is_(True))
            )
            or 0
        )
        failing_groups = int(
            await session.scalar(
                select(func.count())
                .select_from(GroupAgentConfig)
                .where(GroupAgentConfig.memory_consecutive_failures > 0)
            )
            or 0
        )
        recent_error_row = (
            await session.execute(
                select(
                    GroupAgentConfig.group_id,
                    GroupAgentConfig.memory_last_error,
                    GroupAgentConfig.memory_last_attempt_at,
                )
                .where(GroupAgentConfig.memory_last_error.is_not(None))
                .order_by(
                    GroupAgentConfig.memory_last_attempt_at.desc().nulls_last()
                )
                .limit(1)
            )
        ).first()
        reminder_errors = int(
            await session.scalar(
                select(func.count())
                .select_from(ScheduledReminder)
                .where(
                    ScheduledReminder.enabled.is_(True),
                    ScheduledReminder.last_error.is_not(None),
                )
            )
            or 0
        )
        rpg_ended_today = await _ended_today(session, "rpg", day_start)
        werewolf_ended_today = await _ended_today(session, "werewolf", day_start)
        fanqie = await _fanqie_status_counts(session)

    recent_error = None
    if recent_error_row is not None:
        group_id, error, attempted = recent_error_row
        recent_error = {"groupId": str(group_id), "error": error, "at": iso(attempted)}

    return {
        "activity": {
            "messages24h": int(messages_24h),
            "activeGroups24h": int(active_groups_24h),
            "agentResponseGroups24h": response_groups_24h,
            "proactiveToday": proactive_today,
            "adminToolToday": admin_tool_today,
        },
        "memory": {
            "rebuildRequired": rebuild_required,
            "failingGroups": failing_groups,
            "recentError": recent_error,
        },
        "games": {
            "live": {
                "rpg": _live_game_count("rpg"),
                "werewolf": _live_game_count("werewolf"),
            },
            "endedToday": {"rpg": rpg_ended_today, "werewolf": werewolf_ended_today},
        },
        "jobs": {
            "fanqie": fanqie,
            "reminderErrors": reminder_errors,
        },
    }


async def _ended_today(
    session: AsyncSession, kind: str, day_start: datetime
) -> int | None:
    """统计今日已结束对局；子插件未加载或表不可用时返回 None 表示口径不可用。"""

    if kind == "rpg":
        try:
            from ..yawn_rpg.models import RPGGame
        except Exception:  # noqa: BLE001
            return None
        model, ended_at = RPGGame, RPGGame.ended_at
    else:
        try:
            from ..yawn_werewolf.models import WerewolfGame
        except Exception:  # noqa: BLE001
            return None
        model, ended_at = WerewolfGame, WerewolfGame.ended_at
    try:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(model)
                .where(ended_at.is_not(None), ended_at >= day_start)
            )
            or 0
        )
    except Exception:  # noqa: BLE001
        await session.rollback()
        return None


async def _fanqie_status_counts(session: AsyncSession) -> dict[str, Any]:
    """番茄任务按状态计数；子插件未加载或表不可用时标记 available=False。"""

    try:
        from ..yawn_fanqie.models import FanqieJob
    except Exception:  # noqa: BLE001
        return {"available": False, "byStatus": {}}
    try:
        rows = (
            await session.execute(
                select(FanqieJob.status, func.count()).group_by(FanqieJob.status)
            )
        ).all()
    except Exception:  # noqa: BLE001
        await session.rollback()
        return {"available": False, "byStatus": {}}
    return {
        "available": True,
        "byStatus": {str(status): int(count) for status, count in rows},
    }


def _stats_cache_fresh() -> bool:
    return (
        _stats_state["cache"] is not None
        and time.monotonic() < _stats_state["expires_at"]
    )


async def _cached_db_stats(now: datetime) -> dict[str, Any]:
    """带 TTL 的 DB 统计入口；并发时只放一个聚合在跑。"""

    if _stats_state["lock"] is None:
        _stats_state["lock"] = asyncio.Lock()
    lock: asyncio.Lock = _stats_state["lock"]
    if _stats_cache_fresh():
        return _stats_state["cache"]
    async with lock:
        if _stats_cache_fresh():
            return _stats_state["cache"]
        stats = await _db_stats(now)
        _stats_state["cache"] = stats
        _stats_state["expires_at"] = time.monotonic() + _STATS_TTL_SECONDS
        return stats


def reset_stats_cache_for_tests() -> None:
    """清空概览统计缓存；只供测试隔离使用。"""

    _stats_state["cache"] = None
    _stats_state["expires_at"] = 0.0


async def overview() -> dict[str, Any]:
    async with get_session() as session:
        group_count = int(
            await session.scalar(select(func.count()).select_from(BotGroup)) or 0
        )
        user_count = int(
            await session.scalar(select(func.count()).select_from(BotUser)) or 0
        )
        enabled_agents = int(
            await session.scalar(
                select(func.count())
                .select_from(GroupAgentConfig)
                .where(
                    GroupAgentConfig.enabled.is_(True),
                    ~exists(
                        select(GroupFeature.group_id).where(
                            GroupFeature.group_id == GroupAgentConfig.group_id,
                            GroupFeature.feature == "group_agent",
                            GroupFeature.enabled.is_(False),
                        )
                    ),
                )
            )
            or 0
        )
        recent = list(
            (
                await session.execute(
                    select(AgentAudit).order_by(AgentAudit.id.desc()).limit(5)
                )
            )
            .scalars()
            .all()
        )
    from .. import get_sub_plugin_load_report

    report = get_sub_plugin_load_report()
    wanted = {"群聊 Agent", "狼人杀", "跑团", "番茄小说"}
    plugins = [
        {"name": "Core", "state": "loaded", "detail": None},
        *[
            {"name": item.label, "state": item.state, "detail": item.detail}
            for item in report
            if item.label in wanted
        ],
    ]
    now = datetime.now(BEIJING_TZ)
    # DB 列为 naive 北京时间（见 now_beijing 约定），聚合查询必须用同口径比较。
    db_stats = await _cached_db_stats(now.replace(tzinfo=None))
    metrics_snapshot = snapshot_metrics()
    memory_stats = dict(db_stats["memory"])
    memory_stats["compactingGroups"] = compacting_group_count()
    llm_stats = _llm_runtime_status()
    return {
        "bots": [str(bot_id) for bot_id in sorted(get_bots())],
        "plugins": plugins,
        "counts": {
            "groups": group_count,
            "users": user_count,
            "enabledAgents": enabled_agents,
        },
        "recentAgentActions": [serialize_agent_audit(row) for row in recent],
        "metrics": metrics_snapshot,
        "stats": {
            "ai": {
                **summarize_ai_metrics(metrics_snapshot),
                "health": ai_health_snapshot(),
            },
            "llm": llm_stats,
            "activity": db_stats["activity"],
            "memory": memory_stats,
            "games": db_stats["games"],
            "jobs": db_stats["jobs"],
            "uptime": {
                "startedAt": _LOADED_AT.isoformat(),
                "uptimeSeconds": (now - _LOADED_AT).total_seconds(),
            },
        },
        "generatedAt": now.isoformat(),
    }


async def list_groups(
    session: AsyncSession, *, page: int, page_size: int, search: str
) -> tuple[list[dict[str, Any]], int]:
    conditions = []
    if search:
        pattern = f"%{search}%"
        conditions.append(
            or_(
                BotGroup.group_name.ilike(pattern),
                BotGroup.group_id.cast(String).like(pattern),
            )
        )
    count_stmt = select(func.count()).select_from(BotGroup)
    stmt = select(BotGroup).order_by(BotGroup.last_active_at.desc(), BotGroup.group_id)
    if conditions:
        count_stmt = count_stmt.where(*conditions)
        stmt = stmt.where(*conditions)
    total = int(await session.scalar(count_stmt) or 0)
    rows = list(
        (await session.execute(stmt.offset((page - 1) * page_size).limit(page_size)))
        .scalars()
        .all()
    )
    group_ids = [row.group_id for row in rows]
    member_counts: dict[int, int] = {}
    configs: dict[int, GroupAgentConfig] = {}
    agent_feature_overrides: dict[int, bool] = {}
    if group_ids:
        member_counts = {
            int(group_id): int(count)
            for group_id, count in (
                await session.execute(
                    select(UserGroup.group_id, func.count())
                    .where(UserGroup.group_id.in_(group_ids))
                    .group_by(UserGroup.group_id)
                )
            ).all()
        }
        configs = {
            row.group_id: row
            for row in (
                await session.execute(
                    select(GroupAgentConfig).where(
                        GroupAgentConfig.group_id.in_(group_ids)
                    )
                )
            )
            .scalars()
            .all()
        }
        agent_feature_overrides = {
            int(row.group_id): bool(row.enabled)
            for row in (
                await session.execute(
                    select(GroupFeature).where(
                        GroupFeature.group_id.in_(group_ids),
                        GroupFeature.feature == "group_agent",
                    )
                )
            )
            .scalars()
            .all()
        }
    return [
        {
            "groupId": str(row.group_id),
            "groupName": row.group_name,
            "firstSeenAt": iso(row.first_seen_at),
            "lastActiveAt": iso(row.last_active_at),
            "memberCount": member_counts.get(row.group_id, 0),
            "agentEnabled": bool(
                (configs[row.group_id].enabled if row.group_id in configs else True)
                and agent_feature_overrides.get(int(row.group_id), True)
            ),
        }
        for row in rows
    ], total


async def list_guest_groups(
    session: AsyncSession, *, page: int, page_size: int, search: str
) -> tuple[list[dict[str, Any]], int]:
    """Return only groups explicitly allowlisted for guest viewers.

    Keep this projection intentionally small: guest navigation only needs a human
    readable group identity and member count, not Agent runtime/configuration data.
    """
    conditions = []
    if search:
        pattern = f"%{search}%"
        conditions.append(
            or_(
                BotGroup.group_name.ilike(pattern),
                BotGroup.group_id.cast(String).like(pattern),
            )
        )
    base = BotGroup.__table__.join(
        GuestGroupAccess.__table__, GuestGroupAccess.group_id == BotGroup.group_id
    )
    count_stmt = select(func.count()).select_from(base)
    stmt = (
        select(BotGroup)
        .join(GuestGroupAccess, GuestGroupAccess.group_id == BotGroup.group_id)
        .order_by(BotGroup.last_active_at.desc(), BotGroup.group_id)
    )
    if conditions:
        count_stmt = count_stmt.where(*conditions)
        stmt = stmt.where(*conditions)
    total = int(await session.scalar(count_stmt) or 0)
    rows = list(
        (await session.execute(stmt.offset((page - 1) * page_size).limit(page_size)))
        .scalars()
        .all()
    )
    group_ids = [int(row.group_id) for row in rows]
    member_counts: dict[int, int] = {}
    if group_ids:
        member_counts = {
            int(group_id): int(count)
            for group_id, count in (
                await session.execute(
                    select(UserGroup.group_id, func.count())
                    .where(UserGroup.group_id.in_(group_ids))
                    .group_by(UserGroup.group_id)
                )
            ).all()
        }
    return [
        {
            "groupId": str(row.group_id),
            "groupName": row.group_name,
            "memberCount": member_counts.get(int(row.group_id), 0),
        }
        for row in rows
    ], total


async def get_group(session: AsyncSession, group_id: int) -> dict[str, Any] | None:
    row = await session.get(BotGroup, group_id)
    if row is None:
        return None
    member_count = int(
        await session.scalar(
            select(func.count())
            .select_from(UserGroup)
            .where(UserGroup.group_id == group_id)
        )
        or 0
    )
    return {
        "groupId": str(row.group_id),
        "groupName": row.group_name,
        "firstSeenAt": iso(row.first_seen_at),
        "lastActiveAt": iso(row.last_active_at),
        "memberCount": member_count,
        "features": await group_feature_rows(session, group_id),
    }


async def list_group_members(
    session: AsyncSession, group_id: int, *, page: int, page_size: int, search: str
) -> tuple[list[dict[str, Any]], int]:
    stmt = (
        select(UserGroup, BotUser)
        .join(BotUser, BotUser.user_id == UserGroup.user_id)
        .where(UserGroup.group_id == group_id)
    )
    count_stmt = (
        select(func.count())
        .select_from(UserGroup)
        .where(UserGroup.group_id == group_id)
    )
    if search:
        pattern = f"%{search}%"
        clause = or_(
            BotUser.nickname.ilike(pattern),
            UserGroup.group_nickname.ilike(pattern),
            BotUser.user_id.cast(String).like(pattern),
        )
        stmt = stmt.where(clause)
        count_stmt = count_stmt.join(
            BotUser, BotUser.user_id == UserGroup.user_id
        ).where(clause)
    total = int(await session.scalar(count_stmt) or 0)
    rows = (
        await session.execute(
            stmt.order_by(UserGroup.last_seen_at.desc(), UserGroup.user_id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return [
        {
            "userId": str(user.user_id),
            "nickname": user.nickname,
            "groupNickname": membership.group_nickname,
            "role": membership.role,
            "title": membership.title,
            "lastSeenAt": iso(membership.last_seen_at),
            "active": membership.is_active,
        }
        for membership, user in rows
    ], total


async def list_users(
    session: AsyncSession, *, page: int, page_size: int, search: str
) -> tuple[list[dict[str, Any]], int]:
    stmt = select(BotUser).order_by(BotUser.last_interaction_at.desc(), BotUser.user_id)
    count_stmt = select(func.count()).select_from(BotUser)
    if search:
        pattern = f"%{search}%"
        clause = or_(
            BotUser.nickname.ilike(pattern),
            BotUser.user_id.cast(String).like(pattern),
        )
        stmt = stmt.where(clause)
        count_stmt = count_stmt.where(clause)
    total = int(await session.scalar(count_stmt) or 0)
    rows = list(
        (await session.execute(stmt.offset((page - 1) * page_size).limit(page_size)))
        .scalars()
        .all()
    )
    return [
        {
            "userId": str(row.user_id),
            "nickname": row.nickname,
            "firstInteractionAt": iso(row.first_interaction_at),
            "lastInteractionAt": iso(row.last_interaction_at),
            "affinity": row.affinity,
        }
        for row in rows
    ], total


async def group_feature_rows(
    session: AsyncSession, group_id: int
) -> list[dict[str, Any]]:
    overrides = {
        row.feature: row
        for row in (
            await session.execute(
                select(GroupFeature).where(GroupFeature.group_id == group_id)
            )
        )
        .scalars()
        .all()
    }
    agent_config = await session.get(GroupAgentConfig, group_id)
    result: list[dict[str, Any]] = []
    for key, name in FEATURE_REGISTRY.items():
        override = overrides[key].enabled if key in overrides else None
        effective = override if override is not None else True
        source = "group" if key in overrides else "default"
        if (
            key == "group_agent"
            and agent_config is not None
            and not agent_config.enabled
        ):
            effective = False
            # 兼容历史分叉数据：专用开关已经关闭时，群功能页也直接显示关闭。
            if override is None or override is True:
                override = False
                source = "agent_config"
        result.append(
            {
                "key": key,
                "name": name,
                "override": override,
                "effective": bool(effective),
                "source": source,
            }
        )
    return result


async def user_feature_rows(
    session: AsyncSession, user_id: int, group_id: int | None
) -> list[dict[str, Any]]:
    if group_id is None:
        overrides = {
            row.feature: row
            for row in (
                await session.execute(
                    select(GlobalUserFeature).where(
                        GlobalUserFeature.user_id == user_id
                    )
                )
            )
            .scalars()
            .all()
        }
        return [
            {
                "key": key,
                "name": name,
                "override": overrides[key].enabled if key in overrides else None,
                "effective": overrides[key].enabled if key in overrides else True,
                "source": "global_user" if key in overrides else "default",
            }
            for key, name in FEATURE_REGISTRY.items()
        ]
    group_overrides = {
        row.feature: row
        for row in (
            await session.execute(
                select(GroupFeature).where(GroupFeature.group_id == group_id)
            )
        )
        .scalars()
        .all()
    }
    overrides = {
        row.feature: row
        for row in (
            await session.execute(
                select(UserFeature).where(
                    UserFeature.group_id == group_id, UserFeature.user_id == user_id
                )
            )
        )
        .scalars()
        .all()
    }
    agent_config = await session.get(GroupAgentConfig, group_id)
    group_agent_master = bool(
        (agent_config.enabled if agent_config is not None else True)
        and (
            group_overrides["group_agent"].enabled
            if "group_agent" in group_overrides
            else True
        )
    )
    result = []
    for key, name in FEATURE_REGISTRY.items():
        user_row = overrides.get(key)
        group_row = group_overrides.get(key)
        effective = (
            user_row.enabled if user_row else group_row.enabled if group_row else True
        )
        source = "user" if user_row else "group" if group_row else "default"
        if key == "group_agent" and not group_agent_master:
            # 群级总开关是硬门禁；成员显式开启只能在群级允许时生效。
            effective = False
            source = "group"
        result.append(
            {
                "key": key,
                "name": name,
                "override": user_row.enabled if user_row else None,
                "effective": effective,
                "source": source,
            }
        )
    return result


async def set_group_feature(
    session: AsyncSession, group_id: int, feature: str, override: bool | None
) -> None:
    row = await session.get(GroupFeature, {"group_id": group_id, "feature": feature})
    if override is None:
        if row is not None:
            await session.delete(row)
        return
    if row is None:
        session.add(GroupFeature(group_id=group_id, feature=feature, enabled=override))
    else:
        row.enabled = override


async def set_user_feature(
    session: AsyncSession,
    user_id: int,
    feature: str,
    override: bool | None,
    *,
    group_id: int | None,
) -> None:
    if group_id is None:
        row = await session.get(
            GlobalUserFeature, {"user_id": user_id, "feature": feature}
        )
        if override is None:
            if row is not None:
                await session.delete(row)
        elif row is None:
            session.add(
                GlobalUserFeature(user_id=user_id, feature=feature, enabled=override)
            )
        else:
            row.enabled = override
        return
    row = await session.get(
        UserFeature, {"group_id": group_id, "user_id": user_id, "feature": feature}
    )
    if override is None:
        if row is not None:
            await session.delete(row)
    elif row is None:
        session.add(
            UserFeature(
                group_id=group_id, user_id=user_id, feature=feature, enabled=override
            )
        )
    else:
        row.enabled = override


def serialize_agent_config(
    row: GroupAgentConfig | None, group_id: int
) -> dict[str, Any]:
    if row is None:
        # SQLAlchemy column defaults are applied on INSERT, not on plain construction.
        # Return the real runtime defaults for a group that has no persisted config yet.
        return {
            "groupId": str(group_id),
            "enabled": True,
            "triggerMode": "mention_or_proactive",  # deprecated compatibility output
            "replyTriggerEnabled": True,
            "explicitWakeupEnabled": True,
            "proactiveEnabled": True,
            "proactiveProbability": 0.35,
            "proactiveActiveEnabled": True,
            "shortConversationEnabled": True,
            "proactiveActiveProbability": 0.25,
            "proactiveActiveWindowMinutes": 12,
            "idleThresholdMinutes": 15,
            "cooldownMinutes": 8,
            "dailyLimit": 30,
            "rawRetentionDays": 7,
            "crossGroupVisibility": "public_summary",
            "mediaCacheEnabled": False,
            "adminToolDailyLimit": 30,
            "toolAllowlist": ["mute_member", "create_group_announcement"],
            "proactiveToday": 0,
            "adminToolsToday": 0,
            "version": None,
        }
    return {
        "groupId": str(group_id),
        "enabled": row.enabled,
        "triggerMode": row.trigger_mode,  # deprecated compatibility output
        "replyTriggerEnabled": row.reply_trigger_enabled,
        "explicitWakeupEnabled": row.explicit_wakeup_enabled,
        "proactiveEnabled": row.proactive_enabled,
        "proactiveProbability": row.proactive_probability,
        "proactiveActiveEnabled": row.proactive_active_enabled,
        "shortConversationEnabled": row.short_conversation_enabled,
        "proactiveActiveProbability": row.proactive_active_probability,
        "proactiveActiveWindowMinutes": row.proactive_active_window_minutes,
        "idleThresholdMinutes": row.idle_threshold_minutes,
        "cooldownMinutes": row.cooldown_minutes,
        "dailyLimit": row.daily_limit,
        "rawRetentionDays": row.raw_retention_days,
        "crossGroupVisibility": row.cross_group_visibility,
        "mediaCacheEnabled": row.media_cache_enabled,
        "adminToolDailyLimit": row.admin_tool_daily_limit,
        "toolAllowlist": list(row.tool_allowlist or []),
        "proactiveToday": row.proactive_count,
        "adminToolsToday": row.admin_tool_count,
        "version": version(row.updated_at),
    }


def serialize_persona(row: GroupAgentConfig | None, group_id: int) -> dict[str, Any]:
    editor = persona_editor_profile(row)
    return {
        "groupId": str(group_id),
        "enabled": row.persona_enabled if row else False,
        "schemaVersion": PERSONA_SCHEMA_VERSION,
        "profile": editor.model_dump(by_alias=True),
        "presets": persona_preset_payloads(),
        "summary": persona_summary(row),
        "behavior": persona_behavior(row).as_dict(),
        "emotion": emotion_public_state(
            row.emotion_state if row and isinstance(row.emotion_state, dict) else {},
            expressiveness=editor.expressiveness,
        ),
        "resolved": resolve_persona(row),
        "version": version(row.updated_at) if row else None,
    }


def serialize_memory(row: AgentMemory) -> dict[str, Any]:
    """Administrator projection with full memory provenance/governance fields."""

    return {
        "id": str(row.id),
        "groupId": str(row.group_id) if row.group_id is not None else None,
        "subjectUserId": str(row.subject_user_id)
        if int(row.subject_user_id or 0) != 0
        else None,
        "scope": row.scope,
        "type": row.memory_type,
        "key": row.memory_key,
        "content": row.content,
        "sourceKind": row.source_kind,
        "evidenceMessageIds": [
            str(value) for value in row.evidence_message_ids or []
        ],
        "provenance": {
            "kind": row.source_kind,
            "evidenceCount": len(row.evidence_message_ids or []),
            "firstObservedAt": iso(row.created_at),
            "lastConfirmedAt": iso(row.updated_at),
        },
        "relatedUserIds": [str(value) for value in row.related_user_ids or []],
        "salience": row.salience,
        "confidence": row.confidence,
        "visibility": row.visibility,
        "createdAt": iso(row.created_at),
        "updatedAt": iso(row.updated_at),
        "expiresAt": iso(row.expires_at),
    }


def serialize_guest_memory(row: AgentMemory) -> dict[str, Any]:
    """Minimal human-readable memory projection for guest viewers.

    Resource identity and the subject are retained so the read-only UI can group
    and render memories, while evidence ids, provenance, source/debug metadata,
    related-user internals and governance fields stay administrator-only.
    """

    return {
        "id": str(row.id),
        "subjectUserId": str(row.subject_user_id)
        if int(row.subject_user_id or 0) != 0
        else None,
        "type": row.memory_type,
        "key": row.memory_key,
        "content": row.content,
        "confidence": row.confidence,
        "updatedAt": iso(row.updated_at),
    }


def serialize_relation(row: AgentRelation) -> dict[str, Any]:
    """Administrator projection with relation evidence/source metadata."""

    return {
        "id": str(row.id),
        "groupId": str(row.group_id),
        "subjectUserId": str(row.subject_user_id),
        "objectUserId": str(row.object_user_id),
        "type": row.relation_type,
        "sourceKind": row.source_kind,
        "note": row.note,
        "confidence": row.confidence,
        "evidenceCount": row.evidence_count,
        "lastSeenAt": iso(row.last_seen_at),
    }


def serialize_guest_relation(row: AgentRelation) -> dict[str, Any]:
    """Minimal relation projection for guest viewers."""

    return {
        "id": str(row.id),
        "subjectUserId": str(row.subject_user_id),
        "objectUserId": str(row.object_user_id),
        "type": row.relation_type,
        "note": row.note,
        "confidence": row.confidence,
        "lastSeenAt": iso(row.last_seen_at),
    }


# 图谱端点与导出端点同口径：全量边上限 5000，超出时以 meta.truncated 告知前端。
RELATION_GRAPH_LIMIT = 5000


async def load_relation_graph(
    session: AsyncSession, group_id: int, *, guest: bool = False
) -> dict[str, Any]:
    """关系图谱数据。

    管理员保留全群成员节点以支持治理视图；访客仅返回实际出现在可展示
    关系边中的节点，避免通过孤立节点泄露 opted-out 或无关群成员。
    """

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
    relation_clauses = [AgentRelation.group_id == group_id]
    if opted_out:
        relation_clauses.extend(
            (
                AgentRelation.subject_user_id.not_in(opted_out),
                AgentRelation.object_user_id.not_in(opted_out),
            )
        )
    relation_rows = list(
        (
            await session.execute(
                select(AgentRelation)
                .where(*relation_clauses)
                .order_by(AgentRelation.id)
                .limit(RELATION_GRAPH_LIMIT + 1)
            )
        )
        .scalars()
        .all()
    )
    relation_truncated = len(relation_rows) > RELATION_GRAPH_LIMIT
    if relation_truncated:
        relation_rows = relation_rows[:RELATION_GRAPH_LIMIT]

    degrees: dict[int, int] = {}
    for row in relation_rows:
        degrees[int(row.subject_user_id)] = (
            degrees.get(int(row.subject_user_id), 0) + 1
        )
        degrees[int(row.object_user_id)] = degrees.get(int(row.object_user_id), 0) + 1

    member_rows = list(
        (
            await session.execute(
                select(UserGroup, BotUser)
                .join(BotUser, BotUser.user_id == UserGroup.user_id)
                .where(UserGroup.group_id == group_id)
                .order_by(UserGroup.user_id)
                .limit(RELATION_GRAPH_LIMIT + 1)
            )
        )
        .all()
    )
    member_truncated = len(member_rows) > RELATION_GRAPH_LIMIT
    if member_truncated:
        member_rows = member_rows[:RELATION_GRAPH_LIMIT]

    nodes: dict[int, dict[str, Any]] = {}
    for membership, user in member_rows:
        user_id = int(user.user_id)
        if guest and user_id not in degrees:
            continue
        nodes[user_id] = {
            "userId": str(user.user_id),
            "nickname": user.nickname,
            "groupNickname": membership.group_nickname,
            "role": membership.role,
            "linked": user_id in degrees,
            "degree": degrees.get(user_id, 0),
        }
    # 关系端点可能已不在成员表（退群残留），补齐为昵称回退节点，避免边悬空。
    for user_id, degree in degrees.items():
        if user_id not in nodes:
            nodes[user_id] = {
                "userId": str(user_id),
                "nickname": "",
                "groupNickname": None,
                "role": "member",
                "linked": True,
                "degree": degree,
            }

    relation_serializer = serialize_guest_relation if guest else serialize_relation
    meta = {"relationTruncated": relation_truncated}
    if not guest:
        # 这是管理员治理视图的成员表截断信息；访客没有必要知道群成员表规模。
        meta["memberTruncated"] = member_truncated
    return {
        "nodes": [nodes[key] for key in sorted(nodes)],
        "edges": [relation_serializer(row) for row in relation_rows],
        "meta": meta,
    }


def serialize_agent_message(row: GroupAgentMessage) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "messageId": str(row.message_id),
        "groupId": str(row.group_id),
        "userId": str(row.user_id),
        "senderName": row.sender_name,
        "role": row.role,
        "title": row.title,
        "text": row.normalized_text,
        "receivedAt": iso(row.received_at),
        "expiresAt": iso(row.expires_at),
    }


async def agent_memory_status(session: AsyncSession, group_id: int) -> dict[str, Any]:
    """记忆治理状态：未整理消息量、整理游标与按类型的记忆计数。"""

    now = datetime.now(BEIJING_TZ).replace(tzinfo=None)
    config = await session.get(GroupAgentConfig, group_id)
    runtime_enabled = await agent_runtime_enabled(session, group_id, config=config)
    cursor = int(config.last_compacted_message_id or 0) if config else 0
    # 与整理取数口径一致：过期但未整理的消息仍算待整理（purge 会保留
    # 到硬上限），状态页不得把它们显示成"已无积压"。
    pending_clauses = [
        GroupAgentMessage.group_id == group_id,
        GroupAgentMessage.id > cursor,
    ]
    pending = int(
        await session.scalar(
            select(func.count()).select_from(GroupAgentMessage).where(*pending_clauses)
        )
        or 0
    )
    last_compacted_at = None
    if cursor > 0:
        last_compacted_at = await session.scalar(
            select(GroupAgentMessage.received_at).where(GroupAgentMessage.id == cursor)
        )
    memory_clauses = [
        AgentMemory.group_id == group_id,
        AgentMemory.visibility.in_(("group", "public")),
        (AgentMemory.expires_at.is_(None) | (AgentMemory.expires_at >= now)),
    ]
    type_rows = (
        await session.execute(
            select(AgentMemory.memory_type, func.count())
            .where(*memory_clauses)
            .group_by(AgentMemory.memory_type)
        )
    ).all()
    counts_by_type = {str(row[0]): int(row[1] or 0) for row in type_rows}
    return {
        "groupId": str(group_id),
        "runtimeEnabled": runtime_enabled,
        "pendingMessages": pending,
        "lastCompactedMessageId": cursor or None,
        "lastCompactedAt": iso(last_compacted_at),
        "countsByType": counts_by_type,
        "total": sum(counts_by_type.values()),
        "oldestUpdatedAt": iso(
            await session.scalar(
                select(func.min(AgentMemory.updated_at)).where(*memory_clauses)
            )
        ),
        "newestUpdatedAt": iso(
            await session.scalar(
                select(func.max(AgentMemory.updated_at)).where(*memory_clauses)
            )
        ),
        "rebuildRequired": bool(config and config.memory_rebuild_required),
        "lastAttemptAt": iso(config.memory_last_attempt_at if config else None),
        "lastSuccessAt": iso(config.memory_last_success_at if config else None),
        "lastError": config.memory_last_error if config else None,
        "consecutiveFailures": int(
            config.memory_consecutive_failures if config else 0
        ),
        "inFlight": bool(runtime_enabled and is_memory_compacting(group_id)),
    }


async def agent_diagnostics(  # noqa: C901,PLR0912,PLR0915
    session: AsyncSession, group_id: int
) -> dict[str, Any]:
    """聚合群 Agent 当前运行门槛、LLM 路由和记忆治理状态。"""

    config = await session.get(GroupAgentConfig, group_id)
    enabled = await agent_runtime_enabled(session, group_id, config=config)
    reply_trigger_enabled = bool(config.reply_trigger_enabled) if config else True
    explicit_wakeup_enabled = bool(config.explicit_wakeup_enabled) if config else True
    proactive_configured = bool(config.proactive_enabled) if config else True
    proactive_active_configured = (
        bool(config.proactive_active_enabled) if config else True
    )
    short_conversation_configured = (
        bool(config.short_conversation_enabled) if config else True
    )
    proactive_active_enabled = bool(
        enabled and proactive_configured and proactive_active_configured
    )
    short_conversation_enabled = bool(enabled and short_conversation_configured)
    daily_limit = int(config.daily_limit) if config else 30
    cooldown_minutes = int(config.cooldown_minutes) if config else 8
    last_agent_at = config.last_agent_at if config else None
    last_proactive_at = config.last_proactive_at if config else None
    active_topic = config.active_topic if config else None
    media_cache_enabled = bool(config.media_cache_enabled) if config else False
    now = datetime.now(BEIJING_TZ).replace(tzinfo=None)
    today = now.strftime("%Y-%m-%d")
    proactive_today = (
        int(config.proactive_count)
        if config is not None and config.proactive_day == today
        else 0
    )
    cooldown_remaining = 0
    if last_proactive_at is not None and cooldown_minutes > 0:
        elapsed = max((now - last_proactive_at).total_seconds() / 60.0, 0.0)
        cooldown_remaining = max(0, math.ceil(float(cooldown_minutes) - elapsed))

    conversation = None
    for bot_id in sorted(get_bots()):
        try:
            current = current_conversation(int(bot_id), group_id)
        except (TypeError, ValueError):
            current = None
        if current is not None and short_conversation_enabled:
            conversation = {
                "enabled": short_conversation_enabled,
                "active": True,
                "sessionId": current.session_id,
                "topic": current.topic,
                "botTurns": current.bot_turns,
                "evaluations": current.evaluation_count,
                "consecutiveWaits": current.consecutive_waits,
            }
            break
    if conversation is None:
        conversation = {
            "enabled": short_conversation_enabled,
            "active": False,
            "sessionId": None,
            "topic": None,
            "botTurns": 0,
            "evaluations": 0,
            "consecutiveWaits": 0,
        }

    memory = await agent_memory_status(session, group_id)
    llm = _llm_runtime_status()
    route_by_task = {item["task"]: item for item in llm["routes"]}
    blockers: list[dict[str, str]] = []
    if not enabled:
        blockers.append(
            {
                "code": "agent_disabled",
                "severity": "error",
                "title": "Agent 已关闭",
                "detail": (
                    "群级总开关关闭时不会响应、主动发言或短会话续聊，"
                    "也不会自动采集和整理记忆。"
                ),
            }
        )
    if enabled and not get_bots():
        blockers.append(
            {
                "code": "bot_offline",
                "severity": "error",
                "title": "Bot 当前离线",
                "detail": "没有已连接的 Bot 账号，群 Agent 无法收发消息。",
            }
        )
    if enabled and not route_by_task["agent_dialogue"]["configured"]:
        blockers.append(
            {
                "code": "dialogue_llm_unconfigured",
                "severity": "error",
                "title": "对话 LLM 路由不可用",
                "detail": "Agent 对话任务所选 Provider 缺少可用密钥或模型。",
            }
        )
    proactive_enabled = bool(enabled and proactive_configured)
    if enabled and not proactive_enabled:
        blockers.append(
            {
                "code": "proactive_disabled",
                "severity": "info",
                "title": "主动参与已关闭",
                "detail": (
                    "Agent 仍会响应明确呼叫，但不会自行暖场或加入正在进行的聊天。"
                ),
            }
        )
    if proactive_enabled and proactive_today >= daily_limit:
        blockers.append(
            {
                "code": "proactive_daily_limit",
                "severity": "warning",
                "title": "今日主动发言额度已用尽",
                "detail": f"今日已用 {proactive_today}/{daily_limit} 次。",
            }
        )
    elif proactive_enabled and cooldown_remaining > 0:
        blockers.append(
            {
                "code": "proactive_cooldown",
                "severity": "info",
                "title": "主动发言仍在冷却",
                "detail": f"预计还需约 {cooldown_remaining} 分钟才满足主动冷却门槛。",
            }
        )
    if enabled and (
        proactive_enabled or short_conversation_enabled
    ) and not route_by_task["agent_proactive"]["configured"]:
        blockers.append(
            {
                "code": "proactive_llm_unconfigured",
                "severity": "error",
                "title": "主动/短会话 LLM 路由不可用",
                "detail": "主动发言与短会话续聊任务所选 Provider 缺少可用密钥或模型。",
            }
        )
    if memory["rebuildRequired"]:
        blockers.append(
            {
                "code": "memory_rebuild_required",
                "severity": "warning",
                "title": "记忆需要重建",
                "detail": (
                    "当前记忆状态被标记为需要重建，建议先完成重建再观察对话质量。"
                ),
            }
        )
    if int(memory["consecutiveFailures"]) > 0:
        blockers.append(
            {
                "code": "memory_failures",
                "severity": "warning",
                "title": "记忆整理最近连续失败",
                "detail": str(memory["lastError"] or "记忆整理失败，未记录具体错误。"),
            }
        )

    return {
        "groupId": str(group_id),
        "effective": {
            "enabled": enabled,
            "replyTriggerEnabled": bool(enabled and reply_trigger_enabled),
            "explicitWakeupEnabled": bool(enabled and explicit_wakeup_enabled),
            "proactiveEnabled": proactive_enabled,
            "proactiveActiveEnabled": proactive_active_enabled,
            "proactiveToday": proactive_today,
            "dailyLimit": daily_limit,
            "dailyRemaining": max(daily_limit - proactive_today, 0),
            "cooldownMinutes": cooldown_minutes,
            "cooldownRemainingMinutes": cooldown_remaining,
            "lastAgentAt": iso(last_agent_at),
            "lastProactiveAt": iso(last_proactive_at),
            "activeTopic": active_topic,
            "mediaCacheEnabled": media_cache_enabled,
            "shortConversation": conversation,
        },
        "memory": memory,
        "llm": llm,
        "blockers": blockers,
        "generatedAt": datetime.now(BEIJING_TZ).isoformat(),
    }


def serialize_agent_audit(row: AgentAudit) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "groupId": str(row.group_id),
        "actorUserId": str(row.actor_user_id)
        if row.actor_user_id is not None
        else None,
        "toolName": row.tool_name,
        "arguments": row.arguments,
        "result": row.result,
        "detail": row.detail,
        "createdAt": iso(row.created_at),
    }


def serialize_web_audit(row: WebAdminAudit) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "requestId": row.request_id,
        "actorSession": row.actor_session,
        "action": row.action,
        "resourceType": row.resource_type,
        "resourceId": row.resource_id,
        "result": row.result,
        "detail": row.detail,
        "createdAt": iso(row.created_at),
    }


async def delete_one_memory(
    session: AsyncSession, group_id: int, memory_id: int
) -> int:
    row = await session.scalar(
        select(AgentMemory).where(
            AgentMemory.group_id == group_id,
            AgentMemory.id == memory_id,
        )
    )
    if row is None:
        return 0
    await session.delete(row)
    return 1
