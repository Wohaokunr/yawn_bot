# ruff: noqa: ANN001, ANN002, ANN003, ANN201, DTZ001, F811, PLR2004
"""个人账号「我的空间」3.0：提醒中心、全站搜索、月历、网页签到、成就、数据洞察、
待办批量、清空某一类、网页偏好、聊天记录的跳转与多格式导出。"""

from __future__ import annotations

import importlib
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_webui_me_space import _get, _send, me  # noqa: F401 - pytest fixture
from test_webui_user_accounts import (  # noqa: F401 - pytest fixture
    API,
    G_OWNED,
    NOW,
    OWNER,
    _isolated,
    accounts,
    env,
)

_PKG = "src.plugins.yawn_core"
hub_routes = importlib.import_module(f"{_PKG}.webui.me_hub_routes")
store = importlib.import_module(f"{_PKG}.yawn_agent.private_store")
ChatSession = importlib.import_module(f"{_PKG}.data_models.chat_session").ChatSession
ChatMessage = importlib.import_module(f"{_PKG}.data_models.chat_message").ChatMessage
GroupFeature = importlib.import_module(f"{_PKG}.data_models.group_feature").GroupFeature


@pytest.fixture
def hubme(me, monkeypatch, tmp_path):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_session():
        async with me.factory() as session:
            yield session

    async def no_notify(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(hub_routes, "get_session", fake_session)
    monkeypatch.setattr(hub_routes.hub, "notify_change", no_notify)
    monkeypatch.setenv("AFFINITY_DATA_DIR", str(tmp_path / "affinity"))
    return me


def test_hub_routes_are_user_only(env):
    client = env["client"]
    client.post(f"{API}/auth/login", json={"token": "x" * 40})
    for path in (
        "/me/inbox",
        "/me/search?q=a",
        "/me/calendar",
        "/me/achievements",
        "/me/insights",
        "/me/ui-prefs",
        "/me/checkin/history",
    ):
        assert client.get(f"{API}{path}").status_code == 403, path


def test_inbox_lists_what_needs_attention(hubme):
    today = hub_routes.now_beijing().date()
    _send(
        hubme,
        "POST",
        "/me/assistant/todos",
        {"content": "过期的", "due": (today - timedelta(days=2)).isoformat()},
    )
    _send(
        hubme,
        "POST",
        "/me/assistant/todos",
        {"content": "今天的", "due": today.isoformat()},
    )
    _send(
        hubme,
        "POST",
        "/me/assistant/countdowns",
        {"title": "生日", "date": (today + timedelta(days=2)).isoformat()},
    )
    data = _get(hubme, "/me/inbox")
    kinds = {item["kind"] for item in data["items"]}
    assert {"todo_overdue", "todo_today"} <= kinds
    assert any(kind.startswith("countdown_") for kind in kinds)
    assert data["badges"]["/me/assistant"] == 2
    assert data["items"][0]["tone"] == "danger"
    assert data["total"] >= 2


def test_build_inbox_streak_and_quests():
    now = datetime(2026, 11, 9, 9, 0)
    checkins = [
        SimpleNamespace(
            checked_today=False, streak_days=5, group_name="甲群", group_id=1
        )
    ]
    quests = {
        "enabled": True,
        "items": [{"title": "签到一次", "done": True, "claimed": False}],
    }
    bag = {"shield_until": "2026-11-10", "lucky_today": True}
    data = hub_routes.build_inbox(
        record=store.empty_record(),
        now=now,
        checkins=checkins,
        quests=quests,
        bag=bag,
        paused=True,
    )
    kinds = [item["kind"] for item in data["items"]]
    assert {"streak_risk", "quest_claim", "shield", "lucky", "chat_paused"} <= set(
        kinds
    )
    assert data["badges"] == {"/me/affinity": 1}


def test_search_finds_notes_todos_and_chats(hubme):
    _send(hubme, "POST", "/me/assistant/todos", {"content": "买草莓蛋糕"})
    _send(
        hubme,
        "POST",
        "/me/assistant/notes",
        {"content": "草莓季是 12 月到 4 月", "tag": "吃"},
    )
    _send(hubme, "POST", "/me/assistant/moods", {"score": 5, "note": "吃到草莓了"})

    async def seed():
        async with hubme.factory() as db:
            chat = ChatSession(user_id=OWNER, title="闲聊")
            db.add(chat)
            await db.flush()
            db.add(ChatMessage(session_id=chat.id, role="user", content="你喜欢草莓吗"))
            await db.commit()

    import asyncio

    asyncio.run(seed())
    data = _get(hubme, "/me/search?q=草莓")
    by_kind = {group["kind"]: group for group in data["groups"]}
    assert {"todos", "notes", "moods", "chats"} <= set(by_kind)
    assert by_kind["chats"]["items"][0]["to"].startswith("/me/chats?chat=")
    assert data["total"] == 4
    assert _get(hubme, "/me/search?q=不存在的词")["groups"] == []


def test_calendar_places_items_on_days(hubme):
    today = hub_routes.now_beijing().date()
    _send(
        hubme,
        "POST",
        "/me/assistant/todos",
        {"content": "交报告", "due": today.isoformat()},
    )
    _send(hubme, "POST", "/me/assistant/moods", {"score": 4})
    _send(
        hubme,
        "POST",
        "/me/assistant/countdowns",
        {"title": "今天的日子", "date": today.isoformat()},
    )
    data = _get(hubme, f"/me/calendar?month={today.strftime('%Y-%m')}")
    day = next(row for row in data["days"] if row["date"] == today.isoformat())
    assert day["todos"][0]["content"] == "交报告"
    assert day["mood"] == 4 and day["countdowns"][0]["title"] == "今天的日子"
    assert data["totals"]["todos"] == 1 and len(data["days"]) >= 28
    # 乱写的月份回到本月
    assert _get(hubme, "/me/calendar?month=abc")["month"] == today.strftime("%Y-%m")


def test_web_checkin_signs_every_group_once(hubme):
    first = _send(hubme, "POST", "/me/checkin", {})
    assert first["signed"] >= 1 and first["reward"] > 0
    again = _send(hubme, "POST", "/me/checkin", {})
    assert again["signed"] == 0 and again["already"] == first["signed"]
    history = _get(hubme, "/me/checkin/history")
    assert history["activeDays"] == 1 and history["anyStreak"] == 1
    _send(hubme, "POST", "/me/checkin", {"groupId": "999999"}, expect=403)
    _send(hubme, "POST", "/me/checkin", {"groupId": "abc"}, expect=422)


def test_web_checkin_respects_group_feature_switch(hubme):
    import asyncio

    async def turn_off():
        async with hubme.factory() as db:
            db.add(GroupFeature(group_id=G_OWNED, feature="checkin", enabled=False))
            await db.commit()

    asyncio.run(turn_off())
    result = _send(hubme, "POST", "/me/checkin", {"groupId": str(G_OWNED)})
    assert result["signed"] == 0 and result["disabled"] == 1


def test_todo_batch_bulk_and_clear(hubme):
    bulk = _send(
        hubme,
        "POST",
        "/me/assistant/todos/bulk",
        {"lines": ["- 洗衣服", "  ", "* 拖地", "写周报"], "priority": "low"},
    )
    assert bulk["added"] == 3 and bulk["skipped"] == []
    ids = [item["id"] for item in bulk["items"]]
    done = _send(
        hubme, "POST", "/me/assistant/todos/batch", {"ids": ids[:2], "action": "done"}
    )
    assert done["affected"] == 2 and done["done"] == 2
    later = _send(
        hubme,
        "POST",
        "/me/assistant/todos/batch",
        {"ids": [ids[2]], "action": "postpone", "days": 3},
    )
    row = next(item for item in later["items"] if item["id"] == ids[2])
    assert row["dueInDays"] == 3
    _send(
        hubme,
        "POST",
        "/me/assistant/todos/batch",
        {"ids": [ids[2]], "action": "due", "due": ""},
    )
    _send(
        hubme,
        "POST",
        "/me/assistant/todos/batch",
        {"ids": [ids[2]], "action": "postpone"},
        expect=422,
    )
    _send(
        hubme,
        "POST",
        "/me/assistant/todos/batch",
        {"ids": [98765], "action": "done"},
        expect=404,
    )
    gone = _send(
        hubme, "POST", "/me/assistant/todos/batch", {"ids": ids[:1], "action": "delete"}
    )
    assert gone["done"] == 1
    _send(
        hubme,
        "POST",
        "/me/assistant/clear",
        {"section": "todos", "confirm": "不对"},
        expect=422,
    )
    cleared = _send(
        hubme, "POST", "/me/assistant/clear", {"section": "todos", "confirm": "待办"}
    )
    assert cleared["cleared"] == 2 and store.load(OWNER)["todos"] == []
    # 删掉、清空的已完成待办仍算进成就
    assert store.load(OWNER)["web"]["todos_cleared"] == 2


def test_achievements_unlock_and_stay(hubme):
    _send(hubme, "POST", "/me/assistant/todos", {"content": "第一件"})
    tid = store.load(OWNER)["todos"][0]["id"]
    _send(hubme, "PATCH", f"/me/assistant/todos/{tid}", {"done": True})
    data = _get(hubme, "/me/achievements")
    flat = {item["key"]: item for group in data["groups"] for item in group["items"]}
    assert flat["todo_1"]["done"] and "todo_1" in data["fresh"]
    assert flat["todo_20"]["progress"] == 0.05 and data["total"] == len(
        hub_routes.ACHIEVEMENTS
    )
    _send(hubme, "POST", "/me/assistant/todos/clear-done")
    again = _get(hubme, "/me/achievements")
    flat = {item["key"]: item for group in again["groups"] for item in group["items"]}
    assert (
        flat["todo_1"]["done"] and again["fresh"] == [] and flat["todo_1"]["unlockedAt"]
    )


def test_build_achievements_is_pure():
    payload, known = hub_routes.build_achievements(
        {"bestStreak": 8, "levelIndex": 2}, {}, "2026-11-09"
    )
    assert set(known) == {"streak_7", "level_1", "level_2"}
    assert payload["unlocked"] == 3 and len(payload["next"]) == 3
    payload, known = hub_routes.build_achievements({}, known, "2026-12-01")
    assert payload["unlocked"] == 3 and known["streak_7"] == "2026-11-09"


def test_build_series_counts_by_day():
    today = date(2026, 11, 9)
    record = store.empty_record()
    record["todos"] = [
        {
            "id": 1,
            "content": "a",
            "done": True,
            "at": "2026-11-08 10:00",
            "done_at": "2026-11-09 08:00",
        }
    ]
    record["moods"] = [
        {"at": "2026-11-09 09:00", "score": 4},
        {"at": "2026-11-09 20:00", "score": 2},
    ]
    messages = [
        (datetime(2026, 11, 9, 21, 5), "user"),
        (datetime(2026, 11, 9, 21, 6), "assistant"),
        (datetime(2026, 11, 3, 8, 0), "user"),
    ]
    data = hub_routes.build_series(
        record,
        today,
        14,
        messages=messages,
        checkins={"2026-11-09": 2},
        affinity_by_day={"2026-11-09": 5},
    )
    last = data["series"][-1]
    assert last == {
        "day": "2026-11-09",
        "mine": 1,
        "bot": 1,
        "todosAdded": 0,
        "todosDone": 1,
        "mood": 3.0,
        "checkins": 2,
        "affinity": 5,
    }
    assert (
        data["hours"][21] == 1
        and data["thisWeek"]["mine"] == 2
        and data["lastWeek"]["mine"] == 0
    )
    assert data["bestDay"]["mine"] == 1 and data["busiestHour"] in {8, 21}


def test_insights_endpoint_has_ranks(hubme):
    _send(hubme, "POST", "/me/checkin", {})
    data = _get(hubme, "/me/insights?days=14")
    assert len(data["series"]) == 14 and data["series"][-1]["checkins"] >= 1
    assert data["ranks"] and data["ranks"][0]["pointsRank"] == 1
    assert "userId" not in str(data["ranks"])


def test_ui_prefs_round_trip_and_validation(hubme):
    assert _get(hubme, "/me/ui-prefs")["landing"] == "/me"
    saved = _send(
        hubme,
        "PUT",
        "/me/ui-prefs",
        {
            "homeHidden": ["week"],
            "landing": "/me/calendar",
            "pinnedChats": ["3"],
            "starred": ["/me/games"],
        },
    )
    assert (
        saved["homeHidden"] == ["week"]
        and saved["landing"] == "/me/calendar"
        and saved["pinnedChats"] == ["3"]
    )
    kept = _send(hubme, "PUT", "/me/ui-prefs", {"onboardingDismissed": True})
    assert kept["landing"] == "/me/calendar" and kept["onboardingDismissed"] is True
    _send(hubme, "PUT", "/me/ui-prefs", {"landing": "/overview"}, expect=422)
    _send(hubme, "PUT", "/me/ui-prefs", {"homeHidden": ["nope"]}, expect=422)
    _send(hubme, "PUT", "/me/ui-prefs", {"starred": ["/environment"]}, expect=422)


def test_chat_messages_around_and_export_formats(hubme):
    import asyncio

    async def seed():
        async with hubme.factory() as db:
            chat = ChatSession(user_id=OWNER, title="长对话")
            db.add(chat)
            await db.flush()
            for index in range(30):
                db.add(
                    ChatMessage(
                        session_id=chat.id,
                        role="user" if index % 2 == 0 else "assistant",
                        content=f"第 {index} 句",
                    )
                )
            await db.commit()
            ids = [
                row.id
                for row in (
                    await db.execute(
                        __import__("sqlalchemy")
                        .select(ChatMessage)
                        .where(ChatMessage.session_id == chat.id)
                        .order_by(ChatMessage.id)
                    )
                ).scalars()
            ]
            return chat.id, ids

    chat_id, ids = asyncio.run(seed())
    response = hubme.client.get(
        f"{API}/me/chats/{chat_id}/messages?around={ids[15]}&limit=10"
    )
    body = response.json()
    assert [row["id"] for row in body["data"]] == [str(i) for i in ids[10:20]]
    assert body["meta"]["hasMore"] is True and body["meta"]["hasNewer"] is True
    newer = hubme.client.get(
        f"{API}/me/chats/{chat_id}/messages?after={ids[19]}&limit=50"
    ).json()
    assert len(newer["data"]) == 10 and newer["meta"]["hasNewer"] is False
    md = hubme.client.get(f"{API}/me/chats/{chat_id}/export?format=md")
    assert (
        md.status_code == 200
        and "**我**" in md.text
        and md.headers["content-type"].startswith("text/markdown")
    )
    as_json = hubme.client.get(f"{API}/me/chats/{chat_id}/export?format=json").json()
    assert len(as_json["messages"]) == 30
    assert (
        hubme.client.get(f"{API}/me/chats/{chat_id}/export?format=exe").status_code
        == 422
    )
