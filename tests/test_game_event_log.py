"""P1-5 shared event envelope and non-blocking writer checks."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ACTOR_SEAT = 2
EVENT_COUNT = 2
EVENT_LOG_PATH = PROJECT_ROOT / "src" / "plugins" / "yawn_core" / "event_log.py"
EVENT_LOG_SPEC = importlib.util.spec_from_file_location(
    "yawn_core_event_log_fixture",
    EVENT_LOG_PATH,
)
assert EVENT_LOG_SPEC is not None and EVENT_LOG_SPEC.loader is not None
event_log = importlib.util.module_from_spec(EVENT_LOG_SPEC)
sys.modules[EVENT_LOG_SPEC.name] = event_log
EVENT_LOG_SPEC.loader.exec_module(event_log)


@pytest.fixture(autouse=True)
def reset_event_sequences() -> None:
    event_log.reset_event_log_state_for_tests()


def test_payload_allowlist_drops_private_text(tmp_path: Path) -> None:
    event_log.record_event(
        "rpg",
        "fixture-game",
        "action_received",
        phase="PLAY",
        actor_seat=ACTOR_SEAT,
        payload={
            "action_kind": "say",
            "prompt": "PRIVATE_PROMPT",
            "text": "PRIVATE_MESSAGE",
            "secret": "ENCRYPT_KEY",
            "scene_id": "library",
        },
        root=tmp_path,
    )

    path = event_log.event_log_path("rpg", "fixture-game", root=tmp_path)
    line = path.read_text(encoding="utf-8")
    value = json.loads(line)
    assert value["sequence"] == 1
    assert value["actor_seat"] == ACTOR_SEAT
    assert value["payload"] == {"action_kind": "say", "scene_id": "library"}
    assert "PRIVATE_MESSAGE" not in line
    assert "ENCRYPT_KEY" not in line


@pytest.mark.asyncio
async def test_async_writer_exports_ordered_events(tmp_path: Path) -> None:
    event_log.record_event(
        "werewolf",
        "fixture-game",
        "game_created",
        phase="SIGNUP",
        root=tmp_path,
    )
    event_log.record_event(
        "werewolf",
        "fixture-game",
        "phase_changed",
        phase="DEALING",
        round_no=0,
        root=tmp_path,
    )

    await event_log.flush_events()
    events = event_log.export_events(
        "fixture-game",
        game_kind="werewolf",
        root=tmp_path,
    )
    assert [event["sequence"] for event in events] == [1, 2]
    assert [event["event_type"] for event in events] == [
        "game_created",
        "phase_changed",
    ]
    assert (
        event_log.export_events_jsonl(
            "fixture-game",
            game_kind="werewolf",
            root=tmp_path,
        ).count("\n")
        == EVENT_COUNT
    )


@pytest.mark.asyncio
async def test_writer_failure_does_not_escape_engine_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_write(_path: Path, _line: str) -> None:
        raise OSError

    monkeypatch.setattr(event_log, "_append_line", fail_write)
    event_log.record_event(
        "rpg",
        "fixture-game",
        "game_created",
        root=tmp_path,
    )
    await event_log.flush_events()


def test_record_game_event_uses_stable_fixture_id(tmp_path: Path) -> None:
    class FixtureGame:
        event_log_id = "stable-game"

    event_log.record_game_event(
        FixtureGame(),
        "rpg",
        "game_created",
        root=tmp_path,
    )

    events = event_log.export_events("stable-game", game_kind="rpg", root=tmp_path)
    assert len(events) == 1
    assert events[0]["game_id"] == "stable-game"
