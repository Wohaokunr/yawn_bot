# ruff: noqa: ANN001, ANN201, DTZ001, FBT003, PERF401, PLR2004
"""签到服务与全局好感：奖励、连签、里程碑、每日上限、流水与降级。"""

from __future__ import annotations

import importlib
import sys
from datetime import datetime, timedelta
from pathlib import Path

import nonebot
import pytest
import pytest_asyncio
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

affinity = importlib.import_module("src.plugins.yawn_core.affinity")
service = importlib.import_module("src.plugins.yawn_core.checkin_service")
checkin_cmd = importlib.import_module("src.plugins.yawn_core.checkin")
_models = "src.plugins.yawn_core.data_models"
AffinityEvent = importlib.import_module(f"{_models}.affinity_event").AffinityEvent
BotUser = importlib.import_module(f"{_models}.bot_user").BotUser
BotGroup = importlib.import_module(f"{_models}.bot_group").BotGroup
UserGroup = importlib.import_module(f"{_models}.user_group").UserGroup
CheckinUser = importlib.import_module(f"{_models}.checkin_user").CheckinUser
CheckinRecord = importlib.import_module(f"{_models}.checkin_record").CheckinRecord

DAY = datetime(2026, 10, 31, 9, 0)
USER = 42
G1, G2 = 1001, 1002


async def _factory(*, with_ledger: bool = True):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        BotUser.__table__,
        BotGroup.__table__,
        UserGroup.__table__,
        CheckinUser.__table__,
        CheckinRecord.__table__,
    ]
    if with_ledger:
        tables.append(AffinityEvent.__table__)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync: BotUser.metadata.create_all(sync, tables=tables)
        )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def factory():
    affinity.reset_ledger_state()
    engine, maker = await _factory()
    yield maker
    await engine.dispose()
    affinity.reset_ledger_state()


async def _checkin(maker, group_id: int, when: datetime, reward: int = 7):
    async with maker() as session:
        result = await service.perform_checkin(
            session,
            group_id=group_id,
            user_id=USER,
            nickname="小明",
            now=when,
            points_reward=reward,
        )
        await session.commit()
        return result


# ── 等级 ─────────────────────────────────────────────────


def test_levels_are_monotonic_and_words_hide_numbers() -> None:
    floors = [floor for floor, _ in affinity.LEVELS]
    assert floors == sorted(floors) and floors[0] == 0
    assert affinity.level_for(-5).name == "陌生"
    assert affinity.level_for(0).name == "陌生"
    assert affinity.level_for(20).name == "面熟"
    top = affinity.level_for(10_000)
    assert top.next_floor is None and top.progress(10_000) == 1.0
    assert set(affinity.LEVEL_TONE) == {name for _, name in affinity.LEVELS}
    words = affinity.level_words(160)
    assert words.startswith("朋友") and not any(ch.isdigit() for ch in words)


def test_level_progress_and_distance() -> None:
    level = affinity.level_for(40)
    assert level.name == "面熟"
    assert level.to_next(40) == 20
    assert 0 < level.progress(40) < 1


# ── 好感发放 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_grant_respects_daily_cap_and_writes_ledger(factory) -> None:
    async with factory() as session:
        first = await affinity.grant(session, USER, "group_chat", 3, now=DAY)
        second = await affinity.grant(session, USER, "group_chat", 3, now=DAY)
        third = await affinity.grant(session, USER, "group_chat", 3, now=DAY)
        await session.commit()
    assert (first.applied, second.applied, third.applied) == (3, 1, 0)
    assert second.capped and third.capped
    async with factory() as session:
        user = await session.get(BotUser, USER)
        events = (await session.execute(select(AffinityEvent))).scalars().all()
    assert user.affinity == 4
    assert [event.delta for event in events] == [3, 1]
    assert events[-1].balance_after == 4


@pytest.mark.asyncio
async def test_grant_cap_resets_next_day(factory) -> None:
    async with factory() as session:
        await affinity.grant(session, USER, "private_chat", 9, now=DAY)
        nxt = await affinity.grant(
            session, USER, "private_chat", 2, now=DAY + timedelta(days=1)
        )
        await session.commit()
    assert nxt.applied == 2 and nxt.after == 7


@pytest.mark.asyncio
async def test_grant_rejects_unknown_source_and_negative(factory) -> None:
    async with factory() as session:
        with pytest.raises(ValueError):
            await affinity.grant(session, USER, "nope", 1)
        with pytest.raises(ValueError):
            await affinity.grant(session, USER, "checkin", -1)


@pytest.mark.asyncio
async def test_manual_can_subtract_but_not_below_zero(factory) -> None:
    async with factory() as session:
        await affinity.grant(session, USER, "manual", 5, now=DAY)
        down = await affinity.grant(session, USER, "manual", -20, now=DAY)
        await session.commit()
    assert down.applied == -5 and down.after == 0


