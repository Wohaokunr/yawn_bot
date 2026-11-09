# ruff: noqa: ANN001, ANN201, DTZ001, F811, PLR2004
"""好感玩法：等级权益策略、额度加成、礼物/商店/道具、每日任务、升级礼包、衰减、
久别问候、互动/称呼、签到加成、Agent 工具与 WebUI 接口。"""

from __future__ import annotations

import asyncio
import importlib
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import nonebot
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_webui_user_accounts import (  # noqa: F401 - pytest fixture
    API,
    G_MEMBER,
    G_OWNED,
    GOOD_PASSWORD,
    OWNER,
    _isolated,
    _login,
    accounts,
    env,
)

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
affinity = importlib.import_module(f"{_PKG}.affinity")
policy_mod = importlib.import_module(f"{_PKG}.affinity_policy")
store = importlib.import_module(f"{_PKG}.affinity_store")
perks = importlib.import_module(f"{_PKG}.affinity_perks")
economy = importlib.import_module(f"{_PKG}.affinity_economy")
commands = importlib.import_module(f"{_PKG}.affinity_commands")
service = importlib.import_module(f"{_PKG}.checkin_service")
tool_affinity = importlib.import_module(f"{_PKG}.yawn_agent.tool_handlers.affinity")
tool_args = importlib.import_module(f"{_PKG}.yawn_agent.tool_args")
_models = f"{_PKG}.data_models"
AffinityEvent = importlib.import_module(f"{_models}.affinity_event").AffinityEvent
BotUser = importlib.import_module(f"{_models}.bot_user").BotUser
BotGroup = importlib.import_module(f"{_models}.bot_group").BotGroup
UserGroup = importlib.import_module(f"{_models}.user_group").UserGroup
CheckinUser = importlib.import_module(f"{_models}.checkin_user").CheckinUser
CheckinRecord = importlib.import_module(f"{_models}.checkin_record").CheckinRecord

AffinityPolicy = policy_mod.AffinityPolicy
DAY = datetime(2026, 11, 9, 12, 0)
USER = 4242
G1, G2 = 7001, 7002


async def _engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        BotUser.__table__,
        BotGroup.__table__,
        UserGroup.__table__,
        CheckinUser.__table__,
        CheckinRecord.__table__,
        AffinityEvent.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync: BotUser.metadata.create_all(sync, tables=tables)
        )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed(
    maker, *, points: int = 0, wallet: tuple[int, int] = (500, 0), uid: int = USER
) -> None:
    async with maker() as session:
        session.add_all(
            [
                BotGroup(
                    group_id=G1,
                    group_name="猫猫群",
                    first_seen_at=DAY,
                    last_active_at=DAY,
                ),
                BotGroup(
                    group_id=G2,
                    group_name="摸鱼群",
                    first_seen_at=DAY,
                    last_active_at=DAY,
                ),
            ]
        )
        session.add(
            BotUser(
                user_id=uid, nickname="小明", first_interaction_at=DAY, affinity=points
            )
        )
        await session.flush()
        for gid, pts in zip((G1, G2), wallet, strict=True):
            session.add(
                UserGroup(group_id=gid, user_id=uid, role="member", first_seen_at=DAY)
            )
            await session.flush()
            session.add(
                CheckinUser(
                    group_id=gid,
                    user_id=uid,
                    points=pts,
                    total_days=3,
                    streak_days=3,
                    last_checkin_date=DAY.date() - timedelta(days=1),
                )
            )
        await session.commit()


@pytest_asyncio.fixture
async def maker():
    affinity.reset_ledger_state()
    engine, factory = await _engine()
    yield factory
    await engine.dispose()
    affinity.reset_ledger_state()


async def _points(maker, uid: int = USER) -> int:
    async with maker() as session:
        user = await session.get(BotUser, uid)
        return int(user.affinity)


async def _wallet(maker, uid: int = USER) -> int:
    async with maker() as session:
        return (await economy.wallet(session, uid)).total


# ── 1. 策略 ─────────────────────────────────────────────────────────


def test_default_policy_has_six_levels_with_growing_perks():
    pol = AffinityPolicy()
    assert [row.level for row in pol.levels] == [name for _, name in affinity.LEVELS]
    for field in perks.BONUS_FIELDS:
        values = [int(getattr(row, field)) for row in pol.levels]
        assert values == sorted(values), field
    assert [row.gift_daily for row in pol.levels] == sorted(
        row.gift_daily for row in pol.levels
    )
    assert pol.levels[0].unlocks == ["interact.highfive"]
    assert set(pol.levels[-1].unlocks) == set(policy_mod.UNLOCKS)
    assert all(row.tone for row in pol.levels)


def test_policy_completes_and_sorts_levels_and_ignores_unknown():
    pol = AffinityPolicy.model_validate(
        {"levels": [{"level": "朋友", "gift_daily": 1}, {"level": "不存在"}], "junk": 1}
    )
    assert [row.level for row in pol.levels] == [name for _, name in affinity.LEVELS]
    assert pol.perks("朋友").gift_daily == 1
    assert pol.perks("陌生").gift_daily == AffinityPolicy().perks("陌生").gift_daily


def test_policy_rejects_out_of_range_values():
    with pytest.raises(ValueError):
        AffinityPolicy.model_validate(
            {"levels": [{"level": "陌生", "memory_bonus": 99}]}
        )
    with pytest.raises(ValueError):
        AffinityPolicy.model_validate({"favorite_multiplier": 9})


def test_policy_save_load_reset_round_trip():
    pol = policy_mod.load_policy()
    assert pol.enabled and not (policy_mod.data_root() / "policy.json").exists()
    changed = pol.model_copy(update={"favorite_multiplier": 3})
    policy_mod.save_policy(changed)
    assert policy_mod.load_policy().favorite_multiplier == 3
    policy_mod.save_policy({"enabled": False})
    assert policy_mod.load_policy().enabled is False
    assert policy_mod.reset_policy().enabled is True
    assert policy_mod.load_policy().favorite_multiplier == 2


