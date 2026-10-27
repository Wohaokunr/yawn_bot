# ruff: noqa: E501,TC003,PLR0913
"""表情包标记：模型在正文里写 ``[表情:名字]``，发送时变成单独一条表情包消息。

和 ``|||`` 分条一样，表情包是发送序列里的一条：``哈哈哈|||[表情:捂脸笑]``
会先发文字、停一下、再发表情；整条回复也可以只有一个表情。本模块是纯函数：

- ``expand_sticker_segments``：把 text 段里的标记拆成内部 ``sticker`` 伪段，
  前后各插一个 ``break``，保证表情包自成一条；
- ``strip_sticker_markup``：单条文字场景（进度提示、历史转述）去掉标记；
- ``sticker_instruction``：给模型看的菜单与用法（放在易变的发言策略层）。

``sticker`` 伪段只在 SpeechPlan 内部存在，发送前由 ``sticker_send`` 换成真正的
``reaction`` 段或去掉，绝不进 OneBot 校验。成员发来的表情包在历史里显示为
``[表情包: 狗头]``，模型照着写也能认。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

STICKER_SEGMENT_TYPE = "sticker"
_BREAK = "break"  # 与 speech_rhythm.BREAK_SEGMENT_TYPE 一致；这里不 import，避免循环。
_CODE_FENCE = "```"
MAX_MARKERS = 6

# [表情:名字] [表情包: 名字] 【表情：名字】 [sticker:xx]；名字里不允许括号和换行。
_MARKER_RE = re.compile(
    r"[\[【]\s*(?:表情包|表情|sticker|贴图)\s*[:：]\s*([^\[\]【】\r\n:：]{1,24}?)\s*[\]】]",
    re.IGNORECASE,
)


def has_sticker_markup(text: object) -> bool:
    value = str(text or "")
    return _CODE_FENCE not in value and bool(_MARKER_RE.search(value))


def sticker_names(text: object) -> list[str]:
    value = str(text or "")
    if _CODE_FENCE in value:
        return []
    return [match.group(1).strip() for match in _MARKER_RE.finditer(value)][:MAX_MARKERS]


def strip_sticker_markup(text: object) -> str:
    """去掉标记（单条文字场景用）；整段只剩标记时返回空串。"""

    value = str(text or "")
    if _CODE_FENCE in value:
        return value
    cleaned = _MARKER_RE.sub("", value)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


def describe_sticker_markup(text: object) -> str:
    """历史/审计用：``[表情:捂脸]`` → ``[表情包: 捂脸]``，和成员发的表情包同一个写法。"""

    value = str(text or "")
    if _CODE_FENCE in value:
        return value
    return _MARKER_RE.sub(lambda match: f"[表情包: {match.group(1).strip()}]", value)


def expand_sticker_segments(
    segments: Sequence[dict[str, Any]],
) -> tuple[tuple[dict[str, Any], ...], int]:
    """text 段里的标记 → ``break, {"type": "sticker", "name": …}, break``。

    返回 ``(新消息段, 标记个数)``；超过 ``MAX_MARKERS`` 的标记直接丢掉。
    代码块里的方括号原样保留。
    """

    output: list[dict[str, Any]] = []
    found = 0
    for raw in segments:
        item = dict(raw)
        if str(item.get("type") or "").strip().lower() != "text":
            output.append(item)
            continue
        text = str(item.get("text") or "")
        if not has_sticker_markup(text):
            output.append(item)
            continue
        cursor = 0
        for match in _MARKER_RE.finditer(text):
            before = text[cursor : match.start()]
            if before.strip():
                output.append({**item, "text": before.strip()})
            cursor = match.end()
            found += 1
            if found > MAX_MARKERS:
                continue
            # 标出“为表情包插的 break”：表情包被去掉时连同它们一起收回，
            # 让原本连着写的文字重新合成一条。
            output.append({"type": _BREAK, "around_sticker": True})
            output.append({"type": STICKER_SEGMENT_TYPE, "name": match.group(1).strip()})
            output.append({"type": _BREAK, "around_sticker": True})
        after = text[cursor:]
        if after.strip():
            output.append({**item, "text": after.strip()})
    return tuple(output), found


def drop_sticker_at(
    segments: Sequence[dict[str, Any]], indices: set[int]
) -> list[dict[str, Any]]:
    """去掉指定位置的表情包伪段，收回它两侧自动插的 break，并把因此相邻的
    文字段接回一条（模型自己写的 ||| 留着）。"""

    kept: list[dict[str, Any]] = []
    for index, raw in enumerate(segments):
        item = dict(raw)
        if index in indices:
            if kept and kept[-1].get("type") == _BREAK and kept[-1].get("around_sticker"):
                kept.pop()
            continue
        if (
            item.get("type") == _BREAK
            and item.get("around_sticker")
            and index - 1 in indices
        ):
            continue
        kept.append(item)
    merged: list[dict[str, Any]] = []
    for item in kept:
        if (
            item.get("type") == "text"
            and merged
            and merged[-1].get("type") == "text"
        ):
            merged[-1] = {**merged[-1], "text": f"{merged[-1].get('text') or ''}{item.get('text') or ''}"}
            continue
        merged.append(item)
    return merged


_FREQUENCY_RULES: dict[str, str] = {
    "rare": "表情包要少用：只有气氛特别对、一张图比一句话更传神时才发，大多数回合不发。",
    "normal": "像普通群友一样偶尔发表情包：接梗、吐槽、无语、开心、尴尬时可以单独甩一张，或者说完一句再跟一张；不要每轮都发。",
    "often": "你是爱发表情包的群友，情绪到了就甩一张，可以只回一个表情包；但不要连着几轮都发同一类。",
}


def sticker_instruction(
    menu: Sequence[tuple[str, str]],
    tags: Sequence[tuple[str, int]],
    *,
    frequency: str = "normal",
    max_per_message: int = 1,
    complexity: str = "normal",
    short_scene: bool = False,
) -> str:
    """菜单 + 用法。``menu`` 是 ``(名字, 一句说明)``，``tags`` 是 ``(情绪, 张数)``。"""

    if not menu and not tags:
        return ""
    lines = [
        _FREQUENCY_RULES.get(frequency, _FREQUENCY_RULES["normal"]),
        "发表情包就在正文里写 [表情:名字]，它会单独成一条消息，可以放在 ||| 分条的任意位置，也可以整条回复只发一个表情包；"
        f"一次最多 {max_per_message} 个。只能用下面列出的名字或情绪，列表外的不要编，也不要再用文字描述表情包内容。",
        "认真讨论、讲步骤、解答问题、有人难过或在吵架时不要发表情包。",
    ]
    if complexity == "complex":
        lines.append("本轮问题较复杂，先把话说清楚，一般不发表情包。")
    if short_scene:
        lines.append("插话/暖场时一个合适的表情包可以代替一句话。")
    if menu:
        rendered = "；".join(f"{name}（{hint}）" if hint else name for name, hint in menu)
        lines.append(f"可用表情包：{rendered}。")
    if tags:
        rendered_tags = "、".join(tag if count <= 1 else f"{tag}×{count}" for tag, count in tags)
        lines.append(f"也可以按情绪写 [表情:情绪]，系统从这一类里挑一张：{rendered_tags}。")
    return "".join(lines)


__all__ = [
    "MAX_MARKERS",
    "STICKER_SEGMENT_TYPE",
    "describe_sticker_markup",
    "drop_sticker_at",
    "expand_sticker_segments",
    "has_sticker_markup",
    "sticker_instruction",
    "sticker_names",
    "strip_sticker_markup",
]
