# ruff: noqa: E501
"""私聊对外“只有一个我”：主智能体、后台子智能体、汇报轮、协调轮说出口的话都是同一个人。

这里全是纯函数，不碰数据库和网络：

- ``scrub_internal``：发出前清掉“小助手/子智能体/主助手/任务编号/工具名”这类内部说法；
- ``voice_card``：从人设里压一张短卡片给子智能体，它写进度和追问时用同一个口吻；
- ``dialogue_excerpt``：最近几轮聊天的节选，自动放进任务单，子智能体知道他原话怎么说；
- ``natural_progress``：进度消息的自然说法（没有「标题」书名号前缀）；
- ``HEAVY_TOOLS`` / ``infer_toolsets`` / ``handoff_findings``：派活意愿与自动转后台用。

用户可见文字的每条发送通道都要过 ``scrub_internal``（见 AGENTS.md「私聊 Agent」）。
判断都看结构（标记、附件、工具名），不做关键词判断用户意图。
"""

from __future__ import annotations

import json
import re
from typing import Any

from .speech_format import normalize_qq_text

# 主智能体自己调用时算“重活”的工具：每次都慢、结果长，连着调几次就该交给后台。
HEAVY_TOOLS = frozenset({"web_search", "web_fetch", "run_code", "computer_do"})
VOICE_FIELDS = (
    "name",
    "self_name",
    "calls_members",
    "personality",
    "speaking_style",
    "style",
    "catchphrases",
)
VOICE_LIMIT = 600
EXCERPT_MESSAGES = 8
EXCERPT_CHARS = 1600
EXCERPT_ITEM_CHARS = 300
FINDINGS_CHARS = 2400
FINDING_ITEM_CHARS = 500
NOTICE_PREFIX = "[系统通知"

_INTERNAL_WORDS = r"(?:后台的?)?(?:小助手|子智能体|子\s?agent|sub-?agents?|subagents?)(?:们)?"
_HAND_OVER_RE = re.compile(
    r"(?:派|交|丢|扔|甩|转)(?:给|去给)" + _INTERNAL_WORDS + r"(?:去)?(了)?",
    re.IGNORECASE,
)
_RELAY_RE = re.compile(
    _INTERNAL_WORDS + r"(?:那边)?(?:说|告诉我|汇报|反馈|回复)(?:了|说)?[，,：:]?\s*",
    re.IGNORECASE,
)
_HELPER_RE = re.compile(_INTERNAL_WORDS, re.IGNORECASE)
_MAIN_RE = re.compile(r"主助手|主智能体|主\s?agent|前台的我", re.IGNORECASE)
_TASK_ID_PAREN_RE = re.compile(r"\s?[（(]\s?(?:任务\s?)?t\d{1,4}\s?[）)]")
_TASK_ID_WORD_RE = re.compile(r"(?:这个|那个)?(任务|编号)\s?(?:是\s?)?t\d{1,4}(?![A-Za-z0-9_])\s?")
_TOOL_NAME_RE = re.compile(
    r"(?:用|调用?|通过)?\s?`?(?<![A-Za-z0-9_])(?:delegate_task|delegate_project|report_progress|ask_user|tasks\s(?:message|answer|list|cancel|result))(?![A-Za-z0-9_])`?\s?"
)
_DOUBLE_ME_RE = re.compile(r"我(?:[，,、\s]*我)+")


def _hand_over(match: re.Match[str]) -> str:
    return "安排上了" if match.group(1) else "去"


def scrub_internal(text: str, *, persona_names: tuple[str, ...] = ()) -> str:
    """把内部分工的说法改成“我”；人设名里本身带这些词时不动那部分。"""

    if not text:
        return text
    if any(
        name and _HELPER_RE.search(name) for name in persona_names
    ):  # 人设就叫“小助手”：不替换，避免把名字改坏
        cleaned = text
    else:
        cleaned = _HAND_OVER_RE.sub(_hand_over, text)
        cleaned = _RELAY_RE.sub("", cleaned)
        cleaned = _HELPER_RE.sub("我", cleaned)
    cleaned = _MAIN_RE.sub("我", cleaned)
    cleaned = _TASK_ID_PAREN_RE.sub("", cleaned)
    cleaned = _TASK_ID_WORD_RE.sub(lambda m: "那件事" if m.group(1) == "任务" else "", cleaned)
    cleaned = _TOOL_NAME_RE.sub("", cleaned)
    cleaned = _DOUBLE_ME_RE.sub("我", cleaned)
    return cleaned.replace("让我帮我", "让我").replace("我让我", "我")


