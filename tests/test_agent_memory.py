# ruff: noqa: ANN001,ANN003,DTZ005,PLR0913,PLR2004,TRY003
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
proactive = importlib.import_module("src.plugins.yawn_core.yawn_agent.proactive")
models = importlib.import_module("src.plugins.yawn_core.data_models.agent_memory")
message_models = importlib.import_module(
    "src.plugins.yawn_core.data_models.group_agent_message"
)
config_models = importlib.import_module(
    "src.plugins.yawn_core.data_models.group_agent_config"
)
group_feature_models = importlib.import_module(
    "src.plugins.yawn_core.data_models.group_feature"
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

# Keep retention fixtures relative to the test run. A fixed 2026 date made
# otherwise unrelated context tests start failing once their +7 day rows aged out.
NOW = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)


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


def test_store_model_relations_refreshes_note_only_with_high_confidence() -> None:
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
        },
        {
            "subject_user_id": 111,
            "object_user_id": 222,
            "type": "好友",
            "note": "高置信新背景",
            "evidence_message_ids": [10],
            "confidence": 0.8,
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
        {(111, 222, "好友"): existing},
    )

    assert session.added == []
    # 低置信观察不得覆盖已有备注；置信度明显更高的新证据允许刷新。
    assert existing.note == "高置信新背景"
    assert existing.confidence == pytest.approx(0.81)
    assert existing.evidence_count == 3


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
async def test_model_summary_uses_memory_task_route(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_complete(_messages, **kwargs):
        captured.update(kwargs)
        return '{"summary":"摘要","public_summary":"","facts":[],"relations":[]}'

    monkeypatch.setattr(memory, "get_client", lambda _provider="default": object())
    monkeypatch.setattr(memory, "complete", fake_complete)
    result = await memory._model_summary(
        [{"id": 1, "user_id": 1, "name": "甲", "role": "member", "text": "内容"}]
    )
    assert result is not None and result["summary"] == "摘要"
    assert captured["task"] == "agent_memory"


@pytest.mark.asyncio
async def test_model_summary_rejects_invalid_top_level_summary(monkeypatch) -> None:
    async def fake_complete(_messages, **_kwargs):
        return '{"summary":123,"public_summary":"","facts":[],"relations":[]}'

    monkeypatch.setattr(memory, "get_client", lambda _provider="default": object())
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
async def test_context_guarantees_profile_budget_and_cross_group_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

        query_counts: list[int] = []
        monkeypatch.setattr(
            dialogue, "record_agent_context_db_queries", query_counts.append
        )
        isolated = await dialogue._load_context(session, 100, target)
        # 常规隔离群：scope/privacy + messages + members + local memories +
        # relations + activity，共 6 次顺序 SQL 往返；旧实现约 10~11 次。
        assert query_counts[-1] <= 6
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
        # 跨群公开摘要额外增加 shared summaries + source privacy 两次查询。
        assert query_counts[-1] <= 8
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
                    normalized_text=(
                        f"第 {index} 条群聊消息：大家从晚饭吃什么一路聊到了"
                        "周末的爬山计划与装备清单。"
                    ),
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
                    sender_name="成员",
                    role="member",
                    normalized_text=(
                        f"第 {index} 条群聊内容：大家从晚饭吃什么聊到了周末安排。"
                    ),
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
                normalized_text="这句话需要被模型整理，用于验证失败路径会保留游标并记录退避状态。",
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
                models.AgentRelation(
                    group_id=100,
                    subject_user_id=1,
                    object_user_id=2,
                    relation_type="mentions",
                    source_kind="mention",
                    evidence_count=1,
                    last_seen_at=NOW - timedelta(days=31),
                ),
                models.AgentRelation(
                    group_id=100,
                    subject_user_id=1,
                    object_user_id=3,
                    relation_type="mentions",
                    source_kind="mention",
                    evidence_count=1,
                    last_seen_at=NOW - timedelta(days=29),
                ),
                models.AgentRelation(
                    group_id=100,
                    subject_user_id=1,
                    object_user_id=4,
                    relation_type="好友",
                    source_kind="auto",
                    evidence_count=3,
                    last_seen_at=NOW - timedelta(days=181),
                ),
                models.AgentRelation(
                    group_id=100,
                    subject_user_id=1,
                    object_user_id=5,
                    relation_type="好友",
                    source_kind="manual",
                    evidence_count=1,
                    last_seen_at=NOW - timedelta(days=365),
                ),
                models.AgentRelation(
                    group_id=100,
                    subject_user_id=1,
                    object_user_id=6,
                    relation_type="同事",
                    source_kind="agent",
                    evidence_count=2,
                    last_seen_at=NOW - timedelta(days=181),
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
        relation_targets = set(
            (
                await session.execute(select(models.AgentRelation.object_user_id))
            )
            .scalars()
            .all()
        )
        assert relation_targets == {3, 5}
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
                normalized_text="这是过期但尚未整理的素材，恢复后必须仍能进入整理批次而不是被静默丢弃。",
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
        # v11 将成员画像/关系放在半稳定层；仅新增群消息时这一层继续
        # 保持字节稳定，真正变化被推迟到 realtime 层。
        assert messages_a[2] == messages_b[2]
        assert messages_a[3] != messages_b[3]
        assert "成员们约周末爬山" in messages_a[1]["content"]
        assert "那周六早上出发" in messages_b[3]["content"]
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
                    evidence_message_ids=[555],
                    source_kind="auto",
                    related_user_ids=[],
                    salience=0.9,
                    confidence=0.8,
                    visibility="group",
                    expires_at=NOW + timedelta(days=30),
                ),
                message_models.GroupAgentMessage(
                    bot_id=9,
                    message_id=555,
                    group_id=100,
                    user_id=12345,
                    sender_name="测试用户",
                    normalized_text="[图片] 爬山路线",
                    segments=[],
                    reply_chain=[],
                    forward_tree=[],
                    media_refs=[
                        {
                            "type": "image",
                            "asset_id": 77,
                            "content_hash": "e" * 64,
                        }
                    ],
                    received_at=NOW,
                    expires_at=NOW + timedelta(days=7),
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
            {"query": "爬山", "limit": 10},
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
        assert result[0]["media_types"] == ["image"]
        assert result[0]["_agent_media_refs"][0]["asset_id"] == 77
        assert result[0]["_agent_media_refs"][0]["source_message_id"] == 555
    await engine.dispose()


# ---------- 多值画像与核心记忆 ----------


def test_merge_list_profile_update_appends_and_dedupes() -> None:
    # 空旧值：直接返回新值。
    assert memory.merge_list_profile_update("", 0.5, "爬山", 0.7) == ("爬山", 0.7)
    # 不同值追加；互为子串的详略表述保留更长版本，避免"爬山"与"喜欢爬山"并存。
    assert memory.merge_list_profile_update("爬山", 0.6, "编程", 0.7) == (
        "爬山、编程",
        0.7,
    )
    assert memory.merge_list_profile_update("爬山", 0.6, "喜欢爬山", 0.7) == (
        "喜欢爬山",
        0.7,
    )
    # 已存在的值走确认路径：内容不变、置信度取 max。
    assert memory.merge_list_profile_update("爬山、编程", 0.6, "编程", 0.8) == (
        "爬山、编程",
        0.8,
    )
    # 超出上限淘汰最旧值（FIFO）。
    merged, _confidence = memory.merge_list_profile_update(
        "一、二、三、四、五", 0.9, "六", 0.5
    )
    assert merged == "二、三、四、五、六"
    recurring, _confidence = memory.merge_list_profile_update(
        "话题一、话题二、话题三", 0.8, "话题四", 0.7, max_items=3
    )
    assert recurring == "话题二、话题三、话题四"


def test_store_model_facts_appends_list_key_values() -> None:
    session = _FakeSession()
    existing = _memory(
        memory_id=1, key="hobby", content="爬山", confidence=0.6, subject_user_id=111
    )
    profiles = {(111, "hobby"): existing}

    memory._store_model_facts(
        session,
        100,
        [
            {
                "user_id": 111,
                "key": "hobby",
                "content": "编程",
                "evidence_message_ids": [10],
                "confidence": 0.7,
            }
        ],
        {10},
        {111},
        {10: 111},
        NOW,
        profiles,
    )

    # 多值键追加而非覆盖；内容变化不触发确认棘轮。
    assert existing.content == "爬山、编程"
    assert existing.confidence == pytest.approx(0.7)

    memory._store_model_facts(
        session,
        100,
        [
            {
                "user_id": 111,
                "key": "hobby",
                "content": "编程",
                "evidence_message_ids": [11],
            }
        ],
        {11},
        {111},
        {11: 111},
        NOW,
        profiles,
    )
    # 同值复现走确认路径：内容不变、置信度 +0.02。
    assert existing.content == "爬山、编程"
    assert existing.confidence == pytest.approx(0.72)
    assert session.added == []


def test_maybe_promote_core_requires_repeated_confirmation() -> None:
    row = _memory(memory_id=1, subject_user_id=111, confidence=0.85)
    row.source_kind = "auto"
    row.evidence_message_ids = [1, 2, 3]
    memory._maybe_promote_core(row)
    assert row.memory_type == "core"
    assert row.expires_at is None

    # 置信度或证据数不足都不晋升。
    weak = _memory(memory_id=2, subject_user_id=111, confidence=0.84)
    weak.source_kind = "auto"
    weak.evidence_message_ids = [1, 2, 3]
    memory._maybe_promote_core(weak)
    assert weak.memory_type == "profile"

    thin = _memory(memory_id=3, subject_user_id=111, confidence=0.9)
    thin.source_kind = "auto"
    thin.evidence_message_ids = [1, 2]
    memory._maybe_promote_core(thin)
    assert thin.memory_type == "profile"

    # 手工行不参与自动晋升。
    manual = _memory(memory_id=4, subject_user_id=111, confidence=0.95)
    manual.source_kind = "manual"
    manual.evidence_message_ids = [1, 2, 3]
    memory._maybe_promote_core(manual)
    assert manual.memory_type == "profile"

    dynamic = _memory(
        memory_id=5,
        key="recurring_topic",
        content="常聊测试策略",
        subject_user_id=111,
        confidence=0.95,
    )
    dynamic.source_kind = "auto"
    dynamic.evidence_message_ids = [1, 2, 3, 4]
    memory._maybe_promote_core(dynamic)
    assert dynamic.memory_type == "profile"


# ---------- 检索排序：IDF、按类型半衰期、话题提示 ----------


def test_rank_memories_weights_rare_token_higher_than_common() -> None:
    # 同等基础分下，命中"只出现在少数行"的稀有 token 比两行都有的常见
    # token 得分更高，常见 bigram 不再稀释稀有命中的信号。
    rows = [
        _memory(memory_id=1, content="晚饭吃了火锅", salience=0.5),
        _memory(memory_id=2, content="中午也是火锅局", salience=0.5),
        _memory(memory_id=3, content="原神深渊打法", salience=0.5),
    ]

    ranked = memory.rank_memories(rows, ["今天吃火锅还是打原神"], None, NOW)

    assert ranked[0].id == 3


def test_rank_memories_core_rows_skip_time_decay() -> None:
    core = _memory(
        memory_id=1, memory_type="core", salience=0.6, updated_days_ago=120
    )
    fresh_profile = _memory(
        memory_id=2, memory_type="profile", salience=0.6, updated_days_ago=2
    )

    ranked = memory.rank_memories([fresh_profile, core], [], None, NOW)

    # 120 天前的核心记忆不被时间衰减埋没，仍排在 2 天前的普通画像之前。
    assert ranked[0] is core


def test_rank_memories_topic_hint_recalls_without_displacing_window() -> None:
    weather = _memory(memory_id=1, content="周末天气很适合爬山", salience=0.5)
    genshin = _memory(memory_id=2, content="原神深渊十二层攻略", salience=0.5)
    recent = ["今天天气怎么样", "明天好像要下雨"] * 5

    # 无提示：天气相关记忆领先。
    plain = memory.rank_memories([weather, genshin], recent, None, NOW)
    assert plain[0] is weather

    # 活跃话题提示并入查询集后，相关记忆反超；近期消息窗口不受挤占。
    hinted = memory.rank_memories(
        [weather, genshin], recent, None, NOW, topic_hint="原神深渊"
    )
    assert hinted[0] is genshin


# ---------- 整理管线：低信号跳过与核心晋升（数据库集成） ----------


async def _setup_memory_tables(engine: Any) -> None:
    tables = [
        config_models.GroupAgentConfig.__table__,
        group_feature_models.GroupFeature.__table__,
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


def _agent_message_row(message_id: int, text: str, *, user_id: int = 1) -> Any:
    return message_models.GroupAgentMessage(
        bot_id=9,
        message_id=message_id,
        group_id=100,
        user_id=user_id,
        sender_name="成员",
        role="member",
        normalized_text=text,
        received_at=NOW,
        expires_at=NOW + timedelta(days=7),
    )


@pytest.mark.asyncio
async def test_low_signal_batch_skips_model_but_extracts(monkeypatch) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await _setup_memory_tables(engine)

    async def forbidden_summary(_payload, **_kwargs):
        raise AssertionError("低信号批次不应调用模型")

    monkeypatch.setattr(memory, "_model_summary", forbidden_summary)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(config_models.GroupAgentConfig(group_id=100))
        session.add_all(
            [
                _agent_message_row(index, text)
                for index, text in enumerate(
                    (
                        "[图片]",
                        "哈哈哈",
                        "hhh",
                        "。。。6",
                        "好的",
                        "[表情]",
                        "嗯嗯",
                        "666",
                        "来的",
                        "大家好，我叫阿眠",
                    ),
                    start=1,
                )
            ]
        )
        await session.commit()

        assert await memory.compact_group_memory(session, 100, now=NOW) == 10
        config = await session.get(config_models.GroupAgentConfig, 100)
        assert config is not None
        assert config.last_compacted_message_id == 10
        assert config.memory_consecutive_failures == 0

        # 低信号批次不产生摘要，但确定性昵称提取仍然落库。
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.AgentMemory)
                .where(models.AgentMemory.memory_type == "summary")
            )
            == 0
        )
        display = await session.scalar(
            select(models.AgentMemory).where(
                models.AgentMemory.memory_key == "display_name"
            )
        )
        assert display is not None
        assert display.content == "阿眠"
    await engine.dispose()