def test_unlock_level_and_item_lookup():
    pol = AffinityPolicy()
    assert pol.unlock_level("interact.highfive") == "陌生"
    assert pol.unlock_level("nickname") == "熟人"
    assert pol.unlock_level("care") == "朋友"
    assert pol.item("补签卡").key == "repair_card"
    assert pol.item("shield").name == "好感护盾"
    assert pol.gift("奶茶").key == "milk_tea"
    assert pol.gift("🍰") is None or pol.gift("🍰").key == "cake"


# ── 2. 额度加成 ─────────────────────────────────────────────────────


def test_apply_bonus_rules():
    pol = AffinityPolicy()
    friend = pol.perks("朋友")
    assert perks.apply_bonus(200, friend, "private_turns_pct") == 300
    assert perks.apply_bonus(1, friend, "private_turns_pct") == 2  # 百分比至少 +1
    assert perks.apply_bonus(10, friend, "web_search_bonus") == 18
    assert perks.apply_bonus(0, friend, "web_search_bonus") == 0  # 关掉的额度不加
    assert perks.apply_bonus(-1, friend, "sandbox_bonus") == -1
    assert perks.apply_bonus(10, pol.perks("陌生"), "web_search_bonus") == 10


def test_bonus_limit_uses_cache_and_policy_switch():
    assert perks.bonus_limit(10, USER, "web_search_bonus") == 10  # 没缓存按陌生
    perks.remember_points(USER, 160)
    assert perks.bonus_limit(10, USER, "web_search_bonus") == 18
    assert perks.bonus_limit(10, USER, "web_search_bonus", points=0) == 10
    assert perks.bonus_limit(10, None, "web_search_bonus") == 10
    assert "朋友" in perks.bonus_note(10, 18, USER)
    assert perks.bonus_note(10, 10, USER) == ""
    policy_mod.save_policy({"enabled": False})
    assert perks.bonus_limit(10, USER, "web_search_bonus") == 10


def test_points_cache_expires_and_clears():
    perks.remember_points(USER, 70)
    assert perks.cached_points(USER) == 70
    perks.clear_points_cache()
    assert perks.cached_points(USER) is None
    perks.remember_points(None, 5)
    assert perks.cached_points(None) is None


def test_perks_public_and_next_unlocks_hide_balance():
    pol = AffinityPolicy()
    public = perks.perks_public(pol.perks("熟人"))
    assert {b["key"] for b in public["bonuses"]} >= {
        "private_turns_pct",
        "memory_bonus",
    }
    assert any(u["key"] == "nickname" for u in public["unlocks"])
    upcoming = perks.next_unlocks(70, pol)
    assert upcoming["level"] == "朋友" and upcoming["to_next"] == 80
    assert any(u["key"] == "care" for u in upcoming["unlocks"])
    assert all(row["delta"] > 0 for row in upcoming["better"])
    assert perks.next_unlocks(10_000, pol) is None
    assert "专属称呼" in perks.unlock_summary(pol.perks("熟人"))


def test_dynamic_daily_caps_follow_policy():
    assert perks.dynamic_daily_cap("gift") == 10
    assert perks.dynamic_daily_cap("interact") == 3
    assert perks.dynamic_daily_cap("quest") == 6
    assert perks.dynamic_daily_cap("checkin") is False
    policy_mod.save_policy({"gift_affinity_daily_cap": 4})
    assert perks.dynamic_daily_cap("gift") == 4


def test_has_unlock_by_level():
    assert perks.has_unlock(0, "interact.highfive")
    assert not perks.has_unlock(0, "interact.pat")
    assert perks.has_unlock(20, "interact.pat")
    assert not perks.has_unlock(59, "nickname")
    assert perks.has_unlock(60, "nickname")


# ── 3. 个人存储 ─────────────────────────────────────────────────────


def test_store_round_trip_items_and_limits():
    assert not store.exists(USER)
    record = store.load(USER)
    assert record["inventory"] == {} and record["nickname"] == ""
    added = store.add_items(
        record, {"shield": 5, "lucky_card": 1, "bad": 0}, cap={"shield": 3}
    )
    assert added == {"shield": 3, "lucky_card": 1}
    assert store.take_item(record, "lucky_card") and not store.take_item(
        record, "lucky_card"
    )
    for i in range(40):
        store.push_limited(record, "levels", {"i": i}, store.HISTORY_LIMIT)
    assert (
        len(record["levels"]) == store.HISTORY_LIMIT and record["levels"][-1]["i"] == 39
    )
    store.save(USER, record)
    assert store.exists(USER) and USER in store.known_user_ids()
    assert store.load(USER)["inventory"] == {"shield": 3}
    assert store.delete(USER) and not store.exists(USER)


def test_store_tolerates_corrupt_file():
    path = store.users_dir() / f"{USER}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert store.load(USER)["inventory"] == {}


# ── 4. 每日任务 ─────────────────────────────────────────────────────


def test_quests_are_stable_per_day_and_never_two_checkin_kinds():
    first = perks.quests_view(store.empty_record(), USER, DAY)
    again = perks.quests_view(store.empty_record(), USER, DAY)
    assert [q["key"] for q in first["items"]] == [q["key"] for q in again["items"]]
    assert len(first["items"]) == 3
    for offset in range(30):
        keys = [
            q["key"]
            for q in perks.quests_view(store.empty_record(), USER + offset, DAY)[
                "items"
            ]
        ]
        assert len(keys) == len(set(keys))
        assert sum(1 for k in keys if k.startswith("checkin")) <= 1


