# ruff: noqa: E501,F401,I001,TID252,PLR0912,PLR0913,PLR0917,C901,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100,TC003,PLR2004,PERF203
"""冷场检测和低频主动插话。"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any

from nonebot import get_bots, get_driver, logger
from nonebot.adapters.onebot.v11 import Message
from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_orm import get_session
from sqlalchemy import select

from ..data_models.agent_audit import AgentAudit
from ..data_models.group_agent_config import GroupAgentConfig
from ..data_models.group_agent_message import GroupAgentMessage
from ..llm import complete_with_tools, get_agent_model
from .context import ActivitySnapshot, coldness_score, is_cooldown_active, now_beijing
from .collector import group_lock
from .log import dbg, dbg_exc
from .memory import compact_group_memory
from .media import cleanup_media_cache
from .persona import resolve_persona
from .prompt import build_messages


def should_proactively_speak(
    config: GroupAgentConfig,
    snapshot: ActivitySnapshot,
    now: datetime,
    *,
    rng: random.Random | None = None,
) -> bool:
    group_id = getattr(config, "group_id", None)
    if not config.enabled:
        dbg(f"群 {group_id} 主动发言拒绝: Agent 未启用")
        return False
    if snapshot.proactive_today >= config.daily_limit:
        dbg(
            f"群 {group_id} 主动发言拒绝: 今日已达上限"
            f"({snapshot.proactive_today}/{config.daily_limit})"
        )
        return False
    if is_cooldown_active(snapshot, now, config.cooldown_minutes):
        dbg(
            f"群 {group_id} 主动发言拒绝: 冷却中"
            f"(最后发言 {snapshot.last_agent_at},冷却 {config.cooldown_minutes} 分钟)"
        )
        return False
    if (
        snapshot.last_message_at is None
        or (now - snapshot.last_message_at).total_seconds()
        < config.idle_threshold_minutes * 60
    ):
        idle_seconds = (
            (now - snapshot.last_message_at).total_seconds()
            if snapshot.last_message_at
            else None
        )
        dbg(
            f"群 {group_id} 主动发言拒绝: 冷场时间不足"
            f"(已冷场 {idle_seconds}s,阈值 {config.idle_threshold_minutes * 60}s)"
        )
        return False
    coldness = coldness_score(snapshot, now)
    if coldness < 0.6:
        dbg(f"群 {group_id} 主动发言拒绝: 冷场分数不足({coldness:.2f} < 0.6)")
        return False
    roll = (rng or random).random()
    probability = max(0.0, min(config.proactive_probability, 1.0))
    speak = roll < probability
    dbg(
        f"群 {group_id} 主动发言概率骰子: roll={roll:.3f} probability={probability:.2f} "
        f"coldness={coldness:.2f} → {'发言' if speak else '跳过'}"
    )
    return speak


async def _collect_candidates(session: Any, now: datetime) -> list[dict[str, Any]]:
    """在单个会话内完成候选筛选；返回纯数据，供会话外使用。"""

    rows = (
        (
            await session.execute(
                select(GroupAgentConfig).where(GroupAgentConfig.enabled.is_(True))
            )
        )
        .scalars()
        .all()
    )
    dbg(f"主动发言扫描: 启用的 Agent 配置 {len(rows)} 个")
    candidates: list[dict[str, Any]] = []
    window_start = now - timedelta(hours=1)
    for config in rows:
        # 上一轮若提交过，会话内所有 config 都会过期；逐个刷新，
        # 避免属性访问触发同步惰性加载（MissingGreenlet）。
        await session.refresh(config)
        try:
            if config.trigger_mode != "mention_or_proactive":
                dbg(
                    f"群 {config.group_id} 主动发言跳过: "
                    f"trigger_mode={config.trigger_mode!r} 不含主动模式"
                )
                continue
            recent = (
                (
                    await session.execute(
                        select(GroupAgentMessage)
                        .where(
                            GroupAgentMessage.group_id == config.group_id,
                            (
                                GroupAgentMessage.expires_at.is_(None)
                                | (GroupAgentMessage.expires_at >= now)
                            ),
                        )
                        .order_by(GroupAgentMessage.id.desc())
                        .limit(60)
                    )
                )
                .scalars()
                .all()
            )
            if not recent:
                dbg(f"群 {config.group_id} 主动发言跳过: 保留期内没有任何消息")
                continue
            last = recent[0].received_at
            # 60 分钟窗口按时间过滤；recent 只是保留期内的最新 60 条。
            in_window = [row for row in recent if row.received_at >= window_start]
            day = config.proactive_day or now.strftime("%Y-%m-%d")
            count = config.proactive_count if day == now.strftime("%Y-%m-%d") else 0
            snapshot = ActivitySnapshot(
                last,
                messages_5m=sum(
                    (now - row.received_at).total_seconds() < 300 for row in recent
                ),
                messages_20m=sum(
                    (now - row.received_at).total_seconds() < 1200 for row in recent
                ),
                messages_60m=len(in_window),
                participants_60m=len({row.user_id for row in in_window}),
                last_agent_at=config.last_agent_at,
                proactive_today=count,
            )
            if not should_proactively_speak(config, snapshot, now):
                continue
            dbg(f"群 {config.group_id} 入选主动发言候选(今日第 {count + 1} 条)")
            candidates.append(
                {
                    "group_id": int(config.group_id),
                    "persona": resolve_persona(config),
                    "active_topic": config.active_topic,
                    "day_count": count,
                    "fingerprint_texts": [
                        str(item.get("text"))
                        for item in (config.recent_response_fingerprints or [])
                        if isinstance(item, dict)
                    ],
                    "snapshot": snapshot,
                    "recent": [
                        {
                            "user_id": row.user_id,
                            "name": row.sender_name,
                            "text": row.normalized_text,
                        }
                        for row in reversed(recent[:8])
                    ],
                }
            )
        except Exception:  # noqa: BLE001
            logger.exception("群聊 Agent 主动候选筛选失败: %s", config.group_id)
            dbg_exc(f"群 {config.group_id} 主动候选筛选异常,已回滚并继续其他群")
            await session.rollback()
            # 回滚会使未处理的 config 过期，下一轮 refresh 会重新加载。
    dbg(f"主动发言扫描完成: 候选群 {len(candidates)} 个")
    return candidates


async def _apply_result(
    group_id: int,
    now: datetime,
    *,
    text: str | None,
    day_count: int,
) -> None:
    """发送成功或撞重跳过都要推进 last_agent_at，避免下一分钟反复抽卡。"""

    async with get_session() as session:
        config = await session.get(GroupAgentConfig, group_id)
        if config is None or not config.enabled:
            dbg(f"群 {group_id} 主动发言结果落库跳过: 配置缺失或未启用")
            return
        config.last_agent_at = now
        if text:
            config.proactive_day = now.strftime("%Y-%m-%d")
            config.proactive_count = day_count + 1
            history = list(config.recent_response_fingerprints or [])
            history.append(
                {
                    "text": text[:500],
                    "at": now.isoformat(),
                    "input": "proactive",
                }
            )
            config.recent_response_fingerprints = history[-8:]
            session.add(
                AgentAudit(
                    group_id=group_id,
                    actor_user_id=None,
                    tool_name="proactive_reply",
                    arguments={},
                    result="success",
                    detail=text[:500],
                )
            )
            dbg(f"群 {group_id} 主动发言计数推进: 明日额度剩余 {day_count + 1} 条已用")
        else:
            dbg(f"群 {group_id} 主动发言撞重跳过,仅推进 last_agent_at 防反复抽卡")
        await session.commit()


async def _process_candidate(candidate: dict[str, Any], bots: list[Any]) -> None:
    group_id = candidate["group_id"]
    snapshot: ActivitySnapshot = candidate["snapshot"]
    recent_text = "\n".join(
        item["text"] for item in candidate["recent"] if str(item["text"]).strip()
    )
    context = {
        "group_id": group_id,
        "active_topic": candidate["active_topic"],
        "activity": {
            "coldness_bucket": round(coldness_score(snapshot, now_beijing()) * 10),
            "messages_60m": snapshot.messages_60m,
            "participants_60m": snapshot.participants_60m,
        },
        "messages": candidate["recent"],
        "memories": [],
        "relations": [],
    }
    prompt, _fingerprint = build_messages(
        persona=candidate["persona"],
        tools=[],
        context=context,
        user_prompt=(recent_text or "群里最近有点安静。"),
    )
    primary = bots[0]
    primary_self_id = int(str(getattr(primary, "self_id", "") or 0))
    dbg(
        f"群 {group_id} 主动发言生成: 请求 LLM(user_prompt 含最近 {len(candidate['recent'])} 条消息)"
    )
    # 与对话路径(process_group_message)保持同一锁协议：生成、发送和
    # _apply_result 的读-改-写(recent_response_fingerprints、last_agent_at、
    # 主动计数)都必须在群锁内串行，否则会与对话路径互相覆盖丢更新。
    async with group_lock(group_id, primary_self_id):
        response = await complete_with_tools(  # pyright: ignore[reportArgumentType]
            prompt,  # pyright: ignore[reportArgumentType]
            [],
            model=get_agent_model("agent_dialogue"),
            role="agent_dialogue",
            max_tokens=160,
            timeout=20,
        )
        text = (response.content or "").strip() if response is not None else ""
        if not text:
            dbg(f"群 {group_id} 主动发言放弃: LLM 返回空内容")
            return
        dbg(f"群 {group_id} 主动发言生成结果: {text!r}")
        if text.casefold() in {
            item.casefold() for item in candidate["fingerprint_texts"]
        }:
            dbg(f"群 {group_id} 主动发言与近期回复撞重,跳过发送: {text!r}")
            await _apply_result(
                group_id,
                now_beijing(),
                text=None,
                day_count=candidate["day_count"],
            )
            return
        # 多机器人部署下逐个尝试；通常只有一个连接，首个即成功。
        for bot in bots:
            try:
                await bot.call_api(
                    "send_group_msg",
                    group_id=group_id,
                    message=Message(text),
                )
            except Exception:  # noqa: BLE001
                logger.warning("群 %s 主动消息发送失败，尝试下一个机器人", group_id)
                dbg_exc(
                    f"群 {group_id} 主动发言 bot={getattr(bot, 'self_id', None)} 发送失败"
                )
                continue
            dbg(f"群 {group_id} 主动发言发送成功 bot={getattr(bot, 'self_id', None)}")
            await _apply_result(
                group_id,
                now_beijing(),
                text=text,
                day_count=candidate["day_count"],
            )
            return
        logger.warning("群 %s 主动消息无可用机器人发送", group_id)
        dbg(f"群 {group_id} 主动发言失败: {len(bots)} 个机器人均发送失败")


async def _tick() -> None:
    """调度入口；只负责候选筛选，具体回复由普通 Agent 流程处理。"""

    bots = [bot for bot in get_bots().values() if bot is not None]
    if not bots:
        dbg("主动发言 tick: 没有已连接的机器人,跳过")
        return
    now = now_beijing()
    dbg(f"主动发言 tick 开始: bots={len(bots)}")
    async with get_session() as session:
        candidates = await _collect_candidates(session, now)
    for candidate in candidates:
        try:
            await _process_candidate(candidate, bots)
        except Exception:  # noqa: BLE001
            logger.exception("群聊 Agent 主动插话失败: %s", candidate["group_id"])
            dbg_exc(f"群 {candidate['group_id']} 主动插话流程异常")


async def _compact_tick() -> None:
    """每日整理摘要并清除超过保留期的原始消息。"""

    async with get_session() as session:
        configs = (await session.execute(select(GroupAgentConfig))).scalars().all()
        group_ids = [int(config.group_id) for config in configs]
    dbg(f"每日记忆整理开始: 共 {len(group_ids)} 个群 {group_ids}")
    # 每群独立会话：一个群的错误不能阻断其余群的过期清理。
    for group_id in group_ids:
        try:
            async with get_session() as session:
                await compact_group_memory(session, group_id)
        except Exception:  # noqa: BLE001
            logger.exception("群 %s Agent 记忆整理失败", group_id)
            dbg_exc(f"群 {group_id} 每日记忆整理异常")
    try:
        async with get_session() as session:
            await cleanup_media_cache(session)
            # get_session() 不会自动提交；缺少这一步清理会被静默回滚。
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("Agent 媒体缓存清理失败")
        dbg_exc("每日媒体缓存清理异常")
    dbg("每日记忆整理结束")


@get_driver().on_startup
async def _restore_jobs() -> None:
    dbg("Agent 定时任务启动检查: yawn_core_agent:tick / yawn_core_agent:compact")
    if scheduler.get_job("yawn_core_agent:tick") is None:
        scheduler.add_job(
            _tick,
            "interval",
            minutes=1,
            id="yawn_core_agent:tick",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=60,
        )
        dbg("已注册定时任务 yawn_core_agent:tick(每分钟主动发言扫描)")
    if scheduler.get_job("yawn_core_agent:compact") is None:
        scheduler.add_job(
            _compact_tick,
            "cron",
            hour=3,
            minute=30,
            id="yawn_core_agent:compact",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        dbg("已注册定时任务 yawn_core_agent:compact(每日 03:30 记忆整理)")


__all__ = ["should_proactively_speak"]
