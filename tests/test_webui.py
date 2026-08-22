from __future__ import annotations

import importlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import nonebot
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if nonebot.get_plugin("yawn_core") is None:
    nonebot.load_from_toml("pyproject.toml")

auth = importlib.import_module("src.plugins.yawn_core.webui.auth")
app_module = importlib.import_module("src.plugins.yawn_core.webui.app")
service = importlib.import_module("src.plugins.yawn_core.webui.service")


@pytest.fixture(autouse=True)
def _webui_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth.config, "webui_admin_token", SecretStr("x" * 40))
    monkeypatch.setattr(auth.config, "webui_session_ttl_hours", 12)
    auth.reset_login_failures_for_tests()


def test_signed_session_rejects_tampering_and_expiry() -> None:
    cookie, session = auth.create_session(now=1_000)

    assert auth.verify_session(cookie, now=1_001) == session
    assert auth.verify_session(cookie + "tampered", now=1_001) is None
    assert auth.verify_session(cookie, now=session.expires_at) is None
    assert len(session.actor_fingerprint) == 16  # noqa: PLR2004


def test_login_rate_limit_and_reset() -> None:
    for index in range(5):
        assert not auth.check_admin_token("client", "wrong", now=float(index))
    assert not auth.login_allowed("client", now=5.0)
    assert auth.login_allowed("client", now=301.0)
    assert auth.check_admin_token("client", "x" * 40, now=301.0)
    assert auth.login_allowed("client", now=301.0)


def test_registered_fastapi_routes_serve_login_and_spa() -> None:
    app = FastAPI()
    app_module.register(app)
    client = TestClient(app)
    response = client.post("/webui/api/v1/auth/login", json={"token": "x" * 40})
    assert response.status_code == 200  # noqa: PLR2004
    assert response.json()["data"]["authenticated"] is True
    assert client.get("/webui").status_code == 200  # noqa: PLR2004


def test_agent_config_and_persona_validation() -> None:
    with pytest.raises(ValidationError):
        app_module.AgentConfigPatch.model_validate(
            {"version": None, "triggerMode": "always", "rawRetentionDays": 0}
        )
    body = app_module.AgentConfigPatch.model_validate(
        {
            "version": None,
            "triggerMode": "mention_only",
            "toolAllowlist": ["mute_member", "mute_member"],
        }
    )
    assert body.tool_allowlist == ["mute_member"]

    with pytest.raises(ValidationError):
        app_module.PersonaPatch.model_validate(
            {"version": None, "enabled": True, "overrides": {"unknown": "x"}}
        )


def test_version_conflict_is_explicit() -> None:
    row = service.GroupAgentConfig(group_id=1)
    row.updated_at = None
    app_module.check_version(row, None)
    with pytest.raises(HTTPException) as exc_info:
        app_module.check_version(row, "stale")
    assert exc_info.value.status_code == 409  # noqa: PLR2004


class _FeatureSession:
    def __init__(self, row: Any = None) -> None:
        self.row = row
        self.added: list[Any] = []
        self.deleted: list[Any] = []

    async def get(self, _model: Any, _key: Any) -> Any:
        return self.row

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def delete(self, row: Any) -> None:
        self.deleted.append(row)


@pytest.mark.asyncio
async def test_group_feature_supports_three_state_override() -> None:
    existing = service.GroupFeature(group_id=1, feature="rpg", enabled=True)
    delete_session = _FeatureSession(existing)
    await service.set_group_feature(delete_session, 1, "rpg", None)
    assert delete_session.deleted == [existing]

    create_session = _FeatureSession()
    await service.set_group_feature(create_session, 1, "rpg", override=False)
    assert len(create_session.added) == 1
    assert create_session.added[0].enabled is False


def test_big_integer_identifiers_are_serialized_as_strings() -> None:
    row = service.AgentMemory(
        id=9_007_199_254_740_993,
        scope="group",
        group_id=9_007_199_254_740_992,
        subject_user_id=9_007_199_254_740_991,
        memory_type="summary",
        memory_key="key",
        content="content",
        evidence_message_ids=[],
        salience=0.5,
        confidence=0.5,
        visibility="group",
    )
    payload = service.serialize_memory(row)
    assert payload["id"] == "9007199254740993"
    assert payload["groupId"] == "9007199254740992"
    assert payload["subjectUserId"] == "9007199254740991"


def test_iso_serializes_naive_datetime_as_beijing_time() -> None:
    # 全库约定 naive datetime 为北京时间；若误标为 UTC，前端显示会偏移 8 小时。
    assert service.iso(None) is None
    naive = datetime(2026, 8, 21, 12, 0, 0)  # noqa: DTZ001 被测对象就是 naive 约定
    assert service.iso(naive) == "2026-08-21T12:00:00+08:00"
    aware_utc = datetime(2026, 8, 21, 4, 0, 0, tzinfo=timezone.utc)
    assert service.iso(aware_utc) == "2026-08-21T04:00:00+00:00"
