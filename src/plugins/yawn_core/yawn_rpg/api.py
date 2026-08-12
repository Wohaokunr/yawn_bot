"""OneBot V11 API 的安全封装（跑团版）。

群消息、私聊、成员查询等调用全部 try/except 降级为
logger.warning，API 异常不打断游戏流程。跑团不禁言群成员
（自由发言是输入通道），故无禁言系列封装。
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
) -> bool:
    """发送群消息，返回是否投递成功；失败仅记录日志。"""
    try:
        await bot.send_group_msg(group_id=group_id, message=message)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"跑团群 {group_id} 发送群消息失败: {e!r}")
        return False
    else:
        return True


async def send_dm(
    bot: Bot,
    user_id: int,
    message: Union[str, "Message"],
) -> bool:
    """发送私聊消息，返回是否投递成功。"""
    try:
        await bot.send_private_msg(user_id=user_id, message=message)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"跑团私聊 {user_id} 投递失败: {e!r}")
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
        logger.warning(f"跑团群 {group_id} 查询机器人成员信息失败: {e!r}")
        return False