def persona_names(persona: dict[str, Any] | None) -> tuple[str, ...]:
    compiled = (persona or {}).get("compiled")
    names = [str((persona or {}).get("name") or "")]
    if isinstance(compiled, dict):
        names.append(str(compiled.get("self_name") or ""))
    return tuple(name for name in names if name)


def voice_card(persona: dict[str, Any] | None) -> dict[str, Any]:
    """人设短卡：名字、自称、称呼、性格、说话风格、口癖。给子智能体写用户可见的句子用。"""

    compiled = (persona or {}).get("compiled")
    if not isinstance(compiled, dict):
        name = str((persona or {}).get("name") or "")
        return {"name": name} if name else {}
    card: dict[str, Any] = {}
    for key in VOICE_FIELDS:
        value = compiled.get(key)
        if not value:
            continue
        if isinstance(value, list):
            value = [str(item)[:20] for item in value[:4]]
        else:
            value = str(value)[:200]
        card[key] = value
    while card and len(json.dumps(card, ensure_ascii=False)) > VOICE_LIMIT:
        # 从最不重要的往前删，名字永远留着。
        for key in reversed(VOICE_FIELDS):
            if key in card and key != "name":
                card.pop(key)
                break
        else:
            break
    return card


def dialogue_excerpt(
    history: list[dict[str, Any]] | None,
    *,
    messages: int = EXCERPT_MESSAGES,
    chars: int = EXCERPT_CHARS,
) -> str:
    """最近几轮聊天的节选：“他：… / 我：…”，从新往旧装到字数上限，系统通知不算。"""

    lines: list[str] = []
    used = 0
    for item in reversed(list(history or [])[-messages * 2 :]):
        role = item.get("role")
        content = item.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            continue
        text = " ".join(content.split())
        if not text or text.startswith(NOTICE_PREFIX):
            continue
        if len(text) > EXCERPT_ITEM_CHARS:
            text = text[:EXCERPT_ITEM_CHARS] + "…"
        line = f"{'他' if role == 'user' else '我'}：{text}"
        if lines and used + len(line) > chars:
            break
        lines.append(line)
        used += len(line)
        if len(lines) >= messages:
            break
    lines.reverse()
    return "\n".join(lines)


def natural_progress(
    note: str,
    *,
    title: str = "",
    step: int | None = None,
    total: int | None = None,
    several: bool = False,
) -> str:
    """进度的自然说法：一件事就只说那句话，同时有好几件事才带上是哪件；计数放句尾。"""

    text = normalize_qq_text(str(note or "")).text.strip()
    if not text:
        return ""
    label = str(title or "").split("·", 1)[0].strip()
    if several and label:
        text = f"{label}那边：{text}"
    if step and total and total > 1 and step <= total:
        text = f"{text}（{step}/{total}）"
    return text


def infer_toolsets(*, computer: bool, has_refs: bool = False) -> list[str]:
    """派活没写 tools 时的默认：联网 + 小工具，电脑可用或有文件要处理时加上电脑。"""

    tools = {"web", "toolbox"}
    if computer or has_refs:
        tools.add("computer")
    return sorted(tools)


def short_title(goal: str, limit: int = 12) -> str:
    text = " ".join(str(goal or "").split())
    for mark in ("，", "。", ",", "；", ";", "\n"):
        head = text.split(mark, 1)[0]
        if head:
            text = head
    return text[:limit] or "帮他办的事"


def handoff_findings(messages: list[dict[str, Any]]) -> str:
    """本轮最后一条 user 之后的工具结果摘要：自动转后台时带过去，不用从头查。"""

    start = 0
    for index, item in enumerate(messages):
        if item.get("role") == "user":
            start = index
    names: dict[str, str] = {}
    parts: list[str] = []
    used = 0
    for item in messages[start:]:
        if item.get("role") == "assistant":
            for call in item.get("tool_calls") or []:
                function = call.get("function") or {}
                names[str(call.get("id"))] = str(function.get("name") or "")
        elif item.get("role") == "tool":
            name = names.get(str(item.get("tool_call_id")), "工具")
            content = " ".join(str(item.get("content") or "").split())
            if len(content) > FINDING_ITEM_CHARS:
                content = content[:FINDING_ITEM_CHARS] + "…"
            line = f"[{name}] {content}"
            if parts and used + len(line) > FINDINGS_CHARS:
                break
            parts.append(line)
            used += len(line)
    return "\n".join(parts)


__all__ = [
    "HEAVY_TOOLS",
    "dialogue_excerpt",
    "handoff_findings",
    "infer_toolsets",
    "natural_progress",
    "persona_names",
    "scrub_internal",
    "short_title",
    "voice_card",
]
