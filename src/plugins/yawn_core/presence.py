import asyncio
from datetime import datetime, timedelta, timezone

from nonebot import get_bot, logger
from nonebot.adapters import Event
from nonebot.message import event_preprocessor
from nonebot_plugin_orm import async_scoped_session
from sqlalchemy.exc import IntegrityError, OperationalError

from .data_models.bot_group import BotGroup
from .data_models.bot_user import BotUser
from .data_models.user_group import UserGroup

_CST = timezone(timedelta(hours=8))
_MAX_COMMIT_ATTEMPTS = 3


@event_preprocessor
async def track_user(  # noqa: C901, PLR0915
    event: Event, session: async_scoped_session
) -> None:
    if event.get_type() != "message":
        return

    user_id = int(event.get_user_id())
    now = datetime.now(_CST).replace(tzinfo=None)
    sender = getattr(event, "sender", None)
    nickname: str | None = None
    if sender is not None:
        nickname = getattr(sender, "card", None) or getattr(sender, "nickname", None)
    group_id_raw = getattr(event, "group_id", None)
    group_id = int(group_id_raw) if group_id_raw is not None else None

    async def prepare() -> None:
        bot_user = await session.get(BotUser, user_id)
        if bot_user is None:
            session.add(
                BotUser(
                    user_id=user_id,
                    nickname=nickname,
                    first_interaction_at=now,
                    last_interaction_at=now,
                )
            )
        else:
            bot_user.last_interaction_at = now
            if nickname:
                bot_user.nickname = nickname

        if group_id is None:
            return
        bot_group = await session.get(BotGroup, group_id)
        if bot_group is None:
            group_name: str | None = None
            try:
                info = await get_bot().call_api("get_group_info", group_id=group_id)
                group_name = info.get("group_name")
            except Exception:  # noqa: BLE001
                logger.warning(f"无法获取群 {group_id} 信息")
            session.add(
                BotGroup(
                    group_id=group_id,
                    group_name=group_name,
                    first_seen_at=now,
                    last_active_at=now,
                )
            )
        else:
            bot_group.last_active_at = now
            if bot_group.group_name is None:
                try:
                    info = await get_bot().call_api("get_group_info", group_id=group_id)
                    bot_group.group_name = info.get("group_name")
                except Exception:  # noqa: BLE001
                    pass

        user_group = await session.get(UserGroup, (group_id, user_id))
        if user_group is None:
            session.add(
                UserGroup(
                    group_id=group_id,
                    user_id=user_id,
                    first_seen_at=now,
                    last_seen_at=now,
                    group_nickname=nickname,
                )
            )
        else:
            user_group.last_seen_at = now
            if nickname:
                user_group.group_nickname = nickname

    for attempt in range(_MAX_COMMIT_ATTEMPTS):
        try:
            await prepare()
            await session.commit()
            break
        except (IntegrityError, OperationalError):
            # rollback expires/discards pending objects, so the next attempt
            # must repeat all reads and additions rather than commit an empty unit.
            await session.rollback()
            if attempt == _MAX_COMMIT_ATTEMPTS - 1:
                logger.warning(f"track_user: 数据库写入失败（用户 {user_id}），已跳过")
                return
            await asyncio.sleep(0.1 * (attempt + 1))
