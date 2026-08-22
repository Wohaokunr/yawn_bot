# ruff: noqa: E501,F401,I001,TID252,PLR0911,PLR0912,PLR0913,PLR0915,PLR0917,C901,SIM117,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100,TC003,PLR2004,PERF203
"""主动发言双模式：热闹时像真人群友一样插话，冷场时偶尔暖场。"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any

from nonebot import get_bots, get_driver, logger
from nonebot.adapters.onebot.v11 import Message
from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_orm import get_session
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from ..data_models.agent_audit import AgentAudit
from ..data_models.group_agent_config import GroupAgentConfig
from ..data_models.group_agent_message import GroupAgentMessage
from ..llm import complete_with_tools, get_agent_model
from .context import ActivitySnapshot, is_cooldown_active, now_beijing
from .collector import group_lock
from .dialogue import _extract_message_id, _load_context, persist_bot_reply
from .log import dbg, dbg_exc
from .memory import compact_group_memory
from .media import cleanup_media_cache
from .persona import resolve_persona
from .prompt import build_messages

# 插话前至少等真人消息沉底 2 分钟：刚发完消息就接话像在抢话，
# 话题自然间隙里插话才像真人群友。
_ACTIVE_MIN_GAP_SECONDS = 120.0
# 60 分钟内真人消息不足此数说明群里并没有在聊，不插话。
_ACTIVE_MIN_MEMBER_MESSAGES = 3

_ACTIVE_INTERJECT_PROMPT = (
    "群里正在聊天。你作为其中一个群友，顺着最近的话题自然地插一句嘴："
    "直接对最近某条消息或话题做出反应，像随手打的一条消息。"
    "1~2 句、口语化，可以带点情绪或吐槽；"
    "不要开场白和客套，不要总结聊天记录，不要自称 AI 或助手。"
)

_WARMUP_PROMPT = (
    "群里冷场有一会儿了。你作为群友随口说点什么活跃一下气氛："
    "可以延续之前没聊完的话题、分享一个小见闻，或抛一个轻松的新话题。"
    "1~2 句、口语化、自然随意；"
    "不要问“大家在吗”“在干什么”，不要自称 AI 或助手。"
)


def _clamp_probability(value: Any) -> float:
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return 0.0


def should_proactively_speak(
    config: GroupAgentConfig,
    snapshot: ActivitySnapshot,
    now: datetime,
    *,
    rng: random.Random | None = None,
) -> str | None:
    """返回本次主动发言模式："active"（热闹插话）、"warmup"（冷场暖场）或 None。

    两模式天然互斥：暖场要求全部消息冷场超阈值，插话要求真人消息
    落在窗口内，因此同一时刻至多一个分支可用。
    """

    group_id = getattr(config, "group_id", None)
    if not config.enabled:
        dbg(f"群 {group_id} 主动发言拒绝: Agent 未启用")
        return None
    if snapshot.proactive_today >= config.daily_limit:
        dbg(
            f"群 {group_id} 主动发言拒绝: 今日已达上限"
            f"({snapshot.proactive_today}/{config.daily_limit})"
        )
        return None
    if is_cooldown_active(snapshot, now, config.cooldown_minutes):
        dbg(
            f"群 {group_id} 主动发言拒绝: 冷却中"
            f"(最后发言 {snapshot.last_agent_at},冷却 {config.cooldown_minutes} 分钟)"
        )
        return None
    roll = (rng or random).random()

    # 暖场模式：idle 含 bot 自己的发言——bot 刚说完话不会立刻再暖场。
    if snapshot.last_message_at is not None:
        idle_seconds = (now - snapshot.last_message_at).total_seconds()
        if idle_seconds >= config.idle_threshold_minutes * 60:
            probability = _clamp_probability(config.proactive_probability)
            if roll < probability:
                dbg(
                    f"群 {group_id} 暖场模式触发: 已冷场 {idle_seconds:.0f}s "
                    f"roll={roll:.3f} probability={probability:.2f}"
                )
                return "warmup"
            dbg(
                f"群 {group_id} 暖场模式骰子未中: roll={roll:.3f} "
                f"probability={probability:.2f}"
            )
            return None
    else:
        dbg(f"群 {group_id} 主动发言拒绝: 保留期内没有任何消息")
        return None

    # 插话模式：真人在聊，话题间隙内自然插嘴。
    if not config.proactive_active_enabled:
        dbg(f"群 {group_id} 主动发言拒绝: 热闹插话未开启")
        return None
    if snapshot.last_member_message_at is None:
        dbg(f"群 {group_id} 主动发言拒绝: 60 分钟内没有真人消息")
        return None
    member_idle = (now - snapshot.last_member_message_at).total_seconds()
    window_seconds = max(int(config.proactive_active_window_minutes), 1) * 60
    if member_idle < _ACTIVE_MIN_GAP_SECONDS:
        dbg(
            f"群 {group_id} 主动发言拒绝: 真人消息刚发 {member_idle:.0f}s, "
            f"不抢话(最小间隔 {_ACTIVE_MIN_GAP_SECONDS:.0f}s)"
        )
        return None
    if member_idle >= window_seconds:
        dbg(
            f"群 {group_id} 主动发言拒绝: 话题间隙已过 "
            f"(真人消息 {member_idle:.0f}s 前,窗口 {window_seconds:.0f}s)"
        )
        return None
    if snapshot.member_messages_60m < _ACTIVE_MIN_MEMBER_MESSAGES:
        dbg(
            f"群 {group_id} 主动发言拒绝: 60 分钟内真人消息仅 "
            f"{snapshot.member_messages_60m} 条,群里没在聊"
        )
        return None
    probability = _clamp_probability(config.proactive_active_probability)
    if roll < probability:
        dbg(
            f"群 {group_id} 插话模式触发: 真人消息 {member_idle:.0f}s 前 "
            f"roll={roll:.3f} probability={probability:.2f} "
            f"60m 真人消息={snapshot.member_messages_60m}"
        )
        return "active"
    dbg(
        f"群 {group_id} 插话模式骰子未中: roll={roll:.3f} probability={probability:.2f}"
    )
    return None


async def _collect_candidates(session: Any, now: datetime) -> list[dict[str, Any]]:
    """在单个会话内完成候选筛选；返回群号、当日计数与触发模式。"""

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
            # recent 按 id 倒序，recent[0] 最新；bot 自己落库的发言
            # (role="bot") 只计入整体冷场时间，不算"真人在聊"。
            in_window = [row for row in recent if row.received_at >= window_start]
            member_in_window = [row for row in in_window if row.role != "bot"]
            day = config.proactive_day or now.strftime("%Y-%m-%d")
            count = config.proactive_count if day == now.strftime("%Y-%m-%d") else 0
            snapshot = ActivitySnapshot(
                recent[0].received_at,
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
                last_member_message_at=(
                    member_in_window[0].received_at if member_in_window else None
                ),
                member_messages_60m=len(member_in_window),
            )
            mode = should_proactively_speak(config, snapshot, now)
            if mode is None:
                continue
            dbg(
                f"群 {config.group_id} 入选主动发言候选(模式={mode},"
                f"今日第 {count + 1} 条)"
            )
            candidates.append(
                {
                    "group_id": int(config.group_id),
                    "mode": mode,
                    "day_count": count,
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
    session: Any,
    config: GroupAgentConfig,
    *,
    now: datetime,
    text: str | None,
    day_count: int,
    bot_id: int | None,
    message_id: int | None,
) -> None:
    """在调用方事务内落库：成功或退避都推进 last_agent_at，防止下分钟反复抽卡。"""

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
                group_id=config.group_id,
                actor_user_id=None,
                tool_name="proactive_reply",
                arguments={},
                result="success",
                detail=text[:500],
            )
        )
        await persist_bot_reply(
            session,
            int(bot_id or 0),
            int(config.group_id),
            message_id,
            text,
            int(config.raw_retention_days),
        )
        dbg(f"群 {config.group_id} 主动发言计数推进: 今日已用 {day_count + 1} 条")
    else:
        dbg(f"群 {config.group_id} 主动发言未发出,仅推进 last_agent_at 防反复抽卡")


async def _process_candidate(candidate: dict[str, Any], bots: list[Any]) -> None:
    group_id = candidate["group_id"]
    mode = candidate["mode"]
    user_prompt = _ACTIVE_INTERJECT_PROMPT if mode == "active" else _WARMUP_PROMPT
    primary = bots[0]
    primary_self_id = int(str(getattr(primary, "self_id", "") or 0))
    # 与对话路径(process_group_message)保持同一锁协议：上下文加载、生成、
    # 发送和状态落库都必须在群锁内串行，否则会与对话路径互相覆盖丢更新。
    async with group_lock(group_id, primary_self_id):
        async with get_session() as session:
            config = await session.get(GroupAgentConfig, group_id)
            if (
                config is None
                or not config.enabled
                or config.trigger_mode != "mention_or_proactive"
            ):
                dbg(f"群 {group_id} 主动发言生成中止: 配置缺失/未启用/模式不含主动")
                return
            # 复用对话路径的完整上下文：40 条消息、成员、记忆与关系，
            # 让插话贴着群里的真实话题而不是只看 8 条消息的切片。
            context = await _load_context(session, group_id, config)
            prompt, _fingerprint = build_messages(
                persona=resolve_persona(config),
                tools=[],
                context=context,
                user_prompt=user_prompt,
            )
            dbg(f"群 {group_id} 主动发言生成: 模式={mode}, 请求 LLM")
            response = await complete_with_tools(  # pyright: ignore[reportArgumentType]
                prompt,  # pyright: ignore[reportArgumentType]
                [],
                model=get_agent_model("agent_dialogue"),
                role="agent_dialogue",
                max_tokens=256,
                timeout=25,
            )
            text = (response.content or "").strip() if response is not None else ""
            now = now_beijing()
            if not text:
                # 持续失败只留 warning 会被淹没；同时推进 last_agent_at
                # 退避一个冷却期，避免每分钟反复抽卡反复失败。
                logger.warning(
                    "群 %s 主动发言生成失败: LLM 返回空内容,退避至下个冷却期", group_id
                )
                await _apply_result(
                    session,
                    config,
                    now=now,
                    text=None,
                    day_count=candidate["day_count"],
                    bot_id=primary_self_id,
                    message_id=None,
                )
                await session.commit()
                return
            dbg(f"群 {group_id} 主动发言生成结果: {text!r}")
            if text.casefold() in {
                str(item.get("text") or "").casefold()
                for item in (config.recent_response_fingerprints or [])
                if isinstance(item, dict)
            }:
                dbg(f"群 {group_id} 主动发言与近期回复撞重,跳过发送: {text!r}")
                await _apply_result(
                    session,
                    config,
                    now=now,
                    text=None,
                    day_count=candidate["day_count"],
                    bot_id=primary_self_id,
                    message_id=None,
                )
                await session.commit()
                return
            # 多机器人部署下逐个尝试；通常只有一个连接，首个即成功。
            sent = False
            sent_message_id: int | None = None
            for bot in bots:
                try:
                    result = await bot.call_api(
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
                sent = True
                sent_message_id = _extract_message_id(result)
                dbg(
                    f"群 {group_id} 主动发言发送成功 bot={getattr(bot, 'self_id', None)}"
                )
                break
            if not sent:
                logger.warning("群 %s 主动消息无可用机器人发送", group_id)
                dbg(f"群 {group_id} 主动发言失败: {len(bots)} 个机器人均发送失败")
                return
            await _apply_result(
                session,
                config,
                now=now,
                text=text,
                day_count=candidate["day_count"],
                bot_id=primary_self_id,
                message_id=sent_message_id,
            )
            try:
                await session.commit()
            except SQLAlchemyError:
                logger.warning("群 %s 主动发言状态提交失败", group_id)
                dbg_exc(f"群 {group_id} 主动发言状态提交失败,已回滚")
                await session.rollback()


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
