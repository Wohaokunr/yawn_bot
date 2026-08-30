from __future__ import annotations

import asyncio
import importlib
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nonebot
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from starlette.websockets import WebSocketDisconnect

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
auth_routes = importlib.import_module("src.plugins.yawn_core.webui.auth_routes")
deps = importlib.import_module("src.plugins.yawn_core.webui.deps")
guest_access = importlib.import_module("src.plugins.yawn_core.webui.guest_access")
group_routes = importlib.import_module("src.plugins.yawn_core.webui.groups")
agent_routes = importlib.import_module("src.plugins.yawn_core.webui.agent")
app_module = importlib.import_module("src.plugins.yawn_core.webui.app")
route_helpers = importlib.import_module("src.plugins.yawn_core.webui.route_helpers")
route_models = importlib.import_module("src.plugins.yawn_core.webui.route_models")
service = importlib.import_module("src.plugins.yawn_core.webui.service")


def _concrete_api_path(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "100", path)


@pytest.fixture(autouse=True)
def _webui_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth.config, "webui_admin_token", SecretStr("x" * 40))
    monkeypatch.setattr(auth.config, "webui_session_ttl_hours", 12)
    auth.reset_login_failures_for_tests()


def test_signed_session_rejects_tampering_and_expiry() -> None:
    cookie, session = auth.create_session(now=1_000)

    assert auth.verify_session(cookie, now=1_001) == session
    assert session.role == "admin"
    assert auth.verify_session(cookie + "tampered", now=1_001) is None
    assert auth.verify_session(cookie, now=session.expires_at) is None
    assert len(session.actor_fingerprint) == 16  # noqa: PLR2004

    guest_cookie, guest_session = auth.create_session(
        role="guest", credential_version=7, now=1_000
    )
    assert auth.verify_session(guest_cookie, now=1_001) == guest_session
    assert guest_session.role == "guest"
    assert guest_session.credential_version == 7  # noqa: PLR2004


def test_login_rate_limit_and_reset() -> None:
    for index in range(5):
        assert not auth.check_admin_token("client", "wrong", now=float(index))
    assert not auth.login_allowed("client", now=5.0)
    assert auth.login_allowed("client", now=301.0)
    assert auth.check_admin_token("client", "x" * 40, now=301.0)
    assert auth.login_allowed("client", now=301.0)


def test_guest_credential_hash_and_revocation_versions() -> None:
    credential = guest_access.generate_guest_credential()
    digest = guest_access.hash_guest_credential(credential)
    assert credential.startswith("guest_")
    assert credential not in digest
    assert guest_access.credential_matches(credential, digest)
    assert not guest_access.credential_matches(f"{credential}x", digest)

    config = guest_access.GuestAccessConfig(
        id=1,
        enabled=True,
        credential_hash=digest,
        credential_version=3,
    )
    guest_access.apply_enabled(config, enabled=False)
    assert config.credential_version == 4  # noqa: PLR2004
    guest_access.apply_enabled(config, enabled=True)
    assert config.credential_version == 4  # noqa: PLR2004

    next_credential = guest_access.generate_guest_credential()
    guest_access.apply_new_credential(config, next_credential)
    assert config.credential_version == 5  # noqa: PLR2004
    assert config.credential_hash != next_credential
    assert guest_access.credential_matches(next_credential, config.credential_hash)


def test_registered_fastapi_api_routes_do_not_require_spa_build() -> None:
    app = FastAPI()
    app_module.register(app, include_spa=False)
    client = TestClient(app)
    response = client.post("/webui/api/v1/auth/login", json={"token": "x" * 40})
    assert response.status_code == 200  # noqa: PLR2004
    assert response.json()["data"]["authenticated"] is True
    assert response.json()["data"]["role"] == "admin"
    assert response.json()["data"]["capabilities"]["adminConsole"] is True
    assert client.get("/webui").status_code == 404  # noqa: PLR2004


