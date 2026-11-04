# ruff: noqa: ANN001, ANN003, ARG001, DTZ001, E501, F811, PLR2004
"""私聊主动提议：候选、打扰控制门槛、提议回合、他回话后的跟进、offers 工具、调度和 WebUI。"""

from __future__ import annotations

import asyncio
import importlib
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from test_private_agent import (  # noqa: F401 - pytest fixture
    NOW,
    USER,
    maker,
    tool_names,
)

_PKG = "src.plugins.yawn_core"
store = importlib.import_module(f"{_PKG}.yawn_agent.private_store")
po = importlib.import_module(f"{_PKG}.yawn_agent.private_offers")
policy = importlib.import_module(f"{_PKG}.yawn_agent.private_offer_policy")
turn = importlib.import_module(f"{_PKG}.yawn_agent.private_offer_turn")
ptools = importlib.import_module(f"{_PKG}.yawn_agent.private_tools")
pagent = importlib.import_module(f"{_PKG}.yawn_agent.private_agent")
registry = importlib.import_module(f"{_PKG}.yawn_agent.private_prompt_registry")
prompt_sections = importlib.import_module(f"{_PKG}.yawn_agent.prompt_sections")
private_bridge = importlib.import_module(f"{_PKG}.private_chat_bridge")
routes = importlib.import_module(f"{_PKG}.webui.private_offer_routes")

