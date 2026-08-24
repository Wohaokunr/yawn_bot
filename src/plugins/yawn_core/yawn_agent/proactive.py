# ruff: noqa: E501,F401,I001,TID252,PLR0911,PLR0912,PLR0913,PLR0915,PLR0917,C901,SIM117,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100,TC003,PLR2004,PERF203,PERF401
"""主动发言双模式：热闹时像真人群友一样插话，冷场时偶尔暖场。"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from nonebot import get_bots, get_driver, logger
from nonebot.adapters.onebot.v11 import Message
from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_orm import get_session
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from ..data_models.agent_audit import AgentAudit
from ..data_models.group_agent_config import GroupAgentConfig
from ..data_models.group_agent_message import GroupAgentMessage
from ..llm import ai_config, complete
from .context import ActivitySnapshot, is_recent, now_beijing
from .collector import group_lock
from .config_store import list_agent_group_ids
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
    _extract_message_id,
    _load_context,
    persist_bot_reply,
)
from .log import dbg, dbg_exc
from .memory import (
    compact_group_memory,
    decay_stale_relations,
    memory_retry_due,
    parse_json_reply,
    record_memory_failure,
)
from .media import cleanup_media_cache
from .persona import resolve_persona
from .prompt import build_messages

# 普通活跃场景等 30 秒把零散消息合起来；持续刷屏由高活跃通道直接候选，
# 不再依赖群聊出现 90 秒空隙。
_ACTIVE_MIN_GAP_SECONDS = 30.0
# 60 分钟内真人消息不足此数说明群里并没有在聊，不插话。
_ACTIVE_MIN_MEMBER_MESSAGES = 2
_BUSY_MEMBER_MESSAGES_5M = 6
_BUSY_MEMBER_PARTICIPANTS_5M = 2
# 被动回复（被@答话）后的短守卫：刚答完话立刻主动插话显得话痨，
# 但只挡这几分钟，不再封锁整个主动冷却期。
_POST_REPLY_GUARD_MINUTES = 5.0
# 暖场概率封顶：冷得再久也别超过这个值，防止死群被高频轰炸。
_WARMUP_PROBABILITY_CAP = 0.6
# 主动回复可能启用任务级推理且要输出 JSON；384 tokens 会让推理模型在正文前
# 触发 finish_reason="length"，因此至少给 2048，并保留部署方更高的全局预算。
_PROACTIVE_MIN_MAX_TOKENS = 2048
_PROACTIVE_TIMEOUT_SECONDS = 25.0

_ACTIVE_INTERJECT_PROMPT = (
    "群里正在聊天。先读懂最近的消息：现在在聊什么话题、谁在积极参与、"
    "聊到哪一步了、气氛如何，注意消息里的 minutes_ago 是几分钟前发的。\n"
    "你已经被选中可以插话，默认值得开口——像真人群友一样自然加入聊天。"
    "只有明显不合适时才保持沉默(speak=false)：正在聊非常私密或敏感的话题、"
    "有人正在激烈争吵、或你确实对当前内容毫无反应可说。\n"
    "插话时顺着话题对某条具体消息或具体观点做出反应，"
    "像随手打的一条消息：1~2 句、口语化，可以带点情绪或吐槽；"
    "不要开场白和客套，不要总结聊天记录，不要只回“哈哈”“确实”这类泛泛附和，"
    "不要自称 AI 或助手。"
)

_WARMUP_PROMPT = (
    "群里冷场有一会儿了。先回想冷场前群里最后在聊什么。\n"
    "你已经被选中可以开口，默认值得说点什么活跃气氛——"
    "接上没聊完的话题、分享一个贴合群成员兴趣的小见闻、"
    "或抛一个轻松的新话题都可以。只有确实找不到任何不突兀的开口方式时"
    "才保持沉默(speak=false)。\n"
    "开口时 1~2 句、口语化、自然随意；"
    "不要问“大家在吗”“在干什么”，不要自称 AI 或助手。"
)

_FOLLOWUP_PROMPT = (
    "你刚刚已经参与了这个话题，群友随后又发来一批消息。判断他们是否在延续、"
    "回应或自然关联到当前话题。不要因为每条新消息都抢着回答：群友彼此聊得顺畅时"
    "可以 wait；话题已结束、明显转移或不适合继续时 close；只有确实能推动对话、"
    "回应群友或自然接梗时才 speak。\n"
    "续聊必须承接当前批次，1~2 句、口语化，不重新打招呼，不另起无关话题。"
)

_MEMORY_USE_PROMPT = (
    "结合上下文中的长期记忆，但要自然隐式使用：群总结用于判断群里常聊什么和"
    "未完话题；人物画像用于称呼、兴趣切入点和表达分寸；人物关系只用于调整互动"
    "语气。当前消息永远优先于旧记忆；只在高度相关时体现，不复述记忆清单、"
    "source_scope 或关系清单，也不要声称自己在读取记忆。"
)

_JSON_PROTOCOL = (
    "只返回 JSON，不要输出其他任何内容："
    '{"action": "speak、wait 或 close", "speak": true或false, '
    '"topic": "当前话题的简短概括", '
    '"reason": "一句话说明为何开口或沉默", '
    '"text": "要发送的消息，保持沉默时为空字符串"}'
)


async def _generate_proactive_reply(messages: list[Any]) -> str | None:
    """用统一预算生成主动首轮或短会话续聊决策。"""

    return await complete(  # pyright: ignore[reportArgumentType]
        messages,  # pyright: ignore[reportArgumentType]
        task="agent_proactive",
        max_tokens=max(_PROACTIVE_MIN_MAX_TOKENS, int(ai_config.ai_max_tokens)),
        timeout=_PROACTIVE_TIMEOUT_SECONDS,
    )


def _clamp_probability(value: Any) -> float:
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return 0.0


def _warmup_probability(config: GroupAgentConfig, idle_seconds: float) -> float:
    """冷得越久越可能开口：阈值时为基准值，两倍阈值后翻倍，封顶 0.6。"""

    base = _clamp_probability(config.proactive_probability)
    threshold = max(int(config.idle_threshold_minutes), 1) * 60
    scale = 1.0 + min(max(idle_seconds / threshold - 1.0, 0.0), 1.0)
    return min(base * scale, _WARMUP_PROBABILITY_CAP)


def _skip_backoff_timestamp(now: datetime, cooldown_minutes: int) -> datetime:
    """内容门跳过/生成失败后的退避时间戳：让剩余冷却只剩半个冷却期
    （至少 2 分钟），而不是整个冷却期；未发言也绝不刷新 last_agent_at。"""

    cooldown = max(int(cooldown_minutes), 0)
    backoff = max(2, cooldown // 2)
    # 冷却比退避短时时间戳会略微落在未来，效果等同于把剩余冷却拉到退避值。
    return now - timedelta(minutes=cooldown - backoff)


@dataclass(frozen=True, slots=True)
class _ProactiveDecision:
    action: str
    text: str
    topic: str | None
    reason: str

    @property
    def should_speak(self) -> bool:
        return self.action == "speak" and bool(self.text)


class _RandomSource(Protocol):
    def random(self) -> float: ...


def _decide_proactive_reply(raw: str) -> _ProactiveDecision:
    """解析模型的结构化决策；不合法 JSON 的纯文本回退为直接发言。"""

    cleaned = raw.strip()
    if not cleaned:
        return _ProactiveDecision(
            action="wait", text="", topic=None, reason="LLM 返回空内容"
        )
    parsed = parse_json_reply(cleaned)
    if parsed is not None:
        text = str(parsed.get("text") or "").strip()
        raw_action = str(parsed.get("action") or "").strip().lower()
        if raw_action not in {"speak", "wait", "close"}:
            raw_action = "speak" if bool(parsed.get("speak")) and text else "wait"
        if raw_action == "speak" and not text:
            raw_action = "wait"
        topic = str(parsed.get("topic") or "").strip() or None
        reason = str(parsed.get("reason") or "").strip() or (
            "模型未说明理由"
            if raw_action == "speak"
            else "模型判定此刻不适合发言"
        )
        return _ProactiveDecision(
            action=raw_action,
            text=text if raw_action == "speak" else "",
            topic=topic,
            reason=reason,
        )
    # 疑似 JSON 的碎片不可直接发到群里；纯文本说明模型没走 JSON 协议，
    # 按旧行为整段当发言发出去，保持对不吐 JSON 模型的兼容。
    if cleaned.startswith(("{", "```")):
        return _ProactiveDecision(
            action="wait",
            text="",
            topic=None,
            reason="LLM 返回了无法解析的 JSON",
        )
    return _ProactiveDecision(
        action="speak",
        text=cleaned,
        topic=None,
        reason="模型按纯文本回复,回退为直接发言",
    )


def _recent_proactive_lines(config: GroupAgentConfig) -> list[str]:
    """最近主动发言原文；注入提示词让模型不重复相近说法。"""

    lines = [
        str(item.get("text") or "").strip()
        for item in (config.recent_response_fingerprints or [])
        if isinstance(item, dict) and item.get("input") == "proactive"
    ]
    return [line for line in lines if line][-4:]


def _build_user_prompt(
    mode: str, config: GroupAgentConfig, *, turn: int | None = None
) -> str:
    if mode == "active":
        base = _ACTIVE_INTERJECT_PROMPT
    elif mode == "followup":
        base = _FOLLOWUP_PROMPT
    else:
        base = _WARMUP_PROMPT
    parts = [base, _MEMORY_USE_PROMPT]
    if turn is not None:
        parts.append(
            f"这是本话题中 Bot 的第 {turn} 条候选发言，最多 "
            f"{CONVERSATION_MAX_BOT_TURNS} 条。"
        )
    parts.append(_JSON_PROTOCOL)
    recent = _recent_proactive_lines(config)
    if recent:
        parts.append(
            "你最近主动发言过：\n"
            + "\n".join(f"- {line}" for line in recent)
            + "\n不要重复相近的说法或同一话题的同类反应。"
        )
    return "\n".join(parts)


def should_proactively_speak(
    config: GroupAgentConfig,
    snapshot: ActivitySnapshot,
    now: datetime,
    *,
    rng: _RandomSource | None = None,
) -> str | None:
    """返回本次主动发言模式："active"（热闹插话）、"warmup"（冷场暖场）或 None。

    两模式天然互斥：暖场要求全部消息冷场超阈值，插话要求真人消息
    落在窗口内，因此同一时刻至多一个分支可用。

    冷却基准与被动回复解耦：主动→主动的冷却看 last_proactive_at；
    被@答话只触发 _POST_REPLY_GUARD_MINUTES 的短守卫，不再封锁
    整个主动冷却期。
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
    if is_recent(snapshot.last_proactive_at, now, int(config.cooldown_minutes)):
        dbg(
            f"群 {group_id} 主动发言拒绝: 主动冷却中"
            f"(上次主动 {snapshot.last_proactive_at},"
            f"冷却 {config.cooldown_minutes} 分钟)"
        )
        return None
    if is_recent(snapshot.last_agent_at, now, _POST_REPLY_GUARD_MINUTES):
        dbg(
            f"群 {group_id} 主动发言拒绝: 刚回复过消息,短守卫期内"
            f"(最后发言 {snapshot.last_agent_at},"
            f"守卫 {_POST_REPLY_GUARD_MINUTES:.0f} 分钟)"
        )
        return None
    roll = (rng or random).random()

    # 暖场模式：idle 含 bot 自己的发言——bot 刚说完话不会立刻再暖场。
    if snapshot.last_message_at is not None:
        idle_seconds = (now - snapshot.last_message_at).total_seconds()
        if idle_seconds >= config.idle_threshold_minutes * 60:
            probability = _warmup_probability(config, idle_seconds)
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

    # 插话模式：普通群先等 30 秒合批；持续刷屏群允许在消息流中候选，
    # 否则它们永远等不到自然间隙，反而比安静群更难触发。
    if not config.proactive_active_enabled:
        dbg(f"群 {group_id} 主动发言拒绝: 热闹插话未开启")
        return None
    if snapshot.last_member_message_at is None:
        dbg(f"群 {group_id} 主动发言拒绝: 60 分钟内没有真人消息")
        return None
    member_idle = (now - snapshot.last_member_message_at).total_seconds()
    window_seconds = max(int(config.proactive_active_window_minutes), 1) * 60
    busy_flow = (
        snapshot.member_messages_5m >= _BUSY_MEMBER_MESSAGES_5M
        and snapshot.member_participants_5m >= _BUSY_MEMBER_PARTICIPANTS_5M
    )
    if not busy_flow and member_idle < _ACTIVE_MIN_GAP_SECONDS:
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
            f"群 {group_id} 插话模式触发: {'持续刷屏' if busy_flow else '自然间隙'} "
            f"真人消息 {member_idle:.0f}s 前 "
            f"roll={roll:.3f} probability={probability:.2f} "
            f"5m 真人={snapshot.member_messages_5m}/"
            f"{snapshot.member_participants_5m}人 "
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
    mode: str,
    reason: str,
    session_turn: int,
) -> None:
    """在调用方事务内落库：成功推进 last_proactive_at/last_agent_at；
    未发出（内容门拦截、生成失败、撞重）只把 last_proactive_at 回拨到
    剩余半个冷却期，既防下分钟反复抽卡，也不让一次跳过封锁太久。"""

    if text:
        config.last_proactive_at = now
        config.last_agent_at = now
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
                arguments={
                    "mode": mode,
                    "action": "speak",
                    "session_turn": session_turn,
                    "reason": reason[:240],
                },
                result="speak",
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
        backoff_at = _skip_backoff_timestamp(now, int(config.cooldown_minutes))
        if config.last_proactive_at is None or config.last_proactive_at < backoff_at:
            config.last_proactive_at = backoff_at
        dbg(
            f"群 {config.group_id} 主动发言未发出,退避剩余半个冷却期至 {backoff_at}"
        )


