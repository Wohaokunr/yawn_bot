"""Deterministic QQ-native expression planning for one Agent turn.

The planner never sends anything and never invents identifiers. It only turns
facts already present in ``current_turn`` into bounded guidance for choosing
plain text, reply, @ and reaction message shapes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .speech import (
    SPEECH_SCENE_DIRECT_REPLY,
    SPEECH_SCENE_REACTION,
    SPEECH_SCENE_REPLY_THREAD,
    SpeechStyle,
)

if TYPE_CHECKING:
    from .context import CurrentTurn


@dataclass(frozen=True, slots=True)
class NativeExpressionPlan:
    preferred_modes: tuple[str, ...]
    reply_message_id: int | None = None
    mention_candidates: tuple[int, ...] = ()
    allow_reaction: bool = False
    avoid_redundant_at: bool = True

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "preferred_modes": list(self.preferred_modes),
            "allow_reaction": self.allow_reaction,
            "avoid_redundant_at": self.avoid_redundant_at,
        }
        if self.reply_message_id is not None:
            payload["reply_message_id"] = self.reply_message_id
        if self.mention_candidates:
            payload["mention_candidates"] = list(self.mention_candidates)
        return payload


def _turn_payload(current_turn: CurrentTurn | dict[str, Any] | None) -> dict[str, Any]:
    if current_turn is None:
        return {}
    if isinstance(current_turn, dict):
        return dict(current_turn)
    prompt_dict = getattr(current_turn, "prompt_dict", None)
    if callable(prompt_dict):
        value = prompt_dict()
        return dict(value) if isinstance(value, dict) else {}
    return {}


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def plan_native_expression(
    current_turn: CurrentTurn | dict[str, Any] | None,
    *,
    scene: str,
    style: SpeechStyle,
) -> NativeExpressionPlan:
    payload = _turn_payload(current_turn)
    reply = payload.get("reply_to")
    reply_id = (
        _positive_int(reply.get("message_id")) if isinstance(reply, dict) else None
    )
    actor_id = _positive_int(payload.get("user_id"))
    mentions: list[int] = []
    raw_mentions = payload.get("mentions")
    if isinstance(raw_mentions, (list, tuple)):
        for raw in raw_mentions:
            user_id = _positive_int(raw)
            if user_id is None or user_id == actor_id or user_id in mentions:
                continue
            mentions.append(user_id)

    allow_reaction = bool(
        style.allow_spontaneous_reaction
        and scene not in {SPEECH_SCENE_DIRECT_REPLY}
    )
    modes: list[str] = []
    if reply_id is not None or scene == SPEECH_SCENE_REPLY_THREAD:
        modes.append("reply")
    if mentions:
        modes.append("at_if_targeted")
    if allow_reaction and scene in {SPEECH_SCENE_REPLY_THREAD, SPEECH_SCENE_REACTION}:
        modes.append("reaction_if_sufficient")
    modes.append("text")
    return NativeExpressionPlan(
        preferred_modes=tuple(dict.fromkeys(modes)),
        reply_message_id=reply_id,
        mention_candidates=tuple(mentions[:4]),
        allow_reaction=allow_reaction,
    )


def native_expression_instruction(plan: NativeExpressionPlan) -> str:
    parts = [
        "QQ 原生表达按本轮已知事实选择，不要为了显得活泼机械加段。",
        "只有当前 schema 暴露 send_message/对应 segment 时才使用复合消息，否则直接文本回答。",
    ]
    if plan.reply_message_id is not None:
        parts.append(
            f"当前存在被引用消息 {plan.reply_message_id}；回答确实承接它时优先 reply，"
            "不要把 reply 当装饰。"
        )
    if plan.mention_candidates:
        parts.append(
            "当前明确出现可 @ 候选："
            + ",".join(str(item) for item in plan.mention_candidates)
            + "；只有回复真正指向该成员时才 @，不要机械 @ 当前提问者。"
        )
    if plan.allow_reaction:
        parts.append(
            "轻量接梗/确认时 reaction 可以替代同义废话；有事实、步骤或风险信息时不能只发表情。"
        )
    else:
        parts.append("本轮以信息完整为先，不主动用 reaction 替代正文。")
    return "".join(parts)


__all__ = [
    "NativeExpressionPlan",
    "native_expression_instruction",
    "plan_native_expression",
]
