# ruff: noqa: ANN001, ANN002, ANN003, ANN201, ARG001, PLR2004
"""WebUI 群组 / 用户页 2.0 接口：筛选排序、统计、档案、覆盖一览与批量写入。"""

from __future__ import annotations

import importlib
import inspect
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import nonebot
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
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

_PKG = "src.plugins.yawn_core"
groups = importlib.import_module(f"{_PKG}.webui.groups")
users = importlib.import_module(f"{_PKG}.webui.users")
route_models = importlib.import_module(f"{_PKG}.webui.route_models")
checkin_service = importlib.import_module(f"{_PKG}.checkin_service")
Model = importlib.import_module("nonebot_plugin_orm").Model
_M = f"{_PKG}.data_models"
BotGroup = importlib.import_module(f"{_M}.bot_group").BotGroup
BotUser = importlib.import_module(f"{_M}.bot_user").BotUser
UserGroup = importlib.import_module(f"{_M}.user_group").UserGroup
CheckinUser = importlib.import_module(f"{_M}.checkin_user").CheckinUser
CheckinRecord = importlib.import_module(f"{_M}.checkin_record").CheckinRecord
GroupFeature = importlib.import_module(f"{_M}.group_feature").GroupFeature
UserFeature = importlib.import_module(f"{_M}.user_feature").UserFeature
GlobalUserFeature = importlib.import_module(
    f"{_M}.global_user_feature"
).GlobalUserFeature
GroupAgentConfig = importlib.import_module(f"{_M}.group_agent_config").GroupAgentConfig

NOW = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
TODAY = checkin_service.checkin_today()
G1, G2, G3 = 1001, 1002, 1003
U1, U2, U3 = 11, 22, 33


