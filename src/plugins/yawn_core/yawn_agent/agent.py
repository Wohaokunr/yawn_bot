# ruff: noqa: E501,F401,I001,TID252,PLR0912,PLR0913,PLR0917,C901,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100
"""群聊 Agent 入口：消息监听、触发判定、落库与成员角色同步。

对话主流程（工具循环、多模态降级）在 dialogue.py；配置的
get-or-create 在 config_store.py。
"""

from datetime import timedelta
from typing import Any

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, NoticeEvent
from nonebot.plugin import on_message, on_notice
from nonebot_plugin_orm import async_scoped_session
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from ..data_models.agent_memory import AgentPrivacy
from ..data_models.bot_group import BotGroup
from ..data_models.group_agent_message import GroupAgentMessage
from ..data_models.user_group import UserGroup
from ..permission import check_feature_permission
from .collector import enqueue, ensure_worker
from .config_store import agent_runtime_enabled, get_or_create_config
from .context import now_beijing
from .conversation import observe_member_message
from .dialogue import contains_word, process_group_message
from .log import dbg, dbg_exc
from .message_parser import NormalizedMessage, parse_message

_EXPLICIT_WAKE_WORDS = ("小助手", "群聊agent", "群聊 agent", "yawn")


def should_respond(
    event: GroupMessageEvent,
    bot: Bot,
    trigger_mode: str = "mention_or_proactive",
    *,
    normalized: NormalizedMessage | None = None,
) -> bool:
    """被 @、回复机器人或显式自然语言唤醒时响应。"""

    if int(event.get_user_id()) == int(bot.self_id):
        return False
    self_id = str(bot.self_id)
    # 适配器的 _check_at_me/_check_reply 会把指向机器人的 @ 段(以及 reply 段)
    # 从 event.message 移除并置 to_me,因此 to_me 必须与剩余 at 段一起判断。
    mentioned = bool(getattr(event, "to_me", False)) or any(
        seg.type == "at" and str(seg.data.get("qq")) == self_id for seg in event.message
    )
    reply = getattr(event, "reply", None)
    if reply is not None:
        try:
            replied = int(reply.sender.user_id) == int(bot.self_id)
        except (AttributeError, TypeError, ValueError):
            replied = False
    else:
        replied = False
    if not replied and normalized is not None and normalized.reply_chain:
        # event.reply.sender 缺失或不可用时,用 get_msg 拉取的直接引用作者兜底;
        # 只看第 0 层(直接引用),更深层引用不构成"回复机器人"。
        raw_reply_user = normalized.reply_chain[0].get("user_id")
        if raw_reply_user is not None:
            try:
                replied = int(raw_reply_user) == int(bot.self_id)
            except (TypeError, ValueError):
                replied = False
    text = " ".join(event.get_plaintext().strip().lower().split())
    group_id = getattr(event, "group_id", "?")
    # OneBot 的 at 段在 plaintext 中渲染为 @<昵称>，因此唤醒词跟随机器人昵称。
    words = _EXPLICIT_WAKE_WORDS
    nickname = str(getattr(bot, "nickname", "") or "").strip().lower()
    if nickname:
        words = (*_EXPLICIT_WAKE_WORDS, f"@{nickname}")
    explicit = any(contains_word(text, word) for word in words)
    dbg(
        f"群 {group_id} 触发判定: mentioned={mentioned} replied={replied} "
        f"explicit={explicit} trigger_mode={trigger_mode!r} 文本={text!r}"
    )
    if trigger_mode == "mention_only":
        dbg(f"群 {group_id} mention_only 模式 → 响应={mentioned}")
        return mentioned
    if trigger_mode == "mention_or_reply":
        dbg(f"群 {group_id} mention_or_reply 模式 → 响应={mentioned or replied}")
        return mentioned or replied
    if trigger_mode == "explicit_wakeup":
        dbg(f"群 {group_id} explicit_wakeup 模式 → 响应={mentioned or explicit}")
        return mentioned or explicit
    dbg(f"群 {group_id} 默认模式 → 响应={mentioned or replied or explicit}")
    return mentioned or replied or explicit


