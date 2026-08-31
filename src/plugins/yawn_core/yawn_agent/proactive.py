# ruff: noqa: E501,F401,I001,TID252,PLR0911,PLR0912,PLR0913,PLR0915,PLR0917,C901,SIM117,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100,TC003,PLR2004,PERF203,PERF401
"""主动发言双模式：热闹时像真人群友一样插话，冷场时偶尔暖场。"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import Any

from nonebot import get_bots, get_driver, logger
from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_orm import get_session
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from ..data_models.agent_audit import AgentAudit
from ..data_models.group_agent_config import GroupAgentConfig
from ..data_models.group_agent_message import GroupAgentMessage
from ..llm import ai_config, complete, resolve_llm_request
from .context import ActivitySnapshot, now_beijing
from .collector import group_lock
from .config_store import agent_runtime_enabled, list_agent_group_ids
from .conversation import (
    CONVERSATION_MAX_BOT_TURNS,
    ConversationBatch,
    begin_followup_evaluation,
    close_conversation,
    conversation_is_current,
    finish_followup_evaluation,
    mark_bot_reply,
    prune_expired_conversations,
    set_followup_handler,
    shutdown_conversations,
)
from .dialogue import (
    _activity_window_counts,
    _load_context,
    persist_bot_reply,
)
from .execution_trace import (
    begin_execution_trace,
    bind_execution_trace,
    finish_execution_trace,
    reset_execution_trace,
    trace_event,
)
from .log import dbg, dbg_exc
from .memory import (
    compact_group_memory,
    decay_stale_relations,
    memory_retry_due,
    record_memory_failure,
)
from .media import cleanup_media_cache
from .persona import persona_behavior, resolve_persona
from .prompt import build_messages
from .outbound import (
    PreparedOutboundMessage,
    SendResult,
    prepare_outbound_message,
    prepare_text_message,
    send_prepared_outbound,
)
from .proactive_policy import (
    ProactiveDecision as _ProactiveDecision,
    apply_persona_behavior_to_decision as _apply_persona_behavior_to_decision,
    _ACTIVE_INTERJECT_PROMPT,
    _FOLLOWUP_PROMPT,
    _JSON_PROTOCOL,
    _MEMORY_USE_PROMPT,
    _PROACTIVE_SEGMENT_TYPES,
    _WARMUP_PROMPT,
    build_user_prompt as _build_user_prompt,
    clamp_probability as _clamp_probability,
    decide_proactive_reply as _decide_proactive_reply,
    recent_proactive_lines as _recent_proactive_lines,
    should_proactively_speak,
    skip_backoff_timestamp as _skip_backoff_timestamp,
    warmup_probability as _warmup_probability,
)

_PROACTIVE_MIN_MAX_TOKENS = 2048
_PROACTIVE_TIMEOUT_SECONDS = 25.0


async def _generate_proactive_reply(messages: list[Any]) -> str | None:
    """用统一预算生成主动首轮或短会话续聊决策。"""

    return await complete(  # pyright: ignore[reportArgumentType]
        messages,  # pyright: ignore[reportArgumentType]
        task="agent_proactive",
        max_tokens=max(_PROACTIVE_MIN_MAX_TOKENS, int(ai_config.ai_max_tokens)),
        timeout=_PROACTIVE_TIMEOUT_SECONDS,
    )


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
    for config in rows:
        # 上一轮若提交过，会话内所有 config 都会过期；逐个刷新，
        # 避免属性访问触发同步惰性加载（MissingGreenlet）。
        await session.refresh(config)
        try:
            if not await agent_runtime_enabled(
                session, int(config.group_id), config=config
            ):
                dbg(f"群 {config.group_id} 主动发言跳过: Agent 总开关已关闭")
                continue
            if not config.proactive_enabled:
                dbg(f"群 {config.group_id} 主动参与跳过: 主动参与已关闭")
                continue
            counts = await _activity_window_counts(session, config.group_id, now)
            if counts["last_message_at"] is None:
                dbg(f"群 {config.group_id} 主动发言跳过: 保留期内没有任何消息")
                continue
            day = config.proactive_day or now.strftime("%Y-%m-%d")
            count = config.proactive_count if day == now.strftime("%Y-%m-%d") else 0
            # bot 自言只计入整体冷场时间，不算"真人在聊"；隐私退出用户
            # 已由聚合查询排除，与对话读路径口径一致。
            snapshot = ActivitySnapshot(
                counts["last_message_at"],
                messages_5m=counts["messages_5m"],
                messages_20m=counts["messages_20m"],
                messages_60m=counts["messages_60m"],
                participants_60m=counts["participants_60m"],
                replies_60m=counts["replies_60m"],
                mentions_60m=counts["mentions_60m"],
                last_agent_at=config.last_agent_at,
                last_proactive_at=config.last_proactive_at,
                proactive_today=count,
                last_member_message_at=counts["last_member_message_at"],
                member_messages_60m=counts["member_messages_60m"],
                member_messages_5m=counts["member_messages_5m"],
                member_participants_5m=counts["member_participants_5m"],
            )
            scene = should_proactively_speak(config, snapshot, now)
            if scene is None:
                continue
            dbg(
                f"群 {config.group_id} 入选主动发言候选(场景={scene},"
                f"今日第 {count + 1} 条)"
            )
            candidates.append(
                {
                    "group_id": int(config.group_id),
                    "scene": scene,
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
    send_result: SendResult | None,
    history_text: str | None,
    day_count: int,
    bot_id: int | None,
    scene: str,
    reason: str,
    session_turn: int,
    target_user_id: int | None = None,
    confidence: float | None = None,
) -> None:
    """在调用方事务内落库：成功推进 last_proactive_at/last_agent_at；
    未发出（内容门拦截、生成失败、撞重）只把 last_proactive_at 回拨到
    剩余半个冷却期，既防下分钟反复抽卡，也不让一次跳过封锁太久。"""

    if send_result is not None and send_result.ends_turn:
        audit_text = str(history_text or send_result.normalized_text or "[结构化消息]")
        config.last_proactive_at = now
        config.last_agent_at = now
        config.proactive_day = now.strftime("%Y-%m-%d")
        config.proactive_count = day_count + 1
        history = list(config.recent_response_fingerprints or [])
        history.append(
            {
                "text": audit_text[:500],
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
                arguments={
                    "scene": scene,
                    "action": "speak",
                    "session_turn": session_turn,
                    "reason": reason[:240],
                    "delivery_state": send_result.delivery_state,
                    "target_user_id": target_user_id,
                    "confidence": confidence,
                },
                result=(
                    "speak" if send_result.sent else "delivery_unknown"
                ),
                detail=audit_text[:500],
            )
        )
        if send_result.sent:
            await persist_bot_reply(
                session,
                int(bot_id or 0),
                int(config.group_id),
                send_result.message_id,
                send_result.normalized_text,
                int(config.raw_retention_days),
                segments=send_result.segments,
                reply_chain=send_result.reply_chain,
                forward_tree=send_result.forward_tree,
                media_refs=send_result.media_refs,
            )
        else:
            dbg(
                f"群 {config.group_id} 主动消息投递未知,按可能已送达消耗配额但不写消息历史"
            )
        dbg(f"群 {config.group_id} 主动发言计数推进: 今日已用 {day_count + 1} 条")
    else:
        backoff_at = _skip_backoff_timestamp(now, int(config.cooldown_minutes))
        if config.last_proactive_at is None or config.last_proactive_at < backoff_at:
            config.last_proactive_at = backoff_at
        dbg(
            f"群 {config.group_id} 主动发言未发出,退避剩余半个冷却期至 {backoff_at}"
        )


async def _prepare_proactive_message(
    decision: _ProactiveDecision,
    *,
    session: Any,
    group_id: int,
) -> PreparedOutboundMessage:
    """把主动决策转换为和普通对话完全相同的受限消息计划。"""

    if decision.segments:
        return await prepare_outbound_message(
            list(decision.segments),
            session=session,
            group_id=group_id,
            actor_user_id=None,
        )
    return prepare_text_message(decision.text)


async def _process_candidate_impl(candidate: dict[str, Any], bots: list[Any]) -> str:
    group_id = candidate["group_id"]
    scene = candidate["scene"]
    primary = bots[0]
    primary_self_id = int(str(getattr(primary, "self_id", "") or 0))
    # 与对话路径(process_group_message)保持同一锁协议：上下文加载、生成、
    # 发送和状态落库都必须在群锁内串行，否则会与对话路径互相覆盖丢更新。
    async with group_lock(group_id, primary_self_id):
        async with get_session() as session:
            config = await session.get(GroupAgentConfig, group_id)
            if (
                config is None
                or not await agent_runtime_enabled(session, group_id, config=config)
                or not config.proactive_enabled
            ):
                dbg(f"群 {group_id} 主动发言生成中止: 配置缺失/Agent 未启用/主动参与关闭")
                return "close"
            behavior = persona_behavior(config)
            trace_event(
                "policy",
                "Persona 群聊行为",
                output=behavior.as_dict(),
            )
            # 复用对话路径的相关上下文：先读有限候选池，再保留最后一个
            # 活跃对话簇，避免主动插话把整小时旧聊天与默认空元数据全量注入。
            # 消息仍附带 minutes_ago，便于模型判断话题的新旧与节奏。
            context_started = time.monotonic()
            context = await _load_context(
                session,
                group_id,
                config,
                compact_history=True,
                include_active_profiles=True,
                context_model=resolve_llm_request("agent_proactive").model,
                completion_reserve=max(
                    _PROACTIVE_MIN_MAX_TOKENS, int(ai_config.ai_max_tokens)
                ),
                context_token_limit=1600,
            )
            trace_event(
                "context",
                "主动发言上下文装箱",
                output={
                    "messages": len(list(context.get("messages") or [])),
                    "members": len(list(context.get("members") or [])),
                    "memories": len(list(context.get("memories") or [])),
                    "relations": len(list(context.get("relations") or [])),
                },
                duration_ms=(time.monotonic() - context_started) * 1000,
            )
            prompt_started = time.monotonic()
            prompt, _fingerprint = build_messages(
                persona=resolve_persona(config),
                tools=[],
                context=context,
                user_prompt=_build_user_prompt(scene, config, turn=1),
            )
            trace_event(
                "prompt",
                "主动发言 Prompt 构建",
                output={"message_count": len(prompt), "scene": scene},
                duration_ms=(time.monotonic() - prompt_started) * 1000,
            )
            dbg(f"群 {group_id} 主动发言生成: 场景={scene}, 请求 LLM")
            llm_started = time.monotonic()
            raw = await _generate_proactive_reply(prompt)
            now = now_beijing()
            if raw is None:
                trace_event(
                    "llm",
                    "主动发言决策",
                    status="failed",
                    output={"response": "none"},
                    detail="LLM 返回空内容",
                    duration_ms=(time.monotonic() - llm_started) * 1000,
                )
                # 持续失败只留 warning 会被淹没；同时推进 last_agent_at
                # 退避一个冷却期，避免每分钟反复抽卡反复失败。
                logger.warning(
                    "群 %s 主动发言生成失败: LLM 返回空内容,退避至下个冷却期", group_id
                )
                await _apply_result(
                    session,
                    config,
                    now=now,
                    send_result=None,
                    history_text=None,
                    day_count=candidate["day_count"],
                    bot_id=primary_self_id,
                    scene=scene,
                    reason="LLM 返回空内容",
                    session_turn=1,
                )
                session.add(
                    AgentAudit(
                        group_id=group_id,
                        actor_user_id=None,
                        tool_name="proactive_reply",
                        arguments={
                            "scene": scene,
                            "action": "error",
                            "session_turn": 1,
                            "reason": "LLM 返回空内容",
                        },
                        result="error",
                        detail="LLM 返回空内容",
                    )
                )
                await session.commit()
                return "error"
            decision = _apply_persona_behavior_to_decision(
                config, _decide_proactive_reply(raw)
            )
            trace_event(
                "llm",
                "主动发言决策",
                status="success" if decision.should_speak else "skipped",
                output={
                    "action": decision.action,
                    "target_user_id": decision.target_user_id,
                    "topic": decision.topic,
                    "reason": decision.reason,
                    "confidence": decision.confidence,
                    "segment_count": len(decision.segments),
                },
                duration_ms=(time.monotonic() - llm_started) * 1000,
            )
            if not decision.should_speak:
                # 内容门拦截：模型读懂对话后判定此刻不适合开口。
                # 记录 skip 审计便于观察决策质量，并推进 last_agent_at
                # 防止下一分钟在同一话题上反复抽卡。
                dbg(
                    f"群 {group_id} 主动发言被内容门拦截: "
                    f"reason={decision.reason!r} topic={decision.topic!r}"
                )
                session.add(
                    AgentAudit(
                        group_id=config.group_id,
                        actor_user_id=None,
                        tool_name="proactive_reply",
                        arguments={
                            "scene": scene,
                            "action": decision.action,
                            "session_turn": 1,
                            "reason": decision.reason[:240],
                            "target_user_id": decision.target_user_id,
                            "confidence": decision.confidence,
                        },
                        result=decision.action,
                        detail=decision.reason[:500],
                    )
                )
                await _apply_result(
                    session,
                    config,
                    now=now,
                    send_result=None,
                    history_text=None,
                    day_count=candidate["day_count"],
                    bot_id=primary_self_id,
                    scene=scene,
                    reason=decision.reason,
                    session_turn=1,
                    target_user_id=decision.target_user_id,
                    confidence=decision.confidence,
                )
                await session.commit()
                return decision.action
            history_text = decision.history_text
            dbg(
                f"群 {group_id} 主动发言生成结果: {history_text!r} "
                f"segments={len(decision.segments)} reason={decision.reason!r}"
            )
            if history_text.casefold() in {
                str(item.get("text") or "").casefold()
                for item in (config.recent_response_fingerprints or [])
                if isinstance(item, dict)
            }:
                dbg(f"群 {group_id} 主动发言与近期回复撞重,跳过发送: {history_text!r}")
                session.add(
                    AgentAudit(
                        group_id=group_id,
                        actor_user_id=None,
                        tool_name="proactive_reply",
                        arguments={
                            "scene": scene,
                            "action": "wait",
                            "session_turn": 1,
                            "reason": "近期回复撞重",
                        },
                        result="wait",
                        detail="近期回复撞重",
                    )
                )
                await _apply_result(
                    session,
                    config,
                    now=now,
                    send_result=None,
                    history_text=None,
                    day_count=candidate["day_count"],
                    bot_id=primary_self_id,
                    scene=scene,
                    reason="近期回复撞重",
                    session_turn=1,
                )
                await session.commit()
                return "wait"
            try:
                prepared = await _prepare_proactive_message(
                    decision, session=session, group_id=group_id
                )
            except Exception as exc:  # noqa: BLE001
                reason = f"主动消息计划无效: {exc}"
                logger.warning("群 %s 主动消息计划无效: %s", group_id, exc)
                session.add(
                    AgentAudit(
                        group_id=group_id,
                        actor_user_id=None,
                        tool_name="proactive_reply",
                        arguments={
                            "scene": scene,
                            "action": "wait",
                            "session_turn": 1,
                            "reason": reason[:240],
                        },
                        result="wait",
                        detail=reason[:500],
                    )
                )
                await _apply_result(
                    session,
                    config,
                    now=now,
                    send_result=None,
                    history_text=None,
                    day_count=candidate["day_count"],
                    bot_id=primary_self_id,
                    scene=scene,
                    reason=reason,
                    session_turn=1,
                )
                await session.commit()
                return "wait"

            # 多机器人部署下逐个尝试；通常只有一个连接，首个即成功。
            sent_result: SendResult | None = None
            sent_bot_id = primary_self_id
            for bot in bots:
                try:
                    sent_result = await send_prepared_outbound(
                        bot,
                        group_id,
                        prepared,
                        session=session,
                        actor_user_id=None,
                        source="proactive",
                    )
                except Exception:  # noqa: BLE001
                    logger.warning("群 %s 主动消息发送失败，尝试下一个机器人", group_id)
                    dbg_exc(
                        f"群 {group_id} 主动发言 bot={getattr(bot, 'self_id', None)} 发送失败"
                    )
                    continue
                sent_bot_id = int(str(getattr(bot, "self_id", "") or 0))
                dbg(
                    f"群 {group_id} 主动发言发送成功 bot={getattr(bot, 'self_id', None)}"
                )
                break
            if sent_result is None:
                logger.warning("群 %s 主动消息无可用机器人发送", group_id)
                dbg(f"群 {group_id} 主动发言失败: {len(bots)} 个机器人均发送失败")
                session.add(
                    AgentAudit(
                        group_id=group_id,
                        actor_user_id=None,
                        tool_name="proactive_reply",
                        arguments={
                            "scene": scene,
                            "action": "send_failed",
                            "session_turn": 1,
                            "reason": "无可用机器人发送",
                        },
                        result="send_failed",
                        detail="所有机器人发送失败",
                    )
                )
                await session.commit()
                return "send_failed"
            if decision.topic:
                # 用模型提炼的真实话题更新 active_topic，让后续对话路径的
                # 上下文不再停留在"上次触发消息的原文"。
                config.active_topic = decision.topic[:240]
            await _apply_result(
                session,
                config,
                now=now,
                send_result=sent_result,
                history_text=history_text,
                day_count=candidate["day_count"],
                bot_id=sent_bot_id,
                scene=scene,
                reason=decision.reason,
                session_turn=1,
                target_user_id=decision.target_user_id,
                confidence=decision.confidence,
            )
            if config.short_conversation_enabled:
                mark_bot_reply(
                    sent_bot_id,
                    group_id,
                    topic=decision.topic or str(config.active_topic or ""),
                    source=scene,
                    max_bot_turns=persona_behavior(config).max_followup_bot_turns,
                )
            try:
                await session.commit()
            except SQLAlchemyError:
                logger.warning("群 %s 主动发言状态提交失败", group_id)
                dbg_exc(f"群 {group_id} 主动发言状态提交失败,已回滚")
                await session.rollback()
            return "speak"


async def _process_candidate(candidate: dict[str, Any], bots: list[Any]) -> None:
    started = time.monotonic()
    outcome = "error"
    scene = str(candidate.get("scene") or "")
    trigger_source = {
        "warmup": "proactive_warmup",
        "active": "proactive_interject",
    }.get(scene, "proactive_participation")
    trace = begin_execution_trace(
        int(candidate["group_id"]),
        mode="proactive",
        source="runtime",
        trigger_source=trigger_source,
    )
    token = bind_execution_trace(trace)
    trace_event(
        "intake",
        "主动发言候选进入执行",
        output={
            "trigger_source": trigger_source,
            "scene": candidate.get("scene"),
            "day_count": candidate.get("day_count"),
        },
    )
    try:
        outcome = await _process_candidate_impl(candidate, bots)
    except BaseException:
        trace_event("turn", "主动发言执行异常", status="failed")
        raise
    finally:
        trace_event(
            "turn",
            "主动发言回合结束",
            status="failed" if outcome == "error" else "success",
            output={"outcome": outcome},
            duration_ms=max(time.monotonic() - started, 0.0) * 1000,
        )
        finish_execution_trace(trace, outcome=outcome)
        reset_execution_trace(token)
        try:
            from ..metrics import record_agent_turn

            record_agent_turn(
                "proactive", outcome, max(time.monotonic() - started, 0.0)
            )
        except Exception:  # noqa: BLE001
            dbg_exc("Agent 主动发言回合指标上报失败(忽略)")


async def _process_followup_impl(batch: ConversationBatch) -> str:
    """处理一个已经完成 20~45 秒合批的短会话续聊候选。"""

    bot_id, group_id = batch.key
    if not conversation_is_current(batch):
        dbg(
            f"群 {group_id} 短会话候选已失效: session={batch.session_id}"
        )
        return "close"
    bot = next(
        (
            item
            for item in get_bots().values()
            if int(str(getattr(item, "self_id", "") or 0)) == bot_id
        ),
        None,
    )
    if bot is None:
        close_conversation(bot_id, group_id, reason="会话机器人已离线")
        return "close"

    async with group_lock(group_id, bot_id):
        if not conversation_is_current(batch):
            return "close"
        if not begin_followup_evaluation(batch):
            return "close"
        async with get_session() as session:
            config = await session.get(GroupAgentConfig, group_id)
            if (
                config is None
                or not await agent_runtime_enabled(session, group_id, config=config)
                or not config.short_conversation_enabled
            ):
                close_conversation(bot_id, group_id, reason="配置关闭短会话续聊")
                return "close"
            behavior = persona_behavior(config)
            if batch.bot_turns >= behavior.max_followup_bot_turns:
                close_conversation(
                    bot_id,
                    group_id,
                    reason="Persona 续聊倾向已达到角色轮数上限",
                )
                trace_event(
                    "policy",
                    "Persona 续聊门槛",
                    status="skipped",
                    output=behavior.as_dict(),
                    detail="当前角色不再自动延长本话题",
                )
                return "close"
            trace_event(
                "policy",
                "Persona 群聊行为",
                output=behavior.as_dict(),
            )
            now = now_beijing()
            day = now.strftime("%Y-%m-%d")
            day_count = config.proactive_count if config.proactive_day == day else 0
            if day_count >= int(config.daily_limit):
                dbg(
                    f"群 {group_id} 短会话关闭: 今日自动发言已达上限"
                    f"({day_count}/{config.daily_limit})"
                )
                close_conversation(bot_id, group_id, reason="达到每日上限")
                return "close"
            context_started = time.monotonic()
            context = await _load_context(
                session,
                group_id,
                config,
                bot_id,
                focus_user_ids=batch.user_ids,
                compact_history=True,
                message_cutoff=batch.cutoff_at,
                include_active_profiles=True,
                reference_at=batch.cutoff_at,
                context_model=resolve_llm_request("agent_proactive").model,
                completion_reserve=max(
                    _PROACTIVE_MIN_MAX_TOKENS, int(ai_config.ai_max_tokens)
                ),
                context_token_limit=1600,
            )
            trace_event(
                "context",
                "短会话上下文装箱",
                input={"focus_user_ids": batch.user_ids},
                output={
                    "messages": len(list(context.get("messages") or [])),
                    "members": len(list(context.get("members") or [])),
                    "memories": len(list(context.get("memories") or [])),
                    "relations": len(list(context.get("relations") or [])),
                },
                duration_ms=(time.monotonic() - context_started) * 1000,
            )
            prompt_started = time.monotonic()
            prompt, _fingerprint = build_messages(
                persona=resolve_persona(config),
                tools=[],
                context=context,
                user_prompt=_build_user_prompt(
                    "followup", config, turn=batch.bot_turns + 1
                ),
            )
            trace_event(
                "prompt",
                "短会话 Prompt 构建",
                output={
                    "message_count": len(prompt),
                    "session_turn": batch.bot_turns + 1,
                },
                duration_ms=(time.monotonic() - prompt_started) * 1000,
            )
            dbg(
                f"群 {group_id} 短会话续聊生成: session={batch.session_id} "
                f"turn={batch.bot_turns + 1} users={batch.user_ids}"
            )
            llm_started = time.monotonic()
            raw = await _generate_proactive_reply(prompt)
            if raw is None:
                trace_event(
                    "llm",
                    "短会话决策",
                    status="failed",
                    output={"response": "none"},
                    detail="LLM 返回空内容",
                    duration_ms=(time.monotonic() - llm_started) * 1000,
                )
                reason = "LLM 返回空内容"
                session.add(
                    AgentAudit(
                        group_id=group_id,
                        actor_user_id=None,
                        tool_name="proactive_reply",
                        arguments={
                            "scene": "followup",
                            "action": "error",
                            "session_turn": batch.bot_turns + 1,
                            "session_id": batch.session_id,
                            "reason": reason,
                        },
                        result="error",
                        detail=reason,
                    )
                )
                await session.commit()
                finish_followup_evaluation(batch, "error")
                return "error"
            decision = _apply_persona_behavior_to_decision(
                config, _decide_proactive_reply(raw or "")
            )
            trace_event(
                "llm",
                "短会话决策",
                status="success" if decision.should_speak else "skipped",
                output={
                    "action": decision.action,
                    "target_user_id": decision.target_user_id,
                    "topic": decision.topic,
                    "reason": decision.reason,
                    "confidence": decision.confidence,
                    "segment_count": len(decision.segments),
                },
                duration_ms=(time.monotonic() - llm_started) * 1000,
            )
            action = decision.action
            reason = decision.reason
            history_text = decision.history_text
            if reason == "LLM 返回了无法解析的 JSON":
                action = "error"
                history_text = ""
            if action == "speak" and history_text.casefold() in {
                str(item.get("text") or "").casefold()
                for item in (config.recent_response_fingerprints or [])
                if isinstance(item, dict)
            }:
                action = "wait"
                history_text = ""
                reason = "续聊内容与近期回复撞重"

            if action != "speak":
                session.add(
                    AgentAudit(
                        group_id=group_id,
                        actor_user_id=None,
                        tool_name="proactive_reply",
                        arguments={
                            "scene": "followup",
                            "action": action,
                            "session_turn": batch.bot_turns + 1,
                            "session_id": batch.session_id,
                            "reason": reason[:240],
                            "target_user_id": decision.target_user_id,
                            "confidence": decision.confidence,
                        },
                        result=action,
                        detail=reason[:500],
                    )
                )
                await session.commit()
                dbg(
                    f"群 {group_id} 短会话决策: session={batch.session_id} "
                    f"action={action} reason={reason!r}"
                )
                finish_followup_evaluation(batch, action)
                return action

            try:
                prepared = await _prepare_proactive_message(
                    decision, session=session, group_id=group_id
                )
                sent_result = await send_prepared_outbound(
                    bot,
                    group_id,
                    prepared,
                    session=session,
                    actor_user_id=None,
                    source="followup",
                )
            except Exception:  # noqa: BLE001
                logger.warning("群 %s 短会话续聊发送失败", group_id)
                dbg_exc(
                    f"群 {group_id} 短会话发送失败 session={batch.session_id}"
                )
                session.add(
                    AgentAudit(
                        group_id=group_id,
                        actor_user_id=None,
                        tool_name="proactive_reply",
                        arguments={
                            "scene": "followup",
                            "action": "send_failed",
                            "session_turn": batch.bot_turns + 1,
                            "session_id": batch.session_id,
                            "reason": "群消息发送失败",
                        },
                        result="send_failed",
                        detail="群消息发送失败",
                    )
                )
                await session.commit()
                finish_followup_evaluation(batch, "send_failed")
                return "send_failed"
            if decision.topic:
                config.active_topic = decision.topic[:240]
            sent_at = now_beijing()
            await _apply_result(
                session,
                config,
                now=sent_at,
                send_result=sent_result,
                history_text=history_text,
                day_count=day_count,
                bot_id=bot_id,
                scene="followup",
                reason=reason,
                session_turn=batch.bot_turns + 1,
                target_user_id=decision.target_user_id,
                confidence=decision.confidence,
            )
            mark_bot_reply(
                bot_id,
                group_id,
                topic=decision.topic or batch.topic,
                source="followup",
                preserve_pending=True,
                max_bot_turns=behavior.max_followup_bot_turns,
            )
            finish_followup_evaluation(batch, "speak")
            try:
                await session.commit()
            except SQLAlchemyError:
                logger.warning("群 %s 短会话续聊状态提交失败", group_id)
                dbg_exc(f"群 {group_id} 短会话状态提交失败,已回滚")
                await session.rollback()
            return "speak"


async def _process_followup(batch: ConversationBatch) -> None:
    started = time.monotonic()
    outcome = "error"
    bot_id, group_id = batch.key
    trace = begin_execution_trace(
        group_id,
        mode="proactive",
        source="runtime",
        trigger_source="conversation_followup",
        message_id=batch.message_ids[-1] if batch.message_ids else None,
    )
    token = bind_execution_trace(trace)
    trace_event(
        "intake",
        "短会话合批进入执行",
        output={
            "trigger_source": "conversation_followup",
            "scene": "followup",
            "session_id": batch.session_id,
            "message_count": len(batch.message_ids),
            "bot_turns": batch.bot_turns,
            "bot_id": bot_id,
        },
    )
    try:
        outcome = await _process_followup_impl(batch)
    except BaseException:
        trace_event("turn", "短会话执行异常", status="failed")
        raise
    finally:
        trace_event(
            "turn",
            "短会话回合结束",
            status="failed" if outcome == "error" else "success",
            output={"outcome": outcome},
            duration_ms=max(time.monotonic() - started, 0.0) * 1000,
        )
        finish_execution_trace(trace, outcome=outcome)
        reset_execution_trace(token)
        try:
            from ..metrics import record_agent_turn

            record_agent_turn(
                "followup",
                outcome,
                max(time.monotonic() - started, 0.0),
                queue_wait_seconds=max(
                    (now_beijing() - batch.cutoff_at).total_seconds(), 0.0
                ),
            )
        except Exception:  # noqa: BLE001
            dbg_exc("Agent 短会话回合指标上报失败(忽略)")


async def _tick() -> None:
    """调度入口；只负责候选筛选，具体回复由普通 Agent 流程处理。"""

    pruned = prune_expired_conversations()
    if pruned:
        dbg(f"主动发言 tick: 清理 {pruned} 个过期短会话")
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


set_followup_handler(_process_followup)


_MEMORY_TRIGGER_COUNT = 16
_MEMORY_MAX_PENDING_AGE = timedelta(minutes=8)
_STARTUP_TASKS: set[asyncio.Task[None]] = set()


async def _memory_due(
    session: Any, group_id: int, now: datetime, *, force: bool
) -> bool:
    config = await session.get(GroupAgentConfig, group_id)
    if (
        config is None
        or not await agent_runtime_enabled(session, group_id, config=config)
        or not memory_retry_due(config, now)
    ):
        return False
    if force or config.memory_rebuild_required:
        return True
    cursor = int(config.last_compacted_message_id or 0)
    # 不按 expires_at 过滤：过期但未整理的消息被 purge 保留等待重试，
    # 它们同样要计入触发条件，否则调度器永远不会为它们发起整理。
    count, oldest = (
        await session.execute(
            select(
                func.count(GroupAgentMessage.id),
                func.min(GroupAgentMessage.received_at),
            ).where(
                GroupAgentMessage.group_id == group_id,
                GroupAgentMessage.id > cursor,
            )
        )
    ).one()
    return int(count or 0) >= _MEMORY_TRIGGER_COUNT or bool(
        oldest and oldest <= now - _MEMORY_MAX_PENDING_AGE
    )


async def _compact_one(group_id: int, now: datetime) -> None:
    try:
        async with get_session() as session:
            config = await session.get(GroupAgentConfig, group_id)
            if config is None or not await agent_runtime_enabled(
                session, group_id, config=config
            ):
                dbg(f"群 {group_id} 记忆整理跳过: Agent 总开关已关闭")
                return
            await compact_group_memory(session, group_id, now=now)
    except Exception as exc:  # noqa: BLE001
        logger.exception("群 %s Agent 记忆整理失败", group_id)
        dbg_exc(f"群 {group_id} 定时记忆整理异常")
        # 模型失败由 memory.py 在原事务中记录；这里兜住连接、ORM 等意外
        # 异常，确保 WebUI 仍能看到可靠失败状态并按统一退避重试。
        try:
            async with get_session() as status_session:
                await record_memory_failure(
                    status_session,
                    group_id,
                    f"整理异常: {type(exc).__name__}",
                    now=now,
                )
        except Exception:  # noqa: BLE001
            logger.exception("群 %s Agent 记忆失败状态写入失败", group_id)


async def _compact_tick(*, force: bool = False, cleanup: bool = False) -> None:
    """按数量或最老消息年龄触发近实时整理；每日任务强制扫尾。"""

    async with get_session() as session:
        group_ids = await list_agent_group_ids(session)
    due: list[int] = []
    async with get_session() as session:
        for group_id in group_ids:
            if await _memory_due(session, group_id, now_beijing(), force=force):
                due.append(group_id)
    dbg(f"定时记忆整理扫描: 群={group_ids} 待执行={due} force={force}")
    if due:
        await asyncio.gather(*(_compact_one(group_id, now_beijing()) for group_id in due))
    if cleanup:
        try:
            async with get_session() as session:
                # 每日一次把沉寂超过 90 天的自动关系边置信度衰减落库，
                # 与读取侧分段降权互补；放在每日任务避免整理路径写放大。
                decayed = await decay_stale_relations(session, now_beijing())
                if decayed:
                    dbg(f"每日关系衰减: 更新 {decayed} 条陈旧 auto 边")
                await session.commit()
            # 媒体清理单独使用事务：cleanup_media_cache 只有在 DB 删除提交
            # 成功后才删磁盘文件，不能和关系衰减共享一个未提交事务。
            async with get_session() as media_session:
                await cleanup_media_cache(media_session)
        except Exception:  # noqa: BLE001
            logger.exception("Agent 每日缓存/关系衰减清理失败")
            dbg_exc("每日媒体缓存清理异常")


@get_driver().on_startup
async def _restore_jobs() -> None:
    dbg("Agent 定时任务启动检查: yawn_core_agent:tick / compact / compact_fast")
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
            kwargs={"force": True, "cleanup": True},
        )
        dbg("已注册定时任务 yawn_core_agent:compact(每日 03:30 记忆整理兜底)")
    # 每分钟廉价扫描；高流量 16 条触发，稀疏群最老待处理消息 8 分钟触发。
    if scheduler.get_job("yawn_core_agent:compact_fast") is None:
        scheduler.add_job(
            _compact_tick,
            "interval",
            minutes=1,
            id="yawn_core_agent:compact_fast",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        dbg("已注册定时任务 yawn_core_agent:compact_fast(每分钟近实时整理扫描)")
    startup_task = asyncio.create_task(_compact_tick(force=True))
    _STARTUP_TASKS.add(startup_task)
    startup_task.add_done_callback(_STARTUP_TASKS.discard)


@get_driver().on_shutdown
async def _shutdown_jobs() -> None:
    await shutdown_conversations()
    for task in list(_STARTUP_TASKS):
        task.cancel()
    if _STARTUP_TASKS:
        await asyncio.gather(*_STARTUP_TASKS, return_exceptions=True)
    _STARTUP_TASKS.clear()


__all__ = ["should_proactively_speak"]