async def _process_candidate_impl(candidate: dict[str, Any], bots: list[Any]) -> str:
    group_id = candidate["group_id"]
    mode = candidate["mode"]
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
                return "close"
            # 复用对话路径的完整上下文：40 条消息、成员、记忆与关系，
            # 让插话贴着群里的真实话题而不是只看 8 条消息的切片；
            # 消息附带 minutes_ago 便于模型判断话题的新旧与节奏。
            context = await _load_context(
                session,
                group_id,
                config,
                include_message_age=True,
                include_active_profiles=True,
            )
            prompt, _fingerprint = build_messages(
                persona=resolve_persona(config),
                tools=[],
                context=context,
                user_prompt=_build_user_prompt(mode, config, turn=1),
            )
            dbg(f"群 {group_id} 主动发言生成: 模式={mode}, 请求 LLM")
            raw = await _generate_proactive_reply(prompt)
            now = now_beijing()
            if raw is None:
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
                    mode=mode,
                    reason="LLM 返回空内容",
                    session_turn=1,
                )
                session.add(
                    AgentAudit(
                        group_id=group_id,
                        actor_user_id=None,
                        tool_name="proactive_reply",
                        arguments={
                            "mode": mode,
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
            decision = _decide_proactive_reply(raw)
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
                            "mode": mode,
                            "action": decision.action,
                            "session_turn": 1,
                            "reason": decision.reason[:240],
                        },
                        result=decision.action,
                        detail=decision.reason[:500],
                    )
                )
                await _apply_result(
                    session,
                    config,
                    now=now,
                    text=None,
                    day_count=candidate["day_count"],
                    bot_id=primary_self_id,
                    message_id=None,
                    mode=mode,
                    reason=decision.reason,
                    session_turn=1,
                )
                await session.commit()
                return decision.action
            text = decision.text
            dbg(f"群 {group_id} 主动发言生成结果: {text!r} reason={decision.reason!r}")
            if text.casefold() in {
                str(item.get("text") or "").casefold()
                for item in (config.recent_response_fingerprints or [])
                if isinstance(item, dict)
            }:
                dbg(f"群 {group_id} 主动发言与近期回复撞重,跳过发送: {text!r}")
                session.add(
                    AgentAudit(
                        group_id=group_id,
                        actor_user_id=None,
                        tool_name="proactive_reply",
                        arguments={
                            "mode": mode,
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
                    text=None,
                    day_count=candidate["day_count"],
                    bot_id=primary_self_id,
                    message_id=None,
                    mode=mode,
                    reason="近期回复撞重",
                    session_turn=1,
                )
                await session.commit()
                return "wait"
            # 多机器人部署下逐个尝试；通常只有一个连接，首个即成功。
            sent = False
            sent_message_id: int | None = None
            sent_bot_id = primary_self_id
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
                sent_bot_id = int(str(getattr(bot, "self_id", "") or 0))
                sent_message_id = _extract_message_id(result)
                dbg(
                    f"群 {group_id} 主动发言发送成功 bot={getattr(bot, 'self_id', None)}"
                )
                break
            if not sent:
                logger.warning("群 %s 主动消息无可用机器人发送", group_id)
                dbg(f"群 {group_id} 主动发言失败: {len(bots)} 个机器人均发送失败")
                session.add(
                    AgentAudit(
                        group_id=group_id,
                        actor_user_id=None,
                        tool_name="proactive_reply",
                        arguments={
                            "mode": mode,
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
                text=text,
                day_count=candidate["day_count"],
                bot_id=sent_bot_id,
                message_id=sent_message_id,
                mode=mode,
                reason=decision.reason,
                session_turn=1,
            )
            if config.short_conversation_enabled:
                mark_bot_reply(
                    sent_bot_id,
                    group_id,
                    topic=decision.topic or str(config.active_topic or ""),
                    source=mode,
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
    try:
        outcome = await _process_candidate_impl(candidate, bots)
    finally:
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
                or not config.enabled
                or not config.short_conversation_enabled
            ):
                close_conversation(bot_id, group_id, reason="配置关闭短会话续聊")
                return "close"
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
            context = await _load_context(
                session,
                group_id,
                config,
                bot_id,
                include_message_age=True,
                focus_user_ids=batch.user_ids,
                message_cutoff=batch.cutoff_at,
                include_active_profiles=True,
            )
            prompt, _fingerprint = build_messages(
                persona=resolve_persona(config),
                tools=[],
                context=context,
                user_prompt=_build_user_prompt(
                    "followup", config, turn=batch.bot_turns + 1
                ),
            )
            dbg(
                f"群 {group_id} 短会话续聊生成: session={batch.session_id} "
                f"turn={batch.bot_turns + 1} users={batch.user_ids}"
            )
            raw = await _generate_proactive_reply(prompt)
            if raw is None:
                reason = "LLM 返回空内容"
                session.add(
                    AgentAudit(
                        group_id=group_id,
                        actor_user_id=None,
                        tool_name="proactive_reply",
                        arguments={
                            "mode": "followup",
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
            decision = _decide_proactive_reply(raw or "")
            action = decision.action
            reason = decision.reason
            text = decision.text
            if reason == "LLM 返回了无法解析的 JSON":
                action = "error"
                text = ""
            if action == "speak" and text.casefold() in {
                str(item.get("text") or "").casefold()
                for item in (config.recent_response_fingerprints or [])
                if isinstance(item, dict)
            }:
                action = "wait"
                text = ""
                reason = "续聊内容与近期回复撞重"

            if action != "speak":
                session.add(
                    AgentAudit(
                        group_id=group_id,
                        actor_user_id=None,
                        tool_name="proactive_reply",
                        arguments={
                            "mode": "followup",
                            "action": action,
                            "session_turn": batch.bot_turns + 1,
                            "session_id": batch.session_id,
                            "reason": reason[:240],
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
                result = await bot.call_api(
                    "send_group_msg",
                    group_id=group_id,
                    message=Message(text),
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
                            "mode": "followup",
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
            sent_message_id = _extract_message_id(result)
            if decision.topic:
                config.active_topic = decision.topic[:240]
            sent_at = now_beijing()
            await _apply_result(
                session,
                config,
                now=sent_at,
                text=text,
                day_count=day_count,
                bot_id=bot_id,
                message_id=sent_message_id,
                mode="followup",
                reason=reason,
                session_turn=batch.bot_turns + 1,
            )
            mark_bot_reply(
                bot_id,
                group_id,
                topic=decision.topic or batch.topic,
                source="followup",
                preserve_pending=True,
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
    try:
        outcome = await _process_followup_impl(batch)
    finally:
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
    if config is None or not memory_retry_due(config, now):
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
                await cleanup_media_cache(session)
                await session.commit()
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
