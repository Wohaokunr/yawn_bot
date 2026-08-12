"""跑团多人机制的纯状态回归测试。"""

import sys
import types
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = types.ModuleType("yawn_core")
PACKAGE.__path__ = [str(PLUGIN_ROOT)]
sys.modules.setdefault("yawn_core", PACKAGE)
RPG_PACKAGE = types.ModuleType("yawn_core.yawn_rpg")
RPG_PACKAGE.__path__ = [str(PLUGIN_ROOT / "yawn_rpg")]
sys.modules.setdefault("yawn_core.yawn_rpg", RPG_PACKAGE)

from yawn_core.yawn_rpg import dice
from yawn_core.yawn_rpg.dice import CheckTier, skill_check
from yawn_core.yawn_rpg.module_schema import CheckMode, CheckPoint, load_modules
from yawn_core.yawn_rpg.state import (
    Action,
    ActionKind,
    Game,
    Phase,
    SubmitResult,
    release_action,
    submit_action,
)


@pytest.mark.asyncio
async def test_action_backpressure_and_release() -> None:
    game = Game(group_id=1, host_user_id=10)
    action = Action(ActionKind.JOIN_GAME, 20, expected_phase=Phase.SIGNUP)

    assert (
        submit_action(
            game,
            action,
            queue_max=1,
            user_pending_max=1,
            user_say_pending_max=1,
        )
        is SubmitResult.ACCEPTED
    )
    assert (
        submit_action(
            game,
            Action(ActionKind.JOIN_GAME, 21, expected_phase=Phase.SIGNUP),
            queue_max=1,
            user_pending_max=1,
            user_say_pending_max=1,
        )
        is SubmitResult.QUEUE_FULL
    )
    consumed = game.action_queue.get_nowait()
    game.action_queue.task_done()
    release_action(game, consumed)
    assert not game.pending_actions


@pytest.mark.asyncio
async def test_action_rejects_stale_phase_and_user_overflow() -> None:
    game = Game(group_id=1, host_user_id=10)
    assert (
        submit_action(
            game,
            Action(ActionKind.SAY, 10, expected_phase=Phase.PLAY),
            queue_max=10,
            user_pending_max=1,
            user_say_pending_max=1,
        )
        is SubmitResult.STALE
    )
    assert (
        submit_action(
            game,
            Action(ActionKind.SAY, 10, expected_phase=Phase.SIGNUP),
            queue_max=10,
            user_pending_max=1,
            user_say_pending_max=1,
        )
        is SubmitResult.ACCEPTED
    )
    # SAY 满额合并，而不会再增长队列；确定性动作仍受单用户配额限制。
    assert (
        submit_action(
            game,
            Action(ActionKind.SAY, 10, aux="补充", expected_phase=Phase.SIGNUP),
            queue_max=10,
            user_pending_max=1,
            user_say_pending_max=1,
        )
        is SubmitResult.ACCEPTED
    )
    assert game.action_queue.qsize() == 1
    assert (
        submit_action(
            game,
            Action(ActionKind.CHECK, 10, expected_phase=Phase.SIGNUP),
            queue_max=10,
            user_pending_max=0,
            user_say_pending_max=1,
        )
        is SubmitResult.USER_LIMIT
    )


def test_bonus_dice_uses_lowest_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    # 个位 7，三个十位依次为 8/1/4，候选为 87/17/47，应取 17。
    values = iter([7, 8, 1, 4])
    monkeypatch.setattr(dice.random, "randint", lambda *_: next(values))
    result = skill_check(50, bonus_dice=2)

    expected_roll = 17
    assert result.roll == expected_roll
    assert result.candidates == (87, 17, 47)
    assert result.tier is CheckTier.HARD


def test_existing_module_and_team_check_schema_remain_compatible() -> None:
    modules = load_modules(PLUGIN_ROOT / "yawn_rpg" / "modules")
    assert "yuzhai_old_house" in modules
    check = CheckPoint(
        id="team_check",
        skill="spot_hidden",
        mode=CheckMode.TEAM,
        required_successes=2,
        success_text="发现了线索。",
    )
    assert check.mode is CheckMode.TEAM
