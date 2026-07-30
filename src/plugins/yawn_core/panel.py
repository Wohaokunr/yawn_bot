"""统一管理面板模块。

群聊：/面板 → 个人面板（群内数据），/群管理 → 群管理面板
私聊：/面板 → 个人面板（全量数据 + 群聊列表 + 对话管理）

交互模式：got + reject_arg 循环，matcher.state["view"] 状态机路由。
"""

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from nonebot import get_driver, logger
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageEvent,
)
from nonebot.matcher import Matcher
from nonebot.params import ArgPlainText, CommandArg
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata, on_command
from nonebot_plugin_orm import async_scoped_session
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from .data_models.bot_user import BotUser
from .data_models.chat_message import ChatMessage
from .data_models.chat_session import ChatSession
from .data_models.checkin_user import CheckinUser
from .data_models.global_user_feature import GlobalUserFeature
from .data_models.group_feature import GroupFeature
from .data_models.user_feature import UserFeature
from .data_models.user_group import UserGroup
from .permission import (
    get_feature_display,
    get_user_feature_status,
    is_group_admin,
    list_features,
    resolve_feature_key,
)

if TYPE_CHECKING:
    from .data_models.bot_group import BotGroup

__plugin_meta__ = PluginMetadata(
    name="管理面板",
    description="个人面板与群管理面板",
    usage="发送 /面板 查看个人信息",
    extra={
        "commands": [
            {
                "name": "面板",
                "aliases": ["个人面板", "我的面板"],
                "description": "查看个人信息面板",
                "feature": None,
                "scope": "all",
                "superuser": False,
            },
            {
                "name": "群管理",
                "aliases": ["群管理面板"],
                "description": "群功能开关管理（需群管/超管）",
                "feature": None,
                "scope": "group",
                "superuser": False,
                "admin": True,
            },
            {
                "name": "全局群功能",
                "aliases": [],
                "description": "管理任意群的功能开关",
                "feature": None,
                "scope": "all",
                "superuser": True,
            },
            {
                "name": "全局用户功能",
                "aliases": [],
                "description": "管理任意用户的功能开关",
                "feature": None,
                "scope": "all",
                "superuser": True,
            },
            {
                "name": "权限查询",
                "aliases": [],
                "description": "查询用户权限状态",
                "feature": None,
                "scope": "all",
                "superuser": True,
            },
            {
                "name": "查看用户对话",
                "aliases": [],
                "description": "查看指定用户的对话记录",
                "feature": None,
                "scope": "all",
                "superuser": True,
            },
            {
                "name": "删除用户对话",
                "aliases": [],
                "description": "删除指定用户的对话或消息",
                "feature": None,
                "scope": "all",
                "superuser": True,
            },
        ],
    },
)

logger.info("统一管理面板模块已加载")

# ── 常量 ────────────────────────────────────────────────

_PREVIEW_CHAR_LIMIT = 80
_MIN_DELETE_PARTS = 2
_MIN_ADMIN_DELETE_PARTS = 3
_MIN_GLOBAL_CMD_PARTS = 3

# ── 命令匹配器 ────────────────────────────────────────────

# 个人面板（群聊 + 私聊）
panel_cmd = on_command(
    "面板",
    aliases={"个人面板", "我的面板"},
    priority=5,
    block=True,
)

# 群管理面板（仅群聊，管理员/超管）
group_admin_cmd = on_command(
    "群管理",
    aliases={"群管理面板"},
    priority=3,
    block=True,
)

# 超级管理员命令
global_group_feature_cmd = on_command(
    "全局群功能", permission=SUPERUSER, priority=2, block=True
)
global_user_feature_cmd = on_command(
    "全局用户功能", permission=SUPERUSER, priority=2, block=True
)
perm_query_cmd = on_command(
    "权限查询", permission=SUPERUSER, priority=2, block=True
)
admin_chat_view_cmd = on_command(
    "查看用户对话", permission=SUPERUSER, priority=2, block=True
)
admin_chat_delete_cmd = on_command(
    "删除用户对话", permission=SUPERUSER, priority=2, block=True
)


# ── 辅助函数 ──────────────────────────────────────────────


def _fmt_time(dt: Optional[datetime]) -> str:
    """格式化时间为字符串。"""
    if dt is None:
        return "暂无"
    return f"{dt:%Y-%m-%d %H:%M}"


async def _get_user_role_in_group(
    bot: Bot,
    group_id: int,
    user_id: int,
) -> str:
    """获取用户在群中的角色，失败时返回 member。"""
    try:
        info = await bot.call_api(
            "get_group_member_info",
            group_id=group_id,
            user_id=user_id,
        )
        return info.get("role", "member")
    except Exception:  # noqa: BLE001
        logger.warning(
            f"获取用户 {user_id} 在群 {group_id} 的角色失败"
        )
        return "member"


