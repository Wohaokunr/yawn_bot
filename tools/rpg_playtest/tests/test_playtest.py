"""Focused regression tests for the offline fixed-seed RPG playtester."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from tools.rpg_playtest import simulator

ROOT = Path(__file__).resolve().parents[3]
MODULES = ROOT / "src" / "plugins" / "yawn_core" / "yawn_rpg" / "modules"
OLD_HOUSE = MODULES / "yuzhai_old_house.yaml"
TIDE = MODULES / "before_tide_departs.yaml"
INTRO = MODULES / "fogbound_archive.yaml"


def _config(ending: str, **kwargs: Any) -> simulator.SearchConfig:
    return simulator.SearchConfig(
        seed=kwargs.pop("seed", 0),
        ending_id=ending,
        players=kwargs.pop("players", None),
        max_depth=kwargs.pop("max_depth", 40),
        max_states=kwargs.pop("max_states", 50_000),
    )


def test_old_house_truth_trace_is_deterministic() -> None:
    module = simulator.load_module(OLD_HOUSE)
    config = _config("truth_revealed", seed=0, players=1)

    first = simulator.search_module(module, config).to_dict()
    second = simulator.search_module(module, config).to_dict()

    assert first == second
    assert first["ok"] is True
    assert first["final_ending"]["id"] == "truth_revealed"
    actions = [step["action"] for step in first["steps"]]
    targets = [step["target"] for step in first["steps"]]
    assert actions[0:3] == ["auto_move", "move", "check"]
    assert "basement" in targets
    assert any("brass_key" in step["clues_added"] for step in first["steps"])
    assert any("truth" in step["clues_added"] for step in first["steps"])


def test_different_seed_changes_generated_party_or_rolls() -> None:
    module = simulator.load_module(OLD_HOUSE)
    first = simulator.search_module(
        module, _config("truth_revealed", seed=0, players=1)
    ).to_dict()
    second = simulator.search_module(
        module, _config("truth_revealed", seed=1, players=1)
    ).to_dict()

    assert (
        first["players"] != second["players"]
        or first["steps"] != second["steps"]
    )


def test_intro_trace_includes_deterministic_deduction() -> None:
    module = simulator.load_module(INTRO)

    first = simulator.search_module(
        module, _config("alarm_explained", seed=28, players=1)
    ).to_dict()
    second = simulator.search_module(
        module, _config("alarm_explained", seed=28, players=1)
    ).to_dict()

    assert first == second
    assert first["ok"] is True
    assert any(
        step["action"] == "deduction" and step["target"] == "inside_key_used"
        for step in first["steps"]
    )


def test_tide_lanterns_trace_covers_social_time_and_combat() -> None:
    module = simulator.load_module(TIDE)
    result = simulator.search_module(
        module,
        _config(
            "lanterns_tonight",
            seed=20260813,
            players=2,
            max_depth=40,
        ),
    ).to_dict()

    assert result["ok"] is True
    assert result["final_ending"]["id"] == "lanterns_tonight"
    expected_flags = {
        "voice_choose_route",
        "voice_dream",
        "voice_original_wish",
        "voice_honest",
        "final_stay",
    }
    seen_flags = {
        flag
        for step in result["steps"]
        for flag in step["flags_changed"]
    }
    assert expected_flags.issubset(seen_flags)
    assert any(step["action"] == "attack" for step in result["steps"])
    assert any(step["scene_after"] == "station_platform" for step in result["steps"])
    assert any("festival_safe" in step["clues_added"] for step in result["steps"])


def test_team_check_uses_all_active_players_and_fixed_rng() -> None:
    module = simulator.load_module(TIDE)
    rng = random.Random(56)
    players = simulator._build_players(2, rng)
    state = simulator._State(
        scene=module.start_scene,
        clock_start=module.time.start_minutes,
        elapsed=0,
        players=players,
        rng=rng,
    )

    rolls = simulator._apply_check(
        module, state, simulator._Action("check", 0, "station_team_pages")
    )

    assert [roll["roll"] for roll in rolls] == [24, 25]
    assert "volunteer_list" in state.clues
    assert "station_team_pages" in state.passed_checks


def test_wait_candidates_include_clock_and_schedule_boundaries() -> None:
    module = simulator.load_module(TIDE)
    rng = random.Random(20260813)
    players = simulator._build_players(2, rng)
    state = simulator._State(
        scene=module.start_scene,
        clock_start=module.time.start_minutes,
        elapsed=0,
        players=players,
        rng=rng,
    )
    ending = next(item for item in module.endings if item.id == "lanterns_tonight")
    relevant = simulator._relevance(module, ending)

    assert simulator._wait_values(module, state, relevant) == [1, 5, 30, 35, 85, 120]


def test_invalid_target_and_players_fail_without_search() -> None:
    module = simulator.load_module(OLD_HOUSE)

    unknown = simulator.search_module(
        module, _config("missing", seed=0, players=1)
    )
    invalid_players = simulator.search_module(
        module, _config("truth_revealed", seed=0, players=99)
    )

    assert unknown.reason == "unknown_ending"
    assert invalid_players.reason == "invalid_players"
    assert unknown.explored_states == invalid_players.explored_states == 0


def test_unreachable_target_reports_no_path() -> None:
    module = simulator.ModuleDef.model_validate(
        {
            "id": "unreachable",
            "name": "unreachable",
            "opening": "",
            "start_scene": "start",
            "scenes": [
                {"id": "start", "name": "start", "narration": "", "exits": []}
            ],
            "clues": [{"id": "missing", "name": "missing", "text": ""}],
            "endings": [
                {
                    "id": "goal",
                    "condition": "clue:missing",
                    "text": "",
                    "outcome": "neutral",
                }
            ],
        }
    )

    result = simulator.search_module(
        module, _config("goal", seed=0, players=1)
    )

    assert result.reason == "no_path"
    assert result.explored_states == result.generated_states == 1


def test_search_does_not_touch_global_random_source() -> None:
    module = simulator.load_module(OLD_HOUSE)
    before = random.getstate()

    simulator.search_module(module, _config("truth_revealed", seed=0, players=1))

    assert random.getstate() == before


def test_depth_and_state_limits_are_explicit() -> None:
    module = simulator.load_module(OLD_HOUSE)

    depth = simulator.search_module(
        module,
        _config("truth_revealed", seed=0, players=1, max_depth=0),
    )
    states = simulator.search_module(
        module,
        _config("truth_revealed", seed=0, players=1, max_states=1),
    )

    assert depth.reason == "limit_exceeded"
    assert states.reason == "limit_exceeded"
    assert depth.explored_states >= 1
    assert states.explored_states >= 1


def test_generic_ending_compatibility_table_is_ordered() -> None:
    assert simulator.GENERIC_ENDINGS == (
        ("generic_arson_egg", "flag:arson>=4", "neutral"),
        ("generic_fire", "flag:arson>=2", "bad"),
        ("generic_arrest", "flag:murder", "bad"),
        ("generic_subdued", "flag:assault>=3", "bad"),
        ("generic_tpk", "all_players_incapped", "bad"),
    )
