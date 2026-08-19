from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import nonebot
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FAILURE_TIME_COST = 5


@pytest.fixture(scope="module")
def modules() -> tuple[Any, Any, Any]:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()
    if nonebot.get_plugin("yawn_core") is None:
        nonebot.load_from_toml("pyproject.toml")
    return (
        importlib.import_module("src.plugins.yawn_core.yawn_rpg.engine"),
        importlib.import_module("src.plugins.yawn_core.yawn_rpg.state"),
        importlib.import_module("src.plugins.yawn_core.yawn_rpg.module_schema"),
    )


def _module(schema: Any) -> Any:
    return schema.ModuleDef.model_validate(
        {
            "id": "deduction_test",
            "name": "推理测试",
            "opening": "开始",
            "start_scene": "room",
            "scenes": [{"id": "room", "name": "房间", "narration": "房间"}],
            "clues": [
                {"id": "a", "name": "纸条", "text": "甲"},
                {"id": "b", "name": "铜屑", "text": "乙"},
            ],
            "deductions": [
                {
                    "id": "inside",
                    "name": "内侧开门",
                    "required_clues": ["a", "b"],
                    "conclusion_keywords": [["里面", "内侧"], ["开门"]],
                    "success_text": "推理成立",
                    "unlock_flags": ["inside_open"],
                }
            ],
            "endings": [
                {
                    "id": "done",
                    "condition": "deduction:inside",
                    "text": "结束",
                }
            ],
        }
    )


@pytest.mark.asyncio
async def test_single_player_deduction_is_deterministic(
    modules: tuple[Any, Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, state, schema = modules
    game = state.Game(group_id=1, host_user_id=10, phase=state.Phase.PLAY)
    game.module = _module(schema)
    game.current_scene = "room"
    game.explore_round = 1
    game.players = [state.PlayerState(user_id=10, seat=1)]
    game.public_clues.update({"a", "b"})
    announced: list[str] = []

    async def announce(_game: Any, text: object) -> None:
        announced.append(str(text))

    monkeypatch.setattr(engine, "_announce", announce)
    assert await engine._handle_propose_deduction(
        game, game.players[0], "纸条 + 铜屑：从里面开门"
    )
    assert game.completed_deductions == {"inside"}
    assert game.flags["inside_open"] == 1
    assert schema.evaluate_condition("deduction:inside", game.condition_context())
    assert any("推理成立" in line for line in announced)


@pytest.mark.asyncio
async def test_multiplayer_requires_other_player_confirmation(
    modules: tuple[Any, Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, state, schema = modules
    game = state.Game(group_id=2, host_user_id=10, phase=state.Phase.PLAY)
    game.module = _module(schema)
    game.current_scene = "room"
    game.explore_round = 2
    game.players = [
        state.PlayerState(user_id=10, seat=1),
        state.PlayerState(user_id=20, seat=2),
    ]
    game.public_clues.update({"a", "b"})

    async def announce(_game: Any, _text: object) -> None:
        return None

    monkeypatch.setattr(engine, "_announce", announce)
    assert await engine._handle_propose_deduction(
        game, game.players[0], "纸条 + 铜屑：从内侧开门"
    )
    assert not game.completed_deductions
    assert not await engine._handle_confirm_deduction(game, game.players[0])
    assert await engine._handle_confirm_deduction(game, game.players[1])
    assert game.completed_deductions == {"inside"}


@pytest.mark.asyncio
async def test_repeated_failed_deduction_only_costs_time_once(
    modules: tuple[Any, Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, state, schema = modules
    game = state.Game(group_id=5, host_user_id=10, phase=state.Phase.PLAY)
    game.module = _module(schema)
    game.current_scene = "room"
    game.explore_round = 1
    game.players = [state.PlayerState(user_id=10, seat=1)]
    game.public_clues.update({"a", "b"})

    async def announce(_game: Any, _text: object) -> None:
        return None

    monkeypatch.setattr(engine, "_announce", announce)
    for _ in range(2):
        assert await engine._handle_propose_deduction(
            game, game.players[0], "纸条 + 铜屑：也许是风吹的"
        )
    assert game.elapsed_minutes == FAILURE_TIME_COST


def test_new_round_clears_pending_deduction(modules: tuple[Any, Any, Any]) -> None:
    _, state, _ = modules
    game = state.Game(group_id=3, host_user_id=10)
    game.pending_deduction = state.PendingDeduction(10, ("a", "b"), "结论", "room", 1)
    game.start_explore_round(30)
    assert game.pending_deduction is None


def test_clue_board_never_shows_private_clue_body(
    modules: tuple[Any, Any, Any],
) -> None:
    engine, state, schema = modules
    game = state.Game(group_id=4, host_user_id=10)
    game.module = _module(schema)
    game.discovered_clues.add("a")
    game.clue_owners["a"] = {10}
    rendered = engine.clue_board_text(game)
    assert "纸条" not in rendered
    assert "甲" not in rendered


def test_help_text_is_progressive() -> None:
    tutorial = importlib.import_module("src.plugins.yawn_core.yawn_rpg.tutorial")
    assert "/报名" in tutorial.help_text("报名")
    assert "/推理" in tutorial.help_text("推理")
    assert "五步入门" in tutorial.help_text()
