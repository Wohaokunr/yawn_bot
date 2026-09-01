"""Compact speech evidence derived from already-projected tool results."""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class SpeechEvidence:
    tool_name: str
    ok: bool
    summary: str
    delivery_state: str | None = None
    item_count: int | None = None

    def prompt_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tool": self.tool_name,
            "ok": self.ok,
            "summary": self.summary,
        }
        if self.delivery_state:
            payload["delivery_state"] = self.delivery_state
        if self.item_count is not None:
            payload["item_count"] = self.item_count
        return payload


def build_speech_evidence(tool_name: str, payload: dict[str, Any]) -> SpeechEvidence:
    name = str(tool_name or "工具").strip()[:64] or "工具"
    ok = bool(payload.get("ok"))
    if not ok:
        error = str(payload.get("error") or "执行失败").strip()[:160]
        return SpeechEvidence(tool_name=name, ok=False, summary=f"未成功：{error}")

    result = payload.get("result")
    delivery_state: str | None = None
    item_count: int | None = None
    if isinstance(result, dict):
        delivery_state = str(result.get("delivery_state") or "").strip()[:32] or None
        items = result.get("items")
        if isinstance(items, list):
            item_count = len(items)
        elif isinstance(result.get("count"), int):
            item_count = max(int(result["count"]), 0)
    elif isinstance(result, list):
        item_count = len(result)

    if delivery_state in {"unknown", "delivery_unknown"}:
        summary = "回执不确定；不能重复执行，也不能断言一定失败"
    elif item_count is not None:
        summary = f"成功，返回 {item_count} 项；只挑与当前问题有关的信息"
    else:
        summary = "成功；只说明与当前请求相关的结果"
    return SpeechEvidence(
        tool_name=name,
        ok=True,
        summary=summary,
        delivery_state=delivery_state,
        item_count=item_count,
    )


def tool_result_speech_hint(tool_name: str, payload: dict[str, Any]) -> str:
    evidence = build_speech_evidence(tool_name, payload)
    return f"{evidence.tool_name} {evidence.summary}。"


__all__ = [
    "TOOL_RESULT_SPEECH_INSTRUCTION",
    "SpeechEvidence",
    "build_speech_evidence",
    "tool_result_speech_hint",
]
