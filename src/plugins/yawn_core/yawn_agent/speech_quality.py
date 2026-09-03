# ruff: noqa: E501
"""Deterministic speech-quality checks for user-visible Agent output.

The linter is deliberately conservative. It may remove obvious assistant-like
boilerplate from model prose, but structured tool messages default to lint-only
unless a caller explicitly enables autofix.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

from .speech import (
    SPEECH_SCENE_ACTIVE_INTERJECT,
    SPEECH_SCENE_FOLLOWUP,
    SPEECH_SCENE_REACTION,
    SPEECH_SCENE_WARMUP,
    SpeechPlan,
    SpeechQualityIssue,
)
from .speech_act import (
    SPEECH_ACT_ACKNOWLEDGE,
    SPEECH_ACT_CLOSE,
    SPEECH_ACT_PING_ACK,
    SPEECH_ACT_REACT,
    SPEECH_ACT_REPAIR,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

_BOILERPLATE_PREFIXES = (
    re.compile(r"^(?:好的|当然可以|没问题|可以的|明白了|收到)[！!，,：:\s]+"),
    re.compile(r"^(?:我来帮你(?:分析|看看|解释|整理)(?:一下)?)[！!，,：:\s]*"),
)
_GENERIC_CTA_SUFFIXES = (
    re.compile(
        r"(?:如果你(?:还有|还遇到|还有其他)[^。！？!?]{0,36}(?:可以|欢迎)[^。！？!?]{0,36})[。！？!?]*$"
    ),
    re.compile(r"(?:还需要我(?:帮你)?[^。！？!?]{0,36}吗)[？?。！!]*$"),
    re.compile(r"(?:有(?:其他|别的)问题(?:也)?可以继续问我)[。！？!?]*$"),
)
_ANSWER_STRATEGY_PREFIXES = (
    re.compile(
        r"^(?:我|那我|我这次|这里我)(?:先|就|接下来|这次|这里)?(?:来|会|准备|打算)?"
        r"(?:看(?:一眼|一下)?|分析(?:一下)?|解释(?:一下)?|说(?:一下)?|讲(?:一下)?|回答(?:一下)?)"
        r"[^。！？!?]{0,42}[，,：:]\s*(?:顺便说(?:一|两)?句[，,]\s*)?",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:那我|我这次|我就|接下来我)[^。！？!?]{0,28}"
        r"(?:不整|不说|少说|不复述|不反问|直接说|简单说|简短说|换个说法)"
        r"[^。！？!?]{0,42}[。！？!?，,]\s*",
        re.IGNORECASE,
    ),
)
_ANSWER_STRATEGY_META_RE = re.compile(
    r"(?:我|那我|我这次|接下来我|这里我)[^。！？!?]{0,36}"
    r"(?:怎么回答|回答方式|表达方式|描述方式|说话方式|"
    r"不整|不说那些|少说|不复述|不反问|直接说|简单说|简短说|换个说法)",
    re.IGNORECASE,
)
_FORCED_CHOICE_SUFFIX_RE = re.compile(
    r"(?:你(?:就)?说|你想|你是想|你是要|要我|是要我)[^。！？!?]{0,84}"
    r"(?:还是|或者)[^。！？!?]{0,84}[？?。！!]*$",
    re.IGNORECASE,
)
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?])\s*")
_SHORT_SCENES = frozenset(
    {
        SPEECH_SCENE_ACTIVE_INTERJECT,
        SPEECH_SCENE_WARMUP,
        SPEECH_SCENE_FOLLOWUP,
        SPEECH_SCENE_REACTION,
    }
)
_SHORT_ACTS = frozenset(
    {
        SPEECH_ACT_PING_ACK,
        SPEECH_ACT_REPAIR,
        SPEECH_ACT_ACKNOWLEDGE,
        SPEECH_ACT_REACT,
        SPEECH_ACT_CLOSE,
    }
)
_MIN_ECHO_TEXT_CHARS = 12
_MIN_ECHO_SENTENCES = 2
_MIN_SIMILARITY_CHARS = 8
_ECHO_SIMILARITY_THRESHOLD = 0.72
_REPEAT_SIMILARITY_THRESHOLD = 0.82
_SOCIAL_MONOLOGUE_MULTIPLIER = 1.6
_SOCIAL_MONOLOGUE_MIN_CHARS = 56
_SOCIAL_MONOLOGUE_MAX_CHARS = 140


def _clean_lines(text: object) -> str:
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return ""
    lines = [re.sub(r"[ \t]+", " ", line).rstrip() for line in raw.split("\n")]
    compact: list[str] = []
    blank = False
    for line in lines:
        if not line:
            if compact and not blank:
                compact.append("")
            blank = True
            continue
        compact.append(line)
        blank = False
    return "\n".join(compact).strip()


def _similarity_key(text: object) -> str:
    normalized = str(text or "").casefold()
    return re.sub(r"[\s\W_]+", "", normalized, flags=re.UNICODE)


def speech_similarity(left: object, right: object) -> float:
    first = _similarity_key(left)
    second = _similarity_key(right)
    if not first or not second:
        return 0.0
    if first == second:
        return 1.0
    return SequenceMatcher(a=first, b=second, autojunk=False).ratio()


def _strip_boilerplate(text: str) -> tuple[str, bool]:
    cleaned = text
    changed = False
    for pattern in _BOILERPLATE_PREFIXES:
        updated = pattern.sub("", cleaned, count=1).lstrip()
        if updated != cleaned and updated:
            cleaned = updated
            changed = True
            break
    return cleaned, changed


def _strip_generic_cta(text: str) -> tuple[str, bool]:
    cleaned = text.rstrip()
    for pattern in _GENERIC_CTA_SUFFIXES:
        match = pattern.search(cleaned)
        if match is None or match.start() <= 0:
            continue
        prefix = cleaned[: match.start()].rstrip(" \n，,；;：:")
        if prefix:
            return prefix, True
    return cleaned, False


def _strip_answer_strategy_prefix(text: str) -> tuple[str, bool]:
    cleaned = text.lstrip()
    for pattern in _ANSWER_STRATEGY_PREFIXES:
        updated = pattern.sub("", cleaned, count=1).lstrip()
        if updated != cleaned and updated:
            return updated, True
    return text, False


def _strip_forced_choice_followup(text: str, plan: SpeechPlan) -> tuple[str, bool]:
    if plan.act not in _SHORT_ACTS:
        return text, False
    cleaned = text.rstrip()
    match = _FORCED_CHOICE_SUFFIX_RE.search(cleaned)
    if match is None or match.start() <= 0:
        return text, False
    prefix = cleaned[: match.start()].rstrip(" \n，,；;：:")
    return (prefix, True) if prefix else (text, False)


def _strip_user_echo(text: str, user_text: str) -> tuple[str, bool]:
    if not user_text.strip() or len(text) < _MIN_ECHO_TEXT_CHARS:
        return text, False
    sentences = [item for item in _SENTENCE_SPLIT.split(text) if item]
    if len(sentences) < _MIN_ECHO_SENTENCES:
        return text, False
    first = sentences[0].strip()
    explicit_echo = first.startswith(("你问", "你的问题是", "你刚才说", "你提到"))
    similar = (
        len(_similarity_key(first)) >= _MIN_SIMILARITY_CHARS
        and speech_similarity(first, user_text) >= _ECHO_SIMILARITY_THRESHOLD
    )
    if not explicit_echo and not similar:
        return text, False
    remainder = text[len(sentences[0]) :].lstrip()
    return (remainder, True) if remainder else (text, False)


def _trim_to_boundary(text: str, hard_limit: int, *, minimum_boundary: int) -> str:
    window = text[:hard_limit]
    boundary = max(
        window.rfind(mark) for mark in ("。", "！", "？", "!", "?", "\n")
    )
    if boundary >= minimum_boundary:
        return window[: boundary + 1].rstrip()
    return window.rstrip() + ("…" if len(text) > len(window) else "")


def _trim_social_monologue(text: str, plan: SpeechPlan) -> tuple[str, bool]:
    target = plan.style.soft_target_chars
    if plan.act not in _SHORT_ACTS or not target:
        return text, False
    hard_limit = min(
        max(int(target * _SOCIAL_MONOLOGUE_MULTIPLIER), _SOCIAL_MONOLOGUE_MIN_CHARS),
        _SOCIAL_MONOLOGUE_MAX_CHARS,
    )
    if len(text) <= hard_limit:
        return text, False
    return (
        _trim_to_boundary(text, hard_limit, minimum_boundary=max(16, target // 2)),
        True,
    )


def _trim_short_scene(text: str, plan: SpeechPlan) -> tuple[str, bool]:
    target = plan.style.soft_target_chars
    short_turn = plan.scene in _SHORT_SCENES or plan.act in _SHORT_ACTS
    if not short_turn or not target or len(text) <= target * 2:
        return text, False
    hard_limit = min(max(target * 2, 48), 360)
    return (
        _trim_to_boundary(text, hard_limit, minimum_boundary=max(20, target // 2)),
        True,
    )


def _recent_repeat(text: str, recent_texts: Iterable[str]) -> float:
    best = 0.0
    if len(_similarity_key(text)) < _MIN_SIMILARITY_CHARS:
        return best
    for recent in recent_texts:
        if len(_similarity_key(recent)) < _MIN_SIMILARITY_CHARS:
            continue
        best = max(best, speech_similarity(text, recent))
    return best


def _issue(
    code: str,
    detail: str,
    *,
    autofixed: bool = False,
    severity: str = "info",
) -> SpeechQualityIssue:
    return SpeechQualityIssue(
        code=code,
        detail=detail,
        autofixed=autofixed,
        severity=severity,
    )


def _dedupe_issues(items: list[SpeechQualityIssue]) -> tuple[SpeechQualityIssue, ...]:
    seen: set[tuple[str, bool]] = set()
    result: list[SpeechQualityIssue] = []
    for item in items:
        key = (item.code, item.autofixed)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return tuple(result)


def _polish_text(  # noqa: C901,PLR0912
    text: str,
    plan: SpeechPlan,
    *,
    user_text: str,
    recent_texts: Iterable[str],
    autofix: bool,
) -> tuple[str, tuple[SpeechQualityIssue, ...]]:
    original = _clean_lines(text)
    if not original:
        return "", (_issue("empty", "发言文本为空", severity="warning"),)

    cleaned = original
    issues: list[SpeechQualityIssue] = []

    candidate, boilerplate = _strip_boilerplate(cleaned)
    if boilerplate:
        issues.append(
            _issue(
                "boilerplate_opening",
                "检测到群聊中无信息量的助手式开场",
                autofixed=autofix,
            )
        )
        if autofix:
            cleaned = candidate

    strategy_detected = bool(_ANSWER_STRATEGY_META_RE.search(cleaned))
    candidate, strategy_prefix = _strip_answer_strategy_prefix(cleaned)
    if strategy_prefix or strategy_detected:
        issues.append(
            _issue(
                "answer_strategy_meta",
                "检测到解释“自己准备怎么回答”的元话语；群聊里应直接做出调整，而不是讲回答策略",
                autofixed=autofix and strategy_prefix,
                severity="warning",
            )
        )
        if autofix and strategy_prefix:
            cleaned = candidate

    candidate, echoed = _strip_user_echo(cleaned, user_text)
    if echoed:
        issues.append(
            _issue(
                "user_echo",
                "检测到先复述当前用户问题再回答",
                autofixed=autofix,
            )
        )
        if autofix:
            cleaned = candidate

    candidate, cta = _strip_generic_cta(cleaned)
    if cta:
        issues.append(
            _issue(
                "generic_followup_cta",
                "检测到通用“还有问题继续问”式强行续聊结尾",
                autofixed=autofix,
            )
        )
        if autofix:
            cleaned = candidate

    forced_choice_detected = bool(
        plan.act in _SHORT_ACTS and _FORCED_CHOICE_SUFFIX_RE.search(cleaned)
    )
    candidate, forced_choice = _strip_forced_choice_followup(cleaned, plan)
    if forced_choice_detected:
        issues.append(
            _issue(
                "forced_choice_followup",
                "短社交回合末尾强行追加“是要……还是……”式二选一反问",
                autofixed=autofix and forced_choice,
                severity="warning",
            )
        )
        if autofix and forced_choice:
            cleaned = candidate

    candidate, social_trimmed = _trim_social_monologue(cleaned, plan)
    if social_trimmed:
        issues.append(
            _issue(
                "social_monologue",
                "repair/ping/ack 等短社交动作被展开成小作文",
                autofixed=autofix,
                severity="warning",
            )
        )
        if autofix:
            cleaned = candidate

    candidate, trimmed = _trim_short_scene(cleaned, plan)
    if trimmed:
        issues.append(
            _issue(
                "scene_overlong",
                "短发言场景或短话语动作明显超过本轮软长度目标",
                autofixed=autofix,
            )
        )
        if autofix:
            cleaned = candidate

    repeat_score = _recent_repeat(cleaned, recent_texts)
    if repeat_score >= _REPEAT_SIMILARITY_THRESHOLD:
        issues.append(
            _issue(
                "recent_repeat",
                f"与近期 Bot 发言高度相似（{repeat_score:.2f}）",
                severity="warning",
            )
        )

    return _clean_lines(cleaned), _dedupe_issues(issues)


def finalize_speech_plan(
    plan: SpeechPlan,
    *,
    user_text: str = "",
    recent_texts: Iterable[str] = (),
    autofix: bool = True,
) -> SpeechPlan:
    """Lint one plan and optionally apply only conservative wording fixes."""

    if plan.text:
        text, issues = _polish_text(
            plan.text,
            plan,
            user_text=user_text,
            recent_texts=recent_texts,
            autofix=autofix,
        )
        return plan.with_content(text=text, issues=issues)

    if not plan.segments:
        return plan.with_content(
            issues=(_issue("empty", "发言计划没有文本或消息段", severity="warning"),)
        )

    segments = [dict(item) for item in plan.segments]
    text_indices = [
        index
        for index, item in enumerate(segments)
        if str(item.get("type") or "").strip().lower() == "text"
    ]
    if not text_indices:
        return plan

    combined = "".join(str(segments[index].get("text") or "") for index in text_indices)
    _, issues = _polish_text(
        combined,
        plan,
        user_text=user_text,
        recent_texts=recent_texts,
        autofix=False,
    )
    if not autofix:
        return plan.with_content(segments=tuple(segments), issues=issues)

    first_index = text_indices[0]
    last_index = text_indices[-1]
    first_text = _clean_lines(segments[first_index].get("text"))
    first_text, _ = _strip_boilerplate(first_text)
    first_text, _ = _strip_answer_strategy_prefix(first_text)
    first_text, _ = _strip_user_echo(first_text, user_text)
    if first_text:
        segments[first_index]["text"] = first_text

    last_text = _clean_lines(segments[last_index].get("text"))
    last_text, _ = _strip_generic_cta(last_text)
    last_text, _ = _strip_forced_choice_followup(last_text, plan)
    if last_text:
        segments[last_index]["text"] = last_text

    if len(text_indices) == 1:
        only = str(segments[first_index].get("text") or "")
        social_trimmed, _ = _trim_social_monologue(only, plan)
        trimmed, _ = _trim_short_scene(social_trimmed, plan)
        if trimmed:
            segments[first_index]["text"] = trimmed

    fixed_combined = "".join(
        str(segments[index].get("text") or "") for index in text_indices
    )
    _, fixed_issues = _polish_text(
        fixed_combined,
        plan,
        user_text=user_text,
        recent_texts=recent_texts,
        autofix=False,
    )
    changed_codes = {item.code for item in issues} - {item.code for item in fixed_issues}
    merged = [
        SpeechQualityIssue(
            code=item.code,
            detail=item.detail,
            severity=item.severity,
            autofixed=item.code in changed_codes,
        )
        for item in issues
    ]
    merged.extend(
        item for item in fixed_issues if item.code not in {old.code for old in merged}
    )
    return plan.with_content(segments=tuple(segments), issues=_dedupe_issues(merged))


__all__ = [
    "finalize_speech_plan",
    "speech_similarity",
]
