"""狼人杀审查问题的聚焦回归测试。"""

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


@pytest.fixture(scope="module")
def ww_modules() -> tuple[Any, Any, Any]:
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
    return state, engine, ai_player


def test_dealing_locks_signup_and_rejects_non_member(
    ww_modules: tuple[Any, Any, Any],
) -> None:
    state, engine, _ = ww_modules
    game = state.Game(group_id=1, host_user_id=10, signup_user_ids=[10, 11])

    engine._enter_phase(game, state.Phase.DEALING)

    assert game.phase_token == 1
    assert not state.submit_action(
        game,
        state.Action(state.ActionKind.VOTE, 999, 1),
        user_pending_max=1,
    )
    assert not state.submit_action(
        game,
        state.Action(state.ActionKind.START_GAME, 999),
        user_pending_max=1,
    )
    assert not state.submit_action(
        game,
        state.Action(state.ActionKind.VOTE, 999, 1),
        user_pending_max=1,
        allow_nonmember=True,
    )


def test_ai_late_action_is_dropped(ww_modules: tuple[Any, Any, Any]) -> None:
    state, _, ai_player = ww_modules
    game = state.Game(
        group_id=2,
        host_user_id=-1,
        phase=state.Phase.DAY_VOTE,
        phase_token=4,
        players=[],
    )
    action = state.Action(state.ActionKind.ABSTAIN, -1, phase_token=3)

    assert not ai_player._enqueue_action(game, action)
    assert game.action_queue.empty()


@pytest.mark.asyncio
async def test_hunter_shot_recurses_to_second_hunter(
    ww_modules: tuple[Any, Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    state, engine, _ = ww_modules
    roles = importlib.import_module("yawn_core.yawn_werewolf.roles")
    first = state.PlayerState(1, 1, roles.Role.HUNTER, roles.Faction.GOOD, alive=False)
    second = state.PlayerState(2, 2, roles.Role.HUNTER, roles.Faction.GOOD)
    third = state.PlayerState(3, 3, roles.Role.VILLAGER, roles.Faction.GOOD)
    first.death_cause = roles.DeathCause.VOTED
    game = state.Game(group_id=3, host_user_id=1, players=[first, second, third])
    shots = iter([2, 3])

    async def prompt(*_args: Any) -> int:
        return next(shots)

    async def resolve(game: Any, _cfg: Any, seat: int, _hunter_seat: int) -> None:
        victim = game.player_by_seat(seat)
        victim.alive = False
        victim.death_cause = roles.DeathCause.HUNTER_SHOT

    monkeypatch.setattr(engine, "_hunter_prompt", prompt)
    monkeypatch.setattr(engine, "_resolve_hunter_shot", resolve)

    await engine._resolve_pending_day_effects(game, object(), [first])

    assert not second.alive
    assert not third.alive


@pytest.mark.asyncio
async def test_speech_fallback_is_dropped_after_phase_token_changes(
    ww_modules: tuple[Any, Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    state, _, ai_player = ww_modules
    player = state.PlayerState(1, 1, None, None, is_ai=True)
    game = state.Game(
        group_id=4,
        host_user_id=1,
        phase=state.Phase.DAY_SPEECH,
        phase_token=5,
        players=[player],
        current_speaker=1,
        bot=object(),
    )
    driver = ai_player.AIDriver(game)
    sent: list[str] = []

    async def speech(*_args: object) -> str:
        return "发言"

    async def send(*_args: object) -> bool:
        sent.append(str(_args[-1]))
        if len(sent) == 1:
            game.phase_token += 1
        return False

    monkeypatch.setattr(ai_player, "_llm_speech", speech)
    monkeypatch.setattr(ai_player.api, "safe_group_msg", send)
    await ai_player._do_speech(driver, player)

    assert sent == ["【1号 1号】\n发言"]
    assert game.action_queue.empty()


@pytest.mark.asyncio
async def test_stop_game_discards_task_cancelled_before_first_run(
    ww_modules: tuple[Any, Any, Any],
) -> None:
    state, _, _ = ww_modules
    game = state.create_game(987654321, 123456789)
    assert game is not None

    async def worker() -> None:
        await asyncio.Event().wait()

    game.worker = asyncio.create_task(worker())
    task = game.worker

    await state.stop_game(game)

    assert task.cancelled()
    assert state.get_game(game.group_id) is None
    assert state.game_of_user(game.host_user_id) is None


def test_stale_discard_does_not_remove_replacement(
    ww_modules: tuple[Any, Any, Any],
) -> None:
    state, _, _ = ww_modules
    replacement = state.Game(group_id=55, host_user_id=2, signup_user_ids=[2])
    stale = state.Game(group_id=55, host_user_id=1, signup_user_ids=[1])
    state._games[55] = replacement
    state._user_index[2] = 55

    state.discard_game(stale)

    assert state.get_game(55) is replacement
    assert state.game_of_user(2) is replacement
    state.discard_game(replacement)
