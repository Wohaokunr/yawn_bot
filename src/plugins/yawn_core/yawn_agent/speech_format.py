# ruff: noqa: E501,RUF001,PLR2004,RET504
"""QQ 群聊纯文本规范化：确定性、零 AI 成本。

QQ 不渲染 Markdown，模型写出的 ``**重点**``、``### 标题``、表格会原样
显示成符号；助手腔、免责声明、泄漏的工具 JSON 和内部字段名也会让 Bot 一眼
就像机器人。这里只做保守的形式清洗，不改写语义。代码块原样保留。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

FORMAT_DETAILS: dict[str, tuple[str, str]] = {
    "markdown_heading": ("QQ 不渲染 Markdown 标题，已去掉 # 前缀", "info"),
    "markdown_quote": ("QQ 不渲染 Markdown 引用，已去掉 > 前缀", "info"),
    "markdown_emphasis": ("QQ 不渲染粗体/斜体/行内代码标记，已去掉符号", "info"),
    "markdown_list": ("Markdown 列表符号已改为 ·", "info"),
    "markdown_link": ("Markdown 链接已改为“文字 链接”", "info"),
    "markdown_table": ("Markdown 表格已降级为普通行", "info"),
    "markdown_rule": ("Markdown 分隔线已删除", "info"),
    "punctuation_run": ("连续重复标点已压缩", "info"),
    "laughter_run": ("过长的笑声/语气词已压缩", "info"),
    "emoji_run": ("重复 emoji 已压缩", "info"),
    "ai_disclaimer": ("删除了“作为 AI/语言模型”式免责声明", "warning"),
    "helpdesk_closing": ("删除了客服式结尾（希望对你有帮助/随时找我）", "info"),
    "role_label_prefix": ("删除了“回复：/[Bot]”之类的角色标签前缀", "warning"),
    "leaked_tool_json": ("删除了泄漏到正文里的工具 JSON", "warning"),
    "leaked_internal_field": ("删除了含内部字段名的句子", "warning"),
    "leaked_qq_number": ("删除了括号里泄漏的 QQ 号", "info"),
    "duplicate_sentence": ("同一回复里重复的句子只保留一次", "info"),
}

_CODE_FENCE_RE = re.compile(r"(```.*?```)", re.DOTALL)
_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.MULTILINE)
_QUOTE_RE = re.compile(r"^[ \t]{0,3}>[ \t]?", re.MULTILINE)
_BOLD_RES = (
    re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*"),
    re.compile(r"__(?=\S)(.+?)(?<=\S)__"),
    re.compile(r"~~(?=\S)(.+?)(?<=\S)~~"),
)
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?![\s*])([^*\n]+?)(?<![\s*])\*(?![\w*])")
_INLINE_CODE_RE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
_LIST_RE = re.compile(r"^([ \t]*)[-*+][ \t]+(?=\S)", re.MULTILINE)
_LINK_RE = re.compile(r"\[([^\]\n]{1,80})\]\((https?://[^)\s]+)\)")
_RULE_RE = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$\n?", re.MULTILINE)
_TABLE_SEPARATOR_RE = re.compile(r"^[ \t]*\|?(?:[ \t]*:?-{3,}:?[ \t]*\|)+[ \t]*:?-{0,}:?[ \t]*\|?[ \t]*$")
_TABLE_ROW_RE = re.compile(r"^[ \t]*\|(.+)\|[ \t]*$")
_PUNCTUATION_RUNS = (
    (re.compile(r"[！!]{3,}"), "！！"),
    (re.compile(r"[？?]{3,}"), "？？"),
    (re.compile(r"。{2,}"), "…"),
    (re.compile(r"\.{4,}"), "…"),
    (re.compile(r"…{3,}"), "……"),
    (re.compile(r"[~～]{3,}"), "~~"),
)
_LAUGHTER_RE = re.compile(r"([哈嘿呵嘻草])\1{4,}")
_EMOJI_RUN_RE = re.compile("([\\U0001F300-\\U0001FAFF\\u2600-\\u27BF])(?:\\uFE0F)?(?:\\1(?:\\uFE0F)?){2,}")
_AI_DISCLAIMER_RE = re.compile(
    r"(?:^|(?<=[。！？!?\n]))[ \t]*(?:但是?|不过)?[，, ]?作为(?:一[个名款位])?(?:AI|ai|人工智能|语言模型|大语言模型|AI助手|AI 助手|聊天机器人|虚拟助手)[^。！？!?\n]{0,60}[。！？!?]?"
)
_HELPDESK_CLOSING_RES = (
    re.compile(r"[，, ]*希望(?:这些|以上|我的)?(?:内容|回答|信息|解释)?(?:能)?(?:对你|帮到你|帮助到你|对您|帮到您)[^。！？!?\n]{0,10}[。！!～~]*$"),
    re.compile(r"[，, ]*(?:如果)?(?:还)?有(?:任何|什么)?(?:其他)?问题(?:的话)?[，, ]*(?:随时|欢迎)?(?:找我|问我|告诉我|联系我)[^。！？!?\n]{0,6}[。！!～~]*$"),
    re.compile(r"[，, ]*(?:有需要)?随时(?:叫我|找我|告诉我|@我)[^。！？!?\n]{0,6}[。！!～~]*$"),
)
_ROLE_LABEL_RE = re.compile(
    r"^[ \t]*(?:【(?:回复|回答|Bot|bot|机器人)】|\[(?:bot|Bot|BOT|AI|回复)\]|(?:回复|回答|答|Assistant|assistant|AI|Bot|bot)[ \t]*[：:])[ \t]*"
)
_TOOL_JSON_RE = re.compile(
    r"\{[^{}\n]{0,400}\"(?:ok|delivery_state|error_code|speech_evidence|tool_call_id|outcome)\"[ \t]*:[^{}\n]{0,400}\}"
)
_INTERNAL_FIELD_RE = re.compile(
    r"\b(?:current_turn|topic_state|speech_evidence|delivery_state|emotion_state|active_topic|tool_call_id|discover_tools|interaction_plan|segment_types)\b"
)
_QQ_NUMBER_RE = re.compile(r"[ \t]*[（(][ \t]*(?:QQ|qq|Q号|QQ号)?[:：]?[ \t]*\d{5,12}[ \t]*[)）]")
_SENTENCE_RE = re.compile(r"[^。！？!?\n]+[。！？!?]*|\n")
_LEADING_MENTION_RE = re.compile(r"^[ \t]*@[^\s@：:，,]{1,24}[ \t]*[：:，,]?[ \t]*")
PLAIN_MENTION_RE = re.compile(r"(?<![\w@./])@[^\s@：:，,。！？!?]{1,24}")


@dataclass(frozen=True, slots=True)
class FormatResult:
    text: str
    codes: tuple[str, ...]


def _sub(pattern: re.Pattern[str], repl: str, text: str) -> tuple[str, bool]:
    updated = pattern.sub(repl, text)
    return updated, updated != text


def _table_lines(text: str) -> tuple[str, bool]:
    lines = text.split("\n")
    output: list[str] = []
    changed = False
    for line in lines:
        if _TABLE_SEPARATOR_RE.match(line) and "-" in line:
            changed = True
            continue
        row = _TABLE_ROW_RE.match(line)
        if row and row.group(1).count("|") >= 1:
            cells = [cell.strip() for cell in row.group(1).split("|")]
            output.append(" / ".join(cell for cell in cells if cell))
            changed = True
            continue
        output.append(line)
    return "\n".join(output), changed


def _drop_sentences(text: str, pattern: re.Pattern[str]) -> tuple[str, bool]:
    pieces = _SENTENCE_RE.findall(text)
    if not pieces:
        return text, False
    kept = [piece for piece in pieces if piece == "\n" or not pattern.search(piece)]
    remaining = "".join(kept).strip()
    if not remaining or len(kept) == len(pieces):
        return text, False
    return remaining, True


def _dedupe_sentences(text: str) -> tuple[str, bool]:
    pieces = _SENTENCE_RE.findall(text)
    seen: set[str] = set()
    kept: list[str] = []
    changed = False
    for piece in pieces:
        key = re.sub(r"[\s。！？!?，,]+", "", piece)
        if piece != "\n" and len(key) >= 4:
            if key in seen:
                changed = True
                continue
            seen.add(key)
        kept.append(piece)
    return ("".join(kept), True) if changed else (text, False)


def _strip_tool_json(text: str) -> tuple[str, bool]:
    updated, changed = _sub(_TOOL_JSON_RE, "", text)
    if changed:
        updated = re.sub(r"[ \t]{2,}", " ", updated)
    return updated, changed


def _normalize_prose(text: str, codes: list[str]) -> str:
    def apply(code: str, updated: tuple[str, bool]) -> str:
        value, changed = updated
        if changed and code not in codes:
            codes.append(code)
        return value

    text = apply("markdown_table", _table_lines(text))
    text = apply("markdown_rule", _sub(_RULE_RE, "", text))
    text = apply("markdown_heading", _sub(_HEADING_RE, "", text))
    text = apply("markdown_quote", _sub(_QUOTE_RE, "", text))
    text = apply("markdown_link", _sub(_LINK_RE, r"\1 \2", text))
    for pattern in _BOLD_RES:
        text = apply("markdown_emphasis", _sub(pattern, r"\1", text))
    text = apply("markdown_emphasis", _sub(_ITALIC_RE, r"\1", text))
    text = apply("markdown_emphasis", _sub(_INLINE_CODE_RE, r"\1", text))
    text = apply("markdown_list", _sub(_LIST_RE, r"\1· ", text))
    for pattern, replacement in _PUNCTUATION_RUNS:
        text = apply("punctuation_run", _sub(pattern, replacement, text))
    text = apply("laughter_run", _sub(_LAUGHTER_RE, r"\1\1\1\1", text))
    text = apply("emoji_run", _sub(_EMOJI_RUN_RE, r"\1\1", text))
    text = apply("leaked_tool_json", _strip_tool_json(text))
    text = apply("leaked_qq_number", _sub(_QQ_NUMBER_RE, "", text))
    return text


def normalize_qq_text(text: str) -> FormatResult:
    """返回清洗后的文本与触发的规则码；没有可保留内容时返回原文。"""

    original = str(text or "")
    if not original.strip():
        return FormatResult(original, ())
    codes: list[str] = []
    parts = _CODE_FENCE_RE.split(original)
    rebuilt = "".join(
        part if part.startswith("```") else _normalize_prose(part, codes)
        for part in parts
    )

    def apply(code: str, updated: tuple[str, bool]) -> None:
        nonlocal rebuilt
        value, changed = updated
        if changed and value.strip():
            rebuilt = value
            if code not in codes:
                codes.append(code)

    if "```" not in rebuilt:
        apply("role_label_prefix", _sub(_ROLE_LABEL_RE, "", rebuilt))
        apply("ai_disclaimer", _sub(_AI_DISCLAIMER_RE, "", rebuilt))
        stripped = rebuilt.rstrip()
        for pattern in _HELPDESK_CLOSING_RES:
            updated, changed = _sub(pattern, "", stripped)
            if changed and updated.strip():
                stripped = updated
                if "helpdesk_closing" not in codes:
                    codes.append("helpdesk_closing")
        rebuilt = stripped
        apply("leaked_internal_field", _drop_sentences(rebuilt, _INTERNAL_FIELD_RE))
        apply("duplicate_sentence", _dedupe_sentences(rebuilt))
    rebuilt = re.sub(r"[ \t]+\n", "\n", rebuilt)
    rebuilt = re.sub(r"\n{3,}", "\n\n", rebuilt).strip()
    if not rebuilt:
        return FormatResult(original, ())
    return FormatResult(rebuilt, tuple(codes))


def strip_leading_mention(text: str) -> tuple[str, bool]:
    """已经引用回复时，正文开头的“@某某”是多余的。"""

    updated = _LEADING_MENTION_RE.sub("", text, count=1)
    if updated != text and updated.strip():
        return updated, True
    return text, False


def has_plain_text_mention(text: str) -> bool:
    return bool(PLAIN_MENTION_RE.search(str(text or "")))


def strip_announcement_markup(text: object) -> str:
    """公告正文：去 CQ 码、Markdown 符号，压缩多余空行。"""

    cleaned = re.sub(r"\[CQ:[^\]]*\]", "", str(text or ""))
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _normalize_prose(cleaned, [])
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


__all__ = [
    "FORMAT_DETAILS",
    "PLAIN_MENTION_RE",
    "FormatResult",
    "has_plain_text_mention",
    "normalize_qq_text",
    "strip_announcement_markup",
    "strip_leading_mention",
]