async def call(func, *args, **kwargs):
    """直接调用路由函数：位置参数按签名顺序，其余 Query(...) 换成真实默认值。"""

    params = {}
    for index, (name, param) in enumerate(inspect.signature(func).parameters.items()):
        if index < len(args):
            params[name] = args[index]
        elif name in kwargs:
            params[name] = kwargs[name]
        elif hasattr(param.default, "default"):
            params[name] = param.default.default
    return await func(**params)


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Model.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all(
            [
                BotGroup(
                    group_id=G1,
                    group_name="猫猫群",
                    first_seen_at=NOW - timedelta(days=30),
                    last_active_at=NOW - timedelta(hours=1),
                ),
                BotGroup(
                    group_id=G2,
                    group_name="摸鱼群",
                    first_seen_at=NOW - timedelta(days=3),
                    last_active_at=NOW - timedelta(days=10),
                ),
                BotGroup(
                    group_id=G3,
                    group_name=None,
                    first_seen_at=NOW - timedelta(days=1),
                    last_active_at=None,
                ),
                BotUser(
                    user_id=U1,
                    nickname="小明",
                    affinity=300,
                    first_interaction_at=NOW - timedelta(days=40),
                    last_interaction_at=NOW - timedelta(minutes=5),
                ),
                BotUser(
                    user_id=U2,
                    nickname="阿狸",
                    affinity=20,
                    first_interaction_at=NOW - timedelta(days=2),
                    last_interaction_at=NOW - timedelta(days=20),
                ),
                BotUser(
                    user_id=U3,
                    nickname="三花",
                    affinity=0,
                    first_interaction_at=NOW - timedelta(days=1),
                    last_interaction_at=None,
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                UserGroup(
                    group_id=G1,
                    user_id=U1,
                    role="owner",
                    group_nickname="群主小明",
                    group_affinity=50,
                    first_seen_at=NOW - timedelta(days=30),
                    last_seen_at=NOW - timedelta(hours=1),
                ),
                UserGroup(
                    group_id=G1,
                    user_id=U2,
                    role="member",
                    group_affinity=5,
                    first_seen_at=NOW - timedelta(days=2),
                    last_seen_at=NOW - timedelta(days=12),
                ),
                UserGroup(
                    group_id=G1,
                    user_id=U3,
                    role="admin",
                    first_seen_at=NOW - timedelta(days=1),
                    last_seen_at=None,
                ),
                UserGroup(
                    group_id=G2,
                    user_id=U1,
                    role="member",
                    first_seen_at=NOW - timedelta(days=3),
                    last_seen_at=NOW - timedelta(days=10),
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                CheckinUser(
                    group_id=G1,
                    user_id=U1,
                    total_days=40,
                    streak_days=12,
                    points=100,
                    last_checkin_date=TODAY,
                ),
                # 断签：库里 streak 还是 9，但上次是三天前
                CheckinUser(
                    group_id=G1,
                    user_id=U2,
                    total_days=9,
                    streak_days=9,
                    points=20,
                    last_checkin_date=TODAY - timedelta(days=3),
                ),
                CheckinRecord(group_id=G1, user_id=U1, checkin_date=TODAY, reward=5),
                GroupFeature(group_id=G1, feature="fanqie", enabled=False),
                GroupFeature(group_id=G2, feature="group_agent", enabled=False),
                UserFeature(group_id=G1, user_id=U2, feature="werewolf", enabled=False),
                UserFeature(group_id=G1, user_id=U2, feature="fanqie", enabled=False),
                GlobalUserFeature(user_id=U3, feature="ai_chat", enabled=False),
            ]
        )
        await session.commit()

    @asynccontextmanager
    async def fake_session():
        async with factory() as session:
            yield session

    notified: list[tuple[str, str]] = []

    async def notify(resource: str, key: str, **_kwargs) -> None:
        notified.append((resource, key))

    closed: list[int] = []
    for module in (groups, users):
        monkeypatch.setattr(module, "get_session", fake_session)
        monkeypatch.setattr(module.hub, "notify_change", notify)
    monkeypatch.setattr(
        groups, "close_group_conversations", lambda gid, **_: closed.append(gid)
    )
    yield factory, notified, closed
    await engine.dispose()


def _ids(result, key="groupId"):
    return [row[key] for row in result["data"]]


@pytest.mark.asyncio
async def test_group_list_filters_sorts_and_stats(db):
    result = await call(groups.get_groups, None)
    # 默认按最近活跃，从没活跃过的群排最后
    assert _ids(result) == [str(G1), str(G2), str(G3)]
    first = result["data"][0]
    assert first["memberCount"] == 3
    assert first["activeMembers7d"] == 1
    assert first["checkinsToday"] == 1
    assert first["overrideCount"] == 1
    assert first["disabledFeatures"] == ["fanqie"]
    assert first["memberOverrideCount"] == 2
    assert first["agentEnabled"] is True
    assert result["data"][1]["agentEnabled"] is False

    assert _ids(await call(groups.get_groups, None, agent="off")) == [str(G2)]
    assert _ids(await call(groups.get_groups, None, agent="on")) == [str(G1), str(G3)]
    assert _ids(await call(groups.get_groups, None, overrides="member")) == [str(G1)]
    assert set(_ids(await call(groups.get_groups, None, overrides="group"))) == {
        str(G1),
        str(G2),
    }
    assert _ids(await call(groups.get_groups, None, active="24h")) == [str(G1)]
    assert set(_ids(await call(groups.get_groups, None, active="dormant"))) == {str(G3)}
    assert _ids(await call(groups.get_groups, None, sort="members"))[0] == str(G1)
    assert _ids(await call(groups.get_groups, None, sort="first_seen"))[0] == str(G3)
    assert _ids(await call(groups.get_groups, None, sort="name"))[-1] == str(G3)
    assert _ids(await call(groups.get_groups, None, sort="bogus"))[0] == str(G1)
    paged = await call(groups.get_groups, None, page_size=1, page=2)
    assert paged["meta"]["total"] == 3
    assert _ids(paged) == [str(G2)]


@pytest.mark.asyncio
async def test_groups_summary_counts(db):
    data = (await groups.get_groups_summary(None))["data"]
    assert data == {
        "total": 3,
        "agentEnabled": 2,
        "active24h": 1,
        "active7d": 1,
        "withOverrides": 2,
        "checkinsToday": 1,
        "members": 4,
    }


@pytest.mark.asyncio
async def test_group_overview_ignores_broken_streaks(db):
    data = (await groups.get_group_overview(G1, None))["data"]
    assert data["roles"] == {"owner": 1, "member": 1, "admin": 1}
    assert data["activeMembers"] == {"24h": 1, "7d": 1, "30d": 2}
    assert data["checkins"]["today"] == 1
    assert [row["userId"] for row in data["checkins"]["streakTop"]] == [str(U1)]
    assert data["affinityTop"][0]["name"] == "群主小明"
    assert data["overrides"] == {"group": 1, "member": 2, "membersWithOverrides": 1}
    assert data["agent"]["enabled"] is True
    with pytest.raises(HTTPException):
        await groups.get_group_overview(999, None)


@pytest.mark.asyncio
async def test_member_list_filters_and_fields(db):
    result = await call(groups.get_members, G1, None)
    assert _ids(result, "userId") == [str(U1), str(U2), str(U3)]
    by_id = {row["userId"]: row for row in result["data"]}
    assert by_id[str(U1)]["streakDays"] == 12
    assert by_id[str(U1)]["checkedToday"] is True
    assert by_id[str(U2)]["streakDays"] == 0  # 断签显示 0
    assert by_id[str(U2)]["overrideCount"] == 2
    assert _ids(await call(groups.get_members, G1, None, role="admin"), "userId") == [
        str(U3)
    ]
    assert _ids(await call(groups.get_members, G1, None, overrides=True), "userId") == [
        str(U2)
    ]
    assert _ids(
        await call(groups.get_members, G1, None, activity="dormant"), "userId"
    ) == [str(U3)]
    assert _ids(await call(groups.get_members, G1, None, sort="streak"), "userId")[
        0
    ] == str(U1)
    assert _ids(await call(groups.get_members, G1, None, search="群主"), "userId") == [
        str(U1)
    ]


@pytest.mark.asyncio
async def test_member_overrides_listing_marks_redundant(db):
    rows = (await groups.get_group_member_overrides(G1, None))["data"]
    by_feature = {row["feature"]: row for row in rows}
    assert by_feature["werewolf"]["groupEffective"] is True
    assert by_feature["werewolf"]["redundant"] is False
    # 群里已经关了番茄，成员再关一次是多余的
    assert by_feature["fanqie"]["groupEffective"] is False
    assert by_feature["fanqie"]["redundant"] is True


@pytest.mark.asyncio
async def test_member_agent_override_blocked_by_group_master(db):
    factory, _notified, _closed = db
    async with factory() as session:
        session.add(
            UserFeature(group_id=G2, user_id=U1, feature="group_agent", enabled=True)
        )
        await session.commit()
    rows = (await groups.get_group_member_overrides(G2, None))["data"]
    assert rows[0]["blockedByGroup"] is True
    assert rows[0]["effective"] is False


@pytest.mark.asyncio
async def test_reset_group_features_keeps_agent_by_default(db):
    factory, notified, _closed = db
    body = route_models.GroupFeatureResetBody()
    result = await groups.reset_group_feature_overrides(G2, body, None)
    assert result["data"]["removed"] == []
    async with factory() as session:
        assert await session.get(
            GroupFeature, {"group_id": G2, "feature": "group_agent"}
        )
    body = route_models.GroupFeatureResetBody.model_validate({"includeAgent": True})
    result = await groups.reset_group_feature_overrides(G2, body, None)
    assert result["data"]["removed"] == ["group_agent"]
    async with factory() as session:
        cfg = await session.get(GroupAgentConfig, G2)
        assert cfg is not None
        assert cfg.enabled is True
    assert ("agent_config", str(G2)) in notified


@pytest.mark.asyncio
async def test_batch_group_feature_and_agent_close(db):
    factory, notified, closed = db
    body = route_models.FeatureBatchBody(
        ids=[str(G1), str(G3), str(G1)], feature="group_agent", override=False
    )
    result = await groups.batch_group_feature(body, None)
    assert result["data"]["updated"] == 2
    assert closed == [G1, G3]
    async with factory() as session:
        for gid in (G1, G3):
            cfg = await session.get(GroupAgentConfig, gid)
            assert cfg is not None
            assert cfg.enabled is False
    assert ("group_feature", f"{G1}:group_agent") in notified

    with pytest.raises(HTTPException) as missing:
        await groups.batch_group_feature(
            route_models.FeatureBatchBody(ids=["424242"], feature="rpg", override=True),
            None,
        )
    assert missing.value.status_code == 404
    with pytest.raises(HTTPException):
        await groups.batch_group_feature(
            route_models.FeatureBatchBody(ids=[str(G1)], feature="nope", override=True),
            None,
        )
    with pytest.raises(HTTPException) as bad:
        await groups.batch_group_feature(
            route_models.FeatureBatchBody(ids=["abc"], feature="rpg", override=True),
            None,
        )
    assert bad.value.status_code == 400


@pytest.mark.asyncio
async def test_batch_member_feature_and_reset(db):
    factory, _notified, _closed = db
    body = route_models.FeatureBatchBody(
        ids=[str(U1), str(U3)], feature="rpg", override=False
    )
    assert (await groups.batch_member_feature(G1, body, None))["data"]["updated"] == 2
    with pytest.raises(HTTPException):
        await groups.batch_member_feature(
            G2,
            route_models.FeatureBatchBody(ids=[str(U3)], feature="rpg", override=False),
            None,
        )
    result = await groups.reset_member_feature_overrides(G1, U2, None)
    assert result["data"]["removed"] == 2
    async with factory() as session:
        left = (
            (
                await session.execute(
                    select(UserFeature).where(UserFeature.group_id == G1)
                )
            )
            .scalars()
            .all()
        )
    assert {(row.user_id, row.feature) for row in left} == {(U1, "rpg"), (U3, "rpg")}


@pytest.mark.asyncio
async def test_user_list_filters_and_summary(db):
    result = await call(users.get_users, None)
    assert _ids(result, "userId") == [str(U1), str(U2), str(U3)]
    first = result["data"][0]
    assert first["groupCount"] == 2
    assert first["affinityLevel"]
    assert _ids(await call(users.get_users, None, overrides="private"), "userId") == [
        str(U3)
    ]
    assert _ids(await call(users.get_users, None, overrides="member"), "userId") == [
        str(U2)
    ]
    assert _ids(await call(users.get_users, None, sort="groups"), "userId")[0] == str(
        U1
    )
    assert _ids(await call(users.get_users, None, activity="24h"), "userId") == [
        str(U1)
    ]
    summary = (await users.get_users_summary(None))["data"]
    assert summary["total"] == 3
    assert summary["active24h"] == 1
    assert summary["withOverrides"] == 1
    assert summary["withMemberOverrides"] == 1
    assert summary["checkinsToday"] == 1


@pytest.mark.asyncio
async def test_user_profile_lists_memberships(db):
    data = (await users.get_user_profile(U1, None))["data"]
    assert data["groupCount"] == 2
    assert data["checkedTodayCount"] == 1
    assert data["level"]["name"] == data["affinityLevel"]
    first = data["memberships"][0]
    assert first["groupId"] == str(G1)
    assert first["role"] == "owner"
    assert first["streakDays"] == 12
    assert data["memberships"][1]["agentEnabled"] is False
    with pytest.raises(HTTPException):
        await users.get_user_profile(999, None)


@pytest.mark.asyncio
async def test_batch_user_feature(db):
    factory, notified, _closed = db
    body = route_models.FeatureBatchBody(
        ids=[str(U1), str(U2)], feature="ai_chat", override=False
    )
    assert (await users.batch_user_feature(body, None))["data"]["updated"] == 2
    async with factory() as session:
        rows = (await session.execute(select(GlobalUserFeature))).scalars().all()
    assert {row.user_id for row in rows} == {U1, U2, U3}
    assert ("global_user_feature", f"{U1}:ai_chat") in notified
    with pytest.raises(HTTPException):
        await users.batch_user_feature(
            route_models.FeatureBatchBody(ids=["5"], feature="ai_chat", override=None),
            None,
        )


def test_batch_body_limits():
    with pytest.raises(ValueError, match="at most 100"):
        route_models.FeatureBatchBody(
            ids=[str(i) for i in range(101)], feature="rpg", override=True
        )
    with pytest.raises(ValueError, match="at least 1"):
        route_models.FeatureBatchBody(ids=[], feature="rpg", override=True)
