"""管理面板的交互路由；命令注册仍留在 ``panel.py``。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nonebot import logger

from .command_ux import command_failure, validation_failed
from .data_models.chat_message import ChatMessage
from .data_models.group_feature import GroupFeature
from .data_models.user_feature import UserFeature
from .panel_data import (
    build_group_personal_view,
    build_private_personal_view,
    ensure_scope_records,
    get_admin_panel_data,
    get_session_messages,
    get_user_sessions,
    list_user_groups,
)
from .panel_menu import (
    AdminFlow,
    PanelFlow,
    PanelMode,
    PanelView,
    admin_panel_text,
    chat_detail_text,
    chat_list_text,
    group_detail_text,
    group_list_text,
    personal_panel_text,
    user_feature_text,
)
from .permission import get_user_feature_status, list_features
from .session_interaction import SessionIntent

if TYPE_CHECKING:
    from nonebot.adapters import Bot
    from nonebot.matcher import Matcher
    from nonebot_plugin_orm import async_scoped_session

_CHOICE_ARG = "panel_choice"
_ADMIN_ARG = "admin_choice"
_COMMAND_PARTS = 2


async def render_panel_menu(
    bot: Bot,
    session: async_scoped_session,
    flow: PanelFlow,
) -> str:
    """按结构化视图状态重新查询并渲染当前菜单。"""

    if flow.view is PanelView.MAIN:
        if flow.mode is PanelMode.GROUP:
            assert flow.group_id is not None
            view = await build_group_personal_view(
                session, flow.user_id, flow.group_id, flow.group_name
            )
        else:
            view = await build_private_personal_view(session, flow.user_id)
        return personal_panel_text(view)
    if flow.view is PanelView.GROUPS:
        flow.groups = await list_user_groups(bot, session, flow.user_id)
        return group_list_text(flow.groups)
    if flow.view is PanelView.GROUP_DETAIL and flow.selected_group is not None:
        return group_detail_text(flow.selected_group)
    if flow.view is PanelView.CHAT_LIST:
        flow.sessions = await get_user_sessions(session, flow.user_id)
        return chat_list_text(flow.sessions)
    if flow.view is PanelView.CHAT_DETAIL and flow.current_session is not None:
        messages = await get_session_messages(session, flow.current_session.id)
        return chat_detail_text(flow.current_session, messages)
    return command_failure("菜单显示失败", "会话状态已经失效", "发送 /面板 重新进入")


async def route_panel_choice(  # noqa: PLR0913, PLR0917
    bot: Bot,
    matcher: Matcher,
    session: async_scoped_session,
    flow: PanelFlow,
    text: str,
    intent: SessionIntent,
) -> None:
    """处理个人面板输入；所有分支保持在同一个 got 参数上。"""

    if intent is SessionIntent.MENU:
        menu = await render_panel_menu(bot, session, flow)
        await matcher.reject_arg(_CHOICE_ARG, menu)
    if intent is SessionIntent.BACK:
        await _back_panel(bot, matcher, session, flow)
    if flow.mode is PanelMode.GROUP:
        await _route_group_panel(matcher, session, flow, text)
    if flow.view is PanelView.MAIN:
        await _route_private_main(bot, matcher, session, flow, text)
    if flow.view is PanelView.GROUPS:
        await _route_groups(matcher, flow, text)
    if flow.view is PanelView.GROUP_DETAIL:
        await matcher.reject_arg(
            _CHOICE_ARG,
            validation_failed("当前是群详情页", "发送「返回」回群列表"),
        )
    if flow.view is PanelView.CHAT_LIST:
        await _route_chat_list(matcher, session, flow, text)
    if flow.view is PanelView.CHAT_DETAIL:
        await _route_chat_detail(matcher, session, flow, text)
    await matcher.reject_arg(
        _CHOICE_ARG,
        command_failure("操作未执行", "会话状态已经失效", "发送 /面板 重新进入"),
    )


async def _back_panel(
    bot: Bot,
    matcher: Matcher,
    session: async_scoped_session,
    flow: PanelFlow,
) -> None:
    if flow.view is PanelView.MAIN:
        await matcher.finish("已退出面板。")
    if flow.view is PanelView.GROUP_DETAIL:
        flow.view = PanelView.GROUPS
        flow.selected_group = None
    elif flow.view is PanelView.CHAT_DETAIL:
        flow.view = PanelView.CHAT_LIST
        flow.current_session = None
    else:
        flow.view = PanelView.MAIN
    await matcher.reject_arg(_CHOICE_ARG, await render_panel_menu(bot, session, flow))


async def _route_group_panel(
    matcher: Matcher,
    session: async_scoped_session,
    flow: PanelFlow,
    text: str,
) -> None:
    parts = text.split()
    if len(parts) == _COMMAND_PARTS and parts[0] == "功能" and parts[1].isdigit():
        assert flow.group_id is not None
        statuses = await get_user_feature_status(flow.user_id, flow.group_id, session)
        index = int(parts[1])
        if 1 <= index <= len(statuses):
            _key, display, enabled, source = statuses[index - 1]
            await matcher.reject_arg(
                _CHOICE_ARG,
                f"功能「{display}」\n状态：{'开启' if enabled else '关闭'}\n"
                f"来源：{source}\n\n功能 <序号> 查看其他项 | 菜单 重新显示 | 取消 退出",
            )
        await matcher.reject_arg(
            _CHOICE_ARG,
            validation_failed(f"功能序号不在 1-{len(statuses)} 范围内", "输入有效序号"),
        )
    await matcher.reject_arg(
        _CHOICE_ARG,
        validation_failed("没有识别到面板操作", "输入「功能 <序号>」"),
    )


async def _route_private_main(
    bot: Bot,
    matcher: Matcher,
    session: async_scoped_session,
    flow: PanelFlow,
    text: str,
) -> None:
    if text == "1":
        flow.groups = await list_user_groups(bot, session, flow.user_id)
        if not flow.groups:
            await matcher.reject_arg(
                _CHOICE_ARG,
                command_failure(
                    "群聊列表为空",
                    "还没有记录到你与机器人共同所在的群",
                    "先在目标群与机器人互动，或发送「返回」退出面板",
                ),
            )
        flow.view = PanelView.GROUPS
        await matcher.reject_arg(_CHOICE_ARG, group_list_text(flow.groups))
    if text == "2":
        flow.sessions = await get_user_sessions(session, flow.user_id)
        flow.view = PanelView.CHAT_LIST
        await matcher.reject_arg(_CHOICE_ARG, chat_list_text(flow.sessions))
    await matcher.reject_arg(
        _CHOICE_ARG,
        validation_failed("没有这个主菜单选项", "输入 1 或 2", back=False),
    )


async def _route_groups(matcher: Matcher, flow: PanelFlow, text: str) -> None:
    if not text.isdigit():
        await matcher.reject_arg(
            _CHOICE_ARG,
            validation_failed("群序号格式不正确", "输入列表中的群序号"),
        )
    index = int(text)
    if not 1 <= index <= len(flow.groups):
        await matcher.reject_arg(
            _CHOICE_ARG,
            validation_failed(
                f"群序号不在 1-{len(flow.groups)} 范围内", "输入有效序号"
            ),
        )
    flow.selected_group = flow.groups[index - 1]
    flow.view = PanelView.GROUP_DETAIL
    await matcher.reject_arg(_CHOICE_ARG, group_detail_text(flow.selected_group))


async def _route_chat_list(
    matcher: Matcher,
    session: async_scoped_session,
    flow: PanelFlow,
    text: str,
) -> None:
    parts = text.split()
    if len(parts) == _COMMAND_PARTS and parts[0] == "删除" and parts[1].isdigit():
        index = int(parts[1])
        if not 1 <= index <= len(flow.sessions):
            await matcher.reject_arg(
                _CHOICE_ARG,
                validation_failed(
                    f"对话序号不在 1-{len(flow.sessions)} 范围内", "输入有效序号"
                ),
            )
        target = flow.sessions[index - 1]
        title = target.title or "未命名"
        target.is_deleted = True
        await session.commit()
        logger.info(f"用户删除了对话 #{target.id}「{title}」")
        flow.sessions = await get_user_sessions(session, flow.user_id)
        await matcher.reject_arg(
            _CHOICE_ARG, f"已删除对话「{title}」\n\n{chat_list_text(flow.sessions)}"
        )
    if text.isdigit():
        index = int(text)
        if not 1 <= index <= len(flow.sessions):
            await matcher.reject_arg(
                _CHOICE_ARG,
                validation_failed(
                    f"对话序号不在 1-{len(flow.sessions)} 范围内", "输入有效序号"
                ),
            )
        flow.current_session = flow.sessions[index - 1]
        flow.view = PanelView.CHAT_DETAIL
        messages = await get_session_messages(session, flow.current_session.id)
        await matcher.reject_arg(
            _CHOICE_ARG, chat_detail_text(flow.current_session, messages)
        )
    await matcher.reject_arg(
        _CHOICE_ARG,
        validation_failed("没有识别到对话操作", "输入对话序号或「删除 <序号>」"),
    )


async def _route_chat_detail(
    matcher: Matcher,
    session: async_scoped_session,
    flow: PanelFlow,
    text: str,
) -> None:
    parts = text.split()
    if len(parts) == _COMMAND_PARTS and parts[0] == "删除消息" and parts[1].isdigit():
        message_id = int(parts[1])
        message = await session.get(ChatMessage, message_id)
        if message is None or message.is_deleted:
            await matcher.reject_arg(
                _CHOICE_ARG,
                validation_failed("没有找到这条消息", "检查消息 ID 后重试"),
            )
        assert flow.current_session is not None
        if message.session_id != flow.current_session.id:
            await matcher.reject_arg(
                _CHOICE_ARG,
                validation_failed("这条消息不属于当前对话", "输入当前页中的消息 ID"),
            )
        message.is_deleted = True
        await session.commit()
        messages = await get_session_messages(session, flow.current_session.id)
        await matcher.reject_arg(
            _CHOICE_ARG,
            f"已删除消息 #{message_id}\n\n"
            + chat_detail_text(flow.current_session, messages),
        )
    await matcher.reject_arg(
        _CHOICE_ARG,
        validation_failed("没有识别到详情页操作", "输入「删除消息 <ID>」"),
    )


async def render_admin_menu(
    bot: Bot,
    session: async_scoped_session,
    flow: AdminFlow,
) -> str:
    if flow.view is PanelView.USER_FEATURE and flow.target_user_id is not None:
        statuses = await get_user_feature_status(
            flow.target_user_id, flow.group_id, session
        )
        return user_feature_text(flow.target_user_id, statuses)
    data = await get_admin_panel_data(session, bot, flow.group_id, flow.group_name)
    return admin_panel_text(data)


async def route_admin_choice(  # noqa: PLR0913, PLR0917
    bot: Bot,
    matcher: Matcher,
    session: async_scoped_session,
    flow: AdminFlow,
    text: str,
    intent: SessionIntent,
) -> None:
    if intent is SessionIntent.MENU:
        menu = await render_admin_menu(bot, session, flow)
        await matcher.reject_arg(_ADMIN_ARG, menu)
    if intent is SessionIntent.BACK:
        if flow.view is PanelView.MAIN:
            await matcher.finish("已退出群管理面板。")
        flow.view = PanelView.MAIN
        flow.target_user_id = None
        menu = await render_admin_menu(bot, session, flow)
        await matcher.reject_arg(_ADMIN_ARG, menu)
    if flow.view is PanelView.USER_FEATURE:
        await _route_user_feature(matcher, session, flow, text)
    await _route_admin_main(bot, matcher, session, flow, text)


async def _route_admin_main(
    bot: Bot,
    matcher: Matcher,
    session: async_scoped_session,
    flow: AdminFlow,
    text: str,
) -> None:
    parts = text.split()
    if len(parts) == _COMMAND_PARTS and parts[0] == "用户" and parts[1].isdigit():
        flow.target_user_id = int(parts[1])
        await ensure_scope_records(
            session, user_id=flow.target_user_id, group_id=flow.group_id
        )
        flow.view = PanelView.USER_FEATURE
        menu = await render_admin_menu(bot, session, flow)
        await matcher.reject_arg(_ADMIN_ARG, menu)
    if text.isdigit():
        features = list_features()
        index = int(text)
        if not 1 <= index <= len(features):
            await matcher.reject_arg(
                _ADMIN_ARG,
                validation_failed(
                    f"功能序号不在 1-{len(features)} 范围内", "输入有效序号"
                ),
            )
        key, display = features[index - 1]
        record = await session.get(
            GroupFeature, {"group_id": flow.group_id, "feature": key}
        )
        enabled = not record.enabled if record is not None else False
        if record is None:
            session.add(
                GroupFeature(group_id=flow.group_id, feature=key, enabled=enabled)
            )
        else:
            record.enabled = enabled
        await session.commit()
        await matcher.reject_arg(
            _ADMIN_ARG,
            f"已{'开启' if enabled else '关闭'}功能「{display}」\n\n"
            + await render_admin_menu(bot, session, flow),
        )
    await matcher.reject_arg(
        _ADMIN_ARG,
        validation_failed("没有识别到群管理操作", "输入功能序号或「用户 <QQ号>」"),
    )


async def _route_user_feature(
    matcher: Matcher,
    session: async_scoped_session,
    flow: AdminFlow,
    text: str,
) -> None:
    parts = text.split()
    if not (
        len(parts) == _COMMAND_PARTS
        and parts[0] in {"开启", "关闭"}
        and parts[1].isdigit()
    ):
        await matcher.reject_arg(
            _ADMIN_ARG,
            validation_failed(
                "用户功能操作格式不正确", "输入「开启 <序号>」或「关闭 <序号>」"
            ),
        )
    features = list_features()
    index = int(parts[1])
    if not 1 <= index <= len(features):
        await matcher.reject_arg(
            _ADMIN_ARG,
            validation_failed(f"功能序号不在 1-{len(features)} 范围内", "输入有效序号"),
        )
    assert flow.target_user_id is not None
    key, display = features[index - 1]
    enabled = parts[0] == "开启"
    await ensure_scope_records(
        session, user_id=flow.target_user_id, group_id=flow.group_id
    )
    record = await session.get(
        UserFeature,
        {
            "group_id": flow.group_id,
            "user_id": flow.target_user_id,
            "feature": key,
        },
    )
    if record is None:
        session.add(
            UserFeature(
                group_id=flow.group_id,
                user_id=flow.target_user_id,
                feature=key,
                enabled=enabled,
            )
        )
    else:
        record.enabled = enabled
    await session.commit()
    statuses = await get_user_feature_status(
        flow.target_user_id, flow.group_id, session
    )
    status = "开启" if enabled else "关闭"
    await matcher.reject_arg(
        _ADMIN_ARG,
        f"已为用户 {flow.target_user_id} {status}功能「{display}」\n\n"
        + user_feature_text(flow.target_user_id, statuses),
    )


__all__ = [
    "render_admin_menu",
    "render_panel_menu",
    "route_admin_choice",
    "route_panel_choice",
]
