# ruff: noqa: E501,F401,I001,TID252,PLR0912,PLR0913,PLR0915,PLR0917,PLR2004,C901,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100,SIM117
"""群聊 Agent 入口、上下文构建和共享 LLM 工具循环。"""

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, NoticeEvent
from nonebot.plugin import on_message, on_notice
from nonebot_plugin_orm import async_scoped_session, get_session
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

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
from .capabilities import probe_group_capabilities
from .collector import enqueue, ensure_worker, group_lock
from .context import ActivitySnapshot, build_context
from .media import get_cached_caption, prepare_image_inputs, store_caption
from .message_parser import NormalizedMessage, parse_message
from .persona import resolve_persona
from .prompt import build_messages, prompt_cache_key
from .tools import MAX_TOOL_ROUNDS, build_tool_schemas, execute_tool

_GREETING_WORDS = ("你好", "嗨", "hello", "hi", "早上好", "晚上好", "在吗", "在不在")
_EXPLICIT_WAKE_WORDS = ("小助手", "群聊agent", "群聊 agent", "yawn", "@机器人")
_PROMPT_CACHE_KEYS: set[str] = set()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


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


async def _config(session: Any, group_id: int) -> GroupAgentConfig:
    record = await session.get(GroupAgentConfig, group_id)
    if record is None:
        record = GroupAgentConfig(group_id=group_id)
        session.add(record)
        await session.flush()
    return record


def should_respond(
    event: GroupMessageEvent, bot: Bot, trigger_mode: str = "mention_or_proactive"
) -> bool:
    """被 @、回复机器人或显式自然语言唤醒时响应。"""

    if int(event.get_user_id()) == int(bot.self_id):
        return False
    self_id = str(bot.self_id)
    mentioned = any(
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
    text = " ".join(event.get_plaintext().strip().lower().split())
    explicit = any(_contains_word(text, word) for word in _EXPLICIT_WAKE_WORDS)
    if trigger_mode == "mention_only":
        return mentioned
    if trigger_mode == "mention_or_reply":
        return mentioned or replied
    if trigger_mode == "explicit_wakeup":
        return mentioned or explicit
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
    if message_id <= 0:
        return
    duplicate = await session.scalar(
        select(GroupAgentMessage).where(
            GroupAgentMessage.bot_id == bot_id,
            GroupAgentMessage.message_id == message_id,
        )
    )
    if duplicate is not None:
        return
    sender = event.sender
    config = await _config(session, group_id)
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
            received_at=_now(),
            expires_at=_now() + timedelta(days=retention),
        )
    )
    group = await session.get(BotGroup, group_id)
    if group is not None:
        group.last_active_at = _now()
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()


