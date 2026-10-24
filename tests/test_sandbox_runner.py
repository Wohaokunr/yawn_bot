# ruff: noqa: E501, PLR2004, PTH101, PTH116
"""代码沙箱 runner（``sandbox/yawn_sandbox``）的进程内测试。

CI 以普通用户运行，覆盖不降权模式：执行、限额、输出截断、工作区路径安全、
历史与 HTTP 路由。需要 root 的按群降权隔离用 ``skipif`` 单独标出；完整的
容器级隔离（无网络、只读根、跨群不可见）在部署契约测试与人工 e2e 里验证。
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SANDBOX_ROOT = REPO_ROOT / "sandbox"
if str(SANDBOX_ROOT) not in sys.path:
    sys.path.insert(0, str(SANDBOX_ROOT))

from yawn_sandbox import server as sandbox_server
from yawn_sandbox.config import Ceilings, RunnerConfig
from yawn_sandbox.limits import resolve_limits
from yawn_sandbox.workspace import (
    WorkspaceError,
    file_kind,
    split_relative_path,
    validate_workspace_id,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="沙箱 runner 只支持 Linux")


def _config(tmp_path: Path, **overrides: Any) -> RunnerConfig:
    values: dict[str, Any] = {
        "state_dir": tmp_path / "state",
        "jobs_dir": tmp_path / "jobs",
        "socket_path": tmp_path / "run" / "runner.sock",
        "socket_gid": None,
        "http_host": None,
        "http_port": 0,
        "token": "",
        "python": sys.executable,
        "lib_dir": SANDBOX_ROOT,
        "concurrency": 2,
        "queue_limit": 4,
        "queue_wait_seconds": 5,
        "history_limit": 50,
        "retention_days": 14,
        "uid_base": 20000,
        "uid_span": 100,
        "max_upload_mb": 1,
        "ceilings": Ceilings(timeout_seconds=20, memory_mb=1024, output_bytes=64 * 1024, disk_quota_mb=5, max_files=50),
        "require_isolation": False,
    }
    values.update(overrides)
    return RunnerConfig(**values)


@pytest.fixture
def service(tmp_path: Path) -> sandbox_server.RunnerService:
    return sandbox_server.RunnerService(_config(tmp_path), drop_privileges=False)


def _run(service: sandbox_server.RunnerService, code: str, workspace: str = "g1001", **limits: int) -> dict[str, Any]:
    return service.run({"workspace": workspace, "code": code, "limits": limits, "meta": {"source": "test"}})


# ── 纯函数 ────────────────────────────────────────────────────────────────


def test_limits_are_clamped_to_ceilings_and_bad_values_fall_back() -> None:
    ceilings = Ceilings(timeout_seconds=30, memory_mb=512)
    limits = resolve_limits({"timeout_seconds": 999, "memory_mb": "lots", "unknown": 1}, ceilings)
    assert limits.timeout_seconds == 30
    assert limits.memory_mb <= 512
    assert resolve_limits(None, ceilings).timeout_seconds <= 30
    assert resolve_limits({"timeout_seconds": -5}, ceilings).timeout_seconds >= 1


@pytest.mark.parametrize("value", ["", "../x", "a/../../b", "/etc/passwd", "a\x00b", "x" * 600])
def test_relative_paths_reject_escapes(value: str) -> None:
    with pytest.raises(WorkspaceError):
        split_relative_path(value)


def test_relative_path_normalizes_harmless_parts() -> None:
    assert split_relative_path("./out//chart.png") == ("out", "chart.png")
    assert split_relative_path("a\\b.txt") == ("a", "b.txt")


@pytest.mark.parametrize("value", ["g 1", "-x", "a" * 49, "g1/../admin", ""])
def test_workspace_ids_are_strict(value: str) -> None:
    with pytest.raises(WorkspaceError):
        validate_workspace_id(value)


def test_file_kinds_cover_common_outputs() -> None:
    assert file_kind("a.PNG") == "image"
    assert file_kind("song.wav") == "audio"
    assert file_kind("song.mid") == "midi"
    assert file_kind("data.xlsx") == "table"
    assert file_kind("chart.svg") == "vector"
    assert file_kind("x.unknown") == "other"


# ── 执行 ──────────────────────────────────────────────────────────────────


def test_run_echoes_last_expression_and_records_files(service: sandbox_server.RunnerService) -> None:
    result = _run(service, "print('hi')\nopen('out.txt', 'w').write('data')\n6 * 7")
    assert result["status"] == "ok"
    assert result["exit_code"] == 0
    assert "hi" in result["stdout"]
    assert result["stdout"].strip().endswith("42")
    assert [item["path"] for item in result["files"]] == ["out.txt"]
    assert result["files"][0]["created"] is True


def test_workspace_persists_between_runs(service: sandbox_server.RunnerService) -> None:
    _run(service, "open('state.txt', 'w').write('kept')")
    result = _run(service, "print(open('state.txt').read())")
    assert result["stdout"].strip() == "kept"
    assert result["files"] == []


def test_shell_runs_in_the_same_workspace(service: sandbox_server.RunnerService) -> None:
    _run(service, "open('notes.txt', 'w').write('apple\\nbanana\\ncherry\\n')")
    result = service.run(
        {
            "workspace": "g1001",
            "code": "mkdir -p sub && cd sub && echo hi > a.txt && cd .. && grep -c an notes.txt && cat sub/a.txt",
            "language": "shell",
            "limits": {},
            "meta": {"source": "test"},
        }
    )
    assert result["status"] == "ok", result
    assert result["language"] == "shell"
    assert result["stdout"].split() == ["1", "hi"]
    assert "sub/a.txt" in [item["path"] for item in result["files"]]


def test_shell_failures_report_exit_code_and_pipefail(service: sandbox_server.RunnerService) -> None:
    result = service.run(
        {"workspace": "g1001", "code": "cat missing.txt | wc -l", "language": "shell", "limits": {}, "meta": {}}
    )
    assert result["status"] == "error"
    assert result["exit_code"] not in (None, 0)
    assert "missing.txt" in result["stderr"]


def test_unknown_language_is_rejected(service: sandbox_server.RunnerService) -> None:
    with pytest.raises(sandbox_server.ApiError):
        service.run({"workspace": "g1001", "code": "x", "language": "ruby", "limits": {}, "meta": {}})
    assert "shell" in service.health()["languages"]


def test_user_errors_show_only_user_frames(service: sandbox_server.RunnerService) -> None:
    result = _run(service, "def boom():\n    raise ValueError('bad input')\nboom()")
    assert result["status"] == "error"
    assert "ValueError: bad input" in result["stderr"]
    assert "main.py" in result["stderr"]
    assert "bootstrap.py" not in result["stderr"]


def test_timeout_kills_runaway_code(service: sandbox_server.RunnerService) -> None:
    started = time.monotonic()
    result = _run(service, "import time\nwhile True:\n    time.sleep(0.05)", timeout_seconds=1)
    assert result["status"] == "timeout"
    assert time.monotonic() - started < 10


def test_output_flood_is_truncated(service: sandbox_server.RunnerService) -> None:
    result = _run(service, "import sys\nfor _ in range(200000):\n    sys.stdout.write('x' * 100 + '\\n')", output_bytes=8192)
    assert result["status"] in {"ok", "output_limit"}
    assert result["stdout_truncated"] is True
    assert len(result["stdout"]) < 20000


def test_memory_limit_is_reported(service: sandbox_server.RunnerService) -> None:
    result = _run(service, "x = bytearray(1024 * 1024 * 1024)", memory_mb=256)
    assert result["status"] == "memory_limit"


def test_environment_is_scrubbed(service: sandbox_server.RunnerService, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", "super-secret")
    result = _run(service, "import os\nprint(sorted(os.environ))")
    assert "ONEBOT_ACCESS_TOKEN" not in result["stdout"]
    assert "YAWN_WORKSPACE" in result["stdout"]


def test_background_processes_do_not_outlive_the_run(service: sandbox_server.RunnerService, tmp_path: Path) -> None:
    marker = tmp_path / "late.txt"
    child = f"import time; time.sleep(2); open({str(marker)!r}, 'w').write('x')"
    code = f"import subprocess, sys\nsubprocess.Popen([sys.executable, '-c', {child!r}])\nprint('spawned')"
    result = _run(service, code)
    assert result["status"] == "ok"
    time.sleep(3)
    assert not marker.exists()


def test_disk_quota_blocks_new_runs(service: sandbox_server.RunnerService) -> None:
    result = _run(service, "open('big.bin', 'wb').write(b'0' * 6 * 1024 * 1024)", disk_quota_mb=5)
    assert result["status"] == "disk_limit"
    assert [item["path"] for item in result["files"]] == ["big.bin"]
    blocked = _run(service, "print('should not run')", disk_quota_mb=5)
    assert blocked["status"] == "disk_limit"
    assert "should not run" not in blocked["stdout"]
    service.delete_file("g1001", {"path": "big.bin"})
    assert _run(service, "print('back')", disk_quota_mb=5)["status"] == "ok"


def test_empty_code_and_bad_workspace_are_rejected(service: sandbox_server.RunnerService) -> None:
    with pytest.raises(WorkspaceError):
        _run(service, "print(1)", workspace="../etc")
    result = _run(service, "   ")
    assert result["status"] in {"rejected", "ok"}


def test_require_isolation_refuses_runs(tmp_path: Path) -> None:
    service = sandbox_server.RunnerService(_config(tmp_path, require_isolation=True), drop_privileges=False)
    with pytest.raises(sandbox_server.ApiError) as excinfo:
        _run(service, "print(1)")
    assert excinfo.value.code == "not_isolated"
    assert service.health()["accepting_runs"] is False


# ── 文件与历史 ────────────────────────────────────────────────────────────


def test_file_api_round_trip_and_symlinks_are_refused(service: sandbox_server.RunnerService, tmp_path: Path) -> None:
    service.write_file("g1001", {"path": "uploads/data.csv"}, b"a,b\n1,2\n")
    data, mime, kind = service.read_file("g1001", {"path": "uploads/data.csv"})
    assert data == b"a,b\n1,2\n"
    assert kind == "table"
    assert mime.startswith("text/")
    listing = service.workspace_files("g1001", {})
    assert [item["path"] for item in listing["items"]] == ["uploads/data.csv"]

    secret = tmp_path / "secret.txt"
    secret.write_text("host secret", encoding="utf-8")
    _run(service, f"import os\nos.symlink({str(secret)!r}, 'link.txt')")
    with pytest.raises(WorkspaceError):
        service.read_file("g1001", {"path": "link.txt"})
    assert "link.txt" not in [item["path"] for item in service.workspace_files("g1001", {})["items"]]

    assert service.delete_file("g1001", {"path": "uploads/data.csv"})["removed"] == 1
    with pytest.raises(WorkspaceError):
        service.read_file("g1001", {"path": "uploads/data.csv"})


def test_upload_respects_size_and_quota(service: sandbox_server.RunnerService) -> None:
    with pytest.raises(sandbox_server.ApiError) as excinfo:
        service.write_file("g1001", {"path": "big.bin"}, b"0" * (2 * 1024 * 1024))
    assert excinfo.value.code == "too_large"


def test_history_lists_filters_and_forgets(service: sandbox_server.RunnerService) -> None:
    _run(service, "print('alpha')", workspace="g1")
    _run(service, "raise SystemExit(3)", workspace="g2")
    page = service.runs({})
    assert len(page["items"]) == 2
    assert page["items"][0]["workspace"] == "g2"
    assert service.runs({"workspace": "g1"})["items"][0]["preview"] == "print('alpha')"
    assert len(service.runs({"q": "alpha"})["items"]) == 1
    assert service.runs({"status": "error"})["items"][0]["workspace"] == "g2"
    detail = service.run_detail(page["items"][1]["run_id"])
    assert detail["code"] == "print('alpha')"
    stats = service.stats()["runs"]
    assert stats["total"] == 2
    assert stats["by_status"]["ok"] == 1
    assert sum(stats["hourly"]) == 2
    reset = service.reset_workspace("g1", {"forget_history": "1"})
    assert reset["history_removed"] == 1
    assert [item["workspace"] for item in service.runs({})["items"]] == ["g2"]


def test_history_survives_restart(tmp_path: Path) -> None:
    first = sandbox_server.RunnerService(_config(tmp_path), drop_privileges=False)
    _run(first, "print(1)")
    second = sandbox_server.RunnerService(_config(tmp_path), drop_privileges=False)
    assert len(second.runs({})["items"]) == 1


def test_health_reports_isolation_and_packages(service: sandbox_server.RunnerService) -> None:
    health = service.health()
    assert health["ok"] is True
    assert set(health["isolation"]["checks"]) >= {"network_disabled", "privilege_drop", "readonly_root", "child_reaper"}
    assert health["isolation"]["checks"]["privilege_drop"] is False
    assert health["python"]
    assert health["concurrency"] == 2


# ── HTTP over Unix socket ─────────────────────────────────────────────────


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str) -> None:
        super().__init__("sandbox", timeout=30)
        self.unix_path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self.unix_path)
        self.sock = sock


@pytest.fixture
def unix_server(tmp_path: Path) -> Iterator[tuple[str, sandbox_server.RunnerService]]:
    config = _config(tmp_path)
    service = sandbox_server.RunnerService(config, drop_privileges=False)
    assert config.socket_path is not None
    server = sandbox_server._bind_unix(config.socket_path, None, sandbox_server.make_handler(service))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    try:
        yield str(config.socket_path), service
    finally:
        server.shutdown()
        server.server_close()


def _request(path: str, method: str, url: str, body: bytes | None = None) -> tuple[int, Any, dict[str, str]]:
    connection = _UnixConnection(path)
    connection.request(method, url, body=body, headers={"Content-Type": "application/json"} if body else {})
    response = connection.getresponse()
    raw = response.read()
    headers = {key.lower(): value for key, value in response.getheaders()}
    try:
        return response.status, json.loads(raw), headers
    except ValueError:
        return response.status, raw, headers


def test_http_api_routes(unix_server: tuple[str, sandbox_server.RunnerService]) -> None:
    path, _service = unix_server
    assert oct(os.stat(path).st_mode & 0o777) == oct(0o660)
    status, body, _ = _request(path, "GET", "/v1/health")
    assert status == 200
    assert body["data"]["api"] == 1

    payload = json.dumps({"workspace": "g42", "code": "open('r.txt','w').write('ok')\nprint('done')"}).encode()
    status, body, _ = _request(path, "POST", "/v1/run", payload)
    assert status == 200
    assert body["data"]["status"] == "ok"

    status, raw, headers = _request(path, "GET", "/v1/workspaces/g42/file?path=r.txt")
    assert status == 200
    assert raw == b"ok"
    assert headers["x-file-kind"] == "text"

    status, body, _ = _request(path, "GET", "/v1/workspaces/g42/file?path=../../etc/passwd")
    assert status == 400
    assert body["error"]["code"] == "invalid_path"

    status, body, _ = _request(path, "GET", "/v1/workspaces/g42/file?path=missing.txt")
    assert status == 404

    status, body, _ = _request(path, "GET", "/v1/nope")
    assert status == 404

    status, body, _ = _request(path, "POST", "/v1/run", b"{not json")
    assert status == 400

    status, body, _ = _request(path, "GET", "/v1/workspaces")
    assert [item["id"] for item in body["data"]["items"]] == ["g42"]


def test_http_token_is_required_when_configured(tmp_path: Path) -> None:
    config = _config(tmp_path, token="s3cret")
    service = sandbox_server.RunnerService(config, drop_privileges=False)
    assert config.socket_path is not None
    server = sandbox_server._bind_unix(config.socket_path, None, sandbox_server.make_handler(service))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    try:
        status, body, _ = _request(str(config.socket_path), "GET", "/v1/health")
        assert status == 401
        assert body["error"]["code"] == "unauthorized"
    finally:
        server.shutdown()
        server.server_close()


# ── root：按群降权 ────────────────────────────────────────────────────────


SYSTEM_PYTHON = Path("/usr/bin/python3")


@pytest.mark.skipif(not IS_ROOT or not SYSTEM_PYTHON.exists(), reason="按群降权需要 root 与系统 Python")
def test_privilege_drop_isolates_workspaces() -> None:
    # 降权后的 uid 要能穿过所有目录：不用 pytest 的 0700 tmp，也不用 venv 里的解释器。
    root = Path(tempfile.mkdtemp(prefix="yawn-sandbox-test-"))
    os.chmod(root, 0o755)
    config = _config(root, jobs_dir=root / "jobs", python=str(SYSTEM_PYTHON), lib_dir=root / "lib")
    service = sandbox_server.RunnerService(config, drop_privileges=True)
    first = _run(service, "import os\nopen('secret.txt','w').write('g1 only')\nprint(os.getuid())", workspace="g1")
    second = _run(
        service,
        "import os\nprint(os.getuid())\nprint(open('../g1/secret.txt').read())",
        workspace="g2",
    )
    assert first["status"] == "ok"
    assert int(first["stdout"].split()[0]) >= 20000
    assert second["status"] == "error"
    assert "PermissionError" in second["stderr"]
    assert first["stdout"].split()[0] != second["stdout"].split()[0]
    shutil.rmtree(root, ignore_errors=True)
