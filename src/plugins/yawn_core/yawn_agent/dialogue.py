# ruff: noqa: E501,F401,I001,TID252,PLR0911,PLR0912,PLR0913,PLR0915,PLR0917,PLR2004,C901,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100,SIM117
"""群聊 Agent 对话主流程：上下文加载、多模态降级、LLM 工具循环与回复收尾。"""

import asyncio
import hashlib
import json
import re
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot_plugin_orm import get_session
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from ..data_models.agent_memory import AgentMemory, AgentPrivacy, AgentRelation
from ..data_models.bot_group import BotGroup
from ..data_models.group_agent_config import GroupAgentConfig
from ..data_models.group_agent_message import GroupAgentMessage
from ..data_models.user_group import UserGroup
from ..llm import (
    LLMMultimodalUnsupportedError,
    agent_multimodal_mode,
    ai_config,
    complete,
    complete_with_tools,
    get_agent_model,
)
from .capabilities import probe_group_capabilities, user_can_manage_group
from .collector import group_lock, is_pending_trigger_expired
from .config_store import get_or_create_config
from .context import ActivitySnapshot, build_context, now_beijing
from .log import dbg, dbg_exc
from .media import prepare_image_inputs, store_caption
from .message_parser import NormalizedMessage
from .persona import resolve_persona
from .prompt import build_messages, prompt_cache_key
from .tools import MAX_TOOL_ROUNDS, build_tool_schemas, execute_tool

_GREETING_WORDS = ("你好", "嗨", "hello", "hi", "早上好", "晚上好", "在吗", "在不在")
_PROMPT_CACHE_KEYS: OrderedDict[str, None] = OrderedDict()
_PROMPT_CACHE_LIMIT = 256
_MAX_TURN_SECONDS = 120.0
_SEND_TIMEOUT = 15.0
_FALLBACK_NOTICE = "现在有点忙，稍后再试～"
_TURN_END_NOTICE = "这个话题我先记下了，稍后再继续聊～"
_VISION_SYSTEM_PROMPT = (
    "你是图片识别器。只描述图片中可见且与用户问题相关的事实，"
    "不猜测身份、隐私或图片外的信息。"
)


def _extract_message_id(result: Any) -> int | None:
    """OneBot 实现对 send_group_msg 返回值不统一：dict、对象或裸 int；0 视为缺失。"""

    raw: Any
    if isinstance(result, dict):
        raw = result.get("message_id")
    else:
        raw = getattr(result, "message_id", result)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value or None


async def _send_group_text(
    bot: Bot, group_id: int, text: str
) -> tuple[bool, int | None]:
    """返回 (是否发出, message_id)；message_id 缺失不视为发送失败。"""

    try:
        result = await asyncio.wait_for(
            bot.call_api("send_group_msg", group_id=group_id, message=Message(text)),
            timeout=_SEND_TIMEOUT,
        )
    except Exception:  # noqa: BLE001
        logger.warning("群聊 Agent 发送群消息失败: %s", group_id)
        dbg_exc(f"群 {group_id} 发送群消息失败 text={text!r}")
        return False, None
    dbg(f"群 {group_id} 发送群消息成功 text={text!r}")
    return True, _extract_message_id(result)


def contains_word(text: str, word: str) -> bool:
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


def _deterministic_reply(text: str) -> str | None:
    """无 AI key 时仅对简单问候给出稳定反馈。"""

    normalized = " ".join(text.lower().split())
    if any(contains_word(normalized, word) for word in _GREETING_WORDS):
        return "我在呀，有事直接说～"
    if "agent状态" in normalized or "群聊agent" in normalized:
        return "群聊 Agent 在线；复杂对话需要配置 AI_API_KEY。"
    return None


async def _send_unless_expired(
    bot: Bot,
    group_id: int,
    text: str,
    enqueued_at: float | None,
    *,
    label: str,
    message_id: Any = None,
) -> tuple[bool, int | None]:
    """过期触发不发送；返回 (是否真正发出, message_id)。"""

    if enqueued_at is not None and is_pending_trigger_expired(enqueued_at):
        dbg(f"群 {group_id} {label}前触发已过期,跳过发送: message_id={message_id}")
        return False, None
    return await _send_group_text(bot, group_id, text)