def test_refresh_keeps_done_quests():
    record = store.empty_record()
    quests = perks.ensure_quests(record, USER, DAY)
    quests["items"][0]["done"] = True
    keep = quests["items"][0]["key"]
    fresh = perks.refresh_quests(record, USER, DAY)
    assert record["quests"]["items"][0]["key"] == keep
    assert len(record["quests"]["items"]) == 3 and len(fresh) == 2


@pytest.mark.asyncio
async def test_grant_advances_quests_and_settle_pays_once(maker):
    await _seed(maker, points=0)

    def force(rec):
        rec["quests"] = {
            "day": DAY.date().isoformat(),
            "items": [
                {"key": "group_chat", "progress": 0, "done": False, "claimed": False}
            ],
            "bonus_claimed": False,
            "refreshed": 0,
        }

    store.update(USER, force)
    async with maker() as session:
        await affinity.grant(session, USER, "group_chat", 1, now=DAY)
        await affinity.grant(session, USER, "group_chat", 1, now=DAY)
        settled = await perks.settle_quests(session, USER, now=DAY)
        again = await perks.settle_quests(session, USER, now=DAY)
        await session.commit()
    assert settled["claimed"] == ["在群里找我聊 2 次"] and settled["affinity"] == 2
    assert settled["bonus_item"] == "签到暴击卡"
    assert again["claimed"] == [] and again["bonus_item"] is None
    assert store.load(USER)["inventory"]["lucky_card"] == 1
    assert "签到暴击卡" in perks.quest_reply(settled)
    assert perks.quest_reply(again) == ""


@pytest.mark.asyncio
async def test_settle_quests_disabled_by_policy(maker):
    await _seed(maker)
    policy_mod.save_policy({"quests": {"enabled": False}})
    async with maker() as session:
        assert (await perks.settle_quests(session, USER, now=DAY))["claimed"] == []


# ── 5. 升级礼包与刚升级提示 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_levelup_records_history_gives_items_once_and_announces_once(maker):
    await _seed(maker, points=15)
    async with maker() as session:
        change = await affinity.grant(session, USER, "manual", 10, now=DAY)
        await session.commit()
    assert change.leveled_up and change.level_after.name == "面熟"
    record = store.load(USER)
    assert record["inventory"] == {"repair_card": 1}
    assert (
        record["levels"][-1]["level"] == "面熟" and record["levels"][-1]["up"] is True
    )
    view = perks.speaker_view(USER, 25, DAY, channel="group")
    assert "面熟" in view["just_leveled_up"] and "数值" in view["just_leveled_up"]
    assert "just_leveled_up" not in perks.speaker_view(USER, 25, DAY, channel="group")
    assert "just_leveled_up" in perks.speaker_view(USER, 25, DAY, channel="private")
    # 掉回去再升回来不重复送
    async with maker() as session:
        await affinity.grant(session, USER, "manual", -10, now=DAY)
        await affinity.grant(session, USER, "manual", 10, now=DAY)
        await session.commit()
    assert store.load(USER)["inventory"] == {"repair_card": 1}


@pytest.mark.asyncio
async def test_skipping_levels_gives_each_level_pack(maker):
    await _seed(maker, points=0)
    async with maker() as session:
        await affinity.grant(session, USER, "manual", 160, now=DAY)
        await session.commit()
    inv = store.load(USER)["inventory"]
    # 面熟 补签卡 1 + 熟人 护盾 1 暴击 1 + 朋友 补签 1 暴击 2
    assert inv == {"repair_card": 2, "shield": 1, "lucky_card": 3}


def test_levelup_is_stale_after_three_days():
    def put(rec):
        rec["pending_levelup"] = {
            "level": "朋友",
            "at": (DAY - timedelta(days=4)).isoformat(),
            "group": False,
            "private": False,
        }

    store.update(USER, put)
    assert "just_leveled_up" not in perks.speaker_view(USER, 160, DAY, channel="group")


def test_speaker_view_words_only_and_nickname_requires_unlock():
    store.update(USER, lambda rec: rec.__setitem__("nickname", "阿明"))
    low = perks.speaker_view(USER, 10, DAY, channel="group")
    assert "call_them" not in low and "what_you_can_do" not in low
    high = perks.speaker_view(USER, 160, DAY, channel="group")
    assert high["call_them"] == "阿明"
    assert high["what_you_can_do"]
    assert not any(ch.isdigit() for value in high.values() for ch in str(value))
    policy_mod.save_policy({"enabled": False})
    assert perks.speaker_view(USER, 160, DAY, channel="group") == {}


# ── 6. 衰减 ─────────────────────────────────────────────────────────


def test_decay_amount_rules():
    pol = AffinityPolicy()
    today = DAY.date()
    rec = store.empty_record()
    assert perks.decay_amount(100, today - timedelta(days=13), today, rec, pol) == 0
    assert perks.decay_amount(100, today - timedelta(days=14), today, rec, pol) == 1
    assert perks.decay_amount(100, today - timedelta(days=20), today, rec, pol) == 7
    # 保级：熟人（60）只掉到 60
    assert perks.decay_amount(62, today - timedelta(days=40), today, rec, pol) == 2
    assert perks.decay_amount(100, None, today, rec, pol) == 0
    rec["shield_until"] = today.isoformat()
    assert perks.decay_amount(100, today - timedelta(days=40), today, rec, pol) == 0
    rec = {**store.empty_record(), "decay_day": (today - timedelta(days=1)).isoformat()}
    assert perks.decay_amount(100, today - timedelta(days=40), today, rec, pol) == 1
    no_keep = AffinityPolicy.model_validate({"decay": {"keep_level": False}})
    assert (
        perks.decay_amount(
            62, today - timedelta(days=40), today, store.empty_record(), no_keep
        )
        == 27
    )


