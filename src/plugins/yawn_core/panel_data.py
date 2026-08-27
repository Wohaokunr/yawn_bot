"""管理面板的数据查询与只读视图组装。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from nonebot import logger
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from .data_models.bot_group import BotGroup
from .data_models.bot_user import BotUser
from .data_models.chat_message import ChatMessage
from .data_models.chat_session import ChatSession
from .data_models.checkin_user import CheckinUser
from .data_models.group_feature import GroupFeature
from .data_models.user_group import UserGroup
from .permission import get_user_feature_status, list_features
from .ui.panel_renderer import PanelFeature, PanelStat, PersonalPanelView

if TYPE_CHECKING:
    from datetime import datetime

    from nonebot.adapters import Bot
    from nonebot_plugin_orm import async_scoped_session


@dataclass(frozen=True, slots=True)
class GroupListItem:
    group_id: int
    group_name: str | None
    is_admin: bool
    last_active_at: datetime | None
    first_seen_at: datetime | None
    last_seen_at: datetime | None


@dataclass(frozen=True, slots=True)
class AdminPanelData:
    group_id: int
    group_name: str | None
    member_count: int | None
    tracked_users: int
    last_active_at: datetime | None
    features: tuple[tuple[str, bool], ...]


async def ensure_scope_records(
    session: async_scoped_session,
    *,
    user_id: int,
    group_id: int | None = None,
) -> None:
    """Ensure administration targets have the ORM parents required by FKs."""

    if await session.get(BotUser, user_id) is None:
        session.add(BotUser(user_id=user_id))
    if group_id is None:
        await session.flush()
        return
    if await session.get(BotGroup, group_id) is None:
        session.add(BotGroup(group_id=group_id))
        await session.flush()
    if await session.get(UserGroup, (group_id, user_id)) is None:
        session.add(UserGroup(group_id=group_id, user_id=user_id))
    await session.flush()


async def get_group_name(session: async_scoped_session, group_id: int) -> str | None:
    group = await session.get(BotGroup, group_id)
    return group.group_name if group else None


async def ensure_group_record(session: async_scoped_session, group_id: int) -> None:
    if await session.get(BotGroup, group_id) is None:
        session.add(BotGroup(group_id=group_id))
        await session.flush()


async def get_user_sessions(
    session: async_scoped_session,
    user_id: int,
    group_id: int | None = None,
) -> list[ChatSession]:
    result = await session.execute(
        select(ChatSession)
        .where(
            ChatSession.user_id == user_id,
            ChatSession.group_id == group_id,
            ChatSession.is_deleted == False,  # noqa: E712
        )
        .order_by(ChatSession.updated_at.desc().nullslast())
    )
    return list(result.scalars().all())


async def get_session_messages(
    session: async_scoped_session, session_id: int
) -> list[ChatMessage]:
    result = await session.execute(
        select(ChatMessage)
        .where(
            ChatMessage.session_id == session_id,
            ChatMessage.is_deleted == False,  # noqa: E712
        )
        .order_by(ChatMessage.id.asc())
    )
    return list(result.scalars().all())


async def list_user_groups(
    bot: Bot,
    session: async_scoped_session,
    user_id: int,
) -> tuple[GroupListItem, ...]:
    result = await session.execute(
        select(UserGroup)
        .options(selectinload(UserGroup.group))
        .where(UserGroup.user_id == user_id)
        .order_by(UserGroup.group_id)
    )
    items: list[GroupListItem] = []
    for user_group in result.scalars().all():
        group = user_group.group
        try:
            info = await bot.call_api(
                "get_group_member_info",
                group_id=group.group_id,
                user_id=user_id,
            )
            is_admin = info.get("role", "member") in {"owner", "admin"}
        except Exception:  # noqa: BLE001
            logger.warning(f"获取用户 {user_id} 在群 {group.group_id} 的角色失败")
            is_admin = False
        items.append(
            GroupListItem(
                group_id=group.group_id,
                group_name=group.group_name,
                is_admin=is_admin,
                last_active_at=group.last_active_at,
                first_seen_at=user_group.first_seen_at,
                last_seen_at=user_group.last_seen_at,
            )
        )
    return tuple(items)


async def _count_user_ai_sessions(
    session: async_scoped_session,
    user_id: int,
    *,
    group_id: int | None = None,
) -> int:
    statement = (
        select(func.count())
        .select_from(ChatSession)
        .where(ChatSession.user_id == user_id, ChatSession.is_deleted == False)  # noqa: E712
    )
    if group_id is not None:
        statement = statement.where(ChatSession.group_id == group_id)
    return int(await session.scalar(statement) or 0)


def _format_time(value: datetime | None) -> str:
    return f"{value:%Y-%m-%d %H:%M}" if value else "暂无"


async def build_group_personal_view(
    session: async_scoped_session,
    user_id: int,
    group_id: int,
    group_name: str | None,
) -> PersonalPanelView:
    bot_user = await session.get(BotUser, user_id)
    user_group = await session.get(UserGroup, (group_id, user_id))
    checkin = await session.get(CheckinUser, (group_id, user_id))
    statuses = await get_user_feature_status(user_id, group_id, session)
    group_count = int(
        await session.scalar(
            select(func.count())
            .select_from(UserGroup)
            .where(UserGroup.user_id == user_id)
        )
        or 0
    )
    ai_count = await _count_user_ai_sessions(session, user_id, group_id=group_id)
    last_active = (
        user_group.last_seen_at
        if user_group and user_group.last_seen_at
        else bot_user.last_interaction_at
        if bot_user
        else None
    )
    return PersonalPanelView(
        user_id=user_id,
        nickname=(bot_user.nickname if bot_user else None) or f"QQ {user_id}",
        mode_label="群聊模式",
        subtitle=group_name or f"群 {group_id}",
        last_active=_format_time(last_active),
        avatar_url=f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640",
        stats=(
            PanelStat("累计签到", f"{checkin.total_days if checkin else 0} 天"),
            PanelStat("积分", str(checkin.points if checkin else 0)),
            PanelStat("活跃群", f"{group_count} 个"),
            PanelStat("AI 对话", f"{ai_count} 个", "当前群"),
            PanelStat("群经验", str(user_group.exp if user_group else 0)),
            PanelStat("金币", str(user_group.coins if user_group else 0)),
        ),
        features=tuple(
            PanelFeature(display, enabled, source)
            for _key, display, enabled, source in statuses[:6]
        ),
        actions=("功能 <序号> 查看详情", "菜单 重新显示", "0 退出"),
    )


async def build_private_personal_view(
    session: async_scoped_session, user_id: int
) -> PersonalPanelView:
    bot_user = await session.get(BotUser, user_id)
    total_days = int(
        await session.scalar(
            select(func.coalesce(func.sum(CheckinUser.total_days), 0)).where(
                CheckinUser.user_id == user_id
            )
        )
        or 0
    )
    total_points = int(
        await session.scalar(
            select(func.coalesce(func.sum(CheckinUser.points), 0)).where(
                CheckinUser.user_id == user_id
            )
        )
        or 0
    )
    group_count = int(
        await session.scalar(
            select(func.count())
            .select_from(UserGroup)
            .where(UserGroup.user_id == user_id)
        )
        or 0
    )
    statuses = await get_user_feature_status(user_id, None, session)
    ai_count = await _count_user_ai_sessions(session, user_id)
    return PersonalPanelView(
        user_id=user_id,
        nickname=(bot_user.nickname if bot_user else None) or f"QQ {user_id}",
        mode_label="个人模式",
        subtitle="你的 YawnBot 使用概览",
        last_active=_format_time(bot_user.last_interaction_at if bot_user else None),
        avatar_url=f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640",
        stats=(
            PanelStat("累计签到", f"{total_days} 天"),
            PanelStat("总积分", str(total_points)),
            PanelStat("活跃群", f"{group_count} 个"),
            PanelStat("AI 对话", f"{ai_count} 个"),
            PanelStat("好感度", str(bot_user.affinity if bot_user else 0)),
            PanelStat(
                "首次互动",
                _format_time(bot_user.first_interaction_at if bot_user else None),
            ),
        ),
        features=tuple(
            PanelFeature(display, enabled, source)
            for _key, display, enabled, source in statuses[:6]
        ),
        actions=("1 我的群聊", "2 对话管理", "菜单 重新显示", "0 退出"),
    )


async def get_admin_panel_data(
    session: async_scoped_session,
    bot: Bot,
    group_id: int,
    group_name: str | None,
) -> AdminPanelData:
    member_count: int | None = None
    try:
        info = await bot.call_api("get_group_info", group_id=group_id)
        member_count = info.get("member_count")
    except Exception:  # noqa: BLE001
        logger.debug(f"获取群 {group_id} 成员数失败", exc_info=True)
    tracked = int(
        await session.scalar(
            select(func.count())
            .select_from(UserGroup)
            .where(UserGroup.group_id == group_id)
        )
        or 0
    )
    group = await session.get(BotGroup, group_id)
    features: list[tuple[str, bool]] = []
    for key, display in list_features():
        record = await session.get(GroupFeature, {"group_id": group_id, "feature": key})
        features.append((display, record.enabled if record is not None else True))
    return AdminPanelData(
        group_id=group_id,
        group_name=group_name,
        member_count=member_count,
        tracked_users=tracked,
        last_active_at=group.last_active_at if group else None,
        features=tuple(features),
    )


__all__ = [
    "AdminPanelData",
    "GroupListItem",
    "build_group_personal_view",
    "build_private_personal_view",
    "ensure_group_record",
    "ensure_scope_records",
    "get_admin_panel_data",
    "get_group_name",
    "get_session_messages",
    "get_user_sessions",
    "list_user_groups",
]
