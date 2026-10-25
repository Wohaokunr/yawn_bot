# ruff: noqa: TC003
"""按本轮装载的工具决定模型请求超时、轮次与回合时限。

旧实现所有回合统一 ``max_tokens=800``、30 秒请求超时、120 秒回合时限、5 轮。
``run_python`` 的代码写在工具参数里，800 token 连一段像样的 pandas + matplotlib
都装不下，参数 JSON 被截断后只剩一句“工具参数解析失败”。现在对话请求不再传
``max_tokens``（``llm.NO_OUTPUT_LIMIT``），输出与推理深浅交给推理等级
（``AGENT_DIALOGUE_REASONING_EFFORT``）；这里只按工具放宽时间。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from .prompt_sections import section_text
from .tool_registry import TOOL_BY_NAME
from .tool_router import MAX_TOOL_ROUNDS, SANDBOX_TOOL_ROUNDS

# 不限输出长度后推理可能更久；长参数工具（代码、公告、合并转发）再多给一些。
DEFAULT_COMPLETION_TIMEOUT = 60.0
LONG_ARGUMENT_COMPLETION_TIMEOUT = 90.0
DEFAULT_TURN_SECONDS = 120.0
SANDBOX_TURN_SECONDS = 210.0
# 回合中途才发现沙箱时，从发现那一刻起至少再给这么久。
SANDBOX_EXTENSION_SECONDS = 150.0
LONG_ARGUMENT_TOOLS = frozenset(
    {"run_python", "run_shell", "create_group_announcement", "send_forward"}
)
FINAL_ROUND_NOTE = (
    "工具轮次只剩最后一轮：这一轮不要再调用查询或运行类工具，"
    "根据已有结果直接给出最终回复；没做成的事如实说明原因。"
)
TOOL_MEDIA_NOTE = "以下图片来自刚才的工具查询结果，请结合该工具结果继续回答。"
TRUNCATED_ARGS_HINT = (
    "上一轮输出被服务商截断，工具参数没写完整。把代码写短一些"
    "（去掉注释和示例数据）、分成两次运行，或先把中间结果写进文件再读取。"
)


def _names(tools_or_names: Iterable[Any]) -> set[str]:
    names: set[str] = set()
    for item in tools_or_names:
        if isinstance(item, str):
            names.add(item)
        elif isinstance(item, dict):
            name = str(item.get("function", {}).get("name") or "")
            if name:
                names.add(name)
    return names


def has_sandbox_tools(tools_or_names: Iterable[Any]) -> bool:
    return any(
        TOOL_BY_NAME[name].family == "sandbox"
        for name in _names(tools_or_names)
        if name in TOOL_BY_NAME
    )


def completion_timeout(tools: Iterable[Any]) -> float:
    if _names(tools) & LONG_ARGUMENT_TOOLS:
        return LONG_ARGUMENT_COMPLETION_TIMEOUT
    return DEFAULT_COMPLETION_TIMEOUT


def turn_seconds(tools: Iterable[Any]) -> float:
    """整轮时限：跑代码要给“写代码 → 运行 → 修 → 再运行 → 发图”留时间。"""

    return SANDBOX_TURN_SECONDS if has_sandbox_tools(tools) else DEFAULT_TURN_SECONDS


def discovery_round_limit(
    round_limit: int, rounds_done: int, tool_names: Iterable[Any]
) -> int:
    """discover_tools 装载新工具后的轮次上限：沙箱要多留“运行 → 修 → 再运行”的轮次。"""

    if has_sandbox_tools(tool_names):
        return max(round_limit, min(SANDBOX_TOOL_ROUNDS, rounds_done + 4))
    return max(round_limit, min(MAX_TOOL_ROUNDS, rounds_done + 2))


def discovery_deadline(deadline: float, now: float, tool_names: Iterable[Any]) -> float:
    """中途装载沙箱时顺延回合时限；只会变长，不会变短。"""

    if has_sandbox_tools(tool_names):
        return max(deadline, now + SANDBOX_EXTENSION_SECONDS)
    return deadline


def is_final_round(rounds_done: int, round_limit: int) -> bool:
    return round_limit > 1 and rounds_done == round_limit - 1


def argument_error_payload(
    raw_args: object, exc: Exception, *, finish_reason: str | None
) -> dict[str, Any]:
    """工具参数不是合法 JSON 时给模型的结构化错误，区分“被截断”和“写错了”。"""

    text = str(raw_args or "")
    truncated = finish_reason == "length" or _looks_truncated(text)
    if truncated:
        return {
            "ok": False,
            "error": "工具参数不完整（输出被截断）",
            "error_code": "invalid_args",
            "truncated": True,
            "hint": section_text("tools.truncated_args", TRUNCATED_ARGS_HINT),
        }
    return {
        "ok": False,
        "error": f"工具参数不是合法的 JSON 对象：{str(exc)[:120]}",
        "error_code": "invalid_args",
        "hint": "参数必须是一个 JSON 对象，字符串里的换行和引号要转义",
    }


def final_round_messages() -> list[dict[str, str]]:
    """最后一轮前追加的 system 提示；WebUI 删掉这一段时不追加空消息。"""

    text = section_text("tools.final_round", FINAL_ROUND_NOTE)
    return [{"role": "system", "content": text}] if text else []


def tool_media_note() -> str:
    """工具查询带回图片时，追加的 user 消息里那句说明（可在 WebUI 改写）。"""

    return section_text("tools.media_followup", TOOL_MEDIA_NOTE)


def _looks_truncated(text: str) -> bool:
    stripped = text.rstrip()
    if not stripped.startswith("{"):
        return False
    try:
        json.loads(stripped)
    except ValueError:
        return not stripped.endswith("}")
    return False


__all__ = [
    "DEFAULT_COMPLETION_TIMEOUT",
    "DEFAULT_TURN_SECONDS",
    "FINAL_ROUND_NOTE",
    "LONG_ARGUMENT_COMPLETION_TIMEOUT",
    "LONG_ARGUMENT_TOOLS",
    "SANDBOX_EXTENSION_SECONDS",
    "SANDBOX_TOOL_ROUNDS",
    "SANDBOX_TURN_SECONDS",
    "argument_error_payload",
    "completion_timeout",
    "discovery_deadline",
    "discovery_round_limit",
    "final_round_messages",
    "has_sandbox_tools",
    "is_final_round",
    "tool_media_note",
    "turn_seconds",
]
