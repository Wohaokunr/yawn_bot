# ruff: noqa: ANN001, ANN003, E501, F811, PLR0913, PLW0108
"""群聊记忆 ↔ 私聊记忆打通（yawn_agent/memory_link.py）。

覆盖：忘掉的墓碑（私聊/某群/各处）、私聊 → 群按类别共享、静音群与“私聊里别提群”、
群画像跨群合并与冲突、称呼统一、recall 扩源、群里工具（各处都忘掉、亲口说的私聊也记着、
画像带共享事实）、群上下文注入与提示词、前缀缓存稳定、WebUI 接口。
"""

from __future__ import annotations

import importlib
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_private_agent import (
    BOT,
    G_OFF,
    G_ON,
    G_OPT,
    NOW,
    OTHER,
    USER,
    AgentMemory,
    BotGroup,
    GroupAgentConfig,
    UserGroup,
    _tool_ctx,
    bridge,
    maker,  # noqa: F401 - pytest fixture
    pctx,
    pmem,
    ptools,
)

_PKG = "src.plugins.yawn_core"
link = importlib.import_module(f"{_PKG}.yawn_agent.memory_link")
store = importlib.import_module(f"{_PKG}.yawn_agent.private_store")
memory = importlib.import_module(f"{_PKG}.yawn_agent.memory")
memory_handler = importlib.import_module(f"{_PKG}.yawn_agent.tool_handlers.memory")
tool_execution = importlib.import_module(f"{_PKG}.yawn_agent.tool_execution")
capabilities = importlib.import_module(f"{_PKG}.yawn_agent.capabilities")
context_loader = importlib.import_module(f"{_PKG}.yawn_agent.context_loader")
prompt = importlib.import_module(f"{_PKG}.yawn_agent.prompt")
sections = importlib.import_module(f"{_PKG}.yawn_agent.prompt_sections")
agent_bridge = importlib.import_module(f"{_PKG}.yawn_agent.agent_bridge")
registry = importlib.import_module(f"{_PKG}.yawn_agent.tool_registry")
routes = importlib.import_module(f"{_PKG}.webui.private_agent_routes")

G_TWO = 104


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVATE_AGENT_DATA_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("AGENT_BRIDGE_DATA_DIR", str(tmp_path / "bridge"))
    link.reset()
    sections.set_store_path(tmp_path / "prompt_overrides.json")
    yield
    link.reset()
    sections.set_store_path(None)


async def _add_second_group(session) -> None:
    """再加一个开着 Agent 的群，他在里面也有画像（爱好是另一样）。"""

    session.add_all(
        [
            BotGroup(group_id=G_TWO, group_name="第二个群"),
            GroupAgentConfig(group_id=G_TWO, enabled=True),
        ]
    )
    member = UserGroup(group_id=G_TWO, user_id=USER, last_seen_at=NOW, group_nickname="明哥")
    member.exp = 0
    member.group_affinity = 0
    session.add(member)
    session.add(_profile(G_TWO, "hobby", "爬山", salience=0.6))
    await session.commit()


def _profile(group_id, key, content, *, source="auto", salience=0.7, updated=None):
    return AgentMemory(
        group_id=group_id,
        subject_user_id=USER,
        memory_type="profile",
        memory_key=key,
        content=content,
        evidence_message_ids=[],
        source_kind=source,
        related_user_ids=[USER],
        salience=salience,
        confidence=0.8,
        visibility="group",
        expires_at=NOW + timedelta(days=30),
        updated_at=updated or NOW - timedelta(days=2),
    )


def _group_ctx(group_id, actor, session):
    return tool_execution.ToolExecutionContext(
        bot=SimpleNamespace(self_id=str(BOT)),
        group_id=group_id,
        actor_user_id=actor,
        session=session,
        capabilities=capabilities.BotGroupCapabilities(role="member", can_manage=False, actions=frozenset()),
    )


async def _run(ctx, name, **args):
    return await ptools.execute_private_tool(name, args, ctx)


# ── 墓碑：忘掉的事不再记回来 ─────────────────────────────────