async def persist_bot_reply(
    session: Any,
    bot_id: int,
    group_id: int,
    message_id: int | None,
    text: str,
    retention_days: int,
) -> None:
    """把 bot 自己发出的消息以 role="bot" 落库，让后续上下文记得自己说过什么。

    message_id 缺失或撞 (bot_id, message_id) 唯一键时跳过；随调用方事务提交。
    """

    if not message_id:
        dbg(f"群 {group_id} bot 发言缺少 message_id,跳过自言落库")
        return
    duplicate = await session.scalar(
        select(GroupAgentMessage).where(
            GroupAgentMessage.bot_id == bot_id,
            GroupAgentMessage.message_id == message_id,
        )
    )
    if duplicate is not None:
        dbg(f"群 {group_id} bot 发言 {message_id} 已落库过,去重跳过")
        return
    now = now_beijing()
    retention = max(1, min(int(retention_days), 365))
    session.add(
        GroupAgentMessage(
            bot_id=bot_id,
            message_id=message_id,
            group_id=group_id,
            user_id=bot_id,
            sender_name=None,
            role="bot",
            title=None,
            normalized_text=text,
            received_at=now,
            expires_at=now + timedelta(days=retention),
        )
    )
    dbg(f"群 {group_id} bot 发言 {message_id} 已加入自言落库(role=bot)")


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
    member_rows_in_window = [row for row in in_window if row.role != "bot"]
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
        last_member_message_at=(
            member_rows_in_window[0].received_at if member_rows_in_window else None
        ),
        member_messages_60m=len(member_rows_in_window),
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


async def _caption_single_image(
    group_id: int, normalized: NormalizedMessage, block: dict[str, Any]
) -> str | None:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _VISION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [{"type": "text", "text": normalized.prompt_text()}, block],
        },
    ]
    result = await complete(  # pyright: ignore[reportArgumentType]
        messages,  # pyright: ignore[reportArgumentType]
        model=get_agent_model("agent_vision"),
        role="agent_vision",
        max_tokens=500,
        timeout=30,
    )  # pyright: ignore[reportArgumentType]
    return (result or "").strip() or None


async def _describe_images(
    group_id: int,
    normalized: NormalizedMessage,
    blocks: list[dict[str, Any]],
    session: Any,
    config: GroupAgentConfig,
    cached: list[tuple[str, str]],
    digests: list[str],
) -> str:
    """视觉转述降级路径：逐图独立生成并缓存 caption。

    多图必须逐图转述，否则单一 caption 写进每个 digest 的缓存后，
    任一单图命中缓存都会拿到混合描述。URL 透传图没有 digest、
    不可缓存，但仍参与转述；data: 前缀的 block 与 digests 按序对齐。
    """

    vision_model_configured = bool(
        str(getattr(ai_config, "agent_vision_model", "") or "").strip()
    )
    if not blocks:
        dbg(f"群 {group_id} 跳过图片识别: 无可用图片 block")
        return "[图片未识别：当前未配置 AGENT_VISION_MODEL]"
    caption_by_digest = dict(cached)
    digest_iter = iter(digests)
    parts: list[str] = []
    for block in blocks:
        url = str(((block.get("image_url") or {}).get("url")) or "")
        digest = next(digest_iter, None) if url.startswith("data:") else None
        caption = caption_by_digest.get(digest) if digest else None
        if caption:
            parts.append(f"[图片转述（缓存）] {caption}")
            continue
        if not vision_model_configured:
            parts.append("[图片未识别：当前未配置 AGENT_VISION_MODEL]")
            continue
        caption = await _caption_single_image(group_id, normalized, block)
        if caption is None:
            dbg(f"群 {group_id} 视觉模型返回空结果 digest={digest}")
            parts.append("[图片未识别：视觉模型没有返回结果]")
            continue
        dbg(f"群 {group_id} 视觉模型识别完成 digest={digest} caption={caption!r}")
        if digest:
            await store_caption(
                session,
                group_id,
                digest,
                caption,
                get_agent_model("agent_vision"),
                cache_enabled=bool(config.media_cache_enabled),
            )
        parts.append(f"[图片转述] {caption[:2000]}")
    return "\n".join(parts)


async def _prepare_media_prompt(
    group_id: int,
    normalized: NormalizedMessage,
    session: Any,
    config: GroupAgentConfig,
    media_blocks: list[dict[str, Any]],
    cached_captions: list[tuple[str, str]],
    media_digests: list[str],
) -> tuple[str, list[dict[str, Any]]]:
    """多模态关闭时改为视觉转述注入 prompt；否则透传媒体 block。"""

    mode = agent_multimodal_mode()
    dbg(f"群 {group_id} 多模态模式={mode!r}")
    user_prompt = normalized.prompt_text()
    if media_blocks and mode == "false":
        dbg(f"群 {group_id} 多模态关闭,改走视觉转述注入 prompt")
        user_prompt = f"{user_prompt}\n{await _describe_images(group_id, normalized, media_blocks, session, config, cached_captions, media_digests)}"
        return user_prompt, []
    if cached_captions:
        dbg(f"群 {group_id} 追加缓存字幕 {len(cached_captions)} 条到 prompt")
        user_prompt = f"{user_prompt}\n" + "\n".join(
            f"[图片转述（缓存）] {caption}" for _digest, caption in cached_captions
        )
    return user_prompt, media_blocks


