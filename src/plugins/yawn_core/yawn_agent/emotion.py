"""Lightweight dynamic emotion state for the group-chat Agent.

The state describes the Agent's temporary stance, not a claim about a member's
mental state. It is updated deterministically from coarse interaction cues so
it adds no LLM cost, decays lazily toward neutral, and never changes safety or
permission policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

EMOTION_SCHEMA_VERSION = 1
EMOTION_HALF_LIFE_MINUTES = 35.0
EMOTION_PRUNE_AFTER_MINUTES = 180.0
_MAX_EVENT_COUNT = 9999
_NEUTRAL_LABEL_THRESHOLD = 0.08
_PRUNE_INTENSITY_THRESHOLD = 0.025
_VISIBLE_EXPRESSION_THRESHOLD = 0.12

EmotionLabel = Literal[
    "neutral",
    "warm",
    "amused",
    "curious",
    "concerned",
    "guarded",
    "irritated",
]

_LABELS: dict[str, str] = {
    "neutral": "平静",
    "warm": "亲和",
    "amused": "愉快",
    "curious": "好奇",
    "concerned": "关切",
    "guarded": "谨慎",
    "irritated": "轻微不耐",
}

_EXPRESSION_HINTS: dict[str, str] = {
    "neutral": "保持 Persona 的基础气质即可。",
    "warm": "可以稍微更亲近、积极，但不要突然过度热情。",
    "amused": "可以带一点轻松或笑意；是否接梗仍受 Persona reaction 倾向约束。",
    "curious": "可以表现出适度好奇，但不要为了续聊而连续追问。",
    "concerned": "表达可以更耐心、关切，避免轻佻或拿对方困扰开玩笑。",
    "guarded": "表达更谨慎、少下断言；仍保持礼貌和事实约束。",
    "irritated": "可以略显克制的不耐，但不得攻击、羞辱、报复或升级冲突。",
}

_HOSTILE = (
    "滚",
    "闭嘴",
    "傻逼",
    "煞笔",
    "蠢货",
    "废物",
    "垃圾",
    "有病",
    "妈的",
    "草泥马",
    "fuck",
    "stfu",
)
_DISTRESS = (
    "难过",
    "伤心",
    "不开心",
    "崩溃",
    "想哭",
    "害怕",
    "焦虑",
    "痛苦",
    "好累",
    "累死",
)
_LAUGHTER = (
    "哈哈",
    "笑死",
    "绷不住",
    "乐死",
    "太好笑",
    "hh",
    "lol",
    "233",
)
_POSITIVE = (
    "谢谢",
    "感谢",
    "好耶",
    "厉害",
    "牛逼",
    "牛啊",
    "真棒",
    "不错",
    "可爱",
    "喜欢",
    "辛苦了",
    "666",
)
_CHALLENGE = (
    "瞎说",
    "胡说",
    "骗人",
    "你错了",
    "不对吧",
    "真的假的",
    "确定吗",
    "靠谱吗",
)
_CURIOSITY = ("为什么", "怎么回事", "咋回事", "真的吗", "啥意思", "什么情况")


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(float(value), high))


def _parse_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class EmotionSnapshot:
    label: EmotionLabel
    valence: float
    arousal: float
    intensity: float
    updated_at: datetime | None
    source: str
    reason: str
    event_count: int
    age_minutes: float

    @property
    def display_label(self) -> str:
        return _LABELS[self.label]

    def storage_payload(self, *, updated_at: datetime | None = None) -> dict[str, Any]:
        timestamp = updated_at or self.updated_at or _utc_now()
        return {
            "schema_version": EMOTION_SCHEMA_VERSION,
            "label": self.label,
            "valence": round(_clamp(self.valence), 4),
            "arousal": round(_clamp(self.arousal, 0.0, 1.0), 4),
            "intensity": round(_clamp(self.intensity, 0.0, 1.0), 4),
            "updated_at": timestamp.isoformat(),
            "source": self.source[:32],
            "reason": self.reason[:80],
            "event_count": min(max(int(self.event_count), 0), _MAX_EVENT_COUNT),
        }


@dataclass(frozen=True, slots=True)
class EmotionSignal:
    label: EmotionLabel
    valence: float
    arousal: float
    intensity: float
    reason: str


@dataclass(frozen=True, slots=True)
class EmotionMutation:
    state: dict[str, Any]
    storage_changed: bool
    signal: EmotionSignal | None


def neutral_emotion() -> EmotionSnapshot:
    return EmotionSnapshot(
        label="neutral",
        valence=0.0,
        arousal=0.15,
        intensity=0.0,
        updated_at=None,
        source="none",
        reason="",
        event_count=0,
        age_minutes=0.0,
    )


def resolve_emotion_state(
    raw: object, *, now: datetime | None = None
) -> EmotionSnapshot:
    """Resolve persisted state after lazy exponential decay toward neutral."""

    if not isinstance(raw, dict) or not raw:
        return neutral_emotion()
    current = now or _utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    updated_at = _parse_datetime(raw.get("updated_at"))
    age_minutes = (
        max((current - updated_at).total_seconds() / 60.0, 0.0)
        if updated_at is not None
        else EMOTION_PRUNE_AFTER_MINUTES
    )
    decay = 0.5 ** (age_minutes / EMOTION_HALF_LIFE_MINUTES)
    try:
        raw_valence = float(raw.get("valence") or 0.0)
        raw_arousal = float(raw.get("arousal") or 0.15)
        raw_intensity = float(raw.get("intensity") or 0.0)
        event_count = int(raw.get("event_count") or 0)
    except (TypeError, ValueError):
        return neutral_emotion()
    valence = _clamp(raw_valence * decay)
    arousal = _clamp(0.15 + (raw_arousal - 0.15) * decay, 0.0, 1.0)
    intensity = _clamp(raw_intensity * decay, 0.0, 1.0)
    raw_label = str(raw.get("label") or "neutral").strip().lower()
    label: EmotionLabel = (
        raw_label
        if raw_label in _LABELS and intensity >= _NEUTRAL_LABEL_THRESHOLD
        else "neutral"
    )  # type: ignore[assignment]
    return EmotionSnapshot(
        label=label,
        valence=valence,
        arousal=arousal,
        intensity=intensity,
        updated_at=updated_at,
        source=str(raw.get("source") or "none")[:32],
        reason=str(raw.get("reason") or "")[:80] if label != "neutral" else "",
        event_count=min(max(event_count, 0), _MAX_EVENT_COUNT),
        age_minutes=age_minutes,
    )


def detect_emotion_signal(  # noqa: PLR0911
    text: str, *, directed: bool
) -> EmotionSignal | None:
    """Detect coarse cues without inferring a user's private mental state."""

    cleaned = " ".join(str(text or "").lower().split())[:800]
    if not cleaned:
        return None

    if any(token in cleaned for token in _HOSTILE):
        if directed:
            return EmotionSignal("irritated", -0.65, 0.72, 0.72, "收到直接的敌意表达")
        return EmotionSignal("guarded", -0.22, 0.42, 0.28, "群聊出现较强冲突语气")
    if any(token in cleaned for token in _DISTRESS):
        return EmotionSignal("concerned", -0.20, 0.36, 0.50, "对话出现需要关切的表达")
    if any(token in cleaned for token in _LAUGHTER):
        return EmotionSignal("amused", 0.66, 0.68, 0.62, "对话出现明显轻松或玩梗氛围")
    if any(token in cleaned for token in _POSITIVE):
        return EmotionSignal("warm", 0.55, 0.35, 0.52, "收到友好或积极反馈")
    if any(token in cleaned for token in _CHALLENGE):
        return EmotionSignal("guarded", -0.18, 0.45, 0.38, "对话出现质疑或纠错信号")
    if directed and any(token in cleaned for token in _CURIOSITY):
        return EmotionSignal("curious", 0.08, 0.42, 0.30, "收到直接的探索性提问")
    return None