async def _get_user_sessions(
    session: async_scoped_session,
    user_id: int,
    group_id: Optional[int] = None,
) -> list[ChatSession]:
    """获取用户的活跃会话列表。"""
    stmt = (
        select(ChatSession)
        .where(
            ChatSession.user_id == user_id,
            ChatSession.group_id == group_id,
            ChatSession.is_deleted == False,  # noqa: E712
        )
        .order_by(ChatSession.updated_at.desc().nullslast())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _get_session_messages(
    session: async_scoped_session,
    session_id: int,
) -> list[ChatMessage]:
    """获取会话中的活跃消息。"""
    stmt = (
        select(ChatMessage)
        .where(
            ChatMessage.session_id == session_id,
            ChatMessage.is_deleted == False,  # noqa: E712
        )
        .order_by(ChatMessage.id.asc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ── 面板构建函数 ──────────────────────────────────────────


async def _build_group_personal_panel(
    session: async_scoped_session,
    user_id: int,
    group_id: int,
    group_name: Optional[str],
) -> str:
    """构建群聊个人面板文本。"""
    lines: list[str] = [
        f"═══ 个人面板 · {group_name or '未知群聊'} ═══"
    ]

    bot_user = await session.get(BotUser, user_id)
    if bot_user:
        lines.append(f"昵称: {bot_user.nickname or '未知'}")
        lines.append(f"好感度: {bot_user.affinity}")

    ug = await session.get(
        UserGroup, (group_id, user_id)
    )
    if ug:
        lines.append(
            f"群好感度: {ug.group_affinity} | "
            f"经验: {ug.exp} | 金币: {ug.coins}"
        )
        lines.append(
            f"首次发言: {_fmt_time(ug.first_seen_at)}"
        )
        lines.append(
            f"最后发言: {_fmt_time(ug.last_seen_at)}"
        )

    # 签到数据
    cu = await session.get(
        CheckinUser, (group_id, user_id)
    )
    if cu:
        lines.append(
            f"签到: 累计{cu.total_days}天 | "
            f"连续{cu.streak_days}天 | 积分{cu.points}"
        )

    # 功能状态
    statuses = await get_user_feature_status(
        user_id, group_id, session
    )
    lines.append("─── 功能状态 ───")
    for i, (_key, display, enabled, source) in enumerate(
        statuses, 1
    ):
        icon = "✓" if enabled else "✗"
        lines.append(
            f"  {i}. {icon} {display}（{source}）"
        )

    lines.append("──────────────")
    lines.append("输入「功能 <序号>」查看详情")
    lines.append("输入「取消」退出")
    return "\n".join(lines)


async def _build_private_main_panel(
    session: async_scoped_session,
    user_id: int,
) -> str:
    """构建私聊主菜单面板文本。"""
    lines: list[str] = ["═══ 个人面板 ═══"]

    bot_user = await session.get(BotUser, user_id)
    if bot_user:
        lines.append(
            f"昵称: {bot_user.nickname or '未知'}"
        )
        lines.append(f"全局好感度: {bot_user.affinity}")
        lines.append(
            f"首次互动: "
            f"{_fmt_time(bot_user.first_interaction_at)}"
        )
        lines.append(
            f"最后活跃: "
            f"{_fmt_time(bot_user.last_interaction_at)}"
        )

    # 签到汇总
    total_days = await session.scalar(
        select(func.coalesce(func.sum(CheckinUser.total_days), 0)).where(
            CheckinUser.user_id == user_id
        )
    )
    total_points = await session.scalar(
        select(func.coalesce(func.sum(CheckinUser.points), 0)).where(
            CheckinUser.user_id == user_id
        )
    )
    if total_days or total_points:
        lines.append("─── 签到汇总 ───")
        lines.append(
            f"  总签到天数: {total_days} | "
            f"总积分: {total_points}"
        )

    # 群聊数量
    group_count = await session.scalar(
        select(func.count()).select_from(UserGroup).where(
            UserGroup.user_id == user_id
        )
    )
    # 对话数量
    chat_count = await session.scalar(
        select(func.count()).select_from(ChatSession).where(
            ChatSession.user_id == user_id,
            ChatSession.group_id == None,  # noqa: E711
            ChatSession.is_deleted == False,  # noqa: E712
        )
    )

    lines.append("")
    lines.append(f"1. 我的群聊 ({group_count or 0}个群)")
    lines.append(f"2. 对话管理 ({chat_count or 0}个对话)")
    lines.append("──────────────")
    lines.append("输入序号进入 | 输入「取消」退出")
    return "\n".join(lines)


def _build_group_list_text(
    groups: list[dict],
) -> str:
    """构建群聊编号列表文本。"""
    lines = ["═══ 我的群聊 ═══"]
    for i, g in enumerate(groups, 1):
        tag = "[管理员] " if g["is_admin"] else ""
        name = g["group_name"] or "未知群聊"
        lines.append(
            f"{i}. {tag}{name} ({g['group_id']})"
        )
    lines.append("──────────────")
    lines.append("输入群序号查看详情 | 输入「返回」回主菜单")
    return "\n".join(lines)


def _build_group_detail_text(
    group: dict,
    *,
    is_admin: bool,
) -> str:
    """构建单个群的详情文本（非管理员视角）。"""
    name = group["group_name"] or "未知群聊"
    lines = [
        f"═══ {name} ═══",
        f"  群号: {group['group_id']}",
        f"  首次加入: {_fmt_time(group['first_seen_at'])}",
        f"  群最后活跃: "
        f"{_fmt_time(group['last_active_at'])}",
        f"  我的最后发言: "
        f"{_fmt_time(group['last_seen_at'])}",
    ]
    if is_admin:
        lines.append("  身份: 管理员")
    lines.append("──────────────")
    lines.append("输入「返回」回群列表 | 输入「取消」退出")
    return "\n".join(lines)


def _fmt_session_list(
    sessions: list[ChatSession],
) -> str:
    """格式化会话列表。"""
    if not sessions:
        return "  暂无对话记录。"
    lines: list[str] = []
    for i, s in enumerate(sessions, 1):
        title = s.title or "未命名对话"
        time_str = (
            f"{s.updated_at:%m-%d %H:%M}"
            if s.updated_at
            else f"{s.created_at:%m-%d %H:%M}"
        )
        lines.append(
            f"  {i}. [{s.id}] {title} ({time_str})"
        )
    return "\n".join(lines)


def _fmt_message_list(
    messages: list[ChatMessage],
) -> str:
    """格式化消息列表。"""
    if not messages:
        return "  该对话暂无消息。"
    lines: list[str] = []
    for msg in messages:
        role_label = "我" if msg.role == "user" else "AI"
        preview = (
            msg.content[:_PREVIEW_CHAR_LIMIT]
            .replace("\n", " ")
        )
        if len(msg.content) > _PREVIEW_CHAR_LIMIT:
            preview += "..."
        time_str = f"{msg.created_at:%m-%d %H:%M}"
        lines.append(
            f"  [{msg.id}] {role_label} ({time_str}): "
            f"{preview}"
        )
    return "\n".join(lines)


def _build_chat_list_text(
    sessions: list[ChatSession],
) -> str:
    """构建对话管理列表文本。"""
    lines = ["═══ 对话管理 ═══"]
    lines.append(_fmt_session_list(sessions))
    lines.append("──────────────")
    lines.append("输入序号查看详情")
    lines.append("输入「删除 <序号>」删除对话")
    lines.append("输入「返回」回主菜单 | 「取消」退出")
    return "\n".join(lines)


def _build_chat_detail_text(
    target: ChatSession,
    messages: list[ChatMessage],
) -> str:
    """构建对话详情文本。"""
    lines = [
        f"═══ 对话 #{target.id}："
        f"{target.title or '未命名'} ═══",
        f"消息数：{len(messages)}",
        "",
        _fmt_message_list(messages),
        "",
        "──────────────",
        "输入「删除消息 <ID>」删除指定消息",
        "输入「返回」回对话列表 | 「取消」退出",
    ]
    return "\n".join(lines)


async def _build_admin_panel(
    session: async_scoped_session,
    bot: Bot,
    group_id: int,
    group_name: Optional[str],
) -> str:
    """构建群管理面板文本。"""
    lines: list[str] = [
        f"═══ 群管理 · {group_name or '未知群聊'} ═══"
    ]

    # 群基础信息
    member_count: Optional[int] = None
    try:
        ginfo = await bot.call_api(
            "get_group_info", group_id=group_id
        )
        member_count = ginfo.get("member_count")
    except Exception:  # noqa: BLE001
        pass

    info_parts = [f"群号: {group_id}"]
    if member_count is not None:
        info_parts.append(f"成员: {member_count}")
    lines.append(" | ".join(info_parts))

    tracked = await session.scalar(
        select(func.count()).select_from(UserGroup).where(
            UserGroup.group_id == group_id
        )
    )
    lines.append(f"已追踪用户: {tracked or 0}")

    # 群最后活跃
    from .data_models.bot_group import BotGroup

    grp = await session.get(BotGroup, group_id)
    if grp:
        lines.append(
            f"群最后活跃: {_fmt_time(grp.last_active_at)}"
        )

    # 功能开关列表
    lines.append("─── 功能开关 ───")
    features = list_features()
    for i, (key, display) in enumerate(features, 1):
        gf = await session.get(
            GroupFeature,
            {"group_id": group_id, "feature": key},
        )
        enabled = gf.enabled if gf is not None else True
        icon = "开" if enabled else "关"
        lines.append(f"  {i}. [{icon}] {display}")

    lines.append("──────────────")
    lines.append("输入功能序号切换开关")
    lines.append("输入「用户 <QQ号>」管理用户功能")
    lines.append("输入「取消」退出")
    return "\n".join(lines)


async def _build_user_feature_text(
    session: async_scoped_session,
    target_user_id: int,
    group_id: int,
) -> str:
    """构建用户功能管理子面板文本。"""
    statuses = await get_user_feature_status(
        target_user_id, group_id, session
    )
    lines = [
        f"═══ 用户 {target_user_id} 功能管理 ═══"
    ]
    for i, (_key, display, enabled, source) in enumerate(
        statuses, 1
    ):
        icon = "✓" if enabled else "✗"
        lines.append(
            f"  {i}. {icon} {display}（{source}）"
        )
    lines.append("──────────────")
    lines.append("输入「开启 <序号>」或「关闭 <序号>」切换")
    lines.append("输入「返回」回群管理面板")
    return "\n".join(lines)


# ── /面板 事件处理 ────────────────────────────────────────


@panel_cmd.handle()
async def handle_panel_entry(
    event: MessageEvent,
    matcher: Matcher,
    session: async_scoped_session,
    args: Message = CommandArg(),
) -> None:
    """面板入口：根据消息类型构建对应面板。"""
    user_id = int(event.get_user_id())

    if isinstance(event, GroupMessageEvent):
        # 群聊模式：展示群内个人数据
        group_id = event.group_id
        group_name: Optional[str] = None
        from .data_models.bot_group import BotGroup

        grp = await session.get(BotGroup, group_id)
        if grp:
            group_name = grp.group_name

        panel_text = await _build_group_personal_panel(
            session, user_id, group_id, group_name
        )
        matcher.state["mode"] = "group"
        matcher.state["group_id"] = group_id
        matcher.state["view"] = "main"
    else:
        # 私聊模式：展示全量数据 + 菜单
        panel_text = await _build_private_main_panel(
            session, user_id
        )
        matcher.state["mode"] = "private"
        matcher.state["view"] = "main"

    matcher.state["user_id"] = user_id
    # 先发送面板内容，再进入 got 等待输入
    await panel_cmd.send(panel_text)

    # 若命令自带参数，跳过 got 询问
    arg_text = args.extract_plain_text().strip()
    if arg_text:
        matcher.set_arg("panel_choice", args)


@panel_cmd.got(
    "panel_choice",
    prompt="请输入操作，或发送「取消」退出",
)
async def handle_panel_choice(
    bot: Bot,
    matcher: Matcher,
    session: async_scoped_session,
    choice: str = ArgPlainText("panel_choice"),
) -> None:
    """统一交互路由：根据 mode 和 view 分发处理。

    通过 matcher.state["view"] 区分当前视图层级，
    所有反馈均通过 reject_arg 发送，保证单消息刷新。
    """
    text = choice.strip()

    if text in ("取消", "退出", "q"):
        await panel_cmd.finish("已退出面板，下次再见~")

    mode: str = matcher.state.get("mode", "group")

    if mode == "group":
        await _handle_group_panel(
            matcher, session, text
        )
    else:
        await _handle_private_panel(
            bot, matcher, session, text
        )


async def _handle_group_panel(
    matcher: Matcher,
    session: async_scoped_session,
    text: str,
) -> None:
    """群聊个人面板交互：仅支持功能详情查看。"""
    user_id: int = matcher.state["user_id"]
    group_id: int = matcher.state["group_id"]

    if text.startswith("功能"):
        parts = text.split()
        if (
            len(parts) < _MIN_DELETE_PARTS
            or not parts[1].isdigit()
        ):
            await panel_cmd.reject_arg(
                "panel_choice",
                "格式：功能 <序号>",
            )
        idx = int(parts[1])
        statuses = await get_user_feature_status(
            user_id, group_id, session
        )
        if idx < 1 or idx > len(statuses):
            await panel_cmd.reject_arg(
                "panel_choice",
                f"序号超出范围（1-{len(statuses)}）",
            )
        _key, display, enabled, source = statuses[idx - 1]
        status_text = "开启" if enabled else "关闭"
        detail = (
            f"功能「{display}」\n"
            f"  状态: {status_text}\n"
            f"  来源: {source}\n"
            f"──────────────\n"
            f"输入「功能 <序号>」查看详情\n"
            f"输入「取消」退出"
        )
        await panel_cmd.reject_arg(
            "panel_choice", detail
        )

    # 无效输入
    await panel_cmd.reject_arg(
        "panel_choice",
        "输入「功能 <序号>」查看详情，"
        "或「取消」退出",
    )


async def _handle_private_panel(
    bot: Bot,
    matcher: Matcher,
    session: async_scoped_session,
    text: str,
) -> None:
    """私聊面板交互：多层级视图状态机。"""
    view: str = matcher.state.get("view", "main")

    if view == "main":
        await _private_view_main(
            bot, matcher, session, text
        )
    elif view == "groups":
        await _private_view_groups(
            matcher, session, text
        )
    elif view == "group_detail":
        await _private_view_group_detail(
            matcher, text
        )
    elif view == "chat_list":
        await _private_view_chat_list(
            matcher, session, text
        )
    elif view == "chat_detail":
        await _private_view_chat_detail(
            matcher, session, text
        )
    else:
        await panel_cmd.reject_arg(
            "panel_choice", "未知视图，请输入「取消」退出"
        )


async def _private_view_main(
    bot: Bot,
    matcher: Matcher,
    session: async_scoped_session,
    text: str,
) -> None:
    """私聊主菜单：选择进入群聊列表或对话管理。"""
    user_id: int = matcher.state["user_id"]

    if text == "1":
        # 进入群聊列表
        stmt = (
            select(UserGroup)
            .options(selectinload(UserGroup.group))
            .where(UserGroup.user_id == user_id)
            .order_by(UserGroup.group_id)
        )
        result = await session.execute(stmt)
        user_groups = result.scalars().all()

        if not user_groups:
            await panel_cmd.reject_arg(
                "panel_choice",
                "你还没有和我共同存在的群聊哦~\n"
                "输入「取消」退出",
            )

        groups: list[dict] = []
        for ug in user_groups:
            grp: "BotGroup" = ug.group
            role = await _get_user_role_in_group(
                bot, grp.group_id, user_id
            )
            groups.append(
                {
                    "group_id": grp.group_id,
                    "group_name": grp.group_name,
                    "is_admin": role in ("owner", "admin"),
                    "last_active_at": grp.last_active_at,
                    "first_seen_at": ug.first_seen_at,
                    "last_seen_at": ug.last_seen_at,
                }
            )

        matcher.state["groups"] = groups
        matcher.state["view"] = "groups"
        await panel_cmd.reject_arg(
            "panel_choice",
            _build_group_list_text(groups),
        )

    elif text == "2":
        # 进入对话管理
        sessions = await _get_user_sessions(
            session, user_id
        )
        matcher.state["sessions"] = sessions
        matcher.state["view"] = "chat_list"
        await panel_cmd.reject_arg(
            "panel_choice",
            _build_chat_list_text(sessions),
        )

    else:
        await panel_cmd.reject_arg(
            "panel_choice",
            "请输入 1 或 2 选择功能，"
            "或「取消」退出",
        )


async def _private_view_groups(
    matcher: Matcher,
    session: async_scoped_session,
    text: str,
) -> None:
    """群聊列表视图：选择群查看详情或返回。"""
    if text == "返回":
        user_id: int = matcher.state["user_id"]
        panel_text = await _build_private_main_panel(
            session, user_id
        )
        matcher.state["view"] = "main"
        await panel_cmd.reject_arg(
            "panel_choice", panel_text
        )

    if not text.isdigit():
        await panel_cmd.reject_arg(
            "panel_choice",
            "请输入有效的群序号，"
            "或「返回」回主菜单",
        )

    idx = int(text)
    groups: list[dict] = matcher.state["groups"]

    if idx < 1 or idx > len(groups):
        await panel_cmd.reject_arg(
            "panel_choice",
            f"序号超出范围（1-{len(groups)}），请重新输入",
        )

    selected = groups[idx - 1]
    matcher.state["selected_group"] = selected
    matcher.state["view"] = "group_detail"
    await panel_cmd.reject_arg(
        "panel_choice",
        _build_group_detail_text(
            selected, is_admin=selected["is_admin"]
        ),
    )


async def _private_view_group_detail(
    matcher: Matcher,
    text: str,
) -> None:
    """群详情视图：返回群列表。"""
    if text == "返回":
        groups: list[dict] = matcher.state["groups"]
        matcher.state["view"] = "groups"
        await panel_cmd.reject_arg(
            "panel_choice",
            _build_group_list_text(groups),
        )

    await panel_cmd.reject_arg(
        "panel_choice",
        "输入「返回」回群列表，或「取消」退出",
    )


async def _private_view_chat_list(
    matcher: Matcher,
    session: async_scoped_session,
    text: str,
) -> None:
    """对话列表视图：查看详情、删除对话、返回。"""
    user_id: int = matcher.state["user_id"]

    if text == "返回":
        # 重建主菜单
        sessions = await _get_user_sessions(
            session, user_id
        )
        matcher.state["sessions"] = sessions
        matcher.state["view"] = "main"
        panel_text = await _build_private_main_panel(
            session,
            user_id,
        )
        await panel_cmd.reject_arg(
            "panel_choice", panel_text
        )

    # 删除对话
    if (
        text.startswith("删除")
        and not text.startswith("删除消息")
    ):
        parts = text.split()
        if (
            len(parts) < _MIN_DELETE_PARTS
            or not parts[1].isdigit()
        ):
            await panel_cmd.reject_arg(
                "panel_choice",
                "格式：删除 <序号>\n例如：删除 1",
            )
        sessions: list[ChatSession] = matcher.state[
            "sessions"
        ]
        idx = int(parts[1])
        if idx < 1 or idx > len(sessions):
            await panel_cmd.reject_arg(
                "panel_choice",
                f"序号超出范围（1-{len(sessions)}）",
            )

        target = sessions[idx - 1]
        # commit 前缓存字段，避免 MissingGreenlet
        target_id = target.id
        target_title = target.title or "未命名"
        target.is_deleted = True
        await session.commit()

        logger.info(
            f"用户删除了对话 #{target_id}「{target_title}」"
        )
        # 刷新列表
        sessions = await _get_user_sessions(
            session, user_id
        )
        matcher.state["sessions"] = sessions
        await panel_cmd.reject_arg(
            "panel_choice",
            f"已删除对话「{target_title}」\n\n"
            + _build_chat_list_text(sessions),
        )

    # 进入对话详情
    if text.isdigit():
        sessions = matcher.state["sessions"]
        idx = int(text)
        if idx < 1 or idx > len(sessions):
            await panel_cmd.reject_arg(
                "panel_choice",
                f"序号超出范围（1-{len(sessions)}）",
            )

        target = sessions[idx - 1]
        messages = await _get_session_messages(
            session, target.id
        )
        matcher.state["view"] = "chat_detail"
        matcher.state["current_session"] = target
        await panel_cmd.reject_arg(
            "panel_choice",
            _build_chat_detail_text(target, messages),
        )

    # 无效输入
    await panel_cmd.reject_arg(
        "panel_choice",
        "请输入有效序号，或「删除 <序号>」，"
        "或「返回」回主菜单",
    )


async def _private_view_chat_detail(
    matcher: Matcher,
    session: async_scoped_session,
    text: str,
) -> None:
    """对话详情视图：删除消息、返回列表。"""
    user_id: int = matcher.state["user_id"]

    if text == "返回":
        sessions = await _get_user_sessions(
            session, user_id
        )
        matcher.state["sessions"] = sessions
        matcher.state["view"] = "chat_list"
        matcher.state.pop("current_session", None)
        await panel_cmd.reject_arg(
            "panel_choice",
            _build_chat_list_text(sessions),
        )

    # 删除消息
    if text.startswith("删除消息"):
        parts = text.split()
        if (
            len(parts) < _MIN_DELETE_PARTS
            or not parts[1].isdigit()
        ):
            await panel_cmd.reject_arg(
                "panel_choice",
                "格式：删除消息 <消息ID>",
            )
        msg_id = int(parts[1])
        msg = await session.get(ChatMessage, msg_id)

        if msg is None or msg.is_deleted:
            await panel_cmd.reject_arg(
                "panel_choice",
                "未找到该消息，请检查 ID 是否正确",
            )

        current: ChatSession = matcher.state[
            "current_session"
        ]
        if msg.session_id != current.id:
            await panel_cmd.reject_arg(
                "panel_choice",
                "该消息不属于当前对话",
            )

        msg.is_deleted = True
        await session.commit()
        logger.info(f"用户删除了消息 #{msg_id}")

        # 刷新详情
        messages = await _get_session_messages(
            session, current.id
        )
        await panel_cmd.reject_arg(
            "panel_choice",
            f"已删除消息 #{msg_id}\n\n"
            + _build_chat_detail_text(current, messages),
        )

    # 无效操作
    await panel_cmd.reject_arg(
        "panel_choice",
        "无效操作。可用：删除消息 <ID> / 返回 / 取消",
    )


# ── /群管理 事件处理 ──────────────────────────────────────


@group_admin_cmd.handle()
async def handle_group_admin_entry(
    bot: Bot,
    event: MessageEvent,
    matcher: Matcher,
    session: async_scoped_session,
    args: Message = CommandArg(),
) -> None:
    """群管理面板入口：权限检查，构建管理面板。"""
    if not isinstance(event, GroupMessageEvent):
        await group_admin_cmd.finish(
            "请在群聊中使用群管理功能~"
        )

    # 权限检查：群管/群主 或 超级用户
    superusers = get_driver().config.superusers
    user_id = int(event.get_user_id())
    is_su = str(user_id) in superusers

    if not is_su and not is_group_admin(event):
        await group_admin_cmd.finish(
            "仅群主、群管理员或超级用户可使用群管理功能"
        )

    group_id = event.group_id
    group_name: Optional[str] = None
    from .data_models.bot_group import BotGroup

    grp = await session.get(BotGroup, group_id)
    if grp:
        group_name = grp.group_name

    matcher.state["group_id"] = group_id
    matcher.state["group_name"] = group_name
    matcher.state["view"] = "main"

    panel_text = await _build_admin_panel(
        session, bot, group_id, group_name
    )
    # 先发送面板内容，再进入 got 等待输入
    await group_admin_cmd.send(panel_text)

    arg_text = args.extract_plain_text().strip()
    if arg_text:
        matcher.set_arg("admin_choice", args)


@group_admin_cmd.got(
    "admin_choice",
    prompt="请输入操作，或发送「取消」退出",
)
async def handle_group_admin_choice(
    bot: Bot,
    matcher: Matcher,
    session: async_scoped_session,
    choice: str = ArgPlainText("admin_choice"),
) -> None:
    """群管理面板交互路由。

    view="main": 功能序号切换 / 进入用户管理
    view="user_feature": 用户功能开启/关闭
    """
    text = choice.strip()

    if text in ("取消", "退出", "q"):
        await group_admin_cmd.finish("已退出群管理面板")

    view: str = matcher.state.get("view", "main")
    group_id: int = matcher.state["group_id"]
    group_name: Optional[str] = matcher.state.get(
        "group_name"
    )

    if view == "user_feature":
        await _admin_view_user_feature(
            bot, matcher, session, text,
            group_id, group_name,
        )
        return

    # ── 主视图 ──

    # 进入用户功能管理
    if text.startswith("用户"):
        parts = text.split()
        if (
            len(parts) < _MIN_DELETE_PARTS
            or not parts[1].isdigit()
        ):
            await group_admin_cmd.reject_arg(
                "admin_choice",
                "格式：用户 <QQ号>",
            )
        target_uid = int(parts[1])

        # 确保 UserGroup 存在（FK 约束）
        ug = await session.get(
            UserGroup, (group_id, target_uid)
        )
        if ug is None:
            ug = UserGroup(
                group_id=group_id, user_id=target_uid
            )
            session.add(ug)
            await session.flush()

        matcher.state["target_user_id"] = target_uid
        matcher.state["view"] = "user_feature"
        panel_text = await _build_user_feature_text(
            session, target_uid, group_id
        )
        await group_admin_cmd.reject_arg(
            "admin_choice", panel_text
        )

    # 切换功能开关
    if text.isdigit():
        idx = int(text)
        features = list_features()
        if idx < 1 or idx > len(features):
            await group_admin_cmd.reject_arg(
                "admin_choice",
                f"序号超出范围（1-{len(features)}）",
            )

        feat_key, display = features[idx - 1]
        gf = await session.get(
            GroupFeature,
            {"group_id": group_id, "feature": feat_key},
        )
        if gf is None:
            new_enabled = False
            gf = GroupFeature(
                group_id=group_id,
                feature=feat_key,
                enabled=False,
            )
            session.add(gf)
        else:
            new_enabled = not gf.enabled
            gf.enabled = new_enabled

        await session.commit()
        status_text = "开启" if new_enabled else "关闭"
        logger.info(
            f"群 {group_id} {status_text}了功能「{display}」"
        )

        panel_text = await _build_admin_panel(
            session, bot, group_id, group_name
        )
        await group_admin_cmd.reject_arg(
            "admin_choice",
            f"已{status_text}功能「{display}」\n\n"
            + panel_text,
        )

    # 无效输入
    await group_admin_cmd.reject_arg(
        "admin_choice",
        "输入功能序号切换开关，"
        "「用户 <QQ号>」管理用户功能，"
        "或「取消」退出",
    )


async def _admin_view_user_feature(  # noqa: PLR0913, PLR0917
    bot: Bot,
    matcher: Matcher,
    session: async_scoped_session,
    text: str,
    group_id: int,
    group_name: Optional[str],
) -> None:
    """用户功能管理子视图。"""
    target_uid: int = matcher.state["target_user_id"]

    if text == "返回":
        matcher.state["view"] = "main"
        matcher.state.pop("target_user_id", None)
        panel_text = await _build_admin_panel(
            session, bot, group_id, group_name
        )
        await group_admin_cmd.reject_arg(
            "admin_choice", panel_text
        )

    # 开启/关闭 <序号>
    parts = text.split()
    if (
        len(parts) >= _MIN_DELETE_PARTS
        and parts[0] in ("开启", "关闭")
        and parts[1].isdigit()
    ):
        enabled = parts[0] == "开启"
        idx = int(parts[1])
        features = list_features()
        if idx < 1 or idx > len(features):
            await group_admin_cmd.reject_arg(
                "admin_choice",
                f"序号超出范围（1-{len(features)}）",
            )

        feat_key, display = features[idx - 1]

        # 确保 UserGroup 存在
        ug = await session.get(
            UserGroup, (group_id, target_uid)
        )
        if ug is None:
            ug = UserGroup(
                group_id=group_id, user_id=target_uid
            )
            session.add(ug)
            await session.flush()

        record = await session.get(
            UserFeature,
            {
                "group_id": group_id,
                "user_id": target_uid,
                "feature": feat_key,
            },
        )
        if record is None:
            record = UserFeature(
                group_id=group_id,
                user_id=target_uid,
                feature=feat_key,
                enabled=enabled,
            )
            session.add(record)
        else:
            record.enabled = enabled

        await session.commit()
        status_text = "开启" if enabled else "关闭"
        logger.info(
            f"群 {group_id} 为用户 {target_uid} "
            f"{status_text}了功能「{display}」"
        )

        panel_text = await _build_user_feature_text(
            session, target_uid, group_id
        )
        await group_admin_cmd.reject_arg(
            "admin_choice",
            f"已为用户 {target_uid} "
            f"{status_text}功能「{display}」\n\n"
            + panel_text,
        )

    # 无效输入
    await group_admin_cmd.reject_arg(
        "admin_choice",
        "输入「开启 <序号>」或「关闭 <序号>」切换，"
        "或「返回」回群管理面板",
    )


# ── 超级管理员命令 ────────────────────────────────────────


@global_group_feature_cmd.handle()
async def handle_global_group_feature(
    event: MessageEvent,
    session: async_scoped_session,
    args: Message = CommandArg(),
) -> None:
    """超管管理任意群的功能开关。"""
    text = args.extract_plain_text().strip()
    parts = text.split()
    if (
        len(parts) < _MIN_GLOBAL_CMD_PARTS
        or not parts[0].isdigit()
    ):
        await global_group_feature_cmd.finish(
            "格式：/全局群功能 <群号> 开启/关闭 <功能名>"
        )

    group_id = int(parts[0])
    action_enabled, feature_key = _parse_action_feature(
        parts[1:]
    )
    if action_enabled is None or feature_key is None:
        await global_group_feature_cmd.finish(
            "格式：/全局群功能 <群号> 开启/关闭 <功能名>"
        )

    gf = await session.get(
        GroupFeature,
        {"group_id": group_id, "feature": feature_key},
    )
    if gf is None:
        gf = GroupFeature(
            group_id=group_id,
            feature=feature_key,
            enabled=action_enabled,
        )
        session.add(gf)
    else:
        gf.enabled = action_enabled

    await session.commit()
    display = get_feature_display(feature_key)
    status_text = "开启" if action_enabled else "关闭"
    logger.info(
        f"超级管理员 {event.user_id} 为群 {group_id} "
        f"{status_text}了功能「{display}」"
    )
    await global_group_feature_cmd.finish(
        f"已为群 {group_id} {status_text}功能「{display}」"
    )


@global_user_feature_cmd.handle()
async def handle_global_user_feature(
    event: MessageEvent,
    session: async_scoped_session,
    args: Message = CommandArg(),
) -> None:
    """超管管理任意用户的功能开关。

    带群号 → 写入 UserFeature（群内用户级覆盖）
    不带群号 → 写入 GlobalUserFeature（全局用户开关）
    """
    text = args.extract_plain_text().strip()
    parts = text.split()

    if (
        len(parts) < _MIN_GLOBAL_CMD_PARTS
        or not parts[0].isdigit()
    ):
        await global_user_feature_cmd.finish(
            "格式：/全局用户功能 <QQ号> <群号> "
            "开启/关闭 <功能名>\n"
            "或：/全局用户功能 <QQ号> "
            "开启/关闭 <功能名>（全局生效）"
        )

    target_user_id = int(parts[0])

    # 判断第二个参数是群号还是动作
    group_id: Optional[int] = None
    rest_parts: list[str] = parts[1:]
    if parts[1].isdigit():
        group_id = int(parts[1])
        rest_parts = parts[2:]

    action_enabled, feature_key = _parse_action_feature(
        rest_parts
    )
    if action_enabled is None or feature_key is None:
        await global_user_feature_cmd.finish(
            "格式：/全局用户功能 <QQ号> [群号] "
            "开启/关闭 <功能名>"
        )

    display = get_feature_display(feature_key)
    status_text = "开启" if action_enabled else "关闭"

    if group_id is not None:
        # 群内用户级覆盖
        ug = await session.get(
            UserGroup, (group_id, target_user_id)
        )
        if ug is None:
            ug = UserGroup(
                group_id=group_id,
                user_id=target_user_id,
            )
            session.add(ug)
            await session.flush()

        record = await session.get(
            UserFeature,
            {
                "group_id": group_id,
                "user_id": target_user_id,
                "feature": feature_key,
            },
        )
        if record is None:
            record = UserFeature(
                group_id=group_id,
                user_id=target_user_id,
                feature=feature_key,
                enabled=action_enabled,
            )
            session.add(record)
        else:
            record.enabled = action_enabled
        scope_text = f"群 {group_id} 内"
    else:
        # 全局用户开关
        record_g = await session.get(
            GlobalUserFeature,
            {
                "user_id": target_user_id,
                "feature": feature_key,
            },
        )
        if record_g is None:
            record_g = GlobalUserFeature(
                user_id=target_user_id,
                feature=feature_key,
                enabled=action_enabled,
            )
            session.add(record_g)
        else:
            record_g.enabled = action_enabled
        scope_text = "全局"

    await session.commit()
    logger.info(
        f"超级管理员 {event.user_id} 为用户 "
        f"{target_user_id} {scope_text}"
        f"{status_text}了功能「{display}」"
    )
    await global_user_feature_cmd.finish(
        f"已为用户 {target_user_id} {scope_text}"
        f"{status_text}功能「{display}」"
    )


@perm_query_cmd.handle()
async def handle_perm_query(
    session: async_scoped_session,
    args: Message = CommandArg(),
) -> None:
    """超管查询某用户的完整权限状态。"""
    text = args.extract_plain_text().strip()
    parts = text.split()

    if not parts or not parts[0].isdigit():
        await perm_query_cmd.finish(
            "格式：/权限查询 <QQ号> [群号]"
        )

    target_user_id = int(parts[0])
    group_id: Optional[int] = None
    if len(parts) > 1 and parts[1].isdigit():
        group_id = int(parts[1])

    statuses = await get_user_feature_status(
        target_user_id, group_id, session
    )

    if group_id is not None:
        header = (
            f"═══ 用户 {target_user_id} "
            f"在群 {group_id} 的权限 ═══"
        )
    else:
        header = (
            f"═══ 用户 {target_user_id} "
            f"的全局权限（私聊）═══"
        )

    lines = [header]
    for _key, display, enabled, source in statuses:
        icon = "✓" if enabled else "✗"
        lines.append(f"  {icon} {display}（{source}）")
    await perm_query_cmd.finish("\n".join(lines))


@admin_chat_view_cmd.handle()
async def handle_admin_view(
    session: async_scoped_session,
    args: Message = CommandArg(),
) -> None:
    """超管查看指定用户的对话记录。

    格式：/查看用户对话 <QQ号> [会话ID]
    """
    text = args.extract_plain_text().strip()
    parts = text.split()

    if not parts or not parts[0].isdigit():
        await admin_chat_view_cmd.finish(
            "格式：/查看用户对话 <QQ号> [会话ID]"
        )

    target_uid = int(parts[0])

    # 查看指定会话的消息
    if len(parts) > 1 and parts[1].isdigit():
        sess_id = int(parts[1])
        chat_sess = await session.get(
            ChatSession, sess_id
        )
        if (
            chat_sess is None
            or chat_sess.user_id != target_uid
        ):
            await admin_chat_view_cmd.finish(
                f"未找到用户 {target_uid} "
                f"的会话 #{sess_id}"
            )

        messages = await _get_session_messages(
            session, sess_id
        )
        lines = [
            f"═══ 用户 {target_uid} 的对话 "
            f"#{sess_id} ═══",
            f"标题：{chat_sess.title or '未命名'}",
            f"消息数：{len(messages)}",
            "",
            _fmt_message_list(messages),
        ]
        await admin_chat_view_cmd.finish(
            "\n".join(lines)
        )

    # 列出用户所有会话
    sessions = await _get_user_sessions(
        session, target_uid
    )
    lines = [
        f"═══ 用户 {target_uid} 的对话列表 ═══",
        _fmt_session_list(sessions),
        "",
        "使用 /查看用户对话 "
        f"{target_uid} <会话ID> 查看详情",
    ]
    await admin_chat_view_cmd.finish("\n".join(lines))


@admin_chat_delete_cmd.handle()
async def handle_admin_delete(
    session: async_scoped_session,
    args: Message = CommandArg(),
) -> None:
    """超管删除指定用户的对话或消息。

    格式：
      /删除用户对话 <QQ号> 会话 <会话ID>
      /删除用户对话 <QQ号> 消息 <消息ID>
    """
    text = args.extract_plain_text().strip()
    parts = text.split()

    if (
        len(parts) < _MIN_ADMIN_DELETE_PARTS
        or not parts[0].isdigit()
    ):
        await admin_chat_delete_cmd.finish(
            "格式：\n"
            "  /删除用户对话 <QQ号> 会话 <会话ID>\n"
            "  /删除用户对话 <QQ号> 消息 <消息ID>"
        )

    target_uid = int(parts[0])
    target_type = parts[1]
    if not parts[2].isdigit():
        await admin_chat_delete_cmd.finish(
            "ID 必须为数字"
        )
    target_id = int(parts[2])

    if target_type == "会话":
        chat_sess = await session.get(
            ChatSession, target_id
        )
        if (
            chat_sess is None
            or chat_sess.user_id != target_uid
        ):
            await admin_chat_delete_cmd.finish(
                f"未找到用户 {target_uid} 的会话 "
                f"#{target_id}"
            )
        chat_sess.is_deleted = True
        await session.commit()
        logger.info(
            f"超管删除了用户 {target_uid} 的会话 "
            f"#{target_id}"
        )
        await admin_chat_delete_cmd.finish(
            f"已删除用户 {target_uid} 的会话 #{target_id}"
        )

    elif target_type == "消息":
        msg = await session.get(ChatMessage, target_id)
        if msg is None:
            await admin_chat_delete_cmd.finish(
                f"未找到消息 #{target_id}"
            )
        chat_sess = await session.get(
            ChatSession, msg.session_id
        )
        if (
            chat_sess is None
            or chat_sess.user_id != target_uid
        ):
            await admin_chat_delete_cmd.finish(
                f"消息 #{target_id} 不属于用户 "
                f"{target_uid}"
            )
        msg.is_deleted = True
        await session.commit()
        logger.info(
            f"超管删除了用户 {target_uid} 的消息 "
            f"#{target_id}"
        )
        await admin_chat_delete_cmd.finish(
            f"已删除用户 {target_uid} 的消息 #{target_id}"
        )

    else:
        await admin_chat_delete_cmd.finish(
            "类型必须为「会话」或「消息」"
        )


# ── 内部工具函数 ──────────────────────────────────────────


def _parse_action_feature(
    parts: list[str],
) -> tuple[Optional[bool], Optional[str]]:
    """从参数列表中解析 (动作, 功能key)。

    返回 (None, None) 表示解析失败。
    """
    if len(parts) < _MIN_DELETE_PARTS:
        return None, None
    action_str, feature_str = parts[0], parts[1]
    if action_str not in ("开启", "关闭"):
        return None, None
    feature_key = resolve_feature_key(feature_str)
    if feature_key is None:
        return None, None
    return action_str == "开启", feature_key
