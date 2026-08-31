# ruff: noqa: E501,F401,I001,TID252,PLR0911,PLR0912,PLR0913,PLR0915,PLR0917,PLR2004,C901,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100,SIM117
"""群聊 Agent 对话主流程：上下文加载、多模态降级、LLM 工具循环与回复收尾。"""

import asyncio
import hashlib
import json
import re
import time
from collections import OrderedDict
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot_plugin_orm import get_session
from sqlalchemy import case, exists, func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from ..data_models.agent_memory import AgentMemory, AgentPrivacy, AgentRelation
from ..data_models.bot_group import BotGroup
from ..data_models.group_agent_config import GroupAgentConfig
from ..data_models.group_agent_message import GroupAgentMessage
from ..data_models.user_group import UserGroup
from ..llm import (
    LLMMultimodalUnsupportedError,
    complete,
    complete_with_tools_result,
    resolve_llm_request,
    vision_model_configured,
)
from .capabilities import (
    get_segment_capabilities,
    probe_group_capabilities,
    user_can_manage_group,
)
from .collector import group_lock, is_pending_trigger_expired
from .config_store import agent_runtime_enabled, get_or_create_config
from .conversation import mark_bot_reply
from .context import (
    ActivitySnapshot,
    CurrentTurn,
    build_context,
    build_current_turn,
    now_beijing,
    trim_context_messages,
)
from .context_history import (
    bot_message_meta as _bot_message_meta,
    history_message_meta as _history_message_meta,
    history_message_payload as _history_message_payload,
    select_context_messages,
    select_context_messages_only as _select_context_messages,
)
from .context_budget import pack_context
from .emotion import emotion_context_state
from .execution_trace import (
    begin_execution_trace,
    bind_execution_trace,
    finish_execution_trace,
    reset_execution_trace,
    trace_event,
)
from .log import dbg, dbg_exc
from .media import prepare_image_inputs, store_caption
from .memory import effective_relation_confidence, rank_memories
from .message_parser import NormalizedMessage
from .outbound import (
    DELIVERY_CONFIRMED_FAILURE,
    PreparedOutboundMessage,
    SendResult,
    extract_message_id as extract_outbound_message_id,
    prepare_text_message,
    send_prepared_outbound,
)
from .persona import persona_behavior, persona_editor_profile, resolve_persona
from .prompt import (
    build_messages,
    prompt_cache_key,
    render_current_turn,
    stable_context_key,
)
from .tools import (
    MAX_TOOL_ROUNDS,
    build_tool_schemas,
    dialogue_tool_round_limit,
    execute_tool,
    select_dialogue_message_segment_types,
    select_dialogue_tool_names,
)

_GREETING_WORDS = ("你好", "嗨", "hello", "hi", "早上好", "晚上好", "在吗", "在不在")
_PROMPT_CACHE_KEYS: OrderedDict[str, None] = OrderedDict()
_PROMPT_CACHE_LIMIT = 256
_MAX_TURN_SECONDS = 120.0
_FALLBACK_NOTICE = "现在有点忙，稍后再试～"
_TURN_END_NOTICE = "这个话题我先记下了，稍后再继续聊～"
_VISIBLE_SEND_TOOLS = frozenset({"send_message", "send_forward"})
_MEMORY_CONTEXT_CHAR_BUDGET = 6_000
# 条目上限对齐各层最大配额（5+4+12+3），字符预算仍是实际约束。
_MEMORY_CONTEXT_LIMIT = 24
_VISION_SYSTEM_PROMPT = (
    "你是图片识别器。只描述图片中可见且与用户问题相关的事实，"
    "不猜测身份、隐私或图片外的信息。"
)


def _accumulate_turn_usage(total: dict[str, int], result: Any) -> dict[str, Any]:
    """累计一次用户回合内多次 LLM 请求的真实 token 用量。"""

    total["rounds"] = total.get("rounds", 0) + 1
    fields = (
        ("prompt_tokens", "input"),
        ("completion_tokens", "output"),
        ("cached_tokens", "cached"),
        ("cache_miss_tokens", "cache_miss"),
    )
    current: dict[str, int | None] = {}
    try:
        from ..metrics import record_ai_tokens

        for field, source in fields:
            raw = getattr(result, field, None)
            value = int(raw) if isinstance(raw, int) and raw >= 0 else None
            current[field] = value
            if value is None:
                continue
            total[field] = total.get(field, 0) + value
            if value > 0:
                record_ai_tokens("agent_dialogue_turn", source, value)
    except Exception:  # noqa: BLE001
        dbg_exc("累计 Agent 回合 token 指标失败(忽略)")
    return {
        "request": current,
        "turn": {
            "rounds": total.get("rounds", 0),
            **{field: total.get(field, 0) for field, _source in fields},
        },
    }


