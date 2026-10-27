# ruff: noqa: E501,RUF001,PLR2004,C901,PLR0911,PLR0912,PLR0913,PLR0917
"""发言节奏：把一次想说的话规划成 1~N 条 QQ 消息，以及每条之前的停顿。

真人在群里很少把想说的话憋成一大段再发：平常聊天会顺手拆成两三条，激动时
短句连发，搞怪时一个字一个字往外蹦；只有认真讨论、讲步骤时才一条发完整段。
节奏本身就在表达心情，所以由模型自己选：

- 正文里用 ``|||`` 把要分开发的部分隔开，每部分就是一条消息；
- 每次都在开头写 ``【节奏:整段/分条/连发/蹦字】`` 选定节奏，标签发送前会剥掉。

本模块是纯函数、零 AI 成本：只解析模型给的标记，按节奏算出每条之前的“打字”
停顿（情绪和人设只调快慢）。模型没选节奏时只按它自己写的空行/换行兜底，不再替它
把一整段按句号切开；模型选了“分条/连发”却没写 ``|||`` 时才按句子/短句拆。
不按关键词猜意图，也不改写语义。真正的发送在 ``speech_delivery.py``。
"""

from __future__ import annotations

import hashlib
import math
import os
import random
import re
from dataclasses import dataclass
from typing import Any

RHYTHM_AUTO = "auto"
RHYTHM_BLOCK = "block"
RHYTHM_SPLIT = "split"
RHYTHM_BURST = "burst"
RHYTHM_POP = "pop"
RHYTHMS = frozenset({RHYTHM_AUTO, RHYTHM_BLOCK, RHYTHM_SPLIT, RHYTHM_BURST, RHYTHM_POP})
RHYTHM_LABELS: dict[str, str] = {
    RHYTHM_AUTO: "自动",
    RHYTHM_BLOCK: "整段",
    RHYTHM_SPLIT: "分条",
    RHYTHM_BURST: "连发",
    RHYTHM_POP: "蹦字",
}
_RHYTHM_ALIASES: dict[str, str] = {
    "auto": RHYTHM_AUTO,
    "自动": RHYTHM_AUTO,
    "block": RHYTHM_BLOCK,
    "整段": RHYTHM_BLOCK,
    "一段": RHYTHM_BLOCK,
    "一条": RHYTHM_BLOCK,
    "split": RHYTHM_SPLIT,
    "分条": RHYTHM_SPLIT,
    "分开": RHYTHM_SPLIT,
    "burst": RHYTHM_BURST,
    "连发": RHYTHM_BURST,
    "刷屏": RHYTHM_BURST,
    "pop": RHYTHM_POP,
    "蹦字": RHYTHM_POP,
    "逐字": RHYTHM_POP,
}

SPLIT_MODE_AUTO = "auto"
SPLIT_MODE_MARKED = "marked"
SPLIT_MODE_OFF = "off"
SPLIT_MODES = frozenset({SPLIT_MODE_AUTO, SPLIT_MODE_MARKED, SPLIT_MODE_OFF})

PART_MARKER = "|||"
DEFAULT_MAX_PARTS = 3
MAX_SPLIT_PARTS = 4
BURST_MAX_PARTS = 5
POP_MAX_PARTS = 8
POP_MAX_CHARS = 12
# 三条以上、每条不超过这么多字，就按“连发”的快节奏发。
BURST_PART_MAX_CHARS = 5
# 声明连发但有一条超过这么多字，就按“分条”的正常速度发。
BURST_LONG_PART_CHARS = 16
DEFAULT_MAX_SPREAD_SECONDS = 10.0
MAX_SPREAD_SECONDS_LIMIT = 30.0
AUTO_SENTENCE_MIN_CHARS = 40
AUTO_PART_MIN_CHARS = 8
AUTO_PARAGRAPH_MAX_CHARS = 160
AUTO_LINE_MAX_CHARS = 80

