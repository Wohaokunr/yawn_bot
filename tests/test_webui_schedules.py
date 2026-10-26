# ruff: noqa: E501, PLR2004, ARG001, DTZ001
"""WebUI「定时任务」后端：列表/详情、手动新建、乐观锁、动作、批量、预览与策略校验。"""

from __future__ import annotations

import importlib
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nonebot
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()
if nonebot.get_plugin("yawn_core") is None:
    nonebot.load_from_toml("pyproject.toml")

routes = importlib.import_module("src.plugins.yawn_core.webui.schedules")
deps = importlib.import_module("src.plugins.yawn_core.webui.deps")
config = importlib.import_module("src.plugins.yawn_core.webui.config")
policy = importlib.import_module("src.plugins.yawn_core.yawn_agent.schedule_policy")
task_models = importlib.import_module("src.plugins.yawn_core.data_models.agent_scheduled_task")
user_group_models = importlib.import_module("src.plugins.yawn_core.data_models.user_group")
bot_user_models = importlib.import_module("src.plugins.yawn_core.data_models.bot_user")
bot_group_models = importlib.import_module("src.plugins.yawn_core.data_models.bot_group")

BASE = f"{config.API_PATH}/schedules"
NOW = datetime(2026, 10, 26, 8, 20)
GROUP = 123456
ALICE = 20001
ADMIN = SimpleNamespace(role="admin")


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(policy, "STATE_DIR", tmp_path / "agent_schedule")
    policy.reset_settings_cache()
    monkeypatch.setattr(routes, "now_beijing", lambda: NOW)
    notified: list[str] = []

    async def _notify(topic: str) -> None:
        notified.append(topic)

    monkeypatch.setattr(routes.hub, "notify_change", _notify)
    yield
    policy.reset_settings_cache()


