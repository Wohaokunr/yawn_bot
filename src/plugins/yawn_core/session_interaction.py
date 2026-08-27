"""NoneBot2 多轮命令的轻量交互约定。

这里只提供选择、取消、开关值和确认等无业务状态 helper；具体流程仍由各命令
自己的 ``got``/``reject_arg`` 处理器维护，避免形成跨插件的万能状态机。
"""

from __future__ import annotations

from dataclasses import dataclass

EXIT_WORDS = frozenset({"取消", "退出", "0", "q", "quit", "cancel"})
BACK_WORDS = frozenset({"返回", "back", "b"})
CANCEL_WORDS = EXIT_WORDS | BACK_WORDS


@dataclass(frozen=True, slots=True)
class SessionChoice:
    """一个可用序号、名称或别名选择的会话菜单项。"""

    key: str
    label: str
    aliases: tuple[str, ...] = ()


def normalize_session_text(value: object) -> str:
    """统一会话输入：去首尾空白、压缩中间空白并转小写。"""

    return " ".join(str(value or "").strip().split()).lower()


def is_cancel(value: object) -> bool:
    """Whether a one-level session should cancel/finish."""

    return normalize_session_text(value) in CANCEL_WORDS


def is_back(value: object) -> bool:
    """Whether the user wants to return to the parent view."""

    return normalize_session_text(value) in BACK_WORDS


def is_exit(value: object) -> bool:
    """Whether the user explicitly wants to leave the whole interaction."""

    return normalize_session_text(value) in EXIT_WORDS


def resolve_choice(value: object, choices: tuple[SessionChoice, ...]) -> str | None:
    """按 1-based 序号、显示名、key 或别名解析菜单项。"""

    text = normalize_session_text(value)
    if text.isdigit():
        index = int(text) - 1
        if 0 <= index < len(choices):
            return choices[index].key
    for choice in choices:
        names = (choice.key, choice.label, *choice.aliases)
        if text in {normalize_session_text(name) for name in names}:
            return choice.key
    return None


def parse_toggle(value: object) -> bool | None:
    """解析常见的中英文开关输入；无法识别时返回 ``None``。"""

    text = normalize_session_text(value)
    if text in {"开", "开启", "启用", "是", "on", "true", "yes", "1"}:
        return True
    if text in {"关", "关闭", "禁用", "否", "off", "false", "no", "0"}:
        return False
    return None


def format_change_preview(label: str, before: object, after: object) -> str:
    """生成统一的“修改前 → 修改后”预览。"""

    return f"{label}\n修改前：{before}\n修改后：{after}"


def confirmation_matches(value: object, expected: str) -> bool:
    """危险操作只接受明确的完整确认短语。"""

    return normalize_session_text(value) == normalize_session_text(expected)


__all__ = [
    "BACK_WORDS",
    "CANCEL_WORDS",
    "EXIT_WORDS",
    "SessionChoice",
    "confirmation_matches",
    "format_change_preview",
    "is_back",
    "is_cancel",
    "is_exit",
    "normalize_session_text",
    "parse_toggle",
    "resolve_choice",
]
