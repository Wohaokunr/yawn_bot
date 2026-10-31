# ruff: noqa: C901, PLR0912, PLR2004, E501, PLR0911
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
SANDBOX_TOOL_ROUNDS = 7
# 搜索 → 打开一两条核对 → 必要时换关键词再搜 → 作答。
WEB_TOOL_ROUNDS = 6

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


# 模型常把工具包写成中文名、英文复数或 OneBot 风格的叫法；这里统一归一到注册表里
# 的 family 标识。未知名字返回 None，由调用方退回按任务搜索，而不是过滤成空结果。
_FAMILY_ALIASES: dict[str, tuple[str, ...]] = {
    "notice": ("公告", "群公告", "announcement", "announcements", "notices", "group_notice", "group_notices", "bulletin"),
    "file": ("文件", "群文件", "files", "group_file", "group_files", "upload"),
    "moderation": ("管理", "群管", "禁言", "撤回", "admin", "mute", "recall", "moderate"),
    "member": ("成员", "群成员", "members", "group_member", "group_members", "card", "title"),
    "essence": ("精华", "精华消息", "essences", "essence_message"),
    "history": ("历史", "聊天记录", "历史消息", "message_history", "messages"),
    "memory": ("记忆", "memories", "remember"),
    "profile": ("画像", "人物画像", "关系", "profiles", "relation", "relations"),
    "reaction": ("表情", "表情包", "reactions", "sticker", "stickers", "emoji"),
    "group": ("群", "群资料", "群信息", "group_info", "honor"),
    "message": ("消息", "发消息", "转发", "send", "forward"),
    "web": ("网页", "网页搜索", "联网", "联网搜索", "上网", "搜索", "搜一下", "网上", "web_search", "search", "internet", "browse", "browser", "fetch", "news", "新闻"),
    "checkin": ("签到", "打卡", "积分", "连签", "好感", "好感度", "check_in", "checkins", "sign_in", "signin", "affinity"),
    "schedule": ("定时", "定时任务", "提醒", "闹钟", "日程", "计划任务", "schedule", "schedules", "reminder", "reminders", "timer", "cron", "scheduled_task", "scheduled_tasks"),
    "sandbox": ("沙箱", "代码沙箱", "虚拟机", "代码", "python", "编程", "计算", "画图", "绘图", "图表", "数据分析", "作曲", "内置电脑", "电脑", "附件", "inbox", "code", "run_code", "python_sandbox", "vm"),
}
_FAMILY_LOOKUP: dict[str, str] = {
    alias.casefold(): family
    for family, aliases in _FAMILY_ALIASES.items()
    for alias in (family, *aliases)
}


def normalize_tool_family(value: str | None) -> str | None:
    """把模型给出的工具包名字归一到注册表 family；无法识别时返回 None。"""

    folded = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
    if not folded:
        return None
    if folded in _FAMILY_LOOKUP:
        return _FAMILY_LOOKUP[folded]
    if folded.endswith("s") and folded[:-1] in _FAMILY_LOOKUP:
        return _FAMILY_LOOKUP[folded[:-1]]
    return None


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
    raw_family = str(family or "").strip().casefold()
    family_filter = (
        raw_family
        if raw_family in {"message_read", "message-read"}
        else normalize_tool_family(raw_family) or raw_family
    )
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


SANDBOX_SEGMENT_TYPES = frozenset({"image", "record"})
# 发现/预装某个工具包后，本轮最终回复需要能用的消息段。
_FAMILY_SEGMENT_TYPES: dict[str, frozenset[str]] = {
    "sandbox": SANDBOX_SEGMENT_TYPES,
    "reaction": frozenset({"reaction", "face"}),
}


def discovery_segment_types(tool_names: frozenset[str] | set[str]) -> frozenset[str]:
    """装载了这些工具时，本轮额外开放的消息段（如沙箱产物要用 image/record 发）。"""

    selected: set[str] = set()
    for name in tool_names:
        definition = TOOL_BY_NAME.get(name)
        if definition is not None:
            selected |= _FAMILY_SEGMENT_TYPES.get(definition.family, frozenset())
    return frozenset(selected)


def select_dialogue_message_segment_types(
    text: str | None = None,
    *,
    has_target_mentions: bool = False,
    has_reply: bool = False,
    has_media: bool = False,
    planned: frozenset[str] | set[str] | None = None,
) -> frozenset[str]:
    """本轮 send_message 可用的最小消息段集合。

    只看结构信号（回复、@ 了别人、带附件）和工具路由模型给出的 ``planned``；
    不再按关键词猜用户想要什么段。``text`` 参数保留给旧调用方，不参与判断。
    """

    del text
    selected: set[str] = {"text"}
    if has_reply:
        # 回复线程里一个表情回应往往比一句“收到”更自然；reaction_id 仍须来自
        # search_reactions，发送时 outbound 照常校验。
        selected.update({"reply", "reaction"})
    if has_target_mentions:
        selected.add("at")
    if has_media:
        selected.add("image")
    if planned:
        selected.update(planned)
    return frozenset(selected)


def dialogue_tool_round_limit(tool_names: frozenset[str] | set[str]) -> int:
    """Return a bounded LLM round budget for the selected tool bundle."""

    names = frozenset(tool_names)
    if not names:
        return 1
    if any(
        TOOL_BY_NAME[name].family == "sandbox" for name in names if name in TOOL_BY_NAME
    ):
        # 写代码 → 运行 → 报错修正 → 再运行 → 发图/作答，比普通工具多要几轮。
        return SANDBOX_TOOL_ROUNDS
    if any(
        TOOL_BY_NAME[name].family == "web" for name in names if name in TOOL_BY_NAME
    ):
        return WEB_TOOL_ROUNDS
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
    "SANDBOX_SEGMENT_TYPES",
    "SANDBOX_TOOL_ROUNDS",
    "WEB_TOOL_ROUNDS",
    "dialogue_tool_round_limit",
    "discovery_segment_types",
    "has_explicit_critical_intent",
    "normalize_tool_family",
    "rank_discoverable_tools",
    "select_critical_tool_names",
    "select_dialogue_message_segment_types",
    "select_dialogue_tool_names",
]
