# ruff: noqa: E501,F401,I001,TID252,PLR0911,PLR0912,PLR0913,PLR0915,PLR0917,PLR2004,C901,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100,SIM117
"""群聊 Agent 入口、上下文构建和共享 LLM 工具循环。"""

import asyncio
import hashlib
import json
import re
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, NoticeEvent
from nonebot.plugin import on_message, on_notice
from nonebot_plugin_orm import async_scoped_session, get_session
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from ..data_models.agent_memory import AgentMemory, AgentPrivacy, AgentRelation
from ..data_models.bot_group import BotGroup
from ..data_models.group_agent_config import GroupAgentConfig
from ..data_models.group_agent_message import GroupAgentMessage
from ..data_models.user_group import UserGroup
from ..llm import (
    LLMMultimodalUnsupportedError,
    agent_multimodal_mode,
    complete,
    complete_with_tools,
    get_agent_model,
    ai_config,
)
from ..permission import check_feature_permission
from .capabilities import probe_group_capabilities, user_can_manage_group
from .collector import (
    enqueue,
    ensure_worker,
    group_lock,
    is_pending_trigger_expired,
)
from .context import ActivitySnapshot, build_context, now_beijing
from .log import dbg, dbg_exc
from .media import get_cached_caption, prepare_image_inputs, store_caption
from .message_parser import NormalizedMessage, parse_message
from .persona import resolve_persona
from .prompt import build_messages, prompt_cache_key
from .tools import MAX_TOOL_ROUNDS, build_tool_schemas, execute_tool

_GREETING_WORDS = ("你好", "嗨", "hello", "hi", "早上好", "晚上好", "在吗", "在不在")
_EXPLICIT_WAKE_WORDS = ("小助手", "群聊agent", "群聊 agent", "yawn")
_PROMPT_CACHE_KEYS: OrderedDict[str, None] = OrderedDict()
_PROMPT_CACHE_LIMIT = 256
_MAX_TURN_SECONDS = 120.0
_SEND_TIMEOUT = 15.0
_FALLBACK_NOTICE = "现在有点忙，稍后再试～"
_TURN_END_NOTICE = "这个话题我先记下了，稍后再继续聊～"


async def _send_group_text(bot: Bot, group_id: int, text: str) -> bool:
    try:
        await asyncio.wait_for(
            bot.call_api("send_group_msg", group_id=group_id, message=Message(text)),
            timeout=_SEND_TIMEOUT,
        )
    except Exception:  # noqa: BLE001
        logger.warning("群聊 Agent 发送群消息失败: %s", group_id)
        dbg_exc(f"群 {group_id} 发送群消息失败 text={text!r}")
        return False
    dbg(f"群 {group_id} 发送群消息成功 text={text!r}")
    return True


def _contains_word(text: str, word: str) -> bool:
    if not word:
        return False
    if re.fullmatch(r"[a-z0-9 ]+", word):
        return (
            re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) is not None
        )
    return word in text


def _is_recent_duplicate(
    item: object,
    input_fingerprint: str,
    response_fingerprint: str,
    now: datetime,
) -> bool:
    if (
        not isinstance(item, dict)
        or item.get("input") != input_fingerprint
        or item.get("response") != response_fingerprint
    ):
        return False
    raw_at = item.get("at")
    if not raw_at:
        return True
    try:
        return now - datetime.fromisoformat(str(raw_at)) < timedelta(minutes=10)
    except (TypeError, ValueError):
        return False


async def _config(session: Any, group_id: int) -> GroupAgentConfig | None:
    record = await session.get(GroupAgentConfig, group_id)
    if record is None:
        record = GroupAgentConfig(group_id=group_id)
        session.add(record)
        try:
            await session.flush()
        except IntegrityError:
            # block=False 监听器下同一群可能并发创建；输的一方重新读取。
            dbg(f"群 {group_id} Agent 配置并发创建竞态,回滚后重新读取")
            await session.rollback()
            record = await session.get(GroupAgentConfig, group_id)
        else:
            dbg(f"群 {group_id} 新建 Agent 配置记录")
    return record


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
    explicit = any(_contains_word(text, word) for word in words)
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


