# ruff: noqa: ANN001,ANN003,ARG001,E501,PLR2004,TC003
"""群定时任务放宽上限 + 到点 @全体成员 / @其他成员（2026-11）。

复用 test_agent_schedule 的内存库和假 Bot；这里只测新增的行为。
"""

from __future__ import annotations

import importlib
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import test_agent_schedule as base

policy = base.policy
store = base.store
runner = base.runner
handler = base.handler
wake_mod = base.wake_mod
outbound = importlib.import_module(f"{base._PKG}.outbound")
reminder = importlib.import_module("src.plugins.yawn_core.reminder")
Settings = policy.ScheduleSettings
GROUP, ALICE, BOB, NOW = base.GROUP, base.ALICE, base.BOB, base.NOW
CAROL = 203


@pytest.fixture(autouse=True)
def _isolated_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(policy, "STATE_DIR", tmp_path / "agent_schedule")
    policy.reset_settings_cache()
    monkeypatch.setattr(handler, "now_beijing", lambda: NOW)
    yield
    policy.reset_settings_cache()


# ── 上限放宽 ─────────────────────────────────────────────


def test_new_default_limits_fit_seventy_per_member() -> None:
    settings = Settings()
    assert settings.max_active_per_member == 70
    assert settings.max_active_per_group >= settings.max_active_per_member
    assert settings.group_daily_runs >= 300
    assert reminder._MAX_CREATOR_REMINDERS == 70
    assert reminder._MAX_GROUP_REMINDERS >= 70


def test_old_settings_file_with_old_defaults_is_upgraded(tmp_path: Path) -> None:
    path = policy.STATE_DIR / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    # 旧文件：没有 settings_version，上限还是旧默认值；每日次数被管理员改成了 99。
    old = {**Settings().model_dump(), "max_active_per_member": 5, "max_active_per_group": 20, "group_daily_runs": 99}
    old.pop("settings_version")
    path.write_text(json.dumps(old), encoding="utf-8")
    policy.reset_settings_cache()
    loaded = policy.load_settings()
    assert loaded.max_active_per_member == 70
    assert loaded.max_active_per_group == 300
    assert loaded.group_daily_runs == 99  # 改过的不动
    assert loaded.settings_version == policy.SETTINGS_VERSION


def test_new_settings_file_keeps_small_values() -> None:
    payload = {"max_active_per_member": 5, "settings_version": policy.SETTINGS_VERSION}
    assert policy.upgrade_settings_payload(payload) == payload


@pytest.mark.asyncio
async def test_member_can_hold_seventy_tasks_then_hits_limit() -> None:
    engine, factory = await base._session_factory()
    try:
        async with factory() as session:
            for index in range(70):
                await base._create(session, {"kind": "once", "delay_minutes": 30 + index}, instruction=f"提醒 {index}")
            assert await store.count_open(session, GROUP, creator_user_id=ALICE) == 70
            with pytest.raises(store.ScheduleRuleError) as info:
                await base._create(session, {"kind": "once", "delay_minutes": 500}, instruction="第 71 个")
            assert info.value.code == "member_limit"
            # 别人不受他的个人上限影响。
            await base._create(session, {"kind": "once", "delay_minutes": 30}, creator=BOB, instruction="鲍勃的")
    finally:
        await engine.dispose()


# ── @ 目标整理 ───────────────────────────────────────────


def test_normalize_mentions_dedupes_and_accepts_all_words() -> None:
    settings = Settings()
    assert store.normalize_mentions(mention_all=False, user_ids=[BOB, BOB, str(CAROL)], settings=settings) == [BOB, CAROL]
    assert store.normalize_mentions(mention_all=False, user_ids=["全体成员", BOB], settings=settings) == ["all", BOB]
    assert store.normalize_mentions(mention_all=True, user_ids=None, settings=settings) == ["all"]
    with pytest.raises(store.ScheduleRuleError) as info:
        store.normalize_mentions(mention_all=False, user_ids=["小明"], settings=settings)
    assert info.value.code == "bad_mention"
    with pytest.raises(store.ScheduleRuleError) as info:
        store.normalize_mentions(mention_all=False, user_ids=list(range(1, 30)), settings=Settings(max_mention_members=3))
    assert info.value.code == "too_many_mentions"
    with pytest.raises(store.ScheduleRuleError) as info:
        store.normalize_mentions(mention_all=True, user_ids=[], settings=Settings(mention_all_who="off"))
    assert info.value.code == "mention_all_closed"


