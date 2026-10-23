# ruff: noqa: TID252, RUF001, TC003, PLR2004
"""工具侧记忆检索：多词召回、相关性重排、去冗余与证据媒体回填。

旧实现只用整句 ``LIKE %query%``，"上次爬山去的哪"这类自然问句几乎永远
命不中"周末去爬了香山"。这里先把查询拆成若干检索词做宽召回，再交给
``rank_memories`` 按 IDF 相关性、显著度衰减与召回强化重排，最后 MMR
去掉同一件事的重复表述。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select

from ..data_models.agent_memory import AgentMemory, AgentPrivacy
from ..data_models.group_agent_message import GroupAgentMessage
from .memory import rank_memories
from .memory_access import record_access

# 宽召回候选上限：足够覆盖一个群的活跃记忆，又不至于把全表拉进内存。
SEARCH_CANDIDATE_LIMIT = 80
MAX_QUERY_TERMS = 8
# 结果最低相关性：低于它说明只是碰巧共享一个常见字对，不如不返回。
MIN_SEARCH_RELEVANCE = 0.08
_TERM_SPLIT_RE = re.compile(r"[\s,，。.!！?？;；:：、/|()（）\[\]【】\"'“”‘’]+")
# 问句里的功能词不参与召回，否则"什么""那个"会命中大量无关记忆。
_STOP_TERMS = frozenset(
    {
        "什么",
        "那个",
        "这个",
        "就是",
        "还有",
        "上次",
        "之前",
        "以前",
        "记得",
        "是不",
        "不是",
        "怎么",
        "哪里",
        "哪个",
        "有没",
        "没有",
        "我们",
        "你们",
        "他们",
        "大家",
        "一下",
        "一起",
        "还记",
        "记不",
    }
)
_CJK_RE = re.compile(r"[一-鿿]+")


def query_terms(query: str, *, limit: int = MAX_QUERY_TERMS) -> list[str]:
    """把自然语言查询拆成检索词：英文/数字词、短中文词与长中文串的二元组。"""

    terms: list[str] = []
    for chunk in _TERM_SPLIT_RE.split(str(query or "").strip()):
        if not chunk:
            continue
        ascii_words = re.findall(r"[A-Za-z0-9]{2,}", chunk)
        terms.extend(word.lower() for word in ascii_words)
        for run in _CJK_RE.findall(chunk):
            if len(run) <= 3:
                terms.append(run)
            else:
                terms.extend(run[index : index + 2] for index in range(len(run) - 1))
    unique = [
        term
        for term in dict.fromkeys(terms)
        if len(term) >= 2 and term not in _STOP_TERMS
    ]
    # 长词更具区分度，优先保留。
    unique.sort(key=len, reverse=True)
    return unique[:limit]


async def opted_out_user_ids(session: Any, group_id: int) -> set[int]:
    return set(
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


def _visible_clauses(group_id: int, now: datetime) -> list[Any]:
    return [
        AgentMemory.group_id == group_id,
        AgentMemory.visibility.in_(("group", "public")),
        (AgentMemory.expires_at.is_(None) | (AgentMemory.expires_at >= now)),
    ]


async def search_group_memories(
    session: Any,
    group_id: int,
    query: str,
    *,
    limit: int,
    now: datetime,
) -> list[AgentMemory]:
    """返回与查询最相关的可见记忆（已过滤隐私退出成员）。"""

    cleaned = str(query or "").strip()
    if not cleaned:
        return []
    terms = query_terms(cleaned)
    matchers = [AgentMemory.content.contains(cleaned, autoescape=True)]
    for term in terms:
        matchers.append(AgentMemory.content.contains(term, autoescape=True))
        matchers.append(AgentMemory.memory_key.contains(term, autoescape=True))
    rows = list(
        (
            await session.execute(
                select(AgentMemory)
                .where(*_visible_clauses(group_id, now), or_(*matchers))
                .order_by(AgentMemory.updated_at.desc(), AgentMemory.id.desc())
                .limit(SEARCH_CANDIDATE_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    opted_out = await opted_out_user_ids(session, group_id)
    rows = [
        row
        for row in rows
        if int(row.subject_user_id or 0) not in opted_out
        and not opted_out.intersection(set(row.related_user_ids or []))
    ]
    exact = {int(row.id or 0) for row in rows if cleaned in str(row.content or "")}
    ranked = rank_memories(
        rows,
        [cleaned, *terms],
        None,
        now,
        limit=limit,
        diversify=True,
        min_relevance=MIN_SEARCH_RELEVANCE,
    )
    if not ranked and exact:
        # 整句精确命中但相关性被 IDF 稀释（候选很少时）仍应返回。
        ranked = [row for row in rows if int(row.id or 0) in exact][:limit]
    record_access((int(row.id or 0) for row in ranked), now=now)
    return ranked


async def evidence_images(
    session: Any, group_id: int, rows: list[AgentMemory]
) -> dict[int, list[dict[str, Any]]]:
    """按记忆行返回其证据消息中的图片引用。

    整理任务写入的 ``evidence_message_ids`` 是 ``GroupAgentMessage.id``
    （库内行 id），不是 OneBot ``message_id``。旧实现按 ``message_id`` 反查，
    两个编号空间偶然重合时会把别的消息的图片挂到记忆上。
    """

    wanted: dict[int, list[int]] = {}
    for row in rows:
        ids = [
            int(value)
            for value in list(row.evidence_message_ids or [])
            if str(value).isdigit()
        ]
        if ids:
            wanted[int(row.id or 0)] = ids
    all_ids = sorted({value for ids in wanted.values() for value in ids})
    if not all_ids:
        return {}
    messages = (
        (
            await session.execute(
                select(GroupAgentMessage).where(
                    GroupAgentMessage.group_id == group_id,
                    GroupAgentMessage.id.in_(all_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    refs_by_row_id = {
        int(message.id): [
            {
                **dict(ref),
                "source": "tool",
                "source_message_id": int(message.message_id),
            }
            for ref in list(message.media_refs or [])
            if isinstance(ref, dict) and str(ref.get("type") or "") == "image"
        ]
        for message in messages
    }
    return {
        memory_id: [ref for value in ids for ref in refs_by_row_id.get(value, [])]
        for memory_id, ids in wanted.items()
    }


__all__ = [
    "MIN_SEARCH_RELEVANCE",
    "evidence_images",
    "opted_out_user_ids",
    "query_terms",
    "search_group_memories",
]