async def _persist_message(
    bot: Bot, event: GroupMessageEvent, normalized: NormalizedMessage, session: Any
) -> None:
    group_id = int(event.group_id)
    bot_id = int(bot.self_id)
    message_id = int(getattr(event, "message_id", 0) or 0)
    # 部分 OneBot 实现的群消息 id 为负数,同样有效;仅 0 视为缺失。
    if message_id == 0:
        dbg(f"群 {group_id} 消息缺少 message_id,跳过落库")
        return
    duplicate = await session.scalar(
        select(GroupAgentMessage).where(
            GroupAgentMessage.bot_id == bot_id,
            GroupAgentMessage.message_id == message_id,
        )
    )
    if duplicate is not None:
        dbg(f"群 {group_id} 消息 {message_id} 已落库过,去重跳过")
        return
    sender = event.sender
    config = await get_or_create_config(session, group_id)
    if config is None:
        dbg(f"群 {group_id} 无法取得 Agent 配置,消息 {message_id} 不落库")
        return
    retention = max(1, min(int(config.raw_retention_days), 365))
    stored = normalized.storage_dict()
    session.add(
        GroupAgentMessage(
            bot_id=bot_id,
            message_id=message_id,
            group_id=group_id,
            user_id=int(event.get_user_id()),
            sender_name=sender.card or sender.nickname,
            role=str(sender.role or "member"),
            title=sender.title,
            normalized_text=normalized.plain_text,
            segments=stored.get("segments", []),
            reply_chain=stored.get("reply_chain", []),
            forward_tree=stored.get("forward_tree", []),
            media_refs=stored.get("media_refs", []),
            received_at=now_beijing(),
            expires_at=now_beijing() + timedelta(days=retention),
        )
    )
    group = await session.get(BotGroup, group_id)
    if group is not None:
        group.last_active_at = now_beijing()
    try:
        await session.commit()
    except SQLAlchemyError:
        # 含 SQLite 锁超时等瞬时错误；必须回滚，否则处理器共享的
        # scoped session 会带着待回滚事务毒化后续查询。
        logger.warning("群聊 Agent 消息落库失败: %s", message_id)
        dbg_exc(f"群 {group_id} 消息 {message_id} 落库失败,已回滚")
        await session.rollback()
    else:
        dbg(
            f"群 {group_id} 消息 {message_id} 落库成功: user={int(event.get_user_id())} "
            f"保留天数={retention} 段数={len(stored.get('segments', []))} "
            f"回复链={len(stored.get('reply_chain', []))} 媒体={len(stored.get('media_refs', []))} "
            f"文本={normalized.plain_text!r}"
        )


agent_listener = on_message(priority=8, block=False)
member_notice = on_notice(priority=20, block=False)


