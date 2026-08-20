"""群聊 Agent 的稳定提示词前缀和动态上下文尾部。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .persona import canonical_persona

PROMPT_VERSION = "yawn-agent-v3"

_STATIC_RULES = (
    "你是 QQ 群里的自然群友。保持简洁、口语化和尊重上下文。"
    "只使用提供的事实和公开记忆；不泄露私聊、隐私记忆、权限信息或工具内部结果。"
    "不确定时明确说明不确定，不编造群成员经历。工具只能执行当前 schema 中允许的动作。"
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_static_prefix(persona: dict[str, str], tools: list[dict[str, Any]]) -> str:
    tool_payload = sorted(
        [
            {"type": item.get("type"), "function": item.get("function", {})}
            for item in tools
        ],
        key=lambda item: str(item.get("function", {}).get("name", "")),
    )
    return "\n".join(
        (
            f"提示词版本：{PROMPT_VERSION}",
            _STATIC_RULES,
            f"人格：{canonical_persona(persona)}",
            f"工具：{canonical_json(tool_payload)}",
        )
    )


def build_messages(
    *,
    persona: dict[str, str],
    tools: list[dict[str, Any]],
    context: dict[str, Any],
    user_prompt: str,
    media_inputs: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """返回消息和固定前缀指纹；动态 JSON 永远位于用户输入之前。"""

    static = build_static_prefix(persona, tools)
    dynamic = canonical_json(context)
    user_content: str | list[dict[str, Any]] = user_prompt
    if media_inputs:
        user_content = [{"type": "text", "text": user_prompt}, *media_inputs]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": static},
        {"role": "system", "content": f"当前群聊状态：{dynamic}"},
        {"role": "user", "content": user_content},
    ]
    fingerprint = hashlib.sha256(static.encode("utf-8")).hexdigest()
    return messages, fingerprint


def prompt_cache_key(
    *,
    persona: dict[str, str],
    tools: list[dict[str, Any]],
    model: str,
    persona_version: int = 1,
) -> str:
    """Stable key for provider/local prompt-prefix cache instrumentation."""

    payload = {
        "version": PROMPT_VERSION,
        "model": model,
        "persona_version": int(persona_version),
        "static": build_static_prefix(persona, tools),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


__all__ = [
    "PROMPT_VERSION",
    "build_messages",
    "build_static_prefix",
    "canonical_json",
    "prompt_cache_key",
]
