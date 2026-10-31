# ruff: noqa: ANN001, ANN201, DTZ001, PLR2004, TRY003
"""群 Agent 与签到/好感的打通：签到工具、发言人层现状、群聊互动好感、声明核验。"""

from __future__ import annotations

import importlib
import sys
from datetime import date, datetime, timedelta
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

_PKG = "src.plugins.yawn_core"
affinity = importlib.import_module(f"{_PKG}.affinity")
standing = importlib.import_module(f"{_PKG}.yawn_agent.member_standing")
repo_mod = importlib.import_module(f"{_PKG}.yawn_agent.context_repository")
registry = importlib.import_module(f"{_PKG}.yawn_agent.tool_registry")
router = importlib.import_module(f"{_PKG}.yawn_agent.tool_router")
planner = importlib.import_module(f"{_PKG}.yawn_agent.tool_planner")
claims = importlib.import_module(f"{_PKG}.yawn_agent.speech_claims")
capabilities = importlib.import_module(f"{_PKG}.yawn_agent.capabilities")
tools = importlib.import_module(f"{_PKG}.yawn_agent.tools")
prompt = importlib.import_module(f"{_PKG}.yawn_agent.prompt")
Model = importlib.import_module("nonebot_plugin_orm").Model
BotUser = importlib.import_module(f"{_PKG}.data_models.bot_user").BotUser
BotGroup = importlib.import_module(f"{_PKG}.data_models.bot_group").BotGroup
UserGroup = importlib.import_module(f"{_PKG}.data_models.user_group").UserGroup
CheckinUser = importlib.import_module(f"{_PKG}.data_models.checkin_user").CheckinUser
AgentPrivacy = importlib.import_module(f"{_PKG}.data_models.agent_memory").AgentPrivacy
GroupFeature = importlib.import_module(f"{_PKG}.data_models.group_feature").GroupFeature

NOW = datetime(2026, 10, 31, 12, 0)
GROUP = 500
BOT = 9
MEMBER = 7


@pytest_asyncio.fixture
async def factory():
    affinity.reset_ledger_state()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Model.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        session.add_all(
            [
                BotGroup(group_id=GROUP, group_name="测试群"),
                BotUser(user_id=MEMBER, affinity=0),
            ]
        )
        await session.flush()
        member = UserGroup(group_id=GROUP, user_id=MEMBER, group_nickname="小明")
        member.group_affinity = 0
        member.exp = 0
        session.add(member)
        await session.commit()
    yield maker
    await engine.dispose()
    affinity.reset_ledger_state()


# ── 发言人层现状 ───────────────────────────────────────────


def test_standing_view_hides_numbers_and_skips_noise() -> None:
    fresh = repo_mod.MemberStanding(
        affinity=0, streak_days=1, last_checkin_date=NOW.date()
    )
    assert standing.standing_view(fresh, NOW) == {}
    assert standing.standing_view(None, NOW) == {}
    warm = repo_mod.MemberStanding(
        affinity=180, streak_days=5, last_checkin_date=NOW.date() - timedelta(days=1)
    )
    view = standing.standing_view(warm, NOW)
    assert view["overall"].startswith("朋友")
    assert view["checkin_streak"] == "本群连续签到 5 天"
    assert "180" not in str(view)


def test_standing_view_drops_broken_streak() -> None:
    broken = repo_mod.MemberStanding(
        affinity=0, streak_days=9, last_checkin_date=date(2026, 10, 20)
    )
    assert "checkin_streak" not in standing.standing_view(broken, NOW)


def test_merge_speaker_standing_extends_or_prepends() -> None:
    record = repo_mod.MemberStanding(affinity=30, streak_days=0, last_checkin_date=None)
    views = [{"user_id": MEMBER, "stage": "熟人"}, {"user_id": 8, "stage": "面熟"}]
    merged = standing.merge_speaker_standing(
        views, speaker_id=MEMBER, speaker_name="小明", standing=record, now=NOW
    )
    assert merged[0]["overall"].startswith("面熟")
    assert "overall" not in views[0]  # 不改原列表
    alone = standing.merge_speaker_standing(
        [], speaker_id=MEMBER, speaker_name="小明", standing=record, now=NOW
    )
    assert alone == [
        {"user_id": MEMBER, "name": "小明", "overall": affinity.level_words(30)}
    ]
    assert (
        standing.merge_speaker_standing(
            views, speaker_id=None, speaker_name=None, standing=record, now=NOW
        )
        == views
    )


