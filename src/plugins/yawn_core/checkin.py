from datetime import datetime, timedelta
from random import randint
from typing import Optional
from zoneinfo import ZoneInfo

from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    MessageSegment,
)
from nonebot.dependencies import Dependent
from nonebot_plugin_orm import async_scoped_session
from sqlalchemy.exc import IntegrityError

from .data_models.bot_group import BotGroup
from .data_models.bot_user import BotUser
from .data_models.checkin_record import CheckinRecord
from .data_models.checkin_user import CheckinUser
from .data_models.user_group import UserGroup
from .permission import require_feature

logger.info("签到模块已加载")

checkin = on_command(
    "签到",
    priority=5,
    block=True,
)

# 签到日期按照中国时间计算
CHECKIN_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _get_group_nickname(event: GroupMessageEvent) -> Optional[str]:
    """获取用户当前群昵称。"""

    card = event.sender.card
    if card:
        return card
    return event.sender.nickname or None


@checkin.handle()
async def handle_checkin(
    event: GroupMessageEvent,
    session: async_scoped_session,
    _perm: Dependent = require_feature("checkin"),
) -> None:
    now = datetime.now(CHECKIN_TIMEZONE)
    today = now.date()
    yesterday = today - timedelta(days=1)
    group_nickname = _get_group_nickname(event)

    # 每次签到随机获得 5~15 积分
    reward = randint(5, 15)

    user = await session.get(BotUser, event.user_id)
    if user is None:
        user = BotUser(
            user_id=event.user_id,
            nickname=group_nickname,
            last_interaction_at=now,
        )
        session.add(user)
    else:
        user.last_interaction_at = now
        if group_nickname is not None:
            user.nickname = group_nickname

    group = await session.get(BotGroup, event.group_id)
    if group is None:
        group = BotGroup(
            group_id=event.group_id,
            last_active_at=now,
        )
        session.add(group)
    else:
        group.last_active_at = now

    user_group = await session.get(
        UserGroup,
        {
            "group_id": event.group_id,
            "user_id": event.user_id,
        },
    )
    if user_group is None:
        user_group = UserGroup(
            group_id=event.group_id,
            user_id=event.user_id,
            last_seen_at=now,
            group_nickname=group_nickname,
            is_active=True,
        )
        session.add(user_group)
    else:
        user_group.last_seen_at = now
        user_group.is_active = True
        if group_nickname is not None:
            user_group.group_nickname = group_nickname

    await session.flush()

    record = CheckinRecord(
        group_id=event.group_id,
        user_id=event.user_id,
        checkin_date=today,
        reward=reward,
    )
    session.add(record)

    try:
        await session.flush()  # 提前 flush，触发数据库唯一约束检查
    except IntegrityError:
        await session.rollback()
        await checkin.finish("你今天已经签到过了哦~")

    checkin_user = await session.get(
        CheckinUser,
        {
            "group_id": event.group_id,
            "user_id": event.user_id,
        },
    )
    if checkin_user is None:
        checkin_user = CheckinUser(
            group_id=event.group_id,
            user_id=event.user_id,
        )
        session.add(checkin_user)

    checkin_user.total_days += 1
    checkin_user.points += reward
    if checkin_user.last_checkin_date == yesterday:
        checkin_user.streak_days += 1
    else:
        checkin_user.streak_days = 1
    checkin_user.last_checkin_date = today

    logger.info(
        f"用户 {event.user_id} 在群 {event.group_id} 签到成功，获得 {reward} 积分"
    )
    await checkin.finish(
        MessageSegment.at(event.user_id)
        + f"签到成功！你今天获得了 {reward} 积分~\n"
        + f"累计签到 {checkin_user.total_days} 天，"
        + f"连续签到 {checkin_user.streak_days} 天，"
        + f"当前积分 {checkin_user.points}。"
    )
