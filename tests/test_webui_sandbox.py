# ruff: noqa: E501, PLR2004, ARG001, ARG002, RUF012, PLR0913, PLR0917
"""WebUI「代码沙箱」后端：camelCase、离线降级、下载头与策略校验。"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nonebot
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()
if nonebot.get_plugin("yawn_core") is None:
    nonebot.load_from_toml("pyproject.toml")

routes = importlib.import_module("src.plugins.yawn_core.webui.sandbox")
deps = importlib.import_module("src.plugins.yawn_core.webui.deps")
config = importlib.import_module("src.plugins.yawn_core.webui.config")
policy = importlib.import_module("src.plugins.yawn_core.yawn_agent.sandbox_policy")
client_mod = importlib.import_module("src.plugins.yawn_core.yawn_agent.sandbox_client")

BASE = f"{config.API_PATH}/sandbox"


class FakeClient:
    files: dict[str, tuple[bytes, str, str]] = {}

    async def read_file(self, workspace: str, path: str, *, max_bytes: int | None = None) -> tuple[bytes, str, str]:
        if path not in FakeClient.files:
            raise client_mod.SandboxError("not_found", "没有这个文件")
        return FakeClient.files[path]


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(policy, "STATE_DIR", tmp_path)
    policy.reset_state_for_tests()

    async def offline(*, refresh: bool = False) -> tuple[Any, Any]:
        return None, client_mod.SandboxError("offline", "连不上沙箱")

    async def notify(_topic: str) -> None:
        return None

    monkeypatch.setattr(routes, "cached_health", offline)
    monkeypatch.setattr(routes, "sandbox_endpoint", lambda: SimpleNamespace(enabled=True, transport_label="unix"))
    monkeypatch.setattr(routes, "SandboxClient", FakeClient)
    monkeypatch.setattr(routes.hub, "notify_change", notify)
    FakeClient.files = {}
    app = FastAPI()
    app.include_router(routes.router)
    session = SimpleNamespace(role="admin")
    app.dependency_overrides[deps.admin_read_session] = lambda: session
    app.dependency_overrides[deps.admin_write_session] = lambda: session
    return TestClient(app)


def test_camelize_keeps_opaque_keys() -> None:
    value = routes.camelize(
        {"run_id": "x", "by_status": {"memory_limit": 1}, "nested_list": [{"exit_code": 0}], "all": {"numpy_core": "1"}}
    )
    assert value == {"runId": "x", "byStatus": {"memory_limit": 1}, "nestedList": [{"exitCode": 0}], "all": {"numpy_core": "1"}}


def test_status_degrades_when_offline(api: TestClient) -> None:
    response = api.get(f"{BASE}/status")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["state"] == "offline" and data["online"] is False
    assert data["error"] == {"code": "offline", "message": "连不上沙箱"}
    assert data["settings"]["memberDailyRuns"] == policy.SandboxSettings().member_daily_runs


@pytest.mark.parametrize(
    ("path", "mime", "kind", "query", "disposition"),
    [
        ("chart.png", "image/png", "image", "", "inline"),
        ("chart.png", "image/png", "image", "&download=1", "attachment"),
        ("evil.html", "text/html", "document", "", "attachment"),
        ("logo.svg", "image/svg+xml", "vector", "", "attachment"),
    ],
)
def test_file_download_headers(api: TestClient, path: str, mime: str, kind: str, query: str, disposition: str) -> None:
    FakeClient.files[path] = (b"data", mime, kind)
    response = api.get(f"{BASE}/workspaces/g123456/file?path={path}{query}")
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith(disposition)
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"].startswith("sandbox")
    if disposition == "attachment":
        assert response.headers["content-type"] == "application/octet-stream"


def test_file_preview_text_only(api: TestClient) -> None:
    FakeClient.files["a.csv"] = (b"a,b\n1,2", "text/csv", "table")
    FakeClient.files["b.xlsx"] = (b"PK", "application/vnd.ms-excel", "table")
    preview = api.get(f"{BASE}/workspaces/g123456/file?path=a.csv&preview=1")
    assert preview.json()["data"] == {"text": "a,b\n1,2", "truncated": False, "kind": "table"}
    assert api.get(f"{BASE}/workspaces/g123456/file?path=b.xlsx&preview=1").status_code == 415
    assert api.get(f"{BASE}/workspaces/g123456/file?path=nope.txt").status_code == 404


@pytest.mark.parametrize("workspace", ["G1", "../etc", "a b", "-x"])
def test_workspace_ids_validated(api: TestClient, workspace: str) -> None:
    response = api.get(f"{BASE}/workspaces/{workspace}/file?path=a.txt")
    assert response.status_code in {400, 404, 422}


def _settings(**changes: Any) -> dict[str, Any]:
    payload = routes._settings_payload(policy.SandboxSettings())
    payload.update(changes)
    return payload


def test_settings_roundtrip(api: TestClient) -> None:
    response = api.put(
        f"{BASE}/settings",
        json=_settings(groupMode="allowlist", allowedGroups=["123456", " 123456 ", ""], timeoutSeconds=60),
    )
    assert response.status_code == 200
    saved = policy.load_settings()
    assert saved.allowed_groups == [123456] and saved.timeout_seconds == 60
    assert response.json()["data"]["settings"]["allowedGroups"] == ["123456"]


@pytest.mark.parametrize(
    "changes",
    [{"allowedGroups": ["12"]}, {"blockedGroups": ["abc"]}, {"timeoutSeconds": 1000}, {"memoryMb": 1}],
)
def test_settings_rejects_invalid(api: TestClient, changes: dict[str, Any]) -> None:
    response = api.put(f"{BASE}/settings", json=_settings(**changes))
    assert response.status_code == 422
    assert policy.load_settings() == policy.SandboxSettings()