@pytest.mark.asyncio
async def test_compaction_promotes_reconfirmed_display_name_to_core(
    monkeypatch,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await _setup_memory_tables(engine)

    async def forbidden_summary(_payload, **_kwargs):
        raise AssertionError("低信号批次不应调用模型")

    monkeypatch.setattr(memory, "_model_summary", forbidden_summary)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(config_models.GroupAgentConfig(group_id=100))
        session.add(
            models.AgentMemory(
                group_id=100,
                subject_user_id=1,
                memory_type="profile",
                memory_key="display_name",
                content="阿眠",
                evidence_message_ids=[90],
                source_kind="auto",
                related_user_ids=[1],
                salience=0.8,
                confidence=0.9,
                visibility="group",
                expires_at=NOW + timedelta(days=90),
            )
        )
        session.add_all(
            [
                _agent_message_row(2, "大家好，叫我阿眠"),
                _agent_message_row(3, "以后大家叫我阿眠。"),
            ]
        )
        await session.commit()

        assert await memory.compact_group_memory(session, 100, now=NOW) == 2
        row = await session.scalar(
            select(models.AgentMemory).where(
                models.AgentMemory.memory_key == "display_name"
            )
        )
        assert row is not None
        # 两批证据 + 既有证据达到 3 条、置信度棘轮到 1.0：晋升为核心记忆。
        assert row.memory_type == "core"
        assert row.expires_at is None
        assert row.confidence == pytest.approx(1.0)
    await engine.dispose()


@pytest.mark.asyncio
async def test_core_memory_survives_purge_and_prefetch() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await _setup_memory_tables(engine)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(config_models.GroupAgentConfig(group_id=100))
        session.add_all(
            [
                models.AgentMemory(
                    group_id=100,
                    subject_user_id=1,
                    memory_type="core",
                    memory_key="display_name",
                    content="阿眠",
                    evidence_message_ids=[1, 2, 3],
                    source_kind="auto",
                    related_user_ids=[1],
                    salience=0.8,
                    confidence=0.9,
                    visibility="group",
                    expires_at=None,
                ),
                models.AgentMemory(
                    group_id=100,
                    subject_user_id=2,
                    memory_type="profile",
                    memory_key="hobby",
                    content="爬山",
                    evidence_message_ids=[1],
                    source_kind="auto",
                    related_user_ids=[2],
                    salience=0.5,
                    confidence=0.5,
                    visibility="group",
                    expires_at=NOW - timedelta(days=1),
                ),
                message_models.GroupAgentMessage(
                    bot_id=9,
                    message_id=1,
                    group_id=100,
                    user_id=1,
                    sender_name="成员",
                    role="member",
                    normalized_text="旧消息",
                    received_at=NOW - timedelta(days=10),
                    expires_at=NOW - timedelta(days=1),
                ),
            ]
        )
        await session.commit()
        config = await session.get(config_models.GroupAgentConfig, 100)
        assert config is not None
        config.last_compacted_message_id = 1
        await session.commit()

        # 空批次整理只做清理：core 永不过期，过期 profile 被删除。
        assert await memory.compact_group_memory(session, 100, now=NOW) == 0
        core_row = await session.scalar(
            select(models.AgentMemory).where(models.AgentMemory.memory_type == "core")
        )
        assert core_row is not None
        assert core_row.content == "阿眠"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.AgentMemory)
                .where(models.AgentMemory.memory_type == "profile")
            )
            == 0
        )

        # 预取包含 core：同 key 事实走更新而非插入，避免撞唯一约束。
        profiles = await memory._prefetch_profiles(session, 100)
        assert (1, "display_name") in profiles
        fake_session = _FakeSession()
        memory._store_model_facts(
            fake_session,
            100,
            [
                {
                    "user_id": 1,
                    "key": "display_name",
                    "content": "阿眠",
                    "evidence_message_ids": [1],
                }
            ],
            {1},
            {1},
            {1: 1},
            NOW,
            profiles,
        )
        assert fake_session.added == []
        assert core_row.confidence == pytest.approx(0.92)
    await engine.dispose()