def test_split_mention_targets_tolerates_null_and_junk() -> None:
    assert store.split_mention_targets(None) == (False, [])
    assert store.split_mention_targets(["all", 5, "x", True, -1, 5]) == (True, [5])


# ── 工具 ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_creates_task_that_mentions_other_members() -> None:
    engine, factory = await base._session_factory()
    try:
        async with factory() as session:
            created = await handler.handle(
                "schedule_task",
                {"instruction": "开会啦", "mode": "notify", "kind": "daily", "times": ["21:00"], "mention_user_ids": [BOB, ALICE]},
                base._context(session, ALICE),
            )
            view = created.result["task"]
            # 设任务的人自己由 mention_me 管，不重复出现在名单里。
            assert view["mention_user_ids"] == [BOB]
            assert "mention_all" not in view
            row = await store.get_task(session, view["task_id"])
            assert row.mention_targets == [BOB]

            with pytest.raises(handler.ToolError) as info:
                await handler.handle(
                    "schedule_task",
                    {"instruction": "提醒陌生人", "kind": "daily", "times": ["21:00"], "mention_user_ids": [999999]},
                    base._context(session, ALICE),
                )
            assert "不是本群已知成员" in str(info.value)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_mention_all_needs_admin_and_bot_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, factory = await base._session_factory()
    args = {"instruction": "全员打卡", "mode": "notify", "kind": "daily", "times": ["09:00"], "mention_all": True}
    try:
        async with factory() as session:
            with pytest.raises(handler.ToolError) as info:
                await handler.handle("schedule_task", args, base._context(session, BOB))
            assert info.value.code == "permission"

            monkeypatch.setattr(handler, "cached_bot_role", lambda *_a: "member")
            with pytest.raises(handler.ToolError) as info:
                await handler.handle("schedule_task", args, base._context(session, BOB, admin=True))
            assert "不是管理员" in str(info.value)

            monkeypatch.setattr(handler, "cached_bot_role", lambda *_a: "admin")
            created = await handler.handle("schedule_task", args, base._context(session, BOB, admin=True))
            assert created.result["task"]["mention_all"] is True

            # 策略放开给所有成员后，普通成员也能设。
            policy.save_settings(Settings(mention_all_who="members"))
            created = await handler.handle("schedule_task", {**args, "times": ["10:00"]}, base._context(session, ALICE))
            assert created.result["task"]["mention_all"] is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_update_keeps_mentions_unless_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler, "cached_bot_role", lambda *_a: None)
    engine, factory = await base._session_factory()
    try:
        async with factory() as session:
            created = await handler.handle(
                "schedule_task",
                {"instruction": "交周报", "kind": "weekly", "weekdays": [5], "times": ["17:00"], "mention_user_ids": [BOB], "mention_all": True},
                base._context(session, ALICE, admin=True),
            )
            task_id = created.result["task"]["task_id"]
            # 只改时间：@ 目标保留；不是管理员的人改时间也不会被 @全体 的权限挡住。
            edited = await handler.handle("update_scheduled_task", {"task_id": task_id, "kind": "weekly", "weekdays": [5], "times": ["18:00"]}, base._context(session, ALICE))
            assert edited.result["task"]["mention_user_ids"] == [BOB]
            assert edited.result["task"]["mention_all"] is True
            cleared = await handler.handle("update_scheduled_task", {"task_id": task_id, "mention_user_ids": [], "mention_all": False}, base._context(session, ALICE))
            assert "mention_user_ids" not in cleared.result["task"]
            assert "mention_all" not in cleared.result["task"]
            # 非管理员不能新加 @全体。
            with pytest.raises(handler.ToolError):
                await handler.handle("update_scheduled_task", {"task_id": task_id, "mention_all": True}, base._context(session, ALICE))
    finally:
        await engine.dispose()


def test_apply_draft_without_mention_fields_keeps_old_targets() -> None:
    task = base._fake_task(raw={"kind": "daily", "times": ["08:00"]}, mention_targets=["all", BOB], creator_user_id=ALICE, mention_creator=True)
    validated = store.validate_draft(store.TaskDraft(instruction="改个说法", schedule={"kind": "daily", "times": ["09:00"]}), settings=Settings(), now=NOW)
    for key in ("instruction", "title", "mode", "description", "failure_count", "lease_until", "updated_at", "revision", "status_reason", "cancelled_at", "cancelled_by"):
        setattr(task, key, getattr(task, key, None))
    store.apply_draft(task, validated, now=NOW)
    assert task.mention_targets == ["all", BOB]


