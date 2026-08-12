"""RPG 与狼人杀共享群组/用户占用的回归测试。"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = types.ModuleType("yawn_core")
PACKAGE.__path__ = [str(PLUGIN_ROOT)]
sys.modules.setdefault("yawn_core", PACKAGE)
RPG_PACKAGE = types.ModuleType("yawn_core.yawn_rpg")
RPG_PACKAGE.__path__ = [str(PLUGIN_ROOT / "yawn_rpg")]
sys.modules.setdefault("yawn_core.yawn_rpg", RPG_PACKAGE)
WW_PACKAGE = types.ModuleType("yawn_core.yawn_werewolf")
WW_PACKAGE.__path__ = [str(PLUGIN_ROOT / "yawn_werewolf")]
sys.modules.setdefault("yawn_core.yawn_werewolf", WW_PACKAGE)

from yawn_core import game_registry
from yawn_core.yawn_rpg import state as rpg_state
from yawn_core.yawn_werewolf import state as ww_state


@pytest.fixture(autouse=True)
def clean_game_registries() -> Any:
    game_registry.reset_for_tests()
    yield
    for module in (rpg_state, ww_state):
        for game in list(module._games.values()):
            module.discard_game(game)
    game_registry.reset_for_tests()


def test_games_and_players_cannot_cross_play() -> None:
    rpg = rpg_state.create_game(1001, 11)
    assert rpg is not None

    assert ww_state.create_game(1001, 12) is None
    assert ww_state.create_game(1002, 11) is None

    assert rpg_state.join_signup(rpg, 13)
    assert ww_state.create_game(1003, 13) is None

    rpg_state.discard_game(rpg)
    ww = ww_state.create_game(1001, 12)
    assert ww is not None


def test_werewolf_action_queue_is_bounded_deduplicated_and_released() -> None:
    game = ww_state.Game(
        group_id=2001,
        host_user_id=21,
        action_queue=asyncio.Queue(maxsize=2),
    )
    first = ww_state.Action(ww_state.ActionKind.KILL, 21, 2)
    assert ww_state.submit_action(game, first, user_pending_max=1)
    assert not ww_state.submit_action(
        game,
        ww_state.Action(ww_state.ActionKind.KILL, 21, 2),
        user_pending_max=1,
    )
    assert not ww_state.submit_action(
        game,
        ww_state.Action(ww_state.ActionKind.CHECK, 21, 3),
        user_pending_max=1,
    )
    second = ww_state.Action(ww_state.ActionKind.VOTE, 22, 3)
    assert ww_state.submit_action(game, second, user_pending_max=1)
    assert not ww_state.submit_action(
        game,
        ww_state.Action(ww_state.ActionKind.SKIP, 23),
        user_pending_max=1,
    )

    consumed = game.action_queue.get_nowait()
    game.action_queue.task_done()
    ww_state.release_action(game, consumed)
    assert ww_state.submit_action(
        game,
        ww_state.Action(ww_state.ActionKind.SKIP, 23),
        user_pending_max=1,
    )