def _trace_prompt_shape(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Return useful prompt diagnostics without retaining the full prompt."""

    roles: dict[str, int] = {}
    text_chars = 0
    media_blocks = 0
    tool_call_messages = 0
    for message in messages:
        role = str(message.get("role") or "unknown")
        roles[role] = roles.get(role, 0) + 1
        if message.get("tool_calls"):
            tool_call_messages += 1
        content = message.get("content")
        if isinstance(content, str):
            text_chars += len(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text_chars += len(str(block.get("text") or ""))
                elif str(block.get("type") or "").startswith("image"):
                    media_blocks += 1
    return {
        "roles": roles,
        "text_chars": text_chars,
        "media_blocks": media_blocks,
        "tool_call_messages": tool_call_messages,
    }


def _visible_tool_send_ends_turn(result: dict[str, Any]) -> bool:
    """用户可见发送一旦成功，本轮必须结束，禁止再追加最终纯文本。"""

    return result.get("sent") is True


def _extract_message_id(result: Any) -> int | None:
    """OneBot 实现对 send_group_msg 返回值不统一：dict、对象或裸 int；0 视为缺失。"""

    return extract_outbound_message_id(result)


async def _send_group_text(
    bot: Bot, group_id: int, text: str
) -> tuple[bool, int | None]:
    """返回 (是否发出, message_id)；message_id 缺失不视为发送失败。"""

    try:
        prepared = prepare_text_message(text)
        result = await send_prepared_outbound(bot, group_id, prepared)
    except Exception:  # noqa: BLE001
        logger.warning("群聊 Agent 发送群消息失败: %s", group_id)
        dbg_exc(f"群 {group_id} 发送群消息失败 text={text!r}")
        return False, None
    dbg(f"群 {group_id} 发送群消息成功 text={text!r}")
    return result.ends_turn, result.message_id


def contains_word(text: str, word: str) -> bool:
    if not word:
        return False
    if re.fullmatch(r"[a-z0-9 ]+", word):
        return (
            re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) is not None
        )
    return word in text


def _current_turn_focus_ids(
    actor_user_id: int,
    normalized: NormalizedMessage,
    *,
    bot_id: int | None = None,
) -> list[int]:
    focus = [int(actor_user_id)]
    focus.extend(
        int(user_id)
        for user_id in normalized.mentions
        if bot_id is None or int(user_id) != bot_id
    )
    if normalized.reply_chain:
        raw_user_id = normalized.reply_chain[0].get("user_id")
        try:
            reply_user_id = int(str(raw_user_id))
        except (TypeError, ValueError):
            reply_user_id = 0
        if reply_user_id > 0 and reply_user_id != bot_id:
            focus.append(reply_user_id)
    return list(dict.fromkeys(focus))


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
    message: str | PreparedOutboundMessage,
    enqueued_at: float | None,
    *,
    label: str,
    message_id: Any = None,
    session: Any = None,
    actor_user_id: int | None = None,
    source: str = "dialogue",
) -> SendResult:
    """过期触发不发送；普通文本与复合 Message 统一走 sender。"""

    if enqueued_at is not None and is_pending_trigger_expired(enqueued_at):
        trace_event(
            "outbound",
            label,
            status="skipped",
            output={"sent": False, "reason": "trigger_expired"},
            detail="触发消息在队列/群锁等待期间过期，取消用户可见发送",
        )
        dbg(f"群 {group_id} {label}前触发已过期,跳过发送: message_id={message_id}")
        return SendResult(
            sent=False,
            message_id=None,
            normalized_text="",
            segment_types=(),
            outcome="expired",
            delivery_state=DELIVERY_CONFIRMED_FAILURE,
        )
    prepared = prepare_text_message(message) if isinstance(message, str) else message
    try:
        return await send_prepared_outbound(
            bot,
            group_id,
            prepared,
            session=session,
            actor_user_id=actor_user_id,
            source=source,
        )
    except Exception:  # noqa: BLE001
        logger.warning("群聊 Agent 发送群消息失败: %s", group_id)
        dbg_exc(f"群 {group_id} {label}失败")
        return SendResult(
            sent=False,
            message_id=None,
            normalized_text=prepared.normalized_text,
            segment_types=(),
            outcome="send_failed",
            delivery_state=DELIVERY_CONFIRMED_FAILURE,
        )


async def persist_bot_reply(
    session: Any,
    bot_id: int,
    group_id: int,
    message_id: int | None,
    text: str,
    retention_days: int,
    *,
    segments: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    reply_chain: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    forward_tree: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    media_refs: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
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
            GroupAgentMessage.group_id == group_id,
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
            segments=list(segments or []),
            reply_chain=list(reply_chain or []),
            forward_tree=list(forward_tree or []),
            media_refs=list(media_refs or []),
            received_at=now,
            expires_at=now + timedelta(days=retention),
        )
    )
    dbg(f"群 {group_id} bot 发言 {message_id} 已加入自言落库(role=bot)")


async def _activity_window_counts(
    session: Any,
    group_id: int,
    now: datetime,
    *,
    bot_id: int | None = None,
    exclude_user_ids: set[int] | None = None,
    retention_at: datetime | None = None,
) -> dict[str, Any]:
    """60 分钟窗口活跃度的一条 SQL 聚合；对话与主动发言路径共用。

    旧实现从"最新 40/60 条消息"在 Python 侧数窗口，活跃群覆盖不全会
    低估 messages_60m/participants_60m/member_messages_60m；聚合查询不受
    加载条数截断影响。隐私退出用户统一排除（原主动发言路径未排除，
    与对话读路径口径不一致）。last_message_at 不限窗口，供冷场判定。
    """

    clauses: list[Any] = [
        GroupAgentMessage.group_id == group_id,
        (
            GroupAgentMessage.expires_at.is_(None)
            | (GroupAgentMessage.expires_at >= (retention_at or now))
        ),
        GroupAgentMessage.received_at <= now,
    ]
    if exclude_user_ids is not None and exclude_user_ids:
        clauses.append(GroupAgentMessage.user_id.not_in(exclude_user_ids))
    elif exclude_user_ids is None:
        opted_out = select(AgentPrivacy.user_id).where(
            AgentPrivacy.group_id == group_id,
            AgentPrivacy.user_id == GroupAgentMessage.user_id,
            AgentPrivacy.opted_out.is_(True),
        )
        clauses.append(~exists(opted_out))
    if bot_id is not None:
        clauses.append(GroupAgentMessage.bot_id == bot_id)
    in_window = GroupAgentMessage.received_at >= now - timedelta(hours=1)
    in_5m = GroupAgentMessage.received_at >= now - timedelta(minutes=5)
    is_member = GroupAgentMessage.role != "bot"
    row = (
        await session.execute(
            select(
                func.max(GroupAgentMessage.received_at),
                func.max(
                    case((in_window & is_member, GroupAgentMessage.received_at))
                ),
                func.sum(
                    case(
                        (
                            GroupAgentMessage.received_at >= now - timedelta(minutes=5),
                            1,
                        ),
                        else_=0,
                    )
                ),
                func.sum(
                    case(
                        (
                            GroupAgentMessage.received_at
                            >= now - timedelta(minutes=20),
                            1,
                        ),
                        else_=0,
                    )
                ),
                func.sum(case((in_window, 1), else_=0)),
                func.sum(case((in_window & is_member, 1), else_=0)),
                func.sum(case((in_5m & is_member, 1), else_=0)),
                func.count(
                    func.distinct(
                        case((in_5m & is_member, GroupAgentMessage.user_id))
                    )
                ),
                func.count(func.distinct(case((in_window, GroupAgentMessage.user_id)))),
                func.sum(
                    case(
                        (
                            in_window & GroupAgentMessage.normalized_text.contains("@"),
                            1,
                        ),
                        else_=0,
                    )
                ),
                func.sum(
                    case(
                        (
                            in_window
                            & (
                                func.json_array_length(GroupAgentMessage.reply_chain)
                                > 0
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
            ).where(*clauses)
        )
    ).one()
    return {
        "last_message_at": row[0],
        "last_member_message_at": row[1],
        "messages_5m": int(row[2] or 0),
        "messages_20m": int(row[3] or 0),
        "messages_60m": int(row[4] or 0),
        "member_messages_60m": int(row[5] or 0),
        "member_messages_5m": int(row[6] or 0),
        "member_participants_5m": int(row[7] or 0),
        "participants_60m": int(row[8] or 0),
        "mentions_60m": int(row[9] or 0),
        "replies_60m": int(row[10] or 0),
    }


async def _load_context(
    session: Any,
    group_id: int,
    config: GroupAgentConfig,
    bot_id: int | None = None,
    *,
    focus_user_ids: Sequence[int] | None = None,
    query_text: str | None = None,
    compact_history: bool = False,
    message_cutoff: datetime | None = None,
    include_active_profiles: bool = False,
    exclude_message_id: int | None = None,
    reference_at: datetime | None = None,
    selection_trace: list[dict[str, Any]] | None = None,
    budget_trace: list[dict[str, Any]] | None = None,
    context_model: str | None = None,
    completion_reserve: int = 2048,
    context_token_limit: int | None = None,
) -> dict[str, Any]:
    now = now_beijing()
    context_now = reference_at or now
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
    if message_cutoff is not None:
        message_stmt = message_stmt.where(
            GroupAgentMessage.received_at <= message_cutoff
        )
    if opted_out:
        message_stmt = message_stmt.where(GroupAgentMessage.user_id.not_in(opted_out))
    if bot_id is not None:
        message_stmt = message_stmt.where(GroupAgentMessage.bot_id == bot_id)
    if exclude_message_id is not None:
        message_stmt = message_stmt.where(
            GroupAgentMessage.message_id != int(exclude_message_id)
        )
    rows = (
        (
            await session.execute(
                message_stmt.order_by(GroupAgentMessage.id.desc()).limit(40)
            )
        )
        .scalars()
        .all()
    )
    messages_unbounded: list[dict[str, Any]] = []
    previous_at: datetime | None = None
    for row in reversed(rows):
        messages_unbounded.append(
            _history_message_payload(
                row,
                context_now=context_now,
                previous_at=previous_at,
            )
        )
        previous_at = row.received_at
    if query_text is None and not compact_history:
        # 兼容内部/测试调用：没有当前回合查询、也没有主动会话语义时，
        # 保持原来的有界历史行为。线上被动对话会显式传 query_text；
        # 主动发言/短会话会显式传 compact_history=True。
        messages = trim_context_messages(messages_unbounded)
    else:
        selection = select_context_messages(
            messages_unbounded,
            focus_user_ids=focus_user_ids,
            query_text=query_text,
        )
        messages = selection.messages
        if selection_trace is not None:
            selection_trace.extend(selection.trace)
    dbg(
        f"群 {group_id} 加载上下文: 原始历史 {len(messages_unbounded)} 条 -> "
        f"有效历史 {len(messages)} 条/"
        f"{sum(len(str(item.get('text') or '')) for item in messages)} 字"
    )
    # 记忆相关性只看最终实际注入的历史，并显式加入当前回合文本。
    recent_texts = [str(item["text"] or "") for item in messages[-6:]]
    if query_text:
        recent_texts.append(str(query_text)[:1000])
    previous_speaker_id = next(
        (
            int(item["user_id"])
            for item in reversed(messages)
            if item.get("role") != "bot"
        ),
        None,
    )
    recent_member_ids: list[int] = []
    for item in reversed(messages):
        if item.get("role") == "bot":
            continue
        member_id = int(item["user_id"])
        if member_id not in recent_member_ids:
            recent_member_ids.append(member_id)
    requested_focus = [int(user_id) for user_id in (focus_user_ids or [])]
    speaker_id = requested_focus[0] if requested_focus else previous_speaker_id
    focus_ids: list[int] = []
    focus_candidates = [
        *requested_focus,
        *(
            recent_member_ids
            if include_active_profiles or requested_focus
            else ([speaker_id] if speaker_id is not None else [])
        ),
    ]
    for member_id in focus_candidates:
        if member_id in opted_out or member_id in focus_ids:
            continue
        focus_ids.append(member_id)
        if len(focus_ids) >= 4:
            break
    relevant_member_ids = {
        int(item["user_id"])
        for item in messages
        if item.get("role") != "bot"
    }
    relevant_member_ids.update(focus_ids)
    member_rows = (
        (
            await session.execute(
                select(UserGroup)
                .where(
                    UserGroup.group_id == group_id,
                    UserGroup.user_id.in_(relevant_member_ids),
                )
                .order_by(UserGroup.last_seen_at.desc())
            )
        )
        .scalars()
        .all()
        if relevant_member_ids
        else []
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
    dbg(
        f"群 {group_id} 加载上下文: 近期相关成员 {len(members)} 人"
        f"(候选 {len(relevant_member_ids)})"
    )
    memory_clauses = [
        AgentMemory.group_id == group_id,
        AgentMemory.visibility.in_(("group", "public")),
        (AgentMemory.expires_at.is_(None) | (AgentMemory.expires_at >= now)),
    ]
    candidate_rows = (
        (
            await session.execute(
                select(AgentMemory)
                .where(*memory_clauses)
                .order_by(AgentMemory.salience.desc(), AgentMemory.updated_at.desc())
                .limit(160)
            )
        )
        .scalars()
        .all()
    )
    # 复现池捞回显著度榜外但近期被再确认的记忆：只按 salience 取候选会
    # 让安静复述的旧知识结构性不可见。
    recency_rows = (
        (
            await session.execute(
                select(AgentMemory)
                .where(*memory_clauses)
                .order_by(AgentMemory.updated_at.desc(), AgentMemory.id.desc())
                .limit(60)
            )
        )
        .scalars()
        .all()
    )
    summary_rows = (
        (
            await session.execute(
                select(AgentMemory)
                .where(*memory_clauses, AgentMemory.memory_type == "summary")
                .order_by(AgentMemory.updated_at.desc())
                .limit(40)
            )
        )
        .scalars()
        .all()
    )
    focus_rows: list[AgentMemory] = []
    if focus_ids:
        # 活跃成员画像独立查询，不能先和群级高显著记忆争抢 SQL LIMIT。
        # core 行排最前；随后按成员轮转分配 12 条预算，避免一人占满。
        focus_rows = list(
            (
                await session.execute(
                    select(AgentMemory)
                    .where(
                        *memory_clauses,
                        AgentMemory.subject_user_id.in_(focus_ids),
                        AgentMemory.memory_type.in_(("core", "profile", "manual")),
                    )
                    .order_by(
                        case(
                            (AgentMemory.memory_type == "core", 0),
                            (AgentMemory.memory_type == "profile", 1),
                            else_=2,
                        ),
                        AgentMemory.salience.desc(),
                        AgentMemory.updated_at.desc(),
                    )
                    .limit(96)
                )
            )
            .scalars()
            .all()
        )

    memory_rows = [*focus_rows, *summary_rows, *candidate_rows, *recency_rows]
    seen_local_ids: set[int] = set()
    local_rows: list[AgentMemory] = []
    for row in memory_rows:
        row_id = int(row.id or 0)
        if row_id in seen_local_ids:
            continue
        seen_local_ids.add(row_id)
        if (
            str(row.memory_key).startswith("public_daily:")
            or int(row.subject_user_id or 0) in opted_out
            or opted_out.intersection(set(row.related_user_ids or []))
        ):
            continue
        local_rows.append(row)
    focus_by_member: dict[int, list[AgentMemory]] = {user_id: [] for user_id in focus_ids}
    for row in local_rows:
        subject_id = int(row.subject_user_id or 0)
        if (
            subject_id in focus_by_member
            and row.memory_type in {"core", "profile", "manual"}
        ):
            focus_by_member[subject_id].append(row)
    focused_profiles: list[AgentMemory] = []
    for position in range(32):
        for member_id in focus_ids:
            rows_for_member = focus_by_member[member_id]
            if position < len(rows_for_member):
                focused_profiles.append(rows_for_member[position])
                if len(focused_profiles) >= 12:
                    break
        if len(focused_profiles) >= 12 or not any(
            position + 1 < len(rows) for rows in focus_by_member.values()
        ):
            break
    summaries = [row for row in local_rows if row.memory_type == "summary"]
    ranked_local = rank_memories(
        local_rows,
        recent_texts,
        speaker_id,
        context_now,
        limit=40,
        topic_hint=str(config.active_topic or ""),
    )

    shared_rows: list[AgentMemory] = []
    if config.cross_group_visibility == "public_summary":
        shared_rows = list(
            (
                await session.execute(
                    select(AgentMemory)
                    .join(
                        GroupAgentConfig,
                        GroupAgentConfig.group_id == AgentMemory.group_id,
                    )
                    .where(
                        AgentMemory.group_id != group_id,
                        AgentMemory.memory_type == "summary",
                        AgentMemory.visibility == "public",
                        AgentMemory.memory_key.startswith("public_daily:"),
                        AgentMemory.expires_at.is_not(None),
                        AgentMemory.expires_at >= now,
                        GroupAgentConfig.cross_group_visibility == "public_summary",
                    )
                    .order_by(AgentMemory.updated_at.desc())
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
        source_groups = {int(row.group_id or 0) for row in shared_rows}
        shared_optouts = set(
            (
                await session.execute(
                    select(AgentPrivacy.group_id, AgentPrivacy.user_id).where(
                        AgentPrivacy.group_id.in_(source_groups),
                        AgentPrivacy.opted_out.is_(True),
                    )
                )
            ).all()
        ) if source_groups else set()
        shared_rows = [
            row
            for row in shared_rows
            if not any(
                (int(row.group_id or 0), int(user_id)) in shared_optouts
                for user_id in row.related_user_ids or []
            )
        ]
        # 跨群共享摘要按 updated_at 确定性取前 4：候选超过 4 条时若按
        # 话题相关性重排，选择会随每条新消息翻转，击穿稳定层前缀缓存。
        shared_rows = shared_rows[:4]

    ordered: list[tuple[AgentMemory, str]] = []
    seen_memory_ids: set[int] = set()
    # source_scope 同时决定记忆进入提示词的稳定层（group_summary/shared_public，
    # 只随整理变化、可被前缀缓存命中）还是易变层（speaker/topic，随请求变化）。
    # 稳定来源排在前面：6000 字符预算从前往后消耗，稳定条目先入账，
    # 其截断点才不会随发言人画像的长短浮动。
    # 活跃成员画像总预算 12 条，按成员轮转；话题记忆维持最多 3 条。
    topic_rows = ranked_local[:3]
    for row, source in [
        *((row, "group_summary") for row in summaries[:5]),
        *((row, "shared_public") for row in shared_rows),
        *((
            row,
            "speaker"
            if speaker_id is not None
            and int(row.subject_user_id or 0) == speaker_id
            else "participant",
        ) for row in focused_profiles),
        *((row, "topic") for row in topic_rows),
    ]:
        row_id = int(row.id or 0)
        if row_id in seen_memory_ids:
            continue
        seen_memory_ids.add(row_id)
        ordered.append((row, source))

    name_by_id = {
        int(item["user_id"]): str(item.get("name") or item["user_id"])
        for item in members
    }
    for item in messages:
        user_id = int(item["user_id"])
        if item.get("name") and user_id not in name_by_id:
            name_by_id[user_id] = str(item["name"])
    memories: list[dict[str, Any]] = []
    # 以最终 JSON 数组的真实字符数计预算（含 [] 和条目间的 ", "）。
    memory_chars = 2
    for row, source in ordered:
        content = str(row.content or "").strip()
        if not content:
            continue
        if len(memories) >= _MEMORY_CONTEXT_LIMIT:
            break
        subject_id = int(row.subject_user_id or 0)
        item: dict[str, Any] = {
            "type": row.memory_type,
            "subject_user_id": subject_id or None,
            "subject_name": name_by_id.get(subject_id) if subject_id else None,
            "key": row.memory_key,
            "content": "",
            "salience": row.salience,
            "confidence": row.confidence,
            "source": row.source_kind,
            "evidence_count": len(row.evidence_message_ids or []),
            "first_observed_date": (row.created_at or now).date().isoformat(),
            "last_confirmed_date": (row.updated_at or row.created_at or now).date().isoformat(),
            "source_scope": source,
            "source_date": str(row.memory_key).rsplit(":", 1)[-1]
            if "daily:" in str(row.memory_key)
            else (row.updated_at or row.created_at or now).date().isoformat(),
        }
        overhead = len(json.dumps(item, ensure_ascii=False))
        separator_chars = 2 if memories else 0
        remaining = (
            _MEMORY_CONTEXT_CHAR_BUDGET
            - memory_chars
            - separator_chars
            - overhead
        )
        if remaining <= 0:
            break
        item["content"] = content[:remaining]
        item_chars = len(json.dumps(item, ensure_ascii=False))
        memories.append(item)
        memory_chars += separator_chars + item_chars
    dbg(
        f"群 {group_id} 加载上下文: 记忆 {len(memories)} 条/"
        f"{memory_chars} 字(预算 {_MEMORY_CONTEXT_CHAR_BUDGET},当前发言人={speaker_id})"
    )
    participant_ids = {
        int(item["user_id"]) for item in messages if item.get("role") != "bot"
    }
    participant_ids.update(focus_ids)
    relation_rows: list[AgentRelation] = []
    if participant_ids:
        # 先在 SQL 层限定当前上下文参与者，再取候选池，避免无关高置信边
        # 把低一些但当前真正相关的关系挤出候选集。
        relation_rows = list(
            (
                await session.execute(
                    select(AgentRelation)
                    .where(
                        AgentRelation.group_id == group_id,
                        AgentRelation.subject_user_id.not_in(opted_out),
                        AgentRelation.object_user_id.not_in(opted_out),
                        or_(
                            AgentRelation.source_kind != "mention",
                            AgentRelation.evidence_count >= 2,
                        ),
                        or_(
                            AgentRelation.subject_user_id.in_(participant_ids),
                            AgentRelation.object_user_id.in_(participant_ids),
                        ),
                    )
                    .order_by(AgentRelation.confidence.desc())
                    .limit(60)
                )
            )
            .scalars()
            .all()
        )

    def _relation_label(user_id: int) -> str:
        # 只加载近期相关成员；解析不到名字时兜底 QQ 号，避免渲染成 null。
        name = name_by_id.get(int(user_id))
        return f"{name}({user_id})" if name else str(user_id)

    # 生效置信度按"最后见到"分段降权：沉寂数月的老边让位给近期仍在互动的新边。
    ranked_relations = sorted(
        relation_rows,
        key=lambda row: (
            -effective_relation_confidence(
                float(row.confidence or 0.0), row.last_seen_at, context_now
            ),
            -int(row.evidence_count or 0),
            int(row.id or 0),
        ),
    )[:20]
    relations: list[str] = []
    for row in ranked_relations:
        line = (
            f"{_relation_label(int(row.subject_user_id))} "
            f"—{row.relation_type}→ {_relation_label(int(row.object_user_id))}"
        )
        note = str(row.note or "").strip()
        relations.append(f"{line}：{note}" if note else line)
    context_pack = pack_context(
        messages=messages,
        members=members,
        memories=memories,
        relations=relations,
        model=context_model,
        completion_reserve=completion_reserve,
        target_context_limit=context_token_limit,
    )
    messages = context_pack.messages
    members = context_pack.members
    memories = context_pack.memories
    relations = context_pack.relations
    if budget_trace is not None:
        budget_trace.extend(context_pack.trace)
    dbg(
        f"群 {group_id} token 上下文装箱: model={context_pack.budget.model!r} "
        f"window={context_pack.budget.context_window} "
        f"context={context_pack.trace[0]['usedTokens']}/{context_pack.budget.context_limit} tokens"
    )
    dbg(
        f"群 {group_id} 加载上下文: 关系 {len(relations)} 条"
        f"(候选 {len(relation_rows)},上限 20)"
    )
    # 活跃度改用聚合查询精确统计 60 分钟窗口，不再受最新 40 条截断影响。
    counts = await _activity_window_counts(
        session,
        group_id,
        context_now,
        bot_id=bot_id,
        exclude_user_ids=opted_out,
        retention_at=now,
    )
    activity = ActivitySnapshot(
        counts["last_message_at"],
        messages_5m=counts["messages_5m"],
        messages_20m=counts["messages_20m"],
        messages_60m=counts["messages_60m"],
        participants_60m=counts["participants_60m"],
        replies_60m=counts["replies_60m"],
        mentions_60m=counts["mentions_60m"],
        last_agent_at=config.last_agent_at,
        proactive_today=config.proactive_count,
        last_member_message_at=counts["last_member_message_at"],
        member_messages_60m=counts["member_messages_60m"],
        member_messages_5m=counts["member_messages_5m"],
        member_participants_5m=counts["member_participants_5m"],
    )
    dbg(
        f"群 {group_id} 活跃度快照: 5m={activity.messages_5m} 20m={activity.messages_20m} "
        f"60m={activity.messages_60m} 参与人数={activity.participants_60m} "
        f"回复数={activity.replies_60m} 提及数={activity.mentions_60m} "
        f"5m真人={activity.member_messages_5m}/"
        f"{activity.member_participants_5m}人 "
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
        emotion_state=emotion_context_state(
            config.emotion_state if isinstance(config.emotion_state, dict) else {},
            now=context_now,
            expressiveness=persona_editor_profile(config).expressiveness,
        ),
        reference_at=context_now,
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
        task="agent_image",
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

    has_vision_model = vision_model_configured()
    if not blocks:
        dbg(f"群 {group_id} 跳过图片识别: 无可用图片 block")
        return "[图片未识别：没有可用的图片数据]"
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
        if not has_vision_model:
            parts.append("[图片未识别：当前未配置可用的识图模型]")
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
                resolve_llm_request("agent_image").model,
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

    mode = resolve_llm_request("agent_dialogue").multimodal
    dbg(f"群 {group_id} 多模态模式={mode!r}")
    user_prompt = normalized.prompt_text()
    if media_blocks and mode == "unsupported":
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
    content: str | PreparedOutboundMessage,
    user_prompt: str,
    enqueued_at: float | None,
    message_id: Any,
) -> None:
    """最终回复分支：去重检查、发送、指纹记录、话题推进与状态提交。"""

    prepared = prepare_text_message(content) if isinstance(content, str) else content
    reply_text = prepared.normalized_text
    short_conversation_enabled = bool(config.short_conversation_enabled)
    max_followup_bot_turns = (
        persona_behavior(config).max_followup_bot_turns
        if short_conversation_enabled
        else 1
    )
    fingerprint_source = reply_text or json.dumps(
        list(prepared.segment_records), ensure_ascii=False, sort_keys=True
    )
    input_fingerprint = hashlib.sha256(
        user_prompt.casefold().encode("utf-8")
    ).hexdigest()
    response_fingerprint = hashlib.sha256(
        fingerprint_source.casefold().encode("utf-8")
    ).hexdigest()
    now = now_beijing()
    recent = list(config.recent_response_fingerprints or [])
    duplicate = any(
        _is_recent_duplicate(item, input_fingerprint, response_fingerprint, now)
        for item in recent
    )
    if duplicate:
        trace_event(
            "outbound",
            "重复回复抑制",
            status="skipped",
            output={"sent": False},
            detail="与近 10 分钟同一输入/回复指纹重复",
        )
        dbg(f"群 {group_id} 回复与近 10 分钟内重复,抑制发送: {reply_text!r}")
        return
    sent = await _send_unless_expired(
        bot,
        group_id,
        prepared,
        enqueued_at,
        label="正文发送",
        message_id=message_id,
        session=session,
        actor_user_id=None,
        source="dialogue",
    )
    if not sent.ends_turn:
        dbg(f"群 {group_id} 回复确认未发送(触发过期或明确失败),放弃本轮状态更新")
        return
    next_active_topic = config.active_topic
    topic_changed = bool(
        normalized.plain_text
        and normalized.plain_text[:240] != config.active_topic
    )
    if topic_changed:
        next_active_topic = normalized.plain_text[:240]

    # 发送已经是不可逆外部副作用。之后的消息历史、去重、冷却等本地状态
    # 只能降级失败，不能再把整轮标记成“执行失败”，否则 WebUI 会出现
    # “OneBot 已确认成功，但 Trace 最终失败”的假阴性。
    try:
        if sent.sent:
            # 只有确认成功才写入 Bot 消息历史；unknown 不能伪造一条确定存在的 QQ 消息。
            await persist_bot_reply(
                session,
                int(bot.self_id),
                group_id,
                sent.message_id,
                sent.normalized_text,
                int(config.raw_retention_days),
                segments=sent.segments,
                reply_chain=sent.reply_chain,
                forward_tree=sent.forward_tree,
                media_refs=sent.media_refs,
            )
        else:
            dbg(f"群 {group_id} 回复投递状态未知,按可能已送达推进冷却/去重但不写消息历史")
        recent.append(
            {
                "input": input_fingerprint,
                "response": response_fingerprint,
                "text": reply_text[:500],
                "at": now.isoformat(),
            }
        )
        config.recent_response_fingerprints = recent[-8:]
        config.last_response_fingerprint = response_fingerprint
        config.last_response_input_fingerprint = input_fingerprint
        config.last_response_at = now
        config.last_agent_at = now
        if topic_changed:
            config.context_epoch += 1
            config.active_topic = next_active_topic
            dbg(
                f"群 {group_id} 话题切换: epoch={config.context_epoch} "
                f"topic={config.active_topic!r}"
            )
        await session.commit()
    except Exception as exc:  # noqa: BLE001
        trace_event(
            "state",
            "回复后状态提交",
            status="degraded",
            output={
                "rolled_back": True,
                "delivery_state": sent.delivery_state,
                "error_type": type(exc).__name__,
            },
            detail="消息已结束投递流程，但本地消息历史/去重/冷却状态写入失败",
        )
        # 消息已经发出或投递结果未知；状态丢失只影响本地上下文/重复抑制，不能上抛。
        dbg_exc(f"群 {group_id} 回复后状态提交失败,已回滚")
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            dbg_exc(f"群 {group_id} 回复后状态回滚失败(忽略)")
    else:
        trace_event(
            "state",
            "回复后状态提交",
            output={
                "recent_fingerprints": len(recent[-8:]),
                "context_epoch": config.context_epoch,
                "delivery_state": sent.delivery_state,
            },
        )
        dbg(f"群 {group_id} 回复后状态已提交(指纹记录 {len(recent[-8:])} 条)")

    if short_conversation_enabled:
        try:
            mark_bot_reply(
                int(bot.self_id),
                group_id,
                topic=str(next_active_topic or normalized.plain_text or ""),
                source="dialogue",
                max_bot_turns=max_followup_bot_turns,
            )
        except Exception as exc:  # noqa: BLE001
            trace_event(
                "state",
                "短会话状态推进",
                status="degraded",
                output={"error_type": type(exc).__name__},
                detail="正文已经结束投递流程，但短会话内存状态推进失败",
            )
            dbg_exc(f"群 {group_id} 短会话状态推进失败(忽略)")


async def _process_group_message(
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
            if config is None or not await agent_runtime_enabled(
                session, group_id, config=config
            ):
                dbg(
                    f"群 {group_id} 处理中止: Agent 总开关"
                    f"{'配置缺失' if config is None else '已关闭'}"
                )
                return
            actor_user_id = int(event.get_user_id())
            model = resolve_llm_request("agent_dialogue").model
            context_started = time.monotonic()
            context = await _load_context(
                session,
                group_id,
                config,
                bot_id,
                focus_user_ids=_current_turn_focus_ids(
                    actor_user_id, normalized, bot_id=bot_id
                ),
                query_text=normalized.prompt_text(),
                exclude_message_id=int(message_id) if message_id is not None else None,
                context_model=model,
                completion_reserve=800,
                context_token_limit=2400,
            )
            trace_event(
                "context",
                "上下文选择与装箱",
                input={
                    "focus_user_ids": _current_turn_focus_ids(
                        actor_user_id, normalized, bot_id=bot_id
                    ),
                    "query_chars": len(normalized.prompt_text()),
                    "query_preview": normalized.prompt_text()[:240],
                    "context_token_limit": 2400,
                    "completion_reserve": 800,
                },
                output={
                    "messages": len(list(context.get("messages") or [])),
                    "members": len(list(context.get("members") or [])),
                    "memories": len(list(context.get("memories") or [])),
                    "relations": len(list(context.get("relations") or [])),
                    "model": model,
                },
                duration_ms=(time.monotonic() - context_started) * 1000,
            )
            capability_started = time.monotonic()
            capabilities = await probe_group_capabilities(bot, group_id)
            allow_admin_tools = await user_can_manage_group(
                bot, group_id, actor_user_id
            )
            dbg(
                f"群 {group_id} 能力探测完成: bot_role={capabilities.role!r} "
                f"can_manage={capabilities.can_manage} actions={len(capabilities.actions)} 个 "
                f"发起人 {actor_user_id} 管理工具权限={allow_admin_tools}"
            )
            has_target_mentions = any(
                int(user_id) != int(bot_id) for user_id in normalized.mentions
            )
            tool_intent_text = normalized.intent_text()
            has_reply_context = bool(normalized.reply_chain)
            has_media_context = bool(normalized.media_refs)
            selected_tool_names = select_dialogue_tool_names(
                tool_intent_text,
                has_reply=has_reply_context,
                has_mentions=has_target_mentions,
                has_media=has_media_context,
                allow_admin_tools=allow_admin_tools,
            )
            message_segment_types = select_dialogue_message_segment_types(
                tool_intent_text,
                has_target_mentions=has_target_mentions,
                has_reply=has_reply_context,
                has_media=has_media_context,
            )
            tools = build_tool_schemas(
                capabilities,
                allow_admin_tools=allow_admin_tools,
                segment_capabilities=get_segment_capabilities(bot, group_id),
                privileged_allowlist=set(config.tool_allowlist or []),
                include_names=selected_tool_names,
                message_segment_types=(
                    message_segment_types
                    if "send_message" in selected_tool_names
                    else None
                ),
            )
            round_limit = dialogue_tool_round_limit(selected_tool_names)
            trace_event(
                "capability",
                "协议能力与工具权限计算",
                output={
                    "bot_role": capabilities.role,
                    "bot_can_manage": capabilities.can_manage,
                    "onebot_actions": sorted(capabilities.actions),
                    "actor_can_manage": allow_admin_tools,
                    "round_limit": round_limit,
                    "message_segment_types": sorted(message_segment_types),
                    "selected_tool_names": sorted(selected_tool_names),
                    "tool_names": [
                        str(item.get("function", {}).get("name") or "")
                        for item in tools
                    ],
                    "tool_schema_chars": len(
                        json.dumps(tools, ensure_ascii=False, separators=(",", ":"))
                    ),
                    "tool_count": len(tools),
                },
                duration_ms=(time.monotonic() - capability_started) * 1000,
            )
            dbg(
                f"群 {group_id} 本轮可用工具 {len(tools)} 个,"
                f"模型轮次上限={round_limit}"
            )
            media_started = time.monotonic()
            media_diagnostics: list[dict[str, Any]] = []
            media_blocks, cached_captions, media_digests = await prepare_image_inputs(
                bot,
                group_id,
                normalized.media_refs,
                session=session,
                cache_enabled=bool(config.media_cache_enabled),
                diagnostics=media_diagnostics,
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
            trace_event(
                "media",
                "多模态输入准备",
                input={
                    "media": [
                        {"type": item.get("type"), "source": item.get("source", "current")}
                        for item in normalized.media_refs
                    ]
                },
                output={
                    "vision_blocks": len(media_blocks),
                    "cached_captions": len(cached_captions),
                    "content_hashes": [digest[:12] for digest in media_digests],
                    "items": media_diagnostics,
                    "cache_enabled": bool(config.media_cache_enabled),
                    "multimodal_mode": resolve_llm_request("agent_dialogue").multimodal,
                },
                duration_ms=(time.monotonic() - media_started) * 1000,
            )
            current_turn: CurrentTurn = build_current_turn(
                message_id=int(message_id) if message_id is not None else None,
                user_id=actor_user_id,
                name=event.sender.card or event.sender.nickname,
                role=str(event.sender.role or "member"),
                title=event.sender.title,
                content=user_prompt,
                mentions=normalized.mentions,
                reply_chain=normalized.reply_chain,
                trigger=normalized.trigger_source or "explicit_call",
                received_at=now_beijing(),
                media_refs=normalized.media_refs,
                forward_nodes=len(normalized.forward_tree),
                truncated=normalized.truncated,
            )
            dbg(f"群 {group_id} 对话模型={model!r}")
            prompt_started = time.monotonic()
            messages, _prefix_fingerprint = build_messages(
                persona=resolve_persona(config),
                tools=tools,
                context=context,
                user_prompt=user_prompt,
                current_turn=current_turn,
                media_inputs=media_blocks
                if resolve_llm_request("agent_dialogue").multimodal
                != "unsupported"
                else None,
            )
            cache_key = prompt_cache_key(
                persona=resolve_persona(config),
                tools=tools,
                model=model,
                persona_version=config.persona_version,
            )
            stable_key = stable_context_key(context)
            prompt_shape = _trace_prompt_shape(messages)
            trace_event(
                "prompt",
                "Prompt 构建",
                input={
                    "tool_count": len(tools),
                    "media_blocks": len(media_blocks),
                    "persona_version": config.persona_version,
                },
                output={
                    "message_count": len(messages),
                    **prompt_shape,
                    "current_turn_chars": len(user_prompt),
                    "current_turn_preview": user_prompt[:240],
                    "tool_names": [
                        str(item.get("function", {}).get("name") or "")
                        for item in tools
                    ],
                    "prefix_fingerprint": _prefix_fingerprint[:12],
                    "prompt_cache": "hit" if cache_key in _PROMPT_CACHE_KEYS else "miss",
                    "context_cache": "hit" if stable_key in _PROMPT_CACHE_KEYS else "miss",
                },
                duration_ms=(time.monotonic() - prompt_started) * 1000,
            )
            dbg(
                f"群 {group_id} 提示词构建完成: messages={len(messages)} 条 "
                f"prompt 前缀指纹={_prefix_fingerprint[:12]}… "
                f"前缀稳定性={'复用' if cache_key in _PROMPT_CACHE_KEYS else '变化'} "
                f"稳定上下文={'复用' if stable_key in _PROMPT_CACHE_KEYS else '变化'} "
                f"用户 prompt={user_prompt!r}"
            )
            try:
                from ..metrics import record_agent_cache

                record_agent_cache(
                    "prompt", "hit" if cache_key in _PROMPT_CACHE_KEYS else "miss"
                )
                # 只观测本地前缀是否稳定；服务商实际缓存 token 由 usage 指标记录。
                record_agent_cache(
                    "context", "hit" if stable_key in _PROMPT_CACHE_KEYS else "miss"
                )
            except Exception:  # noqa: BLE001
                dbg_exc(f"群 {group_id} 上报 prompt 缓存指标失败(忽略)")
            for key in (cache_key, stable_key):
                _PROMPT_CACHE_KEYS[key] = None
                _PROMPT_CACHE_KEYS.move_to_end(key)
            while len(_PROMPT_CACHE_KEYS) > _PROMPT_CACHE_LIMIT:
                _PROMPT_CACHE_KEYS.popitem(last=False)
            fallback_attempted = False
            deadline = time.monotonic() + _MAX_TURN_SECONDS
            rounds = 0
            turn_usage: dict[str, int] = {}
            while rounds < round_limit:
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
                llm_started = time.monotonic()
                try:
                    completion = await complete_with_tools_result(  # pyright: ignore[reportArgumentType]
                        messages,  # pyright: ignore[reportArgumentType]
                        tools,  # pyright: ignore[reportArgumentType]
                        task="agent_dialogue",
                        max_tokens=800,
                        timeout=30,
                        multimodal=bool(media_blocks),
                        raise_on_unsupported=bool(media_blocks)
                        and not fallback_attempted,
                    )
                except LLMMultimodalUnsupportedError:
                    trace_event(
                        "llm",
                        "模型多模态请求",
                        status="degraded",
                        output={"model": model, "fallback": "vision_caption"},
                        detail="模型不支持当前多模态输入，改用视觉转述后重建 Prompt",
                        duration_ms=(time.monotonic() - llm_started) * 1000,
                        round_index=rounds + 1,
                    )
                    dbg(
                        f"群 {group_id} 模型不支持多模态,降级为视觉转述重建提示词(不占轮次)"
                    )
                    fallback_attempted = True
                    user_prompt = f"{normalized.prompt_text()}\n{await _describe_images(group_id, normalized, media_blocks, session, config, cached_captions, media_digests)}"
                    current_turn = CurrentTurn(
                        **{**current_turn.as_dict(), "content": user_prompt}
                    )
                    messages, _prefix_fingerprint = build_messages(
                        persona=resolve_persona(config),
                        tools=tools,
                        context=context,
                        user_prompt=user_prompt,
                        current_turn=current_turn,
                    )
                    media_blocks = []
                    # 多模态降级重建提示词，不占用工具轮次。
                    continue
                rounds += 1
                usage = _accumulate_turn_usage(turn_usage, completion)
                response = completion.message
                if response is None:
                    trace_event(
                        "llm",
                        "模型调用",
                        status="degraded",
                        output={
                            "model": model,
                            "response": "none",
                            "outcome": completion.outcome,
                            "usage": usage,
                        },
                        detail="LLM 返回空结果，进入确定性兜底回复",
                        duration_ms=(time.monotonic() - llm_started) * 1000,
                        round_index=rounds,
                    )
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
                trace_event(
                    "llm",
                    "模型调用",
                    output={
                        "model": model,
                        "content_chars": len(content),
                        "tool_calls": [
                            str(getattr(getattr(call, "function", None), "name", "") or "")
                            for call in tool_calls
                        ],
                        "finish_reason": completion.finish_reason,
                        "content_preview": content[:320],
                        "usage": usage,
                    },
                    duration_ms=(time.monotonic() - llm_started) * 1000,
                    round_index=rounds,
                )
                dbg(
                    f"群 {group_id} 第 {rounds}/{round_limit} 轮 LLM 响应: "
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
                            render_current_turn(current_turn),
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
                round_sent_message = False
                discovered_tool_names: set[str] = set()
                for call in tool_calls:
                    function = getattr(call, "function", None)
                    if function is None:
                        dbg(
                            f"群 {group_id} 跳过缺少 function 的 tool_call id={getattr(call, 'id', None)}"
                        )
                        continue
                    tool_name = str(getattr(function, "name", "") or "")
                    raw_args = getattr(function, "arguments", "{}") or "{}"
                    tool_started = time.monotonic()
                    dbg(
                        f"群 {group_id} 第 {rounds} 轮工具调用: "
                        f"name={tool_name!r} args={raw_args}"
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
                        if (
                            tool_name in _VISIBLE_SEND_TOOLS
                            and enqueued_at is not None
                            and is_pending_trigger_expired(enqueued_at)
                        ):
                            result = {
                                "ok": False,
                                "error": "触发消息已过期，取消发送",
                                "expired": True,
                            }
                            dbg(
                                f"群 {group_id} 工具 {tool_name} 发送前触发已过期,取消副作用"
                            )
                        else:
                            result = await execute_tool(
                                tool_name,
                                args,
                                bot=bot,
                                group_id=group_id,
                                actor_user_id=actor_user_id,
                                session=session,
                                capabilities=capabilities,
                            )
                    if tool_name == "discover_tools" and bool(result.get("ok")):
                        discovery = result.get("result")
                        discovery_rows = (
                            discovery.get("tools", [])
                            if isinstance(discovery, dict)
                            else []
                        )
                        for item in discovery_rows:
                            if isinstance(item, dict) and item.get("name"):
                                discovered_tool_names.add(str(item["name"]))
                    trace_event(
                        "tool",
                        f"工具 {tool_name or '[unknown]'}",
                        status=(
                            "success"
                            if bool(result.get("ok"))
                            else "failed"
                        ),
                        input={"arguments": args},
                        output={
                            "ok": bool(result.get("ok")),
                            "error": result.get("error"),
                            "ends_turn": _visible_tool_send_ends_turn(result),
                        },
                        duration_ms=(time.monotonic() - tool_started) * 1000,
                        round_index=rounds,
                    )
                    dbg(
                        f"群 {group_id} 工具 {tool_name!r} 返回: "
                        f"{json.dumps(result, ensure_ascii=False)}"
                    )
                    if _visible_tool_send_ends_turn(result):
                        round_sent_message = True
                        payload = (
                            result.get("result", {}).get("outbound", {})
                            if isinstance(result.get("result"), dict)
                            else {}
                        )
                        if isinstance(payload, dict):
                            await persist_bot_reply(
                                session,
                                int(bot.self_id),
                                group_id,
                                _extract_message_id(payload.get("message_id")),
                                str(payload.get("text") or ""),
                                int(config.raw_retention_days),
                                segments=(
                                    payload.get("segments")
                                    if isinstance(payload.get("segments"), list)
                                    else []
                                ),
                                reply_chain=(
                                    payload.get("reply_chain")
                                    if isinstance(payload.get("reply_chain"), list)
                                    else []
                                ),
                                forward_tree=(
                                    payload.get("forward_tree")
                                    if isinstance(payload.get("forward_tree"), list)
                                    else []
                                ),
                                media_refs=(
                                    payload.get("media_refs")
                                    if isinstance(payload.get("media_refs"), list)
                                    else []
                                ),
                            )
                            now = now_beijing()
                            fingerprint_source = str(payload.get("text") or "") or json.dumps(
                                {
                                    "segments": payload.get("segments", []),
                                    "forward_tree": payload.get("forward_tree", []),
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                            )
                            response_fingerprint = hashlib.sha256(
                                fingerprint_source.casefold().encode("utf-8")
                            ).hexdigest()
                            input_fingerprint = hashlib.sha256(
                                render_current_turn(current_turn).casefold().encode("utf-8")
                            ).hexdigest()
                            recent = list(config.recent_response_fingerprints or [])
                            recent.append(
                                {
                                    "input": input_fingerprint,
                                    "response": response_fingerprint,
                                    "text": str(payload.get("text") or "")[:500],
                                    "at": now.isoformat(),
                                }
                            )
                            config.recent_response_fingerprints = recent[-8:]
                            config.last_response_fingerprint = response_fingerprint
                            config.last_response_input_fingerprint = input_fingerprint
                            config.last_response_at = now
                            config.last_agent_at = now
                            if config.short_conversation_enabled:
                                mark_bot_reply(
                                    int(bot.self_id),
                                    group_id,
                                    topic=str(config.active_topic or normalized.plain_text or ""),
                                    source="dialogue",
                                    max_bot_turns=persona_behavior(
                                        config
                                    ).max_followup_bot_turns,
                                )
                    assistant["tool_calls"].append(
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
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
                        trace_event(
                            "state",
                            "工具轮状态提交",
                            status="failed",
                            detail="数据库提交失败并已回滚",
                            round_index=rounds,
                        )
                        dbg_exc(f"群 {group_id} 工具轮状态提交失败,已回滚")
                        await session.rollback()
                    else:
                        trace_event(
                            "state",
                            "工具轮状态提交",
                            output={"tool": tool_name},
                            round_index=rounds,
                        )
                    # 提交会过期会话内对象；后续轮次还要读取 config 属性，
                    # 先刷新避免同步惰性加载（MissingGreenlet）。
                    await session.refresh(config)
                    if round_sent_message:
                        # 一次模型决策最多执行一个用户可见发送动作；避免模型同一轮
                        # 同时调用 send_message/send_forward 连发多条。
                        break
                if discovered_tool_names and not round_sent_message:
                    selected_tool_names = frozenset(
                        set(selected_tool_names) | discovered_tool_names
                    )
                    tools = build_tool_schemas(
                        capabilities,
                        allow_admin_tools=allow_admin_tools,
                        segment_capabilities=get_segment_capabilities(bot, group_id),
                        privileged_allowlist=set(config.tool_allowlist or []),
                        include_names=selected_tool_names,
                        message_segment_types=(
                            message_segment_types
                            if "send_message" in selected_tool_names
                            else None
                        ),
                    )
                    loaded_names = {
                        str(item.get("function", {}).get("name") or "")
                        for item in tools
                    }
                    loaded_discoveries = sorted(
                        name for name in discovered_tool_names if name in loaded_names
                    )
                    round_limit = max(
                        round_limit,
                        min(MAX_TOOL_ROUNDS, rounds + 2),
                    )
                    trace_event(
                        "capability",
                        "动态工具发现",
                        output={
                            "requested": sorted(discovered_tool_names),
                            "loaded": loaded_discoveries,
                            "tool_count": len(tools),
                            "round_limit": round_limit,
                        },
                        round_index=rounds,
                    )
                if round_sent_message:
                    dbg(f"群 {group_id} 工具已发送用户可见消息,结束本轮避免重复回复")
                    return
            # 走到这里说明所有轮次都被工具调用耗尽、始终没有最终回复。
            # 其余分支均已 return；给用户一个交代，不能静默。
            dbg(
                f"群 {group_id} {round_limit} 轮全部被工具调用耗尽,无最终回复,"
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


async def process_group_message(
    bot: Bot,
    event: GroupMessageEvent,
    normalized: NormalizedMessage,
    *,
    enqueued_at: float | None = None,
) -> None:
    """处理明确触发并记录低基数的端到端回合指标。"""

    started = time.monotonic()
    outcome = "completed"
    trace = begin_execution_trace(
        int(event.group_id),
        mode="dialogue",
        source="runtime",
        trigger_source=normalized.trigger_source or "explicit_call",
        actor_user_id=int(event.get_user_id()),
        message_id=(
            int(event.message_id)
            if getattr(event, "message_id", None) is not None
            else None
        ),
    )
    token = bind_execution_trace(trace)
    for stage in normalized.parse_trace:
        if not isinstance(stage, dict):
            continue
        trace_event(
            "parse",
            str(stage.get("label") or "消息解析"),
            output=(
                stage.get("output")
                if isinstance(stage.get("output"), dict)
                else {}
            ),
            duration_ms=(
                float(stage.get("duration_ms") or 0.0)
                if stage.get("duration_ms") is not None
                else None
            ),
        )
    trace_event(
        "intake",
        "消息归一化完成",
        output={
            "trigger_source": normalized.trigger_source or "explicit_call",
            "trigger_signals": dict(normalized.trigger_signals),
            "text_chars": len(normalized.plain_text),
            "text_preview": normalized.plain_text[:240],
            "segment_types": [item.type for item in normalized.segments],
            "media": [
                {
                    "type": item.get("type"),
                    "source": item.get("source", "current"),
                }
                for item in normalized.media_refs
            ],
            "reply_depth": len(normalized.reply_chain),
            "forward_nodes": len(normalized.forward_tree),
            "mentions": normalized.mentions,
            "truncated": normalized.truncated,
            "queue_wait_ms": (
                round(max(started - enqueued_at, 0.0) * 1000, 1)
                if enqueued_at is not None
                else None
            ),
        },
    )
    try:
        await _process_group_message(
            bot,
            event,
            normalized,
            enqueued_at=enqueued_at,
        )
    except BaseException as exc:
        outcome = "error"
        trace_event(
            "turn",
            "执行异常",
            status="failed",
            output={
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:320],
            },
            detail="未处理异常终止本轮",
        )
        raise
    finally:
        trace_event(
            "turn",
            "回合结束",
            status="failed" if outcome == "error" else "success",
            output={"outcome": outcome},
            duration_ms=max(time.monotonic() - started, 0.0) * 1000,
        )
        finish_execution_trace(trace, outcome=outcome)
        reset_execution_trace(token)
        try:
            from ..metrics import record_agent_turn

            record_agent_turn(
                "dialogue",
                outcome,
                max(time.monotonic() - started, 0.0),
                queue_wait_seconds=(
                    max(started - enqueued_at, 0.0)
                    if enqueued_at is not None
                    else None
                ),
            )
        except Exception:  # noqa: BLE001
            dbg_exc("Agent 对话回合指标上报失败(忽略)")