def update_emotion_state(
    raw: object,
    *,
    text: str,
    directed: bool,
    now: datetime | None = None,
) -> EmotionMutation:
    """Apply one group-message event and return a sparse persisted mutation."""

    current_time = now or _utc_now()
    current = resolve_emotion_state(raw, now=current_time)
    signal = detect_emotion_signal(text, directed=directed)
    if signal is None:
        should_prune = bool(raw) and (
            current.age_minutes >= EMOTION_PRUNE_AFTER_MINUTES
            or current.intensity < _PRUNE_INTENSITY_THRESHOLD
        )
        return (
            EmotionMutation(state={}, storage_changed=True, signal=None)
            if should_prune
            else EmotionMutation(
                state=dict(raw) if isinstance(raw, dict) else {},
                storage_changed=False,
                signal=None,
            )
        )

    exposure = 1.0 if directed else 0.38
    alpha = 0.38 + 0.34 * exposure
    valence = _clamp(current.valence * (1.0 - alpha) + signal.valence * alpha)
    arousal = _clamp(
        current.arousal * (1.0 - alpha) + signal.arousal * alpha, 0.0, 1.0
    )
    intensity = _clamp(
        current.intensity * 0.52 + signal.intensity * (0.48 * exposure + 0.22),
        0.0,
        1.0,
    )
    snapshot = EmotionSnapshot(
        label=signal.label,
        valence=valence,
        arousal=arousal,
        intensity=intensity,
        updated_at=current_time,
        source="direct" if directed else "ambient",
        reason=signal.reason,
        event_count=min(current.event_count + 1, _MAX_EVENT_COUNT),
        age_minutes=0.0,
    )
    return EmotionMutation(
        state=snapshot.storage_payload(updated_at=current_time),
        storage_changed=True,
        signal=signal,
    )


