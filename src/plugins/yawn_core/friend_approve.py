from datetime import datetime, timedelta, timezone

from nonebot import get_bot, get_driver
from nonebot.adapters.onebot.v11 import (
    Bot,
    FriendRequestEvent,
    Message,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.log import logger
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata, on_command, on_request
from nonebot_plugin_orm import async_scoped_session
from sqlalchemy import select

from .data_models.friend_request import FriendRequest

_CST = timezone(timedelta(hours=8))

__plugin_meta__ = PluginMetadata(
    name="好友审批",
    description="好友申请审批管理",
    usage="发送 /pending 查看待审批列表",
    extra={
        "commands": [
            {
                "name": "approve",
                "aliases": ["同意"],
                "description": "同意好友申请",
                "feature": None,
                "scope": "all",
                "superuser": True,
            },
            {
                "name": "reject",
                "aliases": ["拒绝"],
                "description": "拒绝好友申请",
                "feature": None,
                "scope": "all",
                "superuser": True,
            },
            {
                "name": "pending",
                "aliases": ["待审批"],
                "description": "查看待审批好友申请列表",
                "feature": None,
                "scope": "all",
                "superuser": True,
            },
        ],
    },
)

superusers = get_driver().config.superusers
num = int(next(iter(superusers)))
friend_request= on_request()

approve_cmd = on_command("approve", aliases={"同意"}, priority=1, block=True)
reject_cmd = on_command("reject", aliases={"拒绝"}, priority=1, block=True)
list_cmd = on_command("pending", aliases={"待审批"}, priority=1, block=True)

logger.debug(superusers)

@friend_request.handle()
async def handle_friend_request(
    event: FriendRequestEvent,
    session: async_scoped_session,
) -> None:
    bot = get_bot()

    user_id = event.user_id
    flag = event.flag
    comment = event.comment

    logger.info(f"用户 {user_id} 请求加好友，flag: {flag}, comment: {comment}")

    # 同一用户只保留一行，新申请会覆盖旧申请（QQ号是申请表的主键）
    record = await session.get(FriendRequest, int(user_id))
    if record is None:
        record = FriendRequest(user_id=int(user_id))
        session.add(record)
    record.flag = flag
    record.comment = comment or None
    record.status = "pending"
    record.processed_at = None
    await session.commit()

    message = Message(
        MessageSegment.text("有新的好友申请\n")+
        MessageSegment.text("---------------\n")+
        MessageSegment.text(f"|用户： {user_id} \n")+
        MessageSegment.text(f"|验证信息: {comment}\n")+
        MessageSegment.text("---------------\n")+
        MessageSegment.text(f"/approve {user_id} 同意 | /reject {user_id} 拒绝\n")
        )

    await bot.send_private_msg(user_id= num, message=message)


@list_cmd.handle()
async def handle_list(
    event: PrivateMessageEvent,
    session: async_scoped_session,
) -> None:
    if str(event.user_id) not in superusers:
        return

    stmt = select(FriendRequest).where(
        FriendRequest.status == "pending"
    )
    result = await session.execute(stmt)
    rows = result.scalars().all()

    if not rows:
        await list_cmd.finish("当前没有待审批的好友申请")

    lines = ["待审批好友申请:"]
    for r in rows:
        comment = r.comment or "无"
        lines.append(
            f"  用户: {r.user_id}"
            f"  验证: {comment}"
        )
    lines.append("使用 /approve <QQ号> 同意这个请求 \n 使用/reject <QQ号> 拒绝这个请求")
    await list_cmd.finish("\n".join(lines))


@approve_cmd.handle()
async def handle_approve(
    bot: Bot,
    event: PrivateMessageEvent,
    session: async_scoped_session,
    args: Message = CommandArg(),
) -> None:
    if str(event.user_id) not in superusers:
        return

    text = args.extract_plain_text().strip()
    if not text.isdigit():
        await approve_cmd.finish("请指定QQ号，例如: /approve 123456")

    record = await session.get(FriendRequest, int(text))
    if record is None:
        await approve_cmd.finish(f"未找到用户 {text} 的申请")
    if record.status != "pending":
        await approve_cmd.finish(f"用户 {text} 的申请已处理({record.status})")

    await bot.set_friend_add_request(flag=record.flag, approve=True)
    uid = record.user_id
    record.status = "approved"
    record.processed_at = datetime.now(_CST).replace(tzinfo=None)
    await session.commit()

    logger.info(f"已同意用户 {uid} 的好友申请")
    await approve_cmd.finish(f"已同意用户 {uid} 的好友申请")


@reject_cmd.handle()
async def handle_reject(
    bot: Bot,
    event: PrivateMessageEvent,
    session: async_scoped_session,
    args: Message = CommandArg(),
) -> None:
    if str(event.user_id) not in superusers:
        return

    text = args.extract_plain_text().strip()
    if not text.isdigit():
        await reject_cmd.finish("请指定QQ号，例如: /reject 123456")

    record = await session.get(FriendRequest, int(text))
    if record is None:
        await reject_cmd.finish(f"未找到用户 {text} 的申请")
    if record.status != "pending":
        await reject_cmd.finish(f"用户 {text} 的申请已处理({record.status})")

    await bot.set_friend_add_request(flag=record.flag, approve=False)
    uid = record.user_id
    record.status = "rejected"
    record.processed_at = datetime.now(_CST).replace(tzinfo=None)
    await session.commit()

    logger.info(f"已拒绝用户 {uid} 的好友申请")
    await reject_cmd.finish(f"已拒绝用户 {uid} 的好友申请")