@pytest.mark.asyncio
async def test_context_prioritizes_core_memory_for_speaker() -> None:
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
                config_models.GroupAgentConfig(group_id=100),
                message_models.GroupAgentMessage(
                    bot_id=9,
                    message_id=0,
                    group_id=100,
                    user_id=1,
                    sender_name="当前成员",
                    role="member",
                    normalized_text="三天前的旧会话",
                    received_at=NOW - timedelta(days=3),
                    expires_at=NOW + timedelta(days=7),
                ),
                message_models.GroupAgentMessage(
                    bot_id=9,
                    message_id=2,
                    group_id=100,
                    user_id=2,
                    sender_name="另一成员",
                    role="member",
                    normalized_text="我也想继续聊测试策略",
                    received_at=NOW - timedelta(seconds=1),
                    expires_at=NOW + timedelta(days=7),
                ),
                message_models.GroupAgentMessage(
                    bot_id=9,
                    message_id=3,
                    group_id=100,
                    user_id=9,
                    sender_name="Yawn",
                    role="bot",
                    normalized_text="回放时点之后的消息",
                    received_at=NOW + timedelta(minutes=1),
                    expires_at=NOW + timedelta(days=7),
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
                user_group_models.UserGroup(
                    group_id=100,
                    user_id=1,
                    group_nickname="当前成员",
                    last_seen_at=NOW,
                ),
                user_group_models.UserGroup(
                    group_id=100,
                    user_id=2,
                    group_nickname="另一成员",
                    last_seen_at=NOW,
                ),
                user_group_models.UserGroup(
                    group_id=100,
                    user_id=3,
                    group_nickname="明确关注成员",
                    last_seen_at=NOW,
                ),
                user_group_models.UserGroup(
                    group_id=100,
                    user_id=99,
                    group_nickname="无关成员",
                    last_seen_at=NOW,
                ),
                models.AgentMemory(
                    group_id=100,
                    subject_user_id=1,
                    memory_type="core",
                    memory_key="preferred_address",
                    content="叫他眠宝",
                    evidence_message_ids=[1],
                    source_kind="auto",
                    related_user_ids=[1],
                    salience=0.4,
                    confidence=0.9,
                    visibility="group",
                    expires_at=None,
                ),
                models.AgentMemory(
                    group_id=100,
                    subject_user_id=1,
                    memory_type="profile",
                    memory_key="hobby",
                    content="爬山、编程",
                    evidence_message_ids=[1],
                    source_kind="auto",
                    related_user_ids=[1],
                    salience=0.9,
                    confidence=0.6,
                    visibility="group",
                    expires_at=NOW + timedelta(days=90),
                ),
                models.AgentMemory(
                    group_id=100,
                    subject_user_id=3,
                    memory_type="profile",
                    memory_key="hobby",
                    content="当前触发者喜欢桌游",
                    evidence_message_ids=[2],
                    source_kind="auto",
                    related_user_ids=[3],
                    salience=0.7,
                    confidence=0.8,
                    visibility="group",
                    expires_at=NOW + timedelta(days=90),
                ),
                models.AgentMemory(
                    group_id=100,
                    subject_user_id=2,
                    memory_type="profile",
                    memory_key="skill",
                    content="擅长测试设计",
                    evidence_message_ids=[2],
                    source_kind="auto",
                    related_user_ids=[2],
                    salience=0.8,
                    confidence=0.8,
                    visibility="group",
                    expires_at=NOW + timedelta(days=90),
                ),
                models.AgentMemory(
                    group_id=100,
                    subject_user_id=0,
                    memory_type="summary",
                    memory_key="daily:2026-08-23",
                    content="群里最近持续讨论 Agent 测试策略",
                    evidence_message_ids=[1, 2],
                    source_kind="auto",
                    related_user_ids=[1, 2],
                    salience=0.8,
                    confidence=0.8,
                    visibility="group",
                    expires_at=NOW + timedelta(days=45),
                ),
                models.AgentRelation(
                    group_id=100,
                    subject_user_id=1,
                    object_user_id=2,
                    relation_type="朋友",
                    source_kind="auto",
                    note="经常一起讨论测试",
                    confidence=0.8,
                    evidence_count=2,
                    last_seen_at=NOW,
                ),
                models.AgentRelation(
                    group_id=100,
                    subject_user_id=1,
                    object_user_id=2,
                    relation_type="mentions",
                    source_kind="mention",
                    confidence=0.4,
                    evidence_count=1,
                    last_seen_at=NOW,
                ),
                models.AgentRelation(
                    group_id=100,
                    subject_user_id=2,
                    object_user_id=1,
                    relation_type="mentions",
                    source_kind="mention",
                    confidence=0.5,
                    evidence_count=2,
                    last_seen_at=NOW,
                ),
            ]
        )
        await session.commit()
        target = await session.get(config_models.GroupAgentConfig, 100)
        assert target is not None

        bounded = await dialogue._load_context(
            session,
            100,
            target,
            exclude_message_id=2,
            focus_user_ids=[3],
            message_cutoff=NOW,
            reference_at=NOW,
        )
        assert all(
            item["text"] != "我也想继续聊测试策略"
            for item in bounded["messages"]
        )
        assert {item["user_id"] for item in bounded["members"]} == {1, 3}
        assert [item["message_id"] for item in bounded["messages"]] == [0, 1]
        assert bounded["messages"][0]["minutes_ago"] == 3 * 24 * 60
        assert bounded["messages"][1]["minutes_ago"] == 0
        assert bounded["messages"][1]["topic_break_before"] is True
        assert any(
            item["source_scope"] == "speaker"
            and item["subject_user_id"] == 3
            and item["content"] == "当前触发者喜欢桌游"
            for item in bounded["memories"]
        )

        context = await dialogue._load_context(session, 100, target)
        speaker_items = [
            item for item in context["memories"] if item["source_scope"] == "speaker"
        ]
        # 核心记忆不与 salience 竞争：发言者区首位始终是 core 行。
        assert speaker_items
        assert speaker_items[0]["type"] == "core"
        assert speaker_items[0]["content"] == "叫他眠宝"

        social_context = await dialogue._load_context(
            session, 100, target, include_active_profiles=True
        )
        assert any(
            item["source_scope"] == "participant"
            and item["subject_user_id"] == 2
            and item["content"] == "擅长测试设计"
            for item in social_context["memories"]
        )
        assert any(
            item["source_scope"] == "group_summary"
            and "测试策略" in item["content"]
            for item in social_context["memories"]
        )
        assert any("—朋友→" in line for line in social_context["relations"])
        assert not any(
            line.startswith("当前成员(1) —mentions→")
            for line in social_context["relations"]
        )
        assert any(
            line.startswith("另一成员(2) —mentions→")
            for line in social_context["relations"]
        )
    await engine.dispose()