# ── 到点执行 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_notify_run_mentions_all_and_members(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, factory = await base._session_factory()
    bot = base._Bot()
    base._patch_runtime(monkeypatch, factory, bot)
    sent: list[Any] = []

    async def _send(bot_obj, group_id, prepared, **kwargs):
        sent.append(prepared)
        return SimpleNamespace(sent=True, message_id=8001, normalized_text=prepared.normalized_text, segments=prepared.segment_records, may_have_delivered=True, outcome="success")

    monkeypatch.setattr(runner, "send_prepared_outbound", _send)
    try:
        async with factory() as session:
            await base._create(
                session,
                {"kind": "once", "delay_minutes": 30},
                instruction="今晚八点开黑",
                mode="notify",
                mention_all=True,
                mention_user_ids=[BOB],
            )
            await session.commit()
        assert await runner.run_due_tasks(now=NOW + timedelta(minutes=31)) == 1
        assert len(sent) == 1
        records = sent[0].segment_records
        assert records[0]["data"] == {"qq": "all"}
        assert [item["data"]["qq"] for item in records if item["type"] == "at"] == ["all", str(ALICE), str(BOB)]
        assert sent[0].normalized_text.startswith("@全体成员")
        assert "今晚八点开黑" in sent[0].normalized_text
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_run_opens_mentions_and_announces_all(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, factory = await base._session_factory()
    bot = base._Bot()
    base._patch_runtime(monkeypatch, factory, bot)
    seen: dict[str, Any] = {}
    announced: list[Any] = []
    dialogue = importlib.import_module(f"{base._PKG}.dialogue")

    async def _process(bot_obj, event, normalized, **_kwargs):
        seen["wake"] = wake_mod.current_wake()
        seen["normalized"] = normalized

    async def _send(bot_obj, group_id, prepared, **kwargs):
        announced.append(prepared)
        return SimpleNamespace(sent=True, message_id=8002, normalized_text=prepared.normalized_text, segments=prepared.segment_records, may_have_delivered=True, outcome="success")

    monkeypatch.setattr(dialogue, "process_group_message", _process)
    monkeypatch.setattr(runner, "send_prepared_outbound", _send)
    try:
        async with factory() as session:
            await base._create(session, {"kind": "daily", "times": ["08:50"]}, instruction="提醒大家交作业", title="交作业", mention_all=True, mention_user_ids=[BOB])
            await session.commit()
        await runner.run_due_tasks(now=NOW + timedelta(minutes=31))
        assert seen["normalized"].mentions == [ALICE, BOB]
        payload = seen["wake"].prompt_payload()
        assert payload["remind_members"] == [BOB]
        assert "mention_all_sent" in payload
        assert announced and announced[0].segment_records[0]["data"] == {"qq": "all"}
        assert "交作业" in announced[0].normalized_text
    finally:
        await engine.dispose()


def test_degraded_message_drops_mention_all() -> None:
    message = outbound.Message(outbound.MessageSegment.text("提醒"))
    prepared = outbound.PreparedOutboundMessage(
        message=message,
        normalized_text="提醒",
        segment_records=({"type": "at", "data": {"qq": str(BOB)}}, {"type": "text", "data": {"text": "提醒"}, "text": "提醒"}),
    )
    with_all = runner.with_mention_all(prepared)
    assert with_all.segment_records[0]["data"] == {"qq": "all"}
    fallback = outbound._build_degraded_message(with_all, allow_at=True)
    qqs = [item["data"]["qq"] for item in fallback.segment_records if item["type"] == "at"]
    assert qqs == [str(BOB)]


@pytest.mark.asyncio
async def test_old_rows_without_column_value_read_as_no_mentions() -> None:
    engine, factory = await base._session_factory()
    try:
        async with factory() as session:
            task, _ = await base._create(session, {"kind": "daily", "times": ["08:00"]})
            task.mention_targets = None
            await session.commit()
            row = await store.get_task(session, task.id)
            assert store.task_mentions(row) == (False, [])
            assert "mention_user_ids" not in store.task_public_dict(row)
    finally:
        await engine.dispose()
