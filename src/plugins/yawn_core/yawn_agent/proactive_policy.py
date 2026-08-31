# ruff: noqa: E501,TID252,TC001,C901,PLR0911,PLR0912,PLR0913,RUF022
"""主动发言策略、Prompt 与结构化决策解析。

调度、数据库和发送副作用留在 proactive.py；本模块保持纯策略逻辑，便于独立测试。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from ..data_models.group_agent_config import GroupAgentConfig
from .context import ActivitySnapshot, is_recent
from .conversation import CONVERSATION_MAX_BOT_TURNS
from .log import dbg
from .memory import parse_json_reply
from .outbound import MAX_OUTBOUND_SEGMENTS
from .persona import (
    PersonaBehavior,
    persona_behavior,
    persona_behavior_instruction,
)

_ACTIVE_MIN_GAP_SECONDS = 30.0
_ACTIVE_MIN_MEMBER_MESSAGES = 2
_BUSY_MEMBER_MESSAGES_5M = 6
_BUSY_MEMBER_PARTICIPANTS_5M = 2
_POST_REPLY_GUARD_MINUTES = 5.0
_WARMUP_PROBABILITY_CAP = 0.6
_PROACTIVE_SEGMENT_TYPES = frozenset({"text", "reply", "at", "face", "reaction"})

_ACTIVE_INTERJECT_PROMPT = (
    "群里正在聊天。先读懂最近的消息：现在在聊什么话题、谁在积极参与、"
    "聊到哪一步了、气氛如何，注意消息里的 minutes_ago 是几分钟前发的。\n"
    "只有能回应具体问题、补充新信息或接住一个明确的梗时才开口。"
    "群友彼此聊得顺畅、内容只是在重复改写、或你只能泛泛附和时保持沉默(speak=false)。\n"
    "插话时顺着话题对某条具体消息或具体观点做出反应，"
    "像随手打的一条消息：1~2 句、口语化，可以带点情绪或吐槽；"
    "不要开场白和客套，不要总结聊天记录，不要只回“哈哈”“确实”这类泛泛附和，"
    "不要自称 AI 或助手。"
)

_WARMUP_PROMPT = (
    "群里冷场有一会儿了。先回想冷场前群里最后在聊什么。\n"
    "只有存在自然切入点时才开口——"
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
    "回应群友或自然接梗时才 speak。连续同义复述、互相总结、没有新增事实或问题时"
    "直接 close，不要再换一种说法总结，也不要靠结尾反问延长对话。\n"
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
    '"target_user_id": "主要回应对象的 QQ 号；没有明确对象时为 null", '
    '"topic": "当前话题的简短概括", '
    '"reason": "一句话说明为何开口或沉默", '
    '"confidence": 0.8, '
    '"message": {"segments": [{"type": "text/reply/at/face/reaction", "...": "对应字段"}]}, '
    '"text": "兼容旧模型的纯文本；使用 message 时可省略，保持沉默时为空字符串"}。'
    "主动消息只允许 text/reply/at/face/reaction；reply.message_id 与 at.user_id 必须来自上下文，"
    "reaction_id 必须是系统表情包索引中的真实 ID；不确定时不要使用 reaction，禁止猜图片路径。"
)


@dataclass(frozen=True, slots=True)
class SpeakDecision:
    """纯“是否开口”决策，不承载最终要发送的文本/消息段。"""

    action: str
    target_user_id: int | None
    topic: str | None
    reason: str
    confidence: float = 0.5

    @property
    def should_speak(self) -> bool:
        return self.action == "speak"


@dataclass(frozen=True, slots=True, init=False)
class ProactiveDecision:
    """SpeakDecision + 最终消息计划。

    自定义 init 保留 P3 期间 ``ProactiveDecision(action, text, topic, reason, segments)``
    的兼容调用；内部状态已经把“为什么/是否说”与“说什么”分开。
    """

    decision: SpeakDecision
    text: str
    segments: tuple[dict[str, Any], ...] = ()

    def __init__(
        self,
        action: str,
        text: str,
        topic: str | None,
        reason: str,
        segments: tuple[dict[str, Any], ...] = (),
        *,
        target_user_id: int | None = None,
        confidence: float = 0.5,
    ) -> None:
        object.__setattr__(
            self,
            "decision",
            SpeakDecision(
                action=action,
                target_user_id=target_user_id,
                topic=topic,
                reason=reason,
                confidence=clamp_probability(confidence),
            ),
        )
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "segments", segments)

    @property
    def action(self) -> str:
        return self.decision.action

    @property
    def target_user_id(self) -> int | None:
        return self.decision.target_user_id

    @property
    def topic(self) -> str | None:
        return self.decision.topic

    @property
    def reason(self) -> str:
        return self.decision.reason

    @property
    def confidence(self) -> float:
        return self.decision.confidence

    @property
    def speak_decision(self) -> SpeakDecision:
        return self.decision

    @property
    def should_speak(self) -> bool:
        return self.decision.should_speak and (bool(self.text) or bool(self.segments))

    @property
    def history_text(self) -> str:
        if self.text:
            return self.text
        labels = [str(item.get("type") or "") for item in self.segments]
        return f"[结构化消息: {','.join(item for item in labels if item)}]"


class RandomSource(Protocol):
    def random(self) -> float: ...


def clamp_probability(value: Any) -> float:
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return 0.0


def warmup_probability(config: GroupAgentConfig, idle_seconds: float) -> float:
    """冷得越久越可能开口，但 Persona 只能在运行概率上限内继续收窄。"""

    base = clamp_probability(config.proactive_probability)
    threshold = max(int(config.idle_threshold_minutes), 1) * 60
    idle_scale = 1.0 + min(max(idle_seconds / threshold - 1.0, 0.0), 1.0)
    behavior_scale = persona_behavior(config).warmup_probability_scale
    return min(base * idle_scale, _WARMUP_PROBABILITY_CAP) * behavior_scale


def skip_backoff_timestamp(now: datetime, cooldown_minutes: int) -> datetime:
    """内容门跳过/生成失败后只保留半个冷却期，至少退避 2 分钟。"""

    cooldown = max(int(cooldown_minutes), 0)
    backoff = max(2, cooldown // 2)
    return now - timedelta(minutes=cooldown - backoff)


def decide_proactive_reply(raw: str) -> ProactiveDecision:
    """解析模型结构化决策；不合法 JSON 纯文本回退为直接发言。"""

    cleaned = raw.strip()
    if not cleaned:
        return ProactiveDecision("wait", "", None, "LLM 返回空内容")
    parsed = parse_json_reply(cleaned)
    if parsed is not None:
        text = str(parsed.get("text") or "").strip()
        segments: tuple[dict[str, Any], ...] = ()
        raw_message = parsed.get("message")
        if isinstance(raw_message, dict):
            raw_segments = raw_message.get("segments")
            if isinstance(raw_segments, list) and 0 < len(raw_segments) <= MAX_OUTBOUND_SEGMENTS:
                if all(
                    isinstance(item, dict)
                    and str(item.get("type") or "").strip().lower()
                    in _PROACTIVE_SEGMENT_TYPES
                    for item in raw_segments
                ):
                    segments = tuple(dict(item) for item in raw_segments)
                else:
                    return ProactiveDecision(
                        "wait", "", None, "主动消息包含不允许的消息段"
                    )
        if segments:
            text = "".join(
                str(item.get("text") or "")
                for item in segments
                if str(item.get("type") or "").strip().lower() == "text"
            ).strip()
        raw_action = str(parsed.get("action") or "").strip().lower()
        if raw_action not in {"speak", "wait", "close"}:
            raw_action = (
                "speak" if bool(parsed.get("speak")) and (text or segments) else "wait"
            )
        if raw_action == "speak" and not text and not segments:
            raw_action = "wait"
        topic = str(parsed.get("topic") or "").strip() or None
        reason = str(parsed.get("reason") or "").strip() or (
            "模型未说明理由"
            if raw_action == "speak"
            else "模型判定此刻不适合发言"
        )
        raw_target = parsed.get("target_user_id")
        try:
            target_user_id = int(raw_target) if raw_target is not None else None
        except (TypeError, ValueError):
            target_user_id = None
        if target_user_id is not None and target_user_id <= 0:
            target_user_id = None
        confidence = clamp_probability(parsed.get("confidence", 0.5))
        return ProactiveDecision(
            raw_action,
            text if raw_action == "speak" else "",
            topic,
            reason,
            segments if raw_action == "speak" else (),
            target_user_id=target_user_id,
            confidence=confidence,
        )
    if cleaned.startswith(("{", "```")):
        return ProactiveDecision("wait", "", None, "LLM 返回了无法解析的 JSON")
    return ProactiveDecision(
        "speak", cleaned, None, "模型按纯文本回复,回退为直接发言"
    )


def apply_persona_behavior_to_decision(
    config: GroupAgentConfig, decision: ProactiveDecision
) -> ProactiveDecision:
    """对模型给出的主动消息执行 Persona 行为硬约束。

    当前只限制“主动 reaction”：用户明确要求表情包时走普通 dialogue 工具路径，
    不受这里影响。低 reaction 倾向的角色不能在主动/续聊场景自己刷表情包。
    """

    behavior = persona_behavior(config)
    if behavior.allow_spontaneous_reaction or not decision.segments:
        return decision
    filtered = tuple(
        item
        for item in decision.segments
        if str(item.get("type") or "").strip().lower() != "reaction"
    )
    if filtered == decision.segments:
        return decision
    text = "".join(
        str(item.get("text") or "")
        for item in filtered
        if str(item.get("type") or "").strip().lower() == "text"
    ).strip() or decision.text
    if not filtered and not text:
        return ProactiveDecision(
            "wait",
            "",
            decision.topic,
            "Persona 禁止当前角色主动使用 reaction",
            target_user_id=decision.target_user_id,
            confidence=decision.confidence,
        )
    return ProactiveDecision(
        decision.action,
        text,
        decision.topic,
        decision.reason,
        filtered,
        target_user_id=decision.target_user_id,
        confidence=decision.confidence,
    )


def recent_proactive_lines(config: GroupAgentConfig) -> list[str]:
    lines = [
        str(item.get("text") or "").strip()
        for item in (config.recent_response_fingerprints or [])
        if isinstance(item, dict) and item.get("input") == "proactive"
    ]
    return [line for line in lines if line][-4:]


def build_user_prompt(
    scene: str,
    config: GroupAgentConfig,
    *,
    turn: int | None = None,
    behavior: PersonaBehavior | None = None,
) -> str:
    """Build a prompt for an internally selected participation scene."""
    if scene == "active":
        base = _ACTIVE_INTERJECT_PROMPT
    elif scene == "followup":
        base = _FOLLOWUP_PROMPT
    else:
        base = _WARMUP_PROMPT
    resolved_behavior = behavior or persona_behavior(config)
    parts = [
        base,
        persona_behavior_instruction(resolved_behavior, scene=scene),
        _MEMORY_USE_PROMPT,
    ]
    if turn is not None:
        max_turns = min(
            CONVERSATION_MAX_BOT_TURNS, resolved_behavior.max_followup_bot_turns
        )
        parts.append(
            f"这是本话题中 Bot 的第 {turn} 条候选发言，当前 Persona 最多 "
            f"{max_turns} 条。"
        )
    parts.append(_JSON_PROTOCOL)
    recent = recent_proactive_lines(config)
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
    rng: RandomSource | None = None,
) -> str | None:
    """返回内部参与场景 active / warmup / None；只做策略判定，不执行副作用。"""

    group_id = getattr(config, "group_id", None)
    if not config.enabled:
        dbg(f"群 {group_id} 主动发言拒绝: Agent 未启用")
        return None
    if not bool(getattr(config, "proactive_enabled", True)):
        dbg(f"群 {group_id} 主动发言拒绝: 主动参与已关闭")
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
            f"(上次主动 {snapshot.last_proactive_at},冷却 {config.cooldown_minutes} 分钟)"
        )
        return None
    if is_recent(snapshot.last_agent_at, now, _POST_REPLY_GUARD_MINUTES):
        dbg(
            f"群 {group_id} 主动发言拒绝: 刚回复过消息,短守卫期内"
            f"(最后发言 {snapshot.last_agent_at},守卫 {_POST_REPLY_GUARD_MINUTES:.0f} 分钟)"
        )
        return None
    roll = (rng or random).random()

    if snapshot.last_message_at is not None:
        idle_seconds = (now - snapshot.last_message_at).total_seconds()
        if idle_seconds >= config.idle_threshold_minutes * 60:
            probability = warmup_probability(config, idle_seconds)
            if roll < probability:
                dbg(
                    f"群 {group_id} 暖场场景触发: 已冷场 {idle_seconds:.0f}s "
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
    behavior = persona_behavior(config)
    probability = min(
        clamp_probability(config.proactive_active_probability)
        * behavior.active_probability_scale,
        clamp_probability(config.proactive_active_probability),
    )
    if roll < probability:
        dbg(
            f"群 {group_id} 插话场景触发: {'持续刷屏' if busy_flow else '自然间隙'} "
            f"真人消息 {member_idle:.0f}s 前 roll={roll:.3f} probability={probability:.2f} "
            f"5m 真人={snapshot.member_messages_5m}/{snapshot.member_participants_5m}人 "
            f"60m 真人消息={snapshot.member_messages_60m}"
        )
        return "active"
    dbg(
        f"群 {group_id} 插话场景骰子未中: roll={roll:.3f} probability={probability:.2f}"
    )
    return None


__all__ = [
    "ProactiveDecision",
    "RandomSource",
    "apply_persona_behavior_to_decision",
    "build_user_prompt",
    "clamp_probability",
    "decide_proactive_reply",
    "recent_proactive_lines",
    "should_proactively_speak",
    "skip_backoff_timestamp",
    "warmup_probability",
    "_ACTIVE_INTERJECT_PROMPT",
    "_FOLLOWUP_PROMPT",
    "_JSON_PROTOCOL",
    "_MEMORY_USE_PROMPT",
    "_PROACTIVE_SEGMENT_TYPES",
    "_WARMUP_PROMPT",
]
