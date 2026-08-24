from __future__ import annotations

import importlib
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import nonebot
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if nonebot.get_plugin("yawn_core") is None:
    nonebot.load_from_toml("pyproject.toml")

games = importlib.import_module("src.plugins.yawn_core.webui.games")
auth = importlib.import_module("src.plugins.yawn_core.webui.auth")
app_module = importlib.import_module("src.plugins.yawn_core.webui.app")
rpg_state = importlib.import_module("src.plugins.yawn_core.yawn_rpg.state")
rpg_engine = importlib.import_module("src.plugins.yawn_core.yawn_rpg.engine")
rpg_schema = importlib.import_module("src.plugins.yawn_core.yawn_rpg.module_schema")
modules_api = importlib.import_module("src.plugins.yawn_core.webui.rpg_modules")

HOST_USER_ID = 1001
SECOND_USER_ID = 1002
PRIMARY_HP = 8
REPLAY_ROW_ID = 7
MISSING_USER_ID = 9999


@pytest.fixture(autouse=True)
def _webui_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth.config, "webui_admin_token", SecretStr("x" * 40))
    monkeypatch.setattr(auth.config, "webui_session_ttl_hours", 12)
    monkeypatch.setattr(auth.config, "webui_cookie_secure", False)
    auth.reset_login_failures_for_tests()


@pytest.fixture
def webui_client() -> TestClient:
    app = FastAPI()
    app_module.register(app)
    client = TestClient(app)
    response = client.post("/webui/api/v1/auth/login", json={"token": "x" * 40})
    assert response.status_code == 200  # noqa: PLR2004
    client.headers["X-CSRF-Token"] = response.json()["data"]["csrfToken"]
    return client


def _game(group_id: int = 2401) -> Any:
    module = rpg_schema.list_modules()[0]
    game = rpg_state.Game(group_id=group_id, host_user_id=HOST_USER_ID)
    game.phase = rpg_state.Phase.PLAY
    game.module = module
    game.current_scene = module.start_scene
    game.players = [
        rpg_state.PlayerState(
            user_id=HOST_USER_ID,
            seat=1,
            hp=PRIMARY_HP,
            san=55,
            confirmed=True,
        ),
        rpg_state.PlayerState(
            user_id=SECOND_USER_ID,
            seat=2,
            hp=7,
            san=48,
            confirmed=True,
        ),
    ]
    game.signup_user_ids[:] = [HOST_USER_ID, SECOND_USER_ID]
    game.group_log.extend(["〔系统〕第一条", "〔系统〕第二条"])
    return game


@pytest.fixture
def wire_rpg(monkeypatch: pytest.MonkeyPatch) -> Any:
    game = _game()
    monkeypatch.setattr(
        rpg_state,
        "get_game",
        lambda group_id: game if group_id == game.group_id else None,
    )
    monkeypatch.setattr(games, "_rpg_state", lambda: rpg_state)
    monkeypatch.setattr(games, "_rpg_engine", lambda: rpg_engine)
    monkeypatch.setattr(games, "_rpg_config", lambda: rpg_engine.config)
    return game


def test_registered_rpg_routes_serve_live_detail_and_modules(
    webui_client: TestClient,
    wire_rpg: Any,
) -> None:
    detail = webui_client.get(
        f"/webui/api/v1/games/rpg/{wire_rpg.group_id}/detail"
    )
    assert detail.status_code == 200  # noqa: PLR2004
    assert detail.json()["data"]["game"]["groupId"] == wire_rpg.group_id

    modules = webui_client.get("/webui/api/v1/rpg/modules")
    assert modules.status_code == 200  # noqa: PLR2004
    assert modules.json()["data"]


