"""统一管理面板模块。

群聊：/面板 → 个人面板（群内数据），/群管理 → 群管理面板
私聊：/面板 → 个人面板（全量数据 + 群聊列表 + 对话管理）

数据查询、菜单定义和交互路由分别位于 panel_data/panel_menu/panel_router。
"""

from typing import Optional

from nonebot import get_driver, logger
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.matcher import Matcher
from nonebot.params import ArgPlainText, CommandArg
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata, on_command
from nonebot_plugin_orm import async_scoped_session

from .command_catalog import CommandSpec, PluginCommandGroup, register_command_group
from .command_ux import command_failure, permission_required, scope_required
from .data_models.chat_message import ChatMessage
from .data_models.chat_session import ChatSession
from .data_models.global_user_feature import GlobalUserFeature
from .data_models.group_feature import GroupFeature
from .data_models.user_feature import UserFeature
from .data_models.user_group import UserGroup
from .panel_data import (
    build_group_personal_view,
    build_private_personal_view,
    get_group_name,
)
from .panel_data import (
    ensure_group_record as _ensure_group_record,
)
from .panel_data import (
    ensure_scope_records as _ensure_scope_records,
)
from .panel_data import (
    get_session_messages as _get_session_messages,
)
from .panel_data import (
    get_user_sessions as _get_user_sessions,
)
from .panel_menu import (
    AdminFlow,
    PanelFlow,
    PanelMode,
    personal_panel_text,
)
from .panel_menu import (
    message_list_text as _fmt_message_list,
)
from .panel_menu import (
    session_list_text as _fmt_session_list,
)
from .panel_router import render_admin_menu, route_admin_choice, route_panel_choice
from .permission import (
    get_feature_display,
    get_user_feature_status,
    is_group_admin,
    resolve_feature_key,
)
from .session_interaction import (
    SessionIntent,
    pass_through_new_command,
    resolve_session_intent,
)
from .ui.panel_renderer import render_personal_panel

COMMAND_GROUP = register_command_group(
    PluginCommandGroup(
        plugin_id="yawn_core.panel",
        display_name="管理面板",
        entrypoint="面板",
        commands=(
            CommandSpec(
                name="面板",
                aliases=("个人面板", "我的面板"),
                description="查看个人信息面板",
            ),
            CommandSpec(
                name="群管理",
                aliases=("群管理面板",),
                description="群功能开关管理（需群管/超管）",
                scope="group",
                permission="group_admin",
                display_level="advanced",
                help_section="admin",
            ),
            CommandSpec(
                name="全局群功能",
                description="管理任意群的功能开关",
                permission="superuser",
                display_level="advanced",
                help_section="admin",
            ),
            CommandSpec(
                name="全局用户功能",
                description="管理任意用户的功能开关",
                permission="superuser",
                display_level="advanced",
                help_section="admin",
            ),
            CommandSpec(
                name="权限查询",
                description="查询用户权限状态",
                permission="superuser",
                display_level="advanced",
                help_section="admin",
            ),
            CommandSpec(
                name="查看用户对话",
                description="查看指定用户的对话记录",
                permission="superuser",
                display_level="advanced",
                help_section="admin",
            ),
            CommandSpec(
                name="删除用户对话",
                description="删除指定用户的对话或消息",
                permission="superuser",
                display_level="advanced",
                help_section="admin",
            ),
        ),
    )
)

