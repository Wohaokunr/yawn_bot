# ruff: noqa: TID252, TRY003
"""成员自助记忆：让成员亲口要求 Agent 记住或忘掉关于自己的事实。

只允许调用者写/删**自己的**画像：别人不能替某人"记住"他的爱好，也不能
让 Agent 忘掉别人的事实。管理员手工维护（manual）的条目不受影响。
自助写入的条目 ``source_kind="agent"``：重建不会清除，自动整理不会覆盖。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from ..data_models.agent_memory import AgentMemory, AgentPrivacy
from .memory import (
    PROFILE_TTL_DAYS,
    SHORT_TERM_FACT_TTL_DAYS,
    merge_list_profile_update,
    normalize_fact_key,
)
from .memory_hygiene import (
    is_near_duplicate,
    normalize_list_fact,
    reject_reason,
    sanitize_fact,
)

SELF_FACT_KEYS: tuple[str, ...] = (
    "display_name",
    "preferred_address",
    "hobby",
    "preference",
    "dislike",
    "skill",
    "current_status",
)
_SELF_LIST_KEYS = frozenset({"hobby", "preference", "dislike", "skill"})
_FORGET_LIST_KEYS = _SELF_LIST_KEYS | {"recurring_topic"}
# 称呼类自述是最强证据，直接作为核心记忆（不过期、不衰减）。
_SELF_CORE_KEYS = frozenset({"display_name", "preferred_address"})
# 亲口要求记住的普通事实保留期是自动画像的两倍。
SELF_FACT_TTL_DAYS = PROFILE_TTL_DAYS * 2
SELF_FACT_CONFIDENCE = 0.9
_REJECT_MESSAGES = {
    "empty": "内容不能为空",
    "instruction": "这句话像是在给机器人下指令，不能作为记忆保存",
    "sensitive": "健康、宗教、政治立场、性取向、收入或住址等敏感信息不会被记住",
    "pii_only": "联系方式、证件号或链接不会被记住",
}


async def _ensure_not_opted_out(session: Any, group_id: int, user_id: int) -> None:
    privacy = await session.get(AgentPrivacy, (group_id, user_id))
    if privacy is not None and privacy.opted_out:
        raise PermissionError("该成员已退出 Agent 记忆，不能写入")


async def _subject_rows(
    session: Any, group_id: int, user_id: int
) -> dict[str, AgentMemory]:
    rows = (
        (
            await session.execute(
                select(AgentMemory).where(
                    AgentMemory.group_id == group_id,
                    AgentMemory.subject_user_id == user_id,
                    AgentMemory.memory_type.in_(("core", "profile")),
                )
            )
        )
        .scalars()
        .all()
    )
    return {str(row.memory_key): row for row in rows}


async def remember_member_fact(  # noqa: PLR0913
    session: Any,
    group_id: int,
    user_id: int,
    raw_key: str,
    raw_content: str,
    *,
    now: datetime,
) -> dict[str, Any]:
    key = normalize_fact_key(raw_key)
    if key not in SELF_FACT_KEYS:
        raise ValueError(f"不支持的记忆类别：{raw_key}")
    rejected = reject_reason(raw_content)
    if rejected is not None:
        raise ValueError(_REJECT_MESSAGES.get(rejected, "该内容不能作为记忆保存"))
    content = sanitize_fact(raw_content, max_chars=100) or ""
    if key in _SELF_LIST_KEYS:
        content = normalize_list_fact(content)
    if not content:
        raise ValueError("内容不能为空")
    await _ensure_not_opted_out(session, group_id, user_id)
    existing = (await _subject_rows(session, group_id, user_id)).get(key)
    if existing is not None and existing.source_kind == "manual":
        raise PermissionError("这条资料由管理员维护，不能通过对话修改")
    ttl_days = SHORT_TERM_FACT_TTL_DAYS.get(key, SELF_FACT_TTL_DAYS)
    memory_type = "core" if key in _SELF_CORE_KEYS else "profile"
    expires_at = None if memory_type == "core" else now + timedelta(days=ttl_days)
    if existing is None:
        session.add(
            AgentMemory(
                group_id=group_id,
                subject_user_id=user_id,
                memory_type=memory_type,
                memory_key=key,
                content=content,
                evidence_message_ids=[],
                source_kind="agent",
                related_user_ids=[user_id],
                salience=0.7,
                confidence=SELF_FACT_CONFIDENCE,
                visibility="group",
                expires_at=expires_at,
            )
        )
        return {"ok": True, "created": True, "key": key, "content": content}
    old_content = str(existing.content or "")
    if key in _SELF_LIST_KEYS and existing.source_kind == "agent":
        merged, _confidence = merge_list_profile_update(
            old_content, SELF_FACT_CONFIDENCE, content, SELF_FACT_CONFIDENCE
        )
    else:
        # 自动提取的旧值可能有误；本人亲口说的版本整体替换它。
        merged = content
    existing.content = merged
    existing.source_kind = "agent"
    existing.memory_type = memory_type
    existing.confidence = max(float(existing.confidence or 0.0), SELF_FACT_CONFIDENCE)
    existing.related_user_ids = [user_id]
    existing.expires_at = expires_at
    return {
        "ok": True,
        "created": False,
        "key": key,
        "content": merged,
        "changed": merged != old_content,
    }


async def forget_member_facts(
    session: Any,
    group_id: int,
    user_id: int,
    *,
    raw_key: str | None = None,
    keyword: str | None = None,
) -> dict[str, Any]:
    """删除调用者自己的画像；可按类别和/或关键词收窄。

    列表类事实带关键词时只移除匹配的那一项（"别记我喜欢香菜了"不应连
    "喜欢火锅"一起删掉）。管理员手工条目跳过并在结果里说明。
    """

    key = normalize_fact_key(raw_key) if raw_key else ""
    if raw_key and not key:
        raise ValueError(f"不支持的记忆类别：{raw_key}")
    needle = str(keyword or "").strip()
    rows = await _subject_rows(session, group_id, user_id)
    removed: list[str] = []
    trimmed: list[str] = []
    protected: list[str] = []
    for row_key, row in rows.items():
        if key and row_key != key:
            continue
        content = str(row.content or "")
        if needle and needle not in content and not is_near_duplicate(needle, content):
            continue
        if row.source_kind == "manual":
            protected.append(row_key)
            continue
        if needle and row_key in _FORGET_LIST_KEYS:
            values = [part for part in content.split("、") if part.strip()]
            kept = [
                part
                for part in values
                if needle not in part and not is_near_duplicate(needle, part)
            ]
            if kept and len(kept) < len(values):
                row.content = "、".join(kept)
                trimmed.append(row_key)
                continue
        await session.delete(row)
        removed.append(row_key)
    return {
        "ok": True,
        "removed": sorted(removed),
        "trimmed": sorted(trimmed),
        "protected": sorted(protected),
        "changed": bool(removed or trimmed),
    }


__all__ = [
    "SELF_FACT_KEYS",
    "forget_member_facts",
    "remember_member_fact",
]
