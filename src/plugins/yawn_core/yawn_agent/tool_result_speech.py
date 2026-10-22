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
    hint: str | None = None

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
        if self.hint:
            payload["hint"] = self.hint
        return payload


def _action_summary(tool_name: str, result: dict[str, Any]) -> str | None:  # noqa: C901, PLR0911, PLR0912
    """写操作的具体结果说明，让最终回复说得准确而不是泛泛的“成功”。"""

    if tool_name == "create_group_announcement":
        extras = []
        if result.get("pinned"):
            extras.append("已置顶")
        if result.get("confirm_required"):
            extras.append("需要群成员确认")
        suffix = f"（{'，'.join(extras)}）" if extras else ""
        return f"群公告已发布{suffix}"
    if tool_name == "delete_group_notice":
        return "群公告已删除"
    if tool_name == "mute_member":
        until = result.get("until")
        return f"已禁言，解禁时间 {until}" if until else "已禁言"
    if tool_name == "unmute_member":
        return "已解除禁言"
    if tool_name in {"recall_message", "recall_bot_message"}:
        return "消息已撤回"
    if tool_name == "send_file":
        folder = result.get("folder")
        where = f"群文件夹「{folder}」" if folder else "群文件"
        return f"文件「{result.get('name') or '文件'}」已上传到{where}"
    if tool_name == "create_group_folder":
        if result.get("existed"):
            return "同名文件夹本来就存在，没有重复创建"
        return "群文件夹已创建"
    if tool_name == "set_essence_message":
        return "已设为精华消息"
    if tool_name == "remove_essence_message":
        return "已移出精华消息"
    if tool_name == "set_group_card":
        return "群名片已清空" if result.get("cleared") else "群名片已修改"
    if tool_name == "set_special_title":
        return "专属头衔已设置"
    if tool_name == "set_group_name":
        return "群名已修改"
    if tool_name == "find_group_member":
        if not result.get("items"):
            return "没找到匹配的成员；如实说找不到，请对方给 @ 或更准确的昵称"
        if result.get("ambiguous"):
            return "找到多个候选，先向对方确认是哪一位，不要擅自选一个执行管理操作"
    return None


def build_speech_evidence(tool_name: str, payload: dict[str, Any]) -> SpeechEvidence:
    name = str(tool_name or "工具").strip()[:64] or "工具"
    ok = bool(payload.get("ok"))
    if not ok:
        error = str(payload.get("error") or "执行失败").strip()[:160]
        code = str(payload.get("error_code") or "").strip()[:32]
        prefix = f"未成功（{code}）" if code else "未成功"
        hint = str(payload.get("hint") or "").strip()[:160] or None
        return SpeechEvidence(
            tool_name=name, ok=False, summary=f"{prefix}：{error}", hint=hint
        )
    if payload.get("duplicate"):
        return SpeechEvidence(
            tool_name=name,
            ok=True,
            summary="本轮已经执行过同样的操作，复用了第一次的结果；只算一次",
        )

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

    action_summary = _action_summary(name, result) if isinstance(result, dict) else None
    if delivery_state in {"unknown", "delivery_unknown"}:
        summary = "回执不确定；不能重复执行，也不能断言一定失败"
    elif action_summary:
        summary = action_summary
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