@pytest.mark.asyncio
async def test_run_decay_only_touches_idle_users(maker):
    await _seed(maker, points=0)
    async with maker() as session:
        session.add(
            BotUser(
                user_id=USER + 1, nickname="活跃", first_interaction_at=DAY, affinity=0
            )
        )
        await session.flush()
        old = DAY - timedelta(days=20)
        await affinity.grant(session, USER, "manual", 0, now=old)
        session.add(
            AffinityEvent(
                user_id=USER,
                source="checkin",
                delta=100,
                balance_after=100,
                note="",
                day=old.date(),
                created_at=old,
            )
        )
        (await session.get(BotUser, USER)).affinity = 100
        session.add(
            AffinityEvent(
                user_id=USER + 1,
                source="checkin",
                delta=100,
                balance_after=100,
                note="",
                day=DAY.date(),
                created_at=DAY,
            )
        )
        (await session.get(BotUser, USER + 1)).affinity = 100
        await session.commit()
    async with maker() as session:
        result = await perks.run_decay(session, now=DAY)
        again = await perks.run_decay(session, now=DAY)
        await session.commit()
    assert result == {USER: 7} and again == {}
    assert await _points(maker) == 93 and await _points(maker, USER + 1) == 100
    assert store.load(USER)["decay_day"] == DAY.date().isoformat()


@pytest.mark.asyncio
async def test_decay_disabled_or_shielded(maker):
    await _seed(maker, points=100)
    async with maker() as session:
        old = DAY - timedelta(days=30)
        session.add(
            AffinityEvent(
                user_id=USER,
                source="checkin",
                delta=100,
                balance_after=100,
                note="",
                day=old.date(),
                created_at=old,
            )
        )
        await session.commit()
    store.update(
        USER,
        lambda rec: rec.__setitem__(
            "shield_until", (DAY.date() + timedelta(days=1)).isoformat()
        ),
    )
    async with maker() as session:
        assert await perks.run_decay(session, now=DAY) == {}
    store.update(USER, lambda rec: rec.__setitem__("shield_until", ""))
    policy_mod.save_policy({"decay": {"enabled": False}})
    async with maker() as session:
        assert await perks.run_decay(session, now=DAY) == {}


# ── 7. 久别问候 ─────────────────────────────────────────────────────


def test_care_message_window_and_once_per_absence():
    rec = {
        **store.empty_record(),
        "last_private": (DAY - timedelta(days=5)).isoformat(),
    }
    assert perks.care_message(USER, dict(rec), 70, DAY) is None  # 熟人没解锁
    first = perks.care_message(USER, rec, 160, DAY, name="小明")
    assert first and "小明" in first
    assert perks.care_message(USER, rec, 160, DAY) is None  # 同一次久别只问一次
    late = {
        **store.empty_record(),
        "last_private": (DAY - timedelta(days=5)).isoformat(),
    }
    assert perks.care_message(USER, late, 160, DAY.replace(hour=23)) is None
    gone = {
        **store.empty_record(),
        "last_private": (DAY - timedelta(days=60)).isoformat(),
    }
    assert perks.care_message(USER, gone, 160, DAY) is None
    fresh = {
        **store.empty_record(),
        "last_private": (DAY - timedelta(days=1)).isoformat(),
    }
    assert perks.care_message(USER, fresh, 160, DAY) is None


def test_care_uses_nickname():
    rec = {
        **store.empty_record(),
        "last_private": (DAY - timedelta(days=5)).isoformat(),
        "nickname": "阿明",
    }
    assert "阿明" in perks.care_message(USER, rec, 160, DAY, name="小明")


@pytest.mark.asyncio
async def test_private_chat_grant_marks_last_private(maker):
    await _seed(maker)
    async with maker() as session:
        await affinity.grant(session, USER, "private_chat", 1, now=DAY)
        await session.commit()
    assert store.load(USER)["last_private"].startswith(DAY.date().isoformat())


# ── 8. 礼物 ─────────────────────────────────────────────────────────


def test_favorite_gift_is_stable_per_day():
    first = economy.favorite_gift(DAY)
    assert first is not None and economy.favorite_gift(DAY).key == first.key
    keys = {economy.favorite_gift(DAY + timedelta(days=i)).key for i in range(30)}
    assert len(keys) > 1


def test_price_after_discount():
    assert economy.price_after_discount(100, 0) == 100
    assert economy.price_after_discount(100, 15) == 85
    assert economy.price_after_discount(1, 50) >= 1


@pytest.mark.asyncio
async def test_send_gift_spends_points_and_grants_affinity(maker, monkeypatch):
    await _seed(maker, points=0, wallet=(20, 15))
    monkeypatch.setattr(economy, "favorite_gift", lambda *_a, **_k: None)
    async with maker() as session:
        result = await economy.send_gift(session, USER, "奶茶", 1, group_id=G1, now=DAY)
        await session.commit()
    assert result.cost == 30 and result.affinity_gain == 2 and not result.favorite
    assert await _wallet(maker) == 5
    assert await _points(maker) == 2
    record = store.load(USER)
    assert record["gift_totals"] == {"milk_tea": 1} and record["gifts_today"][
        "names"
    ] == ["奶茶"]
    public = result.public_dict()
    assert public["affinity_up"] is True and public["points_spent"] == 30
    assert not any(key.startswith("affinity_gain") or key == "points" for key in public)
    assert perks.speaker_view(USER, 2, DAY, channel="group")["gifts_today"].endswith(
        "奶茶"
    )


@pytest.mark.asyncio
async def test_favorite_gift_doubles_and_cap_limits(maker):
    await _seed(maker, points=0, wallet=(5000, 0))
    fav = economy.favorite_gift(DAY)
    async with maker() as session:
        result = await economy.send_gift(session, USER, fav.name, 1, now=DAY)
        await session.commit()
    assert result.favorite and result.affinity_gain == min(fav.affinity * 2, 10)
    async with maker() as session:
        await economy.send_gift(session, USER, "星星灯", 1, now=DAY)
        await session.commit()
    assert await _points(maker) == 10  # 礼物每天最多 +10