def test_guest_session_is_role_aware_and_cannot_access_admin_routes(  # noqa: C901,PLR0915
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential_version = 7
    audit_events: list[dict[str, Any]] = []

    async def authenticate(token: str) -> Any:
        assert token == "guest-test-code"
        return guest_access.GuestPolicySnapshot(
            enabled=True,
            credential_configured=True,
            credential_version=credential_version,
            authorized_group_count=2,
            updated_at=None,
        )

    async def session_is_current(version: int | None) -> bool:
        return version == credential_version

    async def group_is_allowed(_group_id: int) -> bool:
        return False

    async def record_request_audit(**kwargs: Any) -> None:
        audit_events.append(kwargs)

    monkeypatch.setattr(auth_routes, "authenticate_guest_credential", authenticate)
    monkeypatch.setattr(deps, "guest_session_is_current", session_is_current)
    monkeypatch.setattr(deps, "guest_group_is_allowed", group_is_allowed)
    monkeypatch.setattr(app_module, "_record_request_audit", record_request_audit)
    app = FastAPI()
    app_module.register(app, include_spa=False)
    client = TestClient(app, base_url="https://testserver")

    login = client.post(
        "/webui/api/v1/auth/guest", json={"token": "guest-test-code"}
    )
    assert login.status_code == 200  # noqa: PLR2004
    data = login.json()["data"]
    assert data["authenticated"] is True
    assert data["role"] == "guest"
    assert data["capabilities"] == {
        "adminConsole": False,
        "adminWrite": False,
        "realtimeAdminStream": False,
        "guestGroupRead": True,
    }

    current = client.get("/webui/api/v1/auth/session")
    assert current.status_code == 200  # noqa: PLR2004
    assert current.json()["data"]["role"] == "guest"

    guest_group_get_whitelist = {
        "/webui/api/v1/groups/{group_id}",
        "/webui/api/v1/agent/groups/{group_id}/memories",
        "/webui/api/v1/agent/groups/{group_id}/memories/subjects",
        "/webui/api/v1/agent/groups/{group_id}/relations",
        "/webui/api/v1/agent/groups/{group_id}/relations/graph",
        "/webui/api/v1/agent/groups/{group_id}/relations/types",
    }

    schema_paths = app.openapi()["paths"]

    # Every management GET must fail before its handler can touch service/DB state.
    checked_management_gets: list[str] = []
    for path, operations in schema_paths.items():
        if "get" not in operations or not path.startswith("/webui/api/v1/"):
            continue
        if path.startswith("/webui/api/v1/auth/"):
            continue
        if path == "/webui/api/v1/guest/groups":
            continue
        if path in guest_group_get_whitelist:
            continue
        response = client.get(_concrete_api_path(path))
        assert response.status_code == 403, path  # noqa: PLR2004
        checked_management_gets.append(path)
    assert checked_management_gets

    # The explicit guest GET whitelist is still group-scoped; this group is not allowed.
    for path in guest_group_get_whitelist:
        response = client.get(_concrete_api_path(path))
        assert response.status_code == 403, path  # noqa: PLR2004

    # Every non-auth write is admin-only. A forged CSRF token cannot bypass
    # the role gate.
    checked_writes: list[tuple[str, str]] = []
    for path, operations in schema_paths.items():
        if not path.startswith("/webui/api/v1/") or path.startswith(
            "/webui/api/v1/auth/"
        ):
            continue
        for method in ("post", "put", "patch", "delete"):
            if method not in operations:
                continue
            response = client.request(
                method.upper(),
                _concrete_api_path(path),
                headers={"X-CSRF-Token": "forged-admin-csrf"},
                json={},
            )
            assert response.status_code == 403, (method, path)  # noqa: PLR2004
            checked_writes.append((method.upper(), path))

    assert checked_writes
    assert len(audit_events) == len(checked_writes)
    assert all(event["session"].role == "guest" for event in audit_events)
    assert all(event["status_code"] == 403 for event in audit_events)  # noqa: PLR2004

    guest_cookie = client.cookies.get(auth.COOKIE_NAME)
    assert guest_cookie
    with pytest.raises(WebSocketDisconnect) as exc_info, client.websocket_connect(
        "/webui/api/v1/stream",
        headers={"cookie": f"{auth.COOKIE_NAME}={guest_cookie}"},
    ):
        pass
    assert exc_info.value.code == 4403  # noqa: PLR2004


def test_anonymous_webui_reads_are_rejected_before_business_handlers() -> None:
    app = FastAPI()
    app_module.register(app, include_spa=False)
    client = TestClient(app)

    for path in (
        "/webui/api/v1/auth/session",
        "/webui/api/v1/guest/groups",
        "/webui/api/v1/overview",
        "/webui/api/v1/groups/100",
        "/webui/api/v1/agent/groups/100/memories",
    ):
        assert client.get(path).status_code == 401, path  # noqa: PLR2004


def test_admin_stream_keeps_snapshot_behavior_but_guest_never_reaches_overview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overview_calls = 0

    async def safe_overview() -> dict[str, Any]:
        nonlocal overview_calls
        overview_calls += 1
        return {"sensitive": "admin-only"}

    monkeypatch.setattr(app_module, "overview", safe_overview)
    app = FastAPI()
    app_module.register(app, include_spa=False)
    client = TestClient(app)

    admin_cookie = auth.create_session(role="admin")[0]
    with client.websocket_connect(
        "/webui/api/v1/stream",
        headers={"cookie": f"{auth.COOKIE_NAME}={admin_cookie}"},
    ) as websocket:
        assert websocket.receive_json() == {
            "type": "snapshot",
            "version": 1,
            "data": {"sensitive": "admin-only"},
        }
    assert overview_calls == 1

    guest_cookie = auth.create_session(role="guest", credential_version=7)[0]
    with pytest.raises(WebSocketDisconnect) as exc_info, client.websocket_connect(
        "/webui/api/v1/stream",
        headers={"cookie": f"{auth.COOKIE_NAME}={guest_cookie}"},
    ):
        pass
    assert exc_info.value.code == 4403  # noqa: PLR2004
    assert overview_calls == 1

    with pytest.raises(WebSocketDisconnect) as anonymous_exc, client.websocket_connect(
        "/webui/api/v1/stream"
    ):
        pass
    assert anonymous_exc.value.code == 4401  # noqa: PLR2004
    assert overview_calls == 1


def test_guest_group_authorization_revocation_applies_to_existing_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"allowed": True, "version": 5}

    async def authenticate(_token: str) -> Any:
        return guest_access.GuestPolicySnapshot(
            enabled=True,
            credential_configured=True,
            credential_version=state["version"],
            authorized_group_count=1,
            updated_at=None,
        )

    async def session_is_current(version: int | None) -> bool:
        return version == state["version"]

    async def group_is_allowed(group_id: int) -> bool:
        return group_id == 100 and state["allowed"]  # noqa: PLR2004

    async def get_group(_db: Any, group_id: int) -> dict[str, Any]:
        assert group_id == 100  # noqa: PLR2004
        return {
            "groupId": "100",
            "groupName": "测试群",
            "memberCount": 42,
            "features": [],
        }

    monkeypatch.setattr(auth_routes, "authenticate_guest_credential", authenticate)
    monkeypatch.setattr(deps, "guest_session_is_current", session_is_current)
    monkeypatch.setattr(deps, "guest_group_is_allowed", group_is_allowed)
    monkeypatch.setattr(
        group_routes, "get_session", lambda: _FakeSessionFactory(SimpleNamespace())
    )
    monkeypatch.setattr(group_routes, "get_group", get_group)

    app = FastAPI()
    app_module.register(app, include_spa=False)
    guest_client = TestClient(app, base_url="https://testserver")
    login = guest_client.post("/webui/api/v1/auth/guest", json={"token": "guest-code"})
    assert login.status_code == 200  # noqa: PLR2004
    assert guest_client.get("/webui/api/v1/groups/100").status_code == 200  # noqa: PLR2004

    state["allowed"] = False
    revoked = guest_client.get("/webui/api/v1/groups/100")
    assert revoked.status_code == 403  # noqa: PLR2004
    assert "未向访客开放" in revoked.json()["error"]["message"]

    admin_client = TestClient(app, base_url="https://testserver")
    assert admin_client.post(
        "/webui/api/v1/auth/login", json={"token": "x" * 40}
    ).status_code == 200  # noqa: PLR2004
    assert admin_client.get("/webui/api/v1/groups/100").status_code == 200  # noqa: PLR2004


def test_guest_session_is_immediately_invalid_when_policy_version_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"version": 11}

    async def authenticate(_token: str) -> Any:
        return guest_access.GuestPolicySnapshot(
            enabled=True,
            credential_configured=True,
            credential_version=state["version"],
            authorized_group_count=1,
            updated_at=None,
        )

    async def session_is_current(version: int | None) -> bool:
        return version == state["version"]

    monkeypatch.setattr(auth_routes, "authenticate_guest_credential", authenticate)
    monkeypatch.setattr(deps, "guest_session_is_current", session_is_current)

    app = FastAPI()
    app_module.register(app, include_spa=False)
    client = TestClient(app, base_url="https://testserver")
    login = client.post("/webui/api/v1/auth/guest", json={"token": "guest-code"})
    assert login.status_code == 200  # noqa: PLR2004
    assert client.get("/webui/api/v1/auth/session").status_code == 200  # noqa: PLR2004

    state["version"] += 1
    assert client.get("/webui/api/v1/auth/session").status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_group_view_scope_allows_admin_and_only_allowlisted_guest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = auth.create_session(role="admin", now=1_000)[1]
    guest = auth.create_session(role="guest", credential_version=7, now=1_000)[1]
    current = {"session": admin}
    checked_group_ids: list[int] = []
    allowed_group_id = 100

    async def authenticated_session(_request: Any) -> Any:
        return current["session"]

    async def group_is_allowed(group_id: int) -> bool:
        checked_group_ids.append(group_id)
        return group_id == allowed_group_id

    monkeypatch.setattr(deps, "authenticated_session", authenticated_session)
    monkeypatch.setattr(deps, "guest_group_is_allowed", group_is_allowed)

    assert await deps.require_group_view_access(999, SimpleNamespace()) == admin
    assert checked_group_ids == []

    current["session"] = guest
    assert await deps.require_group_view_access(100, SimpleNamespace()) == guest
    with pytest.raises(HTTPException) as exc_info:
        await deps.require_group_view_access(101, SimpleNamespace())
    assert exc_info.value.status_code == 403  # noqa: PLR2004
    assert checked_group_ids == [100, 101]


