"""Deterministic scorecard for Agent speech regression scenarios.

The scorecard is intentionally model-free: CI can evaluate representative
outputs without network access or extra inference cost. It reuses the runtime
speech-quality rules, then adds act and group-turn-taking checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .speech import SpeechStyle, speech_plan_from_text
from .speech_act import (
    SPEECH_ACT_ACKNOWLEDGE,
    SPEECH_ACT_ANSWER,
    SPEECH_ACT_CLOSE,
    SPEECH_ACT_PING_ACK,
    SPEECH_ACT_REACT,
    SPEECH_ACT_REPAIR,
)
from .speech_quality import finalize_speech_plan
from .turn_taking import TURN_PRESSURE_HIGH

if TYPE_CHECKING:
    from collections.abc import Iterable

_MAX_SCORE = 100
_PASS_SCORE = 80
_MIN_ANSWER_CHARS = 4
_MAX_ACK_CHARS = 80
_MAX_REACTION_CHARS = 48
_MAX_REPAIR_CHARS = 88
_MAX_PING_ACK_CHARS = 56
_MAX_HIGH_PRESSURE_CHARS = 120

_QUALITY_PENALTIES = {
    "empty": 100,
    "boilerplate_opening": 15,
    "user_echo": 20,
    "generic_followup_cta": 15,
    "answer_strategy_meta": 25,
    "forced_choice_followup": 25,
    "social_monologue": 25,
    "scene_overlong": 10,
    "recent_repeat": 25,
}


@dataclass(frozen=True, slots=True)
class SpeechScore:
    score: int
    deductions: tuple[str, ...]
    quality_codes: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return self.score >= _PASS_SCORE

    def as_dict(self) -> dict[str, object]:
        return {
            "score": self.score,
            "passed": self.passed,
            "deductions": list(self.deductions),
            "quality_codes": list(self.quality_codes),
        }


@dataclass(frozen=True, slots=True)
class SpeechScenario:
    name: str
    text: str
    scene: str = "conversation"
    user_text: str = ""
    recent_texts: tuple[str, ...] = ()
    expected_act: str | None = None
    turn_pressure: str = "low"
    minimum_score: int = _PASS_SCORE


@dataclass(frozen=True, slots=True)
class SpeechSuiteResult:
    total: int
    passed: int
    average_score: float
    failed_names: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.passed == self.total

    def as_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "passed": self.passed,
            "average_score": round(self.average_score, 2),
            "failed_names": list(self.failed_names),
            "ok": self.ok,
        }


def _deduct(
    score: int,
    amount: int,
    reason: str,
    deductions: list[str],
) -> int:
    deductions.append(reason)
    return max(score - amount, 0)


def score_speech_output(  # noqa: C901,PLR0913
    text: str,
    *,
    scene: str = "conversation",
    user_text: str = "",
    recent_texts: Iterable[str] = (),
    expected_act: str | None = None,
    turn_pressure: str = "low",
) -> SpeechScore:
    plan = speech_plan_from_text(
        text,
        scene=scene,
        style=SpeechStyle(soft_target_chars=_MAX_HIGH_PRESSURE_CHARS),
        act=expected_act or "continue",
    )
    checked = finalize_speech_plan(
        plan,
        user_text=user_text,
        recent_texts=recent_texts,
        autofix=False,
    )
    codes = tuple(dict.fromkeys(item.code for item in checked.issues))
    score = _MAX_SCORE
    deductions: list[str] = []
    for code in codes:
        penalty = _QUALITY_PENALTIES.get(code, 5)
        score = _deduct(score, penalty, f"quality:{code}", deductions)

    visible = checked.visible_text.strip()
    if expected_act == SPEECH_ACT_ANSWER and len(visible) < _MIN_ANSWER_CHARS:
        score = _deduct(score, 35, "act:answer_too_thin", deductions)
    elif expected_act == SPEECH_ACT_ACKNOWLEDGE and len(visible) > _MAX_ACK_CHARS:
        score = _deduct(score, 20, "act:ack_overexpanded", deductions)
    elif expected_act == SPEECH_ACT_REACT and len(visible) > _MAX_REACTION_CHARS:
        score = _deduct(score, 25, "act:reaction_overexpanded", deductions)
    elif expected_act == SPEECH_ACT_REPAIR and len(visible) > _MAX_REPAIR_CHARS:
        score = _deduct(score, 30, "act:repair_overexpanded", deductions)
    elif expected_act == SPEECH_ACT_PING_ACK and len(visible) > _MAX_PING_ACK_CHARS:
        score = _deduct(score, 30, "act:ping_ack_overexpanded", deductions)
    elif expected_act == SPEECH_ACT_CLOSE and visible.endswith(("?", "？")):
        score = _deduct(score, 30, "act:close_reopened", deductions)

    if turn_pressure == TURN_PRESSURE_HIGH:
        if len(visible) > _MAX_HIGH_PRESSURE_CHARS:
            score = _deduct(score, 20, "turn:high_pressure_overlong", deductions)
        if visible.endswith(("?", "？")):
            score = _deduct(score, 20, "turn:high_pressure_followup", deductions)

    return SpeechScore(
        score=max(min(score, _MAX_SCORE), 0),
        deductions=tuple(deductions),
        quality_codes=codes,
    )


def run_speech_scorecard(scenarios: Iterable[SpeechScenario]) -> SpeechSuiteResult:
    items = list(scenarios)
    if not items:
        return SpeechSuiteResult(
            total=0,
            passed=0,
            average_score=100.0,
            failed_names=(),
        )
    scores: list[int] = []
    failed: list[str] = []
    for scenario in items:
        result = score_speech_output(
            scenario.text,
            scene=scenario.scene,
            user_text=scenario.user_text,
            recent_texts=scenario.recent_texts,
            expected_act=scenario.expected_act,
            turn_pressure=scenario.turn_pressure,
        )
        scores.append(result.score)
        if result.score < scenario.minimum_score:
            failed.append(scenario.name)
    return SpeechSuiteResult(
        total=len(items),
        passed=len(items) - len(failed),
        average_score=sum(scores) / len(scores),
        failed_names=tuple(failed),
    )


__all__ = [
    "SpeechScenario",
    "SpeechScore",
    "SpeechSuiteResult",
    "run_speech_scorecard",
    "score_speech_output",
]
