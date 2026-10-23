# ruff: noqa: C901,PLR0912,PLR2004,E501
"""Deterministic, zero-AI-cost routing from one dialogue turn to tool schemas."""

from __future__ import annotations

from .tool_registry import (
    CONTROLLED_TOOLS,
    CORE_DIALOGUE_TOOL_NAMES,
    CRITICAL_TOOLS,
    MESSAGE_SEND_TOOLS,
    TOOL_BY_NAME,
    TOOL_PERMISSION_CRITICAL,
    ToolDefinition,
)

# discover → 查目标（如 find_group_member）→ 执行 → 可能的二次发现 → 最终回答。
MAX_TOOL_ROUNDS = 5

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


def _bigrams(text: str) -> set[str]:
    compact = "".join(char for char in text if not char.isspace())
    return {compact[index : index + 2] for index in range(len(compact) - 1)}


def _fuzzy_overlap(query: str, keyword: str) -> float:
    """中文二元组重叠率：容忍“置顶一下公告”与“置顶公告”这类语序/插词差异。"""

    keyword_grams = _bigrams(keyword)
    if len(keyword_grams) < 2:
        return 0.0
    return len(keyword_grams & _bigrams(query)) / len(keyword_grams)


# 意图提示：当前消息里出现这些词时，在易变层提示模型优先发现对应工具包。
# 只是提示，不注入 schema，也绝不涉及 critical 工具。
_FAMILY_HINT_WORDS: dict[str, tuple[str, ...]] = {
    "notice": ("公告",),
    "file": ("文件", "群文件", "资料包", "文件夹"),
    "moderation": ("禁言", "解禁", "撤回", "闭嘴"),
    "member": ("管理员", "群主", "群名片", "头衔", "谁是"),
    "essence": ("精华", "加精"),
    "history": ("聊天记录", "刚才说", "说了什么", "之前的消息"),
    "memory": ("记得", "还记不记得", "上次", "记住我", "别忘了", "忘掉我", "别记"),
    "reaction": ("表情包",),
}


def suggest_tool_families(text: str | None, *, limit: int = 3) -> tuple[str, ...]:
    """零成本按当前消息意图给出建议发现的工具包（最多 ``limit`` 个）。"""

    normalized = str(text or "").casefold()
    if not normalized.strip():
        return ()
    families = [
        family
        for family, words in _FAMILY_HINT_WORDS.items()
        if any(word in normalized for word in words)
    ]
    return tuple(families[:limit])


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


def has_explicit_critical_intent(name: str, text: str | None) -> bool:
    """当前触发消息（调用者本人正文）是否显式提出了该 critical 工具的动作。

    ``text`` 只能来自本轮触发消息的用户正文（``NormalizedMessage.intent_text``），
    不能是历史、引用链或模型生成内容。非 critical 工具恒返回 False。
    """

    definition = TOOL_BY_NAME.get(name)
    if definition is None or name not in CRITICAL_TOOLS:
        return False
    normalized = str(text or "").strip().casefold()
    if not normalized:
        return False
    return any(
        keyword.casefold() in normalized for keyword in definition.intent_keywords
    )


def select_critical_tool_names(text: str | None) -> frozenset[str]:
    """按当前触发消息的显式动作意图选择可装载的 critical 工具。

    critical 工具永远不能经 ``discover_tools`` 发现；只有当前用户消息里直接
    出现对应动作词时，才把它们作为候选交给调用方继续做权限/白名单过滤。
    """

    return frozenset(
        name for name in CRITICAL_TOOLS if has_explicit_critical_intent(name, text)
    )


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
        if (
            not definition.discoverable
            or definition.name == "discover_tools"
            or definition.permission_level == TOOL_PERMISSION_CRITICAL
        ):
            continue
        if family_filter:
            accepted_families = (
                {"message", "history"}
                if family_filter in {"message", "message_read", "message-read"}
                else {family_filter}
            )
            if definition.family.casefold() not in accepted_families:
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
            elif _fuzzy_overlap(normalized, folded) >= 0.6:
                score += 2
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
    if has_reply or any(
        hint in normalized for hint in ("回复", "引用", "回他", "回她", "回一下", "回这条")
    ):
        selected.add("reply")
    if has_reply:
        # P6: reply threads may naturally use one reaction instead of a
        # redundant acknowledgement sentence. Actual reaction_id still has to
        # come from search_reactions and outbound validation remains unchanged.
        selected.add("reaction")
    if has_target_mentions or any(
        hint in normalized
        for hint in ("艾特", "提醒一下", "提醒他", "提醒她", "叫他", "叫她", "叫一下", "cue", "喊他", "喊她", "@一下")
    ):
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
    "has_explicit_critical_intent",
    "rank_discoverable_tools",
    "select_critical_tool_names",
    "select_dialogue_message_segment_types",
    "select_dialogue_tool_names",
    "suggest_tool_families",
]