def emotion_public_state(
    raw: object,
    *,
    now: datetime | None = None,
    expressiveness: int = 2,
) -> dict[str, Any]:
    """Return a privacy-safe, human-readable view for Prompt/WebUI/QQ."""

    snapshot = resolve_emotion_state(raw, now=now)
    level = min(max(int(expressiveness), 0), 4)
    expression_scale = (0.20, 0.40, 0.62, 0.82, 1.0)[level]
    expression_intensity = snapshot.intensity * expression_scale
    age_bucket = int(snapshot.age_minutes // 5 * 5) if snapshot.updated_at else 0
    hint = _EXPRESSION_HINTS[snapshot.label]
    if (
        snapshot.label != "neutral"
        and expression_intensity < _VISIBLE_EXPRESSION_THRESHOLD
    ):
        hint = "情绪已较弱，只轻微影响措辞，整体仍以 Persona 基础气质为主。"
    return {
        "schemaVersion": EMOTION_SCHEMA_VERSION,
        "label": snapshot.label,
        "displayLabel": snapshot.display_label,
        "valence": round(snapshot.valence, 2),
        "arousal": round(snapshot.arousal, 2),
        "intensity": round(snapshot.intensity, 2),
        "expressionIntensity": round(expression_intensity, 2),
        "expressionHint": hint,
        "source": snapshot.source,
        "reason": snapshot.reason,
        "updatedAt": snapshot.updated_at.isoformat() if snapshot.updated_at else None,
        "ageMinutesBucket": age_bucket,
        "eventCount": snapshot.event_count,
    }


def emotion_context_state(
    raw: object,
    *,
    now: datetime | None = None,
    expressiveness: int = 2,
) -> dict[str, Any]:
    """Compact dynamic state injected into the volatile context layer."""

    public = emotion_public_state(raw, now=now, expressiveness=expressiveness)
    if public["updatedAt"] is None or (
        public["label"] == "neutral"
        and float(public["expressionIntensity"]) < _NEUTRAL_LABEL_THRESHOLD
    ):
        return {}
    return {
        "label": public["label"],
        "intensity": public["intensity"],
        "expression_intensity": public["expressionIntensity"],
        "expression_hint": public["expressionHint"],
        "source": public["source"],
        "reason": public["reason"],
        "age_minutes_bucket": public["ageMinutesBucket"],
    }


__all__ = [
    "EMOTION_HALF_LIFE_MINUTES",
    "EMOTION_SCHEMA_VERSION",
    "EmotionMutation",
    "EmotionSignal",
    "EmotionSnapshot",
    "detect_emotion_signal",
    "emotion_context_state",
    "emotion_public_state",
    "neutral_emotion",
    "resolve_emotion_state",
    "update_emotion_state",
]