@pytest.mark.asyncio
async def test_load_members_returns_standing_in_one_query(factory) -> None:
    async with factory() as session:
        summary = CheckinUser(
            group_id=GROUP, user_id=MEMBER, total_days=4, points=20, streak_days=4
        )
        summary.last_checkin_date = NOW.date()
        session.add(summary)
        user = await session.get(BotUser, MEMBER)
        user.affinity = 70
        await session.commit()
        repo = repo_mod.AgentContextRepository(session)
        members = await repo.load_members(GROUP, {MEMBER})
    assert [m.user_id for m in members] == [MEMBER]
    assert repo.query_count == 1
    assert repo.member_standing[MEMBER].affinity == 70
    assert repo.member_standing[MEMBER].streak_days == 4


def test_bond_guidance_mentions_new_fields() -> None:
    assert "overall" in prompt._BOT_BOND_GUIDANCE
    assert "checkin_streak" in prompt._BOT_BOND_GUIDANCE


# ── 群聊互动好感 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_group_interaction_rewards_member_once_per_cap(factory) -> None:
    async with factory() as session:
        results = [
            await standing.reward_group_interaction(
                session,
                group_id=GROUP,
                bot_id=BOT,
                actor_user_id=MEMBER,
                trigger_source="mention",
                now=NOW,
            )
            for _ in range(6)
        ]
        await session.commit()
        user = await session.get(BotUser, MEMBER)
        member = await session.get(UserGroup, (GROUP, MEMBER))
    cap = affinity.SOURCE_RULES["group_chat"].daily_cap
    assert results.count(True) == cap
    assert user.affinity == cap and member.group_affinity == cap


@pytest.mark.asyncio
async def test_group_interaction_skips_bot_wake_and_opted_out(factory) -> None:
    async with factory() as session:
        assert not await standing.reward_group_interaction(
            session,
            group_id=GROUP,
            bot_id=BOT,
            actor_user_id=BOT,
            trigger_source="mention",
            now=NOW,
        )
        assert not await standing.reward_group_interaction(
            session,
            group_id=GROUP,
            bot_id=BOT,
            actor_user_id=None,
            trigger_source="mention",
            now=NOW,
        )
        assert not await standing.reward_group_interaction(
            session,
            group_id=GROUP,
            bot_id=BOT,
            actor_user_id=MEMBER,
            trigger_source="scheduled_task",
            now=NOW,
        )
        session.add(AgentPrivacy(group_id=GROUP, user_id=MEMBER, opted_out=True))
        await session.flush()
        assert not await standing.reward_group_interaction(
            session,
            group_id=GROUP,
            bot_id=BOT,
            actor_user_id=MEMBER,
            trigger_source="mention",
            now=NOW,
        )


@pytest.mark.asyncio
async def test_group_interaction_never_raises() -> None:
    broken = SimpleNamespace()  # 没有 scalar/begin_nested
    assert not await standing.reward_group_interaction(
        broken,
        group_id=GROUP,
        bot_id=BOT,
        actor_user_id=MEMBER,
        trigger_source="mention",
        now=NOW,
    )


# ── 签到工具 ───────────────────────────────────────────────