def _deterministic_reply(text: str) -> str | None:
    """无 AI key 时仅对简单问候给出稳定反馈。"""

    normalized = " ".join(text.lower().split())
    if any(_contains_word(normalized, word) for word in _GREETING_WORDS):
        return "我在呀，有事直接说～"
    if "agent状态" in normalized or "群聊agent" in normalized:
        return "群聊 Agent 在线；复杂对话需要配置 AI_API_KEY。"
    return None


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
    config = await _config(session, group_id)
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


async def _load_context(
    session: Any, group_id: int, config: GroupAgentConfig, bot_id: int | None = None
) -> dict[str, Any]:
    now = now_beijing()
    # 隐私退出是读路径级别的：历史消息同样不得进入提示词。
    opted_out = set(
        (
            await session.execute(
                select(AgentPrivacy.user_id).where(
                    AgentPrivacy.group_id == group_id,
                    AgentPrivacy.opted_out.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    dbg(
        f"群 {group_id} 加载上下文: 隐私退出用户数={len(opted_out)} ids={sorted(opted_out)}"
    )
    message_stmt = select(GroupAgentMessage).where(
        GroupAgentMessage.group_id == group_id,
        (
            GroupAgentMessage.expires_at.is_(None)
            | (GroupAgentMessage.expires_at >= now)
        ),
    )
    if opted_out:
        message_stmt = message_stmt.where(GroupAgentMessage.user_id.not_in(opted_out))
    if bot_id is not None:
        message_stmt = message_stmt.where(GroupAgentMessage.bot_id == bot_id)
    rows = (
        (
            await session.execute(
                message_stmt.order_by(GroupAgentMessage.id.desc()).limit(40)
            )
        )
        .scalars()
        .all()
    )
    messages = [
        {
            "user_id": row.user_id,
            "name": row.sender_name,
            "role": row.role,
            "title": row.title,
            "text": row.normalized_text,
        }
        for row in reversed(rows)
    ]
    dbg(f"群 {group_id} 加载上下文: 历史消息 {len(messages)} 条(上限 40)")
    member_rows = (
        (
            await session.execute(
                select(UserGroup).where(UserGroup.group_id == group_id).limit(100)
            )
        )
        .scalars()
        .all()
    )
    members = [
        {
            "user_id": row.user_id,
            "name": row.group_nickname,
            "role": row.role,
            "title": row.title,
            "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
        }
        for row in member_rows
    ]
    dbg(f"群 {group_id} 加载上下文: 成员 {len(members)} 人(上限 100)")
    memory_stmt = (
        select(AgentMemory)
        .where(
            AgentMemory.group_id == group_id,
            AgentMemory.visibility.in_(("group", "public")),
            (AgentMemory.expires_at.is_(None) | (AgentMemory.expires_at >= now)),
        )
        .order_by(AgentMemory.salience.desc(), AgentMemory.updated_at.desc())
        .limit(30)
    )
    memory_rows = (await session.execute(memory_stmt)).scalars().all()
    memories = [
        {
            "type": row.memory_type,
            "subject_user_id": row.subject_user_id,
            "key": row.memory_key,
            "content": row.content,
            "salience": row.salience,
            "confidence": row.confidence,
        }
        for row in memory_rows
    ]
    dbg(f"群 {group_id} 加载上下文: 记忆 {len(memories)} 条(上限 30,按 salience 排序)")
    relation_rows = (
        (
            await session.execute(
                select(AgentRelation)
                .where(AgentRelation.group_id == group_id)
                .order_by(AgentRelation.confidence.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    relations = [
        {
            "subject_user_id": row.subject_user_id,
            "object_user_id": row.object_user_id,
            "type": row.relation_type,
            "confidence": row.confidence,
            "evidence_count": row.evidence_count,
        }
        for row in relation_rows
    ]
    dbg(f"群 {group_id} 加载上下文: 关系 {len(relations)} 条(上限 50)")
    # 活跃度统计按 60 分钟时间窗过滤；rows 只是保留期内的最新 40 条。
    window_start = now - timedelta(hours=1)
    in_window = [row for row in rows if row.received_at >= window_start]
    activity = ActivitySnapshot(
        rows[0].received_at if rows else None,
        messages_5m=sum(
            (now - row.received_at).total_seconds() < 300 for row in in_window
        ),
        messages_20m=sum(
            (now - row.received_at).total_seconds() < 1200 for row in in_window
        ),
        messages_60m=len(in_window),
        participants_60m=len({row.user_id for row in in_window}),
        replies_60m=sum(bool(row.reply_chain) for row in in_window),
        mentions_60m=sum("@" in row.normalized_text for row in in_window),
        last_agent_at=config.last_agent_at,
        proactive_today=config.proactive_count,
    )
    dbg(
        f"群 {group_id} 活跃度快照: 5m={activity.messages_5m} 20m={activity.messages_20m} "
        f"60m={activity.messages_60m} 参与人数={activity.participants_60m} "
        f"回复数={activity.replies_60m} 提及数={activity.mentions_60m} "
        f"今日主动发言={activity.proactive_today} 最后消息={activity.last_message_at} "
        f"最后发言={activity.last_agent_at}"
    )
    group = await session.get(BotGroup, group_id)
    dbg(f"群 {group_id} 上下文组装完成: 群名={group.group_name if group else None!r}")
    return build_context(
        group_id=group_id,
        group_name=group.group_name if group else None,
        messages=messages,
        members=members,
        memories=memories,
        relations=relations,
        activity=activity,
        active_topic=config.active_topic,
        emotion_state=config.emotion_state
        if isinstance(config.emotion_state, dict)
        else {},
    )


async def _describe_images(
    bot: Bot,
    group_id: int,
    normalized: NormalizedMessage,
    blocks: list[dict[str, Any]],
    session: Any,
    config: GroupAgentConfig,
    cached: list[tuple[str, str]],
    digests: list[str],
) -> str:
    captions = [caption for _digest, caption in cached]
    if captions:
        dbg(f"群 {group_id} 图片转述命中缓存 {len(captions)} 条: {captions!r}")
        return "\n".join(f"[图片转述（缓存）] {caption}" for caption in captions)
    vision_model_configured = str(
        getattr(ai_config, "agent_vision_model", "") or ""
    ).strip()
    if not blocks or not vision_model_configured:
        dbg(
            f"群 {group_id} 跳过图片识别: blocks={len(blocks)} "
            f"vision_model 已配置={bool(vision_model_configured)}"
        )
        return "[图片未识别：当前未配置 AGENT_VISION_MODEL]"
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": "你是图片识别器。只描述图片中可见且与用户问题相关的事实，不猜测身份、隐私或图片外的信息。",
        },
        {
            "role": "user",
            "content": [{"type": "text", "text": normalized.prompt_text()}, *blocks],
        },
    ]
    result = await complete(  # pyright: ignore[reportArgumentType]
        messages,  # pyright: ignore[reportArgumentType]
        model=get_agent_model("agent_vision"),
        role="agent_vision",
        max_tokens=500,
        timeout=30,
    )  # pyright: ignore[reportArgumentType]
    caption = (result or "").strip()
    if not caption:
        dbg(f"群 {group_id} 视觉模型返回空结果")
        return "[图片未识别：视觉模型没有返回结果]"
    dbg(f"群 {group_id} 视觉模型识别完成 caption={caption!r}")
    for digest in digests:
        await store_caption(
            session,
            group_id,
            digest,
            caption,
            get_agent_model("agent_vision"),
            cache_enabled=bool(config.media_cache_enabled),
        )
    return f"[图片转述] {caption[:2000]}"


async def process_group_message(
    bot: Bot,
    event: GroupMessageEvent,
    normalized: NormalizedMessage,
    *,
    enqueued_at: float | None = None,
) -> None:
    group_id = int(event.group_id)
    bot_id = int(bot.self_id)
    turn_started_at = time.monotonic()
    dbg(
        f"群 {group_id} 开始处理消息: bot={bot_id} user={event.get_user_id()} "
        f"message_id={getattr(event, 'message_id', None)} "
        f"完整消息={normalized.prompt_text()!r}"
    )
    async with group_lock(group_id, bot_id):
        if enqueued_at is not None and is_pending_trigger_expired(enqueued_at):
            dbg(
                f"群 {group_id} 触发在等待群锁期间过期,跳过回复: "
                f"message_id={getattr(event, 'message_id', None)}"
            )
            return
        dbg(f"群 {group_id} 已取得群锁,开始处理")
        async with get_session() as session:
            config = await _config(session, group_id)
            if config is None or not config.enabled:
                dbg(
                    f"群 {group_id} 处理中止: config={'缺失' if config is None else '未启用'}"
                )
                return
            context = await _load_context(session, group_id, config, bot_id)
            capabilities = await probe_group_capabilities(bot, group_id)
            actor_user_id = int(event.get_user_id())
            allow_admin_tools = await user_can_manage_group(
                bot, group_id, actor_user_id
            )
            dbg(
                f"群 {group_id} 能力探测完成: bot_role={capabilities.role!r} "
                f"can_manage={capabilities.can_manage} actions={len(capabilities.actions)} 个 "
                f"发起人 {actor_user_id} 管理工具权限={allow_admin_tools}"
            )
            tools = build_tool_schemas(
                capabilities, allow_admin_tools=allow_admin_tools
            )
            dbg(f"群 {group_id} 本轮可用工具 {len(tools)} 个")
            media_blocks, cached_captions, media_digests = await prepare_image_inputs(
                bot,
                group_id,
                normalized.media_refs,
                session=session,
                cache_enabled=bool(config.media_cache_enabled),
            )
            dbg(
                f"群 {group_id} 媒体输入: media_blocks={len(media_blocks)} "
                f"缓存字幕={len(cached_captions)} digests={media_digests}"
            )
            user_prompt = normalized.prompt_text()
            mode = agent_multimodal_mode()
            dbg(f"群 {group_id} 多模态模式={mode!r}")
            if media_blocks and mode == "false":
                dbg(f"群 {group_id} 多模态关闭,改走视觉转述注入 prompt")
                user_prompt = f"{user_prompt}\n{await _describe_images(bot, group_id, normalized, media_blocks, session, config, cached_captions, media_digests)}"
                media_blocks = []
            elif cached_captions:
                dbg(f"群 {group_id} 追加缓存字幕 {len(cached_captions)} 条到 prompt")
                user_prompt = f"{user_prompt}\n" + "\n".join(
                    f"[图片转述（缓存）] {caption}"
                    for _digest, caption in cached_captions
                )
            model = get_agent_model("agent_dialogue")
            dbg(f"群 {group_id} 对话模型={model!r}")
            messages, _prefix_fingerprint = build_messages(
                persona=resolve_persona(config),
                tools=tools,
                context=context,
                user_prompt=user_prompt,
                media_inputs=media_blocks if mode != "false" else None,
            )
            cache_key = prompt_cache_key(
                persona=resolve_persona(config),
                tools=tools,
                model=model,
                persona_version=config.persona_version,
            )
            dbg(
                f"群 {group_id} 提示词构建完成: messages={len(messages)} 条 "
                f"prompt 前缀指纹={_prefix_fingerprint[:12]}… "
                f"cache_key={'命中' if cache_key in _PROMPT_CACHE_KEYS else '未命中'} "
                f"用户 prompt={user_prompt!r}"
            )
            try:
                from ..metrics import record_agent_cache

                record_agent_cache(
                    "prompt", "hit" if cache_key in _PROMPT_CACHE_KEYS else "miss"
                )
            except Exception:  # noqa: BLE001
                dbg_exc(f"群 {group_id} 上报 prompt 缓存指标失败(忽略)")
            _PROMPT_CACHE_KEYS[cache_key] = None
            _PROMPT_CACHE_KEYS.move_to_end(cache_key)
            while len(_PROMPT_CACHE_KEYS) > _PROMPT_CACHE_LIMIT:
                _PROMPT_CACHE_KEYS.popitem(last=False)
            fallback_attempted = False
            deadline = time.monotonic() + _MAX_TURN_SECONDS
            rounds = 0
            while rounds < MAX_TOOL_ROUNDS:
                if time.monotonic() > deadline:
                    dbg(
                        f"群 {group_id} 工具循环超过 {_MAX_TURN_SECONDS}s 时限,发送收尾提示"
                    )
                    if enqueued_at is not None and is_pending_trigger_expired(
                        enqueued_at
                    ):
                        dbg(
                            f"群 {group_id} 收尾前触发已过期,跳过发送: "
                            f"message_id={getattr(event, 'message_id', None)}"
                        )
                        return
                    await _send_group_text(bot, group_id, _TURN_END_NOTICE)
                    return
                try:
                    response = await complete_with_tools(  # pyright: ignore[reportArgumentType]
                        messages,  # pyright: ignore[reportArgumentType]
                        tools,  # pyright: ignore[reportArgumentType]
                        model=model,
                        role="agent_dialogue",
                        max_tokens=800,
                        timeout=30,
                        multimodal=bool(media_blocks),
                        raise_on_unsupported=bool(media_blocks)
                        and not fallback_attempted,
                    )
                except LLMMultimodalUnsupportedError:
                    dbg(
                        f"群 {group_id} 模型不支持多模态,降级为视觉转述重建提示词(不占轮次)"
                    )
                    fallback_attempted = True
                    user_prompt = f"{normalized.prompt_text()}\n{await _describe_images(bot, group_id, normalized, media_blocks, session, config, cached_captions, media_digests)}"
                    messages, _prefix_fingerprint = build_messages(
                        persona=resolve_persona(config),
                        tools=tools,
                        context=context,
                        user_prompt=user_prompt,
                    )
                    media_blocks = []
                    # 多模态降级重建提示词，不占用工具轮次。
                    continue
                rounds += 1
                if response is None:
                    fallback = (
                        _deterministic_reply(normalized.plain_text) or _FALLBACK_NOTICE
                    )
                    dbg(
                        f"群 {group_id} 第 {rounds} 轮 LLM 返回 None,降级回复={fallback!r}"
                    )
                    if enqueued_at is not None and is_pending_trigger_expired(
                        enqueued_at
                    ):
                        dbg(
                            f"群 {group_id} 兜底回复前触发已过期,跳过发送: "
                            f"message_id={getattr(event, 'message_id', None)}"
                        )
                        return
                    await _send_group_text(bot, group_id, fallback)
                    return
                content = (response.content or "").strip()
                tool_calls = response.tool_calls or []
                dbg(
                    f"群 {group_id} 第 {rounds}/{MAX_TOOL_ROUNDS} 轮 LLM 响应: "
                    f"content={content!r} tool_calls={[getattr(getattr(c, 'function', None), 'name', None) for c in tool_calls]}"
                )
                if not tool_calls:
                    if content:
                        input_fingerprint = hashlib.sha256(
                            user_prompt.casefold().encode("utf-8")
                        ).hexdigest()
                        response_fingerprint = hashlib.sha256(
                            content.casefold().encode("utf-8")
                        ).hexdigest()
                        now = now_beijing()
                        recent = list(config.recent_response_fingerprints or [])
                        duplicate = any(
                            _is_recent_duplicate(
                                item, input_fingerprint, response_fingerprint, now
                            )
                            for item in recent
                        )
                        if duplicate:
                            dbg(
                                f"群 {group_id} 回复与近 10 分钟内重复,抑制发送: {content!r}"
                            )
                            return
                        if enqueued_at is not None and is_pending_trigger_expired(
                            enqueued_at
                        ):
                            dbg(
                                f"群 {group_id} 正文发送前触发已过期,跳过发送: "
                                f"message_id={getattr(event, 'message_id', None)}"
                            )
                            return
                        if not await _send_group_text(bot, group_id, content):
                            dbg(f"群 {group_id} 回复发送失败,放弃本轮状态更新")
                            return
                        recent.append(
                            {
                                "input": input_fingerprint,
                                "response": response_fingerprint,
                                "text": content[:500],
                                "at": now.isoformat(),
                            }
                        )
                        config.recent_response_fingerprints = recent[-8:]
                        config.last_response_fingerprint = response_fingerprint
                        config.last_response_input_fingerprint = input_fingerprint
                        config.last_response_at = now
                        config.last_agent_at = now
                        if (
                            normalized.plain_text
                            and normalized.plain_text[:240] != config.active_topic
                        ):
                            config.context_epoch += 1
                            config.active_topic = normalized.plain_text[:240]
                            dbg(
                                f"群 {group_id} 话题切换: epoch={config.context_epoch} "
                                f"topic={config.active_topic!r}"
                            )
                        try:
                            await session.commit()
                        except SQLAlchemyError:
                            # 消息已经发出；状态丢失只影响重复抑制，不能上抛。
                            dbg_exc(f"群 {group_id} 回复后状态提交失败,已回滚")
                            await session.rollback()
                        else:
                            dbg(
                                f"群 {group_id} 回复后状态已提交(指纹记录 {len(recent[-8:])} 条)"
                            )
                    return
                assistant: dict[str, Any] = {
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": [],
                }
                messages.append(assistant)
                for call in tool_calls:
                    function = getattr(call, "function", None)
                    if function is None:
                        dbg(
                            f"群 {group_id} 跳过缺少 function 的 tool_call id={getattr(call, 'id', None)}"
                        )
                        continue
                    raw_args = getattr(function, "arguments", "{}") or "{}"
                    dbg(
                        f"群 {group_id} 第 {rounds} 轮工具调用: "
                        f"name={getattr(function, 'name', '')!r} args={raw_args}"
                    )
                    try:
                        args = json.loads(raw_args)
                        if not isinstance(args, dict):
                            raise ValueError("工具参数必须是对象")
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        args = {}
                        result = {"ok": False, "error": str(exc)}
                        dbg(f"群 {group_id} 工具参数解析失败: {exc}")
                    else:
                        result = await execute_tool(
                            getattr(function, "name", ""),
                            args,
                            bot=bot,
                            group_id=group_id,
                            actor_user_id=actor_user_id,
                            session=session,
                            capabilities=capabilities,
                        )
                    dbg(
                        f"群 {group_id} 工具 {getattr(function, 'name', '')!r} 返回: "
                        f"{json.dumps(result, ensure_ascii=False)}"
                    )
                    assistant["tool_calls"].append(
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": getattr(function, "name", ""),
                                "arguments": raw_args,
                            },
                        }
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
                    try:
                        await session.commit()
                    except SQLAlchemyError:
                        dbg_exc(f"群 {group_id} 工具轮状态提交失败,已回滚")
                        await session.rollback()
                    # 提交会过期会话内对象；后续轮次还要读取 config 属性，
                    # 先刷新避免同步惰性加载（MissingGreenlet）。
                    await session.refresh(config)
            # 走到这里说明所有轮次都被工具调用耗尽、始终没有最终回复。
            # 其余分支均已 return；给用户一个交代，不能静默。
            dbg(
                f"群 {group_id} {MAX_TOOL_ROUNDS} 轮全部被工具调用耗尽,无最终回复,"
                f"发送收尾提示;整轮耗时 {time.monotonic() - turn_started_at:.1f}s"
            )
            if enqueued_at is not None and is_pending_trigger_expired(enqueued_at):
                dbg(
                    f"群 {group_id} 工具收尾前触发已过期,跳过发送: "
                    f"message_id={getattr(event, 'message_id', None)}"
                )
                return
            await _send_group_text(bot, group_id, _TURN_END_NOTICE)


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
    # 常驻监听器不走 require_feature 依赖，需要手动接受功能开关约束；
    # 关闭时既不落库也不响应。
    if not await check_feature_permission(
        int(event.get_user_id()), int(event.group_id), "group_agent", _session
    ):
        dbg(f"群 {event.group_id} 跳过: group_agent 功能开关关闭(用户或群级别)")
        return
    config = await _config(_session, int(event.group_id))
    if config is None or not config.enabled:
        dbg(
            f"群 {event.group_id} 跳过: Agent 配置"
            f"{'缺失' if config is None else '未启用'}"
        )
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
    # _persist_message 提交后 config 属性会过期，先在提交前取出触发模式，
    # 避免异步引擎上触发同步惰性加载（MissingGreenlet）。
    trigger_mode = config.trigger_mode
    await _persist_message(bot, event, normalized, _session)
    if not should_respond(event, bot, trigger_mode, normalized=normalized):
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
