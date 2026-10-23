# 人物关系面板后端：列表排序/筛选/名字搜索/互相标记、摘要指标与批量删除。
# ruff: noqa: PLR2004, E501, ARG001
from __future__ import annotations

import importlib
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nonebot
import pytest
import pytest_asyncio
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

relation_routes = importlib.import_module(
    "src.plugins.yawn_core.webui.agent_relation_routes"
)
route_models = importlib.import_module("src.plugins.yawn_core.webui.route_models")
memory_models = importlib.import_module("src.plugins.yawn_core.data_models.agent_memory")
user_group_models = importlib.import_module("src.plugins.yawn_core.data_models.user_group")
bot_user_models = importlib.import_module("src.plugins.yawn_core.data_models.bot_user")
context = importlib.import_module("src.plugins.yawn_core.yawn_agent.context")

ADMIN = SimpleNamespace(role="admin")
GUEST = SimpleNamespace(role="guest")


@pytest_asyncio.fixture
async def factory(monkeypatch: pytest.MonkeyPatch) -> Any:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        memory_models.AgentRelation.__table__,
        memory_models.AgentPrivacy.__table__,
        user_group_models.UserGroup.__table__,
        bot_user_models.BotUser.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: memory_models.AgentRelation.metadata.create_all(sync, tables=tables)
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = context.now_beijing()

    def relation(subject: int, obj: int, kind: str, **extra: Any) -> Any:
        return memory_models.AgentRelation(
            group_id=100,
            subject_user_id=subject,
            object_user_id=obj,
            relation_type=kind,
            source_kind=extra.get("source", "auto"),
            note=extra.get("note", ""),
            confidence=extra.get("confidence", 0.8),
            evidence_count=extra.get("evidence", 1),
            last_seen_at=now - timedelta(days=extra.get("days", 0)),
        )

    async with session_factory() as session:
        session.add_all(
            [
                bot_user_models.BotUser(user_id=1, nickname="阿一"),
                bot_user_models.BotUser(user_id=2, nickname="小二"),
                bot_user_models.BotUser(user_id=3, nickname="张三"),
                bot_user_models.BotUser(user_id=4, nickname="退出者"),
                *[
                    user_group_models.UserGroup(group_id=100, user_id=uid, group_nickname=nick)
                    for uid, nick in ((1, None), (2, "二哥"), (3, None), (4, None))
                ],
                memory_models.AgentPrivacy(group_id=100, user_id=4, opted_out=True),
                relation(1, 2, "好友", confidence=0.9, evidence=3, days=1),
                relation(2, 1, "好友", confidence=0.7, evidence=5, days=2, source="manual"),
                relation(1, 3, "对立", confidence=0.3, evidence=1, days=90, source="mention"),
                relation(3, 2, "同学", confidence=0.6, evidence=9, days=30),
                relation(1, 4, "好友", confidence=0.95, days=0),
                memory_models.AgentRelation(
                    group_id=200, subject_user_id=1, object_user_id=2,
                    relation_type="好友", source_kind="auto", note="",
                    confidence=0.5, evidence_count=1, last_seen_at=now,
                ),
            ]
        )
        await session.commit()

    async def require_group(*_args: object) -> None:
        return None

    deleted: list[str] = []

    async def notify_change(_resource: str, entity: str, **_kwargs: object) -> None:
        deleted.append(entity)

    monkeypatch.setattr(relation_routes, "get_session", session_factory)
    monkeypatch.setattr(relation_routes, "require_group", require_group)
    monkeypatch.setattr(relation_routes.hub, "notify_change", notify_change)
    yield SimpleNamespace(factory=session_factory, notified=deleted)
    await engine.dispose()


async def _list(session: Any = ADMIN, **params: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "page": 1, "page_size": 20, "search": "", "relation_type": "",
        "source": "", "member": None, "min_confidence": None,
        "max_confidence": None, "sort": "confidence", "order": "desc",
    }
    defaults.update(params)
    return await relation_routes.get_relations(100, session, **defaults)