def test_tombstone_scopes() -> None:
    link.add_tombstone(USER, key="hobby", keyword="香菜", scope="private", now=NOW)
    assert link.suppressed(USER, "hobby", "喜欢香菜、火锅", now=NOW)
    # 只在私聊忘掉的，群整理照常
    assert not link.suppressed(USER, "hobby", "喜欢香菜", now=NOW, scope="group", group_id=G_ON)
    link.add_tombstone(USER, key="hobby", keyword="香菜", scope="everywhere", now=NOW)
    assert link.suppressed(USER, "hobby", "香菜", now=NOW, scope="group", group_id=G_ON)
    # 同一条合并成一条（范围取大的）
    assert len(link.load(USER, now=NOW).tombstones) == 1
    # 某个群自己的只挡那个群，私聊不受影响
    link.add_tombstone(USER, key="skill", keyword="", scope="group", group_id=G_ON, now=NOW)
    assert link.suppressed(USER, "skill", "画画", now=NOW, scope="group", group_id=G_ON)
    assert not link.suppressed(USER, "skill", "画画", now=NOW, scope="group", group_id=G_TWO)
    assert not link.suppressed(USER, "skill", "画画", now=NOW)
    # 过期后失效
    later = NOW + timedelta(days=link.tombstone_days() + 1)
    assert not link.suppressed(USER, "hobby", "香菜", now=later)


def test_disabled_switch_restores_old_behaviour(monkeypatch) -> None:
    monkeypatch.setattr(bridge.private_agent_config, "memory_link_enabled", False)
    assert link.add_tombstone(USER, key="hobby", keyword="香菜", scope="private", now=NOW) is None
    assert not link.suppressed(USER, "hobby", "香菜", now=NOW)
    assert link.load(USER) == link.LinkState()


@pytest.mark.asyncio
async def test_private_consolidation_does_not_relearn_forgotten(maker) -> None:
    link.add_tombstone(USER, key="hobby", keyword="香菜", scope="private", now=NOW)
    async with maker() as session:
        auto = await pmem.remember(session, USER, "hobby", "喜欢香菜", now=NOW, source="auto")
        assert auto.get("suppressed") is True
        assert await pmem.list_facts(session, USER, now=NOW) == []
        # 他又亲口说了：私聊墓碑作废，记上
        told = await pmem.remember(session, USER, "hobby", "喜欢香菜", now=NOW, source="agent")
        assert told["created"] is True
    assert link.load(USER, now=NOW).tombstones == []


@pytest.mark.asyncio
async def test_consolidation_input_lists_known_facts(maker) -> None:
    async with maker() as session:
        await pmem.remember(session, USER, "skill", "弹吉他", now=NOW)
        text = await pmem._consolidate_input(session, USER, ["user: 今天好累"], NOW)
    assert "skill: 弹吉他（私聊）" in text
    assert "hobby: 喜欢猫（群里）" in text
    assert "不该出现" not in text
    assert text.endswith("user: 今天好累")


def test_group_consolidation_skips_forgotten_facts() -> None:
    link.add_tombstone(USER, key="hobby", keyword="香菜", scope="everywhere", now=NOW)
    session = SimpleNamespace(added=[], add=lambda row: session.added.append(row))
    facts = [
        {"user_id": USER, "key": "hobby", "content": "喜欢香菜", "evidence_message_ids": [1]},
        {"user_id": USER, "key": "skill", "content": "会做饭", "evidence_message_ids": [1]},
    ]
    memory._store_model_facts(session, G_ON, facts, {1}, {USER}, {1: USER}, NOW, {})
    assert [row.memory_key for row in session.added] == ["skill"]


# ── 私聊里的忘掉 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_private_forget_shadows_group_profile_and_reports_it(maker) -> None:
    async with maker() as session:
        ctx = await _tool_ctx(session)
        result = await _run(ctx, "forget_about_me", key="hobby", keyword="猫")
        assert result["ok"]
        payload = result["result"]
        assert payload["wont_relearn"] is True
        assert payload["still_in_groups"] == ["开着的群"]
        context = await pctx.build_private_context(session, USER, bot_id=BOT, nickname="小明", now=NOW)
        remembered = await _run(ctx, "what_you_remember")
    assert "喜欢猫" not in context.render()
    assert remembered["result"]["learned_in_groups"] == []
    assert remembered["result"]["asked_to_forget"] == 1


