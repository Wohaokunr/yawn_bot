from nonebot import on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
from nonebot.dependencies import Dependent
from nonebot_plugin_orm import async_scoped_session

from .data_models.bot_user import BotUser
from .data_models.user_group import UserGroup
from .permission import require_feature

info = on_command(
    "info", aliases={"个人信息", "我的信息", "用户信息"}, priority=5, block=True
)


@info.handle()
async def handle_info(
    event: GroupMessageEvent,
    session: async_scoped_session,
    _perm: Dependent = require_feature("info"),
) -> None:
    user_id = event.user_id
    group_id = event.group_id

    # 从数据库读取用户信息
    bot_user = await session.get(BotUser, user_id)
    user_group = await session.get(UserGroup, (group_id, user_id))

    lines: list[str] = ["═══ 个人信息 ═══"]

    if bot_user:
        lines.append(f"昵称: {bot_user.nickname or '未知'}")
        lines.append(f"好感度: {bot_user.affinity}")
        lines.append(f"首次对话: {bot_user.first_interaction_at:%Y-%m-%d %H:%M}")
        if bot_user.last_interaction_at:
            lines.append(f"最后活跃: {bot_user.last_interaction_at:%Y-%m-%d %H:%M}")

    if user_group:
        lines.append(f"群名片: {user_group.group_nickname or '未设置'}")
        lines.append(f"群好感度: {user_group.group_affinity}")
        lines.append(f"经验: {user_group.exp}  金币: {user_group.coins}")
        lines.append(f"首次发言: {user_group.first_seen_at:%Y-%m-%d %H:%M}")
        if user_group.last_seen_at:
            lines.append(f"最后发言: {user_group.last_seen_at:%Y-%m-%d %H:%M}")

    await info.finish(Message(MessageSegment.text("\n".join(lines))))