@pytest.mark.asyncio
async def test_guest_group_detail_only_returns_basic_display_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "groupId": "100",
        "groupName": "测试群",
        "memberCount": 42,
        "firstSeenAt": "2026-01-01T00:00:00Z",
        "lastActiveAt": "2026-08-26T00:00:00Z",
        "features": [{"key": "group_agent", "enabled": True}],
    }

    async def get_group(_db: Any, _group_id: int) -> dict[str, Any]:
        return dict(payload)

    monkeypatch.setattr(
        group_routes, "get_session", lambda: _FakeSessionFactory(SimpleNamespace())
    )
    monkeypatch.setattr(group_routes, "get_group", get_group)

    guest = auth.create_session(role="guest", credential_version=7, now=1_000)[1]
    guest_result = await group_routes.get_group_detail(100, guest)
    assert guest_result["data"] == {
        "groupId": "100",
        "groupName": "测试群",
        "memberCount": 42,
    }

    admin = auth.create_session(role="admin", now=1_000)[1]
    admin_result = await group_routes.get_group_detail(100, admin)
    assert admin_result["data"] == payload


@pytest.mark.asyncio
async def test_guest_group_collection_is_guest_only_and_uses_public_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def list_guest_groups(
        _db: Any, *, page: int, page_size: int, search: str
    ) -> tuple[list[dict[str, Any]], int]:
        assert (page, page_size, search) == (1, 20, "测试")
        return ([{"groupId": "100", "groupName": "测试群", "memberCount": 42}], 1)

    monkeypatch.setattr(
        group_routes, "get_session", lambda: _FakeSessionFactory(SimpleNamespace())
    )
    monkeypatch.setattr(group_routes, "list_guest_groups", list_guest_groups)

    admin = auth.create_session(role="admin", now=1_000)[1]
    with pytest.raises(HTTPException) as exc_info:
        await group_routes.get_guest_groups(admin, page=1, page_size=20, search="测试")
    assert exc_info.value.status_code == 403  # noqa: PLR2004

    guest = auth.create_session(role="guest", credential_version=7, now=1_000)[1]
    result = await group_routes.get_guest_groups(
        guest, page=1, page_size=20, search="测试"
    )
    assert result["data"] == [
        {"groupId": "100", "groupName": "测试群", "memberCount": 42}
    ]
    assert result["meta"] == {"page": 1, "pageSize": 20, "total": 1}


def test_agent_debug_route_requires_authentication_and_csrf() -> None:
    app = FastAPI()
    app_module.register(app, include_spa=False)
    anonymous = TestClient(app)
    path = "/webui/api/v1/agent/groups/100/debug/run"
    body = {"mode": "dialogue", "text": "测试", "actorUserId": 123}
    assert anonymous.post(path, json=body).status_code == 401  # noqa: PLR2004

    client = TestClient(app, base_url="https://testserver")
    assert client.post(
        "/webui/api/v1/auth/login", json={"token": "x" * 40}
    ).status_code == 200  # noqa: PLR2004
    assert client.post(path, json=body).status_code == 403  # noqa: PLR2004


def test_split_route_modules_are_all_registered() -> None:
    route_modules = [
        importlib.import_module("src.plugins.yawn_core.webui.auth_routes"),
        importlib.import_module("src.plugins.yawn_core.webui.overview_routes"),
        importlib.import_module("src.plugins.yawn_core.webui.environment_routes"),
        importlib.import_module("src.plugins.yawn_core.webui.guest_access_routes"),
        importlib.import_module("src.plugins.yawn_core.webui.groups"),
        importlib.import_module("src.plugins.yawn_core.webui.users"),
        importlib.import_module("src.plugins.yawn_core.webui.agent"),
        importlib.import_module("src.plugins.yawn_core.webui.audits"),
    ]
    app = FastAPI()
    app_module.register(app, include_spa=False)

    schema = app.openapi()
    registered = {
        (method.upper(), path)
        for path, operations in schema["paths"].items()
        for method in operations
        if method in {"get", "post", "put", "patch", "delete"}
    }
    split_routes = {
        (method, route.path)
        for module in route_modules
        for route in module.router.routes
        for method in route.methods or []
    }

    assert split_routes <= registered
    assert {
        ("GET", "/webui/api/v1/overview"),
        ("GET", "/webui/api/v1/guest-access"),
        ("POST", "/webui/api/v1/guest-access/credential"),
        ("PATCH", "/webui/api/v1/guest-access/groups/{group_id}"),
        ("GET", "/webui/api/v1/groups"),
        ("GET", "/webui/api/v1/guest/groups"),
        ("GET", "/webui/api/v1/users"),
        ("GET", "/webui/api/v1/agent/groups/{group_id}/diagnostics"),
        ("GET", "/webui/api/v1/agent/groups/{group_id}/execution-traces"),
        ("POST", "/webui/api/v1/agent/groups/{group_id}/debug/run"),
        ("GET", "/webui/api/v1/web-audits"),
        ("PATCH", "/webui/api/v1/environment"),
    } <= registered


def test_business_api_routes_only_expose_the_explicit_guest_group_get_whitelist(
) -> None:
    app = FastAPI()
    app_module.register(app, include_spa=False)
    guest_group_get_whitelist = {
        "/webui/api/v1/groups/{group_id}",
        "/webui/api/v1/agent/groups/{group_id}/memories",
        "/webui/api/v1/agent/groups/{group_id}/memories/subjects",
        "/webui/api/v1/agent/groups/{group_id}/relations",
        "/webui/api/v1/agent/groups/{group_id}/relations/graph",
        "/webui/api/v1/agent/groups/{group_id}/relations/types",
    }
    guest_collection_get_whitelist = {"/webui/api/v1/guest/groups"}

    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if not route.path.startswith("/webui/api/v1/"):
            continue
        if route.path.startswith("/webui/api/v1/auth/"):
            continue

        dependency_names: set[str] = set()
        pending = list(route.dependant.dependencies)
        while pending:
            dependency = pending.pop()
            dependency_names.add(getattr(dependency.call, "__name__", ""))
            pending.extend(dependency.dependencies)

        if route.methods == {"GET"} and route.path in guest_group_get_whitelist:
            assert "require_group_view_access" in dependency_names, (
                route.path,
                dependency_names,
            )
            admin_dependencies = {"admin_read_session", "admin_write_session"}
            assert not dependency_names & admin_dependencies, (
                route.path,
                dependency_names,
            )
            continue

        if route.methods == {"GET"} and route.path in guest_collection_get_whitelist:
            assert "authenticated_session" in dependency_names, (
                route.path,
                dependency_names,
            )
            admin_dependencies = {"admin_read_session", "admin_write_session"}
            assert not dependency_names & admin_dependencies, (
                route.path,
                dependency_names,
            )
            continue

        assert dependency_names & {"admin_read_session", "admin_write_session"}, (
            route.path,
            dependency_names,
        )