@pytest.mark.asyncio
async def test_gift_errors(maker):
    await _seed(maker, points=0, wallet=(5, 0))
    async with maker() as session:
        with pytest.raises(economy.EconomyError) as missing:
            await economy.send_gift(session, USER, "火箭", now=DAY)
        assert missing.value.code == "not_found" and "糖果" in str(missing.value)
        with pytest.raises(economy.EconomyError) as poor:
            await economy.send_gift(session, USER, "奶茶", now=DAY)
        assert poor.value.code == "points"
        with pytest.raises(economy.EconomyError) as bad_count:
            await economy.send_gift(session, USER, "糖果", 99, now=DAY)
        assert bad_count.value.code == "invalid"
    assert await _wallet(maker) == 5


@pytest.mark.asyncio
async def test_gift_daily_limit_by_level(maker):
    await _seed(maker, points=0, wallet=(5000, 0))
    async with maker() as session:
        await economy.send_gift(session, USER, "糖果", 3, now=DAY)
        with pytest.raises(economy.EconomyError) as quota:
            await economy.send_gift(session, USER, "糖果", 1, now=DAY)
        assert quota.value.code == "quota" and "陌生" in str(quota.value)


@pytest.mark.asyncio
async def test_spend_prefers_largest_balance_and_specific_group(maker):
    await _seed(maker, wallet=(10, 50))
    async with maker() as session:
        taken = await economy.spend(session, USER, 55)
        await session.commit()
    assert taken == [(G2, 50), (G1, 5)]
    async with maker() as session:
        with pytest.raises(economy.EconomyError):
            await economy.spend(session, USER, 6, group_id=G1)


# ── 9. 商店与道具 ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_buy_item_level_gate_hold_cap_and_discount(maker):
    await _seed(maker, points=0, wallet=(1000, 0))
    async with maker() as session:
        with pytest.raises(economy.EconomyError) as locked:
            await economy.buy_item(session, USER, "好感护盾", now=DAY)
        assert locked.value.code == "level"
        bought = await economy.buy_item(session, USER, "补签卡", 3, now=DAY)
        assert bought["have"] == 3 and bought["points_spent"] == 240
        with pytest.raises(economy.EconomyError) as full:
            await economy.buy_item(session, USER, "补签卡", 1, now=DAY)
        assert full.value.code == "quota"
        await session.commit()
    await _seed_points(maker, 600)
    async with maker() as session:
        bought = await economy.buy_item(session, USER, "签到暴击卡", 1, now=DAY)
    assert bought["discount_pct"] == 20 and bought["points_spent"] == 32


async def _seed_points(maker, points: int) -> None:
    async with maker() as session:
        (await session.get(BotUser, USER)).affinity = points
        await session.commit()
    perks.clear_points_cache()


def test_shop_view_marks_locked_and_owned():
    rec = {**store.empty_record(), "inventory": {"shield": 1}}
    rows = {row["key"]: row for row in economy.shop_view(0, rec)}
    assert rows["shield"]["unlocked"] is False and rows["shield"]["have"] == 1
    assert rows["repair_card"]["unlocked"] is True
    assert rows["repair_card"]["final_price"] == rows["repair_card"]["price"]


@pytest.mark.asyncio
async def test_repair_card_restores_recent_streak(maker):
    await _seed(maker)
    async with maker() as session:
        summary = await session.get(CheckinUser, (G1, USER))
        summary.last_checkin_date = DAY.date() - timedelta(days=4)
        summary.streak_days = 9
        await session.commit()
    store.update(USER, lambda rec: store.add_items(rec, {"repair_card": 1}))
    async with maker() as session:
        result = await economy.use_item(session, USER, "补签卡", now=DAY)
        await session.commit()
    assert (
        result["days_restored"] == 3
        and result["streak_days"] == 12
        and result["group_id"] == G1
    )
    async with maker() as session:
        summary = await session.get(CheckinUser, (G1, USER))
        assert summary.last_checkin_date == DAY.date() - timedelta(days=1)
    assert store.load(USER)["inventory"] == {}


@pytest.mark.asyncio
async def test_repair_card_not_applicable_keeps_card(maker):
    await _seed(maker)
    store.update(USER, lambda rec: store.add_items(rec, {"repair_card": 1}))
    async with maker() as session:
        with pytest.raises(economy.EconomyError) as err:
            await economy.use_item(session, USER, "补签卡", now=DAY)
    assert err.value.code == "not_applicable"
    assert store.load(USER)["inventory"] == {"repair_card": 1}


@pytest.mark.asyncio
async def test_shield_stacks_and_lucky_once_per_day(maker):
    await _seed(maker)
    store.update(USER, lambda rec: store.add_items(rec, {"shield": 2, "lucky_card": 2}))
    async with maker() as session:
        first = await economy.use_item(session, USER, "好感护盾", now=DAY)
        second = await economy.use_item(session, USER, "shield", now=DAY)
        lucky = await economy.use_item(session, USER, "签到暴击卡", now=DAY)
        with pytest.raises(economy.EconomyError):
            await economy.use_item(session, USER, "签到暴击卡", now=DAY)
    assert first["shield_until"] == (DAY.date() + timedelta(days=14)).isoformat()
    assert second["shield_until"] == (DAY.date() + timedelta(days=28)).isoformat()
    assert lucky["lucky_day"] == DAY.date().isoformat()
    inv = economy.inventory_view(store.load(USER), today=DAY.date())
    assert (
        inv["lucky_today"]
        and inv["shield_until"]
        and inv["items"]
        == [
            {
                "key": "lucky_card",
                "name": "签到暴击卡",
                "count": 1,
                "description": inv["items"][0]["description"],
            }
        ]
    )


