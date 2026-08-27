"""NoneBot2 多轮命令的轻量交互约定。

这里只提供选择、取消、开关值和确认等无业务状态 helper；具体流程仍由各命令
自己的 ``got``/``reject_arg`` 处理器维护，避免形成跨插件的万能状态机。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nonebot.matcher import Matcher

EXIT_WORDS = frozenset({"取消", "退出", "0", "q", "quit", "cancel"})
BACK_WORDS = frozenset({"返回", "back", "b"})
MENU_WORDS = frozenset({"菜单", "重新显示", "重显", "menu", "m"})
CANCEL_WORDS = EXIT_WORDS | BACK_WORDS


class SessionIntent(Enum):
    """多轮输入中所有命令共享的生命周期意图。"""

    INPUT = "input"
    BACK = "back"
    EXIT = "exit"
    MENU = "menu"
    NEW_COMMAND = "new_command"


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


def is_menu(value: object) -> bool:
    """Whether the user wants the current menu rendered again."""

    return normalize_session_text(value) in MENU_WORDS


def is_new_command(value: object, *, command_starts: tuple[str, ...] = ("/",)) -> bool:
    """识别应交还给 NoneBot 命令路由的新命令输入。"""

    text = str(value or "").lstrip()
    return any(
        text.startswith(start) and len(text) > len(start) for start in command_starts
    )


def resolve_session_intent(value: object) -> SessionIntent:
    """按退出、返回、菜单、新命令的优先级解析会话输入。"""

    if is_exit(value):
        return SessionIntent.EXIT
    if is_back(value):
        return SessionIntent.BACK
    if is_menu(value):
        return SessionIntent.MENU
    if is_new_command(value):
        return SessionIntent.NEW_COMMAND
    return SessionIntent.INPUT


async def pass_through_new_command(matcher: Matcher, value: object) -> None:
    """结束当前临时会话且解除 block，让同一事件继续匹配新命令。"""

    if not is_new_command(value):
        return
    matcher.block = False
    await matcher.finish()


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
    "MENU_WORDS",
    "SessionChoice",
    "SessionIntent",
    "confirmation_matches",
    "format_change_preview",
    "is_back",
    "is_cancel",
    "is_exit",
    "is_menu",
    "is_new_command",
    "normalize_session_text",
    "parse_toggle",
    "pass_through_new_command",
    "resolve_choice",
    "resolve_session_intent",
]