Settings = policy.OfferSettings
Facts = po.Facts
ADMIN = SimpleNamespace(role="admin", user_id=None, actor_fingerprint="t")
# 周二下午 3 点：不在夜间时段、不是周末
AT = datetime(2026, 11, 3, 15, 0)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVATE_AGENT_DATA_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("PRIVATE_AGENT_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("PRIVATE_AGENT_OFFER_DIR", str(tmp_path / "offers"))
    monkeypatch.setenv("AGENT_BRIDGE_DATA_DIR", str(tmp_path / "bridge"))
    policy.reset_settings_cache()
    private_bridge.reset_state_for_tests()
    yield
    policy.reset_settings_cache()
    private_bridge.reset_state_for_tests()


def _record(**sections: Any) -> dict[str, Any]:
    record = store.empty_record()
    record.update(sections)
    return record


def _facts(**kw: Any) -> Any:
    base = {"last_user_at": AT - timedelta(hours=5), "last_bot_at": AT - timedelta(hours=5), "friend": True}
    base.update(kw)
    return Facts(**base)


def _todos() -> list[dict[str, Any]]:
    return [
        {"id": 1, "content": "交实验报告", "due": "2026-11-01", "done": False, "at": "2026-10-20 10:00"},
        {"id": 2, "content": "买猫粮", "due": "2026-11-03", "done": False, "priority": "high", "at": "2026-11-02 10:00"},
        {"id": 3, "content": "学吉他", "done": False, "at": "2026-10-01 10:00"},
        {"id": 4, "content": "已经做完的", "done": True, "due": "2026-10-01", "at": "2026-09-01 10:00"},
    ]


# ── 1. 策略 ────────────────────────────────────────────────────────────


def test_policy_defaults_roundtrip_and_kind_normalization() -> None:
    settings = policy.load_settings()
    assert settings.interval_hours == 3 and settings.max_unanswered == 2 and settings.default_on
    assert settings.in_quiet_hours("23:00") and settings.in_quiet_hours("08:59") and not settings.in_quiet_hours("15:00")
    assert set(settings.kinds) == set(policy.ALL_KINDS) and all(settings.kinds.values())
    saved = policy.save_settings(Settings(interval_hours=5, kinds={"mood_care": False, "nope": False}))
    policy.reset_settings_cache()
    loaded = policy.load_settings()
    assert loaded.interval_hours == 5 and loaded.kinds["mood_care"] is False and "nope" not in loaded.kinds
    assert saved.kind_on("todo_overdue") and not saved.kind_on("mood_care")
    # 坏文件回落默认
    policy.save_settings(Settings())
    (policy.state_dir() / "settings.json").write_text("{坏的", encoding="utf-8")
    policy.reset_settings_cache()
    assert policy.load_settings().interval_hours == 3


def test_policy_user_lists_and_usage() -> None:
    settings = Settings(user_mode="allowlist", allowed_users=[USER, USER, -1], blocked_users=[7])
    assert settings.allowed_users == [USER]
    assert settings.user_open(USER) is None and settings.user_open(8) == "not_listed" and settings.user_open(7) == "blocked"
    policy.record_usage("sent", ["todo_today"])
    policy.record_usage("sent", ["todo_today", "checkin"])
    usage = policy.usage_snapshot()
    assert usage["results"]["sent"] == 2 and usage["kinds"]["todo_today"] == 2
    assert {row["kind"] for row in policy.kind_catalog()} == set(policy.ALL_KINDS)


# ── 2. 候选 ────────────────────────────────────────────────────────────


def test_todo_candidates_cover_overdue_today_stale_pile_and_plans() -> None:
    record = _record(todos=_todos())
    found = {c.kind: c for c in po.collect_candidates(record, AT)}
    assert found["todo_overdue"].what == "交实验报告" and found["todo_overdue"].details["days_late"] == 2
    assert found["todo_today"].what == "买猫粮" and found["todo_today"].score == 80  # 高优先级加分
    assert found["todo_stale"].what == "学吉他"
    assert "todo_pile" not in found  # 只有 3 件没做完
    morning = {c.kind for c in po.collect_candidates(record, AT.replace(hour=9))}
    assert "day_plan" in morning
    many = _record(todos=[{"id": i, "content": f"事{i}", "done": False, "at": "2026-11-03 09:00"} for i in range(8)])
    assert any(c.kind == "todo_pile" for c in po.collect_candidates(many, AT))
    sunday_evening = datetime(2026, 11, 8, 19, 0)
    assert any(c.kind == "weekly_plan" for c in po.collect_candidates(record, sunday_evening))


def test_reminder_countdown_thread_task_mood_notes_handoff_checkin() -> None:
    record = _record(
        reminders=[
            {"id": 10, "content": "组会", "due": "2026-11-03 17:00"},
            {"id": 11, "content": "替我查天气", "due": "2026-11-03 16:00", "mode": "agent"},
            {"id": 12, "content": "很久以后", "due": "2026-11-09 17:00"},
            {"id": 13, "content": "坏掉的", "due": "2026-11-03 16:00", "broken": True},
        ],
        countdowns=[{"id": 20, "title": "考试", "date": "2026-11-05"}, {"id": 21, "title": "远的", "date": "2026-12-25"}],
        recap={"summary": "", "open_threads": ["周五面试"], "updated_at": "2026-11-02 21:00"},
        tasks=[
            {"id": "t1", "title": "整理论文", "status": "blocked", "question": "要中文还是英文？"},
            {"id": "t2", "title": "做表格", "status": "failed", "finished_at": "2026-11-03 12:00", "error": "网页打不开"},
            {"id": "t3", "title": "旧的", "status": "failed", "finished_at": "2026-10-30 12:00"},
        ],
        moods=[{"at": "2026-11-02 20:00", "score": 1}, {"at": "2026-11-03 09:00", "score": 2}],
        notes=[{"id": 30 + i, "content": f"笔记{i}", "at": "2026-11-02 10:00"} for i in range(5)],
        flags={"reminder_log": [{"at": "2026-11-03 08:00", "id": 14, "content": "吃药", "result": "failed"}]},
    )
    handoffs = [{"id": "h1", "topic": "帮我找论文", "group_name": "开着的群", "created": "2026-11-03 10:00"}]
    found = po.collect_candidates(record, AT.replace(hour=19), unchecked_groups=2, handoffs=handoffs)
    kinds = [c.kind for c in found]
    assert kinds.count("reminder_prep") == 0  # 19 点时组会已经过了
    found_afternoon = {c.key: c for c in po.collect_candidates(record, AT)}
    assert "rem:10:2026-11-03 17:00" in found_afternoon  # 3 小时内、notify 模式
    assert not any(k.startswith("rem:11:") for k in found_afternoon)  # agent 模式会自己去办
    assert "rem:13:broken" in found_afternoon and "rem:14:failed:2026-11-03" in found_afternoon
    for kind in ("countdown_near", "open_thread", "task_blocked", "task_failed", "mood_care", "notes_digest", "handoff_open", "checkin"):
        assert kind in kinds, kind
    assert sum(1 for c in found if c.kind == "task_failed") == 1  # 一天以前的不算
    assert next(c for c in found if c.kind == "task_blocked").details["question"] == "要中文还是英文？"
    # 15 点还没到傍晚：不提签到
    assert not any(c.kind == "checkin" for c in po.collect_candidates(record, AT, unchecked_groups=2))
    # 群里刚转来（不到 2 小时）的事不问
    fresh = [{"id": "h2", "topic": "新的", "group_name": "群", "created": "2026-11-03 14:30"}]
    assert not any(c.kind == "handoff_open" for c in po.collect_candidates(_record(), AT, handoffs=fresh))


def test_filter_drops_kind_off_muted_cooldown_low_score_and_sorts() -> None:
    record = _record(todos=_todos())
    data = po.state(record)
    data["seen"]["todo:2:today"] = "2026-11-03 10:00"
    po.mute_kind(record, "todo_stale", AT, 3)
    settings = Settings(kinds={"todo_overdue": True}, min_score=0, max_candidates=4)
    kept, dropped = po.filter_candidates(record, po.collect_candidates(record, AT), settings, AT)
    reasons = {row["kind"]: row["dropped"] for row in dropped}
    assert reasons == {"todo_today": "cooldown", "todo_stale": "muted"}
    assert [c.kind for c in kept] == ["todo_overdue"]
    off = Settings(kinds={"todo_overdue": False})
    kept, dropped = po.filter_candidates(record, po.collect_candidates(record, AT), off, AT)
    assert not kept and {row["dropped"] for row in dropped} == {"kind_off", "cooldown", "muted"}
    high_bar = Settings(min_score=90)
    kept, dropped = po.filter_candidates(_record(todos=_todos()), po.collect_candidates(_record(todos=_todos()), AT), high_bar, AT)
    assert not kept and all(row["dropped"] == "low_score" for row in dropped)
    # 冷却过了就能再问；排序按分数
    later = AT + timedelta(days=2)
    kept, _ = po.filter_candidates(_record(todos=_todos()), po.collect_candidates(_record(todos=_todos()), later), Settings(), later)
    assert kept[0].kind == "todo_overdue" and kept[0].score >= kept[-1].score


# ── 3. 打扰控制 ──────────────────────────────────────────────────────────


def _gate(record=None, settings=None, facts=None, now=AT, **kw):
    return po.evaluate_gate(record or _record(), settings or Settings(), facts or _facts(), now, USER, **kw)


def test_gate_respects_switches_and_wishes() -> None:
    assert _gate().ok
    assert _gate(env_on=False).reason == "env_off"
    assert _gate(agent_on=False).reason == "agent_off"
    assert _gate(settings=Settings(enabled=False)).reason == "policy_off"
    assert _gate(settings=Settings(blocked_users=[USER])).reason == "blocked"
    assert _gate(settings=Settings(user_mode="allowlist")).reason == "not_listed"
    assert _gate(settings=Settings(default_on=False)).reason == "user_off"
    opted_in = _record()
    po.set_enabled(opted_in, True)
    assert _gate(opted_in, settings=Settings(default_on=False)).ok
    off = _record()
    po.set_enabled(off, False)
    assert _gate(off).reason == "user_off"
    assert _gate(facts=_facts(paused=True)).reason == "paused"
    assert _gate(facts=_facts(friend=False)).reason == "not_friend"
    assert _gate(facts=_facts(friend=False), settings=Settings(only_friends=False)).ok
    assert _gate(facts=_facts(last_user_at=None)).reason == "no_history"
    assert _gate(facts=_facts(last_user_at=AT - timedelta(days=8))).reason == "inactive"
    assert _gate(facts=_facts(busy=True)).reason == "busy"


def test_gate_quiet_hours_dnd_snooze_and_chatting() -> None:
    night = _gate(now=AT.replace(hour=23))
    assert night.reason == "quiet" and night.next_at == datetime(2026, 11, 4, 9, 0)
    assert _gate(now=AT.replace(hour=23), settings=Settings(quiet_hours_enabled=False)).ok
    dnd = _record(prefs={"quiet_start": "14:00", "quiet_end": "16:00"})
    blocked = _gate(dnd)
    assert blocked.reason == "dnd" and blocked.next_at == AT.replace(hour=16)
    snoozed = _record()
    po.snooze(snoozed, AT, 2)
    assert _gate(snoozed).reason == "snoozed"
    chatting = _gate(facts=_facts(last_user_at=AT - timedelta(minutes=10)))
    assert chatting.reason == "chatting" and chatting.next_at == AT + timedelta(minutes=20)
    # 我刚说过话也不插话
    assert _gate(facts=_facts(last_bot_at=AT - timedelta(minutes=5))).reason == "chatting"


def test_gate_interval_backoff_unanswered_and_daily_cap() -> None:
    settings = Settings(jitter_minutes=0)
    record = _record()
    # 3 小时前刚聊过：还没到
    recent = _facts(last_user_at=AT - timedelta(hours=2), last_bot_at=AT - timedelta(hours=2))
    gate = _gate(record, settings, recent)
    assert gate.reason == "not_due" and gate.next_at == AT + timedelta(hours=1)
    # 问了一次没回：间隔翻倍
    po.record_attempt(record, "sent", AT - timedelta(hours=4), candidates=[po.Candidate("todo_today", "k1", "x", "y", 70)], picks=[1])
    gate = _gate(record, settings)
    assert gate.unanswered == 1 and gate.reason == "not_due" and gate.next_at == AT + timedelta(hours=2)
    assert gate.interval_hours == 6
    # 再问一次也没回：停到他先开口
    po.record_attempt(record, "sent", AT - timedelta(hours=1), candidates=[po.Candidate("todo_today", "k2", "x", "y", 70)], picks=[1])
    assert _gate(record, settings).reason == "unanswered"
    # 他开口之后清零
    spoke = _facts(last_user_at=AT - timedelta(minutes=50), last_bot_at=AT - timedelta(minutes=50))
    assert _gate(record, settings, spoke).unanswered == 0
    # 当日上限
    capped = _record()
    for hour in (9, 10, 11):
        po.record_attempt(capped, "sent", AT.replace(hour=hour), picks=[])
        po.state(capped)["log"][-1]["outcome"] = "replied"
    gate = _gate(capped, Settings(daily_max=3), _facts(last_user_at=AT - timedelta(hours=5)))
    assert gate.reason == "daily_cap" and gate.next_at == datetime(2026, 11, 4)
    # 手动试一次跳过间隔、没回次数和上限，不跳过他的意愿
    assert _gate(capped, Settings(daily_max=3), force=True).ok
    off = _record()
    po.set_enabled(off, False)
    assert _gate(off, force=True).reason == "user_off"


def test_backoff_is_capped_jitter_is_stable_and_user_interval_has_floor() -> None:
    settings = Settings(jitter_minutes=0, max_backoff_hours=8, max_unanswered=5)
    record = _record()
    for hours in (30, 20, 10):
        po.record_attempt(record, "sent", AT - timedelta(hours=hours), picks=[])
    quiet_since = AT - timedelta(hours=40)
    due, missed, hours = po.next_due(record, settings, _facts(last_user_at=quiet_since, last_bot_at=quiet_since), USER)
    assert missed == 3 and hours == 8 and due == AT - timedelta(hours=2)
    one = po.jitter_minutes(USER, AT, 15)
    assert one == po.jitter_minutes(USER, AT, 15) and 0 <= one <= 15 and po.jitter_minutes(USER, AT, 0) == 0
    own = _record()
    assert po.set_interval(own, 0.5, Settings(user_min_interval_hours=1)) == 1
    assert po.interval_hours(own, Settings()) == 1
    po.set_interval(own, None, Settings())
    assert po.interval_hours(own, Settings(interval_hours=4)) == 4


# ── 4. 问完之后 ──────────────────────────────────────────────────────────


def test_parse_pick_variants() -> None:
    assert po.parse_pick("【提议:2】要不要我帮你把报告拆一下？", 3) == ("要不要我帮你把报告拆一下？", [2], False)
    assert po.parse_pick("【提议：1，3】两件事", 3)[1] == [1, 3]
    assert po.parse_pick("【提议:9】越界", 3)[1] == [1]  # 越界按第一件
    assert po.parse_pick("没写标记", 2)[1] == [1]
    assert po.parse_pick("【提议:无】", 2) == ("", [], True)
    assert po.parse_pick("", 2) == ("", [], False)


def test_attempt_turn_view_and_resolve_flow() -> None:
    settings = Settings()
    record = _record()
    cands = [po.Candidate("todo_today", "todo:2:today", "买猫粮", "帮他准备", 70), po.Candidate("mood_care", "mood:x", "心情", "陪聊", 50)]
    row = po.record_attempt(record, "sent", AT, candidates=cands, picks=[1], text="要我帮你下单猫粮吗？", settings=settings)
    data = po.state(record)
    assert data["pending"] == row["id"] and data["seen"] == {"todo:2:today": "2026-11-03 15:00"}
    view = po.turn_view(record, AT + timedelta(hours=1), settings)
    assert view["offer_waiting"]["about"] == ["买猫粮"] and view["offer_waiting"]["you_asked"].startswith("要我帮你")
    assert po.state(record)["log"][-1]["replied_at"] == "2026-11-03 16:00"
    result = po.resolve(record, "declined", AT + timedelta(hours=1), settings)
    assert result["muted_kinds"] == ["今天到期的待办"]
    assert "todo_today" in po.muted_kinds(record, AT + timedelta(days=2))
    assert "todo_today" not in po.muted_kinds(record, AT + timedelta(days=4))
    assert po.turn_view(record, AT + timedelta(hours=2), settings) == {}
    with pytest.raises(ValueError):
        po.resolve(record, "maybe", AT, settings)
    with pytest.raises(LookupError):
        po.resolve(_record(), "accepted", AT, settings)
    assert po.stats(record)["declined"] == 1


def test_unanswered_offer_expires_and_chatting_about_else_clears() -> None:
    settings = Settings(answer_window_hours=12)
    record = _record()
    po.record_attempt(record, "sent", AT, candidates=[po.Candidate("todo_today", "k", "x", "y", 70)], picks=[1], text="要帮忙吗")
    assert po.turn_view(record, AT + timedelta(hours=13), settings) == {}
    assert po.state(record)["log"][-1]["outcome"] == "ignored" and "pending" not in po.state(record)
    again = _record()
    po.record_attempt(again, "sent", AT, candidates=[po.Candidate("todo_today", "k", "x", "y", 70)], picks=[1], text="要帮忙吗")
    for minutes in (10, 20, 30):
        assert po.turn_view(again, AT + timedelta(minutes=minutes), settings)
    assert po.turn_view(again, AT + timedelta(minutes=40), settings) == {}
    assert po.state(again)["log"][-1]["outcome"] == "replied"


def test_skipped_and_failed_attempts() -> None:
    record = _record()
    cands = [po.Candidate("notes_digest", "notes:w", "笔记", "整理", 35)]
    po.record_attempt(record, "skipped", AT, candidates=cands)
    assert po.state(record)["seen"]["notes:w"] == "2026-11-03 15:00"
    assert po.last_attempt_at(po.state(record)) == AT
    po.record_attempt(record, "failed", AT)
    assert po.state(record)["retry_at"] == "2026-11-03 15:30"
    assert _gate(record, facts=_facts()).reason == "backoff"
    po.record_attempt(record, "user_spoke", AT)
    assert po.last_attempt_at(po.state(record)) == AT  # 他先开口不算一次提议
    history = po.history_view(record)
    assert history[0]["status"] == "正要问时他先说话了"


# ── 5. offers 工具 ──────────────────────────────────────────────────────


def _ctx():
    return ptools.PrivateToolContext(session=None, user_id=USER, bot_id=9, groups=[], now=AT)


async def _tool(ctx, **args):
    return await ptools.execute_private_tool("offers", args, ctx)


def test_offers_tool_actions_and_claims() -> None:
    assert "offers" in ptools.TOOLS_BY_NAME and "offers" in ptools.WRITE_TOOLS
    assert "offers" in {s["function"]["name"] for s in ptools.tool_schemas()}

    async def go():
        ctx = _ctx()
        status = await _tool(ctx, action="status")
        assert status["ok"] and status["result"]["settings"]["on"] is True
        off = await _tool(ctx, action="off")
        assert off["result"]["done"] and "offers.off" in ctx.succeeded and "offers.done" in ctx.succeeded
        assert not po.user_enabled(store.load(USER), Settings())
        await _tool(ctx, action="on")
        paused = await _tool(ctx, action="pause", hours=5)
        assert paused["result"]["paused_until"] == "2026-11-03 20:00"
        every = await _tool(ctx, action="every", hours=0.5)
        assert every["result"]["every_hours"] == 1 and "note" in every["result"]
        muted = await _tool(ctx, action="mute", kind="mood_care")
        assert muted["result"]["until"] == "一直"
        missing = await _tool(ctx, action="mute")
        assert not missing["ok"]
        await _tool(ctx, action="unmute", kind="mood_care")
        assert po.muted_kinds(store.load(USER), AT) == {}
        nothing = await _tool(ctx, action="resolve", outcome="accepted")
        assert not nothing["ok"] and nothing["error_code"] == "not_found"
        store.update(USER, lambda rec: po.record_attempt(rec, "sent", AT, candidates=[po.Candidate("todo_today", "k", "x", "y", 70)], picks=[1]))
        done = await _tool(ctx, action="resolve", outcome="accepted")
        assert done["result"]["outcome"] == "accepted"
        history = await _tool(ctx, action="history")
        assert history["result"]["stats"]["accepted"] == 1

    asyncio.run(go())
    assert policy.usage_snapshot()["results"]["accepted"] == 1


# ── 6. 提议回合（完整私聊主回合） ─────────────────────────────────────────


def _payload(n: int = 2) -> dict[str, Any]:
    cands = [po.Candidate("todo_overdue", "todo:1:overdue", "交实验报告", "拆小", 84), po.Candidate("mood_care", "mood:x", "心情", "陪聊", 50)][:n]
    return po.notice_payload(cands, Settings(), _facts(), AT, _record())


def _offer_request(session, payload, out):
    from test_private_agent import _request

    class PrivBot:
        self_id = "9"

        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_private_msg(self, *, user_id, message):  # noqa: ARG002
            self.sent.append(str(message))

    bot = PrivBot()
    request = _request(session, bot, "[系统通知]")
    request.event = None
    request.extra = {"offer": payload, "offer_out": out}
    return request, bot


def test_offer_turn_asks_strips_marker_and_blocks_doing(maker, monkeypatch) -> None:
    from test_private_agent import _message, _script

    seen: list[dict] = []
    _script(
        monkeypatch,
        [
            _message(calls=[("todos", {"action": "add", "content": "偷偷加一个"})]),
            _message("【提议:1】报告拖了两天啦，要不要我帮你拆成几步，今晚先写个开头？"),
        ],
        seen,
    )
    monkeypatch.setattr(pagent, "now_beijing", lambda: NOW)
    out: dict[str, Any] = {}

    async def go():
        async with maker() as session:
            request, bot = _offer_request(session, _payload(), out)
            text = await pagent.handle_private_turn(request)
            return text, bot

    text, bot = asyncio.run(go())
    assert text and "【提议" not in text and "拆成几步" in text
    assert out["picks"] == [1] and bot.sent
    assert store.load(USER)["todos"] == []  # 只问不做
    tool_result = next(m for m in seen[1]["messages"] if m.get("role") == "tool")
    assert json.loads(tool_result["content"])["error_code"] == "not_allowed"
    system3 = seen[0]["messages"][-1]["content"]
    assert "主动私聊他（他没有发消息）" in system3
    # 和主回合同一份工具表（前缀缓存）
    assert tool_names(seen[0]["tools"]) >= {"offers", "todos", "recall"}


def test_offer_turn_can_skip_and_yields_when_he_speaks(maker, monkeypatch) -> None:
    from test_private_agent import _message, _script

    _script(monkeypatch, [_message("【提议:无】")])
    monkeypatch.setattr(pagent, "now_beijing", lambda: NOW)
    out: dict[str, Any] = {}

    async def go(payload):
        async with maker() as session:
            request, bot = _offer_request(session, payload, out)
            return await pagent.handle_private_turn(request), bot

    text, bot = asyncio.run(go(_payload()))
    assert text == "" and not bot.sent
    # 不许跳过时照样发（把标记去掉）
    _script(monkeypatch, [_message("【提议:无】那我先不打扰啦，有事叫我")])
    text, bot = asyncio.run(go({**_payload(), "may_skip": False}))
    assert text and bot.sent
    # 生成期间他先开口：不发
    _script(monkeypatch, [_message("【提议:2】最近还好吗？")])
    calls = {"n": 0}

    def arrived(_uid, _since):
        calls["n"] += 1
        return True

    monkeypatch.setattr(pagent.turn_taking, "newer_message_arrived", arrived)
    out.clear()
    text, bot = asyncio.run(go(_payload()))
    assert text == "" and out["user_spoke"] and not bot.sent


def test_user_turn_sees_offer_waiting_and_can_resolve(maker, monkeypatch) -> None:
    from test_private_agent import _Bot, _message, _request, _script

    store.update(
        USER,
        lambda rec: po.record_attempt(
            rec,
            "sent",
            NOW - timedelta(hours=1),
            candidates=[po.Candidate("todo_overdue", "todo:1:overdue", "交实验报告", "拆小", 84)],
            picks=[1],
            text="要不要我帮你把报告拆一下？",
        ),
    )
    seen: list[dict] = []
    _script(
        monkeypatch,
        [_message(calls=[("offers", {"action": "resolve", "outcome": "accepted"})]), _message("好嘞，先列个提纲给你")],
        seen,
    )
    monkeypatch.setattr(pagent, "now_beijing", lambda: NOW)

    async def go():
        async with maker() as session:
            return await pagent.handle_private_turn(_request(session, _Bot(), "好啊"))

    assert asyncio.run(go())
    system3 = seen[0]["messages"][-1]["content"]
    assert "offer_waiting" in system3 and "要不要我帮你把报告拆一下" in system3
    assert "use_tool 调 offers（action=resolve" in system3
    row = po.state(store.load(USER))["log"][-1]
    assert row["outcome"] == "accepted" and row["replied_at"]


# ── 7. 调度 ────────────────────────────────────────────────────────────


def _setup_user(monkeypatch, *, last_user=AT - timedelta(hours=5)):
    store.update(USER, lambda rec: rec["todos"].extend(_todos()))

    async def times(_uid):
        return last_user, last_user

    monkeypatch.setattr(turn, "chat_times", times)

    async def friend(_bot, _uid):
        return True

    agent_bridge = importlib.import_module(f"{_PKG}.yawn_agent.agent_bridge")
    monkeypatch.setattr(agent_bridge, "is_friend", friend)


def test_tick_runs_offer_records_and_respects_cheap_skips(monkeypatch) -> None:
    _setup_user(monkeypatch)
    payloads: list[dict] = []

    async def runner(_bot, uid, payload):
        payloads.append(payload)
        return turn.OfferResult("sent", "要我帮你把报告拆一下吗？", [1])

    done = asyncio.run(turn.run_offer_tick(AT, object(), runner=runner))
    assert [row["status"] for row in done] == ["sent"] and done[0]["user_id"] == USER
    assert payloads[0]["things"][0]["what"] == "交实验报告" and payloads[0]["ask_at_most"] == 2
    data = po.state(store.load(USER))
    assert data["log"][-1]["items"][0]["kind"] == "todo_overdue" and data["pending"]
    assert policy.usage_snapshot()["kinds"]["todo_overdue"] == 1
    # 刚问过：下一分钟直接跳过，不再跑回合
    assert asyncio.run(turn.run_offer_tick(AT + timedelta(minutes=1), object(), runner=runner)) == []
    assert len(payloads) == 1
    # 总闸关掉
    monkeypatch.setattr(private_bridge.private_agent_config, "private_agent_offers", False)
    assert asyncio.run(turn.run_offer_tick(AT + timedelta(hours=8), object(), runner=runner)) == []


def test_tick_notes_reason_without_running_and_handles_failures(monkeypatch) -> None:
    _setup_user(monkeypatch, last_user=AT - timedelta(minutes=5))

    async def runner(_bot, _uid, _payload):
        raise AssertionError("不该跑")

    assert asyncio.run(turn.run_offer_tick(AT, object(), runner=runner)) == []
    assert po.state(store.load(USER))["last_check"]["reason"] == "chatting"
    _setup_user(monkeypatch)

    async def failing(_bot, _uid, _payload):
        return turn.OfferResult("failed")

    done = asyncio.run(turn.run_offer_tick(AT, object(), runner=failing))
    assert done[0]["status"] == "failed"
    assert po.state(store.load(USER))["retry_at"] == "2026-11-03 15:30"


def test_offer_once_without_candidates_does_nothing(monkeypatch) -> None:
    async def times(_uid):
        return AT - timedelta(hours=5), AT - timedelta(hours=5)

    monkeypatch.setattr(turn, "chat_times", times)
    store.update(USER, lambda rec: rec["notes"].append({"id": 1, "content": "x", "at": "2026-11-01 10:00"}))
    result = asyncio.run(turn.offer_once(None, USER, AT, runner=None))
    assert result == {"status": "not_now", "reason": "no_candidates", "label": "没找到值得问的事"}
    assert po.state(store.load(USER))["log"] == []


def test_registry_and_sections() -> None:
    assert "offer" in registry.PIPELINE_INDEX
    for section in ("private.offer_turn", "private.offer_notice", "private.offer_waiting"):
        assert prompt_sections.get_section(section) is not None, section
        assert registry.pipelines_of(section), section
    assert prompt_sections.get_section("ptool.offers") is not None
    assert set(ptools.TOOLS_BY_NAME) >= turn.OFFER_ALLOWED_TOOLS
    assert not (turn.OFFER_ALLOWED_TOOLS & ptools.WRITE_TOOLS)


# ── 8. WebUI ───────────────────────────────────────────────────────────


def test_webui_overview_settings_user_detail_and_actions(monkeypatch) -> None:
    _setup_user(monkeypatch)
    monkeypatch.setattr(routes, "now_beijing", lambda: AT)
    monkeypatch.setattr(routes, "_bot", lambda: None)
    overview = asyncio.run(routes.get_offers_overview(ADMIN, limit=100))["data"]
    assert overview["env_on"] and overview["settings"]["interval_hours"] == 3
    assert [row["user_id"] for row in overview["users"]] == [USER]
    assert overview["reasons"]["quiet"] and len(overview["kinds"]) == len(policy.ALL_KINDS)
    saved = asyncio.run(routes.put_offer_settings({"interval_hours": 4, "daily_max": 2}, ADMIN))["data"]
    assert saved["settings"]["interval_hours"] == 4 and policy.load_settings().daily_max == 2
    with pytest.raises(HTTPException) as bad:
        asyncio.run(routes.put_offer_settings({"interval_hours": 0}, ADMIN))
    assert bad.value.status_code == 422

    detail = asyncio.run(routes.get_offer_user(USER, ADMIN))["data"]
    assert detail["gate"]["reason"] == "ok" and detail["candidates"][0]["kind"] == "todo_overdue"
    assert detail["payload"]["things"] and detail["log"] == []

    prefs = asyncio.run(
        routes.put_offer_user_prefs(USER, routes.OfferUserPrefsBody(enabled=False, interval_hours=6, mute=["mood_care"]), ADMIN)
    )["data"]["prefs"]
    assert prefs["on"] is False and prefs["every_hours"] == 6 and prefs["muted"][0]["kind"] == "mood_care"
    prefs = asyncio.run(
        routes.put_offer_user_prefs(USER, routes.OfferUserPrefsBody(follow_default=True, reset_interval=True, unmute=["mood_care"], snooze_hours=2), ADMIN)
    )["data"]["prefs"]
    assert prefs["on"] is True and not prefs["own_choice"] and prefs["paused_until"] == "2026-11-03 17:00" and "muted" not in prefs
    with pytest.raises(HTTPException):
        asyncio.run(routes.put_offer_user_prefs(USER, routes.OfferUserPrefsBody(mute=["nope"]), ADMIN))

    store.update(USER, lambda rec: po.record_attempt(rec, "sent", AT, candidates=[po.Candidate("todo_today", "k", "x", "y", 70)], picks=[1], text="要帮忙吗"))
    detail = asyncio.run(routes.get_offer_user(USER, ADMIN))["data"]
    assert detail["log"][0]["text"] == "要帮忙吗" and detail["cooling"][0]["key"] == "k"
    asyncio.run(routes.post_offer_user_reset(USER, routes.OfferResetBody(), ADMIN))
    data = po.state(store.load(USER))
    assert data["log"] == [] and data["prefs"].get("snooze_until")  # 设置保留
    asyncio.run(routes.post_offer_user_reset(USER, routes.OfferResetBody(clear_prefs=True), ADMIN))
    assert po.state(store.load(USER))["prefs"] == {"muted_kinds": {}}
    with pytest.raises(HTTPException) as offline:
        asyncio.run(routes.post_offer_user_run(USER, ADMIN))
    assert offline.value.status_code == 409
    with pytest.raises(HTTPException):
        asyncio.run(routes.get_offer_user(0, ADMIN))