@pytest.mark.asyncio
async def test_private_forget_everywhere_blocks_group_relearning(maker) -> None:
    async with maker() as session:
        ctx = await _tool_ctx(session)
        result = await _run(ctx, "forget_about_me", key="hobby", include_groups=True)
        assert "开着的群" in result["result"]["groups_changed"]
    assert link.suppressed(USER, "hobby", "随便什么", now=NOW, scope="group", group_id=G_ON)
    kinds = [item["kind"] for item in link.load(USER).ledger]
    assert kinds == ["forget_everywhere"]


# ── 私聊 → 群：按类别共享 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_share_reaches_only_readable_groups(maker) -> None:
    async with maker() as session:
        ctx = await _tool_ctx(session)
        await _run(ctx, "remember_about_me", key="preferred_address", content="阿明")
        await session.commit()
        shared = await _run(ctx, "memory_link", action="share", keys=["preferred_address"])
        assert shared["ok"], shared
        assert shared["result"]["groups_that_see_it"] == ["开着的群"]
        on = await link.shared_facts_for_group(session, G_ON, [USER, OTHER], now=NOW)
        off = await link.shared_facts_for_group(session, G_OFF, [USER], now=NOW)
        opted = await link.shared_facts_for_group(session, G_OPT, [USER], now=NOW, opted_out={USER})
    assert [row.content for row in on[USER]] == ["阿明"]
    assert OTHER not in on
    # 没开 Agent 的群照样不该读到——由调用方（群上下文只在开着的群跑）保证；隐私退出直接挡
    assert opted == {}
    assert off.get(USER) is not None or off == {}


@pytest.mark.asyncio
async def test_share_can_be_limited_to_one_group_and_revoked(maker) -> None:
    async with maker() as session:
        await _add_second_group(session)
        ctx = await _tool_ctx(session)
        await _run(ctx, "remember_about_me", key="hobby", content="打羽毛球")
        await session.commit()
        await _run(ctx, "memory_link", action="share", keys=["hobby"], group="第二个群")
        first = await link.shared_facts_for_group(session, G_ON, [USER], now=NOW)
        second = await link.shared_facts_for_group(session, G_TWO, [USER], now=NOW)
        assert first == {} and [row.content for row in second[USER]] == ["打羽毛球"]
        await _run(ctx, "memory_link", action="unshare", keys=["hobby"])
        after = await link.shared_facts_for_group(session, G_TWO, [USER], now=NOW)
    assert after == {}
    assert link.load(USER).share_groups == frozenset()


@pytest.mark.asyncio
async def test_share_switch_off_keeps_settings_but_stops_flow(maker, monkeypatch) -> None:
    async with maker() as session:
        ctx = await _tool_ctx(session)
        await _run(ctx, "remember_about_me", key="hobby", content="打羽毛球")
        await session.commit()
        await _run(ctx, "memory_link", action="share", keys=["hobby"])
        monkeypatch.setattr(bridge.private_agent_config, "memory_link_share", False)
        assert await link.shared_facts_for_group(session, G_ON, [USER], now=NOW) == {}
        refused = await _run(ctx, "memory_link", action="share", keys=["skill"])
    assert refused["ok"] is False
    assert link.load(USER).share_keys == frozenset({"hobby"})


@pytest.mark.asyncio
async def test_shared_fact_leaves_when_member_leaves_group(maker) -> None:
    async with maker() as session:
        ctx = await _tool_ctx(session)
        await _run(ctx, "remember_about_me", key="hobby", content="打羽毛球")
        await _run(ctx, "memory_link", action="share", keys=["hobby"])
        member = await session.scalar(select(UserGroup).where(UserGroup.user_id == USER, UserGroup.group_id == G_ON))
        member.is_active = False
        await session.commit()
        assert await link.shared_facts_for_group(session, G_ON, [USER], now=NOW) == {}