def test_agent_config_defaults_expose_participation_capabilities() -> None:
    result = service.serialize_agent_config(None, 100)

    assert result["groupId"] == "100"
    assert result["replyTriggerEnabled"] is True
    assert result["explicitWakeupEnabled"] is True
    assert result["proactiveEnabled"] is True
    assert result["shortConversationEnabled"] is True
    assert result["proactiveProbability"] == 0.35  # noqa: PLR2004
    assert result["dailyLimit"] == 30  # noqa: PLR2004


def test_agent_config_and_persona_validation() -> None:
    with pytest.raises(ValidationError):
        route_models.AgentConfigPatch.model_validate(
            {"version": None, "triggerMode": "always", "rawRetentionDays": 0}
        )
    body = route_models.AgentConfigPatch.model_validate(
        {
            "version": None,
            "triggerMode": "mention_only",
            "toolAllowlist": ["mute_member", "mute_member"],
        }
    )
    assert body.tool_allowlist == ["mute_member"]
    privileged = route_models.AgentConfigPatch.model_validate(
        {"version": None, "toolAllowlist": ["send_file"]}
    )
    assert privileged.tool_allowlist == ["send_file"]
    assert (
        route_models.AgentConfigPatch.model_validate(
            {"version": None, "crossGroupVisibility": "public_summary"}
        ).cross_group_visibility
        == "public_summary"
    )
    with pytest.raises(ValidationError):
        route_models.AgentConfigPatch.model_validate(
            {"version": None, "crossGroupVisibility": "all"}
        )

    with pytest.raises(ValidationError):
        route_models.AgentConfigPatch.model_validate(
            {"version": None, "proactiveActiveProbability": 1.5}
        )
    with pytest.raises(ValidationError):
        route_models.AgentConfigPatch.model_validate(
            {"version": None, "proactiveActiveWindowMinutes": 0}
        )
    proactive_patch = route_models.AgentConfigPatch.model_validate(
        {
            "version": None,
            "proactiveEnabled": False,
            "explicitWakeupEnabled": False,
            "replyTriggerEnabled": False,
            "proactiveActiveEnabled": False,
            "shortConversationEnabled": False,
            "proactiveActiveProbability": 0.1,
            "proactiveActiveWindowMinutes": 6,
        }
    )
    assert proactive_patch.proactive_enabled is False
    assert proactive_patch.explicit_wakeup_enabled is False
    assert proactive_patch.reply_trigger_enabled is False
    assert proactive_patch.proactive_active_enabled is False
    assert proactive_patch.short_conversation_enabled is False
    assert proactive_patch.proactive_active_probability == 0.1  # noqa: PLR2004
    assert proactive_patch.proactive_active_window_minutes == 6  # noqa: PLR2004

    with pytest.raises(ValidationError):
        route_models.PersonaPatch.model_validate(
            {"version": None, "enabled": True, "overrides": {"unknown": "x"}}
        )

    history = route_models.AgentDebugRunBody.model_validate(
        {"mode": "dialogue", "messageId": 42, "runModel": False}
    )
    assert history.message_id == 42  # noqa: PLR2004
    simulation = route_models.AgentDebugRunBody.model_validate(
        {"mode": "followup", "text": "又重复了一遍", "actorUserId": 123}
    )
    assert simulation.actor_user_id == 123  # noqa: PLR2004
    for invalid in (
        {},
        {"text": "少了成员"},
        {"actorUserId": 123},
        {"messageId": 42, "text": "两种来源", "actorUserId": 123},
    ):
        with pytest.raises(ValidationError):
            route_models.AgentDebugRunBody.model_validate(invalid)


def test_version_conflict_is_explicit() -> None:
    row = service.GroupAgentConfig(group_id=1)
    row.updated_at = None
    route_helpers.check_version(row, None)
    with pytest.raises(HTTPException) as exc_info:
        route_helpers.check_version(row, "stale")
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
        source_kind="manual",
        related_user_ids=[9_007_199_254_740_991],
        salience=0.5,
        confidence=0.5,
        visibility="group",
    )
    payload = service.serialize_memory(row)
    assert payload["id"] == "9007199254740993"
    assert payload["groupId"] == "9007199254740992"
    assert payload["subjectUserId"] == "9007199254740991"
    assert payload["sourceKind"] == "manual"
    assert payload["relatedUserIds"] == ["9007199254740991"]

    row.subject_user_id = 0
    assert service.serialize_memory(row)["subjectUserId"] is None


def test_guest_memory_projection_excludes_internal_provenance_fields() -> None:
    row = service.AgentMemory(
        id=7,
        scope="group",
        group_id=100,
        subject_user_id=111,
        memory_type="profile",
        memory_key="favorite_game",
        content="喜欢一起玩桌游",
        evidence_message_ids=[123, 456],
        source_kind="auto",
        related_user_ids=[222],
        salience=0.8,
        confidence=0.9,
        visibility="group",
        created_at=datetime(2026, 8, 20, 12, 0, 0),  # noqa: DTZ001
        updated_at=datetime(2026, 8, 21, 12, 0, 0),  # noqa: DTZ001
        expires_at=datetime(2026, 9, 20, 12, 0, 0),  # noqa: DTZ001
    )

    payload = service.serialize_guest_memory(row)

    assert payload == {
        "id": "7",
        "subjectUserId": "111",
        "type": "profile",
        "key": "favorite_game",
        "content": "喜欢一起玩桌游",
        "confidence": 0.9,
        "updatedAt": "2026-08-21T12:00:00+08:00",
    }
    assert {
        "groupId",
        "scope",
        "sourceKind",
        "evidenceMessageIds",
        "provenance",
        "relatedUserIds",
        "salience",
        "visibility",
        "createdAt",
        "expiresAt",
    }.isdisjoint(payload)


def test_iso_serializes_naive_datetime_as_beijing_time() -> None:
    # 全库约定 naive datetime 为北京时间；若误标为 UTC，前端显示会偏移 8 小时。
    assert service.iso(None) is None
    naive = datetime(2026, 8, 21, 12, 0, 0)  # noqa: DTZ001 被测对象就是 naive 约定
    assert service.iso(naive) == "2026-08-21T12:00:00+08:00"
    aware_utc = datetime(2026, 8, 21, 4, 0, 0, tzinfo=timezone.utc)
    assert service.iso(aware_utc) == "2026-08-21T04:00:00+00:00"


@pytest.mark.asyncio
async def test_games_live_reports_loaded_sub_plugins_available() -> None:
    # 回归：games.py 曾用单点相对导入（.yawn_werewolf）解析兄弟包，
    # 永远抛 ModuleNotFoundError，导致子插件全部启用时前端仍显示"子插件未加载"。
    games = importlib.import_module("src.plugins.yawn_core.webui.games")
    payload = (await games.get_live_games(None))["data"]

    assert payload["werewolf"]["available"] is True
    assert payload["rpg"]["available"] is True
    assert payload["werewolf"]["games"] == []
    assert payload["rpg"]["games"] == []
    # 必须读到子插件注册表的同一模块实例，否则实时对局永远为空。
    assert (
        games._werewolf_state()
        is sys.modules["src.plugins.yawn_core.yawn_werewolf.state"]
    )
    assert games._rpg_state() is sys.modules["src.plugins.yawn_core.yawn_rpg.state"]
    assert (
        games._werewolf_game_log()
        is sys.modules["src.plugins.yawn_core.yawn_werewolf.game_log"]
    )