@pytest.mark.asyncio
async def test_rpg_detail_and_private_views_keep_separate_channels(
    wire_rpg: Any,
) -> None:
    detail = (await games.get_rpg_detail(wire_rpg.group_id, None))["data"]
    assert detail["game"]["groupId"] == wire_rpg.group_id
    assert detail["players"][0]["hp"] == PRIMARY_HP
    assert detail["situationText"]
    assert detail["clueBoardText"]
    assert detail["groupLog"] == ["〔系统〕第一条", "〔系统〕第二条"]
    private = (await games.get_rpg_player_private(
        wire_rpg.group_id, HOST_USER_ID, None
    ))["data"]
    assert private["userId"] == HOST_USER_ID
    with pytest.raises(HTTPException) as exc_info:
        await games.get_rpg_player_private(wire_rpg.group_id, MISSING_USER_ID, None)
    assert exc_info.value.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_rpg_action_submit_duplicate_stale_and_cleanup(
    wire_rpg: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    accepted = await games.submit_rpg_action(
        wire_rpg.group_id,
        games.RpgActionSubmit(userId=HOST_USER_ID, kind="WAIT", minutes=3),
        None,
    )
    assert accepted["data"]["accepted"] is True
    queued = wire_rpg.action_queue.get_nowait()
    assert queued.expected_phase is rpg_state.Phase.PLAY
    rpg_state.release_action(wire_rpg, queued)
    assert not wire_rpg.pending_actions

    payload = games.RpgActionSubmit(
        userId=HOST_USER_ID, kind="PASS_TURN", actionId="same"
    )
    await games.submit_rpg_action(wire_rpg.group_id, payload, None)
    with pytest.raises(HTTPException) as duplicate:
        await games.submit_rpg_action(wire_rpg.group_id, payload, None)
    assert duplicate.value.status_code == 409  # noqa: PLR2004
    wire_rpg.release_unprocessed_actions()

    monkeypatch.setattr(
        rpg_state,
        "submit_action",
        lambda *_args, **_kwargs: rpg_state.SubmitResult.STALE,
    )
    with pytest.raises(HTTPException) as stale:
        await games.submit_rpg_action(
            wire_rpg.group_id,
            games.RpgActionSubmit(userId=HOST_USER_ID, kind="PASS_TURN"),
            None,
        )
    assert stale.value.status_code == 409  # noqa: PLR2004


@pytest.mark.asyncio
async def test_rpg_modules_list_and_missing_detail() -> None:
    payload = (await modules_api.list_rpg_modules(None))["data"]
    assert payload
    assert {"id", "sceneCount", "endingCount", "health"} <= payload[0].keys()
    assert payload[0]["health"]["schemaValidated"] is True
    assert payload[0]["health"]["status"] in {
        "healthy",
        "warning",
        "error",
        "schema-only",
    }
    detail = (await modules_api.get_rpg_module(payload[0]["id"], None))["data"]
    assert "issues" in detail["health"]
    with pytest.raises(HTTPException) as exc_info:
        await modules_api.get_rpg_module("missing-module", None)
    assert exc_info.value.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_rpg_replay_endpoint_uses_event_log_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = SimpleNamespace(event_log_id="rpg-log-1")

    class _Db:
        async def get(self, _model: Any, row_id: int) -> Any:
            return row if row_id == REPLAY_ROW_ID else None

    @asynccontextmanager
    async def _session() -> AsyncIterator[_Db]:
        yield _Db()

    projection = SimpleNamespace(
        as_dict=lambda: {"available": True, "game_id": "rpg-log-1"}
    )
    monkeypatch.setattr(games, "get_session", _session)
    monkeypatch.setattr(
        games, "load_replay", lambda *_args, **_kwargs: projection
    )
    result = await games.get_rpg_history_replay(REPLAY_ROW_ID, None)
    assert result["data"]["game_id"] == "rpg-log-1"

    class _MissingDb:
        async def get(self, _model: Any, _row_id: int) -> Any:
            return SimpleNamespace(event_log_id=None)

    @asynccontextmanager
    async def _missing_session() -> AsyncIterator[_MissingDb]:
        yield _MissingDb()

    monkeypatch.setattr(games, "get_session", _missing_session)
    with pytest.raises(HTTPException) as exc_info:
        await games.get_rpg_history_replay(REPLAY_ROW_ID, None)
    assert exc_info.value.status_code == 404  # noqa: PLR2004
