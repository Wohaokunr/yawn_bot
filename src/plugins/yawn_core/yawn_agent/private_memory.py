# ruff: noqa: TID252
"""私聊记忆：Bot 在私聊里了解到的、关于这位用户本人的事。

存在 ``AgentMemory`` 里，``scope="private"``、``group_id=NULL``、
``subject_user_id=用户``、``memory_type="private_fact"``。群里的任何加载路径都按
``group_id`` 过滤，读不到这些行——私聊内容不会流进群。

来源沿用记忆系统的语义：

- ``agent``：用户在私聊里亲口让 Bot 记住的（``remember``），自动整理不覆盖；
- ``auto``：每隔几轮由记忆模型从私聊记录里整理出来的（``consolidate``），会过期；
- 所有入库文本都过 ``memory_hygiene``（脱敏、拒绝注入和高敏感类别）。

``group_id`` 为 NULL 时 SQLite 的唯一约束不生效，所以这里一律先查后写。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from ..data_models.agent_memory import AgentMemory
from ..data_models.chat_message import ChatMessage
from ..data_models.chat_session import ChatSession
from ..private_chat_bridge import (
    PRIVATE_MEMORY_LABELS,
    PRIVATE_MEMORY_SCOPE,
    PRIVATE_MEMORY_TYPE,
    delete_private_memories,
    private_memory_query,
)
from .log import dbg, dbg_exc
from .memory_hygiene import (
    is_near_duplicate,
    normalize_list_fact,
    reject_reason,
    sanitize_fact,
)
from .memory_self import SELF_FACT_KEYS
from .private_prompt import PRIVATE_MEMORY_SYSTEM
from .prompt_sections import system_messages

PRIVATE_SCOPE = PRIVATE_MEMORY_SCOPE
LIST_KEYS = frozenset({"hobby", "preference", "dislike", "skill"})
# 亲口要求记住的：一年；自动整理的：90 天；近期状态：14 天。
AGENT_TTL_DAYS = 365
AUTO_TTL_DAYS = 90
STATUS_TTL_DAYS = 14
# 每位用户最多保留的私聊事实条数（每类一行，类别本来就有限）。
MAX_FACTS = len(SELF_FACT_KEYS)
# 每多少条用户消息整理一次。
CONSOLIDATE_EVERY = 6
# 整理时最多看最近多少条私聊消息。
CONSOLIDATE_WINDOW = 30
_CONTENT_LIMIT = 100

KEY_LABELS = PRIVATE_MEMORY_LABELS

_REJECT_MESSAGES = {
    "pii_only": "联系方式、证件号或链接我不会记，保护你的隐私",
    "instruction": "这句话像是在给我下指令，不能当成关于你的资料记",
    "sensitive": "这类敏感信息我不主动记，避免以后不小心说出来",
    "empty": "内容是空的",
}


def _ttl(key: str, source: str, now: datetime) -> datetime:
    if key == "current_status":
        return now + timedelta(days=STATUS_TTL_DAYS)
    return now + timedelta(days=AGENT_TTL_DAYS if source == "agent" else AUTO_TTL_DAYS)


def _base_query(user_id: int) -> Any:
    return private_memory_query(user_id)


async def list_facts(session: Any, user_id: int, *, now: datetime) -> list[AgentMemory]:
    rows = (await session.execute(_base_query(user_id))).scalars().all()
    return [row for row in rows if row.expires_at is None or row.expires_at >= now]


def fact_view(row: AgentMemory) -> dict[str, Any]:
    key = str(row.memory_key)
    return {
        "key": key,
        "label": KEY_LABELS.get(key, key),
        "content": str(row.content or ""),
        "source": "你让我记的"
        if row.source_kind == "agent"
        else "聊天里了解到的"
        if row.source_kind == "auto"
        else "管理员设置的",
    }


def _clean(key: str, raw: str) -> str:
    if key not in SELF_FACT_KEYS:
        raise ValueError(f"不支持的记忆类别：{key}")
    rejected = reject_reason(raw)
    if rejected is not None:
        raise ValueError(_REJECT_MESSAGES.get(rejected, "这条内容不能作为记忆保存"))
    content = sanitize_fact(raw, max_chars=_CONTENT_LIMIT) or ""
    if key in LIST_KEYS:
        content = normalize_list_fact(content)
    if not content:
        raise ValueError("内容是空的")
    return content


async def _row_for(session: Any, user_id: int, key: str) -> AgentMemory | None:
    return await session.scalar(
        _base_query(user_id).where(AgentMemory.memory_key == key).limit(1)
    )


def _merge_list(old: str, new: str) -> str:
    items = [part for part in old.split("、") if part.strip()]
    for raw in new.split("、"):
        part = raw.strip()
        if part and not any(is_near_duplicate(part, item) for item in items):
            items.append(part)
    return "、".join(items[-8:])


async def remember(  # noqa: PLR0913
    session: Any,
    user_id: int,
    key: str,
    raw_content: str,
    *,
    now: datetime,
    source: str = "agent",
) -> dict[str, Any]:
    """写一条私聊事实；``auto`` 不覆盖 ``agent``/``manual``。不提交。"""

    content = _clean(key, raw_content)
    row = await _row_for(session, user_id, key)
    if row is None:
        session.add(
            AgentMemory(
                scope=PRIVATE_SCOPE,
                group_id=None,
                subject_user_id=user_id,
                memory_type=PRIVATE_MEMORY_TYPE,
                memory_key=key,
                content=content,
                evidence_message_ids=[],
                source_kind=source,
                related_user_ids=[user_id],
                salience=0.7 if source == "agent" else 0.5,
                confidence=0.9 if source == "agent" else 0.6,
                visibility="private",
                expires_at=_ttl(key, source, now),
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        return {"created": True, "key": key, "content": content}
    if row.source_kind == "manual" or (source == "auto" and row.source_kind == "agent"):
        return {"created": False, "key": key, "content": str(row.content), "kept": True}
    old = str(row.content or "")
    merged = (
        _merge_list(old, content)
        if key in LIST_KEYS and source == row.source_kind
        else content
    )
    row.content = merged
    row.source_kind = source
    row.expires_at = _ttl(key, source, now)
    row.updated_at = now
    await session.flush()
    return {"created": False, "key": key, "content": merged, "changed": merged != old}


async def forget(
    session: Any, user_id: int, *, key: str | None = None, keyword: str | None = None
) -> dict[str, Any]:
    """删私聊事实；可按类别、关键词收窄，列表类按关键词只删那一项。不提交。"""

    if key and key not in SELF_FACT_KEYS:
        raise ValueError(f"不支持的记忆类别：{key}")
    needle = str(keyword or "").strip()
    rows = (await session.execute(_base_query(user_id))).scalars().all()
    removed: list[str] = []
    trimmed: list[str] = []
    protected: list[str] = []
    for row in rows:
        row_key = str(row.memory_key)
        content = str(row.content or "")
        if key and row_key != key:
            continue
        if needle and needle not in content and not is_near_duplicate(needle, content):
            continue
        if row.source_kind == "manual":
            protected.append(row_key)
            continue
        if needle and row_key in LIST_KEYS:
            parts = [part for part in content.split("、") if part.strip()]
            kept = [
                p for p in parts if needle not in p and not is_near_duplicate(needle, p)
            ]
            if kept and len(kept) < len(parts):
                row.content = "、".join(kept)
                trimmed.append(row_key)
                continue
        await session.delete(row)
        removed.append(row_key)
    await session.flush()
    return {
        "removed": sorted(removed),
        "trimmed": sorted(trimmed),
        "protected": sorted(protected),
        "changed": bool(removed or trimmed),
    }


async def purge_user(session: Any, user_id: int) -> int:
    """删掉某人全部私聊记忆（WebUI / 删除对话时用）。不提交。"""

    return await delete_private_memories(session, user_id)


# ── 自动整理 ────────────────────────────────────────────────


async def user_message_count(session: Any, user_id: int) -> int:
    """该用户私聊里未删除的 user 消息总数，用来决定什么时候整理。"""

    return int(
        await session.scalar(
            select(func.count())
            .select_from(ChatMessage)
            .join(ChatSession, ChatSession.id == ChatMessage.session_id)
            .where(
                ChatSession.user_id == user_id,
                ChatSession.group_id.is_(None),
                ChatMessage.role == "user",
                ChatMessage.is_deleted.is_(False),
            )
        )
        or 0
    )


def should_consolidate(count: int) -> bool:
    return count > 0 and count % CONSOLIDATE_EVERY == 0


async def _recent_user_lines(session: Any, user_id: int) -> list[str]:
    rows = (
        await session.execute(
            select(ChatMessage.role, ChatMessage.content)
            .join(ChatSession, ChatSession.id == ChatMessage.session_id)
            .where(
                ChatSession.user_id == user_id,
                ChatSession.group_id.is_(None),
                ChatMessage.is_deleted.is_(False),
            )
            .order_by(ChatMessage.id.desc())
            .limit(CONSOLIDATE_WINDOW)
        )
    ).all()
    lines = []
    for role, content in reversed(rows):
        speaker = "user" if role == "user" else "bot"
        lines.append(f"{speaker}: {str(content or '')[:300]}")
    return lines


def parse_facts(raw: str | None) -> list[tuple[str, str]]:
    """解析整理模型的 JSON；格式不对返回空。"""

    if not raw:
        return []
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    facts = payload.get("facts") if isinstance(payload, dict) else None
    result: list[tuple[str, str]] = []
    for item in facts if isinstance(facts, list) else []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        content = str(item.get("content") or "").strip()
        if key in SELF_FACT_KEYS and content:
            result.append((key, content[:_CONTENT_LIMIT]))
    return result[:MAX_FACTS]


async def consolidate(session: Any, user_id: int, *, now: datetime) -> int:
    """用记忆模型从最近私聊里整理事实，写成 ``auto``；返回写入条数。会提交。"""

    from .. import llm

    lines = await _recent_user_lines(session, user_id)
    if not any(line.startswith("user:") for line in lines):
        return 0
    from .prompt_sections import use_private_user

    with use_private_user(user_id):
        messages = [
            *system_messages("private.memory_system", PRIVATE_MEMORY_SYSTEM),
            {"role": "user", "content": "\n".join(lines)},
        ]
    raw = await llm.complete(
        messages,  # pyright: ignore[reportArgumentType]
        task="agent_memory",
        max_tokens=llm.NO_OUTPUT_LIMIT,
        timeout=40.0,
        response_format={"type": "json_object"},
    )
    written = 0
    for key, content in parse_facts(raw):
        try:
            result = await remember(
                session, user_id, key, content, now=now, source="auto"
            )
        except ValueError:
            continue
        if result.get("created") or result.get("changed"):
            written += 1
    await session.commit()
    dbg(f"私聊记忆整理: 用户 {user_id} 写入 {written} 条")
    return written


async def consolidate_in_background(user_id: int, now: datetime) -> None:
    """独立会话里跑整理；任何失败只记日志。"""

    from nonebot_plugin_orm import get_session

    try:
        async with get_session() as session:
            await consolidate(session, user_id, now=now)
    except Exception:  # noqa: BLE001
        dbg_exc(f"私聊记忆整理失败: 用户 {user_id}")


__all__ = [
    "CONSOLIDATE_EVERY",
    "KEY_LABELS",
    "PRIVATE_MEMORY_TYPE",
    "PRIVATE_SCOPE",
    "consolidate",
    "consolidate_in_background",
    "fact_view",
    "forget",
    "list_facts",
    "parse_facts",
    "purge_user",
    "remember",
    "should_consolidate",
    "user_message_count",
]