def test_games_resolvers_retry_after_failure() -> None:
    # 回归：解析失败不得落缓存，否则子插件晚加载后页面无法自动恢复。
    games = importlib.import_module("src.plugins.yawn_core.webui.games")
    games._ww_state_resolved = False
    games._ww_state_module = None
    try:
        with pytest.MonkeyPatch.context() as patch:
            # 让真实解析抛错一次，验证失败后 resolved 仍为 False（下次会重试）。
            import builtins

            real_import = builtins.__import__

            def failing_import(name: str, *args: object, **kwargs: object) -> Any:
                if "yawn_werewolf" in name:
                    raise ImportError
                return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

            patch.setattr(builtins, "__import__", failing_import)
            assert games._werewolf_state() is None
            assert games._ww_state_resolved is False
    finally:
        games._ww_state_resolved = False
        games._ww_state_module = None
    # 真实环境里子插件已加载，下一次调用应恢复并缓存。
    assert (
        games._werewolf_state()
        is sys.modules["src.plugins.yawn_core.yawn_werewolf.state"]
    )
    assert games._ww_state_resolved is True


def test_memory_create_body_validates_type_and_expiry() -> None:
    body = route_models.MemoryCreateBody.model_validate(
        {
            "type": "manual",
            "key": "群规",
            "content": "晚上十一点后保持安静",
            "subjectUserId": 123,
            "salience": 0.9,
            "expiresInDays": None,
        }
    )
    assert body.subject_user_id == 123  # noqa: PLR2004
    assert body.expires_in_days is None

    # core 为管理员手动钉住的核心记忆类型，同样允许创建。
    core_body = route_models.MemoryCreateBody.model_validate(
        {"type": "core", "key": "display_name", "content": "阿眠"}
    )
    assert core_body.type == "core"

    with pytest.raises(ValidationError):
        route_models.MemoryCreateBody.model_validate(
            {"type": "unknown", "key": "k", "content": "c"}
        )
    with pytest.raises(ValidationError):
        route_models.MemoryCreateBody.model_validate(
            {"type": "manual", "key": "k", "content": "c", "expiresInDays": 0}
        )
    with pytest.raises(ValidationError):
        route_models.MemoryCreateBody.model_validate(
            {"type": "manual", "key": "", "content": "c"}
        )


def test_memory_patch_body_requires_updatable_field() -> None:
    updates = route_models.MemoryPatchBody.model_validate(
        {"version": "v1", "salience": 0.8, "expiresInDays": None}
    ).model_dump(exclude_unset=True, exclude={"version"})
    assert updates == {"salience": 0.8, "expires_in_days": None}

    empty = route_models.MemoryPatchBody.model_validate({"version": "v1"})
    assert empty.model_dump(exclude_unset=True, exclude={"version"}) == {}


def test_privacy_patch_body_accepts_camel_alias() -> None:
    assert route_models.PrivacyPatchBody.model_validate({"optedOut": True}).opted_out


def test_serialize_relation_and_agent_message_as_strings() -> None:
    relation = service.AgentRelation(
        id=9_007_199_254_740_993,
        group_id=9_007_199_254_740_992,
        subject_user_id=9_007_199_254_740_991,
        object_user_id=9_007_199_254_740_990,
        relation_type="mentions",
        source_kind="manual",
        note="管理员录入",
        confidence=0.55,
        evidence_count=3,
        last_seen_at=datetime(2026, 8, 21, 12, 0, 0),  # noqa: DTZ001
    )
    payload = service.serialize_relation(relation)
    assert payload["id"] == "9007199254740993"
    assert payload["subjectUserId"] == "9007199254740991"
    assert payload["sourceKind"] == "manual"
    assert payload["note"] == "管理员录入"
    assert payload["evidenceCount"] == 3  # noqa: PLR2004

    guest_payload = service.serialize_guest_relation(relation)
    assert guest_payload == {
        "id": "9007199254740993",
        "subjectUserId": "9007199254740991",
        "objectUserId": "9007199254740990",
        "type": "mentions",
        "note": "管理员录入",
        "confidence": 0.55,
        "lastSeenAt": "2026-08-21T12:00:00+08:00",
    }
    assert {"groupId", "sourceKind", "evidenceCount"}.isdisjoint(guest_payload)

    message = service.GroupAgentMessage(
        id=9_007_199_254_740_989,
        bot_id=1,
        message_id=42,
        group_id=9_007_199_254_740_992,
        user_id=9_007_199_254_740_991,
        sender_name="阿眠",
        role="member",
        normalized_text="你好",
        received_at=datetime(2026, 8, 21, 12, 0, 0),  # noqa: DTZ001
        expires_at=datetime(2026, 8, 28, 12, 0, 0),  # noqa: DTZ001
    )
    message_payload = service.serialize_agent_message(message)
    assert message_payload["messageId"] == "42"
    assert message_payload["userId"] == "9007199254740991"
    assert message_payload["receivedAt"] == "2026-08-21T12:00:00+08:00"


def test_relation_create_body_validates_fields() -> None:
    body = route_models.RelationCreateBody.model_validate(
        {
            "subjectUserId": 111,
            "objectUserId": 222,
            "type": "朋友",
            "note": "常一起开黑",
        }
    )
    assert body.subject_user_id == 111  # noqa: PLR2004
    assert body.confidence == 0.9  # noqa: PLR2004

    with pytest.raises(ValidationError):
        route_models.RelationCreateBody.model_validate(
            {"subjectUserId": 0, "objectUserId": 222, "type": "好友"}
        )
    with pytest.raises(ValidationError):
        route_models.RelationCreateBody.model_validate(
            {
                "subjectUserId": 111,
                "objectUserId": 222,
                "type": "好友",
                "confidence": 1.5,
            }
        )
    with pytest.raises(ValidationError):
        route_models.RelationCreateBody.model_validate(
            {"subjectUserId": 111, "objectUserId": 222, "type": ""}
        )


def test_relation_patch_body_requires_updatable_field() -> None:
    updates = route_models.RelationPatchBody.model_validate(
        {"note": "新备注", "confidence": 0.8}
    ).model_dump(exclude_unset=True)
    assert updates == {"note": "新备注", "confidence": 0.8}

    empty = route_models.RelationPatchBody.model_validate({})
    assert empty.model_dump(exclude_unset=True) == {}


class _ScalarResult:
    def __init__(self, values: list[Any]) -> None:
        self._values = values

    def scalars(self) -> "_ScalarResult":
        return self

    def all(self) -> list[Any]:
        return self._values


