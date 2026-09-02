# ruff: noqa: E501,TID252,TC001,TC003,UP035,C901,PLR0912,PLR0915,PLR2004,SIM114
"""群聊 Agent 历史消息稀疏化、相关性筛选与调试追踪。

该模块只负责把数据库中的历史消息转成 Prompt 候选，并按当前回合选择有限的
相关历史。选择追踪与 Prompt 数据分离：trace 只供 WebUI/测试诊断，绝不能注入
模型上下文。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from ..data_models.group_agent_message import GroupAgentMessage
from .context import topic_break_before, trim_context_messages
from .memory import extract_bigrams

CONTEXT_HISTORY_MAX_MESSAGES = 16
CONTEXT_HISTORY_CHAR_BUDGET = 2_800
CONTEXT_MESSAGE_CHAR_LIMIT = 500
CONTEXT_FRESH_MINUTES = 6
CONTEXT_CLUSTER_MAX_AGE_MINUTES = 18
CONTEXT_CLUSTER_GAP_MINUTES = 6
CONTEXT_RELEVANT_MAX_AGE_MINUTES = 60
CONTEXT_PROACTIVE_MAX_AGE_MINUTES = 45
CONTEXT_PROACTIVE_MAX_MESSAGES = 10
LOW_INFO_HISTORY_TEXTS = frozenset(
    {"", "了", "嗯", "哦", "啊", "好", "好的", "ok", "OK", "[图片]", "[json]"}
)
_MEDIA_QUERY_RE = re.compile(
    r"(?:图片|截图|照片|这张|那张|上面那|前面那|刚才那|刚刚那|第[一二三四五六七八九十\d]+张|图里|图上)"
)


@dataclass(frozen=True, slots=True)
class ContextSelection:
    """历史选择结果；trace 不属于模型上下文。"""

    messages: list[dict[str, Any]]
    trace: list[dict[str, Any]]


def bot_message_meta(row: GroupAgentMessage) -> dict[str, Any] | None:
    """给下一轮模型看的 Bot 自己消息摘要；不回灌路径或完整转发 payload。"""

    if str(row.role or "") != "bot":
        return None
    segments = list(row.segments or [])
    segment_types: list[str] = []
    mentions: list[int] = []
    for item in segments[:12]:
        if not isinstance(item, dict):
            continue
        segment_type = str(item.get("type") or "").strip()
        if segment_type and segment_type != "text":
            segment_types.append(segment_type)
        if segment_type == "at":
            data = item.get("data")
            raw_user_id = data.get("qq") if isinstance(data, dict) else None
            if not isinstance(raw_user_id, (int, str)):
                continue
            try:
                user_id = int(raw_user_id)
            except (TypeError, ValueError):
                continue
            if user_id > 0 and user_id not in mentions:
                mentions.append(user_id)
    reply_to: list[int] = []
    for item in list(row.reply_chain or [])[:4]:
        if not isinstance(item, dict):
            continue
        raw_message_id = item.get("message_id")
        if not isinstance(raw_message_id, (int, str)):
            continue
        try:
            target = int(raw_message_id)
        except (TypeError, ValueError):
            continue
        if target and target not in reply_to:
            reply_to.append(target)
    media = [
        {
            "type": str(item.get("type") or "media")[:24],
            **(
                {"reaction_id": str(item.get("reaction_id"))[:64]}
                if item.get("reaction_id")
                else {}
            ),
        }
        for item in list(row.media_refs or [])[:4]
        if isinstance(item, dict)
    ]
    forward_nodes = min(len(list(row.forward_tree or [])), 20)
    meta: dict[str, Any] = {}
    if segment_types:
        meta["segment_types"] = segment_types
    if mentions:
        meta["mentions"] = mentions
    if reply_to:
        meta["reply_to"] = reply_to
    if media:
        meta["media"] = media
    if forward_nodes:
        meta["forward_nodes"] = forward_nodes
    return meta or None


def history_message_meta(row: GroupAgentMessage) -> dict[str, Any]:
    """历史消息的寻址信息；默认空值不进入 Prompt。"""

    mentions: list[int] = []
    for item in list(row.segments or [])[:20]:
        if not isinstance(item, dict):
            continue
        segment_type = str(item.get("type") or "").strip()
        if segment_type != "at":
            continue
        data = item.get("data")
        raw_user_id = data.get("qq") if isinstance(data, dict) else None
        try:
            user_id = int(str(raw_user_id))
        except (TypeError, ValueError):
            continue
        if user_id > 0 and user_id not in mentions:
            mentions.append(user_id)
    reply_to: dict[str, Any] | None = None
    chain = list(row.reply_chain or [])
    if chain and isinstance(chain[0], dict):
        raw = chain[0]
        reply_to = {
            "message_id": raw.get("message_id"),
            "user_id": raw.get("user_id"),
            "name": str(raw.get("nickname") or "未知用户")[:64],
            "text": str(raw.get("text") or "")[:240],
        }
    media_types = [
        str(item.get("type") or "media")[:24]
        for item in list(row.media_refs or [])[:4]
        if isinstance(item, dict)
    ]
    meta: dict[str, Any] = {}
    if mentions:
        meta["mentions"] = mentions
    if reply_to is not None:
        meta["reply_to"] = reply_to
    if media_types:
        meta["media_types"] = media_types
    forward_nodes = min(len(list(row.forward_tree or [])), 20)
    if forward_nodes:
        meta["forward_nodes"] = forward_nodes
    return meta


def history_message_payload(
    row: GroupAgentMessage,
    *,
    context_now: datetime,
    previous_at: datetime | None,
) -> dict[str, Any]:
    """把历史消息渲染成稀疏 Prompt 结构；默认值不占 token。"""

    message: dict[str, Any] = {
        "message_id": row.message_id,
        "user_id": row.user_id,
        "text": row.normalized_text,
        "minutes_ago": max(
            0, int((context_now - row.received_at).total_seconds() // 60)
        ),
    }
    if row.sender_name:
        message["name"] = row.sender_name
    if row.role and str(row.role) != "member":
        message["role"] = row.role
    if row.title:
        message["title"] = row.title
    if topic_break_before(previous_at, row.received_at):
        message["topic_break_before"] = True
    message.update(history_message_meta(row))
    if (bot_meta := bot_message_meta(row)) is not None:
        message["message_meta"] = bot_meta
    return message


def _minutes_ago(item: dict[str, Any]) -> int:
    try:
        return max(int(item.get("minutes_ago") or 0), 0)
    except (TypeError, ValueError):
        return 0


def _is_low_info(item: dict[str, Any]) -> bool:
    text = str(item.get("text") or "").strip()
    if text in LOW_INFO_HISTORY_TEXTS:
        return True
    return bool(re.fullmatch(r"\[表情(?::\d+)?\]", text))


def _directly_touches_focus(
    item: dict[str, Any], focus_user_ids: set[int]
) -> bool:
    if not focus_user_ids:
        return False
    for raw_user_id in item.get("mentions") or []:
        if isinstance(raw_user_id, int):
            user_id = raw_user_id
        elif isinstance(raw_user_id, str) and raw_user_id.isdecimal():
            user_id = int(raw_user_id)
        else:
            continue
        if user_id in focus_user_ids:
            return True
    reply_to = item.get("reply_to")
    if isinstance(reply_to, dict):
        raw_user_id = reply_to.get("user_id")
        if isinstance(raw_user_id, int):
            return raw_user_id in focus_user_ids
        if isinstance(raw_user_id, str) and raw_user_id.isdecimal():
            return int(raw_user_id) in focus_user_ids
    return False


def _trace_row(
    item: dict[str, Any], *, selected: bool, reason: str, score: float | None = None
) -> dict[str, Any]:
    text = str(item.get("text") or "")
    row: dict[str, Any] = {
        "message_id": item.get("message_id"),
        "user_id": item.get("user_id"),
        "name": item.get("name"),
        "role": item.get("role") or "member",
        "title": item.get("title"),
        "text": text[:600],
        "text_truncated": len(text) > 600,
        "minutes_ago": _minutes_ago(item),
        "selected": selected,
        "reason": reason,
    }
    if score is not None:
        row["score"] = round(float(score), 3)
    return row


def select_context_messages(
    messages: list[dict[str, Any]],
    *,
    focus_user_ids: Sequence[int] | None = None,
    query_text: str | None = None,
) -> ContextSelection:
    """选择真正相关的历史，并返回不进入 Prompt 的选择追踪。"""

    if not messages:
        return ContextSelection([], [])
    focus = {
        int(user_id)
        for user_id in (focus_user_ids or [])
        if isinstance(user_id, int) and int(user_id) > 0
    }
    query = str(query_text or "").strip()
    query_tokens = extract_bigrams(query[:1000]) if query else set()
    media_query = bool(query and _MEDIA_QUERY_RE.search(query))
    selected: set[int] = set()
    reasons: dict[int, tuple[str, float | None]] = {}

    def choose(index: int, reason: str, score: float | None = None) -> None:
        selected.add(index)
        previous = reasons.get(index)
        if previous is None or (score or 0.0) > (previous[1] or 0.0):
            reasons[index] = (reason, score)

    if query:
        latest_age = _minutes_ago(messages[-1])
        if latest_age <= CONTEXT_FRESH_MINUTES:
            next_newer_age = latest_age
            for index in range(len(messages) - 1, -1, -1):
                item = messages[index]
                age = _minutes_ago(item)
                if age > CONTEXT_CLUSTER_MAX_AGE_MINUTES:
                    break
                if age - next_newer_age > CONTEXT_CLUSTER_GAP_MINUTES:
                    break
                if (
                    not _is_low_info(item)
                    or age <= 2
                    or _directly_touches_focus(item, focus)
                ):
                    choose(index, "recent_cluster")
                next_newer_age = age

        relevance: list[tuple[float, int, str]] = []
        for index, item in enumerate(messages):
            age = _minutes_ago(item)
            if age > CONTEXT_RELEVANT_MAX_AGE_MINUTES:
                continue
            if media_query and item.get("media_types"):
                # 图片指代无法靠文本 bigram 找回；把近期含媒体消息作为候选，
                # 真正的字节恢复仍由 resolve_media_context 限量完成。
                relevance.append((12.0 - age / 60.0, index, "media_reference"))
                continue
            direct = _directly_touches_focus(item, focus)
            item_tokens = extract_bigrams(str(item.get("text") or "")[:700])
            overlap = query_tokens & item_tokens
            strong_ascii = any(token.isascii() and len(token) >= 3 for token in overlap)
            strong_text = len(overlap) >= 2 or strong_ascii
            if not direct and not strong_text:
                continue
            score = (10.0 if direct else 0.0) + len(overlap) * 2.0 - age / 60.0
            reason = "focus_relation" if direct else "query_overlap"
            relevance.append((score, index, reason))
        for score, index, reason in sorted(relevance, reverse=True)[:6]:
            choose(index, reason, score)
            for neighbor in (index - 1, index + 1):
                if neighbor < 0 or neighbor >= len(messages):
                    continue
                if abs(_minutes_ago(messages[neighbor]) - _minutes_ago(messages[index])) <= 2 and not _is_low_info(messages[neighbor]):
                    choose(neighbor, "relevant_neighbor")
    else:
        next_newer_age = _minutes_ago(messages[-1])
        for index in range(len(messages) - 1, -1, -1):
            item = messages[index]
            age = _minutes_ago(item)
            if age > CONTEXT_PROACTIVE_MAX_AGE_MINUTES:
                break
            if age - next_newer_age > CONTEXT_CLUSTER_GAP_MINUTES:
                break
            if not _is_low_info(item) or age <= 2:
                choose(index, "proactive_recent_cluster")
            next_newer_age = age
            if len(selected) >= CONTEXT_PROACTIVE_MAX_MESSAGES:
                break

    chosen = [messages[index] for index in sorted(selected)]
    trimmed = trim_context_messages(
        chosen,
        max_messages=CONTEXT_HISTORY_MAX_MESSAGES,
        max_message_chars=CONTEXT_MESSAGE_CHAR_LIMIT,
        char_budget=CONTEXT_HISTORY_CHAR_BUDGET,
    )
    # trim_context_messages 会复制 dict，不能按对象身份判断；用原始顺序+message_id 建键。
    kept_keys = [
        (item.get("message_id"), item.get("user_id"), item.get("minutes_ago"))
        for item in trimmed
    ]
    kept_key_set = set(kept_keys)
    trace: list[dict[str, Any]] = []
    for index, item in enumerate(messages):
        key = (item.get("message_id"), item.get("user_id"), item.get("minutes_ago"))
        if index in selected:
            reason, score = reasons.get(index, ("selected", None))
            if key in kept_key_set:
                trace.append(_trace_row(item, selected=True, reason=reason, score=score))
            else:
                trace.append(_trace_row(item, selected=False, reason="context_budget", score=score))
            continue
        age = _minutes_ago(item)
        if _is_low_info(item):
            reason = "low_information"
        elif query and age > CONTEXT_RELEVANT_MAX_AGE_MINUTES:
            reason = "stale"
        elif not query and age > CONTEXT_PROACTIVE_MAX_AGE_MINUTES:
            reason = "stale"
        else:
            reason = "not_relevant"
        trace.append(_trace_row(item, selected=False, reason=reason))
    return ContextSelection(trimmed, trace)


def select_context_messages_only(
    messages: list[dict[str, Any]],
    *,
    focus_user_ids: Sequence[int] | None = None,
    query_text: str | None = None,
) -> list[dict[str, Any]]:
    """兼容旧调用：只返回 Prompt 历史。"""

    return select_context_messages(
        messages, focus_user_ids=focus_user_ids, query_text=query_text
    ).messages


__all__ = [
    "ContextSelection",
    "bot_message_meta",
    "history_message_meta",
    "history_message_payload",
    "select_context_messages",
    "select_context_messages_only",
]
