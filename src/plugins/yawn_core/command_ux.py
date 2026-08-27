"""Small, shared wording helpers for command interactions.

Keep this layer intentionally thin: it formats user-facing guidance but owns no
matcher state or business rules.
"""

from __future__ import annotations


def condition_unmet(reason: str, next_step: str) -> str:
    """Explain why an action is unavailable and what the user can do next."""

    return f"{reason.rstrip('。')}。{next_step}"


def invalid_choice(*, valid: str, back: bool = False) -> str:
    """Return a short retry hint without re-rendering a large menu/card."""

    suffix = "，或发送「返回」回上一级" if back else ""
    return f"没有这个选项。请输入 {valid}{suffix}。"


def retry_value(label: str, expected: str) -> str:
    """Describe an invalid value while keeping the user in the same got step."""

    return f"{label}不正确，请输入{expected}。"


__all__ = ["condition_unmet", "invalid_choice", "retry_value"]