class _FakeRelationSession:
    """覆盖关系边端点用到的 get/execute/scalar/add/commit/refresh 路径。"""

    def __init__(
        self,
        *,
        opted_out: set[int] | None = None,
        conflict: bool = False,
        existing: Any = None,
    ) -> None:
        self.opted_out = opted_out or set()
        self.conflict = conflict
        self.existing = existing
        self.added: list[Any] = []
        self.committed = False

    async def get(self, model: Any, key: Any) -> Any:
        if model.__name__ == "BotGroup" and key == 100:  # noqa: PLR2004
            return service.BotGroup(group_id=key, group_name="测试群")
        return None

    async def execute(self, _stmt: Any) -> _ScalarResult:
        return _ScalarResult(list(self.opted_out))

    async def scalar(self, _stmt: Any) -> Any:
        return self.existing

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        if self.conflict:
            raise agent_routes.IntegrityError("insert", {}, Exception("dup"))

    async def rollback(self) -> None:
        return None

    async def refresh(self, _row: Any) -> None:
        return None


class _FakeSessionFactory:
    def __init__(self, session: Any) -> None:
        self._session = session

    async def __aenter__(self) -> Any:
        return self._session

    async def __aexit__(self, *_args: object) -> None:
        return None


class _FakeDebugResult:
    def __init__(self, first: Any = None, scalars: list[Any] | None = None) -> None:
        self._first = first
        self._scalars = scalars or []

    def first(self) -> Any:
        return self._first

    def scalars(self) -> _ScalarResult:
        return _ScalarResult(self._scalars)


class _FakeDebugSession:
    """调试接口只允许 get/execute；出现 add/commit 会让测试直接失败。"""

    def __init__(self, *, opted_out: bool = False) -> None:
        self.config = service.GroupAgentConfig(group_id=100)
        self.member = service.UserGroup(
            group_id=100,
            user_id=123,
            group_nickname="当前发言人",
            role="member",
        )
        self.opted_out = opted_out

    async def get(self, model: Any, _key: Any) -> Any:
        if model is service.GroupAgentConfig:
            return self.config
        return None

    async def execute(self, stmt: Any) -> _FakeDebugResult:
        entity = stmt.column_descriptions[0]["entity"]
        if entity is service.UserGroup:
            return _FakeDebugResult((self.member, "全局昵称"))
        if entity is service.AgentPrivacy:
            return _FakeDebugResult(scalars=[123] if self.opted_out else [])
        raise AssertionError


@pytest.mark.asyncio
async def test_agent_debug_simulation_is_read_only_and_never_executes_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeDebugSession()
    model_calls: list[tuple[list[Any], list[Any]]] = []

    async def require_group(*_args: object) -> None:
        return None

    async def load_context(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return {
            "group_id": 100,
            "group_name": "测试群",
            "messages": [{"user_id": 7, "text": "旧话题", "minutes_ago": 90}],
            "members": [],
            "memories": [],
            "relations": [],
            "activity": {},
        }

    async def complete(messages: list[Any], tools: list[Any], **_kwargs: object) -> Any:
        model_calls.append((messages, tools))
        tool_call = SimpleNamespace(
            function=SimpleNamespace(
                name="record_user_relation",
                arguments='{"subject_user_id":123,"object_user_id":456,"type":"好友"}',
            )
        )
        return SimpleNamespace(
            message=SimpleNamespace(content="", tool_calls=[tool_call]),
            finish_reason="tool_calls",
            prompt_tokens=120,
            completion_tokens=18,
            cached_tokens=40,
            cache_miss_tokens=80,
            outcome="success",
            duration_ms=12.5,
        )

    monkeypatch.setattr(
        agent_routes, "get_session", lambda: _FakeSessionFactory(session)
    )
    monkeypatch.setattr(agent_routes, "require_group", require_group)
    monkeypatch.setattr(agent_routes, "_load_context", load_context)
    monkeypatch.setattr(agent_routes, "_debug_bots", list)
    monkeypatch.setattr(agent_routes, "complete_with_tools_result", complete)

    preview = await agent_routes.run_agent_debug(
        100,
        route_models.AgentDebugRunBody.model_validate(
            {"mode": "dialogue", "text": "到底有没有一起玩", "actorUserId": 123}
        ),
        None,
    )
    assert preview["data"]["currentTurn"]["user_id"] == 123  # noqa: PLR2004
    assert preview["data"]["result"] is None
    assert preview["data"]["executionTrace"]["source"] == "debug"
    assert any(
        event["phase"] == "llm" and event["status"] == "skipped"
        for event in preview["data"]["executionTrace"]["events"]
    )
    assert model_calls == []

    trial = await agent_routes.run_agent_debug(
        100,
        route_models.AgentDebugRunBody.model_validate(
            {
                "mode": "dialogue",
                "text": "到底有没有一起玩",
                "actorUserId": 123,
                "runModel": True,
            }
        ),
        None,
    )
    assert len(model_calls) == 1
    assert trial["data"]["result"]["toolCalls"] == [
        {
            "name": "record_user_relation",
            "arguments": {
                "subject_user_id": 123,
                "object_user_id": 456,
                "type": "好友",
            },
        }
    ]
    assert trial["data"]["result"]["finishReason"] == "tool_calls"
    assert trial["data"]["result"]["usage"]["cachedTokens"] == 40  # noqa: PLR2004
    assert trial["data"]["result"]["usage"]["cacheMissTokens"] == 80  # noqa: PLR2004
    trace_events = trial["data"]["executionTrace"]["events"]
    phases = [event["phase"] for event in trace_events]
    assert phases[:4] == ["intake", "context", "capability", "prompt"]
    assert any(
        event["phase"] == "tool"
        and event["status"] == "planned"
        and event["output"]["executed"] is False
        for event in trace_events
    )
    assert any(
        event["phase"] == "state" and event["status"] == "skipped"
        for event in trace_events
    )

    active = 0
    max_active = 0

    async def slow_complete(*_args: object, **_kwargs: object) -> Any:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.02)
            return SimpleNamespace(
                message=SimpleNamespace(content="好的", tool_calls=[]),
                finish_reason="stop",
                prompt_tokens=10,
                completion_tokens=2,
                cached_tokens=0,
                cache_miss_tokens=10,
                outcome="success",
                duration_ms=20.0,
            )
        finally:
            active -= 1

    monkeypatch.setattr(agent_routes, "complete_with_tools_result", slow_complete)
    debug_body = route_models.AgentDebugRunBody.model_validate(
        {
            "mode": "dialogue",
            "text": "并发测试",
            "actorUserId": 123,
            "runModel": True,
        }
    )
    await asyncio.gather(
        *(agent_routes.run_agent_debug(100, debug_body, None) for _ in range(3))
    )
    assert max_active == 2  # noqa: PLR2004

    monkeypatch.setattr(agent_routes, "_DEBUG_TIMEOUT_SECONDS", 0.005)
    timed_out = await agent_routes.run_agent_debug(100, debug_body, None)
    assert timed_out["data"]["result"]["outcome"] == "timeout"


