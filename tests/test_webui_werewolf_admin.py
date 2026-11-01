# ruff: noqa: E501
"""狼人杀管理台 2.0 后端：规则元数据、战绩统计与实时快照扩展字段。"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nonebot
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROJECT_ROOT / "src" / "plugins" / "yawn_core"
FINAL_SPEECH = 60
GAMES_TOTAL = 3
SILENCED_SEAT = 2
PHASE_LEFT = 12.3


@pytest.fixture(scope="module")
def ww() -> Any:
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
    roles = importlib.import_module("yawn_core.yawn_werewolf.roles")
    config = importlib.import_module("yawn_core.yawn_werewolf.config")
    admin = importlib.import_module("src.plugins.yawn_core.webui.werewolf_admin")
    games = importlib.import_module("src.plugins.yawn_core.webui.games")
    return SimpleNamespace(roles=roles, config=config, admin=admin, games=games)


def test_build_meta_lists_boards_roles_and_timeouts(ww: Any) -> None:
    cfg = ww.config.Config(ww_speech_timeout=99)
    meta = ww.admin.build_meta(ww.roles, ww.config, cfg)

    keys = [board["key"] for board in meta["boards"]]
    assert ww.roles.DEFAULT_BOARD_KEY in keys
    default = next(board for board in meta["boards"] if board["isDefault"])
    nine = next(row for row in default["counts"] if row["players"] == 9)  # noqa: PLR2004
    # 9 人预女猎白：3 狼 3 神 3 民
    assert (nine["wolves"], nine["gods"], nine["villagers"]) == (3, 3, 3)
    assert sum(role["count"] for role in nine["roles"]) == 9  # noqa: PLR2004
    roles = {row["role"]: row for row in meta["roles"]}
    assert roles["狼人"]["faction"] == "wolf"
    assert roles["预言家"]["isGod"] is True
    assert roles["禁言长老"]["voteVariantSkill"]
    assert meta["config"]["ww_speech_timeout"] == 99  # noqa: PLR2004
    assert meta["phaseTimeouts"]["DAY_SPEECH"] == 99  # noqa: PLR2004
    assert meta["phaseTimeouts"]["SHERIFF_FINAL_SPEECH"] == FINAL_SPEECH
    assert "ENDED" not in meta["phaseTimeouts"]


def _game(game_id: int, *, winner: str | None, rounds: int | None, board: str = "预女猎白") -> Any:
    return SimpleNamespace(
        id=game_id,
        group_id=100 + (game_id % 2),
        board=board,
        player_count=4,
        started_at=datetime(2026, 10, 30, 20, 0),  # noqa: DTZ001
        ended_at=datetime(2026, 10, 30, 21, 0) if winner else None,  # noqa: DTZ001
        winner_faction=winner,
        end_round=rounds,
    )


def _player(game_id: int, user_id: int, role: str, faction: str, *, won: bool | None, dead: int | None = None, cause: str | None = None) -> Any:  # noqa: PLR0913
    return SimpleNamespace(
        game_id=game_id,
        user_id=user_id,
        is_ai=user_id < 0,
        role=role,
        faction=faction,
        is_winner=won,
        is_sheriff=False,
        death_round=dead,
        death_cause=cause,
    )


def test_compute_stats_aggregates_finished_games_only(ww: Any) -> None:
    games = [
        _game(1, winner="wolf", rounds=3),
        _game(2, winner="good", rounds=5),
        _game(3, winner=None, rounds=None),
    ]
    players = [
        _player(1, 10, "狼人", "wolf", won=True),
        _player(1, 11, "预言家", "good", won=False, dead=1, cause="WOLF_KILL"),
        _player(2, 10, "村民", "good", won=True),
        _player(2, -1, "狼人", "wolf", won=False, dead=2, cause="VOTED"),
        _player(3, 10, "女巫", "good", won=None),
    ]
    stats = ww.admin.compute_stats(games, players, names={10: "阿梓"})

    totals = stats["totals"]
    assert totals["games"] == GAMES_TOTAL
    assert totals["finished"] == 2  # noqa: PLR2004
    assert totals["unfinished"] == 1
    assert totals["wolfWinRate"] == 0.5  # noqa: PLR2004
    assert totals["avgRounds"] == 4  # noqa: PLR2004
    wolf = next(row for row in stats["roles"] if row["role"] == "狼人")
    assert (wolf["seats"], wolf["wins"]) == (2, 1)
    # 未结束对局里的女巫不进入角色统计
    assert all(row["role"] != "女巫" for row in stats["roles"])
    top = stats["leaderboard"][0]
    assert top["userId"] == 10  # noqa: PLR2004
    assert top["name"] == "阿梓"
    assert (top["games"], top["wins"]) == (2, 2)
    assert {row["cause"] for row in stats["deathCauses"]} == {"WOLF_KILL", "VOTED"}
    assert stats["aiVsHuman"]["ai"]["winRate"] == 0
    assert len(stats["hours"]) == 24  # noqa: PLR2004
    board = stats["boards"][0]
    assert (board["games"], board["finished"], board["avgRounds"]) == (3, 2, 4)


def test_compute_stats_empty(ww: Any) -> None:
    stats = ww.admin.compute_stats([], [])
    assert stats["totals"]["games"] == 0
    assert stats["totals"]["wolfWinRate"] is None
    assert stats["leaderboard"] == []


def test_replay_endpoint_rejects_bad_id(ww: Any) -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(ww.admin.get_werewolf_replay("../etc/passwd", None))  # type: ignore[arg-type]
    assert exc_info.value.status_code == 400  # noqa: PLR2004


@dataclass
class _Rules:
    sheriff: bool = True
    win_mode: str = "屠边"
    extra: tuple[int, ...] = ()


def test_live_snapshot_exposes_admin_detail_fields(ww: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    roles = ww.roles
    player = SimpleNamespace(
        seat=SILENCED_SEAT,
        user_id=7,
        is_ai=False,
        alive=True,
        is_sheriff=False,
        role=roles.Role.WITCH,
        faction=roles.Faction.GOOD,
        death_round=None,
        death_cause=None,
        dm_ok=False,
        can_vote=True,
        idiot_revealed=False,
        sheriff_candidate=True,
        owner_seat=None,
        save_used=True,
        poison_used=False,
    )
    action = SimpleNamespace(actor_user_id=7, kind=SimpleNamespace(value="vote"))
    game = SimpleNamespace(
        group_id=9,
        host_user_id=7,
        board="预女猎白",
        phase=SimpleNamespace(value="DAY_SPEECH"),
        round_no=2,
        current_speaker=None,
        players=[player],
        signup_user_ids=[7],
        action_queue=SimpleNamespace(qsize=lambda: 0),
        pending_actions={1: action},
        worker=None,
        silenced_seat=SILENCED_SEAT,
        role_requests={7: roles.Role.WITCH},
        rules=_Rules(),
        phase_token=5,
        vote_targets=[1, 2],
        vote_exclude=(3,),
        event_log_id="abc",
    )
    state = SimpleNamespace(
        display_name_of=lambda _game, uid: f"玩家{uid}",
        phase_remaining=lambda _game: PHASE_LEFT,
    )
    monkeypatch.setattr(ww.games, "_werewolf_game_log", lambda: None)
    snap = ww.games._ww_live_game(state, game)

    row = snap["players"][0]
    assert row["silenced"] is True
    assert row["dmOk"] is False
    assert row["saveUsed"] is True
    assert row["requestedRole"] == "女巫"
    assert row["pendingAction"] == "vote"
    assert snap["phaseRemaining"] == PHASE_LEFT
    assert snap["phaseLabel"] == "白天发言"
    assert snap["rules"] == {"sheriff": True, "win_mode": "屠边"}
    assert snap["voteExclude"] == [3]
    assert snap["signup"][0]["requestedRole"] == "女巫"


def test_live_snapshot_tolerates_old_state_shape(ww: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # 没有 rules / phase_remaining / 新玩家字段的旧状态也不报错
    player = SimpleNamespace(
        seat=1, user_id=-1, is_ai=True, alive=False, is_sheriff=False,
        role=None, faction=None, death_round=1, death_cause=None,
    )
    game = SimpleNamespace(
        group_id=1, host_user_id=1, board=None, phase=None, round_no=0,
        current_speaker=None, players=[player], signup_user_ids=[],
        action_queue=SimpleNamespace(qsize=lambda: 0), pending_actions={}, worker=None,
    )
    state = SimpleNamespace(display_name_of=lambda _game, uid: str(uid))
    monkeypatch.setattr(ww.games, "_werewolf_game_log", lambda: None)
    snap = ww.games._ww_live_game(state, game)
    assert snap["rules"] is None
    assert snap["phaseRemaining"] is None
    assert snap["players"][0]["dmOk"] is True
    assert ww.games._WW_PHASE_LABELS["NIGHT_GUARD"] == "夜晚"