@pytest.mark.asyncio
async def test_quest_refresh_and_empty_bag(maker):
    await _seed(maker)
    async with maker() as session:
        with pytest.raises(economy.EconomyError) as empty:
            await economy.use_item(session, USER, "任务刷新券", now=DAY)
        assert empty.value.code == "empty"
    store.update(USER, lambda rec: store.add_items(rec, {"quest_refresh": 1}))
    async with maker() as session:
        result = await economy.use_item(session, USER, "任务刷新券", now=DAY)
    assert len(result["new_quests"]) == 3 and result["left"] == 0


@pytest.mark.asyncio
async def test_economy_disabled(maker):
    await _seed(maker)
    policy_mod.save_policy({"enabled": False})
    async with maker() as session:
        with pytest.raises(economy.EconomyError) as off:
            await economy.buy_item(session, USER, "补签卡", now=DAY)
    assert off.value.code == "disabled"


# ── 10. 签到加成 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_checkin_bonus_by_level_and_lucky_card(maker):
    await _seed(maker, points=160)
    async with maker() as session:
        bonus = await economy.checkin_bonus(session, USER, 10, now=DAY)
        assert (bonus.level_points, bonus.lucky_points, bonus.level) == (2, 0, "朋友")
        store.update(
            USER, lambda rec: rec.__setitem__("lucky_day", DAY.date().isoformat())
        )
        lucky = await economy.checkin_bonus(session, USER, 10, now=DAY)
        assert lucky.lucky_points == 10 and lucky.total == 12
        assert (
            await economy.checkin_bonus(session, USER, 10, now=DAY)
        ).lucky_points == 0
        assert (await economy.checkin_bonus(session, USER, 0, now=DAY)).total == 0


@pytest.mark.asyncio
async def test_perform_checkin_adds_bonus_points(maker):
    await _seed(maker, points=160, wallet=(0, 0))
    async with maker() as session:
        summary = await session.get(CheckinUser, (G1, USER))
        summary.last_checkin_date = None
        await session.commit()
    async with maker() as session:
        result = await service.perform_checkin(
            session,
            group_id=G1,
            user_id=USER,
            nickname="小明",
            now=DAY,
            points_reward=10,
        )
        await session.commit()
    assert result.level_bonus_points == 2
    assert "好感「朋友」签到加成 +2 积分" in service.format_checkin_reply(result)
    async with maker() as session:
        summary = await session.get(CheckinUser, (G1, USER))
        assert summary.points == 12


@pytest.mark.asyncio
async def test_strangers_get_no_checkin_bonus(maker):
    await _seed(maker, points=0)
    async with maker() as session:
        assert (await economy.checkin_bonus(session, USER, 10, now=DAY)).total == 0


# ── 11. 称呼与互动 ──────────────────────────────────────────────────


def test_clean_nickname():
    assert commands.clean_nickname("  阿明  ") == "阿明"
    assert commands.clean_nickname("") is None
    assert (
        commands.clean_nickname("x" * 50) is None
        or len(commands.clean_nickname("x" * 50)) <= commands.NICKNAME_MAX
    )
    assert commands.clean_nickname("@全体成员") is None
    assert commands.clean_nickname("[CQ:at,qq=1]") is None


@pytest.mark.asyncio
async def test_set_nickname_needs_level_and_can_clear(maker):
    await _seed(maker, points=0)
    async with maker() as session:
        ok, text = await commands.set_nickname(session, USER, "阿明")
        assert not ok and "熟人" in text
    await _seed_points(maker, 70)
    async with maker() as session:
        ok, text = await commands.set_nickname(session, USER, "阿明")
        assert ok and "阿明" in text
        assert store.load(USER)["nickname"] == "阿明"
        ok, _text = await commands.set_nickname(session, USER, "清除")
    assert ok and store.load(USER)["nickname"] == ""


@pytest.mark.asyncio
async def test_interact_refuses_then_accepts_with_daily_cap(maker):
    await _seed(maker, points=0)
    async with maker() as session:
        ok, text, _ = await commands.do_interact(
            session, USER, "抱抱", group_id=None, name="小明", now=DAY
        )
        assert not ok and "熟人" in text
        ok, _text, payload = await commands.do_interact(
            session, USER, "击掌", group_id=None, name="小明", now=DAY
        )
        assert ok and payload["affinity_up"]
        for _ in range(4):
            await commands.do_interact(
                session, USER, "击掌", group_id=None, name="小明", now=DAY
            )
        ok, _text, _ = await commands.do_interact(
            session, USER, "跳舞", group_id=None, name="小明", now=DAY
        )
        assert not ok
        await session.commit()
    assert await _points(maker) == 3  # 互动每天最多 +3
    record = store.load(USER)
    assert (
        record["interact_today"]["count"] == 5
        and record["interact_totals"]["interact.highfive"] == 5
    )


def test_interact_reply_and_refusal_text():
    assert perks.interact_reply("interact.cuddle", USER, DAY, name="阿明")
    assert "熟人" in perks.interact_refusal("interact.hug", 0)
    off = AffinityPolicy.model_validate(
        {"levels": [{"level": name, "unlocks": []} for _, name in affinity.LEVELS]}
    )
    assert "没有开放" in perks.interact_refusal("interact.hug", 0, off)


# ── 12. 命令文本 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_format_overview_has_no_raw_balance_leak_beyond_progress(maker):
    await _seed(maker, points=70, wallet=(123, 0))
    async with maker() as session:
        snapshot = await affinity.snapshot(session, USER, now=DAY)
        wallet = await economy.wallet(session, USER)
    text = commands.format_overview(snapshot, store.load(USER), wallet, DAY)
    assert (
        "熟人" in text
        and "签到积分 123" in text
        and "今日任务" in text
        and "朋友" in text
    )