@pytest.mark.asyncio
async def test_grant_updates_group_affinity_when_member(factory) -> None:
    async with factory() as session:
        session.add(BotGroup(group_id=G1))
        session.add(BotUser(user_id=USER, affinity=0))
        await session.flush()
        member = UserGroup(group_id=G1, user_id=USER)
        member.group_affinity = 0
        session.add(member)
        await session.flush()
        await affinity.grant(session, USER, "group_chat", 2, group_id=G1, now=DAY)
        await affinity.grant(session, USER, "group_chat", 1, group_id=G2, now=DAY)
        await session.commit()
    async with factory() as session:
        member = await session.get(UserGroup, (G1, USER))
        user = await session.get(BotUser, USER)
        ranking = await affinity.group_ranking(session, G1)
    assert member.group_affinity == 2
    assert user.affinity == 3
    assert ranking == [(USER, 2)]


@pytest.mark.asyncio
async def test_grant_degrades_when_ledger_missing() -> None:
    affinity.reset_ledger_state()
    engine, maker = await _factory(with_ledger=False)
    try:
        async with maker() as session:
            change = await affinity.grant(session, USER, "checkin", 3, now=DAY)
            assert change.available is False and change.applied == 0
            # 已知缺失后直接短路，不再查库
            again = await affinity.grant(session, USER, "checkin", 3, now=DAY)
            assert again.available is False
            snap = await affinity.snapshot(session, USER, now=DAY)
            assert snap.available is False and snap.points == 0
            assert await affinity.recent_events(session, USER) is None
    finally:
        await engine.dispose()
        affinity.reset_ledger_state()


@pytest.mark.asyncio
async def test_snapshot_and_breakdown(factory) -> None:
    async with factory() as session:
        await affinity.grant(session, USER, "checkin", 3, now=DAY)
        await affinity.grant(session, USER, "private_chat", 2, now=DAY)
        await session.commit()
        snap = await affinity.snapshot(session, USER, now=DAY)
        breakdown = await affinity.source_breakdown(session, USER, now=DAY)
        events = await affinity.recent_events(session, USER)
    assert snap.points == 5 and snap.today == {"checkin": 3, "private_chat": 2}
    assert "points" not in snap.public_dict()
    assert snap.public_dict(include_points=True)["points"] == 5
    assert breakdown == {"checkin": 3, "private_chat": 2}
    assert events[0]["source"] == "private_chat" and events[0]["source_label"] == "私聊"


# ── 签到服务 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_checkin_awards_points_exp_and_affinity(factory) -> None:
    result = await _checkin(factory, G1, DAY)
    assert result.ok and result.reward == 7
    assert result.streak_days == 1 and result.total_days == 1
    assert result.exp_gain == service.EXP_BASE
    assert result.rank_today == 1
    assert result.affinity_gain == service.AFFINITY_BASE
    async with factory() as session:
        user = await session.get(BotUser, USER)
        member = await session.get(UserGroup, (G1, USER))
    assert user.affinity == service.AFFINITY_BASE
    assert member.exp == service.EXP_BASE
    assert member.group_affinity == service.AFFINITY_BASE


@pytest.mark.asyncio
async def test_second_checkin_same_day_is_already(factory) -> None:
    await _checkin(factory, G1, DAY)
    again = await _checkin(factory, G1, DAY + timedelta(hours=3))
    assert again.status == "already"
    assert again.reward == 7 and again.streak_days == 1
    assert "已经签到" in service.format_checkin_reply(again)


@pytest.mark.asyncio
async def test_duplicate_checkin_keeps_callers_pending_writes(factory) -> None:
    await _checkin(factory, G1, DAY)
    async with factory() as session:
        session.add(BotGroup(group_id=9999, group_name="别的写入"))
        await session.flush()
        again = await service.perform_checkin(
            session, group_id=G1, user_id=USER, now=DAY, points_reward=5
        )
        await session.commit()
    assert again.status == "already"
    async with factory() as session:
        assert await session.get(BotGroup, 9999) is not None


@pytest.mark.asyncio
async def test_streak_and_milestone(factory) -> None:
    results = [
        await _checkin(factory, G1, DAY + timedelta(days=offset)) for offset in range(7)
    ]
    last = results[-1]
    assert last.streak_days == 7 and last.milestone == 7
    assert last.milestone_points == service.MILESTONES[7][0]
    assert last.points == 7 * 7 + service.MILESTONES[7][0]
    assert last.exp_gain == service.exp_reward(7)
    assert service.affinity_reward(7) == service.AFFINITY_BASE + 1
    assert last.next_milestone == 30
    assert "里程碑" not in service.format_checkin_reply(results[2])
    assert "连签 7 天达成" in service.format_checkin_reply(last)