# ---------- 调度触发与关系衰减 ----------


@pytest.mark.asyncio
async def test_memory_due_uses_tuned_thresholds() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await _setup_memory_tables(engine)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add_all(
            [config_models.GroupAgentConfig(group_id=100)]
            + [
                _agent_message_row(index, "消息")
                for index in range(1, 16)  # 15 条
            ]
        )
        await session.commit()
        # 15 条新消息：未达数量阈值。
        assert not await proactive._memory_due(session, 100, NOW, force=False)

        session.add(_agent_message_row(16, "消息"))
        await session.commit()
        assert await proactive._memory_due(session, 100, NOW, force=False)

        session.add(
            group_feature_models.GroupFeature(
                group_id=100, feature="group_agent", enabled=False
            )
        )
        await session.commit()
        # 群级总开关关闭后，即使达到阈值或强制扫描也不得自动整理。
        assert not await proactive._memory_due(session, 100, NOW, force=False)
        assert not await proactive._memory_due(session, 100, NOW, force=True)
    await engine.dispose()

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await _setup_memory_tables(engine)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(config_models.GroupAgentConfig(group_id=100))
        aged = _agent_message_row(1, "稀疏群的一条消息")
        aged.received_at = NOW - timedelta(minutes=7)
        session.add(aged)
        await session.commit()
        # 最老消息 7 分钟：未到年龄阈值。
        assert not await proactive._memory_due(session, 100, NOW, force=False)

        aged.received_at = NOW - timedelta(minutes=9)
        await session.commit()
        assert await proactive._memory_due(session, 100, NOW, force=False)
    await engine.dispose()


