"""OneBot V11 API 的安全封装。

禁言、私聊、群成员查询等调用全部 try/except 降级为
logger.warning，API 异常（机器人非群管、协议端限制等）
不打断游戏流程。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Union

from nonebot import logger

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import Bot, Message


async def safe_group_msg(
    bot: Bot,
    group_id: int,
    message: Union[str, "Message"],
) -> None:
    """发送群消息；失败仅记录日志。"""
    try:
        await bot.send_group_msg(group_id=group_id, message=message)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"狼人杀群 {group_id} 发送群消息失败: {e!r}")


async def safe_ban(
    bot: Bot,
    group_id: int,
    user_id: int,
    duration: int,
) -> None:
    """禁言指定成员 duration 秒；duration=0 为解除。"""
    try:
        await bot.set_group_ban(
            group_id=group_id,
            user_id=user_id,
            duration=duration,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"狼人杀群 {group_id} 禁言 {user_id} 失败"
            f"（机器人可能需要管理员权限）: {e!r}"
        )


async def safe_unban(bot: Bot, group_id: int, user_id: int) -> None:
    """解除指定成员的禁言。"""
    await safe_ban(bot, group_id, user_id, 0)


async def safe_whole_ban(bot: Bot, group_id: int, *, enable: bool) -> None:
    """开启/关闭全员禁言。"""
    try:
        await bot.set_group_whole_ban(group_id=group_id, enable=enable)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"狼人杀群 {group_id} 切换全员禁言({enable})失败: {e!r}")


async def send_dm(
    bot: Bot,
    user_id: int,
    message: Union[str, "Message"],
) -> bool:
    """发送私聊消息，返回是否投递成功。"""
    try:
        await bot.send_private_msg(user_id=user_id, message=message)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"狼人杀私聊 {user_id} 投递失败: {e!r}")
        return False
    else:
        return True


async def is_bot_admin(bot: Bot, group_id: int) -> bool:
    """检查机器人自身是否为群主/管理员；查询失败按非管理员处理。"""
    try:
        info = await bot.get_group_member_info(
            group_id=group_id,
            user_id=int(bot.self_id),
        )
        return info.get("role") in ("owner", "admin")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"狼人杀群 {group_id} 查询机器人成员信息失败: {e!r}")
        return False


async def cleanup_group(
    bot: Bot,
    group_id: int,
    user_ids: list[int],
) -> None:
    """对局结束/异常时的群状态清理：关闭全员禁言并解禁所有玩家。"""
    await safe_whole_ban(bot, group_id, enable=False)
    for uid in user_ids:
        await safe_unban(bot, group_id, uid)


async def unban_all_members(bot: Bot, group_id: int) -> None:
    """恢复命令用：关闭全员禁言并解禁全体群成员。"""
    await safe_whole_ban(bot, group_id, enable=False)
    try:
        members = await bot.get_group_member_list(group_id=group_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"狼人杀群 {group_id} 获取成员列表失败: {e!r}")
        return
    for member in members:
        uid = member.get("user_id")
        if uid is not None:
            await safe_unban(bot, group_id, int(uid))
