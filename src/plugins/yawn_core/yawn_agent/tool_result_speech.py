"""Rules for turning structured tool results into natural group-chat speech."""

from __future__ import annotations

from typing import Any

TOOL_RESULT_SPEECH_INSTRUCTION = (
    "工具返回是后台事实，不是可以原样发送给群友的话。"
    "看到 role=tool 后先判断 ok；成功只提用户真正关心的结果，"
    "失败只说明可公开的失败原因和必要下一步。"
    "不要照抄 JSON、字段名、布尔值、内部 outcome/delivery_state、"
    "权限级别、trace、路径或协议细节。"
    "列表结果先概括数量，再按用户问题挑最相关项；"
    "除非用户明确要求，不要机械倾倒全部记录。"
    "写操作只有工具明确成功后才能用完成时；"
    "ok=false、超时或不确定状态不能说“已经完成”。"
)


def tool_result_speech_hint(tool_name: str, payload: dict[str, Any]) -> str:
    """Produce a compact deterministic hint for tests/debugging and future adapters."""

    name = str(tool_name or "工具").strip()[:64] or "工具"
    ok = bool(payload.get("ok"))
    if not ok:
        error = str(payload.get("error") or "执行失败").strip()[:160]
        return f"{name} 未成功：{error}。只说明可公开原因，不声称动作已完成。"
    result = payload.get("result")
    if isinstance(result, dict):
        items = result.get("items")
        if isinstance(items, list):
            return (
                f"{name} 成功，返回 {len(items)} 项；"
                "按用户问题筛选后自然表述。"
            )
        if result.get("count") is not None:
            return f"{name} 成功，共 {result.get('count')} 项；不要照抄结构化字段。"
        if result.get("delivery_state") == "unknown":
            return f"{name} 回执不确定；不能重复发送，也不能断言 QQ 一定未收到。"
    if isinstance(result, list):
        return f"{name} 成功，返回 {len(result)} 项；先概括，再挑相关项。"
    return f"{name} 成功；只向用户说明与请求相关的结果。"


__all__ = ["TOOL_RESULT_SPEECH_INSTRUCTION", "tool_result_speech_hint"]
