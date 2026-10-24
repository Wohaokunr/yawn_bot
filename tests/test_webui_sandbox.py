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
inbox_store = importlib.import_module("src.plugins.yawn_core.yawn_agent.sandbox_inbox_store")

BASE = f"{config.API_PATH}/sandbox"


class FakeClient:
    files: dict[str, tuple[bytes, str, str]] = {}
    calls: list[tuple[str, Any]] = []

    async def write_file(self, workspace: str, path: str, data: bytes, *, overwrite: bool = True) -> dict[str, Any]:
        FakeClient.calls.append(("write", (path, overwrite)))
        if not overwrite and path in FakeClient.files:
            raise client_mod.SandboxError("exists", "同名文件已存在", status=409)
        FakeClient.files[path] = (data, "application/octet-stream", "other")
        return {"file": {"path": path, "size": len(data)}}

    async def delete_file(self, workspace: str, path: str) -> dict[str, Any]:
        FakeClient.calls.append(("delete", path))
        return {"removed": 1 if FakeClient.files.pop(path, None) else 0}

    async def rename_file(self, workspace: str, source: str, target: str, *, overwrite: bool = False) -> dict[str, Any]:
        FakeClient.calls.append(("rename", (source, target, overwrite)))
        FakeClient.files[target] = FakeClient.files.pop(source)
        return {"file": {"path": target, "size": 1}}

    async def make_dir(self, workspace: str, path: str) -> dict[str, Any]:
        FakeClient.calls.append(("mkdir", path))
        return {"path": path}

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
    FakeClient.calls = []
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


def test_settings_accepts_inbox_fields_and_old_payloads(api: TestClient) -> None:
    response = api.put(
        f"{BASE}/settings",
        json=_settings(inboxAutoImport="always", inboxTypes=["image", "file"], inboxMaxFileMb=10),
    )
    assert response.status_code == 200
    saved = policy.load_settings()
    assert saved.inbox_auto_import == "always" and saved.inbox_types == ["image", "file"]
    old = {key: value for key, value in _settings().items() if not key.startswith("inbox")}
    assert api.put(f"{BASE}/settings", json=old).status_code == 200
    assert policy.load_settings().inbox_auto_import == "sandbox_turns"
    assert api.put(f"{BASE}/settings", json=_settings(inboxMaxFileMb=999)).status_code == 422
    assert api.put(f"{BASE}/settings", json=_settings(inboxTypes=["exe"])).status_code == 422


def test_inbox_endpoint_lists_events_and_reasons(api: TestClient) -> None:
    inbox_store.record_events(
        [
            {"group_id": 123456, "user_id": 42, "status": "imported", "media_type": "image", "path": "inbox/图片_1.png", "size": 10, "message_id": 9, "index": 1},
            {"group_id": 123456, "user_id": 42, "status": "failed", "media_type": "file", "reason": "expired"},
            {"group_id": 654321, "user_id": 7, "status": "imported", "media_type": "record", "path": "inbox/语音_1.mp3"},
        ]
    )
    data = api.get(f"{BASE}/inbox").json()["data"]
    assert len(data["items"]) == 3 and data["items"][0]["groupId"] == "654321"
    assert data["summary"]["imported"] == 2 and data["summary"]["byReason"] == {"expired": 1}
    assert "expired" in data["reasons"] and data["hosts"]
    filtered = api.get(f"{BASE}/inbox?group=123456&status=failed").json()["data"]["items"]
    assert [row["reason"] for row in filtered] == ["expired"]
    assert api.get(f"{BASE}/inbox?status=bogus").status_code == 422
    status = api.get(f"{BASE}/status").json()["data"]
    assert status["inbox"]["today"]["imported"] == 2


def test_upload_overwrite_guard_returns_conflict(api: TestClient) -> None:
    assert api.post(f"{BASE}/workspaces/g123456/upload?path=inbox/a.csv", content=b"a").status_code == 200
    conflict = api.post(f"{BASE}/workspaces/g123456/upload?path=inbox/a.csv&overwrite=false", content=b"b")
    assert conflict.status_code == 409
    assert api.post(f"{BASE}/workspaces/g123456/upload?path=inbox/a.csv&overwrite=true", content=b"b").status_code == 200


def test_delete_many_rename_and_mkdir(api: TestClient) -> None:
    FakeClient.files = {"a.txt": (b"a", "text/plain", "text"), "inbox/b.png": (b"b", "image/png", "image")}
    inbox_store.record_events([{"group_id": 123456, "status": "imported", "path": "inbox/b.png", "message_id": 1, "index": 1}])
    result = api.post(f"{BASE}/workspaces/g123456/delete", json={"paths": ["a.txt", "inbox/b.png", "a.txt"]}).json()["data"]
    assert result["removed"] == 2 and result["deleted"] == ["a.txt", "inbox/b.png"]
    assert inbox_store.imported_paths(123456) == {}
    assert api.post(f"{BASE}/workspaces/g123456/delete", json={"paths": []}).status_code == 422

    FakeClient.files["x.csv"] = (b"x", "text/csv", "table")
    renamed = api.post(f"{BASE}/workspaces/g123456/rename", json={"from": "x.csv", "to": "data/x.csv"})
    assert renamed.status_code == 200 and renamed.json()["data"]["file"]["path"] == "data/x.csv"
    assert api.post(f"{BASE}/workspaces/g123456/mkdir", json={"path": "out"}).json()["data"]["path"] == "out"