async def _factory(monkeypatch: pytest.MonkeyPatch) -> Any:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        bot_group_models.BotGroup.__table__,
        bot_user_models.BotUser.__table__,
        user_group_models.UserGroup.__table__,
        task_models.AgentScheduledTask.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: task_models.AgentScheduledTask.metadata.create_all(sync, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(bot_group_models.BotGroup(group_id=GROUP, group_name="测试群"))
        session.add(bot_user_models.BotUser(user_id=ALICE, nickname="爱丽丝"))
        session.add(user_group_models.UserGroup(group_id=GROUP, user_id=ALICE, group_nickname="爱丽丝"))
        await session.commit()

    @asynccontextmanager
    async def _session():
        async with factory() as session:
            yield session

    monkeypatch.setattr(routes, "get_session", _session)
    return engine


def _body(**overrides: Any) -> Any:
    data = {
        "groupId": str(GROUP),
        "creatorUserId": str(ALICE),
        "instruction": "提醒大家喝水",
        "mode": "notify",
        "schedule": {"kind": "daily", "times": ["09:00", "15:00"]},
    }
    data.update(overrides)
    return routes.TaskBody.model_validate(data)


@pytest.mark.asyncio
async def test_create_list_detail_and_status(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = await _factory(monkeypatch)
    try:
        created = (await routes.schedule_create(_body(), ADMIN))["data"]
        assert created["groupId"] == str(GROUP) and created["creatorName"] == "爱丽丝"
        assert created["sourceKind"] == "manual" and created["nextRunAt"] == "2026-10-26T09:00"
        assert created["upcoming"][:2] == ["2026-10-26T09:00", "2026-10-26T15:00"]

        listed = (await routes.schedule_tasks(ADMIN, group=str(GROUP), status_filter="open", mode=None, creator=None, q="", page=1, page_size=20))["data"]
        assert listed["total"] == 1 and listed["items"][0]["groupName"] == "测试群"
        assert "upcoming" not in listed["items"][0]

        detail = (await routes.schedule_task_detail(created["id"], ADMIN))["data"]
        assert detail["schedule"]["kind"] == "daily" and detail["history"] == []

        status_data = (await routes.schedule_status(ADMIN))["data"]
        assert status_data["ready"] is True and status_data["counts"]["active"] == 1
        assert status_data["groups"] == 1 and status_data["upcoming"][0]["id"] == created["id"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_create_rejects_unknown_member_and_bad_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = await _factory(monkeypatch)
    try:
        with pytest.raises(HTTPException) as stranger:
            await routes.schedule_create(_body(creatorUserId="99999"), ADMIN)
        assert stranger.value.status_code == 422 and "成员" in stranger.value.detail
        with pytest.raises(HTTPException) as bad_rule:
            await routes.schedule_create(_body(schedule={"kind": "interval", "every_minutes": 1}), ADMIN)
        assert bad_rule.value.status_code == 422
        with pytest.raises(HTTPException) as bad_group:
            await routes.schedule_create(_body(groupId="12a"), ADMIN)
        assert bad_group.value.status_code == 400
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_update_uses_revision_and_resets_counts_on_rule_change(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = await _factory(monkeypatch)
    try:
        created = (await routes.schedule_create(_body(), ADMIN))["data"]
        with pytest.raises(HTTPException) as stale:
            await routes.schedule_update(created["id"], _body(revision=created["revision"] + 5), ADMIN)
        assert stale.value.status_code == 409

        updated = (await routes.schedule_update(created["id"], _body(instruction="提醒大家站起来走走", revision=created["revision"]), ADMIN))["data"]
        assert updated["instruction"] == "提醒大家站起来走走" and updated["nextRunAt"] == "2026-10-26T09:00"

        moved = (await routes.schedule_update(created["id"], _body(schedule={"kind": "weekly", "weekdays": [3], "times": ["20:00"]}), ADMIN))["data"]
        assert moved["kind"] == "weekly" and moved["nextRunAt"] == "2026-10-28T20:00" and moved["runCount"] == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_actions_bulk_and_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = await _factory(monkeypatch)
    try:
        first = (await routes.schedule_create(_body(), ADMIN))["data"]
        second = (await routes.schedule_create(_body(instruction="提醒交作业"), ADMIN))["data"]

        paused = (await routes.schedule_action(first["id"], "pause", ADMIN))["data"]
        assert paused["changed"] is True and paused["task"]["status"] == "paused" and paused["task"]["nextRunAt"] is None
        again = (await routes.schedule_action(first["id"], "pause", ADMIN))["data"]
        assert again["changed"] is False
        resumed = (await routes.schedule_action(first["id"], "resume", ADMIN))["data"]
        assert resumed["task"]["status"] == "active" and resumed["task"]["nextRunAt"] == "2026-10-26T09:00"

        bulk = (await routes.schedule_bulk(routes.BulkBody(ids=[first["id"], second["id"], 999], action="cancel"), ADMIN))["data"]
        assert bulk == {"changed": 2}
        detail = (await routes.schedule_task_detail(second["id"], ADMIN))["data"]
        assert detail["status"] == "cancelled" and detail["cancelledAt"] == "2026-10-26T08:20"

        assert (await routes.schedule_delete(first["id"], ADMIN))["data"] == {"deleted": True}
        with pytest.raises(HTTPException) as gone:
            await routes.schedule_task_detail(first["id"], ADMIN)
        assert gone.value.status_code == 404
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_run_now_starts_background_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = await _factory(monkeypatch)
    calls: list[tuple[int, bool]] = []

    async def _execute(task_id: int, *, now: Any = None, force: bool = False) -> None:
        calls.append((task_id, force))

    monkeypatch.setattr(routes, "execute_task", _execute)
    try:
        created = (await routes.schedule_create(_body(), ADMIN))["data"]
        assert (await routes.schedule_action(created["id"], "run", ADMIN))["data"] == {"started": True}
        for job in list(routes._RUNNING):
            await job
        assert calls == [(created["id"], True)]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_runs_timeline_flattens_history(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = await _factory(monkeypatch)
    try:
        created = (await routes.schedule_create(_body(), ADMIN))["data"]
        async with routes.get_session() as session:
            task = await session.get(task_models.AgentScheduledTask, created["id"])
            task.history = [
                {"at": "2026-10-25T09:00", "result": "ok"},
                {"at": "2026-10-25T15:00", "result": "failed", "error": "超时"},
            ]
            await session.commit()
        runs = (await routes.schedule_runs(ADMIN, group=None, result=None, limit=100))["data"]
        assert [item["at"] for item in runs["items"]] == ["2026-10-25T15:00", "2026-10-25T09:00"]
        assert runs["items"][0]["taskId"] == created["id"] and runs["items"][0]["groupName"] == "测试群"
        only_failed = (await routes.schedule_runs(ADMIN, group=None, result="failed", limit=100))["data"]
        assert len(only_failed["items"]) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_missing_table_degrades_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @asynccontextmanager
    async def _session():
        async with async_sessionmaker(engine)() as session:
            yield session

    monkeypatch.setattr(routes, "get_session", _session)
    try:
        assert (await routes.schedule_status(ADMIN))["data"]["ready"] is False
        listed = await routes.schedule_tasks(ADMIN, group=None, status_filter=None, mode=None, creator=None, q="", page=1, page_size=20)
        assert listed["data"] == {"ready": False, "items": [], "total": 0}
        with pytest.raises(HTTPException) as missing:
            await routes.schedule_task_detail(1, ADMIN)
        assert missing.value.status_code == 503
    finally:
        await engine.dispose()


@pytest.fixture
def api() -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[deps.admin_read_session] = lambda: ADMIN
    app.dependency_overrides[deps.admin_write_session] = lambda: ADMIN
    return TestClient(app)


def test_preview_describes_rule_without_saving(api: TestClient) -> None:
    good = api.post(f"{BASE}/preview", json={"schedule": {"kind": "cron", "cron": "0 9 * * 1-5"}}).json()["data"]
    assert good["valid"] is True and good["kind"] == "cron"
    assert good["upcoming"][:2] == ["2026-10-26T09:00", "2026-10-27T09:00"]
    assert len(good["upcoming"]) == 10
    once = api.post(f"{BASE}/preview", json={"schedule": {"kind": "once", "delay_minutes": 30}}).json()["data"]
    assert once["upcoming"] == ["2026-10-26T08:50"] and once["schedule"] == {"kind": "once", "at": "2026-10-26 08:50"}
    bad = api.post(f"{BASE}/preview", json={"schedule": {"kind": "weekly"}}).json()["data"]
    assert bad["valid"] is False and bad["code"]


def test_settings_round_trip_and_validation(api: TestClient) -> None:
    current = api.get(f"{BASE}/settings").json()["data"]
    settings = current["settings"]
    assert settings == current["defaults"] and settings["maxActivePerGroup"] == 20
    settings.update({"groupMode": "allowlist", "allowedGroups": [str(GROUP), ""], "minIntervalMinutes": 30})
    saved = api.put(f"{BASE}/settings", json=settings)
    assert saved.status_code == 200
    assert saved.json()["data"]["settings"]["allowedGroups"] == [str(GROUP)]
    assert policy.load_settings().min_interval_minutes == 30

    bad_group = api.put(f"{BASE}/settings", json={**settings, "blockedGroups": ["abc"]})
    assert bad_group.status_code == 422
    bad_clock = api.put(f"{BASE}/settings", json={**settings, "quietStart": "25:00"})
    assert bad_clock.status_code == 422
