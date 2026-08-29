# ruff: noqa: PLR0913,PLR2004
"""Agent Prompt 上下文的近似 token 预算与组件装箱。

这里故意不依赖某一家模型 tokenizer：路由可能在 OpenAI-compatible Provider 间切换。
估算器对 CJK 按单字、ASCII 单词按约 4 字符/token 保守估算，并允许通过环境变量
为具体模型声明 context window。预算轨迹只用于诊断，不能注入 Prompt。
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

_DEFAULT_CONTEXT_WINDOW = 16_384
_SAFETY_RESERVE = 512
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[^\sA-Za-z0-9_]")


@dataclass(frozen=True, slots=True)
class ContextBudget:
    model: str
    context_window: int
    completion_reserve: int
    prompt_limit: int
    context_limit: int
    history_limit: int
    memory_limit: int
    members_limit: int
    relations_limit: int


@dataclass(frozen=True, slots=True)
class ContextPack:
    messages: list[dict[str, Any]]
    members: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    relations: list[str]
    budget: ContextBudget
    trace: list[dict[str, Any]]


def estimate_tokens(value: Any) -> int:
    """跨 Provider 的稳定近似 token 估算；偏保守，适合做硬预算而非计费。"""

    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if not text:
        return 0
    cjk_count = len(_CJK_RE.findall(text))
    ascii_text = _CJK_RE.sub(" ", text)
    ascii_tokens = 0
    for token in _ASCII_TOKEN_RE.findall(ascii_text):
        if token.isascii() and token.replace("_", "").isalnum():
            ascii_tokens += max(1, math.ceil(len(token) / 4))
        else:
            ascii_tokens += 1
    return max(1, cjk_count + ascii_tokens)


def _model_windows() -> dict[str, int]:
    raw = os.environ.get("AGENT_MODEL_CONTEXT_WINDOWS", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    result: dict[str, int] = {}
    for key, value in parsed.items():
        try:
            window = int(value)
        except (TypeError, ValueError):
            continue
        if window >= 4096:
            result[str(key).strip().casefold()] = window
    return result


def model_context_window(model: str | None) -> int:
    default_raw = os.environ.get("AGENT_DEFAULT_CONTEXT_WINDOW", "")
    try:
        default = int(default_raw) if default_raw else _DEFAULT_CONTEXT_WINDOW
    except ValueError:
        default = _DEFAULT_CONTEXT_WINDOW
    default = max(4096, default)
    needle = str(model or "").strip().casefold()
    windows = _model_windows()
    if not needle:
        return default
    if needle in windows:
        return windows[needle]
    # 允许配置如 "gpt-5" 匹配 "openai/gpt-5.6"，优先最长键。
    matches = [(key, value) for key, value in windows.items() if key and key in needle]
    if matches:
        return max(matches, key=lambda item: len(item[0]))[1]
    return default


def build_context_budget(
    *,
    model: str | None = None,
    completion_reserve: int = 2048,
    target_context_limit: int | None = None,
) -> ContextBudget:
    window = model_context_window(model)
    reserve = max(256, min(int(completion_reserve), max(256, window // 2)))
    prompt_limit = max(2048, window - reserve - _SAFETY_RESERVE)
    # context 只是完整 Prompt 的一部分；system/persona/tool schema/current_turn
    # 必须留空间。
    default_context_limit = max(1200, min(8000, int(prompt_limit * 0.58)))
    context_limit = (
        max(1600, min(int(target_context_limit), default_context_limit))
        if target_context_limit is not None
        else default_context_limit
    )
    history = max(500, int(context_limit * 0.34))
    memory = max(500, int(context_limit * 0.38))
    members = max(160, int(context_limit * 0.10))
    relations = max(240, context_limit - history - memory - members)
    return ContextBudget(
        model=str(model or "unknown"),
        context_window=window,
        completion_reserve=reserve,
        prompt_limit=prompt_limit,
        context_limit=context_limit,
        history_limit=history,
        memory_limit=memory,
        members_limit=members,
        relations_limit=relations,
    )


def _fit_mapping(
    item: dict[str, Any], *, budget: int, text_field: str
) -> dict[str, Any] | None:
    if estimate_tokens(item) <= budget:
        return dict(item)
    raw = str(item.get(text_field) or "")
    if not raw:
        return None
    low, high = 0, len(raw)
    best: dict[str, Any] | None = None
    while low <= high:
        middle = (low + high) // 2
        candidate = dict(item)
        candidate[text_field] = raw[:middle]
        if middle < len(raw):
            candidate[f"{text_field}_truncated"] = True
        if estimate_tokens(candidate) <= budget:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best


def _pack_mappings(
    items: Sequence[dict[str, Any]],
    *,
    budget: int,
    prefer_latest: bool,
    text_field: str,
) -> tuple[list[dict[str, Any]], int, int]:
    indexed = list(enumerate(items))
    scan = list(reversed(indexed)) if prefer_latest else indexed
    kept: list[tuple[int, dict[str, Any]]] = []
    used = 0
    for index, item in scan:
        remaining = budget - used
        if remaining <= 0:
            break
        fitted = _fit_mapping(dict(item), budget=remaining, text_field=text_field)
        if fitted is None:
            continue
        cost = estimate_tokens(fitted)
        if cost <= remaining:
            kept.append((index, fitted))
            used += cost
    kept.sort(key=lambda pair: pair[0])
    return [item for _, item in kept], used, max(0, len(items) - len(kept))


def _pack_strings(
    items: Sequence[str], *, budget: int
) -> tuple[list[str], int, int]:
    kept: list[str] = []
    used = 0
    for raw in items:
        text = str(raw)
        cost = estimate_tokens(text)
        remaining = budget - used
        if remaining <= 0:
            break
        if cost > remaining:
            # 关系行很短；若单行异常长则按字符比例截断。
            ratio = max(0.05, remaining / max(cost, 1))
            text = text[: max(1, int(len(text) * ratio))]
            cost = estimate_tokens(text)
        if cost <= remaining:
            kept.append(text)
            used += cost
    return kept, used, max(0, len(items) - len(kept))


def pack_context(
    *,
    messages: Sequence[dict[str, Any]],
    members: Sequence[dict[str, Any]],
    memories: Sequence[dict[str, Any]],
    relations: Sequence[str],
    model: str | None = None,
    completion_reserve: int = 2048,
    target_context_limit: int | None = None,
) -> ContextPack:
    budget = build_context_budget(
        model=model,
        completion_reserve=completion_reserve,
        target_context_limit=target_context_limit,
    )
    packed_messages, history_used, history_dropped = _pack_mappings(
        messages,
        budget=budget.history_limit,
        prefer_latest=True,
        text_field="text",
    )
    packed_memories, memory_used, memory_dropped = _pack_mappings(
        memories,
        budget=budget.memory_limit,
        prefer_latest=False,
        text_field="content",
    )
    packed_members, members_used, members_dropped = _pack_mappings(
        members,
        budget=budget.members_limit,
        prefer_latest=False,
        text_field="name",
    )
    packed_relations, relations_used, relations_dropped = _pack_strings(
        relations, budget=budget.relations_limit
    )
    total_used = history_used + memory_used + members_used + relations_used
    trace = [
        {
            "model": budget.model,
            "contextWindow": budget.context_window,
            "promptLimit": budget.prompt_limit,
            "completionReserve": budget.completion_reserve,
            "contextLimit": budget.context_limit,
            "usedTokens": total_used,
        },
        {
            "component": "history",
            "budgetTokens": budget.history_limit,
            "usedTokens": history_used,
            "kept": len(packed_messages),
            "dropped": history_dropped,
        },
        {
            "component": "memory",
            "budgetTokens": budget.memory_limit,
            "usedTokens": memory_used,
            "kept": len(packed_memories),
            "dropped": memory_dropped,
        },
        {
            "component": "members",
            "budgetTokens": budget.members_limit,
            "usedTokens": members_used,
            "kept": len(packed_members),
            "dropped": members_dropped,
        },
        {
            "component": "relations",
            "budgetTokens": budget.relations_limit,
            "usedTokens": relations_used,
            "kept": len(packed_relations),
            "dropped": relations_dropped,
        },
    ]
    return ContextPack(
        messages=packed_messages,
        members=packed_members,
        memories=packed_memories,
        relations=packed_relations,
        budget=budget,
        trace=trace,
    )


__all__ = [
    "ContextBudget",
    "ContextPack",
    "build_context_budget",
    "estimate_tokens",
    "model_context_window",
    "pack_context",
]
