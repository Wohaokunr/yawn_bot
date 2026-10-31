"""签到命令。逻辑在 ``checkin_service``，这里只做事件解析、权限与回复。

- 群里 ``/签到``：在当前群签到。
- 私聊 ``/签到``：一键在你所在、且签到功能开着的群里全部签到（最多
  ``MAX_BATCH_GROUPS`` 个）。
- ``/签到状态``：看自己在各群的签到、连签和好感等级。
"""

from datetime import datetime
from random import randint
from typing import Optional

from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    MessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.plugin import PluginMetadata
from nonebot_plugin_orm import async_scoped_session

from . import affinity
from .checkin_service import (
    CHECKIN_TIMEZONE,
    MAX_BATCH_GROUPS,
    POINTS_MAX,
    POINTS_MIN,
    CheckinResult,
    checkin_overview,
    format_batch_reply,
    format_checkin_reply,
    perform_checkin,
)
from .command_catalog import CommandSpec, PluginCommandGroup, register_command_group
from .permission import check_feature_permission, require_feature

COMMAND_GROUP = register_command_group(
    PluginCommandGroup(
        plugin_id="yawn_core.checkin",
        display_name="签到",
        entrypoint="签到",
        commands=(
            CommandSpec(
                name="签到",
                description="每日签到获取积分、群经验和好感；私聊里一键签到所有群",
                feature="checkin",
                scope="all",
            ),
            CommandSpec(
                name="签到状态",
                aliases=("我的签到",),
                description="查看各群签到、连签和好感等级",
                feature="checkin",
                scope="all",
                display_level="advanced",
            ),
        ),
    )
)

__plugin_meta__ = PluginMetadata(
    name="签到",
    description="每日签到获取积分、群经验和好感",
    usage="群里发送 /签到；私聊发送 /签到 一键签到所有群；/签到状态 查看进度",
    extra={"command_group": COMMAND_GROUP},
)

logger.info("签到模块已加载")

checkin = on_command("签到", priority=5, block=True)
checkin_status = on_command("签到状态", aliases={"我的签到"}, priority=5, block=True)


def _get_group_nickname(event: GroupMessageEvent) -> Optional[str]:
    """获取用户当前群昵称。"""

    card = event.sender.card
    if card:
        return card
    return event.sender.nickname or None


async def _group_checkin(
    event: GroupMessageEvent, session: async_scoped_session
) -> None:
    result = await perform_checkin(
        session,
        group_id=event.group_id,
        user_id=event.user_id,
        nickname=_get_group_nickname(event),
        now=datetime.now(CHECKIN_TIMEZONE),
        points_reward=randint(POINTS_MIN, POINTS_MAX),
    )
    await session.commit()
    if result.ok:
        logger.info(
            f"用户 {event.user_id} 在群 {event.group_id} 签到成功，"
            f"获得 {result.reward} 积分，好感 +{result.affinity_gain}"
        )
    await checkin.finish(
        MessageSegment.at(event.user_id) + format_checkin_reply(result)
    )


async def _private_checkin(
    event: PrivateMessageEvent, session: async_scoped_session
) -> None:
    user_id = int(event.user_id)
    statuses = await checkin_overview(session, user_id)
    results: list[tuple[str, CheckinResult]] = []
    nickname = event.sender.nickname or None
    for status in statuses[:MAX_BATCH_GROUPS]:
        if not await check_feature_permission(
            user_id, status.group_id, "checkin", session
        ):
            continue
        result = await perform_checkin(
            session,
            group_id=status.group_id,
            user_id=user_id,
            nickname=nickname,
            now=datetime.now(CHECKIN_TIMEZONE),
            points_reward=randint(POINTS_MIN, POINTS_MAX),
        )
        results.append((status.group_name or f"群 {status.group_id}", result))
    await session.commit()
    await checkin.finish(format_batch_reply(results))


@checkin.handle()
async def handle_checkin(
    event: MessageEvent,
    session: async_scoped_session,
    _perm: None = require_feature("checkin"),  # pyright: ignore[reportArgumentType]
) -> None:
    if isinstance(event, GroupMessageEvent):
        await _group_checkin(event, session)
    elif isinstance(event, PrivateMessageEvent):
        await _private_checkin(event, session)


def format_status_reply(
    statuses: list, snapshot: affinity.AffinitySnapshot, *, limit: int = 8
) -> str:
    level = snapshot.level
    lines = [f"和我的关系：{level.name}"]
    if level.next_name:
        lines[0] += (
            f"（距离「{level.next_name}」还差 {level.to_next(snapshot.points)}）"
        )
    if not statuses:
        lines.append("还没有在任何群签到过~")
        return "\n".join(lines)
    for item in statuses[:limit]:
        mark = "✅" if item.checked_today else "⬜"
        streak = (
            f"连签 {item.streak_days} 天"
            if item.streak_days
            else "连签已断"
            if item.streak_broken
            else "未连签"
        )
        lines.append(
            f"{mark} {item.group_name or f'群 {item.group_id}'}：{streak}，"
            f"累计 {item.total_days} 天，积分 {item.points}"
        )
    if len(statuses) > limit:
        lines.append(f"……还有 {len(statuses) - limit} 个群")
    return "\n".join(lines)


@checkin_status.handle()
async def handle_checkin_status(
    event: MessageEvent,
    session: async_scoped_session,
    _perm: None = require_feature("checkin"),  # pyright: ignore[reportArgumentType]
) -> None:
    user_id = int(event.get_user_id())
    group_ids = [event.group_id] if isinstance(event, GroupMessageEvent) else None
    statuses = await checkin_overview(session, user_id, group_ids=group_ids)
    snapshot = await affinity.snapshot(session, user_id)
    await checkin_status.finish(format_status_reply(statuses, snapshot))