@pytest.mark.asyncio
async def test_decay_stale_relations_only_touches_stale_auto_edges() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await _setup_memory_tables(engine)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add_all(
            [
                models.AgentRelation(
                    group_id=100,
                    subject_user_id=1,
                    object_user_id=2,
                    relation_type="好友",
                    source_kind="auto",
                    confidence=0.9,
                    evidence_count=3,
                    last_seen_at=NOW - timedelta(days=100),
                ),
                models.AgentRelation(
                    group_id=100,
                    subject_user_id=1,
                    object_user_id=3,
                    relation_type="搭子",
                    source_kind="auto",
                    confidence=0.9,
                    evidence_count=1,
                    last_seen_at=NOW,
                ),
                models.AgentRelation(
                    group_id=100,
                    subject_user_id=1,
                    object_user_id=4,
                    relation_type="情侣",
                    source_kind="manual",
                    confidence=0.9,
                    evidence_count=1,
                    last_seen_at=NOW - timedelta(days=100),
                ),
            ]
        )
        await session.commit()

        assert await memory.decay_stale_relations(session, NOW) == 1
        rows = {
            int(row.object_user_id): row
            for row in (
                await session.execute(select(models.AgentRelation))
            ).scalars()
        }
        # 沉寂 100 天的 auto 边按 0.85 衰减；近期边与手工边不受影响。
        assert rows[2].confidence == pytest.approx(0.765)
        assert rows[3].confidence == pytest.approx(0.9)
        assert rows[4].confidence == pytest.approx(0.9)
    await engine.dispose()
