"""狼人杀可视化对局日志(game_log)的聚焦测试。

覆盖：内存事件环形队列的 seq/上限/增量读取与清理、引擎埋点
(阶段切换/群播/死亡/计票)、AI 决策与发言的事件记录、
webui events 端点的快照组装与降级。
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path
from typing import Any

import nonebot
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROJECT_ROOT / "src" / "plugins" / "yawn_core"

WOLF_SEAT = 1
SEER_SEAT = 2
VILLAGER_SEAT = 3
OVERFLOW = 30  # 超出环形队列容量的条数
GROUP_LOG = 12


@pytest.fixture(scope="module")
def ww_modules() -> Any:
    """不加载整个 yawn_core，隔离导入狼人杀状态机与 webui 端点。"""
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()
    if nonebot.get_plugin("nonebot_plugin_orm") is None:
        nonebot.load_plugin("nonebot_plugin_orm")
    package = types.ModuleType("yawn_core")
    package.__path__ = [str(PLUGIN_ROOT)]
    sys.modules.setdefault("yawn_core", package)
    ww_package = types.ModuleType("yawn_core.yawn_werewolf")
    ww_package.__path__ = [str(PLUGIN_ROOT / "yawn_werewolf")]
    sys.modules.setdefault("yawn_core.yawn_werewolf", ww_package)
    state = importlib.import_module("yawn_core.yawn_werewolf.state")
    engine = importlib.import_module("yawn_core.yawn_werewolf.engine")
    ai_player = importlib.import_module("yawn_core.yawn_werewolf.ai_player")
    roles = importlib.import_module("yawn_core.yawn_werewolf.roles")
    game_log = importlib.import_module("yawn_core.yawn_werewolf.game_log")
    return types.SimpleNamespace(
        state=state, engine=engine, ai_player=ai_player, roles=roles, game_log=game_log
    )


@pytest.fixture(autouse=True)
def _clean_log(ww_modules: Any) -> None:
    ww_modules.game_log.reset_for_tests()
    yield
    ww_modules.game_log.reset_for_tests()


def _make_game(mod: Any, group_id: int = 401) -> Any:
    players = [
        mod.state.PlayerState(
            WOLF_SEAT, 1, mod.roles.Role.WEREWOLF, mod.roles.Faction.WOLF, is_ai=True
        ),
        mod.state.PlayerState(
            SEER_SEAT, 2, mod.roles.Role.SEER, mod.roles.Faction.GOOD
        ),
        mod.state.PlayerState(
            VILLAGER_SEAT, 3, mod.roles.Role.VILLAGER, mod.roles.Faction.GOOD
        ),
    ]
    game = mod.state.Game(group_id=group_id, host_user_id=1, players=players)
    game.ai_names[1] = "小狼"
    return game


# ── 环形队列语义 ───────────────────────────────────────────


def test_record_events_seq_increment_and_after_seq(ww_modules: Any) -> None:
    log = ww_modules.game_log
    log.record(11, log.TYPE_PHASE, round_no=1, text="SIGNUP")
    log.record(11, log.TYPE_ANNOUNCE, round_no=1, text="公告")
    all_events = log.events(11)
    assert [event.seq for event in all_events] == [1, 2]
    assert [event.text for event in log.events(11, after_seq=1)] == ["公告"]
    assert log.events(999) == []


def test_record_capped_and_clear(ww_modules: Any) -> None:
    log = ww_modules.game_log
    for index in range(log.MAX_EVENTS + OVERFLOW):
        log.record(GROUP_LOG, log.TYPE_ANNOUNCE, text=f"e{index}")
    events = log.events(GROUP_LOG)
    assert len(events) == log.MAX_EVENTS
    # 丢弃最旧,最新一条仍在
    assert events[-1].text == f"e{log.MAX_EVENTS + OVERFLOW - 1}"
    assert events[0].seq == OVERFLOW + 1
    log.clear(GROUP_LOG)
    assert log.events(GROUP_LOG) == []
    # 清理后 seq 重新从 1 开始
    log.record(GROUP_LOG, log.TYPE_SYSTEM, text="restart")
    assert log.events(GROUP_LOG)[0].seq == 1


# ── 引擎埋点 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_engine_instrumentation(ww_modules: Any) -> None:
    mod = ww_modules
    game = _make_game(mod, group_id=402)
    mod.engine._enter_phase(game, mod.state.Phase.DAY_SPEECH)
    await mod.engine._announce(game, "天亮了")
    mod.engine._kill(
        game, game.player_by_seat(VILLAGER_SEAT), mod.roles.DeathCause.VOTED
    )

    events = mod.game_log.events(402)
    by_type = {event.type: event for event in events}
    assert by_type[mod.game_log.TYPE_PHASE].phase == "DAY_SPEECH"
    assert by_type[mod.game_log.TYPE_ANNOUNCE].text == "天亮了"
    death = by_type[mod.game_log.TYPE_DEATH]
    assert death.seat == VILLAGER_SEAT
    assert death.extra["role"] == mod.roles.Role.VILLAGER.value
    assert death.extra["deathCause"] == "VOTED"


@pytest.mark.asyncio
async def test_vote_tally_recorded_with_votes(ww_modules: Any) -> None:
    mod = ww_modules
    game = _make_game(mod, group_id=403)
    game.action_queue.put_nowait(
        mod.state.Action(mod.state.ActionKind.VOTE, 2, WOLF_SEAT)
    )
    game.action_queue.put_nowait(
        mod.state.Action(mod.state.ActionKind.ABSTAIN, 3)
    )
    votes = await mod.engine._collect_votes(
        game,
        type("Cfg", (), {"ww_vote_timeout": 1})(),
        [1, 2, 3],
        mod.state.Phase.DAY_VOTE,
    )
    assert votes[2] == WOLF_SEAT
    assert votes[3] is None
    tally = [e for e in mod.game_log.events(403) if e.type == "vote_tally"]
    assert len(tally) == 1
    assert tally[0].extra["counts"] == {"1": 1.0}
    seat_map = {
        v["voterSeat"]: v["targetSeat"] for v in tally[0].extra["votes"]
    }
    assert seat_map == {SEER_SEAT: WOLF_SEAT, VILLAGER_SEAT: None}


# ── AI 决策 / 发言事件 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_decision_records_context_and_reply(
    ww_modules: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = ww_modules
    game = _make_game(mod, group_id=404)
    driver = mod.ai_player.AIDriver(game)
    mod.ai_player._drivers[game.group_id] = driver
    try:
        async def fake_complete(_messages: Any, **_kwargs: Any) -> str:
            return "验 1 号"

        monkeypatch.setattr(mod.ai_player, "complete", fake_complete)
        seer = game.player_by_seat(SEER_SEAT)
        action = await mod.ai_player._llm_decide(
            driver, seer, "请查验", timeout=5
        )
        assert action is not None
        events = [
            e
            for e in mod.game_log.events(404)
            if e.type == mod.game_log.TYPE_AI_DECISION
        ]
        assert len(events) == 1
        event = events[0]
        assert event.seat == SEER_SEAT
        assert event.text == "验 1 号"
        assert event.extra["instruction"] == "请查验"
        assert "查验" in event.extra["context"] or event.extra["context"]
        assert event.extra["action"]["kind"] == "check"
    finally:
        mod.ai_player._drivers.pop(game.group_id, None)


def test_record_speech_logs_human_speech_without_driver(ww_modules: Any) -> None:
    mod = ww_modules
    game = _make_game(mod, group_id=405)
    # 无 AI 驱动时也必须记录(可视化不依赖 AI 在场)
    assert mod.ai_player._drivers.get(405) is None
    mod.ai_player.record_speech(game, SEER_SEAT, "我昨晚验了1号,是狼!")
    events = mod.game_log.events(405)
    assert len(events) == 1
    assert events[0].type == mod.game_log.TYPE_SPEECH
    assert events[0].seat == SEER_SEAT
    assert events[0].extra["isAi"] is False


# ── webui events 端点 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_events_endpoint_snapshot_and_degradation(
    ww_modules: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # webui 端点用与 test_webui.py 相同的导入路径,避免同组 ORM
    # 模型被两个模块身份重复注册
    games_ep = importlib.import_module("src.plugins.yawn_core.webui.games")
    mod = ww_modules
    group_id = 406
    game = _make_game(mod, group_id=group_id)
    mod.state._games[group_id] = game
    mod.game_log.record(
        group_id, mod.game_log.TYPE_ANNOUNCE, round_no=1, text="hello"
    )
    monkeypatch.setattr(games_ep, "_werewolf_state", lambda: mod.state)
    monkeypatch.setattr(
        games_ep, "_werewolf_game_log", lambda: mod.game_log
    )
    # 直接调用路由函数:ReadSession 仅是依赖注入标记,传 None 即可
    payload = await games_ep.get_werewolf_game_events(
        group_id, None, after_seq=0
    )  # type: ignore[arg-type]
    body = payload["data"]
    assert body["game"]["groupId"] == group_id
    assert body["game"]["currentSpeaker"] is None
    assert [event["text"] for event in body["events"]] == ["hello"]

    from fastapi import HTTPException
    from fastapi import status as http_status

    monkeypatch.setattr(games_ep, "_werewolf_state", lambda: None)
    with pytest.raises(HTTPException) as exc_info:
        await games_ep.get_werewolf_game_events(
            group_id, None, after_seq=0
        )  # type: ignore[arg-type]
    assert (
        exc_info.value.status_code == http_status.HTTP_503_SERVICE_UNAVAILABLE
    )