@pytest.mark.asyncio
async def test_group_context_injects_shared_fact_in_speaker_layer(maker) -> None:
    async with maker() as session:
        ctx = await _tool_ctx(session)
        await _run(ctx, "remember_about_me", key="preferred_address", content="阿明")
        await _run(ctx, "memory_link", action="share", keys=["preferred_address"])
        await session.commit()
        config = await session.get(GroupAgentConfig, G_ON)
        context = await context_loader.load_context(
            session, G_ON, config, focus_user_ids=[USER], reference_at=NOW
        )
    shared = [item for item in context["memories"] if item.get("type") == "shared_by_member"]
    assert [item["content"] for item in shared] == ["阿明"]
    assert shared[0]["source_scope"] == "speaker"
    # 私聊里其他东西不进群
    assert "钓鱼" not in str(context)
    built = prompt.build_prompt(persona={"name": "Yawn"}, tools=[], context=context, user_prompt="hi")
    text = "\n".join(str(m["content"]) for m in built.messages)
    assert "shared_by_member" in text and "不要说是私聊里听来的" in text
    # 没有共享事实时这段规则不出现（静态前缀不变）
    plain = prompt.build_prompt(persona={"name": "Yawn"}, tools=[], context={"group_id": G_ON, "group_name": "x"}, user_prompt="hi")
    assert "不要说是私聊里听来的" not in "\n".join(str(m["content"]) for m in plain.messages)
    assert sections.get_section("core.shared_fact_guidance") is not None


@pytest.mark.asyncio
async def test_group_profile_tool_includes_shared_fact(maker) -> None:
    async with maker() as session:
        ctx = await _tool_ctx(session)
        await _run(ctx, "remember_about_me", key="skill", content="修电脑")
        await _run(ctx, "memory_link", action="share", keys=["skill", "hobby"])
        await session.commit()
        result = await memory_handler.handle("get_person_profile", {"user_id": USER}, _group_ctx(G_ON, OTHER, session))
    items = result.result if hasattr(result, "result") else result[0]
    keys = {item["key"]: item for item in items}
    assert keys["skill"]["shared_by_member"] is True
    # 本群已有爱好画像，不重复给私聊那条
    assert "shared_by_member" not in keys["hobby"]


# ── 群里的工具 ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_group_self_fact_is_also_remembered_in_private(maker) -> None:
    link.add_tombstone(USER, key="hobby", keyword="下棋", scope="everywhere", now=NOW)
    async with maker() as session:
        await memory_handler.handle(
            "remember_about_me", {"key": "hobby", "content": "下棋"}, _group_ctx(G_ON, USER, session)
        )
        await session.commit()
        facts = await pmem.list_facts(session, USER, now=NOW)
    assert [(row.memory_key, row.content, row.source_kind) for row in facts] == [("hobby", "下棋", "agent")]
    state = link.load(USER, now=NOW)
    assert state.tombstones == []
    assert [item["kind"] for item in state.ledger][-1] == "group_self_fact"


@pytest.mark.asyncio
async def test_group_self_fact_mirror_can_be_turned_off(maker, monkeypatch) -> None:
    monkeypatch.setattr(bridge.private_agent_config, "memory_link_group_self_facts", False)
    async with maker() as session:
        await memory_handler.handle(
            "remember_about_me", {"key": "hobby", "content": "下棋"}, _group_ctx(G_ON, USER, session)
        )
        await session.commit()
        assert await pmem.list_facts(session, USER, now=NOW) == []


@pytest.mark.asyncio
async def test_group_forget_everywhere_clears_other_groups_and_private(maker) -> None:
    async with maker() as session:
        await _add_second_group(session)
        await pmem.remember(session, USER, "hobby", "爬山", now=NOW)
        await session.commit()
        result = await memory_handler.handle(
            "forget_about_me", {"key": "hobby", "everywhere": True}, _group_ctx(G_ON, USER, session)
        )
        await session.commit()
        payload = result.result if hasattr(result, "result") else result[0]
        rows = (
            await session.execute(
                select(AgentMemory.group_id).where(
                    AgentMemory.subject_user_id == USER, AgentMemory.memory_key == "hobby"
                )
            )
        ).scalars().all()
    assert payload["everywhere"] is True and payload["cleared_elsewhere"] is True
    # 群里的结果不报他在哪些群、私聊里记过什么
    assert "第二个群" not in str(payload) and "爬山" not in str(payload)
    # 关着 Agent 的群也照删（删除只会让信息变少）；隐私退出的群跳过
    assert set(rows) <= {G_OPT}
    assert link.suppressed(USER, "hobby", "爬山", now=NOW, scope="group", group_id=G_TWO)


