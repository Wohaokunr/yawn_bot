# ruff: noqa: DTZ001,PLR0913,PLR2004
"""yawn_agent 记忆系统纯函数单测：相关性重排、增量合并与画像冲突。"""

from __future__ import annotations

import importlib
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import nonebot

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if nonebot.get_plugin("yawn_core") is None:
    nonebot.load_from_toml("pyproject.toml")

memory = importlib.import_module("src.plugins.yawn_core.yawn_agent.memory")
models = importlib.import_module("src.plugins.yawn_core.data_models.agent_memory")

NOW = datetime(2026, 8, 22, 12, 0, 0)


def _memory(
    *,
    memory_id: int = 1,
    memory_type: str = "profile",
    key: str = "display_name",
    content: str = "阿眠",
    salience: float = 0.5,
    confidence: float = 0.5,
    subject_user_id: int | None = None,
    updated_days_ago: float = 0.0,
) -> Any:
    row = models.AgentMemory(
        id=memory_id,
        scope="group",
        group_id=100,
        subject_user_id=subject_user_id,
        memory_type=memory_type,
        memory_key=key,
        content=content,
        evidence_message_ids=[],
        salience=salience,
        confidence=confidence,
        visibility="group",
    )
    row.updated_at = NOW - timedelta(days=updated_days_ago)
    return row


def _relation(relation_id: int, subject: int, obj: int, confidence: float = 0.5) -> Any:
    return models.AgentRelation(
        id=relation_id,
        group_id=100,
        subject_user_id=subject,
        object_user_id=obj,
        relation_type="mentions",
        confidence=confidence,
        evidence_count=1,
        last_seen_at=NOW,
    )


def test_extract_bigrams_covers_ascii_words_and_cjk_pairs() -> None:
    tokens = memory.extract_bigrams("AI Agent 记忆系统 test")

    assert {"agent", "test", "记忆", "忆系", "系统"} <= tokens
    # 单字符 ASCII 词不参与匹配，避免 a/i 这类高噪声 token。
    assert "ai" in tokens
    assert "a" not in tokens


def test_rank_memories_prefers_topic_relevant_over_stale_hotspot() -> None:
    hotspot = _memory(
        memory_id=1,
        content="晚饭吃了火锅配奶茶",
        salience=1.0,
        confidence=0.5,
        updated_days_ago=30,
    )
    relevant = _memory(
        memory_id=2,
        key="display_name",
        content="阿眠",
        salience=0.3,
        confidence=0.5,
    )

    ranked = memory.rank_memories(
        [hotspot, relevant], ["阿眠今天说了什么", "阿眠喜欢爬山"], None, NOW
    )

    assert ranked[0] is relevant
    assert ranked[1] is hotspot


def test_rank_memories_applies_time_decay_and_speaker_bonus() -> None:
    fresh = _memory(memory_id=1, salience=0.5, updated_days_ago=0.0)
    stale = _memory(memory_id=2, salience=0.5, updated_days_ago=60.0)

    ranked = memory.rank_memories([stale, fresh], [], None, NOW)
    assert ranked[0] is fresh

    # 相同条件下，当前发言人的画像获得加权。
    plain = _memory(memory_id=1, subject_user_id=111)
    speaker_row = _memory(memory_id=2, subject_user_id=222)
    ranked = memory.rank_memories([plain, speaker_row], [], 222, NOW)
    assert ranked[0] is speaker_row


def test_rank_memories_respects_limit_and_newest_tie_break() -> None:
    rows = [
        _memory(memory_id=index, salience=0.4, updated_days_ago=0.0)
        for index in range(1, 6)
    ]

    ranked = memory.rank_memories(rows, [], None, NOW, limit=3)

    assert len(ranked) == 3
    # 同分时新记录（更大 id）在前。
    assert [row.id for row in ranked] == [5, 4, 3]


def test_rank_relations_prioritizes_recent_participants() -> None:
    e1 = _relation(1, 1, 2)
    e2 = _relation(2, 3, 4)
    e3 = _relation(3, 2, 5)

    picked = memory.rank_relations([e1, e2, e3], {1, 2}, limit=2)

    assert picked == [e1, e3]

    # 相关边不足 min_related 时回退补足，保持总数上限。
    everyone = memory.rank_relations([e1, e2, e3], {9}, limit=50, min_related=2)
    assert everyone == [e1, e2, e3]


def test_rank_relations_fills_with_unrelated_when_below_minimum() -> None:
    related = _relation(1, 1, 2)
    others = [_relation(index, 3, 4) for index in range(2, 7)]

    picked = memory.rank_relations([related, *others], {1}, limit=50, min_related=3)

    assert picked[0] is related
    assert picked == [related, *others]


def test_merge_daily_summary_appends_and_keeps_tail_budget() -> None:
    assert memory.merge_daily_summary("", "早上的摘要") == "早上的摘要"
    assert memory.merge_daily_summary("已有的摘要", "已有的摘要") == "已有的摘要"
    assert (
        memory.merge_daily_summary("上午的摘要", "下午的摘要")
        == "上午的摘要\n下午的摘要"
    )

    merged = memory.merge_daily_summary("x" * 1900, "y" * 200)
    # 预算 2000 字符，超出时保留尾部（最新批次完整），不覆盖当天早些时候的内容。
    assert len(merged) == 2000
    assert merged.endswith("y" * 200)
    assert merged.startswith("x" * 1799)


def test_merge_profile_update_requires_confidence_to_overwrite() -> None:
    # 相同内容：合并置信度取 max。
    assert memory.merge_profile_update("阿眠", 0.6, "阿眠", 0.8) == ("阿眠", 0.8)
    # 新内容置信度不低于旧值：允许覆盖。
    assert memory.merge_profile_update("阿眠", 0.6, "小眠", 0.7) == ("小眠", 0.7)
    # 新内容置信度更低：保留旧事实，不被低置信提取冲掉。
    assert memory.merge_profile_update("阿眠", 0.9, "小眠", 0.5) == ("阿眠", 0.9)