def test_format_tables():
    assert (
        "陌生" in commands.format_perk_table()
        and "挚友" in commands.format_perk_table()
    )
    assert "糖果" in commands.format_gift_menu(store.empty_record(), 0, DAY)
    assert "补签卡" in commands.format_shop(store.empty_record(), 0)
    view = perks.quests_view(store.empty_record(), USER, DAY)
    assert view["items"][0]["title"] in commands.format_quests(view)
    assert "护盾" in commands.format_use_result(
        {"item": "好感护盾", "key": "shield", "shield_until": "2026-11-23", "left": 0}
    )


def test_commands_registered_in_catalog():
    text = str(commands.COMMAND_GROUP.commands)
    for word in (
        "好感",
        "好感权益",
        "送礼",
        "好感商店",
        "兑换",
        "背包",
        "使用道具",
        "好感任务",
        "叫我",
        "击掌",
    ):
        assert word in text, word
    assert commands.DECAY_JOB_ID == "yawn_affinity_decay"


# ── 13. 群聊工具 / 私聊工具 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_overview_never_reports_affinity_number(maker):
    await _seed(maker, points=70, wallet=(321, 0))
    async with maker() as session:
        payload = await tool_affinity.overview_payload(
            session, USER, "overview", now=DAY
        )
        perks_view = await tool_affinity.overview_payload(
            session, USER, "perks", now=DAY
        )
        gifts = await tool_affinity.overview_payload(session, USER, "gifts", now=DAY)
        shop = await tool_affinity.overview_payload(session, USER, "shop", now=DAY)
    assert payload["level"] == "熟人" and payload["next_level"] == "朋友"
    assert "points" not in payload and "affinity" not in payload
    assert "to_next" not in payload["next_level_unlocks"]
    assert len(perks_view["all_levels"]) == 6
    assert gifts["gifts"]["menu"] and payload["gifts"]["menu"] is None
    assert shop["checkin_points"] == 321 and shop["shop"]
    assert "70" not in str(payload)


@pytest.mark.asyncio
async def test_tool_actions_and_errors(maker):
    await _seed(maker, points=70, wallet=(500, 0))
    async with maker() as session:
        bought = await tool_affinity.action_payload(
            session,
            USER,
            {"action": "buy", "item": "签到暴击卡"},
            group_id=G1,
            name="",
            now=DAY,
        )
        assert bought["have"] == 1
        used = await tool_affinity.action_payload(
            session,
            USER,
            {"action": "use", "item": "签到暴击卡"},
            group_id=G1,
            name="",
            now=DAY,
        )
        assert used["lucky_day"]
        nick = await tool_affinity.action_payload(
            session,
            USER,
            {"action": "nickname", "nickname": "阿明"},
            group_id=G1,
            name="",
            now=DAY,
        )
        assert nick["done"]
        hug = await tool_affinity.action_payload(
            session,
            USER,
            {"action": "interact", "item": "抱抱"},
            group_id=G1,
            name="小明",
            now=DAY,
        )
        assert hug["done"] and hug["suggested_reply"]
        lift = await tool_affinity.action_payload(
            session,
            USER,
            {"action": "interact", "item": "举高高"},
            group_id=G1,
            name="小明",
            now=DAY,
        )
        assert lift["refused"]
        gift = await tool_affinity.action_payload(
            session,
            USER,
            {"action": "gift", "item": "糖果"},
            group_id=G1,
            name="",
            now=DAY,
        )
        assert gift["affinity_up"] is True
        for args in (
            {"action": "gift"},
            {"action": "buy"},
            {"action": "use"},
            {"action": "dance"},
        ):
            with pytest.raises(tool_args.ToolError):
                await tool_affinity.action_payload(
                    session, USER, args, group_id=G1, name="", now=DAY
                )
        with pytest.raises(tool_args.ToolError) as poor:
            await tool_affinity.action_payload(
                session,
                USER,
                {"action": "buy", "item": "补签卡", "count": 9},
                group_id=G1,
                name="",
                now=DAY,
            )
        assert poor.value.code == "quota"


def test_tools_are_registered_in_checkin_family():
    registry = importlib.import_module(f"{_PKG}.yawn_agent.tool_registry")
    defs = registry.TOOL_BY_NAME
    assert (
        defs["my_affinity"].family == "checkin"
        and defs["affinity_action"].family == "checkin"
    )
    handlers = importlib.import_module(f"{_PKG}.yawn_agent.tool_handlers")
    assert {"my_affinity", "affinity_action"} <= set(handlers._HANDLERS)
    private_tools = importlib.import_module(f"{_PKG}.yawn_agent.private_tools")
    assert "affinity" in private_tools.AFFINITY_NAMES
    claims = importlib.import_module(f"{_PKG}.yawn_agent.speech_claims")
    assert "affinity_item" in str(vars(claims))


@pytest.mark.asyncio
async def test_offers_locked_for_strangers():
    turn = importlib.import_module(f"{_PKG}.yawn_agent.private_offer_turn")
    perks.remember_points(USER, 0)
    assert await turn._offers_locked(USER) is True
    perks.remember_points(USER, 30)
    assert await turn._offers_locked(USER) is False
    perks.remember_points(USER, 0)
    policy_mod.save_policy({"enabled": False})
    assert await turn._offers_locked(USER) is False


def test_offers_gate_reason_label():
    labels = importlib.import_module(
        f"{_PKG}.yawn_agent.private_offer_policy"
    ).REASON_LABELS
    assert "affinity_level" in labels


def test_private_memory_list_keep_grows_with_level():
    memory = importlib.import_module(f"{_PKG}.yawn_agent.private_memory")
    base = memory.list_keep(USER)
    perks.remember_points(USER, 600)
    assert memory.list_keep(USER) == base + 8


