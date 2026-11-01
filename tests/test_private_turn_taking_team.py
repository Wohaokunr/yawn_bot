# ruff: noqa: ANN001, ARG001, ARG005, E501, PLR2004, F811, PLW0108
"""私聊发言系统（不回 / 等他说完 / 让给下一轮）与团队协作（留言板、多成员项目、
依赖接力、主智能体收发消息）、子智能体上下文压缩。"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any

import pytest
from test_private_agent import (  # noqa: F401
    NOW,
    USER,
    _Bot,
    _message,
    _request,
    _script,
    maker,
    pagent,
)

_PKG = "src.plugins.yawn_core"
turn = importlib.import_module(f"{_PKG}.yawn_agent.private_turn_taking")
team = importlib.import_module(f"{_PKG}.yawn_agent.private_team")
ptasks = importlib.import_module(f"{_PKG}.yawn_agent.private_tasks")
worker = importlib.import_module(f"{_PKG}.yawn_agent.private_worker")
store = importlib.import_module(f"{_PKG}.yawn_agent.private_store")
chat_state = importlib.import_module(f"{_PKG}.chat_state")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVATE_AGENT_DATA_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("PRIVATE_AGENT_STATE_PATH", str(tmp_path / "state.json"))
    turn.reset_state_for_tests()
    team.reset_state_for_tests()
    ptasks.reset_state_for_tests()
    yield
    turn.reset_state_for_tests()
    team.reset_state_for_tests()
    ptasks.reset_state_for_tests()


# ── 标记解析与让步计数 ─────────────────────────────────────────


def test_parse_silence_marks() -> None:
    assert turn.parse_silence("【不回】").kind == "skip"
    assert turn.parse_silence("【节奏:一条】【等他说完】").kind == "wait"
    assert turn.parse_silence("  【不回】 ||| ").kind == "skip"
    spoken = turn.parse_silence("【不回】其实还是想说一句")
    assert spoken.kind == "" and spoken.text == "其实还是想说一句"
    plain = turn.parse_silence("好的呀")
    assert plain.kind == "" and plain.text == "好的呀"
    assert turn.parse_silence("").kind == ""


def test_yield_streak_is_capped() -> None:
    assert turn.may_yield(USER, now=0.0)
    for _ in range(turn.MAX_YIELDS):
        turn.record_yield(USER, now=1.0)
    assert not turn.may_yield(USER, now=2.0)
    turn.record_spoke(USER)
    assert turn.may_yield(USER, now=3.0)
    turn.record_yield(USER, now=0.0)
    assert not turn.may_yield(USER, now=turn.MAX_YIELD_SPAN_SECONDS + 1)


def test_arrival_counter_tracks_enqueue() -> None:
    before = chat_state.arrival_count(USER)
    assert turn.newer_message_arrived(USER, before) is False
    assert turn.newer_message_arrived(USER, before - 1) is True


# ── 回合里的不说话 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_turn_skip_sends_nothing(maker, monkeypatch) -> None:
    _script(monkeypatch, [_message("【不回】")])
    monkeypatch.setattr(pagent, "now_beijing", lambda: NOW)
    async with maker() as session:
        bot = _Bot()
        text = await pagent.handle_private_turn(_request(session, bot, text="嗯嗯"))
    assert text == "" and bot.sent == []


@pytest.mark.asyncio
async def test_turn_wait_schedules_resume(maker, monkeypatch) -> None:
    waits: list[tuple[int, float]] = []
    monkeypatch.setattr(turn, "schedule_wait", lambda bot, uid, *, seconds, since: waits.append((uid, seconds)))
    _script(monkeypatch, [_message("【等他说完】")])
    monkeypatch.setattr(pagent, "now_beijing", lambda: NOW)
    async with maker() as session:
        bot = _Bot()
        text = await pagent.handle_private_turn(_request(session, bot, text="问你个事"))
    assert text == "" and bot.sent == []
    assert waits and waits[0][0] == USER and waits[0][1] > 0


@pytest.mark.asyncio
async def test_turn_yields_when_newer_message_arrived(maker, monkeypatch) -> None:
    _script(monkeypatch, [_message("你说的是哪个？")])
    monkeypatch.setattr(pagent, "now_beijing", lambda: NOW)
    monkeypatch.setattr(turn, "newer_message_arrived", lambda uid, since: True)
    async with maker() as session:
        bot = _Bot()
        text = await pagent.handle_private_turn(_request(session, bot, text="那个"))
    assert text == "" and bot.sent == []
    # 让太多次以后照常回复
    for _ in range(turn.MAX_YIELDS):
        turn.record_yield(USER)
    _script(monkeypatch, [_message("好，我来看看")])
    async with maker() as session:
        bot = _Bot()
        text = await pagent.handle_private_turn(_request(session, bot, text="那个"))
    assert bot.sent == ["好，我来看看"]


@pytest.mark.asyncio
async def test_turn_taking_off_always_replies(maker, monkeypatch) -> None:
    monkeypatch.setattr(turn, "enabled", lambda: False)
    _script(monkeypatch, [_message("【不回】")])
    monkeypatch.setattr(pagent, "now_beijing", lambda: NOW)
    async with maker() as session:
        bot = _Bot()
        await pagent.handle_private_turn(_request(session, bot, text="嗯"))
    # 关掉后标记不被当成沉默；正文为空时交给原来的兜底（不发空消息）
    assert all("【不回】" not in item for item in bot.sent)


# ── 团队留言板 ─────────────────────────────────────────────────


def _seed_tasks(*rows: dict[str, Any]) -> None:
    def add(record):
        record["tasks"].extend(rows)

    store.update(USER, add)


def test_team_mail_routing_and_unread() -> None:
    _seed_tasks(
        {"id": "t1", "status": "running", "project": "P1", "member": "调研"},
        {"id": "t2", "status": "running", "project": "P1", "member": "写作"},
        {"id": "t3", "status": "running"},
    )
    sent = team.post(USER, "t1", "写作", "资料在 out/notes.md")
    assert sent["sent_to"] == ["写作（t2）"]
    broadcast = team.post(USER, "t1", "all", "我这边做完了")
    assert "写作（t2）" in broadcast["sent_to"] and all("t3" not in x for x in broadcast["sent_to"])
    inbox = team.unread(USER, "t2")
    assert [m["text"] for m in inbox] == ["资料在 out/notes.md", "我这边做完了"]
    assert team.unread(USER, "t2") == []
    team.post(USER, team.MAIN, "t3", "他补充：要表格")
    assert team.unread(USER, "t3", mark=False)[0]["from"] == "主助手"
    with pytest.raises(Exception, match="找不到收件人"):
        team.post(USER, "t1", "不存在的人", "hi")


def test_helper_question_to_main_triggers_coordination(monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(team, "_schedule_coordination", lambda uid: calls.append(uid))
    _seed_tasks({"id": "t1", "status": "running"})
    team.post(USER, "t1", "main", "要中文还是英文？", need_reply=True)
    team.post(USER, "t1", "main", "顺便同步一下进度")
    assert calls == [USER]
    assert len(team.unread(USER, team.MAIN)) == 2


# ── 多成员项目 ─────────────────────────────────────────────────


def _members() -> list[dict[str, Any]]:
    return [
        {"name": "调研", "goal": "查三家方案的价格", "tools": ["web"]},
        {"name": "分析", "goal": "做对比表", "tools": ["computer"], "depends_on": ["调研"]},
        {"name": "写作", "goal": "写报告", "tools": ["toolbox"], "depends_on": ["调研", "分析"]},
    ]


def test_project_rejects_cycles_and_bad_members() -> None:
    with pytest.raises(Exception, match="环"):
        ptasks._clean_members(
            [
                {"name": "a", "goal": "x", "tools": ["web"], "depends_on": ["b"]},
                {"name": "b", "goal": "y", "tools": ["web"], "depends_on": ["a"]},
            ]
        )
    with pytest.raises(Exception, match="2~"):
        ptasks._clean_members([{"name": "a", "goal": "x", "tools": ["web"]}])
    with pytest.raises(Exception, match="tools"):
        ptasks._clean_members(
            [{"name": "a", "goal": "x", "tools": ["root"]}, {"name": "b", "goal": "y", "tools": ["web"]}]
        )


@pytest.mark.asyncio
async def test_project_runs_members_in_dependency_order(monkeypatch) -> None:
    spawned: list[str] = []
    monkeypatch.setattr(ptasks, "_spawn", lambda uid, task_id, **kw: spawned.append(task_id))
    monkeypatch.setattr(ptasks, "tasks_enabled", lambda: True)
    reports: list[str] = []

    async def fake_report(uid, project_id):
        reports.append(project_id)
        return True

    monkeypatch.setattr(ptasks, "deliver_project_report", fake_report)
    started = ptasks.start_project(
        SimpleNamespace(), USER, title="选方案", goal="出对比报告", brief="预算 1 万", members=_members(), save_to_notes=False, now=NOW
    )
    ids = {row["name"]: row["task_id"] for row in started["members"]}
    assert spawned == [ids["调研"]]
    assert {row["name"]: row["status"] for row in started["members"]}["写作"] == "等队友"

    ptasks._update_task(USER, ids["调研"], status="done", summary="A 贵 B 便宜", details="明细……")
    await ptasks._advance_project(USER, started["project_id"])
    assert spawned == [ids["调研"], ids["分析"]]
    brief = ptasks.get_task(USER, ids["分析"])["brief"]
    assert "调研 的成果" in brief and "A 贵 B 便宜" in brief

    ptasks._update_task(USER, ids["分析"], status="failed", error="表格没做出来")
    await ptasks._advance_project(USER, started["project_id"])
    assert spawned[-1] == ids["写作"]
    assert "分析 没做成" in ptasks.get_task(USER, ids["写作"])["brief"]
    assert reports == []

    ptasks._update_task(USER, ids["写作"], status="done", summary="报告好了")
    await ptasks._advance_project(USER, started["project_id"])
    assert reports == [started["project_id"]]
    summary = ptasks.project_summary(USER, started["project_id"])
    assert summary is not None


def test_main_can_message_helpers() -> None:
    _seed_tasks({"id": "t5", "status": "running", "member": "写作", "project": "P9"})
    result = ptasks.message_task(None, USER, "写作", "他说要加一段总结")
    assert result["sent_to"] == ["写作（t5）"]
    assert team.unread(USER, "t5")[0]["text"] == "他说要加一段总结"


# ── 子智能体上下文压缩 ─────────────────────────────────────────


def test_compact_history_only_shrinks_old_tool_results() -> None:
    history: list[dict[str, Any]] = [{"role": "system", "content": "s" * 100}]
    for index in range(20):
        history.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"c{index}"}]})
        history.append({"role": "tool", "tool_call_id": f"c{index}", "content": f"结果 sandbox:out/f{index}.png " + "x" * 5000})
    compacted = worker.compact_history(history, budget=40000)
    assert compacted > 0
    assert history[0]["content"] == "s" * 100
    old = history[2]["content"]
    assert old.startswith("[早先的结果已压缩") and "sandbox:out/f0.png" in old
    assert len(history[-1]["content"]) > 5000  # 最近的不动
    assert worker.compact_history(history, budget=10**9) == 0


# ── 人性：记得上次聊到哪 ───────────────────────────────────────


@pytest.mark.asyncio
async def test_context_mentions_last_chat_after_a_gap(maker) -> None:
    from datetime import timedelta

    from test_private_agent import ChatMessage, ChatSession

    pctx = importlib.import_module(f"{_PKG}.yawn_agent.private_context")
    async with maker() as session:
        chat = ChatSession(user_id=USER, group_id=None)
        session.add(chat)
        await session.flush()
        session.add(ChatMessage(session_id=chat.id, role="assistant", content="那周末去看猫展吧", created_at=NOW - timedelta(days=2)))
        await session.flush()
        ctx = await pctx.build_private_context(session, USER, bot_id=9, nickname="小明", now=NOW)
        assert ctx.payload["last_chat"] == {"ago": "2 天前", "you_said": "那周末去看猫展吧"}
        recent = await pctx.last_chat(session, USER, NOW - timedelta(days=2) + timedelta(minutes=5))
        assert recent is None
