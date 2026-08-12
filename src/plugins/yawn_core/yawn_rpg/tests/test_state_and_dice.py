"""跑团多人机制的纯状态回归测试。"""

import asyncio
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
    create_game,
    discard_game,
    game_of_user,
    get_game,
    release_action,
    stop_game,
    submit_action,
)


def test_say_after_in_flight_action_is_queued_for_next_batch() -> None:
    game = Game(group_id=1, host_user_id=10)
    first = Action(ActionKind.SAY, 10, aux="第一句", expected_phase=Phase.SIGNUP)
    assert (
        submit_action(
            game,
            first,
            queue_max=10,
            user_pending_max=1,
            user_say_pending_max=1,
        )
        is SubmitResult.ACCEPTED
    )
    consumed = game.action_queue.get_nowait()
    game.action_queue.task_done()
    consumed.in_flight = True
    second = Action(ActionKind.SAY, 10, aux="第二句", expected_phase=Phase.SIGNUP)
    assert (
        submit_action(
            game,
            second,
            queue_max=10,
            user_pending_max=1,
            user_say_pending_max=1,
        )
        is SubmitResult.ACCEPTED
    )
    assert consumed.aux == "第一句"
    assert game.action_queue.qsize() == 1
    release_action(game, consumed)
    queued = game.action_queue.get_nowait()
    game.action_queue.task_done()
    release_action(game, queued)


def test_stowed_say_is_not_rewritten_by_later_submission() -> None:
    game = Game(group_id=1, host_user_id=10)
    first = Action(ActionKind.SAY, 10, aux="切景前", expected_phase=Phase.SIGNUP)
    assert (
        submit_action(
            game,
            first,
            queue_max=10,
            user_pending_max=1,
            user_say_pending_max=1,
        )
        is SubmitResult.ACCEPTED
    )

    game.stow_actions()
    second = Action(ActionKind.SAY, 10, aux="切景后", expected_phase=Phase.SIGNUP)
    assert (
        submit_action(
            game,
            second,
            queue_max=10,
            user_pending_max=1,
            user_say_pending_max=1,
        )
        is SubmitResult.ACCEPTED
    )

    assert list(game.mid_turn_buffer) == [first]
    assert first.in_flight
    assert first.aux == "切景前"
    assert game.action_queue.get_nowait() is second
    game.action_queue.task_done()
    release_action(game, first)
    release_action(game, second)


@pytest.mark.asyncio
async def test_stop_game_cleans_worker_cancelled_before_first_run() -> None:
    game = create_game(91001, 92001)
    assert game is not None
    async def wait_forever() -> None:
        await asyncio.Event().wait()

    game.worker = asyncio.create_task(wait_forever())

    await stop_game(game)

    assert get_game(game.group_id) is None


def test_stale_discard_does_not_remove_replacement_game() -> None:
    replacement = create_game(91002, 92002)
    assert replacement is not None
    stale = Game(group_id=replacement.group_id, host_user_id=93002)

    discard_game(stale)

    assert get_game(replacement.group_id) is replacement
    assert game_of_user(replacement.host_user_id) is replacement
    discard_game(replacement)


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


def test_release_unprocessed_actions_clears_all_action_buckets() -> None:
    game = Game(group_id=1, host_user_id=10)
    actions = [Action(ActionKind.SAY, user_id) for user_id in (10, 11, 12)]
    for action in actions:
        assert (
            submit_action(
                game,
                action,
                queue_max=10,
                user_pending_max=3,
                user_say_pending_max=3,
            )
            is SubmitResult.ACCEPTED
        )

    game.pending = game.action_queue.get_nowait()
    game.action_queue.task_done()
    game.mid_turn_buffer.append(game.action_queue.get_nowait())
    game.action_queue.task_done()
    game.release_unprocessed_actions()

    assert not game.pending_actions
    assert not game.pending_say_by_user
    assert not game.mid_turn_buffer
    assert game.action_queue.empty()


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
