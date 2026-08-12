"""RPG engine boundaries that require the loaded NoneBot plugin."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import nonebot
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def rpg_modules() -> tuple[Any, Any]:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()
    if nonebot.get_plugin("yawn_core") is None:
        nonebot.load_from_toml("pyproject.toml")
    state = importlib.import_module("src.plugins.yawn_core.yawn_rpg.state")
    engine = importlib.import_module("src.plugins.yawn_core.yawn_rpg.engine")
    return state, engine


@pytest.mark.asyncio
@pytest.mark.parametrize("speaker_id", [1, 2])
async def test_combat_say_never_enters_kp_or_npc_tool_chain(
    rpg_modules: tuple[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    speaker_id: int,
) -> None:
    state, engine = rpg_modules
    game = state.Game(group_id=1, host_user_id=1, combat_order=[1])
    player = state.PlayerState(user_id=speaker_id, seat=speaker_id)
    action = state.Action(state.ActionKind.SAY, speaker_id, aux="去下一个场景")
    called = False

    async def handle_say(*_args: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(engine, "_handle_say", handle_say)

    await engine._process_action(game, object(), action, player)

    assert not called
