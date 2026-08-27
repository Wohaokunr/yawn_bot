"""Small, shared wording helpers for command interactions.

Keep this layer intentionally thin: it formats user-facing guidance but owns no
matcher state or business rules.
"""

from __future__ import annotations


def condition_unmet(reason: str, next_step: str) -> str:
    """Explain why an action is unavailable and what the user can do next."""

    return f"{reason.rstrip('。')}。{next_step}"


def command_failure(what: str, reason: str, next_step: str) -> str:
    """统一失败提示为“发生了什么 · 原因 · 下一步”。"""

    return " · ".join(part.strip().rstrip("。") for part in (what, reason, next_step))


def scope_required(action: str, scope: str, next_step: str) -> str:
    """说明命令的会话范围要求。"""

    return command_failure(f"{action}未执行", f"此操作仅支持{scope}", next_step)


def permission_required(action: str, required: str, next_step: str) -> str:
    """说明缺少的身份权限以及可行下一步。"""

    return command_failure(f"{action}未执行", f"需要{required}权限", next_step)


def temporary_failure(action: str, next_step: str) -> str:
    """用于数据库/API 等短暂故障，不暴露内部异常。"""

    return command_failure(f"{action}失败", "服务暂时没有完成请求", next_step)


def invalid_choice(*, valid: str, back: bool = False) -> str:
    """Return a short retry hint without re-rendering a large menu/card."""

    suffix = "，或发送「返回」回上一级" if back else ""
    return f"没有这个选项。请输入 {valid}{suffix}。"


def retry_value(label: str, expected: str) -> str:
    """Describe an invalid value while keeping the user in the same got step."""

    return f"{label}不正确，请输入{expected}。"


def validation_failed(problem: str, expected: str, *, back: bool = True) -> str:
    """说明校验失败原因并保留明确的会话导航。"""

    navigation = "；发送「菜单」重新显示，发送「返回」回上一级" if back else ""
    return f"{problem.rstrip('。')}。{expected}{navigation}。"


__all__ = [
    "command_failure",
    "condition_unmet",
    "invalid_choice",
    "permission_required",
    "retry_value",
    "scope_required",
    "temporary_failure",
    "validation_failed",
]
