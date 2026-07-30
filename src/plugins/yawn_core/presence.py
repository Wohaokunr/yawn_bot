import asyncio
from datetime import datetime, timedelta, timezone

from nonebot import get_bot, logger
from nonebot.adapters import Event
from nonebot.message import event_preprocessor
from nonebot_plugin_orm import async_scoped_session
from sqlalchemy.exc import OperationalError

from .data_models.bot_group import BotGroup
from .data_models.bot_user import BotUser
from .data_models.user_group import UserGroup

# 北京时间 UTC+8
_CST = timezone(timedelta(hours=8))


@event_preprocessor
async def track_user(event: Event, session: async_scoped_session):
    if event.get_type() != "message":
        return

    user_id = int(event.get_user_id())
    now = datetime.now(_CST).replace(tzinfo=None)

    # 提取昵称（OneBot V11: sender.card 为群名片, sender.nickname 为 QQ 昵称）
    sender = getattr(event, "sender", None)
    nickname: str | None = None
    if sender is not None:
        nickname = getattr(sender, "card", None) or getattr(sender, "nickname", None)

    # 1. 确保 BotUser 存在（首次 → 记录"第一次与机器人对话"）
    bot_user = await session.get(BotUser, user_id)
    if bot_user is None:
        bot_user = BotUser(
            user_id=user_id,
            nickname=nickname,
            first_interaction_at=now,
            last_interaction_at=now,
        )
        session.add(bot_user)
        logger.info(f"新用户首次与机器人对话: {user_id}")
    else:
        bot_user.last_interaction_at = now
        if nickname:
            bot_user.nickname = nickname

    # 2. 如果是群聊消息，确保 BotGroup + UserGroup 存在
    #    （首次 → 记录"第一次群聊内发言"）
    group_id = getattr(event, "group_id", None)
    if group_id is not None:
        group_id = int(group_id)

        bot_group = await session.get(BotGroup, group_id)
        if bot_group is None:
            # 首次见到该群，调用 API 获取群名
            group_name: str | None = None
            try:
                bot = get_bot()
                info = await bot.call_api(
                    "get_group_info", group_id=group_id
                )
                group_name = info.get("group_name")
            except Exception:
                logger.warning(f"获取群 {group_id} 信息失败")
            bot_group = BotGroup(
                group_id=group_id,
                group_name=group_name,
                first_seen_at=now,
                last_active_at=now,
            )
            session.add(bot_group)
        else:
            bot_group.last_active_at = now
            # 补填旧记录中缺失的群名
            if bot_group.group_name is None:
                try:
                    bot = get_bot()
                    ginfo = await bot.call_api(
                        "get_group_info", group_id=group_id
                    )
                    bot_group.group_name = ginfo.get(
                        "group_name"
                    )
                except Exception:
                    pass

        user_group = await session.get(UserGroup, (group_id, user_id))
        if user_group is None:
            user_group = UserGroup(
                group_id=group_id,
                user_id=user_id,
                first_seen_at=now,
                last_seen_at=now,
                group_nickname=nickname,
            )
            session.add(user_group)
            logger.info(f"用户 {user_id} 首次在群 {group_id} 发言")
        else:
            user_group.last_seen_at = now
            if nickname:
                user_group.group_nickname = nickname

    # 重试提交，防止 SQLite 并发写锁导致 OperationalError
    for attempt in range(3):
        try:
            await session.commit()
            break
        except OperationalError:
            await session.rollback()
            if attempt == 2:
                logger.warning(
                    f"track_user: 数据库写入失败(用户 {user_id})，已跳过"
                )
                return
            await asyncio.sleep(0.1 * (attempt + 1))