@pytest.mark.asyncio
async def test_agent_debug_rejects_privacy_opted_out_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeDebugSession(opted_out=True)

    async def require_group(*_args: object) -> None:
        return None

    monkeypatch.setattr(
        agent_routes, "get_session", lambda: _FakeSessionFactory(session)
    )
    monkeypatch.setattr(agent_routes, "require_group", require_group)
    with pytest.raises(HTTPException) as exc_info:
        await agent_routes.run_agent_debug(
            100,
            route_models.AgentDebugRunBody.model_validate(
                {"mode": "dialogue", "text": "不要记录我", "actorUserId": 123}
            ),
            None,
        )
    assert exc_info.value.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_create_relation_normalizes_type_and_marks_manual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeRelationSession()
    monkeypatch.setattr(
        agent_routes, "get_session", lambda: _FakeSessionFactory(session)
    )

    create_body = route_models.RelationCreateBody.model_validate(
        {
            "subjectUserId": 111,
            "objectUserId": 222,
            "type": "对象",
            "note": " 官宣过 ",
        }
    )
    result = await agent_routes.create_relation(100, create_body, None)

    assert len(session.added) == 1
    edge = session.added[0]
    assert edge.relation_type == "情侣"
    assert edge.source_kind == "manual"
    assert edge.note == "官宣过"
    assert result["data"]["sourceKind"] == "manual"


@pytest.mark.asyncio
async def test_create_relation_rejects_same_endpoints_and_opted_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeRelationSession()
    monkeypatch.setattr(
        agent_routes, "get_session", lambda: _FakeSessionFactory(session)
    )

    with pytest.raises(HTTPException) as same:
        await agent_routes.create_relation(
            100,
            route_models.RelationCreateBody.model_validate(
                {"subjectUserId": 111, "objectUserId": 111, "type": "好友"}
            ),
            None,
        )
    assert same.value.status_code == 422  # noqa: PLR2004

    privacy_session = _FakeRelationSession(opted_out={222})
    monkeypatch.setattr(
        agent_routes, "get_session", lambda: _FakeSessionFactory(privacy_session)
    )
    with pytest.raises(HTTPException) as opted_out:
        await agent_routes.create_relation(
            100,
            route_models.RelationCreateBody.model_validate(
                {"subjectUserId": 111, "objectUserId": 222, "type": "好友"}
            ),
            None,
        )
    assert opted_out.value.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_create_relation_conflict_returns_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeRelationSession(conflict=True)
    monkeypatch.setattr(
        agent_routes, "get_session", lambda: _FakeSessionFactory(session)
    )

    with pytest.raises(HTTPException) as conflict:
        await agent_routes.create_relation(
            100,
            route_models.RelationCreateBody.model_validate(
                {"subjectUserId": 111, "objectUserId": 222, "type": "好友"}
            ),
            None,
        )
    assert conflict.value.status_code == 409  # noqa: PLR2004


@pytest.mark.asyncio
async def test_update_relation_only_touches_note_and_confidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = service.AgentRelation(
        id=7,
        group_id=100,
        subject_user_id=111,
        object_user_id=222,
        relation_type="好友",
        source_kind="auto",
        note="",
        confidence=0.5,
        evidence_count=1,
        last_seen_at=datetime(2026, 8, 21, 12, 0, 0),  # noqa: DTZ001
    )
    session = _FakeRelationSession(existing=existing)
    monkeypatch.setattr(
        agent_routes, "get_session", lambda: _FakeSessionFactory(session)
    )

    patch_body = route_models.RelationPatchBody.model_validate(
        {"note": "常一起开黑", "confidence": 0.85}
    )
    result = await agent_routes.update_relation(100, 7, patch_body, None)

    assert existing.relation_type == "好友"
    assert existing.note == "常一起开黑"
    assert existing.confidence == 0.85  # noqa: PLR2004
    assert result["data"]["note"] == "常一起开黑"


class _FakeGraphSession:
    """图谱端点用到的 get/execute 路径：按查询目标模型分发结果。"""

    def __init__(
        self,
        *,
        opted_out: set[int] | None = None,
        relations: list[Any] | None = None,
        members: list[tuple[Any, Any]] | None = None,
    ) -> None:
        self.opted_out = opted_out or set()
        self.relations = relations or []
        self.members = members or []

    async def get(self, model: Any, key: Any) -> Any:
        if model.__name__ == "BotGroup" and key == 100:  # noqa: PLR2004
            return service.BotGroup(group_id=key, group_name="测试群")
        return None

    async def execute(self, stmt: Any) -> _ScalarResult:
        entity = stmt.column_descriptions[0]["entity"]
        name = getattr(entity, "__name__", str(entity))
        if name == "AgentPrivacy":
            return _ScalarResult(list(self.opted_out))
        if name == "AgentRelation":
            # 模拟 SQL 的 not_in 过滤：隐私退出成员参与的边不出现在结果中。
            rows = [
                row
                for row in self.relations
                if row.subject_user_id not in self.opted_out
                and row.object_user_id not in self.opted_out
            ]
            return _ScalarResult(rows)
        return _ScalarResult(list(self.members))


def _graph_relation(
    relation_id: int, subject: int, target: int, relation_type: str = "好友"
) -> Any:
    return service.AgentRelation(
        id=relation_id,
        group_id=100,
        subject_user_id=subject,
        object_user_id=target,
        relation_type=relation_type,
        source_kind="auto",
        note="",
        confidence=0.8,
        evidence_count=1,
        last_seen_at=datetime(2026, 8, 21, 12, 0, 0),  # noqa: DTZ001
    )


@pytest.mark.asyncio
async def test_relation_graph_merges_members_and_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relations = [
        _graph_relation(1, 111, 222),
        _graph_relation(2, 333, 111, "对立"),
        # 444 已隐私退出，含其的边应被过滤。
        _graph_relation(3, 111, 444),
    ]
    members = [
        (
            service.UserGroup(group_id=100, user_id=111, role="member"),
            service.BotUser(user_id=111, nickname="小明"),
        ),
        (
            service.UserGroup(group_id=100, user_id=222, role="admin"),
            service.BotUser(user_id=222, nickname="小红"),
        ),
        # 555 是无边成员，应标记为未连接。
        (
            service.UserGroup(group_id=100, user_id=555, role="member"),
            service.BotUser(user_id=555, nickname="小刚"),
        ),
        # 333 已不在成员表（退群残留），仍需补节点避免边悬空。
    ]
    session = _FakeGraphSession(
        opted_out={444}, relations=relations, members=members
    )
    monkeypatch.setattr(
        agent_routes, "get_session", lambda: _FakeSessionFactory(session)
    )

    result = await agent_routes.get_relation_graph(100, None)

    data = result["data"]
    assert [edge["id"] for edge in data["edges"]] == ["1", "2"]
    assert all(isinstance(edge["subjectUserId"], str) for edge in data["edges"])
    nodes = {node["userId"]: node for node in data["nodes"]}
    assert set(nodes) == {"111", "222", "333", "555"}
    assert nodes["111"]["nickname"] == "小明"
    assert nodes["111"]["degree"] == 2  # noqa: PLR2004
    assert nodes["111"]["linked"] is True
    assert nodes["222"]["role"] == "admin"
    assert nodes["333"]["nickname"] == ""
    assert nodes["333"]["linked"] is True
    assert nodes["333"]["degree"] == 1
    assert nodes["555"]["linked"] is False
    assert nodes["555"]["degree"] == 0
    assert data["meta"] == {"relationTruncated": False, "memberTruncated": False}


