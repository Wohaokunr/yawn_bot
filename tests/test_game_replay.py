"""P1-7 事件回放投影与隐私视角回归测试。"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import nonebot
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OWN_ACTION_SEQUENCE = 4
ACCESS_SEAT = 2


@pytest.fixture(scope="module")
def replay_module() -> Any:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()
    if nonebot.get_plugin("yawn_core") is None:
        nonebot.load_from_toml("pyproject.toml")
    return importlib.import_module("src.plugins.yawn_core.replay")


def _werewolf_events() -> list[dict[str, object]]:
    return [
        {
            "game_id": "replay-game",
            "game_kind": "werewolf",
            "sequence": 1,
            "occurred_at": "2026-08-19T00:00:00Z",
            "event_type": "game_created",
            "phase": "SIGNUP",
            "round": 0,
            "payload": {"player_count": 4},
        },
        {
            "game_id": "replay-game",
            "game_kind": "werewolf",
            "sequence": 2,
            "occurred_at": "2026-08-19T00:01:00Z",
            "event_type": "game_started",
            "phase": "DEALING",
            "round": 0,
            "payload": {"board": "预女猎白混", "player_count": 4},
        },
        {
            "game_id": "replay-game",
            "game_kind": "werewolf",
            "sequence": 3,
            "occurred_at": "2026-08-19T00:02:00Z",
            "event_type": "phase_changed",
            "phase": "NIGHT_WOLVES",
            "round": 1,
            "payload": {},
        },
        {
            "game_id": "replay-game",
            "game_kind": "werewolf",
            "sequence": 4,
            "occurred_at": "2026-08-19T00:03:00Z",
            "event_type": "action_received",
            "phase": "NIGHT_WOLVES",
            "round": 1,
            "actor_seat": 1,
            "payload": {"action_kind": "kill", "text": "private"},
        },
        {
            "game_id": "replay-game",
            "game_kind": "werewolf",
            "sequence": 5,
            "occurred_at": "2026-08-19T00:04:00Z",
            "event_type": "action_received",
            "phase": "DAY_VOTE",
            "round": 1,
            "actor_seat": 2,
            "payload": {"action_kind": "vote"},
        },
        {
            "game_id": "replay-game",
            "game_kind": "werewolf",
            "sequence": 6,
            "occurred_at": "2026-08-19T00:05:00Z",
            "event_type": "game_ended",
            "phase": "ENDED",
            "round": 1,
            "payload": {"winner": "good"},
        },
    ]


def test_public_view_hides_night_actions_and_collapses_night_phase(
    replay_module: Any,
) -> None:
    projection = replay_module.project_events(
        "replay-game",
        _werewolf_events(),
        game_kind="werewolf",
    )

    assert projection.available
    assert projection.summary["board"] == "预女猎白混"
    assert [event.sequence for event in projection.events] == [1, 2, 3, 5, 6]
    assert projection.events[2].detail == "阶段：夜间"
    assert all("private" not in event.detail for event in projection.events)


def test_personal_view_adds_only_viewer_seat_private_action(
    replay_module: Any,
) -> None:
    projection = replay_module.project_events(
        "replay-game",
        _werewolf_events(),
        game_kind="werewolf",
        view="personal",
        viewer_seat=1,
    )

    assert projection.available
    own_action = next(
        event for event in projection.events if event.sequence == OWN_ACTION_SEQUENCE
    )
    assert own_action.detail == "你的行动：夜间行动"
    assert not any("private" in event.detail for event in projection.events)


def test_rpg_view_reconstructs_public_timeline_without_private_payload(
    replay_module: Any,
) -> None:
    events = [
        {
            "game_id": "rpg-replay",
            "game_kind": "rpg",
            "sequence": 1,
            "occurred_at": "2026-08-19T00:00:00Z",
            "event_type": "game_created",
            "phase": "SIGNUP",
            "payload": {"player_count": 2},
        },
        {
            "game_id": "rpg-replay",
            "game_kind": "rpg",
            "sequence": 2,
            "occurred_at": "2026-08-19T00:01:00Z",
            "event_type": "game_started",
            "phase": "PLAY",
            "payload": {"module_id": "house", "prompt": "secret"},
        },
        {
            "game_id": "rpg-replay",
            "game_kind": "rpg",
            "sequence": 3,
            "occurred_at": "2026-08-19T00:02:00Z",
            "event_type": "action_received",
            "phase": "CHAR_CREATE",
            "actor_seat": 1,
            "payload": {"action_kind": "confirm_card"},
        },
        {
            "game_id": "rpg-replay",
            "game_kind": "rpg",
            "sequence": 4,
            "occurred_at": "2026-08-19T00:03:00Z",
            "event_type": "scene_entered",
            "phase": "PLAY",
            "round": 1,
            "payload": {"scene_id": "hall"},
        },
        {
            "game_id": "rpg-replay",
            "game_kind": "rpg",
            "sequence": 5,
            "occurred_at": "2026-08-19T00:04:00Z",
            "event_type": "game_ended",
            "phase": "PLAY",
            "round": 1,
            "payload": {"ending_id": "good_end", "outcome": "good"},
        },
    ]

    projection = replay_module.project_events("rpg-replay", events, game_kind="rpg")
    rendered = replay_module.render_replay(projection)

    assert projection.available
    assert [event.sequence for event in projection.events] == [1, 2, 4, 5]
    assert projection.summary == {
        "module_id": "house",
        "last_scene": "hall",
        "ending_id": "good_end",
        "outcome": "good",
    }
    assert "进入场景：hall" in rendered
    assert "secret" not in rendered

    personal = replay_module.project_events(
        "rpg-replay",
        events,
        game_kind="rpg",
        view="personal",
        viewer_seat=1,
    )
    assert any(event.detail == "你的行动：确认角色卡" for event in personal.events)


def test_missing_log_is_explicitly_unavailable(
    replay_module: Any,
    tmp_path: Path,
) -> None:
    projection = replay_module.load_replay("old-game", root=tmp_path)

    assert not projection.available
    assert projection.reason == "未找到事件日志，本局不可回放"


def test_personal_access_mapping_is_process_local(replay_module: Any) -> None:
    replay_module.reset_replay_access_for_tests()
    replay_module.register_replay_participants(
        "access-game",
        "rpg",
        {1001: 2, -10001: 3},
    )

    assert replay_module.replay_viewer_seat("access-game", 1001) == ACCESS_SEAT
    assert replay_module.replay_viewer_seat("access-game", 1002) is None
    assert replay_module.replay_viewer_seat("access-game", -10001) is None