@pytest.mark.asyncio
async def test_streak_resets_after_gap(factory) -> None:
    await _checkin(factory, G1, DAY)
    await _checkin(factory, G1, DAY + timedelta(days=1))
    later = await _checkin(factory, G1, DAY + timedelta(days=4))
    assert later.streak_days == 1 and later.total_days == 3


@pytest.mark.asyncio
async def test_multi_group_checkin_shares_daily_affinity_cap(factory) -> None:
    gains = []
    for group in (G1, G2, 1003, 1004, 1005):
        gains.append((await _checkin(factory, group, DAY)).affinity_gain)
    assert sum(gains) == affinity.SOURCE_RULES["checkin"].daily_cap
    assert gains[-1] == 0


@pytest.mark.asyncio
async def test_checkin_rank_counts_group_today(factory) -> None:
    await _checkin(factory, G1, DAY)
    async with factory() as session:
        other = await service.perform_checkin(
            session, group_id=G1, user_id=77, now=DAY, points_reward=5
        )
        await session.commit()
        board = await service.group_checkin_board(session, G1, now=DAY)
    assert other.rank_today == 2
    assert board == [(USER, 1), (77, 2)]


@pytest.mark.asyncio
async def test_checkin_without_ledger_still_succeeds() -> None:
    affinity.reset_ledger_state()
    engine, maker = await _factory(with_ledger=False)
    try:
        result = await _checkin(maker, G1, DAY)
        assert result.ok and result.affinity_gain == 0
        assert result.leveled_up_to is None
        assert "好感" not in service.format_checkin_reply(result)
    finally:
        await engine.dispose()
        affinity.reset_ledger_state()


@pytest.mark.asyncio
async def test_overview_marks_today_and_broken_streak(factory) -> None:
    await _checkin(factory, G1, DAY - timedelta(days=3))
    await _checkin(factory, G1, DAY - timedelta(days=2))
    await _checkin(factory, G2, DAY)
    async with factory() as session:
        statuses = await service.checkin_overview(session, USER, now=DAY)
    by_group = {item.group_id: item for item in statuses}
    assert by_group[G2].checked_today and by_group[G2].streak_days == 1
    assert not by_group[G1].checked_today
    assert by_group[G1].streak_days == 0 and by_group[G1].streak_broken
    # 今天没签的排前面
    assert statuses[0].group_id == G1
    assert statuses[0].public_dict()["group_name"] == f"群 {G1}"


@pytest.mark.asyncio
async def test_public_dict_hides_affinity_numbers(factory) -> None:
    result = await _checkin(factory, G1, DAY)
    payload = result.public_dict()
    assert payload["affinity_up"] is True
    assert "affinity" not in payload and "affinity_after" not in payload


def test_batch_reply_formats() -> None:
    assert "没有找到" in service.format_batch_reply([])
    ok = service.CheckinResult(
        status="ok", group_id=1, user_id=2, day=DAY.date(), reward=5, streak_days=3
    )
    done = service.CheckinResult(
        status="already", group_id=3, user_id=2, day=DAY.date(), streak_days=9
    )
    text = service.format_batch_reply([("甲群", ok), ("乙群", done)])
    assert "✅ 甲群：+5 积分" in text and "☑️ 乙群" in text


def test_status_reply_lists_groups() -> None:
    snap = affinity.AffinitySnapshot(USER, 25, affinity.level_for(25), {}, True)
    status = service.GroupCheckinStatus(
        G1, "测试群", True, 4, False, 10, 88, 100, 3, DAY.date()
    )
    text = checkin_cmd.format_status_reply([status], snap)
    assert "面熟" in text and "测试群" in text and "连签 4 天" in text
    assert "还没有" in checkin_cmd.format_status_reply([], snap)


def test_command_catalog_lists_checkin_for_private() -> None:
    specs = {spec.name: spec for spec in checkin_cmd.COMMAND_GROUP.commands}
    assert specs["签到"].scope == "all"
    assert "签到状态" in specs


def test_affinity_migration_chains_after_scheduled_tasks() -> None:
    migration = importlib.import_module(
        "data.nonebot_plugin_orm.migrations.yawn_core.e3a5c7e9f1b4_affinity_events"
    )
    assert migration.revision == "e3a5c7e9f1b4"
    assert migration.down_revision == "d7f9b1c3e5a7"
    columns = {column.name for column in AffinityEvent.__table__.columns}
    assert columns == {
        "id",
        "user_id",
        "group_id",
        "source",
        "delta",
        "balance_after",
        "note",
        "day",
        "created_at",
    }