async def _finalize_reply(
    bot: Bot,
    group_id: int,
    config: GroupAgentConfig,
    session: Any,
    normalized: NormalizedMessage,
    content: str,
    user_prompt: str,
    enqueued_at: float | None,
    message_id: Any,
) -> None:
    """最终回复分支：去重检查、发送、指纹记录、话题推进与状态提交。"""

    input_fingerprint = hashlib.sha256(
        user_prompt.casefold().encode("utf-8")
    ).hexdigest()
    response_fingerprint = hashlib.sha256(
        content.casefold().encode("utf-8")
    ).hexdigest()
    now = now_beijing()
    recent = list(config.recent_response_fingerprints or [])
    duplicate = any(
        _is_recent_duplicate(item, input_fingerprint, response_fingerprint, now)
        for item in recent
    )
    if duplicate:
        dbg(f"群 {group_id} 回复与近 10 分钟内重复,抑制发送: {content!r}")
        return
    sent, sent_message_id = await _send_unless_expired(
        bot, group_id, content, enqueued_at, label="正文发送", message_id=message_id
    )
    if not sent:
        dbg(f"群 {group_id} 回复未发送(触发过期或发送失败),放弃本轮状态更新")
        return
    # bot 发言进入消息历史：后续上下文能看到自己最近说过什么，
    # 主动插话才能贴着上文连贯接话而不是自说自话。
    await persist_bot_reply(
        session,
        int(bot.self_id),
        group_id,
        sent_message_id,
        content,
        int(config.raw_retention_days),
    )
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
    if normalized.plain_text and normalized.plain_text[:240] != config.active_topic:
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
        dbg(f"群 {group_id} 回复后状态已提交(指纹记录 {len(recent[-8:])} 条)")


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
    message_id = getattr(event, "message_id", None)
    dbg(
        f"群 {group_id} 开始处理消息: bot={bot_id} user={event.get_user_id()} "
        f"message_id={message_id} 完整消息={normalized.prompt_text()!r}"
    )
    async with group_lock(group_id, bot_id):
        if enqueued_at is not None and is_pending_trigger_expired(enqueued_at):
            dbg(
                f"群 {group_id} 触发在等待群锁期间过期,跳过回复: message_id={message_id}"
            )
            return
        dbg(f"群 {group_id} 已取得群锁,开始处理")
        async with get_session() as session:
            config = await get_or_create_config(session, group_id)
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
            user_prompt, media_blocks = await _prepare_media_prompt(
                group_id,
                normalized,
                session,
                config,
                media_blocks,
                cached_captions,
                media_digests,
            )
            model = get_agent_model("agent_dialogue")
            dbg(f"群 {group_id} 对话模型={model!r}")
            messages, _prefix_fingerprint = build_messages(
                persona=resolve_persona(config),
                tools=tools,
                context=context,
                user_prompt=user_prompt,
                media_inputs=media_blocks
                if agent_multimodal_mode() != "false"
                else None,
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
                    await _send_unless_expired(
                        bot,
                        group_id,
                        _TURN_END_NOTICE,
                        enqueued_at,
                        label="收尾",
                        message_id=message_id,
                    )
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
                    user_prompt = f"{normalized.prompt_text()}\n{await _describe_images(group_id, normalized, media_blocks, session, config, cached_captions, media_digests)}"
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
                    await _send_unless_expired(
                        bot,
                        group_id,
                        fallback,
                        enqueued_at,
                        label="兜底回复",
                        message_id=message_id,
                    )
                    return
                content = (response.content or "").strip()
                tool_calls = response.tool_calls or []
                dbg(
                    f"群 {group_id} 第 {rounds}/{MAX_TOOL_ROUNDS} 轮 LLM 响应: "
                    f"content={content!r} tool_calls={[getattr(getattr(c, 'function', None), 'name', None) for c in tool_calls]}"
                )
                if not tool_calls:
                    if content:
                        await _finalize_reply(
                            bot,
                            group_id,
                            config,
                            session,
                            normalized,
                            content,
                            user_prompt,
                            enqueued_at,
                            message_id,
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
            await _send_unless_expired(
                bot,
                group_id,
                _TURN_END_NOTICE,
                enqueued_at,
                label="工具收尾",
                message_id=message_id,
            )
