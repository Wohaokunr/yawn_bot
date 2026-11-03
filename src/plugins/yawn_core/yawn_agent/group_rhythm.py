# ruff: noqa: E501, PLR2004
"""群聊分条的条数分布：按群记最近几次 Bot 回复分了几条，套模板时提醒一句。

和私聊（``private_rhythm``）同一个思路：模型会模仿自己前几次的形状，连着几次都是
``xxx|||xxx|||xxx`` 就越来越像三段式模板。这里只记“这一次计划发几条”（模型写的
条数，不受“有人排队就合并”影响），在发言策略的易变层（``speech.rhythm_mix``）
给一句“这次按内容来，一条也行”。判断规则在 ``speech_rhythm.mix_streak``，只看条数。

状态在进程内、按群有界，重启清零（只影响提醒）。``AGENT_REPLY_MIX_HINT=false`` 关掉提醒。
"""

from __future__ import annotations

import os
import time
from collections import Counter, deque
from typing import Any

from .speech_rhythm import mix_streak, rhythm_mix_instruction

RECENT_SHAPES = 8
MAX_TRACKED_GROUPS = 2048

_recent: dict[int, deque[tuple[int, float]]] = {}
_by_parts: Counter[str] = Counter()
_hints = 0


def mix_hint_enabled() -> bool:
    raw = os.environ.get("AGENT_REPLY_MIX_HINT", "").strip().lower()
    return raw not in {"false", "0", "no", "off", "disabled"}


def note_group_parts(group_id: int | None, parts: int, *, now: float | None = None) -> None:
    """记一次群里 Bot 回复计划发几条（发出了第一条才记）。"""

    if group_id is None or parts <= 0:
        return
    gid = int(group_id)
    moment = time.time() if now is None else now
    shapes = _recent.get(gid)
    if shapes is None:
        if len(_recent) >= MAX_TRACKED_GROUPS:
            oldest = min(_recent, key=lambda key: _recent[key][-1][1] if _recent[key] else 0.0)
            _recent.pop(oldest, None)
        shapes = deque(maxlen=RECENT_SHAPES)
        _recent[gid] = shapes
    shapes.append((int(parts), moment))
    _by_parts["5+" if parts >= 5 else str(parts)] += 1


def recent_group_counts(group_id: int | None) -> list[int]:
    if group_id is None:
        return []
    return [parts for parts, _at in _recent.get(int(group_id), ())]


def group_mix_instruction(group_id: int | None) -> str:
    """本轮发言策略里的“别套模板”提醒；不需要时返回空串。"""

    global _hints  # noqa: PLW0603
    if not mix_hint_enabled():
        return ""
    text = rhythm_mix_instruction(recent_group_counts(group_id))
    if text:
        _hints += 1
    return text


def snapshot(group_id: int | None = None) -> dict[str, Any]:
    total = sum(_by_parts.values()) or 1
    data: dict[str, Any] = {
        "byParts": {key: _by_parts.get(key, 0) for key in ("1", "2", "3", "4", "5+")},
        "partsShare": {
            key: round(_by_parts.get(key, 0) / total, 3) for key in ("1", "2", "3", "4", "5+")
        },
        "mixHints": _hints,
        "trackedGroups": len(_recent),
    }
    if group_id is not None:
        counts = recent_group_counts(group_id)
        data["recent"] = counts
        data["pattern"] = mix_streak(counts)
    return data


def reset() -> None:
    global _hints  # noqa: PLW0603
    _recent.clear()
    _by_parts.clear()
    _hints = 0


__all__ = [
    "group_mix_instruction",
    "mix_hint_enabled",
    "note_group_parts",
    "recent_group_counts",
    "reset",
    "snapshot",
]