def test_quota_modules_use_bonus():
    web = importlib.import_module(f"{_PKG}.yawn_agent.web_search_policy")
    sandbox = importlib.import_module(f"{_PKG}.yawn_agent.sandbox_policy")
    perks.remember_points(USER, 160)
    assert web.member_daily_limit(10, USER) == 18
    assert sandbox.member_daily_limit(10, USER) == 18
    assert web.member_daily_limit(0, USER) == 0


# ── 14. WebUI 接口 ──────────────────────────────────────────────────


@pytest.fixture
def web(env, monkeypatch):
    routes = importlib.import_module(f"{_PKG}.webui.affinity_routes")
    factory = env["factory"]

    async def seed() -> None:
        async with factory() as session:
            (await session.get(BotUser, OWNER)).affinity = 70
            session.add_all(
                [
                    CheckinUser(
                        group_id=G_OWNED,
                        user_id=OWNER,
                        points=500,
                        total_days=3,
                        streak_days=3,
                    ),
                    CheckinUser(group_id=G_MEMBER, user_id=OWNER, points=0),
                ]
            )
            await session.commit()

    asyncio.run(seed())

    @asynccontextmanager
    async def fake_session():
        async with factory() as session:
            yield session

    monkeypatch.setattr(routes, "get_session", fake_session)
    monkeypatch.setattr(routes, "_now", lambda: DAY)
    perks.clear_points_cache()
    return env


def _admin(web):
    client = web["client"]
    response = client.post(f"{API}/auth/login", json={"token": "x" * 40})
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["data"]["csrfToken"]
    return client


def _member(web):
    accounts.set_password(OWNER, GOOD_PASSWORD, source="test")
    client = web["client"]
    assert _login(client).status_code == 200
    return client


def test_admin_policy_round_trip_and_validation(web):
    client = _admin(web)
    data = client.get(f"{API}/affinity/policy").json()["data"]
    assert len(data["policy"]["levels"]) == 6 and data["meta"]
    policy = data["policy"]
    policy["favorite_multiplier"] = 3
    saved = client.put(f"{API}/affinity/policy", json={"policy": policy})
    assert (
        saved.status_code == 200
        and saved.json()["data"]["policy"]["favorite_multiplier"] == 3
    )
    policy["favorite_multiplier"] = 99
    assert (
        client.put(f"{API}/affinity/policy", json={"policy": policy}).status_code == 422
    )
    reset = client.post(f"{API}/affinity/policy/reset").json()["data"]
    assert reset["policy"]["favorite_multiplier"] == 2


def test_admin_stats_user_perks_and_items(web):
    client = _admin(web)
    stats = client.get(f"{API}/affinity/stats").json()["data"]
    assert stats["users"] >= 1 and stats["withAffinity"] >= 1
    perks_state = client.get(f"{API}/users/{OWNER}/affinity/perks").json()["data"]
    assert perks_state["level"] == "熟人"
    granted = client.post(
        f"{API}/users/{OWNER}/affinity/items",
        json={"item": "shield", "count": 2, "note": "补偿"},
    )
    assert granted.status_code == 200, granted.text
    assert store.load(OWNER)["inventory"]["shield"] == 2
    assert client.post(
        f"{API}/users/{OWNER}/affinity/items", json={"item": "nope", "count": 1}
    ).status_code in {404, 422}
    store.update(OWNER, lambda rec: rec.__setitem__("nickname", "坏名字"))
    assert client.delete(f"{API}/users/{OWNER}/affinity/nickname").status_code == 200
    assert store.load(OWNER)["nickname"] == ""
    run = client.post(f"{API}/affinity/decay/run").json()["data"]
    assert run["affected"] == 0


def test_affinity_admin_routes_need_admin(web):
    client = web["client"]
    assert client.get(f"{API}/affinity/policy").status_code in {401, 403}
    assert client.get(f"{API}/me/affinity/perks").status_code in {401, 403}


def test_me_affinity_play_round_trip(web):
    client = _member(web)
    state = client.get(f"{API}/me/affinity/perks")
    assert state.status_code == 200, state.text
    data = state.json()["data"]
    assert data["level"] == "熟人"
    assert "points" not in data or data.get("points") is None
    bought = client.post(
        f"{API}/me/affinity/buy", json={"item": "lucky_card", "count": 1}
    )
    assert bought.status_code == 200, bought.text
    assert set(bought.json()["data"]) == {"result", "quests", "state"}
    assert store.load(OWNER)["inventory"]["lucky_card"] == 1
    used = client.post(
        f"{API}/me/affinity/use", json={"item": "lucky_card", "groupId": None}
    )
    assert used.status_code == 200, used.text
    gift = client.post(
        f"{API}/me/affinity/gift",
        json={"gift": "candy", "count": 1, "groupId": str(G_OWNED)},
    )
    assert gift.status_code == 200, gift.text
    nick = client.put(f"{API}/me/affinity/nickname", json={"nickname": "阿明"})
    assert nick.status_code == 200 and store.load(OWNER)["nickname"] == "阿明"
    assert client.put(
        f"{API}/me/affinity/nickname", json={"nickname": "@全体成员"}
    ).status_code in {403, 422}
    hug = client.post(f"{API}/me/affinity/interact", json={"action": "抱抱"})
    assert hug.status_code == 200, hug.text
    assert client.post(f"{API}/me/affinity/quests/claim").status_code == 200
    poor = client.post(
        f"{API}/me/affinity/buy", json={"item": "repair_card", "count": 9}
    )
    assert poor.status_code in {409, 429}
    missing = client.post(
        f"{API}/me/affinity/buy", json={"item": "nothing", "count": 1}
    )
    assert missing.status_code == 404


def test_me_affinity_gift_group_must_be_own(web):
    client = _member(web)
    response = client.post(
        f"{API}/me/affinity/gift",
        json={"gift": "candy", "count": 1, "groupId": "123456"},
    )
    assert response.status_code == 403
    assert store.load(OWNER)["gift_log"] == []
