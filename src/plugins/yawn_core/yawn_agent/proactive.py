# ruff: noqa: E501,F401,I001,TID252,PLR0912,PLR0913,PLR0917,C901,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100,DTZ005,TC003,PLR2004
"""冷场检测和低频主动插话。"""

from __future__ import annotations

import random
from datetime import datetime, timezone

from nonebot import get_bot, get_driver, logger
from nonebot.adapters.onebot.v11 import Message
from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_orm import get_session
from sqlalchemy import select

from ..data_models.agent_audit import AgentAudit
from ..data_models.group_agent_config import GroupAgentConfig
from ..data_models.group_agent_message import GroupAgentMessage
from ..llm import complete_with_tools
from .context import ActivitySnapshot, coldness_score, is_cooldown_active
from .collector import group_lock
from .memory import compact_group_memory
from .media import cleanup_media_cache
from .persona import resolve_persona
from .prompt import build_messages
from ..llm import get_agent_model


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def should_proactively_speak(
    config: GroupAgentConfig,
    snapshot: ActivitySnapshot,
    now: datetime,
    *,
    rng: random.Random | None = None,
) -> bool:
    if not config.enabled or snapshot.proactive_today >= config.daily_limit:
        return False
    if is_cooldown_active(snapshot, now, config.cooldown_minutes):
        return False
    if (
        snapshot.last_message_at is None
        or (now - snapshot.last_message_at).total_seconds()
        < config.idle_threshold_minutes * 60
    ):
        return False
    if coldness_score(snapshot, now) < 0.6:
        return False
    return (rng or random).random() < max(0.0, min(config.proactive_probability, 1.0))


async def _tick() -> None:
    """调度入口；只负责候选筛选，具体回复由普通 Agent 流程处理。"""

    now = _now()
    try:
        bot = get_bot()
    except Exception:  # noqa: BLE001
        return
    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    select(GroupAgentConfig).where(GroupAgentConfig.enabled.is_(True))
                )
            )
            .scalars()
            .all()
        )
        for config in rows:
            if config.trigger_mode != "mention_or_proactive":
                continue
            recent = (
                (
                    await session.execute(
                        select(GroupAgentMessage)
                        .where(GroupAgentMessage.group_id == config.group_id)
                        .order_by(GroupAgentMessage.id.desc())
                        .limit(60)
                    )
                )
                .scalars()
                .all()
            )
            if not recent:
                continue
            last = recent[0].received_at
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
                messages_60m=len(recent),
                participants_60m=len({row.user_id for row in recent}),
                last_agent_at=config.last_agent_at,
                proactive_today=count,
            )
            if not should_proactively_speak(config, snapshot, now):
                continue
            recent_text = "\n".join(
                row.normalized_text
                for row in reversed(recent[:8])
                if row.normalized_text.strip()
            )
            context = {
                "group_id": int(config.group_id),
                "active_topic": config.active_topic,
                "activity": {
                    "coldness_bucket": 10,
                    "messages_60m": len(recent),
                    "participants_60m": len({row.user_id for row in recent}),
                },
                "messages": [
                    {
                        "user_id": row.user_id,
                        "name": row.sender_name,
                        "text": row.normalized_text,
                    }
                    for row in reversed(recent[:8])
                ],
                "memories": [],
                "relations": [],
            }
            prompt, _fingerprint = build_messages(
                persona=resolve_persona(config),
                tools=[],
                context=context,
                user_prompt=(recent_text or "群里最近有点安静。"),
            )
            async with group_lock(
                config.group_id, int(getattr(bot, "self_id", 0) or 0)
            ):
                response = await complete_with_tools(  # pyright: ignore[reportArgumentType]
                    prompt,  # pyright: ignore[reportArgumentType]
                    [],
                    model=get_agent_model("agent_dialogue"),
                    role="agent_dialogue",
                    max_tokens=160,
                    timeout=20,
                )
                text = (response.content or "").strip() if response is not None else ""
                if text:
                    recent_replies = [
                        item.get("text")
                        for item in (config.recent_response_fingerprints or [])
                        if isinstance(item, dict)
                    ]
                    if text.casefold() in {
                        str(item).casefold() for item in recent_replies
                    }:
                        continue
                    await bot.call_api(
                        "send_group_msg",
                        group_id=config.group_id,
                        message=Message(text),
                    )
                    config.last_agent_at = now
                    config.proactive_day = now.strftime("%Y-%m-%d")
                    config.proactive_count = count + 1
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
                    await session.commit()


async def _compact_tick() -> None:
    """每日整理摘要并清除超过保留期的原始消息。"""

    async with get_session() as session:
        configs = (await session.execute(select(GroupAgentConfig))).scalars().all()
        for config in configs:
            await compact_group_memory(session, config.group_id)
        await cleanup_media_cache(session)


@get_driver().on_startup
async def _restore_jobs() -> None:
    if scheduler.get_job("yawn_core_agent:tick") is None:
        scheduler.add_job(
            _tick,
            "interval",
            minutes=1,
            id="yawn_core_agent:tick",
            replace_existing=True,
        )
    if scheduler.get_job("yawn_core_agent:compact") is None:
        scheduler.add_job(
            _compact_tick,
            "cron",
            hour=3,
            minute=30,
            id="yawn_core_agent:compact",
            replace_existing=True,
        )


__all__ = ["should_proactively_speak"]
