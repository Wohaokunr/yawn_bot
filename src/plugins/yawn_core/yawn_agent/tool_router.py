# ruff: noqa: C901,PLR0912,PLR2004
"""Deterministic, zero-AI-cost routing from one dialogue turn to tool schemas."""

from __future__ import annotations

from .tool_registry import (
    CONTROLLED_TOOLS,
    CORE_DIALOGUE_TOOL_NAMES,
    MESSAGE_SEND_TOOLS,
    ToolDefinition,
)

MAX_TOOL_ROUNDS = 4

# Natural QQ requests often insert scope words inside a registered keyword, e.g.
# "设置群精华" vs "设置精华" or "删除这条群消息精华".  Keep registry
# keywords concise and normalize only harmless scope fillers before matching instead
# of growing an unbounded synonym list for every tool.
_INTENT_SCOPE_FILLERS = (
    "这条消息",
    "当前消息",
    "群消息",
    "群聊",
    "这条",
    "当前",
    "消息",
    "群",
)


def _compact_tool_intent(value: str) -> str:
    compact = value.casefold()
    for filler in _INTENT_SCOPE_FILLERS:
        compact = compact.replace(filler, "")
    return compact


def _keyword_matches(normalized: str, keyword: str) -> bool:
    folded = keyword.casefold()
    if folded in normalized:
        return True
    compact_keyword = _compact_tool_intent(folded)
    return bool(
        compact_keyword
        and compact_keyword in _compact_tool_intent(normalized)
    )


def select_dialogue_tool_names(
    text: str | None,
    *,
    has_reply: bool = False,
    has_mentions: bool = False,
    has_media: bool = False,
    allow_admin_tools: bool = False,
) -> frozenset[str]:
    """Return only the progressive-disclosure bootstrap bundle.

    Business tools are never inferred from user text here. The model starts with
    the minimal core and must call ``discover_tools`` before any non-core schema
    is injected. Structural signals still shape message-segment schemas elsewhere,
    but they do not bypass tool discovery.
    """

    del text, has_reply, has_mentions, has_media, allow_admin_tools
    return frozenset(CORE_DIALOGUE_TOOL_NAMES)


def rank_discoverable_tools(
    query: str,
    candidates: list[ToolDefinition],
    *,
    family: str | None = None,
    limit: int = 5,
) -> list[ToolDefinition]:
    """Rank eligible registry entries without another model call."""

    normalized = str(query or "").strip().casefold()
    compact_normalized = _compact_tool_intent(normalized)
    family_filter = str(family or "").strip().casefold()
    scored: list[tuple[int, str, ToolDefinition]] = []
    for definition in candidates:
        if not definition.discoverable or definition.name == "discover_tools":
            continue
        if family_filter and definition.family.casefold() != family_filter:
            continue
        score = 0
        if normalized and normalized in definition.name.casefold():
            score += 8
        if normalized and normalized in definition.description.casefold():
            score += 6
        if normalized and normalized in definition.family.casefold():
            score += 4
        compact_description = _compact_tool_intent(definition.description)
        if compact_normalized and compact_normalized in compact_description:
            score += 4
        for keyword in definition.keywords:
            folded = keyword.casefold()
            if folded in normalized:
                score += 5
            elif _keyword_matches(normalized, keyword):
                score += 4
            elif normalized and normalized in folded:
                score += 3
        # A requested family is already a meaningful match; otherwise omit
        # zero-score noise so discovery does not become a catalog dump.
        if score > 0 or family_filter:
            scored.append((score, definition.name, definition))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [definition for _score, _name, definition in scored[:limit]]


def select_dialogue_message_segment_types(
    text: str | None,
    *,
    has_target_mentions: bool = False,
    has_reply: bool = False,
    has_media: bool = False,
) -> frozenset[str]:
    """Select the smallest outbound segment vocabulary useful this turn."""

    normalized = str(text or "").strip().casefold()
    selected: set[str] = {"text"}
    if has_reply or "回复" in normalized or "引用" in normalized:
        selected.add("reply")
    if has_reply:
        # P6: reply threads may naturally use one reaction instead of a
        # redundant acknowledgement sentence. Actual reaction_id still has to
        # come from search_reactions and outbound validation remains unchanged.
        selected.add("reaction")
    if has_target_mentions or "艾特" in normalized:
        selected.add("at")
    if "表情包" in normalized or "reaction" in normalized or any(
        hint in normalized for hint in ("无语", "吃瓜", "震惊")
    ):
        selected.add("reaction")
    elif "表情" in normalized:
        selected.add("face")
    if "图片" in normalized or "发图" in normalized:
        selected.add("image")
    if "语音" in normalized:
        selected.add("record")
    if "视频" in normalized:
        selected.add("video")
    if "骰子" in normalized:
        selected.add("dice")
    if "猜拳" in normalized:
        selected.add("rps")
    if "戳一戳" in normalized or "poke" in normalized:
        selected.add("poke")
    if "分享" in normalized or "链接" in normalized:
        selected.add("share")
    if "名片" in normalized:
        selected.add("contact")
    if "位置" in normalized or "定位" in normalized:
        selected.add("location")
    if "音乐" in normalized or "歌曲" in normalized:
        selected.add("music")
    if has_media and any(
        hint in normalized for hint in ("原图", "这张图", "这图片", "发回来")
    ):
        selected.add("image")
    return frozenset(selected)


def dialogue_tool_round_limit(tool_names: frozenset[str] | set[str]) -> int:
    """Return a bounded LLM round budget for the selected tool bundle."""

    names = frozenset(tool_names)
    if not names:
        return 1
    if names <= MESSAGE_SEND_TOOLS:
        return 2
    if "discover_tools" in names:
        # discover -> execute -> optional supporting tool -> final answer
        return MAX_TOOL_ROUNDS
    if names & CONTROLLED_TOOLS:
        return min(MAX_TOOL_ROUNDS, 3)
    non_send = names - MESSAGE_SEND_TOOLS
    if len(non_send) <= 3:
        return 2
    return min(MAX_TOOL_ROUNDS, 3)


__all__ = [
    "MAX_TOOL_ROUNDS",
    "dialogue_tool_round_limit",
    "rank_discoverable_tools",
    "select_dialogue_message_segment_types",
    "select_dialogue_tool_names",
]