@pytest.mark.asyncio
async def test_group_forget_only_here_blocks_this_group(maker) -> None:
    async with maker() as session:
        result = await memory_handler.handle(
            "forget_about_me", {"key": "hobby"}, _group_ctx(G_ON, USER, session)
        )
    payload = result.result if hasattr(result, "result") else result[0]
    assert payload["wont_relearn"] is True and "everywhere" not in payload
    assert link.suppressed(USER, "hobby", "x", now=NOW, scope="group", group_id=G_ON)
    assert not link.suppressed(USER, "hobby", "x", now=NOW)


def test_group_forget_schema_has_everywhere() -> None:
    tool = next(item for item in registry.TOOL_DEFINITIONS if item.name == "forget_about_me")
    assert "everywhere" in tool.properties


# ── 群 → 私聊：静音、合并、冲突、称呼 ──────────────────────────


@pytest.mark.asyncio
async def test_muted_group_and_no_group_talk(maker) -> None:
    async with maker() as session:
        await _add_second_group(session)
        ctx = await _tool_ctx(session)
        muted = await _run(ctx, "memory_link", action="mute_group", group="开着的群")
        assert muted["result"]["muted"] is True
        context = await pctx.build_private_context(session, USER, bot_id=BOT, nickname="小明", now=NOW)
        text = context.render()
        assert "喜欢猫" not in text and "爬山" in text
        assert context.payload["memory_rules"]["skip_groups"] == ["开着的群"]
        # 群名列表照旧（工具选群要用）
        assert "开着的群" in context.payload["groups"]
        await _run(ctx, "memory_link", action="group_talk", on=False)
        quiet = await pctx.build_private_context(session, USER, bot_id=BOT, nickname="小明", now=NOW)
        recalled = await _run(ctx, "recall", query="爬山", where="groups")
    assert "爬山" not in quiet.render() and "group_profile" not in quiet.payload
    assert "bonds_in_groups" not in quiet.payload
    assert quiet.payload["memory_rules"]["no_group_talk"] is True
    assert recalled["result"]["in_groups"] == []


@pytest.mark.asyncio
async def test_group_profiles_merge_across_groups(maker) -> None:
    async with maker() as session:
        await _add_second_group(session)
        groups = await pctx.shared_groups(session, USER)
        facts = await pctx.group_profile_facts(session, USER, [G_ON, G_TWO], now=NOW)
        context = await pctx.build_private_context(session, USER, bot_id=BOT, nickname="小明", now=NOW)
    hobby = next(item for item in facts if item["key"] == "hobby")
    assert hobby["content"] == "喜欢猫、爬山"
    assert sorted(hobby["group_ids"]) == [G_ON, G_TWO]
    view = next(item for item in context.payload["group_profile"] if item["key"] == "hobby")
    assert view["group"] == "开着的群、第二个群" or view["group"] == "第二个群、开着的群"
    assert view["as_of"] == f"{NOW - timedelta(days=2):%Y-%m-%d}" or "as_of" in view
    assert {g.group_id for g in groups} >= {G_ON, G_TWO}
    # 他在第二个群的群名片和私聊名字不同，给出来
    assert {"group": "第二个群", "card": "明哥"} in context.payload["names_in_groups"]