# 标记：||| / 全角 ｜｜｜ / 【分条】 等，前后的空白一并吃掉。
_MARKER_RE = re.compile(r"[ \t]*(?:\|{3,}|｜{3,}|【分条】|\[分条\]|<分条>)[ \t]*\n?")
_TAG_RE = re.compile(
    r"[【\[<〔（(]\s*(?:节奏|rhythm)\s*[:：=]\s*([A-Za-z一-鿿]{1,6})\s*[】\]>〕）)]\s*",
    re.IGNORECASE,
)
_CODE_FENCE = "```"
_LIST_LINE_RE = re.compile(r"^[ \t]*(?:[·•\-*+]|\d{1,2}[.、)）]|[（(]\d{1,2}[)）]|[一二三四五六七八九十][、.])[ \t]*\S")
_SENTENCE_END_RE = re.compile(r"(?<=[。！？!?…~～])(?![。！？!?…~～”’」』）)])")
_CLAUSE_END_RE = re.compile(r"(?<=[，,、；;。！？!?…~～])(?![，,、；;。！？!?…~～])|\s+")
_PUNCT_ONLY_RE = re.compile(r"^[\s，,、；;：:。.！!？?…~～—\-·'\"“”‘’（）()\[\]【】]*$")
_TRAILING_PERIOD_RE = re.compile(r"(?<![.。])[。.]$")
_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9_@#%&+\-./:']")
_ATTACHING_PUNCT = frozenset("，,、；;：:。.！!？?…~～—”’」』）)]】")
_OPENING_PUNCT = frozenset("“‘「『（([【")


def normalize_rhythm(value: object) -> str:
    key = str(value or "").strip().lower()
    return _RHYTHM_ALIASES.get(key, RHYTHM_AUTO)


@dataclass(frozen=True, slots=True)
class RhythmSettings:
    mode: str = SPLIT_MODE_AUTO
    max_parts: int = DEFAULT_MAX_PARTS
    pace: float = 1.0
    max_spread_seconds: float = DEFAULT_MAX_SPREAD_SECONDS

    @property
    def enabled(self) -> bool:
        return self.mode != SPLIT_MODE_OFF and self.max_parts > 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "max_parts": self.max_parts,
            "pace": self.pace,
            "max_spread_seconds": self.max_spread_seconds,
        }