async def _load_context(
    session: Any, group_id: int, config: GroupAgentConfig, bot_id: int | None = None
) -> dict[str, Any]:
    message_stmt = select(GroupAgentMessage).where(
        GroupAgentMessage.group_id == group_id
    )
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
    memory_stmt = (
        select(AgentMemory)
        .where(
            AgentMemory.group_id == group_id,
            AgentMemory.visibility.in_(("group", "public")),
            (AgentMemory.expires_at.is_(None) | (AgentMemory.expires_at >= _now())),
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
    now = _now()
    activity = ActivitySnapshot(
        rows[0].received_at if rows else None,
        messages_5m=sum((now - row.received_at).total_seconds() < 300 for row in rows),
        messages_20m=sum(
            (now - row.received_at).total_seconds() < 1200 for row in rows
        ),
        messages_60m=len(rows),
        participants_60m=len({row.user_id for row in rows}),
        replies_60m=sum(bool(row.reply_chain) for row in rows),
        mentions_60m=sum("@" in row.normalized_text for row in rows),
        last_agent_at=config.last_agent_at,
        proactive_today=config.proactive_count,
    )
    group = await session.get(BotGroup, group_id)
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
        return "\n".join(f"[图片转述（缓存）] {caption}" for caption in captions)
    vision_model_configured = str(
        getattr(ai_config, "agent_vision_model", "") or ""
    ).strip()
    if not blocks or not vision_model_configured:
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
        return "[图片未识别：视觉模型没有返回结果]"
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
    bot: Bot, event: GroupMessageEvent, normalized: NormalizedMessage
) -> None:
    group_id = int(event.group_id)
    bot_id = int(bot.self_id)
    async with group_lock(group_id, bot_id):
        async with get_session() as session:
            config = await _config(session, group_id)
            if not config.enabled:
                return
            context = await _load_context(session, group_id, config, bot_id)
            capabilities = await probe_group_capabilities(bot, group_id)
            tools = build_tool_schemas(capabilities)
            media_blocks, cached_captions, media_digests = await prepare_image_inputs(
                bot,
                group_id,
                normalized.media_refs,
                session=session,
                cache_enabled=bool(config.media_cache_enabled),
            )
            user_prompt = normalized.prompt_text()
            mode = agent_multimodal_mode()
            if media_blocks and mode == "false":
                user_prompt = f"{user_prompt}\n{await _describe_images(bot, group_id, normalized, media_blocks, session, config, cached_captions, media_digests)}"
                media_blocks = []
            elif cached_captions:
                user_prompt = f"{user_prompt}\n" + "\n".join(
                    f"[图片转述（缓存）] {caption}"
                    for _digest, caption in cached_captions
                )
            model = get_agent_model("agent_dialogue")
            messages, prefix_fingerprint = build_messages(
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
            try:
                from ..metrics import record_agent_cache

                record_agent_cache(
                    "prompt", "hit" if cache_key in _PROMPT_CACHE_KEYS else "miss"
                )
            except Exception:  # noqa: BLE001
                pass
            _PROMPT_CACHE_KEYS.add(cache_key)
            if len(_PROMPT_CACHE_KEYS) > 256:
                _PROMPT_CACHE_KEYS.clear()
                _PROMPT_CACHE_KEYS.add(cache_key)
            fallback_attempted = False
            for _ in range(MAX_TOOL_ROUNDS):
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
                    fallback_attempted = True
                    user_prompt = f"{normalized.prompt_text()}\n{await _describe_images(bot, group_id, normalized, media_blocks, session, config, cached_captions, media_digests)}"
                    messages, prefix_fingerprint = build_messages(
                        persona=resolve_persona(config),
                        tools=tools,
                        context=context,
                        user_prompt=user_prompt,
                    )
                    media_blocks = []
                    continue
                if response is None:
                    fallback = _deterministic_reply(normalized.plain_text)
                    if fallback:
                        await bot.call_api(
                            "send_group_msg",
                            group_id=group_id,
                            message=Message(fallback),
                        )
                    return
                content = (response.content or "").strip()
                tool_calls = response.tool_calls or []
                if not tool_calls:
                    if content:
                        input_fingerprint = hashlib.sha256(
                            user_prompt.casefold().encode("utf-8")
                        ).hexdigest()
                        response_fingerprint = hashlib.sha256(
                            content.casefold().encode("utf-8")
                        ).hexdigest()
                        now = _now()
                        recent = list(config.recent_response_fingerprints or [])
                        duplicate = any(
                            _is_recent_duplicate(
                                item, input_fingerprint, response_fingerprint, now
                            )
                            for item in recent
                        )
                        if duplicate:
                            return
                        await bot.call_api(
                            "send_group_msg",
                            group_id=group_id,
                            message=Message(content),
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
                        if (
                            normalized.plain_text
                            and normalized.plain_text[:240] != config.active_topic
                        ):
                            config.context_epoch += 1
                            config.active_topic = normalized.plain_text[:240]
                        config.emotion_state = {"last_prefix": prefix_fingerprint}
                        await session.commit()
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
                        continue
                    raw_args = getattr(function, "arguments", "{}") or "{}"
                    try:
                        args = json.loads(raw_args)
                        if not isinstance(args, dict):
                            raise ValueError("工具参数必须是对象")
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        args = {}
                        result = {"ok": False, "error": str(exc)}
                    else:
                        result = await execute_tool(
                            getattr(function, "name", ""),
                            args,
                            bot=bot,
                            group_id=group_id,
                            actor_user_id=int(event.get_user_id()),
                            session=session,
                            capabilities=capabilities,
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
                    await session.commit()


agent_listener = on_message(priority=8, block=False)
member_notice = on_notice(priority=20, block=False)


@agent_listener.handle()
async def handle_group_agent_message(
    bot: Bot, event: GroupMessageEvent, _session: async_scoped_session
) -> None:
    if not isinstance(event, GroupMessageEvent) or int(event.get_user_id()) == int(
        bot.self_id
    ):
        return
    config = await _config(_session, int(event.group_id))
    if not config.enabled:
        return
    privacy = await _session.get(
        AgentPrivacy, (int(event.group_id), int(event.get_user_id()))
    )
    if privacy is not None and privacy.opted_out:
        return
    normalized = await parse_message(bot, event.message)
    await _persist_message(bot, event, normalized, _session)
    if not should_respond(event, bot, config.trigger_mode):
        return
    if not enqueue(int(event.group_id), (bot, event, normalized), int(bot.self_id)):
        logger.warning("群聊 Agent 队列已满: %s", event.group_id)
        return
    ensure_worker(int(event.group_id), process_group_message, int(bot.self_id))


@member_notice.handle()
async def handle_member_notice(
    bot: Bot, event: NoticeEvent, session: async_scoped_session
) -> None:
    group_id = getattr(event, "group_id", None)
    user_id = getattr(event, "user_id", None)
    if group_id is None or user_id is None:
        return
    try:
        group_id = int(group_id)
        user_id = int(user_id)
    except (TypeError, ValueError):
        return
    record = await session.get(UserGroup, (group_id, user_id))
    if record is None:
        return
    try:
        info = await bot.call_api(
            "get_group_member_info", group_id=group_id, user_id=user_id
        )
    except Exception:  # noqa: BLE001
        return
    if isinstance(info, dict):
        record.role = str(info.get("role") or record.role or "member")
        if info.get("title") is not None:
            record.title = str(info["title"])
        if info.get("card"):
            record.group_nickname = str(info["card"])
        record.last_role_sync_at = _now()
        await session.commit()


__all__ = [
    "agent_listener",
    "handle_group_agent_message",
    "member_notice",
    "process_group_message",
    "should_respond",
]