@pytest.mark.asyncio
async def test_private_fact_wins_and_conflict_is_noted(maker) -> None:
    async with maker() as session:
        session.add(_profile(G_ON, "preferred_address", "小明同学", source="agent"))
        await session.commit()
        await pmem.remember(session, USER, "preferred_address", "阿明", now=NOW)
        await pmem.remember(session, USER, "hobby", "猫", now=NOW)
        context = await pctx.build_private_context(session, USER, bot_id=BOT, nickname="小明", now=NOW)
    payload = context.payload
    assert payload["user"]["call_them"] == "阿明"
    assert payload["memory_conflicts"] == [
        {"what": "希望的称呼", "private_says": "阿明", "group_remembers": "小明同学", "group": "开着的群"}
    ]
    # 列表类：私聊里已有“猫”，群里“喜欢猫”近似重复不再给
    assert "group_profile" not in payload or all(item["key"] != "hobby" for item in payload["group_profile"])


def test_call_them_falls_back_to_group_self_told_address() -> None:
    profile = [{"key": "preferred_address", "content": "眠眠", "self_told": True}]
    assert pctx.resolve_call_them([], {}, profile) == "眠眠"
    assert pctx.resolve_call_them([], {"call_me": "老大"}, profile) == "老大"
    assert pctx.resolve_call_them([], {}, [{**profile[0], "self_told": False}]) is None


@pytest.mark.asyncio
async def test_link_rules_live_in_stable_layer_and_are_byte_stable(maker) -> None:
    async with maker() as session:
        ctx = await _tool_ctx(session)
        await _run(ctx, "memory_link", action="mute_group", group="开着的群")
        first = await pctx.build_private_context(session, USER, bot_id=BOT, nickname="小明", now=NOW)
        second = await pctx.build_private_context(
            session, USER, bot_id=BOT, nickname="小明", now=NOW + timedelta(minutes=7)
        )
    assert "memory_rules" in first.render_stable()
    assert "memory_rules" not in first.render_volatile()
    assert first.render_stable() == second.render_stable()


@pytest.mark.asyncio
async def test_happenings_mark_when_he_was_there(maker) -> None:
    async with maker() as session:
        session.add(
            AgentMemory(
                group_id=G_ON,
                subject_user_id=0,
                memory_type="summary",
                memory_key=f"public_daily:{NOW:%Y-%m-%d}",
                content="大家约了周末去爬山",
                evidence_message_ids=[],
                source_kind="auto",
                related_user_ids=[USER, OTHER],
                salience=0.5,
                confidence=0.6,
                visibility="public",
            )
        )
        await session.commit()
        groups = await pctx.shared_groups(session, USER)
        items = await pctx.group_happenings(session, groups, now=NOW, user_id=USER)
        link.add_tombstone(USER, key=None, keyword="爬山", scope="private", now=NOW)
        hidden = await pctx.group_happenings(session, groups, now=NOW, user_id=USER, link=link.load(USER, now=NOW))
    assert items[0]["you_were_there"] is True
    assert hidden == []


# ── recall 扩源 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recall_reaches_recap_bridge_and_drops_forgotten(maker) -> None:
    store.update(USER, lambda record: record.update(recap={"summary": "聊了考研复习计划", "open_threads": ["考研报名"]}))
    agent_bridge.create_handoff(USER, group_id=G_ON, group_name="开着的群", topic="考研资料整理", asked="私聊发我", now=NOW)
    async with maker() as session:
        ctx = await _tool_ctx(session)
        found = await _run(ctx, "recall", query="考研")
        link.add_tombstone(USER, key=None, keyword="考研", scope="private", now=NOW)
        gone = await _run(ctx, "recall", query="考研")
    assert found["ok"], found
    result = found["result"]
    assert result["earlier_chat"] == ["聊了考研复习计划", "考研报名"]
    assert result["between_group_and_private"][0]["kind"] == "群里转来私聊的事"
    assert gone["result"]["found"] is False


@pytest.mark.asyncio
async def test_view_explains_settings_and_history(maker) -> None:
    async with maker() as session:
        ctx = await _tool_ctx(session)
        await _run(ctx, "remember_about_me", key="hobby", content="打羽毛球")
        await _run(ctx, "memory_link", action="share", keys=["hobby"])
        await _run(ctx, "forget_about_me", keyword="香菜")
        view = await _run(ctx, "memory_link", action="view")
    result = view["result"]
    assert result["settings"]["shared_with_groups"] == ["爱好"]
    assert result["forgotten"][0]["keyword"] == "香菜"
    assert [item["what"] for item in result["recent_changes"]] == ["允许群里也知道", "让我忘掉（私聊）"]
    assert "不会带到群里" in result["rule"]


