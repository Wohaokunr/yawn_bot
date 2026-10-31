# ruff: noqa: E501,TC001,PLR2004
"""发送前把 SpeechPlan 里的 ``sticker`` 伪段换成真正的表情包。

模型写的 ``[表情:名字]`` 在 :mod:`speech` 里已经拆成伪段；这里按当前群的策略
决定每一个能不能发：

- 功能关了 / 本群没开放 / 冷却中 / 今日额度用完 → 去掉；
- 一条发言里超过 ``max_per_message`` 张 → 多出来的去掉；
- 名字或情绪在本群可用的库里对不上 → 去掉（不猜、不模糊匹配）；
- 对上了 → 换成 ``{"type": "reaction", "reaction_id": …}``，同名/同情绪多张时
  按“避开最近发过的、优先用得少的”挑一张。

去掉表情包只会收回它两侧自动插的 break，不会让整句失败；整句只剩一个被去掉
的表情时，退回成文字 ``[名字]``（QQ 群里常见的文字表情写法），保证明确叫到
Bot 的回合不会石沉大海。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .execution_trace import trace_event
from .log import dbg
from .speech import SpeechPlan
from .sticker_library import match_stickers, tag_counts
from .sticker_markup import STICKER_SEGMENT_TYPE, drop_sticker_at, sticker_instruction
from .sticker_policy import (
    REASON_TEXT,
    StickerExposure,
    exposure,
    menu_entries,
    note_dropped,
    pick_sticker,
)

_PROACTIVE_SCENES = frozenset({"active_interject", "warmup", "followup"})


def has_sticker_segments(plan: SpeechPlan) -> bool:
    return any(item.get("type") == STICKER_SEGMENT_TYPE for item in plan.segments)


def _visible(segments: list[dict[str, Any]]) -> bool:
    return any(
        item.get("type") not in {"break", "reply"}
        and (item.get("type") != "text" or str(item.get("text") or "").strip())
        for item in segments
    )


def _send_gate(plan: SpeechPlan, group_id: int | None) -> tuple[StickerExposure, int]:
    """发送层的闸门与本条最多几张。"""

    gate = exposure(group_id, proactive=plan.scene in _PROACTIVE_SCENES)
    if gate.open and not plan.style.allow_spontaneous_reaction:
        # 提示词层因人设不给菜单，模型照着历史写了标记也不发（与主动发言一致）。
        gate = replace(gate, reason="persona_off")
    if not gate.open:
        return gate, 0
    # 一条里最多几张，同时不超过本群今天剩下的额度。
    return gate, min(gate.settings.max_per_message, gate.left_today)


def resolve_plan_stickers(plan: SpeechPlan, *, group_id: int | None) -> SpeechPlan:
    """把伪段换成 reaction 段或去掉；没有伪段时原样返回。"""

    if not has_sticker_segments(plan):
        return plan
    segments = [dict(item) for item in plan.segments]
    gate, max_count = _send_gate(plan, group_id)
    decisions: list[dict[str, Any]] = []
    dropped: set[int] = set()
    chosen = 0
    for index, item in enumerate(segments):
        if item.get("type") != STICKER_SEGMENT_TYPE:
            continue
        name = str(item.get("name") or "").strip()
        if not gate.open:
            dropped.add(index)
            decisions.append({"name": name, "status": "dropped", "reason": gate.reason})
            continue
        if chosen >= max_count:
            dropped.add(index)
            decisions.append({"name": name, "status": "dropped", "reason": "too_many"})
            continue
        how, candidates = match_stickers(name, group_id, stickers=gate.stickers)
        picked = pick_sticker(int(group_id or 0), candidates) if candidates else None
        if picked is None:
            dropped.add(index)
            decisions.append({"name": name, "status": "dropped", "reason": "unknown"})
            continue
        chosen += 1
        segments[index] = {"type": "reaction", "reaction_id": picked.sticker_id}
        decisions.append(
            {
                "name": name,
                "status": "resolved",
                "match": how,
                "sticker_id": picked.sticker_id,
                "sticker_name": picked.name,
                "candidates": len(candidates),
            }
        )
    resolved = drop_sticker_at(segments, dropped) if dropped else segments
    fallback = None
    if dropped and not _visible(resolved):
        names = [str(segments[index].get("name") or "").strip() for index in sorted(dropped)]
        fallback = next((item for item in names if item), None)
        if fallback:
            resolved = [*resolved, {"type": "text", "text": f"[{fallback}]"}]
    note_dropped(len(dropped))
    reason_text = REASON_TEXT.get(gate.reason or "", "") if gate.reason else ""
    trace_event(
        "speech",
        "表情包",
        status="planned" if chosen else "skipped",
        output={
            "gate": gate.as_dict(),
            "decisions": decisions,
            **({"fallback_text": True} if fallback else {}),
        },
        detail=(
            f"发 {chosen} 张表情包" + (f"，去掉 {len(dropped)} 个" if dropped else "")
            if chosen
            else f"去掉 {len(dropped)} 个表情包标记" + (f"：{reason_text}" if reason_text else "")
        ),
    )
    if dropped:
        dbg(f"群 {group_id} 表情包标记去掉 {len(dropped)} 个: {[item for item in decisions if item['status'] == 'dropped']}")
    return replace(plan, segments=tuple(resolved))


_SHORT_SCENES = frozenset({"active_interject", "warmup", "reaction"})
MAX_MENU_TAGS = 12


def sticker_turn_instruction(
    group_id: int | None,
    *,
    scene: str,
    complexity: str = "normal",
    allow_reaction: bool = True,
) -> str:
    """本轮的表情包用法 + 菜单；不该发（关了、冷却、额度、人设）时返回空串。

    放在易变的发言策略层（``speech.sticker`` / ``proactive.sticker``），不进稳定
    前缀。是否给菜单只看开关、库存、频率和人设，不看聊天内容。
    """

    if group_id is None or not allow_reaction:
        return ""
    gate = exposure(group_id, proactive=scene in _PROACTIVE_SCENES)
    if not gate.open:
        return ""
    settings = gate.settings
    menu = menu_entries(int(group_id), gate.stickers, size=settings.menu_size)
    tags = [
        (tag, count)
        for tag, count in tag_counts(int(group_id), stickers=gate.stickers)
        if count >= 2
    ][:MAX_MENU_TAGS]
    return sticker_instruction(
        menu,
        tags,
        frequency=settings.frequency,
        max_per_message=settings.max_per_message,
        complexity=complexity,
        short_scene=scene in _SHORT_SCENES,
    )


__all__ = ["has_sticker_segments", "resolve_plan_stickers", "sticker_turn_instruction"]
