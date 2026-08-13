"""狼人杀完整流程体验修复的聚焦回归测试。"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path
from typing import Any

import nonebot
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROJECT_ROOT / "src" / "plugins" / "yawn_core"
TARGET_SEAT = 2


@pytest.fixture(scope="module")
def ww_modules() -> tuple[Any, Any, Any, Any]:
    """不加载整个 yawn_core，隔离导入狼人杀状态机。"""
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
    roles = importlib.import_module("yawn_core.yawn_werewolf.roles")
    config = importlib.import_module("yawn_core.yawn_werewolf.config")
    return state, engine, roles, config


async def _async_none(*_args: object, **_kwargs: object) -> None:
    return None


async def _async_true(*_args: object, **_kwargs: object) -> bool:
    return True


def _patch_short_game(
    monkeypatch: pytest.MonkeyPatch,
    engine: Any,
    roles: Any,
) -> None:
    """把昼夜循环缩成一轮，保留报名、发牌和收尾的真实流程。"""

    async def no_deaths(*_args: object, **_kwargs: object) -> list[Any]:
        return []

    async def good_wins(*_args: object, **_kwargs: object) -> Any:
        return roles.Faction.GOOD

    monkeypatch.setattr(engine.ai_player, "start_driver", lambda _game: None)
    monkeypatch.setattr(engine.ai_player, "stop_driver", _async_none)
    monkeypatch.setattr(engine.api, "is_bot_admin", _async_true)
    monkeypatch.setattr(engine.api, "cleanup_group", _async_none)
    monkeypatch.setattr(engine, "_persist_start", _async_none)
    monkeypatch.setattr(engine, "_run_night", no_deaths)
    monkeypatch.setattr(engine, "_run_day", good_wins)
    monkeypatch.setattr(engine, "_finish", _async_none)


@pytest.mark.asyncio
async def test_sheriff_final_speech_and_revote_are_distinct_phases(
    ww_modules: tuple[Any, Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, engine, roles, config = ww_modules
    players = [
        state.PlayerState(1, 1, roles.Role.VILLAGER, roles.Faction.GOOD),
        state.PlayerState(2, 2, roles.Role.VILLAGER, roles.Faction.GOOD),
    ]
    for player in players:
        player.sheriff_candidate = True
    game = state.Game(group_id=201, host_user_id=1, players=players)
    entered: list[Any] = []
    notified: list[Any] = []
    vote_round = 0

    class ExpiredTimer:
        def __init__(self, _deadline: float) -> None:
            pass

        @staticmethod
        def remaining() -> float:
            return 0

    async def announce(*_args: object, **_kwargs: object) -> None:
        return None

    async def speech(
        current_game: Any,
        _cfg: Any,
        _order: list[Any],
        phase: Any,
        **_kwargs: object,
    ) -> None:
        entered.append(phase)
        engine._enter_phase(current_game, phase)

    async def votes(
        current_game: Any,
        _cfg: Any,
        _targets: list[int],
        phase: Any,
        **_kwargs: object,
    ) -> dict[int, int]:
        nonlocal vote_round
        vote_round += 1
        entered.append(phase)
        engine._enter_phase(current_game, phase)
        return {1: 1, 2: 2} if vote_round == 1 else {1: 2, 2: 2}

    monkeypatch.setattr(engine, "_Timer", ExpiredTimer)
    monkeypatch.setattr(engine, "_announce", announce)
    monkeypatch.setattr(engine, "_speech_rotation", speech)
    monkeypatch.setattr(engine, "_collect_votes", votes)
    monkeypatch.setattr(
        engine.ai_player,
        "on_phase_change",
        lambda current_game: notified.append(current_game.phase),
    )

    await engine._sheriff_campaign(game, config.Config())

    assert entered == [
        state.Phase.SHERIFF_SPEECH,
        state.Phase.SHERIFF_VOTE,
        state.Phase.SHERIFF_FINAL_SPEECH,
        state.Phase.SHERIFF_REVOTE,
    ]
    assert notified[-2:] == [
        state.Phase.SHERIFF_FINAL_SPEECH,
        state.Phase.SHERIFF_REVOTE,
    ]
    token = game.phase_token
    engine._enter_phase(game, state.Phase.SHERIFF_REVOTE)
    assert game.phase_token == token
    assert state.Phase.SHERIFF_FINAL_SPEECH in state.SELF_DETONATE_PHASES


@pytest.mark.asyncio
async def test_elder_enters_own_phase_before_accepting_action(
    ww_modules: tuple[Any, Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, engine, roles, config = ww_modules
    elder = state.PlayerState(
        1,
        1,
        roles.Role.SILENT_ELDER,
        roles.Faction.GOOD,
    )
    target = state.PlayerState(2, 2, roles.Role.VILLAGER, roles.Faction.GOOD)
    game = state.Game(
        group_id=202,
        host_user_id=1,
        board="禁言骑士",
        phase=state.Phase.NIGHT_SEER,
        players=[elder, target],
    )
    game.action_queue.put_nowait(state.Action(state.ActionKind.SILENCE, 1, 2))

    async def dm(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(engine, "_dm", dm)

    await engine._phase_elder(game, config.Config(ww_night_timeout=1))

    assert game.phase is state.Phase.NIGHT_ELDER
    assert game.silenced_seat == TARGET_SEAT
    assert elder.elder_last_target == TARGET_SEAT


@pytest.mark.asyncio
async def test_all_wolves_can_explicitly_skip_without_waiting_for_timeout(
    ww_modules: tuple[Any, Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, engine, roles, config = ww_modules
    wolves = [
        state.PlayerState(1, 1, roles.Role.WEREWOLF, roles.Faction.WOLF),
        state.PlayerState(2, 2, roles.Role.WEREWOLF, roles.Faction.WOLF),
    ]
    villager = state.PlayerState(3, 3, roles.Role.VILLAGER, roles.Faction.GOOD)
    game = state.Game(group_id=203, host_user_id=1, players=[*wolves, villager])
    for wolf in wolves:
        game.action_queue.put_nowait(
            state.Action(state.ActionKind.SKIP, wolf.user_id),
        )
    private_messages: list[str] = []

    async def dm(_game: Any, _player: Any, text: str) -> bool:
        private_messages.append(text)
        return True

    monkeypatch.setattr(engine, "_dm", dm)

    kill_seat = await asyncio.wait_for(
        engine._phase_wolves(game, config.Config(ww_wolf_timeout=30)),
        timeout=0.5,
    )

    assert kill_seat is None
    assert any("已响应 2/2" in text for text in private_messages)
    assert any("全员已响应，本夜空刀" in text for text in private_messages)


@pytest.mark.asyncio
async def test_repeated_vote_is_rejected_with_visible_feedback(
    ww_modules: tuple[Any, Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, engine, roles, config = ww_modules
    players = [
        state.PlayerState(uid, uid, roles.Role.VILLAGER, roles.Faction.GOOD)
        for uid in (1, 2, 3)
    ]
    game = state.Game(group_id=204, host_user_id=1, players=players)
    for action in (
        state.Action(state.ActionKind.VOTE, 1, 2),
        state.Action(state.ActionKind.VOTE, 1, 3),
        state.Action(state.ActionKind.VOTE, 2, 3),
        state.Action(state.ActionKind.ABSTAIN, 3),
    ):
        game.action_queue.put_nowait(action)
    announcements: list[str] = []

    async def announce(_game: Any, text: str) -> None:
        announcements.append(text)

    monkeypatch.setattr(engine, "_announce", announce)
    monkeypatch.setattr(engine, "_unban_all_players", _async_none)

    votes = await engine._collect_votes(
        game,
        config.Config(ww_vote_timeout=1),
        [1, 2, 3],
        state.Phase.DAY_VOTE,
    )

    assert votes[1] == TARGET_SEAT
    assert "1号 已完成本轮投票，本轮不可改票" in announcements


@pytest.mark.asyncio
async def test_signup_limits_follow_board_switch_before_manual_start(
    ww_modules: tuple[Any, Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, engine, roles, config = ww_modules
    game = state.Game(
        group_id=205,
        host_user_id=1,
        signup_user_ids=list(range(1, 10)),
        bot=object(),
    )
    announcements: list[str] = []
    dealt_seats: list[int] = []
    calls = 0

    async def announce(_game: Any, text: object) -> None:
        announcements.append(str(text))

    async def next_action(current_game: Any, _step: float) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            current_game.board = "禁言骑士"
            return state.Action(state.ActionKind.START_GAME, 1)
        current_game.signup_user_ids.extend((10, 11, 12))
        return state.Action(state.ActionKind.START_GAME, 1)

    async def dm(_game: Any, player: Any, _text: str) -> bool:
        dealt_seats.append(player.seat)
        return True

    _patch_short_game(monkeypatch, engine, roles)
    monkeypatch.setattr(engine, "config", config.Config(ww_signup_timeout=30))
    monkeypatch.setattr(engine, "_announce", announce)
    monkeypatch.setattr(engine, "_get_action", next_action)
    monkeypatch.setattr(engine, "_dm", dm)

    await engine.run_game(game)

    assert any("当前仅 9 人报名，至少需要 12 人" in text for text in announcements)
    assert dealt_seats == list(range(1, 13))
    assert len(game.players) == len(dealt_seats)


@pytest.mark.asyncio
async def test_board_switch_to_config_conflict_ends_safely(
    ww_modules: tuple[Any, Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, engine, roles, config = ww_modules
    game = state.Game(
        group_id=206,
        host_user_id=1,
        signup_user_ids=list(range(1, 10)),
        bot=object(),
    )
    announcements: list[str] = []

    async def announce(_game: Any, text: object) -> None:
        announcements.append(str(text))

    async def switch_board(current_game: Any, _step: float) -> None:
        current_game.board = "禁言骑士"

    _patch_short_game(monkeypatch, engine, roles)
    monkeypatch.setattr(
        engine,
        "config",
        config.Config(
            ww_max_players=11,
            ww_signup_timeout=30,
        ),
    )
    monkeypatch.setattr(engine, "_announce", announce)
    monkeypatch.setattr(engine, "_get_action", switch_board)

    await engine.run_game(game)

    assert game.phase is state.Phase.ENDED
    assert game.players == []
    assert any("配置冲突" in text and "禁言骑士" in text for text in announcements)


@pytest.mark.asyncio
async def test_role_card_summary_reports_delivery_failures(
    ww_modules: tuple[Any, Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, engine, roles, config = ww_modules
    game = state.Game(
        group_id=207,
        host_user_id=1,
        signup_user_ids=list(range(1, 10)),
        bot=object(),
    )
    announcements: list[str] = []

    async def announce(_game: Any, text: object) -> None:
        announcements.append(str(text))

    async def start(_game: Any, _step: float) -> Any:
        return state.Action(state.ActionKind.START_GAME, 1)

    async def dm(_game: Any, player: Any, _text: str) -> bool:
        return player.seat not in (2, 5)

    _patch_short_game(monkeypatch, engine, roles)
    monkeypatch.setattr(engine, "config", config.Config(ww_signup_timeout=30))
    monkeypatch.setattr(engine, "_announce", announce)
    monkeypatch.setattr(engine, "_get_action", start)
    monkeypatch.setattr(engine, "_dm", dm)

    await engine.run_game(game)

    summary = next(text for text in announcements if "身份卡投递" in text)
    assert "成功 7 人" in summary
    assert "失败 2 人（2号、5号）" in summary
    assert "私聊发送 /身份 重取" in summary
    assert not any("身份已私聊下发" in text for text in announcements)
