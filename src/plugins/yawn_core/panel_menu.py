"""管理面板的视图状态与纯菜单定义。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from .data_models.chat_message import ChatMessage
    from .data_models.chat_session import ChatSession
    from .panel_data import AdminPanelData, GroupListItem
    from .ui.panel_renderer import PersonalPanelView

_PREVIEW_CHAR_LIMIT = 80


class PanelMode(Enum):
    GROUP = "group"
    PRIVATE = "private"


class PanelView(Enum):
    MAIN = "main"
    GROUPS = "groups"
    GROUP_DETAIL = "group_detail"
    CHAT_LIST = "chat_list"
    CHAT_DETAIL = "chat_detail"
    USER_FEATURE = "user_feature"


@dataclass(slots=True)
class PanelFlow:
    user_id: int
    mode: PanelMode
    view: PanelView = PanelView.MAIN
    group_id: int | None = None
    group_name: str | None = None
    groups: tuple[GroupListItem, ...] = ()
    selected_group: GroupListItem | None = None
    sessions: list[ChatSession] = field(default_factory=list)
    current_session: ChatSession | None = None


@dataclass(slots=True)
class AdminFlow:
    group_id: int
    group_name: str | None
    view: PanelView = PanelView.MAIN
    target_user_id: int | None = None


def format_time(value: datetime | None) -> str:
    return f"{value:%Y-%m-%d %H:%M}" if value else "暂无"


def personal_panel_text(view: PersonalPanelView) -> str:
    lines = [f"═══ 个人面板 · {view.subtitle} ═══", f"昵称: {view.nickname}"]
    lines.extend(f"{stat.label}: {stat.value}" for stat in view.stats)
    if view.features:
        lines.append("─── 功能状态 ───")
        lines.extend(
            f"  {index}. {'✓' if feature.enabled else '✗'} "
            f"{feature.name}（{feature.source}）"
            for index, feature in enumerate(view.features, start=1)
        )
    lines.extend(("──────────────", *view.actions))
    return "\n".join(lines)


def group_list_text(groups: tuple[GroupListItem, ...]) -> str:
    lines = ["═══ 我的群聊 ═══"]
    for index, group in enumerate(groups, start=1):
        tag = "[管理员] " if group.is_admin else ""
        lines.append(
            f"{index}. {tag}{group.group_name or '未知群聊'} ({group.group_id})"
        )
    lines.extend(("──────────────", "输入群序号查看详情 | 返回 上一级 | 菜单 重新显示"))
    return "\n".join(lines)


def group_detail_text(group: GroupListItem) -> str:
    lines = [
        f"═══ {group.group_name or '未知群聊'} ═══",
        f"  群号: {group.group_id}",
        f"  首次加入: {format_time(group.first_seen_at)}",
        f"  群最后活跃: {format_time(group.last_active_at)}",
        f"  我的最后发言: {format_time(group.last_seen_at)}",
    ]
    if group.is_admin:
        lines.append("  身份: 管理员")
    lines.extend(("──────────────", "返回 群列表 | 菜单 重新显示 | 取消 退出"))
    return "\n".join(lines)


def session_list_text(sessions: list[ChatSession]) -> str:
    if not sessions:
        return "  暂无对话记录。"
    lines: list[str] = []
    for index, item in enumerate(sessions, start=1):
        updated_at = item.updated_at or item.created_at
        lines.append(
            f"  {index}. [{item.id}] {item.title or '未命名对话'} "
            f"({updated_at:%m-%d %H:%M})"
        )
    return "\n".join(lines)


def chat_list_text(sessions: list[ChatSession]) -> str:
    return "\n".join(
        (
            "═══ 对话管理 ═══",
            session_list_text(sessions),
            "──────────────",
            "输入序号查看详情 | 删除 <序号> 删除对话",
            "返回 上一级 | 菜单 重新显示 | 取消 退出",
        )
    )


def chat_detail_text(target: ChatSession, messages: list[ChatMessage]) -> str:
    message_lines = message_list_text(messages)
    return "\n".join(
        (
            f"═══ 对话 #{target.id}：{target.title or '未命名'} ═══",
            f"消息数：{len(messages)}",
            message_lines,
            "──────────────",
            "删除消息 <ID> | 返回 对话列表 | 菜单 重新显示 | 取消 退出",
        )
    )


def message_list_text(messages: list[ChatMessage]) -> str:
    message_lines: list[str] = []
    for message in messages:
        preview = message.content[:_PREVIEW_CHAR_LIMIT].replace("\n", " ")
        if len(message.content) > _PREVIEW_CHAR_LIMIT:
            preview += "..."
        role = "我" if message.role == "user" else "AI"
        message_lines.append(
            f"  [{message.id}] {role} ({message.created_at:%m-%d %H:%M}): {preview}"
        )
    return "\n".join(message_lines) if message_lines else "  该对话暂无消息。"


def admin_panel_text(data: AdminPanelData) -> str:
    info = [f"群号: {data.group_id}"]
    if data.member_count is not None:
        info.append(f"成员: {data.member_count}")
    lines = [
        f"═══ 群管理 · {data.group_name or '未知群聊'} ═══",
        " | ".join(info),
        f"已追踪用户: {data.tracked_users}",
        f"群最后活跃: {format_time(data.last_active_at)}",
        "─── 功能开关 ───",
    ]
    lines.extend(
        f"  {index}. [{'开' if enabled else '关'}] {display}"
        for index, (display, enabled) in enumerate(data.features, start=1)
    )
    lines.extend(
        (
            "──────────────",
            "输入功能序号切换开关 | 用户 <QQ号> 管理用户功能",
            "菜单 重新显示 | 取消 退出",
        )
    )
    return "\n".join(lines)


def user_feature_text(
    target_user_id: int,
    statuses: list[tuple[str, str, bool, str]],
) -> str:
    lines = [f"═══ 用户 {target_user_id} 功能管理 ═══"]
    lines.extend(
        f"  {index}. {'✓' if enabled else '✗'} {display}（{source}）"
        for index, (_key, display, enabled, source) in enumerate(statuses, start=1)
    )
    lines.extend(
        (
            "──────────────",
            "开启 <序号> / 关闭 <序号>",
            "返回 群管理面板 | 菜单 重新显示 | 取消 退出",
        )
    )
    return "\n".join(lines)


__all__ = [
    "AdminFlow",
    "PanelFlow",
    "PanelMode",
    "PanelView",
    "admin_panel_text",
    "chat_detail_text",
    "chat_list_text",
    "group_detail_text",
    "group_list_text",
    "message_list_text",
    "personal_panel_text",
    "session_list_text",
    "user_feature_text",
]