@pytest.mark.asyncio
async def test_list_sorts_filters_and_hides_opted_out(factory: Any) -> None:
    result = await _list()
    # opted-out 成员 4 的边与其它群的边都不出现。
    assert [row["confidence"] for row in result["data"]] == [0.9, 0.7, 0.6, 0.3]
    assert result["meta"]["total"] == 4

    by_evidence = await _list(sort="evidence", order="asc")
    assert [row["evidenceCount"] for row in by_evidence["data"]] == [1, 3, 5, 9]

    by_seen = await _list(sort="lastSeen", order="desc")
    assert by_seen["data"][0]["confidence"] == 0.9

    unknown_sort = await _list(sort="drop table")
    assert unknown_sort["data"][0]["confidence"] == 0.9

    manual = await _list(source="manual")
    assert [row["sourceKind"] for row in manual["data"]] == ["manual"]

    ranged = await _list(min_confidence=0.5, max_confidence=0.8)
    assert sorted(row["confidence"] for row in ranged["data"]) == [0.6, 0.7]

    member = await _list(member=3)
    assert {row["type"] for row in member["data"]} == {"对立", "同学"}


@pytest.mark.asyncio
async def test_list_searches_names_and_marks_mutual(factory: Any) -> None:
    by_group_nick = await _list(search="二哥")
    assert by_group_nick["meta"]["total"] == 3
    assert {row["objectName"] for row in by_group_nick["data"]} >= {"二哥"}

    none = await _list(search="不存在的人")
    assert none["data"] == []
    assert none["meta"]["total"] == 0

    rows = (await _list())["data"]
    mutual = {(row["subjectUserId"], row["objectUserId"]): row["mutual"] for row in rows}
    assert mutual[("1", "2")] is True
    assert mutual[("2", "1")] is True
    assert mutual[("1", "3")] is False


@pytest.mark.asyncio
async def test_guest_list_ignores_source_filter(factory: Any) -> None:
    result = await _list(GUEST, source="manual")
    assert result["meta"]["total"] == 4
    assert "sourceKind" not in result["data"][0]


@pytest.mark.asyncio
async def test_summary_reports_health_metrics(factory: Any) -> None:
    admin = (await relation_routes.get_relation_summary(100, ADMIN))["data"]
    assert admin["edgeCount"] == 4
    assert admin["linkedMemberCount"] == 3
    assert admin["mutualEdgeCount"] == 2
    assert admin["freshEdgeCount"] == 2
    assert admin["staleEdgeCount"] == 1
    assert admin["lowConfidenceCount"] == 1
    assert admin["avgConfidence"] == pytest.approx(0.625)
    assert {item["source"]: item["count"] for item in admin["sourceCounts"]} == {
        "auto": 2, "manual": 1, "mention": 1,
    }

    guest = (await relation_routes.get_relation_summary(100, GUEST))["data"]
    assert "sourceCounts" not in guest
    assert guest["mutualEdgeCount"] == 2


@pytest.mark.asyncio
async def test_batch_delete_only_touches_current_group(factory: Any) -> None:
    rows = (await _list())["data"]
    target_ids = [int(rows[0]["id"]), int(rows[1]["id"])]
    body = route_models.RelationBatchDeleteBody.model_validate(
        {"ids": [*target_ids, 6, 99999]}
    )
    result = await relation_routes.batch_delete_relations(100, body, ADMIN)
    assert result["data"] == {"deleted": 2, "missing": 2}
    assert sorted(factory.notified) == sorted(str(item) for item in target_ids)
    assert (await _list())["meta"]["total"] == 2

    other = await relation_routes.batch_delete_relations(
        200, route_models.RelationBatchDeleteBody.model_validate({"ids": [6]}), ADMIN
    )
    assert other["data"]["deleted"] == 1


def test_batch_delete_body_limits_size() -> None:
    with pytest.raises(ValueError, match="ids"):
        route_models.RelationBatchDeleteBody.model_validate({"ids": []})
    with pytest.raises(ValueError, match="ids"):
        route_models.RelationBatchDeleteBody.model_validate({"ids": list(range(1, 202))})