class _Bot:
    self_id = str(BOT)

    async def call_api(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("测试环境无 OneBot API")


def test_checkin_tools_registered_in_their_own_family() -> None:
    defs = {d.name: d for d in registry.TOOL_DEFINITIONS}
    assert defs["checkin_for_me"].family == "checkin"
    assert (
        defs["checkin_for_me"].permission_level == registry.TOOL_PERMISSION_STATE_WRITE
    )
    assert defs["get_my_checkin"].permission_level == registry.TOOL_PERMISSION_READ
    assert defs["checkin_for_me"].properties == {}  # 只能签调用者本人
    assert router.normalize_tool_family("签到") == "checkin"
    assert router.normalize_tool_family("好感度") == "checkin"
    assert "checkin" in planner.TOOLPACK_CATALOG


@pytest.mark.asyncio
async def test_checkin_tool_signs_actor_and_reports_status(factory) -> None:
    caps = capabilities.BotGroupCapabilities(
        role="member", can_manage=False, actions=frozenset({"send_group_msg"})
    )
    async with factory() as session:

        async def run(name: str, actor: int | None = MEMBER) -> dict[str, Any]:
            return await tools.execute_tool(
                name,
                {},
                bot=_Bot(),
                group_id=GROUP,
                actor_user_id=actor,
                session=session,
                capabilities=caps,
            )

        first = await run("checkin_for_me")
        assert first["ok"] is True, first
        assert first["result"]["status"] == "ok"
        assert first["result"]["streak_days"] == 1
        assert "affinity" not in first["result"]
        again = await run("checkin_for_me")
        assert again["result"]["status"] == "already"
        status = await run("get_my_checkin")
        assert status["ok"] is True
        assert status["result"]["checkin"]["checked_today"] is True
        assert status["result"]["rank_today"] == 1
        assert "points" not in status["result"]["relationship_with_you"]
        nobody = await run("checkin_for_me", actor=None)
        assert nobody["ok"] is False


@pytest.mark.asyncio
async def test_checkin_tool_respects_feature_switch(factory) -> None:
    caps = capabilities.BotGroupCapabilities(
        role="member", can_manage=False, actions=frozenset({"send_group_msg"})
    )
    async with factory() as session:
        session.add(GroupFeature(group_id=GROUP, feature="checkin", enabled=False))
        await session.commit()
        result = await tools.execute_tool(
            "checkin_for_me",
            {},
            bot=_Bot(),
            group_id=GROUP,
            actor_user_id=MEMBER,
            session=session,
            capabilities=caps,
        )
    assert result["ok"] is False
    assert result.get("error_code") == "permission"


def test_checkin_claim_requires_real_tool() -> None:
    fake = claims.check_action_claims("已经帮你签好了！", frozenset())
    assert fake.changed and "checkin" in fake.families
    real = claims.check_action_claims("已经帮你签好了！", frozenset({"checkin_for_me"}))
    assert not real.changed
    asked = claims.check_action_claims("要不要我帮你签到？", frozenset())
    assert not asked.changed


# ── WebUI ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_webui_affinity_view_and_manual_adjust(factory, monkeypatch) -> None:
    from contextlib import asynccontextmanager

    from fastapi import HTTPException

    users = importlib.import_module(f"{_PKG}.webui.users")
    models = importlib.import_module(f"{_PKG}.webui.route_models")

    @asynccontextmanager
    async def fake_session():
        async with factory() as session:
            yield session

    notified: list[tuple[str, str]] = []

    async def fake_notify(resource: str, key: str, **_kwargs: Any) -> None:
        notified.append((resource, key))

    monkeypatch.setattr(users, "get_session", fake_session)
    monkeypatch.setattr(users.hub, "notify_change", fake_notify)

    view = await users.get_user_affinity(MEMBER, None)
    data = view["data"]
    assert data["level"] == "陌生" and data["points"] == 0
    assert data["groups"][0]["groupName"] == "测试群"
    assert {item["name"] for item in data["levels"]} >= {"陌生", "挚友"}

    adjusted = await users.adjust_user_affinity(
        MEMBER, models.AffinityAdjustBody(delta=25, note="活动奖励"), None
    )
    assert adjusted["data"]["points"] == 25
    assert adjusted["data"]["level"] == "面熟"
    assert adjusted["data"]["events"][0]["note"] == "活动奖励"
    assert notified == [("user_affinity", str(MEMBER))]

    with pytest.raises(HTTPException):
        await users.adjust_user_affinity(
            MEMBER, models.AffinityAdjustBody(delta=0), None
        )
    with pytest.raises(Exception):  # noqa: B017
        models.AffinityAdjustBody(delta=9999)