__plugin_meta__ = PluginMetadata(
    name="管理面板",
    description="个人面板与群管理面板",
    usage="发送 /面板 查看个人信息",
    extra={"command_group": COMMAND_GROUP},
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
perm_query_cmd = on_command("权限查询", permission=SUPERUSER, priority=2, block=True)
admin_chat_view_cmd = on_command(
    "查看用户对话", permission=SUPERUSER, priority=2, block=True
)
admin_chat_delete_cmd = on_command(
    "删除用户对话", permission=SUPERUSER, priority=2, block=True
)


# ── /面板与群管理事件处理 ──────────────────────────────────


@panel_cmd.handle()
async def handle_panel_entry(
    event: MessageEvent,
    matcher: Matcher,
    session: async_scoped_session,
    args: Message = CommandArg(),
) -> None:
    """查询首屏数据并初始化结构化会话状态。"""

    user_id = int(event.get_user_id())
    if isinstance(event, GroupMessageEvent):
        group_id = int(event.group_id)
        group_name = await get_group_name(session, group_id)
        flow = PanelFlow(
            user_id=user_id,
            mode=PanelMode.GROUP,
            group_id=group_id,
            group_name=group_name,
        )
        panel_view = await build_group_personal_view(
            session, user_id, group_id, group_name
        )
    else:
        flow = PanelFlow(user_id=user_id, mode=PanelMode.PRIVATE)
        panel_view = await build_private_personal_view(session, user_id)
    matcher.state["panel_flow"] = flow

    panel_image = await render_personal_panel(panel_view)
    await panel_cmd.send(
        MessageSegment.image(panel_image)
        if panel_image is not None
        else personal_panel_text(panel_view)
    )
    arg_text = args.extract_plain_text().strip()
    if arg_text:
        matcher.set_arg("panel_choice", args)


@panel_cmd.got(
    "panel_choice",
    prompt="请输入操作；返回可退回上一级，菜单可重新显示，取消可退出",
)
async def handle_panel_choice(
    bot: Bot,
    matcher: Matcher,
    session: async_scoped_session,
    choice: str = ArgPlainText("panel_choice"),
) -> None:
    text = choice.strip()
    intent = resolve_session_intent(text)
    if intent is SessionIntent.NEW_COMMAND:
        await pass_through_new_command(matcher, text)
    if intent is SessionIntent.EXIT:
        await panel_cmd.finish("已退出面板。")
    flow = matcher.state.get("panel_flow")
    if not isinstance(flow, PanelFlow):
        await panel_cmd.finish(
            command_failure("面板会话已结束", "会话状态已经失效", "发送 /面板 重新进入")
        )
    await route_panel_choice(bot, matcher, session, flow, text, intent)


@group_admin_cmd.handle()
async def handle_group_admin_entry(
    bot: Bot,
    event: MessageEvent,
    matcher: Matcher,
    session: async_scoped_session,
    args: Message = CommandArg(),
) -> None:
    """鉴权后初始化群管理会话；handler 仍是最终安全边界。"""

    if not isinstance(event, GroupMessageEvent):
        await group_admin_cmd.finish(
            scope_required("群管理", "群聊", "请在目标群发送 /群管理")
        )
    user_id = int(event.get_user_id())
    is_superuser = str(user_id) in get_driver().config.superusers
    if not is_superuser and not is_group_admin(event):
        await group_admin_cmd.finish(
            permission_required(
                "群管理", "群主、群管理员或超级用户", "联系群管理员进行设置"
            )
        )
    group_id = int(event.group_id)
    flow = AdminFlow(
        group_id=group_id,
        group_name=await get_group_name(session, group_id),
    )
    matcher.state["admin_flow"] = flow
    await group_admin_cmd.send(await render_admin_menu(bot, session, flow))
    arg_text = args.extract_plain_text().strip()
    if arg_text:
        matcher.set_arg("admin_choice", args)


@group_admin_cmd.got(
    "admin_choice",
    prompt="请输入操作；返回可退回上一级，菜单可重新显示，取消可退出",
)
async def handle_group_admin_choice(
    bot: Bot,
    matcher: Matcher,
    session: async_scoped_session,
    choice: str = ArgPlainText("admin_choice"),
) -> None:
    text = choice.strip()
    intent = resolve_session_intent(text)
    if intent is SessionIntent.NEW_COMMAND:
        await pass_through_new_command(matcher, text)
    if intent is SessionIntent.EXIT:
        await group_admin_cmd.finish("已退出群管理面板。")
    flow = matcher.state.get("admin_flow")
    if not isinstance(flow, AdminFlow):
        await group_admin_cmd.finish(
            command_failure(
                "群管理会话已结束", "会话状态已经失效", "发送 /群管理 重新进入"
            )
        )
    await route_admin_choice(bot, matcher, session, flow, text, intent)


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
    if len(parts) < _MIN_GLOBAL_CMD_PARTS or not parts[0].isdigit():
        await global_group_feature_cmd.finish(
            "格式：/全局群功能 <群号> 开启/关闭 <功能名>"
        )

    group_id = int(parts[0])
    action_enabled, feature_key = _parse_action_feature(parts[1:])
    if action_enabled is None or feature_key is None:
        await global_group_feature_cmd.finish(
            "格式：/全局群功能 <群号> 开启/关闭 <功能名>"
        )

    await _ensure_group_record(session, group_id)
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
        f"超级管理员 {event.user_id} 为群 {group_id} {status_text}了功能「{display}」"
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

    if len(parts) < _MIN_GLOBAL_CMD_PARTS or not parts[0].isdigit():
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

    action_enabled, feature_key = _parse_action_feature(rest_parts)
    if action_enabled is None or feature_key is None:
        await global_user_feature_cmd.finish(
            "格式：/全局用户功能 <QQ号> [群号] 开启/关闭 <功能名>"
        )

    display = get_feature_display(feature_key)
    status_text = "开启" if action_enabled else "关闭"

    if group_id is not None:
        # 群内用户级覆盖
        await _ensure_scope_records(session, user_id=target_user_id, group_id=group_id)
        ug = await session.get(UserGroup, (group_id, target_user_id))
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
        await _ensure_scope_records(session, user_id=target_user_id)
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
        f"已为用户 {target_user_id} {scope_text}{status_text}功能「{display}」"
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
        await perm_query_cmd.finish("格式：/权限查询 <QQ号> [群号]")

    target_user_id = int(parts[0])
    group_id: Optional[int] = None
    if len(parts) > 1 and parts[1].isdigit():
        group_id = int(parts[1])

    statuses = await get_user_feature_status(target_user_id, group_id, session)

    if group_id is not None:
        header = f"═══ 用户 {target_user_id} 在群 {group_id} 的权限 ═══"
    else:
        header = f"═══ 用户 {target_user_id} 的全局权限（私聊）═══"

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
        await admin_chat_view_cmd.finish("格式：/查看用户对话 <QQ号> [会话ID]")

    target_uid = int(parts[0])

    # 查看指定会话的消息
    if len(parts) > 1 and parts[1].isdigit():
        sess_id = int(parts[1])
        chat_sess = await session.get(ChatSession, sess_id)
        if chat_sess is None or chat_sess.user_id != target_uid:
            await admin_chat_view_cmd.finish(
                f"未找到用户 {target_uid} 的会话 #{sess_id}"
            )

        messages = await _get_session_messages(session, sess_id)
        lines = [
            f"═══ 用户 {target_uid} 的对话 #{sess_id} ═══",
            f"标题：{chat_sess.title or '未命名'}",
            f"消息数：{len(messages)}",
            "",
            _fmt_message_list(messages),
        ]
        await admin_chat_view_cmd.finish("\n".join(lines))

    # 列出用户所有会话
    sessions = await _get_user_sessions(session, target_uid)
    lines = [
        f"═══ 用户 {target_uid} 的对话列表 ═══",
        _fmt_session_list(sessions),
        "",
        f"使用 /查看用户对话 {target_uid} <会话ID> 查看详情",
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

    if len(parts) < _MIN_ADMIN_DELETE_PARTS or not parts[0].isdigit():
        await admin_chat_delete_cmd.finish(
            "格式：\n"
            "  /删除用户对话 <QQ号> 会话 <会话ID>\n"
            "  /删除用户对话 <QQ号> 消息 <消息ID>"
        )

    target_uid = int(parts[0])
    target_type = parts[1]
    if not parts[2].isdigit():
        await admin_chat_delete_cmd.finish("ID 必须为数字")
    target_id = int(parts[2])

    if target_type == "会话":
        chat_sess = await session.get(ChatSession, target_id)
        if chat_sess is None or chat_sess.user_id != target_uid:
            await admin_chat_delete_cmd.finish(
                f"未找到用户 {target_uid} 的会话 #{target_id}"
            )
        chat_sess.is_deleted = True
        await session.commit()
        logger.info(f"超管删除了用户 {target_uid} 的会话 #{target_id}")
        await admin_chat_delete_cmd.finish(
            f"已删除用户 {target_uid} 的会话 #{target_id}"
        )

    elif target_type == "消息":
        msg = await session.get(ChatMessage, target_id)
        if msg is None:
            await admin_chat_delete_cmd.finish(f"未找到消息 #{target_id}")
        chat_sess = await session.get(ChatSession, msg.session_id)
        if chat_sess is None or chat_sess.user_id != target_uid:
            await admin_chat_delete_cmd.finish(
                f"消息 #{target_id} 不属于用户 {target_uid}"
            )
        msg.is_deleted = True
        await session.commit()
        logger.info(f"超管删除了用户 {target_uid} 的消息 #{target_id}")
        await admin_chat_delete_cmd.finish(
            f"已删除用户 {target_uid} 的消息 #{target_id}"
        )

    else:
        await admin_chat_delete_cmd.finish("类型必须为「会话」或「消息」")


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
