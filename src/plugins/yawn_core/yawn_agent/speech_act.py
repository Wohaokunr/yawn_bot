"""Deterministic dialogue-act planning for one Agent utterance.

The act layer does not decide permissions or whether a proactive turn should
exist. It only describes the conversational job of an already-authorized
utterance so the model does not mix answering, acknowledging and forced
follow-up in the same message.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .speech import (
    SPEECH_SCENE_DIRECT_REPLY,
    SPEECH_SCENE_FOLLOWUP,
    SPEECH_SCENE_REACTION,
    SPEECH_SCENE_REPLY_THREAD,
    SPEECH_SCENE_TOOL_RESULT,
    normalize_speech_scene,
)

if TYPE_CHECKING:
    from .context import CurrentTurn

SPEECH_ACT_ANSWER = "answer"
SPEECH_ACT_ACKNOWLEDGE = "acknowledge"
SPEECH_ACT_REACT = "react"
SPEECH_ACT_TOOL_REPORT = "tool_report"
SPEECH_ACT_CLOSE = "close"
SPEECH_ACT_CONTINUE = "continue"

_ACK_TEXTS = frozenset(
    {
        "嗯",
        "嗯嗯",
        "好",
        "好的",
        "行",
        "可以",
        "收到",
        "懂了",
        "明白了",
        "哈哈",
        "哈哈哈",
    }
)
_CLOSE_HINTS = (
    "不用了",
    "不用继续",
    "先这样",
    "到这吧",
    "就这样",
    "谢谢了",
    "谢了",
    "解决了",
    "搞定了",
)


@dataclass(frozen=True, slots=True)
class SpeechActPlan:
    act: str
    must_answer: bool = False
    allow_followup_question: bool = False
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "act": self.act,
            "must_answer": self.must_answer,
            "allow_followup_question": self.allow_followup_question,
            "reason": self.reason,
        }


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


def plan_speech_act(
    current_turn: CurrentTurn | dict[str, Any] | None,
    *,
    scene: str,
) -> SpeechActPlan:
    normalized_scene = normalize_speech_scene(scene)
    payload = _turn_payload(current_turn)
    content = str(payload.get("content") or "").strip()
    compact = "".join(content.split()).rstrip("。.!！?？~")

    if normalized_scene == SPEECH_SCENE_TOOL_RESULT:
        plan = SpeechActPlan(
            SPEECH_ACT_TOOL_REPORT,
            reason="tool_result_scene",
        )
    elif normalized_scene == SPEECH_SCENE_REACTION:
        plan = SpeechActPlan(SPEECH_ACT_REACT, reason="reaction_scene")
    elif any(hint in compact for hint in _CLOSE_HINTS):
        plan = SpeechActPlan(SPEECH_ACT_CLOSE, reason="explicit_closure")
    elif compact in _ACK_TEXTS:
        plan = SpeechActPlan(SPEECH_ACT_ACKNOWLEDGE, reason="short_ack")
    elif normalized_scene in {SPEECH_SCENE_DIRECT_REPLY, SPEECH_SCENE_REPLY_THREAD}:
        plan = SpeechActPlan(
            SPEECH_ACT_ANSWER,
            must_answer=True,
            allow_followup_question=True,
            reason="explicit_turn",
        )
    elif normalized_scene == SPEECH_SCENE_FOLLOWUP:
        plan = SpeechActPlan(SPEECH_ACT_CONTINUE, reason="bounded_followup")
    else:
        plan = SpeechActPlan(SPEECH_ACT_CONTINUE, reason="normal_conversation")
    return plan


def speech_act_instruction(plan: SpeechActPlan) -> str:
    if plan.act == SPEECH_ACT_ANSWER:
        return (
            "话语动作=answer：先直接解决当前问题；只有确实缺少决定答案的关键信息时才反问，"
            "不能用反问代替回答。"
        )
    if plan.act == SPEECH_ACT_ACKNOWLEDGE:
        return "话语动作=acknowledge：自然确认即可，不要把一句确认扩写成解释或新话题。"
    if plan.act == SPEECH_ACT_REACT:
        return "话语动作=react：一个自然短反应就够，不重复同义文字。"
    if plan.act == SPEECH_ACT_TOOL_REPORT:
        return "话语动作=tool_report：只报告真实结果与必要下一步，不展开后台过程。"
    if plan.act == SPEECH_ACT_CLOSE:
        return (
            "话语动作=close：自然收束，不追加新问题，"
            "不用客套 CTA 把已经结束的话题重新打开。"
        )
    return (
        "话语动作=continue：只承接当前最相关的一点并贡献新信息；"
        "不要为了维持对话机械反问或重复上一轮。"
    )


__all__ = [
    "SPEECH_ACT_ACKNOWLEDGE",
    "SPEECH_ACT_ANSWER",
    "SPEECH_ACT_CLOSE",
    "SPEECH_ACT_CONTINUE",
    "SPEECH_ACT_REACT",
    "SPEECH_ACT_TOOL_REPORT",
    "SpeechActPlan",
    "plan_speech_act",
    "speech_act_instruction",
]
