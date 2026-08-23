# ruff: noqa: ANN001,ANN003,DTZ001,PLR0913,PLR2004,PLW0108,TRY003
"""yawn_agent 记忆系统纯函数单测：相关性重排、增量合并与画像冲突。"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import nonebot
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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
dialogue = importlib.import_module("src.plugins.yawn_core.yawn_agent.dialogue")
models = importlib.import_module("src.plugins.yawn_core.data_models.agent_memory")
message_models = importlib.import_module(
    "src.plugins.yawn_core.data_models.group_agent_message"
)
config_models = importlib.import_module(
    "src.plugins.yawn_core.data_models.group_agent_config"
)
user_group_models = importlib.import_module(
    "src.plugins.yawn_core.data_models.user_group"
)
audit_models = importlib.import_module(
    "src.plugins.yawn_core.data_models.agent_audit"
)
bot_group_models = importlib.import_module(
    "src.plugins.yawn_core.data_models.bot_group"
)

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


def _relation(
    relation_id: int,
    subject: int,
    obj: int,
    confidence: float = 0.5,
    *,
    relation_type: str = "mentions",
    source_kind: str = "auto",
    note: str = "",
) -> Any:
    return models.AgentRelation(
        id=relation_id,
        group_id=100,
        subject_user_id=subject,
        object_user_id=obj,
        relation_type=relation_type,
        source_kind=source_kind,
        note=note,
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


def test_normalize_relation_type_maps_aliases_and_keeps_custom() -> None:
    # 常见同义词归并到枚举，避免等价关系拆成多条边互相挤占。
    assert memory.normalize_relation_type("朋友") == "好友"
    assert memory.normalize_relation_type(" 仇人 ") == "对立"
    assert memory.normalize_relation_type("CP") == "情侣"
    assert memory.normalize_relation_type("常提及") == memory.RELATION_MENTION_TYPE
    # 枚举与 @提及保留类型原样返回。
    assert memory.normalize_relation_type("师徒") == "师徒"
    assert memory.normalize_relation_type("mentions") == "mentions"
    # 枚举外的自定义词保留但截断到列宽。
    assert memory.normalize_relation_type("网友面基认识的老乡") == "网友面基认识的老乡"
    assert memory.normalize_relation_type("") == ""
    assert memory.normalize_relation_type(None) == ""


def test_effective_relation_confidence_recency_weights() -> None:
    # 同等置信度下，近期仍在互动的边权重高于沉寂数月的老边。
    fresh = memory.effective_relation_confidence(0.8, NOW, NOW)
    assert fresh == 0.8
    assert memory.effective_relation_confidence(
        0.8, NOW - timedelta(days=20), NOW
    ) == pytest.approx(0.72)
    assert memory.effective_relation_confidence(
        0.8, NOW - timedelta(days=60), NOW
    ) == pytest.approx(0.56)
    assert memory.effective_relation_confidence(
        0.8, NOW - timedelta(days=150), NOW
    ) == pytest.approx(0.4)
    # 超过 180 天按最低权重兜底；缺失 last_seen 时视作刚见到。
    assert memory.effective_relation_confidence(
        0.8, NOW - timedelta(days=400), NOW
    ) == pytest.approx(0.4)
    assert memory.effective_relation_confidence(0.8, None, NOW) == 0.8


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


class _FakeSession:
    """只收集 add() 的 session 替身；被测函数不触库即可纯逻辑验证。"""

    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, row: Any) -> None:
        self.added.append(row)


def _agent_message(message_id: int, user_id: int, text: str) -> Any:
    return message_models.GroupAgentMessage(
        id=message_id,
        group_id=100,
        user_id=user_id,
        normalized_text=text,
    )


def test_match_display_name_strong_patterns_keep_high_confidence() -> None:
    assert memory._match_display_name("大家好，我叫阿眠") == ("阿眠", 0.9)
    assert memory._match_display_name("以后称我为老王") == ("老王", 0.9)
    assert memory._match_display_name("请叫我小眠") == ("小眠", 0.9)
    assert memory._match_display_name("我叫Yawn-ya") == ("Yawn-ya", 0.9)


def test_match_display_name_rejects_ambiguous_predicates() -> None:
    assert memory._match_display_name("我是阿眠") is None
    assert memory._match_display_name("我是真的服了") is None
    assert memory._match_display_name("我是不太想上班") is None
    # 超长短语按弱模式长度上限丢弃。
    assert memory._match_display_name("我是abcdefghijkLMN") is None
    # 与自称无关的文本不匹配。
    assert memory._match_display_name("今天天气不错") is None


def test_extract_structured_memories_weak_match_cannot_overwrite_strong() -> None:
    session = _FakeSession()
    strong = _memory(memory_id=9, subject_user_id=1, content="阿眠", confidence=0.85)
    profiles = {(1, "display_name"): strong}
    rows = [
        _agent_message(101, 1, "我是真的服了"),
        _agent_message(102, 1, "我是小白"),
    ]

    memory._extract_structured_memories(session, 100, rows, NOW, profiles, {})

    # 垃圾谓语被过滤、弱自称置信度不足，都无法覆盖强记录。
    assert strong.content == "阿眠"
    assert strong.confidence == 0.85
    assert session.added == []


def test_extract_structured_memories_strong_overwrites_weak() -> None:
    session = _FakeSession()
    weak = _memory(memory_id=9, subject_user_id=1, content="小白", confidence=0.6)
    profiles = {(1, "display_name"): weak}
    rows = [_agent_message(101, 1, "大家好我叫阿眠")]

    memory._extract_structured_memories(session, 100, rows, NOW, profiles, {})

    assert weak.content == "阿眠"
    assert weak.confidence == 0.9
    assert session.added == []


def test_extract_structured_memories_rejects_ambiguous_new_user_claim() -> None:
    session = _FakeSession()
    rows = [_agent_message(101, 2, "我是阿眠")]

    memory._extract_structured_memories(session, 100, rows, NOW, {}, {})

    assert session.added == []


def test_extract_structured_memories_marks_mention_source_kind() -> None:
    session = _FakeSession()
    rows = [_agent_message(101, 111, "@123456 中午一起吃饭")]

    memory._extract_structured_memories(
        session, 100, rows, NOW, {}, {}, valid_member_ids={111, 123456}
    )

    assert len(session.added) == 1
    edge = session.added[0]
    assert edge.relation_type == memory.RELATION_MENTION_TYPE
    assert edge.source_kind == "mention"


def test_store_model_facts_rejects_users_outside_batch() -> None:
    session = _FakeSession()
    facts = [
        {
            "user_id": 111,
            "key": "hobby",
            "content": "爬山",
            "evidence_message_ids": [10],
        },
        # 幻觉 QQ 号与省略 user_id 的无主事实都不落库。
        {
            "user_id": 999,
            "key": "hobby",
            "content": "幻觉用户",
            "evidence_message_ids": [10],
        },
        {
            "user_id": 0,
            "key": "hobby",
            "content": "省略主体",
            "evidence_message_ids": [10],
        },
        # 真实发言者但证据缺失，同样拒绝。
        {
            "user_id": 111,
            "key": "hobby",
            "content": "无证据",
            "evidence_message_ids": [],
        },
    ]

    memory._store_model_facts(
        session, 100, facts, {10}, {111}, {10: 111}, NOW, {}
    )

    assert len(session.added) == 1
    assert session.added[0].subject_user_id == 111
    assert session.added[0].content == "爬山"


def test_store_model_facts_requires_evidence_from_same_member() -> None:
    session = _FakeSession()
    facts = [
        {
            "user_id": 111,
            "key": "hobby",
            "content": "模型把别人的话算给了我",
            "evidence_message_ids": [10],
        }
    ]

    memory._store_model_facts(
        session, 100, facts, {10}, {111, 222}, {10: 222}, NOW, {}
    )

    assert session.added == []


def test_store_model_relations_rejects_users_outside_batch() -> None:
    session = _FakeSession()
    relations = [
        {
            "subject_user_id": 111,
            "object_user_id": 222,
            "type": "mentions",
            "evidence_message_ids": [10],
        },
        # 关系一端是幻觉用户：整条边拒绝。
        {
            "subject_user_id": 111,
            "object_user_id": 999,
            "type": "mentions",
            "evidence_message_ids": [10],
        },
    ]

    memory._store_model_relations(
        session,
        100,
        relations,
        {10},
        {111},
        {111, 222},
        {10: 111},
        NOW,
        {},
    )

    assert len(session.added) == 1
    assert session.added[0].subject_user_id == 111
    assert session.added[0].object_user_id == 222


def test_store_model_relations_normalizes_type_and_stores_note() -> None:
    session = _FakeSession()
    relations = [
        {
            "subject_user_id": 111,
            "object_user_id": 222,
            "type": "朋友",
            "note": "常一起开黑打游戏",
            "evidence_message_ids": [10],
        }
    ]

    memory._store_model_relations(
        session, 100, relations, {10}, {111}, {111, 222}, {10: 111}, NOW, {}
    )

    assert len(session.added) == 1
    edge = session.added[0]
    assert edge.relation_type == "好友"
    assert edge.source_kind == "auto"
    assert edge.note == "常一起开黑打游戏"


def test_store_model_relations_fills_note_only_when_empty() -> None:
    session = _FakeSession()
    existing = _relation(
        1, 111, 222, 0.6, relation_type="好友", note="已有备注"
    )
    relations = [
        {
            "subject_user_id": 111,
            "object_user_id": 222,
            "type": "好友",
            "note": "整理任务的新备注",
            "evidence_message_ids": [10],
        }
    ]

    memory._store_model_relations(
        session,
        100,
        relations,
        {10},
        {111},
        {111, 222},
        {10: 111},
        NOW,
        {(111, 222, "好友"): existing},
    )

    assert session.added == []
    assert existing.note == "已有备注"
    assert existing.confidence == pytest.approx(0.61)
    assert existing.evidence_count == 2


def test_store_model_relations_preserves_manual_and_agent_edges() -> None:
    session = _FakeSession()
    manual = _relation(
        1, 111, 222, 0.9, relation_type="情侣", source_kind="manual", note="管理员录入"
    )
    agent = _relation(2, 222, 333, 0.6, relation_type="好友", source_kind="agent")
    relations = [
        {
            "subject_user_id": 111,
            "object_user_id": 222,
            "type": "情侣",
            "note": "LLM 想覆盖备注",
            "evidence_message_ids": [10],
        },
        {
            "subject_user_id": 222,
            "object_user_id": 333,
            "type": "朋友",
            "note": "",
            "evidence_message_ids": [11],
        },
    ]

    memory._store_model_relations(
        session,
        100,
        relations,
        {10, 11},
        {111, 222},
        {111, 222, 333},
        {10: 111, 11: 222},
        NOW,
        {(111, 222, "情侣"): manual, (222, 333, "好友"): agent},
    )

    # 管理员与 Agent 的显式结论优先：只续期，不改类型/备注/置信度/证据。
    assert session.added == []
    assert manual.note == "管理员录入"
    assert manual.confidence == 0.9
    assert manual.evidence_count == 1
    assert agent.confidence == 0.6
    assert agent.evidence_count == 1
    assert manual.last_seen_at == NOW
    assert agent.last_seen_at == NOW


def test_batch_rows_never_crosses_date_or_message_limit() -> None:
    rows = [
        _agent_message(index, 1, f"消息 {index}") for index in range(1, 46)
    ]
    for index, row in enumerate(rows):
        row.received_at = NOW + timedelta(days=1 if index >= 42 else 0)
    picked = memory._batch_rows(rows)
    assert [row.id for row in picked] == list(range(1, 41))


def test_public_summary_rejects_member_identity_and_quotes() -> None:
    row = _agent_message(1, 123456, "普通消息")
    row.sender_name = "阿眠"
    assert memory._public_summary_safe("最近在讨论测试策略", [row])
    assert not memory._public_summary_safe("阿眠最近在讨论测试策略", [row])
    assert not memory._public_summary_safe("用户 123456 分享了经验", [row])
    assert not memory._public_summary_safe("最近围绕群号 987654321 展开讨论", [row])
    assert not memory._public_summary_safe("大家说“可以这样做”", [row])


@pytest.mark.asyncio
async def test_model_summary_uses_resolved_memory_model_fallback(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_complete(_messages, **kwargs):
        captured.update(kwargs)
        return '{"summary":"摘要","public_summary":"","facts":[],"relations":[]}'

    monkeypatch.setattr(memory, "get_client", lambda: object())
    monkeypatch.setattr(memory, "get_agent_model", lambda _role: "fallback-ai-model")
    monkeypatch.setattr(memory, "complete", fake_complete)
    result = await memory._model_summary(
        [{"id": 1, "user_id": 1, "name": "甲", "role": "member", "text": "内容"}]
    )
    assert result is not None and result["summary"] == "摘要"
    assert captured["model"] == "fallback-ai-model"


@pytest.mark.asyncio
async def test_model_summary_rejects_invalid_top_level_summary(monkeypatch) -> None:
    async def fake_complete(_messages, **_kwargs):
        return '{"summary":123,"public_summary":"","facts":[],"relations":[]}'

    monkeypatch.setattr(memory, "get_client", lambda: object())
    monkeypatch.setattr(memory, "complete", fake_complete)
    assert await memory._model_summary([]) is None


@pytest.mark.asyncio
async def test_concurrent_compaction_for_same_group_is_mutually_exclusive(
    monkeypatch,
) -> None:
    active = 0
    max_active = 0

    async def fake_locked(_session, _group_id, *, now):
        nonlocal active, max_active
        _ = now
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return 1

    monkeypatch.setattr(memory, "_compact_group_memory_locked", fake_locked)
    results = await asyncio.gather(
        memory.compact_group_memory(object(), 100, now=NOW),
        memory.compact_group_memory(object(), 100, now=NOW),
    )
    assert results == [1, 1]
    assert max_active == 1


@pytest.mark.asyncio
async def test_context_guarantees_profile_budget_and_cross_group_gates() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        bot_group_models.BotGroup.__table__,
        config_models.GroupAgentConfig.__table__,
        message_models.GroupAgentMessage.__table__,
        models.AgentMemory.__table__,
        models.AgentRelation.__table__,
        models.AgentPrivacy.__table__,
        user_group_models.UserGroup.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: models.AgentMemory.metadata.create_all(
                sync_connection, tables=tables
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add_all(
            [
                bot_group_models.BotGroup(group_id=100, group_name="目标群"),
                bot_group_models.BotGroup(group_id=200, group_name="来源群"),
                config_models.GroupAgentConfig(
                    group_id=100, cross_group_visibility="isolated"
                ),
                config_models.GroupAgentConfig(
                    group_id=200, cross_group_visibility="public_summary"
                ),
                message_models.GroupAgentMessage(
                    bot_id=9,
                    message_id=1,
                    group_id=100,
                    user_id=1,
                    sender_name="当前成员",
                    role="member",
                    normalized_text="继续聊测试策略",
                    received_at=NOW,
                    expires_at=NOW + timedelta(days=7),
                ),
                models.AgentMemory(
                    group_id=100,
                    subject_user_id=1,
                    memory_type="profile",
                    memory_key="display_name",
                    content="speaker-profile-must-arrive",
                    evidence_message_ids=[1],
                    source_kind="auto",
                    related_user_ids=[1],
                    salience=0,
                    confidence=0.9,
                    visibility="group",
                    expires_at=NOW + timedelta(days=30),
                ),
                models.AgentMemory(
                    group_id=200,
                    subject_user_id=0,
                    memory_type="summary",
                    memory_key="public_daily:2026-08-22",
                    content="shared-topic",
                    evidence_message_ids=[],
                    source_kind="auto",
                    related_user_ids=[],
                    salience=0.9,
                    confidence=0.7,
                    visibility="public",
                    expires_at=NOW + timedelta(days=30),
                ),
            ]
        )
        session.add_all(
            [
                models.AgentMemory(
                    group_id=100,
                    subject_user_id=0,
                    memory_type="manual",
                    memory_key=f"bulk-{index}",
                    content=f"高显著候选{index}-" + "x" * 180,
                    evidence_message_ids=[],
                    source_kind="manual",
                    related_user_ids=[],
                    salience=1,
                    confidence=1,
                    visibility="group",
                    expires_at=NOW + timedelta(days=30),
                )
                for index in range(140)
            ]
        )
        await session.commit()
        target = await session.get(config_models.GroupAgentConfig, 100)
        assert target is not None

        isolated = await dialogue._load_context(session, 100, target)
        assert any(
            item["content"] == "speaker-profile-must-arrive"
            for item in isolated["memories"]
        )
        assert len(json.dumps(isolated["memories"], ensure_ascii=False)) <= 6000
        assert not any(
            item["content"] == "shared-topic" for item in isolated["memories"]
        )

        target.cross_group_visibility = "public_summary"
        await session.commit()
        shared = await dialogue._load_context(session, 100, target)
        assert any(item["content"] == "shared-topic" for item in shared["memories"])

        source = await session.get(config_models.GroupAgentConfig, 200)
        assert source is not None
        source.cross_group_visibility = "isolated"
        await session.commit()
        source_closed = await dialogue._load_context(session, 100, target)
        assert not any(
            item["content"] == "shared-topic" for item in source_closed["memories"]
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_compaction_processes_every_contiguous_message(monkeypatch) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        config_models.GroupAgentConfig.__table__,
        message_models.GroupAgentMessage.__table__,
        models.AgentMemory.__table__,
        models.AgentRelation.__table__,
        models.AgentPrivacy.__table__,
        audit_models.AgentAudit.__table__,
        user_group_models.UserGroup.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: models.AgentMemory.metadata.create_all(
                sync_connection, tables=tables
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    seen: list[int] = []

    async def fake_summary(payload, **_kwargs):
        seen.extend(int(item["id"]) for item in payload)
        return {
            "summary": f"已处理到 {payload[-1]['id']}",
            "public_summary": "",
            "facts": [],
            "relations": [],
        }

    monkeypatch.setattr(memory, "_model_summary", fake_summary)
    async with session_factory() as session:
        session.add(config_models.GroupAgentConfig(group_id=100))
        session.add_all(
            [
                message_models.GroupAgentMessage(
                    bot_id=9,
                    message_id=index,
                    group_id=100,
                    user_id=1,
                    sender_name="成员",
                    role="member",
                    normalized_text=f"消息 {index}",
                    received_at=NOW,
                    expires_at=NOW + timedelta(days=7),
                )
                for index in range(1, 86)
            ]
        )
        await session.commit()
        assert await memory.compact_group_memory(session, 100, now=NOW) == 40
        assert await memory.compact_group_memory(session, 100, now=NOW) == 40
        assert await memory.compact_group_memory(session, 100, now=NOW) == 5
        config = await session.get(config_models.GroupAgentConfig, 100)
        assert config is not None
        assert config.last_compacted_message_id == 85
        assert not config.memory_rebuild_required
        assert seen == list(range(1, 86))
        assert (
            await session.scalar(
                select(func.count()).select_from(models.AgentMemory)
            )
        ) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_privacy_rows_advance_cursor_but_never_enter_model(monkeypatch) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        config_models.GroupAgentConfig.__table__,
        message_models.GroupAgentMessage.__table__,
        models.AgentMemory.__table__,
        models.AgentRelation.__table__,
        models.AgentPrivacy.__table__,
        audit_models.AgentAudit.__table__,
        user_group_models.UserGroup.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: models.AgentMemory.metadata.create_all(
                sync_connection, tables=tables
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    seen: list[int] = []

    async def fake_summary(payload, **_kwargs):
        seen.extend(int(item["user_id"]) for item in payload)
        return {
            "summary": "仅含可用成员内容",
            "public_summary": "",
            "facts": [],
            "relations": [],
        }

    monkeypatch.setattr(memory, "_model_summary", fake_summary)
    async with session_factory() as session:
        session.add(config_models.GroupAgentConfig(group_id=100))
        session.add(models.AgentPrivacy(group_id=100, user_id=1, opted_out=True))
        session.add_all(
            [
                message_models.GroupAgentMessage(
                    bot_id=9,
                    message_id=index,
                    group_id=100,
                    user_id=user_id,
                    role="member",
                    normalized_text=f"消息 {index}",
                    received_at=NOW,
                    expires_at=NOW + timedelta(days=7),
                )
                for index, user_id in ((1, 1), (2, 2))
            ]
        )
        await session.commit()
        assert await memory.compact_group_memory(session, 100, now=NOW) == 2
        config = await session.get(config_models.GroupAgentConfig, 100)
        assert config is not None and config.last_compacted_message_id == 2
        assert seen == [2]
    await engine.dispose()


@pytest.mark.asyncio
async def test_invalid_summary_keeps_cursor_and_records_backoff(monkeypatch) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        config_models.GroupAgentConfig.__table__,
        message_models.GroupAgentMessage.__table__,
        models.AgentMemory.__table__,
        models.AgentRelation.__table__,
        models.AgentPrivacy.__table__,
        audit_models.AgentAudit.__table__,
        user_group_models.UserGroup.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: models.AgentMemory.metadata.create_all(
                sync_connection, tables=tables
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def failed_summary(_payload, **_kwargs):
        return None

    monkeypatch.setattr(memory, "_model_summary", failed_summary)
    async with session_factory() as session:
        session.add(config_models.GroupAgentConfig(group_id=100))
        session.add(
            message_models.GroupAgentMessage(
                bot_id=9,
                message_id=1,
                group_id=100,
                user_id=1,
                role="member",
                normalized_text="需要记住",
                received_at=NOW,
                expires_at=NOW + timedelta(days=7),
            )
        )
        await session.commit()
        assert await memory.compact_group_memory(session, 100, now=NOW) == 0
        config = await session.get(config_models.GroupAgentConfig, 100)
        assert config is not None
        assert config.last_compacted_message_id is None
        assert config.memory_consecutive_failures == 1
        assert config.memory_last_error
        assert not memory.memory_retry_due(config, NOW + timedelta(minutes=4))
        assert memory.memory_retry_due(config, NOW + timedelta(minutes=5))

        async def successful_summary(_payload, **_kwargs):
            return {
                "summary": "重试成功",
                "public_summary": "",
                "facts": [
                    {
                        "user_id": 1,
                        "key": "hobby",
                        "content": "测试",
                        "evidence_message_ids": [1],
                        "confidence": 0.7,
                        "salience": 0.6,
                    }
                ],
                "relations": [],
            }

        monkeypatch.setattr(memory, "_model_summary", successful_summary)
        assert await memory.compact_group_memory(session, 100, now=NOW) == 1
        assert await memory.compact_group_memory(session, 100, now=NOW) == 0
        profiles = list(
            (
                await session.execute(
                    select(models.AgentMemory).where(
                        models.AgentMemory.memory_type == "profile"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(profiles) == 1
        assert profiles[0].confidence == 0.7
    await engine.dispose()


@pytest.mark.asyncio
async def test_privacy_delete_works_after_raw_evidence_expired() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        config_models.GroupAgentConfig.__table__,
        message_models.GroupAgentMessage.__table__,
        models.AgentMemory.__table__,
        models.AgentRelation.__table__,
        models.AgentPrivacy.__table__,
        audit_models.AgentAudit.__table__,
        user_group_models.UserGroup.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: models.AgentMemory.metadata.create_all(
                sync_connection, tables=tables
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(config_models.GroupAgentConfig(group_id=100))
        session.add_all(
            [
                models.AgentMemory(
                    group_id=100,
                    subject_user_id=0,
                    memory_type="summary",
                    memory_key="daily:2026-08-22",
                    content="自动摘要",
                    evidence_message_ids=[999],
                    source_kind="auto",
                    related_user_ids=[1, 2],
                    visibility="group",
                ),
                models.AgentMemory(
                    group_id=100,
                    subject_user_id=0,
                    memory_type="manual",
                    memory_key="linked",
                    content="关联成员的手工记忆",
                    evidence_message_ids=[],
                    source_kind="manual",
                    related_user_ids=[1],
                    visibility="group",
                ),
                models.AgentMemory(
                    group_id=100,
                    subject_user_id=0,
                    memory_type="manual",
                    memory_key="keep",
                    content="无关手工记忆",
                    evidence_message_ids=[],
                    source_kind="manual",
                    related_user_ids=[2],
                    visibility="group",
                ),
                models.AgentRelation(
                    group_id=100,
                    subject_user_id=2,
                    object_user_id=1,
                    relation_type="friend",
                    confidence=0.8,
                    evidence_count=1,
                    last_seen_at=NOW,
                ),
            ]
        )
        await session.commit()
        deleted = await memory.delete_member_memories(session, 100, 1)
        assert deleted == 3
        rows = list(
            (
                await session.execute(
                    select(models.AgentMemory).order_by(models.AgentMemory.id)
                )
            )
            .scalars()
            .all()
        )
        assert [row.memory_key for row in rows] == ["keep"]
        config = await session.get(config_models.GroupAgentConfig, 100)
        assert config is not None and config.memory_rebuild_required
        assert config.last_compacted_message_id is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_member_delete_keeps_unrelated_relation_edges() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        config_models.GroupAgentConfig.__table__,
        message_models.GroupAgentMessage.__table__,
        models.AgentMemory.__table__,
        models.AgentRelation.__table__,
        models.AgentPrivacy.__table__,
        audit_models.AgentAudit.__table__,
        user_group_models.UserGroup.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: models.AgentMemory.metadata.create_all(
                sync_connection, tables=tables
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(config_models.GroupAgentConfig(group_id=100))
        session.add_all(
            [
                # 与成员 1 相关的两条边 + 其他成员之间的一条边。
                models.AgentRelation(
                    group_id=100,
                    subject_user_id=1,
                    object_user_id=2,
                    relation_type="mentions",
                    confidence=0.6,
                    evidence_count=1,
                    last_seen_at=NOW,
                ),
                models.AgentRelation(
                    group_id=100,
                    subject_user_id=3,
                    object_user_id=1,
                    relation_type="friend",
                    confidence=0.7,
                    evidence_count=1,
                    last_seen_at=NOW,
                ),
                models.AgentRelation(
                    group_id=100,
                    subject_user_id=3,
                    object_user_id=4,
                    relation_type="friend",
                    confidence=0.9,
                    evidence_count=3,
                    last_seen_at=NOW,
                ),
            ]
        )
        await session.commit()
        deleted = await memory.delete_member_memories(session, 100, 1)
        remaining = list(
            (
                await session.execute(
                    select(models.AgentRelation).order_by(models.AgentRelation.id)
                )
            )
            .scalars()
            .all()
        )
        # 只删成员 1 相关的 2 条边；成员 3 与 4 的关系与本次隐私删除无关。
        assert deleted == 2
        assert [
            (row.subject_user_id, row.object_user_id) for row in remaining
        ] == [(3, 4)]
    await engine.dispose()


@pytest.mark.asyncio
async def test_purge_keeps_uncompacted_messages_until_hard_cap() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        config_models.GroupAgentConfig.__table__,
        message_models.GroupAgentMessage.__table__,
        models.AgentMemory.__table__,
        models.AgentRelation.__table__,
        models.AgentPrivacy.__table__,
        audit_models.AgentAudit.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: models.AgentMemory.metadata.create_all(
                sync_connection, tables=tables
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(config_models.GroupAgentConfig(group_id=100))
        session.add_all(
            [
                # id1：已整理（<= 游标）且过期 → 删除。
                message_models.GroupAgentMessage(
                    id=1,
                    bot_id=9,
                    message_id=1,
                    group_id=100,
                    user_id=1,
                    role="member",
                    normalized_text="已整理",
                    received_at=NOW - timedelta(days=9),
                    expires_at=NOW - timedelta(days=2),
                ),
                # id2：未整理（> 游标）且过期 → 保留等待整理。
                message_models.GroupAgentMessage(
                    id=2,
                    bot_id=9,
                    message_id=2,
                    group_id=100,
                    user_id=1,
                    role="member",
                    normalized_text="等待整理",
                    received_at=NOW - timedelta(days=9),
                    expires_at=NOW - timedelta(days=2),
                ),
                # id3：未整理且超过 30 天硬上限 → 无条件删除。
                message_models.GroupAgentMessage(
                    id=3,
                    bot_id=9,
                    message_id=3,
                    group_id=100,
                    user_id=1,
                    role="member",
                    normalized_text="超过硬上限",
                    received_at=NOW - timedelta(days=60),
                    expires_at=NOW - timedelta(days=40),
                ),
            ]
        )
        await session.commit()
        deleted = await memory._purge_expired(session, 100, NOW, compacted_cursor=1)
        assert deleted == 2
        remaining_ids = list(
            (
                await session.execute(
                    select(message_models.GroupAgentMessage.id).order_by(
                        message_models.GroupAgentMessage.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert remaining_ids == [2]
    await engine.dispose()


@pytest.mark.asyncio
async def test_expired_pending_messages_still_get_compacted_after_recovery(
    monkeypatch,
) -> None:
    """整理长期失败时素材过期：恢复后必须仍能进入批次，而不是被静默丢弃。"""

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        config_models.GroupAgentConfig.__table__,
        message_models.GroupAgentMessage.__table__,
        models.AgentMemory.__table__,
        models.AgentRelation.__table__,
        models.AgentPrivacy.__table__,
        audit_models.AgentAudit.__table__,
        user_group_models.UserGroup.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: models.AgentMemory.metadata.create_all(
                sync_connection, tables=tables
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def failed_summary(_payload, **_kwargs):
        return None

    async def recovered_summary(_payload, **_kwargs):
        return {
            "summary": "恢复后的摘要",
            "public_summary": "",
            "facts": [],
            "relations": [],
        }

    async with session_factory() as session:
        session.add(config_models.GroupAgentConfig(group_id=100))
        session.add(
            message_models.GroupAgentMessage(
                bot_id=9,
                message_id=1,
                group_id=100,
                user_id=1,
                sender_name="成员",
                role="member",
                normalized_text="过期但未整理的素材",
                received_at=NOW - timedelta(days=9),
                expires_at=NOW - timedelta(days=2),
            )
        )
        await session.commit()

        monkeypatch.setattr(memory, "_model_summary", failed_summary)
        assert await memory.compact_group_memory(session, 100, now=NOW) == 0
        # 失败路径的 purge 不得删除游标之后的过期素材。
        assert (
            await session.scalar(
                select(func.count()).select_from(message_models.GroupAgentMessage)
            )
        ) == 1

        monkeypatch.setattr(memory, "_model_summary", recovered_summary)
        assert await memory.compact_group_memory(session, 100, now=NOW) == 1
        config = await session.get(config_models.GroupAgentConfig, 100)
        assert config is not None and config.last_compacted_message_id == 1
        # 整理完成后游标已推进，过期素材完成使命被清理。
        assert (
            await session.scalar(
                select(func.count()).select_from(message_models.GroupAgentMessage)
            )
        ) == 0
        summaries = list(
            (
                await session.execute(
                    select(models.AgentMemory).where(
                        models.AgentMemory.memory_type == "summary"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [row.content for row in summaries] == ["恢复后的摘要"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_rebuild_keeps_manual_and_agent_relation_edges() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        config_models.GroupAgentConfig.__table__,
        message_models.GroupAgentMessage.__table__,
        models.AgentMemory.__table__,
        models.AgentRelation.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: models.AgentMemory.metadata.create_all(
                sync_connection, tables=tables
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(config_models.GroupAgentConfig(group_id=100))
        session.add_all(
            [
                models.AgentRelation(
                    group_id=100,
                    subject_user_id=1,
                    object_user_id=2,
                    relation_type="好友",
                    source_kind="auto",
                ),
                models.AgentRelation(
                    group_id=100,
                    subject_user_id=1,
                    object_user_id=2,
                    relation_type="mentions",
                    source_kind="mention",
                ),
                models.AgentRelation(
                    group_id=100,
                    subject_user_id=3,
                    object_user_id=4,
                    relation_type="情侣",
                    source_kind="manual",
                    note="管理员录入",
                ),
                models.AgentRelation(
                    group_id=100,
                    subject_user_id=5,
                    object_user_id=6,
                    relation_type="对立",
                    source_kind="agent",
                    note="对话中记录",
                ),
            ]
        )
        await session.commit()

        await memory.rebuild_group_memories(session, 100)

        rows = list(
            (
                await session.execute(
                    select(models.AgentRelation).order_by(models.AgentRelation.id)
                )
            )
            .scalars()
            .all()
        )
        # 重建只清除 auto/mention 派生边；手工边与 Agent 对话记录的边保留。
        assert [(row.relation_type, row.source_kind) for row in rows] == [
            ("情侣", "manual"),
            ("对立", "agent"),
        ]
        config = await session.get(config_models.GroupAgentConfig, 100)
        assert config is not None and config.memory_rebuild_required
    await engine.dispose()


@pytest.mark.asyncio
async def test_stable_context_layer_is_byte_stable_across_requests() -> None:
    """同一整理窗口内，新消息只改变易变层；群摘要稳定层字节不变。"""

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        bot_group_models.BotGroup.__table__,
        config_models.GroupAgentConfig.__table__,
        message_models.GroupAgentMessage.__table__,
        models.AgentMemory.__table__,
        models.AgentRelation.__table__,
        models.AgentPrivacy.__table__,
        user_group_models.UserGroup.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: models.AgentMemory.metadata.create_all(
                sync_connection, tables=tables
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    from src.plugins.yawn_core.yawn_agent.prompt import (
        build_messages,
        split_context,
        stable_context_key,
    )

    def _message(message_id: int, user_id: int, text: str) -> Any:
        return message_models.GroupAgentMessage(
            bot_id=9,
            message_id=message_id,
            group_id=100,
            user_id=user_id,
            sender_name=f"成员{user_id}",
            role="member",
            normalized_text=text,
            received_at=NOW,
            expires_at=NOW + timedelta(days=7),
        )

    async with session_factory() as session:
        session.add_all(
            [
                bot_group_models.BotGroup(group_id=100, group_name="稳定层测试群"),
                config_models.GroupAgentConfig(group_id=100),
                _message(1, 1, "周末去爬山"),
                _message(2, 2, "我也想去"),
                models.AgentMemory(
                    group_id=100,
                    subject_user_id=0,
                    memory_type="summary",
                    memory_key="daily:2026-08-22",
                    content="成员们约周末爬山",
                    evidence_message_ids=[1, 2],
                    source_kind="auto",
                    related_user_ids=[1, 2],
                    salience=0.8,
                    confidence=0.7,
                    visibility="group",
                    expires_at=NOW + timedelta(days=30),
                ),
                models.AgentMemory(
                    group_id=100,
                    subject_user_id=1,
                    memory_type="profile",
                    memory_key="display_name",
                    content="阿眠",
                    evidence_message_ids=[1],
                    source_kind="auto",
                    related_user_ids=[1],
                    salience=0.6,
                    confidence=0.9,
                    visibility="group",
                    expires_at=NOW + timedelta(days=90),
                ),
                models.AgentRelation(
                    group_id=100,
                    subject_user_id=1,
                    object_user_id=2,
                    relation_type="mentions",
                    confidence=0.7,
                    evidence_count=2,
                    last_seen_at=NOW,
                ),
            ]
        )
        await session.commit()
        config = await session.get(config_models.GroupAgentConfig, 100)
        assert config is not None

        first = await dialogue._load_context(session, 100, config)
        session.add(_message(3, 2, "那周六早上出发"))
        await session.commit()

        second = await dialogue._load_context(session, 100, config)
        # 群摘要进入稳定层且字节不变；发言人画像与消息留在易变层。
        assert stable_context_key(first) == stable_context_key(second)
        stable_first, volatile_first = split_context(first)
        stable_second, volatile_second = split_context(second)
        assert stable_first == stable_second
        assert volatile_first != volatile_second

        messages_a, _ = build_messages(
            persona={"name": "Yawn"}, tools=[], context=first, user_prompt="好的"
        )
        messages_b, _ = build_messages(
            persona={"name": "Yawn"}, tools=[], context=second, user_prompt="收到"
        )
        assert messages_a[1] == messages_b[1]
        assert messages_a[2] != messages_b[2]
        assert "成员们约周末爬山" in messages_a[1]["content"]
        assert "那周六早上出发" in messages_b[2]["content"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_search_group_memory_reranks_substring_candidates() -> None:
    """LIKE 命中超过 10 条时按查询相关性重排，而不是返回先入库的 10 条。"""

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        config_models.GroupAgentConfig.__table__,
        message_models.GroupAgentMessage.__table__,
        models.AgentMemory.__table__,
        models.AgentRelation.__table__,
        models.AgentPrivacy.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: models.AgentMemory.metadata.create_all(
                sync_connection, tables=tables
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    class _DegradedBot:
        self_id = "9"

        async def call_api(self, *_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("测试环境无 OneBot API")

    from src.plugins.yawn_core.yawn_agent import capabilities, tools

    async with session_factory() as session:
        session.add_all(
            [
                # 11 条陈旧的低显著记忆先入库：不带重排时它们会占满前 10。
                models.AgentMemory(
                    group_id=100,
                    subject_user_id=0,
                    memory_type="summary",
                    memory_key=f"daily:2026-07-{index:02d}",
                    content="陈旧的爬山计划记录",
                    evidence_message_ids=[],
                    source_kind="auto",
                    related_user_ids=[],
                    salience=0.2,
                    confidence=0.5,
                    visibility="group",
                    expires_at=NOW + timedelta(days=30),
                )
                for index in range(1, 12)
            ]
            + [
                # 新鲜且显著的相关记忆最后入库：LIKE 顺序下排在第 12 位。
                models.AgentMemory(
                    group_id=100,
                    subject_user_id=0,
                    memory_type="summary",
                    memory_key="daily:2026-08-22",
                    content="刚整理的爬山计划",
                    evidence_message_ids=[],
                    source_kind="auto",
                    related_user_ids=[],
                    salience=0.9,
                    confidence=0.8,
                    visibility="group",
                    expires_at=NOW + timedelta(days=30),
                ),
            ]
        )
        await session.commit()

        # 让 rank_memories 的时间衰减基于固定时钟：把行 updated_at 设为
        # 陈旧/新鲜两档。
        stale_rows = list(
            (
                await session.execute(
                    select(models.AgentMemory).where(
                        models.AgentMemory.memory_key != "daily:2026-08-22"
                    )
                )
            )
            .scalars()
            .all()
        )
        fresh_row = await session.scalar(
            select(models.AgentMemory).where(
                models.AgentMemory.memory_key == "daily:2026-08-22"
            )
        )
        assert fresh_row is not None
        for row in stale_rows:
            row.updated_at = NOW - timedelta(days=60)
        fresh_row.updated_at = NOW
        await session.commit()

        stale_caps = capabilities.BotGroupCapabilities(
            role="member",
            can_manage=False,
            actions=frozenset({"send_group_msg", "get_group_info"}),
        )
        outcome = await tools.execute_tool(
            "search_group_memory",
            {"query": "爬山"},
            bot=_DegradedBot(),
            group_id=100,
            session=session,
            capabilities=stale_caps,
        )
        assert outcome["ok"] is True
        result = outcome["result"]
        assert isinstance(result, list)
        assert len(result) == 10
        # 重排后新鲜高显著记忆排第一，而非被先入库的陈旧记录挤出。
        assert result[0]["content"] == "刚整理的爬山计划"
    await engine.dispose()
