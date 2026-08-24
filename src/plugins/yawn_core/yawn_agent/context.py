# ruff: noqa: E501,F401,I001,TID252,PLR0912,PLR0913,PLR0917,C901,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100,RUF046,DTZ005
"""群聊 Agent 上下文和活跃度计算。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

_CST = timezone(timedelta(hours=8))
_MESSAGE_CHAR_LIMIT = 800
_MESSAGE_CONTEXT_CHAR_BUDGET = 6_000


def now_beijing() -> datetime:
    """北京时间 naive；与 yawn_core 其余模块的时间约定保持一致。"""

    return datetime.now(_CST).replace(tzinfo=None)


@dataclass(frozen=True, slots=True)
class ActivitySnapshot:
    last_message_at: datetime | None
    messages_5m: int = 0
    messages_20m: int = 0
    messages_60m: int = 0
    participants_60m: int = 0
    replies_60m: int = 0
    mentions_60m: int = 0
    last_agent_at: datetime | None = None
    # 主动发言专用冷却基准；与被动回复刷新的 last_agent_at 解耦。
    last_proactive_at: datetime | None = None
    proactive_today: int = 0
    # bot 发言落库后，"群里有人说话"要区分真人与 bot 自言。
    last_member_message_at: datetime | None = None
    member_messages_60m: int = 0
    member_messages_5m: int = 0
    member_participants_5m: int = 0


def coldness_score(snapshot: ActivitySnapshot, now: datetime) -> float:
    if snapshot.last_message_at is None:
        return 1.0
    idle = max((now - snapshot.last_message_at).total_seconds() / 1800.0, 0.0)
    activity = min(snapshot.messages_5m / 5, 1.0) * 0.4
    activity += min(snapshot.messages_20m / 20, 1.0) * 0.3
    activity += min(snapshot.participants_60m / 8, 1.0) * 0.2
    interaction = min((snapshot.replies_60m + snapshot.mentions_60m) / 8, 1.0) * 0.1
    return max(0.0, min(1.0, idle / 2.0 + 0.35 - activity - interaction))


def is_recent(
    timestamp: datetime | None, now: datetime, minutes: float
) -> bool:
    """timestamp 是否落在 now 往前 minutes 分钟内；None 视为很久以前。"""

    return bool(
        timestamp and now - timestamp < timedelta(minutes=max(minutes, 0))
    )


def is_cooldown_active(
    snapshot: ActivitySnapshot, now: datetime, cooldown_minutes: int
) -> bool:
    return is_recent(snapshot.last_agent_at, now, cooldown_minutes)


def trim_context_messages(
    messages: list[dict[str, Any]],
    *,
    max_messages: int = 40,
    max_message_chars: int = _MESSAGE_CHAR_LIMIT,
    char_budget: int = _MESSAGE_CONTEXT_CHAR_BUDGET,
) -> list[dict[str, Any]]:
    """从最新消息向前保留有界历史，并截断单条超长文本。"""

    if max_messages <= 0 or max_message_chars <= 0 or char_budget <= 0:
        return []
    kept: list[dict[str, Any]] = []
    remaining = max(int(char_budget), 0)
    for item in reversed(messages[-max(int(max_messages), 0) :]):
        if remaining <= 0:
            break
        text = str(item.get("text") or "")
        bounded = text[: min(max(int(max_message_chars), 0), remaining)]
        kept.append({**item, "text": bounded})
        remaining -= len(bounded)
    kept.reverse()
    return kept


def build_context(
    *,
    group_id: int,
    group_name: str | None,
    messages: list[dict[str, Any]],
    members: list[dict[str, Any]],
    memories: list[dict[str, Any]],
    relations: list[str],
    activity: ActivitySnapshot,
    persona: dict[str, str] | str | None = None,
    active_topic: str | None = None,
    emotion_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Persona belongs to the stable prompt prefix.  Keeping it out of the
    # dynamic JSON prevents a group message from invalidating the cacheable
    # prefix and avoids sending the same policy twice.
    now = now_beijing().replace(second=0, microsecond=0)
    coldness = coldness_score(activity, now)
    stable_members = sorted(
        [
            {key: value for key, value in item.items() if key != "last_seen_at"}
            for item in members[:100]
        ],
        key=lambda item: (int(item.get("user_id") or 0), str(item.get("name") or "")),
    )
    # core（反复确认的稳定事实）不参与 salience 竞争，始终排在最前；
    # 其余按显著度排序，保证同数据下顺序确定。
    stable_memories = sorted(
        memories[:30],
        key=lambda item: (
            0 if str(item.get("type") or "") == "core" else 1,
            -float(item.get("salience") or 0),
            str(item.get("type") or ""),
            str(item.get("key") or ""),
        ),
    )
    # 关系边由调用方渲染成可读文本行（如"小张(1) —情侣→ 小李(2)：官宣过"），
    # 顺序即注入优先级；关系随每次请求变化，属于易变层而非稳定层。
    relation_lines = list(relations[:50])
    clean_emotion = {
        str(key): value
        for key, value in (emotion_state or {}).items()
        if key not in {"updated_at", "last_prefix"}
    }
    return {
        "group_id": group_id,
        "group_name": group_name or "未知群聊",
        "active_topic": active_topic,
        "emotion_state": clean_emotion,
        "activity": {
            # A coarse bucket keeps the value useful without changing every
            # request solely because wall-clock seconds advanced.
            "coldness_bucket": int(round(coldness * 10)),
            "messages_5m": activity.messages_5m,
            "member_messages_5m": activity.member_messages_5m,
            "member_participants_5m": activity.member_participants_5m,
            "messages_20m": activity.messages_20m,
            "participants_60m": activity.participants_60m,
            "replies_60m": activity.replies_60m,
            "mentions_60m": activity.mentions_60m,
        },
        "members": stable_members,
        "messages": trim_context_messages(messages),
        "memories": stable_memories,
        "relations": relation_lines,
    }


__all__ = [
    "ActivitySnapshot",
    "build_context",
    "coldness_score",
    "is_cooldown_active",
    "is_recent",
    "now_beijing",
    "trim_context_messages",
]