@pytest.mark.asyncio
async def test_purge_drops_shares_but_keeps_tombstones(maker) -> None:
    async with maker() as session:
        ctx = await _tool_ctx(session)
        await _run(ctx, "remember_about_me", key="hobby", content="打羽毛球")
        await _run(ctx, "memory_link", action="share", keys=["hobby"])
        await _run(ctx, "forget_about_me", keyword="香菜")
        await pmem.purge_user(session, USER)
    state = link.load(USER)
    assert state.share_keys == frozenset()
    assert len(state.tombstones) == 1


# ── WebUI ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_webui_overview_detail_and_admin_tightening(maker, monkeypatch) -> None:
    # 路由拿到的是 WebUI 登录会话（没有 execute），数据库会话必须由路由自己开。
    monkeypatch.setattr(routes, "get_session", maker)
    auth = SimpleNamespace(role="admin", user_id=None)
    async with maker() as session:
        ctx = await _tool_ctx(session)
        await _run(ctx, "remember_about_me", key="hobby", content="打羽毛球")
        await _run(ctx, "memory_link", action="share", keys=["hobby"])
        await _run(ctx, "forget_about_me", keyword="香菜")
        await session.commit()
    overview = await routes.get_memory_link(auth, limit=50)
    detail = await routes.get_memory_link_user(USER, auth)
    group_view = {g["name"]: g for g in detail["data"]["groups"]}
    assert group_view["开着的群"]["group_sees_shared"] == [{"label": "爱好", "content": "打羽毛球"}]
    assert group_view["没开的群"]["group_sees_shared"] == []
    unshared = await routes.post_memory_link_unshare(USER, auth)
    assert unshared["data"]["settings"]["share_keys"] == []
    dropped = await routes.delete_memory_link_tombstone(USER, 0, auth)
    assert dropped["data"]["tombstones"] == []
    with pytest.raises(Exception):  # noqa: B017
        await routes.delete_memory_link_tombstone(USER, 5, auth)
    data = overview["data"]
    assert data["totals"] == {"sharing": 1, "muting": 0, "forgetting": 1}
    assert data["users"][0]["user_id"] == str(USER)


def test_store_keeps_memory_link_key() -> None:
    assert "memory_link" in store.empty_record()
    store.update(USER, lambda record: record["memory_link"].update(prefs={"share_keys": ["hobby", "bogus"]}))
    link.invalidate(USER)
    assert link.load(USER).share_keys == frozenset({"hobby"})


@pytest.mark.asyncio
async def test_wipe_keeps_tombstones_and_relationship_respects_mute(maker) -> None:
    async with maker() as session:
        ctx = await _tool_ctx(session)
        await _run(ctx, "forget_about_me", keyword="香菜")
        await _run(ctx, "memory_link", action="mute_group", group="开着的群")
        relation = await _run(ctx, "our_relationship")
        recent = await _run(ctx, "group_recent", group="开着的群")
        wiped = await _run(ctx, "my_data", action="wipe", confirm=True)
    assert relation["result"]["in_groups"] == []
    assert "别再主动提" in recent["result"]["note"]
    assert wiped["ok"]
    state = link.load(USER, now=NOW)
    assert len(state.tombstones) == 1 and state.muted_groups == frozenset()


@pytest.mark.asyncio
async def test_my_data_summary_mentions_shared_categories(maker) -> None:
    async with maker() as session:
        ctx = await _tool_ctx(session)
        plain = await _run(ctx, "my_data", action="summary")
        await _run(ctx, "memory_link", action="share", keys=["hobby"])
        shared = await _run(ctx, "my_data", action="summary")
    assert plain["result"]["where"] == "这些只在私聊里用，不会出现在群里"
    assert "「爱好」群里的我也知道" in shared["result"]["where"]