def _env_float(name: str, default: float, low: float, high: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if math.isnan(value):
        return default
    return max(low, min(value, high))


def rhythm_settings() -> RhythmSettings:
    """每次调用都读环境变量：WebUI 改配置后重启前后都能立刻看到口径。"""

    mode = os.environ.get("AGENT_REPLY_SPLIT", "").strip().lower() or SPLIT_MODE_AUTO
    if mode in {"false", "0", "no", "disabled"}:
        mode = SPLIT_MODE_OFF
    if mode not in SPLIT_MODES:
        mode = SPLIT_MODE_AUTO
    max_parts = int(
        _env_float("AGENT_REPLY_MAX_PARTS", DEFAULT_MAX_PARTS, 1, MAX_SPLIT_PARTS)
    )
    return RhythmSettings(
        mode=mode,
        max_parts=max_parts,
        pace=_env_float("AGENT_REPLY_PACE", 1.0, 0.0, 3.0),
        max_spread_seconds=_env_float(
            "AGENT_REPLY_MAX_SPREAD_SECONDS",
            DEFAULT_MAX_SPREAD_SECONDS,
            0.0,
            MAX_SPREAD_SECONDS_LIMIT,
        ),
    )


# ---------------------------------------------------------------------------
# 标记解析
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RhythmMarkup:
    text: str
    parts: tuple[str, ...]
    rhythm: str = RHYTHM_AUTO

    @property
    def marked(self) -> bool:
        return len(self.parts) >= 2


def _extract_tag(text: str) -> tuple[str, str]:
    declared = RHYTHM_AUTO
    match = _TAG_RE.search(text)
    if match is not None:
        declared = normalize_rhythm(match.group(1))
    return _TAG_RE.sub("", text), declared


def strip_rhythm_markup(text: object) -> str:
    """去掉节奏标签和分条标记，留下一条消息能读的正文（历史、审计、提示用）。"""

    cleaned, _ = _extract_tag(str(text or ""))
    pieces = [item.strip() for item in _MARKER_RE.split(cleaned)]
    return "\n".join(item for item in pieces if item).strip()


def has_rhythm_markup(text: object) -> bool:
    value = str(text or "")
    return bool(_MARKER_RE.search(value) or _TAG_RE.search(value))


def parse_rhythm_markup(text: object) -> RhythmMarkup:
    cleaned, declared = _extract_tag(str(text or ""))
    if _CODE_FENCE in cleaned:
        # 代码块里的 ||| 可能是真代码（shell 管道、正则），整条原样发。
        joined = cleaned.strip()
        return RhythmMarkup(joined, (), RHYTHM_BLOCK if declared == RHYTHM_AUTO else declared)
    pieces = tuple(item.strip() for item in _MARKER_RE.split(cleaned) if item.strip())
    joined = "\n".join(pieces)
    if len(pieces) < 2:
        return RhythmMarkup(joined, (), declared)
    return RhythmMarkup(joined, pieces, declared)


BREAK_SEGMENT_TYPE = "break"


def split_segment_markup(
    segments: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> tuple[tuple[dict[str, Any], ...], str]:
    """把 text 段里的 ||| 转成内部 ``break`` 伪段，并取出节奏标签。

    ``break`` 只在 SpeechPlan 内部存在，发送前一定被 ``segment_groups`` 消费，
    不会进入 OneBot 校验。
    """

    declared = RHYTHM_AUTO
    output: list[dict[str, Any]] = []
    for raw in segments:
        item = dict(raw)
        if str(item.get("type") or "").strip().lower() != "text":
            output.append(item)
            continue
        raw_text = str(item.get("text") or "")
        if not has_rhythm_markup(raw_text):
            output.append(item)
            continue
        text, tag = _extract_tag(raw_text)
        if tag != RHYTHM_AUTO and declared == RHYTHM_AUTO:
            declared = tag
        if _CODE_FENCE in text:
            if text.strip():
                output.append({**item, "text": text})
            continue
        pieces = _MARKER_RE.split(text)
        for index, piece in enumerate(pieces):
            if index > 0:
                output.append({"type": BREAK_SEGMENT_TYPE})
            if piece.strip():
                output.append({**item, "text": piece.strip()})
    # 去掉首尾和连续的 break
    compact: list[dict[str, Any]] = []
    for item in output:
        is_break = item.get("type") == BREAK_SEGMENT_TYPE
        if is_break and (not compact or compact[-1].get("type") == BREAK_SEGMENT_TYPE):
            continue
        compact.append(item)
    while compact and compact[-1].get("type") == BREAK_SEGMENT_TYPE:
        compact.pop()
    return tuple(compact), declared


def segment_groups(
    segments: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = [[]]
    for item in segments:
        if str(item.get("type") or "") == BREAK_SEGMENT_TYPE:
            if groups[-1]:
                groups.append([])
            continue
        groups[-1].append(dict(item))
    return [group for group in groups if group]


def strip_break_segments(
    segments: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> list[dict[str, Any]]:
    """兼容单条发送：break 改成换行文本，相邻文本合并。"""

    output: list[dict[str, Any]] = []
    for item in segments:
        if str(item.get("type") or "") == BREAK_SEGMENT_TYPE:
            if output and output[-1].get("type") == "text":
                output[-1] = {**output[-1], "text": str(output[-1].get("text") or "") + "\n"}
            continue
        if (
            item.get("type") == "text"
            and output
            and output[-1].get("type") == "text"
            and str(output[-1].get("text") or "").endswith("\n")
        ):
            output[-1] = {
                **output[-1],
                "text": str(output[-1].get("text") or "") + str(item.get("text") or ""),
            }
            continue
        output.append(dict(item))
    return output


# ---------------------------------------------------------------------------
# 拆分
# ---------------------------------------------------------------------------


def is_structured_text(text: str) -> bool:
    """代码块、列表、表格式内容放在一条里更好读，不做兜底拆分。"""

    if _CODE_FENCE in text:
        return True
    lines = [line for line in text.split("\n") if line.strip()]
    list_lines = sum(1 for line in lines if _LIST_LINE_RE.match(line))
    return list_lines >= 2 or sum(1 for line in lines if line.count(" / ") >= 2) >= 2


def _visible_len(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def _sentences(text: str) -> list[str]:
    return [item for item in (piece.strip() for piece in _SENTENCE_END_RE.split(text)) if item]


def _balanced_groups(units: list[str], count: int, joiner: str = "") -> list[str]:
    """把有序的句子按字数尽量均匀地分成 count 组，不打乱顺序。"""

    if count <= 1 or len(units) <= 1:
        return [joiner.join(units)] if units else []
    count = min(count, len(units))
    total = sum(_visible_len(item) for item in units)
    target = total / count
    groups: list[list[str]] = [[]]
    acc = 0
    for index, unit in enumerate(units):
        remaining_units = len(units) - index
        remaining_groups = count - len(groups)
        if (
            groups[-1]
            and remaining_groups > 0
            and (acc >= target * len(groups) - _visible_len(unit) / 2 or remaining_units <= remaining_groups)
        ):
            groups.append([])
        groups[-1].append(unit)
        acc += _visible_len(unit)
    return [joiner.join(group) for group in groups if group]


def _auto_parts(
    text: str, *, max_parts: int, sentences: bool = True
) -> tuple[tuple[str, ...], str]:
    """按结构拆分；返回 (parts, source)。

    sentences=False 时只认模型自己写的空行/换行：模型没选节奏时系统不再替它
    把一整段按句号切开（那样只是把长话伪装成分条，还会鼓励它越写越长）。
    """

    if max_parts < 2 or is_structured_text(text):
        return (), "structured" if is_structured_text(text) else "single"
    paragraphs = [item.strip() for item in re.split(r"\n[ \t]*\n", text) if item.strip()]
    if 2 <= len(paragraphs) <= max_parts and all(
        _visible_len(item) <= AUTO_PARAGRAPH_MAX_CHARS for item in paragraphs
    ):
        return tuple(paragraphs), "auto_paragraph"
    if len(paragraphs) > 1:
        return (), "single"
    lines = [item.strip() for item in text.split("\n") if item.strip()]
    if 2 <= len(lines) <= max_parts and all(
        _visible_len(item) <= AUTO_LINE_MAX_CHARS for item in lines
    ):
        return tuple(lines), "auto_line"
    if not sentences or len(lines) > 1 or _visible_len(text) < AUTO_SENTENCE_MIN_CHARS:
        return (), "single"
    units = _sentences(text)
    if len(units) < 2:
        return (), "single"
    count = 2 if _visible_len(text) < 90 else max_parts
    groups = _balanced_groups(units, count)
    if len(groups) < 2 or any(_visible_len(item) < AUTO_PART_MIN_CHARS for item in groups):
        groups = _balanced_groups(units, 2)
    if len(groups) < 2 or any(_visible_len(item) < AUTO_PART_MIN_CHARS for item in groups):
        return (), "single"
    return tuple(groups), "auto_sentence"


def _clauses(text: str) -> list[str]:
    return [item.strip() for item in _CLAUSE_END_RE.split(text) if item and item.strip()]


def pop_pieces(text: str, *, max_parts: int = POP_MAX_PARTS) -> tuple[str, ...]:
    """把一句很短的话拆成一个字一个字；英文单词、数字和 emoji 不拆开，
    标点粘在前一个字后面，开括号/引号粘在后一个字前面。"""

    tokens: list[str] = []
    pending_open = ""
    index = 0
    value = "".join(text.split())
    while index < len(value):
        char = value[index]
        if char in _OPENING_PUNCT:
            pending_open += char
            index += 1
            continue
        if char in _ATTACHING_PUNCT and tokens:
            tokens[-1] += char
            index += 1
            continue
        if _ASCII_WORD_RE.match(char):
            end = index
            while end < len(value) and _ASCII_WORD_RE.match(value[end]):
                end += 1
            token = value[index:end]
            index = end
        else:
            token = char
            index += 1
            # 变体选择符 / 零宽连接的 emoji 序列不拆开
            while index < len(value) and value[index] in "️‍":
                token += value[index]
                index += 1
                if token.endswith("‍") and index < len(value):
                    token += value[index]
                    index += 1
        tokens.append(pending_open + token)
        pending_open = ""
    if pending_open and tokens:
        tokens[-1] += pending_open
    if len(tokens) > max_parts:
        tokens = _balanced_groups(tokens, max_parts)
    return tuple(tokens)


def _merge_overflow(parts: list[str], cap: int) -> list[str]:
    if len(parts) <= cap:
        return parts
    if cap <= 1:
        return ["\n".join(parts)]
    head = parts[: cap - 1]
    return [*head, "\n".join(parts[cap - 1 :])]


def _tidy_parts(parts: list[str], *, rhythm: str) -> list[str]:
    """合并纯标点碎片；分条节奏去掉连续重复；多条时去掉句末的“。”。"""

    tidy: list[str] = []
    for raw in parts:
        part = raw.strip()
        if not part:
            continue
        if _PUNCT_ONLY_RE.match(part) and tidy and rhythm != RHYTHM_POP:
            # 单独一条“。”没有意义；其它标点（？！…）粘到上一条后面。
            if part.strip("。.") and not tidy[-1].endswith(part):
                tidy[-1] += part
            continue
        if rhythm in {RHYTHM_SPLIT, RHYTHM_AUTO} and tidy and tidy[-1] == part:
            continue
        tidy.append(part)
    if len(tidy) >= 2 and rhythm != RHYTHM_POP:
        tidy = [_TRAILING_PERIOD_RE.sub("", item) or item for item in tidy]
    return tidy


def _infer_rhythm(parts: list[str]) -> str:
    if len(parts) <= 1:
        return RHYTHM_BLOCK
    lengths = [_visible_len(item) for item in parts]
    if len(parts) >= 3 and max(lengths) <= 2:
        return RHYTHM_POP
    if len(parts) >= 3 and max(lengths) <= BURST_PART_MAX_CHARS:
        return RHYTHM_BURST
    return RHYTHM_SPLIT


def _rhythm_cap(rhythm: str, settings: RhythmSettings) -> int:
    if rhythm == RHYTHM_POP:
        return POP_MAX_PARTS
    if rhythm == RHYTHM_BURST:
        return max(BURST_MAX_PARTS, settings.max_parts)
    return settings.max_parts


# ---------------------------------------------------------------------------
# 停顿
# ---------------------------------------------------------------------------

# (思考秒数, 每秒打字数, 最短, 最长)：分条像正常打字，连发几乎不停，蹦字最快。
_PACING: dict[str, tuple[float, float, float, float]] = {
    RHYTHM_SPLIT: (0.8, 7.0, 1.0, 4.5),
    RHYTHM_BURST: (0.25, 12.0, 0.35, 1.4),
    RHYTHM_POP: (0.12, 24.0, 0.22, 0.75),
}
# 情绪只调快慢：兴奋/恼火打字更快，关切/谨慎更慢。强度越低越接近 1。
_EMOTION_PACE: dict[str, float] = {
    "amused": 0.8,
    "irritated": 0.75,
    "curious": 0.9,
    "warm": 0.95,
    "concerned": 1.2,
    "guarded": 1.15,
    "neutral": 1.0,
}


def emotion_pace_factor(emotion: object) -> float:
    if not isinstance(emotion, dict):
        return 1.0
    label = str(emotion.get("label") or "neutral")
    base = _EMOTION_PACE.get(label, 1.0)
    try:
        strength = float(
            emotion.get("expression_intensity", emotion.get("intensity", 0.0)) or 0.0
        )
    except (TypeError, ValueError):
        strength = 0.0
    strength = max(0.0, min(strength, 1.0))
    return 1.0 + (base - 1.0) * strength


def persona_pace_factor(style: object) -> float:
    """表现力高的人设打字更快更碎；详略高的人设更慢更稳。"""

    expressiveness = getattr(style, "expressiveness", 1)
    verbosity = getattr(style, "verbosity", 1)
    try:
        factor = 1.0 - (int(expressiveness) - 1) * 0.06 + (int(verbosity) - 1) * 0.04
    except (TypeError, ValueError):
        return 1.0
    return max(0.75, min(factor, 1.25))


def pacing_delays(
    parts: tuple[str, ...] | list[str],
    rhythm: str,
    *,
    settings: RhythmSettings | None = None,
    emotion: object = None,
    style: object = None,
    seed: str = "",
) -> tuple[float, ...]:
    """每条之前的停顿秒数；第一条永远是 0（模型已经想好了，马上发）。"""

    resolved = settings or rhythm_settings()
    if not parts:
        return ()
    if len(parts) == 1 or resolved.pace <= 0:
        return tuple(0.0 for _ in parts)
    think, cps, low, high = _PACING.get(rhythm, _PACING[RHYTHM_SPLIT])
    factor = resolved.pace * emotion_pace_factor(emotion) * persona_pace_factor(style)
    rng = random.Random(int(hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12], 16))
    delays = [0.0]
    for part in parts[1:]:
        raw = think + _visible_len(part) / cps
        jitter = rng.uniform(0.85, 1.15)
        delays.append(round(max(low, min(raw, high)) * factor * jitter, 3))
    spread = sum(delays)
    cap = resolved.max_spread_seconds
    if cap <= 0:
        return tuple(0.0 for _ in parts)
    if spread > cap:
        ratio = cap / spread
        delays = [round(item * ratio, 3) for item in delays]
    return tuple(delays)


# ---------------------------------------------------------------------------
# 规划
# ---------------------------------------------------------------------------

_NO_AUTO_ACTS = frozenset({"ping_ack", "acknowledge", "react", "close"})
_SHORT_SCENES = frozenset({"active_interject", "warmup", "reaction"})


@dataclass(frozen=True, slots=True)
class RhythmPlan:
    rhythm: str
    parts: tuple[str, ...]
    delays: tuple[float, ...]
    source: str
    declared: str = RHYTHM_AUTO

    @property
    def multi(self) -> bool:
        return len(self.parts) >= 2

    @property
    def text(self) -> str:
        return "\n".join(self.parts)

    @property
    def spread_seconds(self) -> float:
        return round(sum(self.delays), 3)

    def trace_payload(self) -> dict[str, Any]:
        return {
            "rhythm": self.rhythm,
            "rhythm_label": RHYTHM_LABELS.get(self.rhythm, self.rhythm),
            "declared": self.declared,
            "source": self.source,
            "parts": len(self.parts),
            "part_chars": [_visible_len(item) for item in self.parts],
            "delays": list(self.delays),
            "spread_seconds": self.spread_seconds,
        }

    def preview(self) -> list[dict[str, Any]]:
        return [
            {"text": part, "delayMs": round(delay * 1000)}
            for part, delay in zip(self.parts, self.delays, strict=False)
        ]


def single_rhythm(text: str, *, source: str, declared: str = RHYTHM_AUTO) -> RhythmPlan:
    return RhythmPlan(RHYTHM_BLOCK, (text,) if text else (), (0.0,) if text else (), source, declared)


def plan_text_rhythm(
    text: str,
    *,
    parts: tuple[str, ...] | list[str] = (),
    declared: str = RHYTHM_AUTO,
    scene: str = "conversation",
    act: str = "continue",
    complexity: str = "normal",
    style: object = None,
    emotion: object = None,
    settings: RhythmSettings | None = None,
) -> RhythmPlan:
    """决定一段已经清洗过的正文怎么发：几条、什么节奏、每条前停多久。"""

    resolved = settings or rhythm_settings()
    declared = normalize_rhythm(declared)
    joined = "\n".join(item for item in parts if item.strip()) if parts else str(text or "").strip()
    if not joined:
        return single_rhythm("", source="empty", declared=declared)
    if not resolved.enabled:
        return single_rhythm(joined, source="disabled", declared=declared)
    if _CODE_FENCE in joined:
        return single_rhythm(joined, source="structured", declared=declared)
    if declared == RHYTHM_BLOCK:
        return single_rhythm(joined, source="model", declared=declared)

    candidates: list[str]
    source: str
    if declared == RHYTHM_POP:
        compact = "".join(joined.split())
        if _visible_len(compact) <= POP_MAX_CHARS and len(pop_pieces(compact)) >= 2:
            candidates = list(pop_pieces(compact))
            source = "model"
        else:
            # 太长的话一个字一个字蹦会刷屏，降级成按短句连发。
            declared_effective = RHYTHM_BURST
            candidates = (
                list(parts)
                if len(parts) >= 2
                else _balanced_groups(_clauses(joined), _rhythm_cap(declared_effective, resolved))
            )
            candidates = _merge_overflow(_tidy_parts(candidates, rhythm=declared_effective), _rhythm_cap(declared_effective, resolved))
            return _finish(candidates, declared_effective, "pop_downgraded", declared, resolved, emotion, style, joined)
    elif len(parts) >= 2:
        candidates = [item.strip() for item in parts if item.strip()]
        source = "model"
    elif declared == RHYTHM_BURST:
        candidates = _balanced_groups(_clauses(joined), _rhythm_cap(RHYTHM_BURST, resolved))
        source = "model_clauses"
    elif declared == RHYTHM_SPLIT:
        auto, auto_source = _auto_parts(joined, max_parts=resolved.max_parts)
        candidates = list(auto) or _balanced_groups(_sentences(joined), resolved.max_parts)
        source = auto_source if auto else "model_sentences"
    else:
        if (
            resolved.mode != SPLIT_MODE_AUTO
            or complexity == "complex"
            or act in _NO_AUTO_ACTS
        ):
            return single_rhythm(joined, source="single", declared=declared)
        cap = 2 if scene in _SHORT_SCENES else resolved.max_parts
        auto, source = _auto_parts(joined, max_parts=cap, sentences=False)
        if not auto:
            return single_rhythm(joined, source=source, declared=declared)
        candidates = list(auto)

    rhythm = declared if declared != RHYTHM_AUTO else RHYTHM_AUTO
    tidy = _tidy_parts(candidates, rhythm=rhythm)
    if rhythm == RHYTHM_AUTO:
        rhythm = _infer_rhythm(tidy)
    cap = _rhythm_cap(rhythm, resolved)
    if scene in _SHORT_SCENES and rhythm == RHYTHM_SPLIT:
        cap = min(cap, 2)
    tidy = _merge_overflow(tidy, cap)
    return _finish(tidy, rhythm, source, declared, resolved, emotion, style, joined)


def _finish(
    parts: list[str],
    rhythm: str,
    source: str,
    declared: str,
    settings: RhythmSettings,
    emotion: object,
    style: object,
    seed: str,
) -> RhythmPlan:
    clean = tuple(item for item in parts if item.strip())
    if len(clean) <= 1:
        return single_rhythm(clean[0] if clean else seed, source=source, declared=declared)
    if rhythm in {RHYTHM_AUTO, RHYTHM_BLOCK}:
        rhythm = _infer_rhythm(list(clean))
    elif rhythm == RHYTHM_BURST and max(_visible_len(item) for item in clean) > BURST_LONG_PART_CHARS:
        # 说是连发、每条却是一整句：按正常打字速度发，别像机关枪一样甩长句。
        rhythm = RHYTHM_SPLIT
    delays = pacing_delays(clean, rhythm, settings=settings, emotion=emotion, style=style, seed=seed)
    return RhythmPlan(rhythm, clean, delays, source, declared)


def plan_group_rhythm(
    group_texts: list[str],
    *,
    declared: str = RHYTHM_AUTO,
    style: object = None,
    emotion: object = None,
    settings: RhythmSettings | None = None,
    seed: str = "",
) -> RhythmPlan:
    """结构化消息段已按 break 分好组时，只算节奏与停顿，不再拆。"""

    resolved = settings or rhythm_settings()
    parts = tuple(item if item.strip() else "[消息段]" for item in group_texts)
    if len(parts) <= 1:
        return single_rhythm(parts[0] if parts else "", source="segments", declared=declared)
    rhythm = normalize_rhythm(declared)
    if rhythm in {RHYTHM_AUTO, RHYTHM_BLOCK}:
        rhythm = _infer_rhythm(list(parts))
    delays = pacing_delays(parts, rhythm, settings=resolved, emotion=emotion, style=style, seed=seed or "".join(parts))
    return RhythmPlan(rhythm, parts, delays, "segments", declared)


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------


# 情绪只给“更像你此刻心情”的倾向，选哪种仍由模型自己定。情绪来自 emotion_state，
# 不看群消息关键词。
_EMOTION_RHYTHM_LEAN: dict[str, str] = {
    "amused": "你现在心情不错，想闹就连发或蹦字",
    "irritated": "你现在有点不耐，短句连发更像你",
    "curious": "你现在挺好奇，分条追问很自然",
    "warm": "你现在挺亲和，分条慢慢说就好",
    "concerned": "你现在有点担心，整段或分条认真说，别蹦字",
    "guarded": "你现在比较谨慎，整段说清楚，别连发也别蹦字",
}
_EMOTION_LEAN_MIN = 0.3


def _emotion_lean(emotion: object) -> str:
    if not isinstance(emotion, dict):
        return ""
    label = str(emotion.get("label") or "neutral")
    try:
        strength = float(
            emotion.get("expression_intensity", emotion.get("intensity", 0.0)) or 0.0
        )
    except (TypeError, ValueError):
        return ""
    if strength < _EMOTION_LEAN_MIN:
        return ""
    lean = _EMOTION_RHYTHM_LEAN.get(label, "")
    return f"{lean}。" if lean else ""


def rhythm_instruction(
    *,
    scene: str,
    act: str,
    complexity: str,
    style: object = None,
    settings: RhythmSettings | None = None,
    emotion: object = None,
) -> str:
    """放在易变的发言策略层，不进稳定前缀；关闭时返回空串。

    要模型每次先写【节奏:…】：节奏是它自己的选择，不再靠系统事后拆句兜底。
    节奏只决定“怎么发”，不是多说几句的理由，所以总字数口径和一条时一样。
    """

    resolved = settings or rhythm_settings()
    if not resolved.enabled:
        return ""
    lean = _emotion_lean(emotion)
    if scene in _SHORT_SCENES:
        return (
            "发送节奏：插话/暖场通常一条就够，开头写【节奏:整段】；"
            "情绪到了也可以【节奏:连发】两三个几个字的短句用 ||| 隔开。"
            "标签不会发出去。" + lean
        )
    expressive = int(getattr(style, "expressiveness", 1) or 1) >= 3
    lines = [
        "发送节奏（每次都要选）：回复最前面先写【节奏:整段/分条/连发/蹦字】，标签不会发出去，"
        "||| 隔开的每段是一条 QQ 消息。节奏就是你此刻的心情，像真人一样挑：",
        "整段=认真讨论、争论、讲道理或给步骤，一条发完；",
        f"分条=平常闲聊，把同一句话在自然停顿处断成 2~{resolved.max_parts} 条，"
        "如【节奏:分条】刚看到|||笑死 这也太巧了；",
        "连发=激动、震惊、急了、强烈吐槽，3~5 条每条几个字的短句，"
        "如【节奏:连发】不是吧|||真的假的|||我不信；",
        "蹦字=搞怪卖萌、拖长音，只写一句 8 字以内的话，会一个字一个字蹦出去，"
        "如【节奏:蹦字】你在干嘛呀。",
        "节奏不是多说话的理由：分几条，加起来的字数也和发一条时一样，守住上面的字数目标；"
        "连发和蹦字通常比一条更短。别回回同一种，代码、列表、链接放同一条。",
    ]
    if act in _NO_AUTO_ACTS:
        lines.append("本轮是短回应，整段一句或几个字的连发就够。")
    elif complexity == "complex":
        lines.append("本轮问题较复杂，用【节奏:整段】一条把话说完整。")
    if lean:
        lines.append(lean)
    if expressive:
        lines.append("你的表现力较强，情绪明显时可以更放开地用连发和蹦字。")
    return "".join(lines)


TOOL_RHYTHM_HINT = "text 里用 ||| 把要分开发的部分隔开会依次发成多条，rhythm 可选 block/split/burst/pop"

__all__ = [
    "BREAK_SEGMENT_TYPE",
    "PART_MARKER",
    "RHYTHMS",
    "RHYTHM_AUTO",
    "RHYTHM_BLOCK",
    "RHYTHM_BURST",
    "RHYTHM_LABELS",
    "RHYTHM_POP",
    "RHYTHM_SPLIT",
    "SPLIT_MODES",
    "TOOL_RHYTHM_HINT",
    "RhythmMarkup",
    "RhythmPlan",
    "RhythmSettings",
    "emotion_pace_factor",
    "has_rhythm_markup",
    "is_structured_text",
    "normalize_rhythm",
    "pacing_delays",
    "parse_rhythm_markup",
    "persona_pace_factor",
    "plan_group_rhythm",
    "plan_text_rhythm",
    "pop_pieces",
    "rhythm_instruction",
    "rhythm_settings",
    "segment_groups",
    "single_rhythm",
    "split_segment_markup",
    "strip_break_segments",
    "strip_rhythm_markup",
]