@pytest.mark.asyncio
async def test_guest_relation_graph_only_returns_linked_allowed_nodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relations = [
        _graph_relation(1, 111, 222),
        # 444 已隐私退出，这条边对访客不可见。
        _graph_relation(2, 222, 444, "同事"),
    ]
    members = [
        (
            service.UserGroup(group_id=100, user_id=111, role="member"),
            service.BotUser(user_id=111, nickname="小明"),
        ),
        (
            service.UserGroup(group_id=100, user_id=222, role="admin"),
            service.BotUser(user_id=222, nickname="小红"),
        ),
        (
            service.UserGroup(group_id=100, user_id=444, role="member"),
            service.BotUser(user_id=444, nickname="已退出记忆成员"),
        ),
        # 无关系的普通成员也不应作为 guest 图谱孤立节点出现。
        (
            service.UserGroup(group_id=100, user_id=555, role="member"),
            service.BotUser(user_id=555, nickname="无关系成员"),
        ),
    ]
    session = _FakeGraphSession(
        opted_out={444}, relations=relations, members=members
    )
    monkeypatch.setattr(
        agent_routes, "get_session", lambda: _FakeSessionFactory(session)
    )
    guest = auth.create_session(role="guest", credential_version=7, now=1_000)[1]

    result = await agent_routes.get_relation_graph(100, guest)

    data = result["data"]
    assert [edge["id"] for edge in data["edges"]] == ["1"]
    assert set(data["edges"][0]) == {
        "id",
        "subjectUserId",
        "objectUserId",
        "type",
        "note",
        "confidence",
        "lastSeenAt",
    }
    assert {node["userId"] for node in data["nodes"]} == {"111", "222"}
    assert data["meta"] == {"relationTruncated": False}


@pytest.mark.asyncio
async def test_relation_graph_marks_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relations = [
        _graph_relation(index, 1000 + index, 9000 + index)
        for index in range(service.RELATION_GRAPH_LIMIT + 1)
    ]
    session = _FakeGraphSession(relations=relations)
    monkeypatch.setattr(
        agent_routes, "get_session", lambda: _FakeSessionFactory(session)
    )

    result = await agent_routes.get_relation_graph(100, None)

    data = result["data"]
    assert len(data["edges"]) == service.RELATION_GRAPH_LIMIT
    assert data["meta"]["relationTruncated"] is True
    assert data["meta"]["memberTruncated"] is False


class _FakeSubjectsSession:
    """成员画像索引端点用到的 get/execute 路径，并在 Python 侧模拟 SQL 过滤。"""

    def __init__(
        self,
        *,
        opted_out: set[int] | None = None,
        memories: list[Any] | None = None,
        members: list[tuple[Any, Any]] | None = None,
    ) -> None:
        self.opted_out = opted_out or set()
        self.memories = memories or []
        self.members = members or []

    async def get(self, model: Any, key: Any) -> Any:
        if model.__name__ == "BotGroup" and key == 100:  # noqa: PLR2004
            return service.BotGroup(group_id=key, group_name="测试群")
        return None

    async def execute(self, stmt: Any) -> _ScalarResult:
        entity = stmt.column_descriptions[0]["entity"]
        name = getattr(entity, "__name__", str(entity))
        if name == "AgentPrivacy":
            return _ScalarResult(list(self.opted_out))
        if name == "UserGroup":
            return _ScalarResult(list(self.members))
        assert name == "AgentMemory"
        # 模拟 SQL 过滤：群级/非画像类型/过期/隐私退出行不进入聚合，
        # 并按 updated_at 降序返回（端点依赖该顺序得出"最近更新"）。
        rows = [
            row
            for row in self.memories
            if int(row.subject_user_id or 0) != 0
            and row.memory_type in agent_routes._SUBJECT_MEMORY_TYPES
            and not (
                row.expires_at is not None
                and row.expires_at < agent_routes.now_beijing()
            )
            and int(row.subject_user_id) not in self.opted_out
            and not any(
                int(value) in self.opted_out
                for value in row.related_user_ids or []
            )
        ]
        rows.sort(key=lambda row: row.updated_at, reverse=True)
        return _ScalarResult(rows)


def _subject_memory(
    memory_id: int,
    subject: int,
    memory_type: str,
    updated_at: datetime,
    *,
    expires_at: datetime | None = None,
) -> Any:
    return service.AgentMemory(
        id=memory_id,
        group_id=100,
        subject_user_id=subject,
        memory_type=memory_type,
        memory_key="hobby",
        content="爬山",
        evidence_message_ids=[],
        source_kind="auto",
        related_user_ids=[subject],
        salience=0.5,
        confidence=0.5,
        visibility="group",
        updated_at=updated_at,
        expires_at=expires_at,
    )


@pytest.mark.asyncio
async def test_memory_subjects_aggregates_counts_and_resolves_nicknames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memories = [
        _subject_memory(1, 111, "core", datetime(2026, 8, 23, 10, 0, 0)),  # noqa: DTZ001
        _subject_memory(2, 111, "profile", datetime(2026, 8, 22, 9, 0, 0)),  # noqa: DTZ001
        _subject_memory(3, 222, "profile", datetime(2026, 8, 20, 9, 0, 0)),  # noqa: DTZ001
        # 333 已退群但画像残留：仍应列出，昵称回退为空。
        _subject_memory(4, 333, "manual", datetime(2026, 8, 21, 9, 0, 0)),  # noqa: DTZ001
        # 群级摘要、过期画像、隐私退出成员的画像都不应计入。
        _subject_memory(5, 0, "summary", datetime(2026, 8, 23, 11, 0, 0)),  # noqa: DTZ001
        _subject_memory(
            6,
            222,
            "profile",
            datetime(2026, 8, 23, 12, 0, 0),  # noqa: DTZ001
            expires_at=datetime(2026, 1, 1),  # noqa: DTZ001
        ),
        _subject_memory(7, 444, "profile", datetime(2026, 8, 23, 9, 0, 0)),  # noqa: DTZ001
    ]
    members = [
        (
            service.UserGroup(group_id=100, user_id=111, role="member"),
            service.BotUser(user_id=111, nickname="小明"),
        ),
        (
            service.UserGroup(group_id=100, user_id=222, role="admin"),
            service.BotUser(user_id=222, nickname="小红"),
        ),
    ]
    session = _FakeSubjectsSession(
        opted_out={444}, memories=memories, members=members
    )
    monkeypatch.setattr(
        agent_routes, "get_session", lambda: _FakeSessionFactory(session)
    )

    guest = auth.create_session(role="guest", credential_version=7, now=1_000)[1]
    result = await agent_routes.get_memory_subjects(100, guest)

    data = result["data"]
    assert "444" not in {entry["userId"] for entry in data}
    assert [entry["userId"] for entry in data] == ["111", "333", "222"]
    assert all(isinstance(entry["userId"], str) for entry in data)
    first = data[0]
    assert first["nickname"] == "小明"
    assert first["counts"] == {"profile": 1, "core": 1, "manual": 0}
    assert first["total"] == 2  # noqa: PLR2004
    assert first["updatedAt"] == "2026-08-23T10:00:00+08:00"
    assert data[1]["nickname"] == ""
    assert data[1]["counts"]["manual"] == 1
    assert data[2]["nickname"] == "小红"
    assert data[2]["counts"] == {"profile": 1, "core": 0, "manual": 0}
    assert data[2]["total"] == 1
