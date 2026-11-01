# ruff: noqa: E501, PLR2004, F811, FBT003, TC003, ARG005
"""沙箱 runner 1.2：大文件流式上传、按请求的配额、出网策略（iptables 规则用假执行器验证）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from test_sandbox_runner import _config, _UnixConnection, unix_server  # noqa: F401
from yawn_sandbox import netpolicy
from yawn_sandbox import server as sandbox_server
from yawn_sandbox.config import Ceilings

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="沙箱 runner 只支持 Linux")


def _put(path: str, url: str, body: bytes) -> tuple[int, Any]:
    connection = _UnixConnection(path)
    connection.request("PUT", url, body=body, headers={"Content-Length": str(len(body))})
    response = connection.getresponse()
    raw = response.read()
    try:
        return response.status, json.loads(raw)
    except ValueError:
        return response.status, raw


def test_stream_upload_handles_bodies_bigger_than_json_limit(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        max_upload_mb=8,
        ceilings=Ceilings(timeout_seconds=20, memory_mb=1024, output_bytes=64 * 1024, disk_quota_mb=64, max_files=50),
    )
    service = sandbox_server.RunnerService(config, drop_privileges=False)
    payload = bytes(range(256)) * (5 * 4096)  # 5MB，超过 JSON 请求体上限
    assert len(payload) > sandbox_server.MAX_JSON_BODY
    result = service.write_file_stream(
        "p42", {"path": "inbox/big.bin"}, len(payload), iter([payload[:1000], payload[1000:]])
    )
    assert result["file"]["size"] == len(payload)
    data, _mime, _kind = service.read_file("p42", {"path": "inbox/big.bin"})
    assert data == payload
    # 不留临时文件
    assert not list((config.state_dir).rglob(".upload-*"))


def test_stream_upload_enforces_size_and_requested_quota(tmp_path: Path) -> None:
    config = _config(tmp_path, max_upload_mb=4)
    service = sandbox_server.RunnerService(config, drop_privileges=False)
    with pytest.raises(sandbox_server.ApiError) as too_big:
        service.write_file_stream("p1", {"path": "a.bin"}, 5 * 1024 * 1024, iter([b""]))
    assert too_big.value.code == "too_large"
    with pytest.raises(sandbox_server.ApiError) as quota:
        service.write_file_stream("p1", {"path": "a.bin", "quota_mb": "1"}, 2 * 1024 * 1024, iter([b"0" * (2 * 1024 * 1024)]))
    assert quota.value.code == "quota_exceeded"
    # quota_mb 只能收紧，不能放大到 ceiling 以上
    assert service._upload_quota({"quota_mb": "999999"}) == config.ceilings.disk_quota_mb * 1024 * 1024


def test_http_put_streams_and_health_reports_network(unix_server: tuple[str, sandbox_server.RunnerService]) -> None:
    path, service = unix_server
    body = b"x" * 300_000
    status, reply = _put(path, "/v1/workspaces/p7/file?path=inbox/x.bin", body)
    assert status == 200, reply
    assert reply["data"]["file"]["size"] == len(body)
    health = service.health()
    assert "network" in health and "stream_upload" in health.get("features", [])


class _FakeIptables:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(self, binary: str, args: list[str]) -> tuple[int, str]:
        self.calls.append((binary, list(args)))
        if binary == "ip6tables":
            return 1, "no ipv6"
        if args[:1] == ["-C"]:
            return 1, "missing"
        return 0, ""


def test_netpolicy_builds_chains_and_toggles_uids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(netpolicy.os, "geteuid", lambda: 0)
    fake = _FakeIptables()
    policy = netpolicy.NetPolicy(uid_base=20000, uid_span=100, enabled=True, runner=fake)
    assert policy.setup() is True and policy.control
    rules = [args for binary, args in fake.calls if binary == "iptables"]
    assert ["-A", netpolicy.CHAIN, "-d", "169.254.0.0/16", "-j", "REJECT"] in rules
    assert ["-I", "OUTPUT", "-m", "owner", "--uid-owner", "20000-20099", "-j", netpolicy.CHAIN] in rules
    fake.calls.clear()
    assert policy.apply(20005, False) is True
    assert fake.calls[-1][1][:2] == ["-A", netpolicy.BLOCK_CHAIN] and "20005" in fake.calls[-1][1]
    assert policy.apply(20005, False) is True and len(fake.calls) == 1  # 已拦截不重复加
    assert policy.apply(20005, True) is True
    assert fake.calls[-1][1][:2] == ["-D", netpolicy.BLOCK_CHAIN]
    report = policy.report()
    assert report["control"] and report["block_private"] and report["blocked_workspaces"] == 0


def test_netpolicy_without_root_or_iptables_reports_why(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(netpolicy.os, "geteuid", lambda: 1000)
    policy = netpolicy.NetPolicy(uid_base=20000, uid_span=10, enabled=True, runner=_FakeIptables())
    assert policy.setup() is False and "root" in (policy.error or "")
    monkeypatch.setattr(netpolicy.os, "geteuid", lambda: 0)
    broken = netpolicy.NetPolicy(uid_base=20000, uid_span=10, enabled=True, runner=lambda b, a: (127, "iptables 不存在"))
    assert broken.setup() is False and "iptables" in (broken.error or "")
    off = netpolicy.NetPolicy(uid_base=20000, uid_span=10, enabled=False)
    assert off.setup() is False
    # 管不住且容器没网：不许联网的要求自然成立
    monkeypatch.setattr(netpolicy.NetPolicy, "attached", staticmethod(lambda: False))
    assert off.apply(20001, False) is True and off.guarded() is True
    monkeypatch.setattr(netpolicy.NetPolicy, "attached", staticmethod(lambda: True))
    assert off.apply(20001, False) is False and off.guarded() is False