@agent_listener.handle()
async def handle_group_agent_message(
    bot: Bot, event: GroupMessageEvent, _session: async_scoped_session
) -> None:
    dbg(
        f"收到群消息: group={getattr(event, 'group_id', None)} "
        f"user={event.get_user_id()} message_id={getattr(event, 'message_id', None)} "
        f"含回复={getattr(event, 'reply', None) is not None} to_me={getattr(event, 'to_me', False)} "
        f"原始消息={str(event.message)!r}"
    )
    if not isinstance(event, GroupMessageEvent) or int(event.get_user_id()) == int(
        bot.self_id
    ):
        dbg("跳过: 非群消息或机器人自身消息")
        return
    config = await get_or_create_config(_session, int(event.group_id))
    # ``GroupAgentConfig.enabled`` 与通用 GroupFeature(group_agent) 共同构成
    # 群级总开关。先做硬门禁，再做用户级权限检查，避免用户 override=True
    # 绕过群管理员已经关闭的 Agent 总开关。
    if config is None or not await agent_runtime_enabled(
        _session, int(event.group_id), config=config
    ):
        dbg(
            f"群 {event.group_id} 跳过: Agent 总开关"
            f"{'配置缺失' if config is None else '已关闭'}"
        )
        return
    # 常驻监听器不走 require_feature 依赖，需要手动接受用户级功能约束；
    # 群级硬开关已经在上面处理，因此这里只允许用户覆盖进一步收紧权限。
    if not await check_feature_permission(
        int(event.get_user_id()), int(event.group_id), "group_agent", _session
    ):
        dbg(f"群 {event.group_id} 跳过: group_agent 用户功能开关关闭")
        return
    privacy = await _session.get(
        AgentPrivacy, (int(event.group_id), int(event.get_user_id()))
    )
    if privacy is not None and privacy.opted_out:
        dbg(f"群 {event.group_id} 跳过: 用户 {event.get_user_id()} 已隐私退出")
        return
    normalized = await parse_message(bot, event.message, reply=event.reply)
    dbg(
        f"群 {event.group_id} 消息解析完成: plain_text={normalized.plain_text!r} "
        f"媒体引用={len(normalized.media_refs)} mentions={normalized.mentions} "
        f"回复链={len(normalized.reply_chain)} 转发树={len(normalized.forward_tree)} "
        f"截断={normalized.truncated}"
    )
    # _persist_message 提交后 config 属性会过期，先在提交前取出运行开关，
    # 避免异步引擎上触发同步惰性加载（MissingGreenlet）。
    trigger_mode = config.trigger_mode
    short_conversation_enabled = bool(config.short_conversation_enabled)
    await _persist_message(bot, event, normalized, _session)
    respond = should_respond(event, bot, trigger_mode, normalized=normalized)
    if short_conversation_enabled:
        observe_member_message(
            int(bot.self_id),
            int(event.group_id),
            user_id=int(event.get_user_id()),
            message_id=int(getattr(event, "message_id", 0) or 0) or None,
            explicit_trigger=respond,
            observed_at=now_beijing(),
        )
    if not respond:
        return
    if not enqueue(int(event.group_id), (bot, event, normalized), int(bot.self_id)):
        logger.warning("群聊 Agent 队列已满: %s", event.group_id)
        dbg(f"群 {event.group_id} 队列已满,消息被丢弃")
        return
    dbg(f"群 {event.group_id} 已入队,等待 worker 处理")
    ensure_worker(int(event.group_id), process_group_message, int(bot.self_id))


@member_notice.handle()
async def handle_member_notice(
    bot: Bot, event: NoticeEvent, session: async_scoped_session
) -> None:
    group_id = getattr(event, "group_id", None)
    user_id = getattr(event, "user_id", None)
    dbg(
        f"收到群通知事件: type={getattr(event, 'notice_type', None)} "
        f"group={group_id} user={user_id}"
    )
    if group_id is None or user_id is None:
        dbg("群通知缺少 group_id/user_id,跳过")
        return
    try:
        group_id = int(group_id)
        user_id = int(user_id)
    except (TypeError, ValueError):
        dbg(f"群通知 group/user id 无法解析: {group_id!r}/{user_id!r}")
        return
    record = await session.get(UserGroup, (group_id, user_id))
    if record is None:
        dbg(f"群 {group_id} 成员 {user_id} 不在 UserGroup 表中,跳过角色同步")
        return
    try:
        info = await bot.call_api(
            "get_group_member_info", group_id=group_id, user_id=user_id
        )
    except Exception:  # noqa: BLE001
        dbg_exc(f"群 {group_id} 获取成员 {user_id} 信息失败,跳过角色同步")
        return
    if isinstance(info, dict):
        record.role = str(info.get("role") or record.role or "member")
        if info.get("title") is not None:
            record.title = str(info["title"])
        if info.get("card"):
            record.group_nickname = str(info["card"])
        record.last_role_sync_at = now_beijing()
        dbg(
            f"群 {group_id} 成员 {user_id} 角色同步: role={record.role!r} "
            f"title={record.title!r} 昵称={record.group_nickname!r}"
        )
        try:
            await session.commit()
        except SQLAlchemyError:
            dbg_exc(f"群 {group_id} 成员角色同步提交失败,已回滚")
            await session.rollback()


__all__ = [
    "agent_listener",
    "handle_group_agent_message",
    "member_notice",
    "process_group_message",
    "should_respond",
]
