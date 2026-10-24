# ruff: noqa: E501, PLR2004, ARG001, RUF012
"""代码沙箱的 Bot 侧：策略/配额、发现门控、结果投影、sandbox: 引用与 handler。

runner 本身的隔离测试在 ``test_sandbox_runner.py``；这里全部用假客户端，不需要
真正的沙箱容器。
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import nonebot
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()
if nonebot.get_plugin("yawn_core") is None:
    nonebot.load_from_toml("pyproject.toml")

_PKG = "src.plugins.yawn_core.yawn_agent"
tools = importlib.import_module(f"{_PKG}.tools")
router = importlib.import_module(f"{_PKG}.tool_router")
policy = importlib.import_module(f"{_PKG}.sandbox_policy")
client_mod = importlib.import_module(f"{_PKG}.sandbox_client")
refs = importlib.import_module(f"{_PKG}.sandbox_refs")
handler = importlib.import_module(f"{_PKG}.tool_handlers.sandbox")
claims = importlib.import_module(f"{_PKG}.speech_claims")
outbound = importlib.import_module(f"{_PKG}.outbound")
tool_args = importlib.import_module(f"{_PKG}.tool_args")

GROUP = 123456
USER = 42
HEALTHY = {"ok": True, "isolation": {"isolated": True}, "accepting_runs": True, "packages": [], "python": "3.12"}


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(policy, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(refs, "agent_file_root", lambda: tmp_path / "files")
    policy.reset_state_for_tests()
    client_mod.reset_health_cache()
    _set_health(monkeypatch, HEALTHY)


def _set_health(monkeypatch: pytest.MonkeyPatch, health: dict[str, Any] | None, error: Exception | None = None) -> None:
    async def fake_health(*, refresh: bool = False) -> tuple[Any, Any]:
        return health, error

    for module in (policy, handler, client_mod):
        monkeypatch.setattr(module, "cached_health", fake_health)


def _save(**changes: Any) -> None:
    policy.save_settings(policy.SandboxSettings(**changes))


def _context(*, admin: bool = False, user: int | None = USER) -> Any:
    return tools.ToolExecutionContext(
        bot=object(),
        group_id=GROUP,
        actor_user_id=user,
        session=None,
        capabilities=tools.BotGroupCapabilities(role="member", can_manage=False, actions=frozenset()),
        actor_can_manage=admin,
        privileged_allowlist=frozenset(),
    )


class FakeClient:
    """记录调用的假 SandboxClient；每个测试改类属性决定返回。"""

    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
    run_result: dict[str, Any] | Exception = {"status": "ok", "stdout": "hi", "files": []}
    files: dict[str, tuple[bytes, str, str]] = {}

    async def run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        FakeClient.calls.append(("run", args, kwargs))
        if isinstance(FakeClient.run_result, Exception):
            raise FakeClient.run_result
        return FakeClient.run_result

    async def read_file(self, workspace: str, path: str, *, max_bytes: int | None = None) -> tuple[bytes, str, str]:
        FakeClient.calls.append(("read_file", (workspace, path), {"max_bytes": max_bytes}))
        if path not in FakeClient.files:
            raise client_mod.SandboxError("not_found", "没有这个文件")
        return FakeClient.files[path]

    async def list_files(self, workspace: str, *, prefix: str = "", limit: int = 200) -> dict[str, Any]:
        FakeClient.calls.append(("list_files", (workspace,), {"prefix": prefix, "limit": limit}))
        return {"items": [{"path": "a.png", "size": 10, "kind": "image", "mtime": 0}], "total": 1, "usage": {"bytes": 10}}

    async def delete_file(self, workspace: str, path: str) -> dict[str, Any]:
        FakeClient.calls.append(("delete_file", (workspace, path), {}))
        return {"removed": 1 if path in FakeClient.files else 0}


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> type[FakeClient]:
    FakeClient.calls = []
    FakeClient.run_result = {"status": "ok", "stdout": "hi", "files": [], "run_id": "r1", "duration_ms": 1200}
    FakeClient.files = {}
    monkeypatch.setattr(handler, "SandboxClient", FakeClient)
    monkeypatch.setattr(refs, "SandboxClient", FakeClient)
    return FakeClient


# ---- 策略与配额 -------------------------------------------------------------


@pytest.mark.asyncio
async def test_reserve_counts_and_refund_restores() -> None:
    reservation = await policy.reserve_run(GROUP, USER, actor_is_admin=False)
    assert policy.member_usage(GROUP, USER) == 1
    assert policy.group_usage(GROUP) == 1
    reservation.refund()
    reservation.refund()  # 重复退还不会变成负数或多退
    assert policy.member_usage(GROUP, USER) == 0
    assert policy.group_usage(GROUP) == 0


@pytest.mark.asyncio
async def test_member_quota_and_cooldown() -> None:
    _save(member_daily_runs=1, member_cooldown_seconds=0)
    await policy.reserve_run(GROUP, USER, actor_is_admin=False)
    with pytest.raises(policy.SandboxPolicyError) as exc:
        await policy.reserve_run(GROUP, USER, actor_is_admin=False)
    assert exc.value.reason == "member_quota"

    _save(member_daily_runs=10, member_cooldown_seconds=60)
    with pytest.raises(policy.SandboxPolicyError) as exc:
        await policy.reserve_run(GROUP, USER, actor_is_admin=False)
    assert exc.value.reason == "cooldown"


@pytest.mark.asyncio
async def test_group_quota_applies_across_members() -> None:
    _save(group_daily_runs=2, member_cooldown_seconds=0)
    await policy.reserve_run(GROUP, 1, actor_is_admin=False)
    await policy.reserve_run(GROUP, 2, actor_is_admin=False)
    with pytest.raises(policy.SandboxPolicyError) as exc:
        await policy.reserve_run(GROUP, 3, actor_is_admin=False)
    assert exc.value.reason == "group_quota"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("settings", "admin", "user", "reason"),
    [
        ({"enabled": False}, True, USER, "sandbox_closed"),
        ({"group_mode": "allowlist", "allowed_groups": [1]}, True, USER, "sandbox_closed"),
        ({"blocked_groups": [GROUP]}, True, USER, "sandbox_closed"),
        ({"admin_only": True}, False, USER, "actor_not_admin"),
        ({}, False, None, "no_actor"),
    ],
)
async def test_policy_rejections(settings: dict[str, Any], admin: bool, user: int | None, reason: str) -> None:  # noqa: FBT001
    _save(**settings)
    with pytest.raises(policy.SandboxPolicyError) as exc:
        await policy.reserve_run(GROUP, user, actor_is_admin=admin)
    assert exc.value.reason == reason
    assert policy.group_usage(GROUP) == 0


@pytest.mark.asyncio
async def test_allowlist_and_admin_only_allow_admin() -> None:
    _save(group_mode="allowlist", allowed_groups=[GROUP], admin_only=True)
    await policy.reserve_run(GROUP, USER, actor_is_admin=True)


@pytest.mark.asyncio
async def test_unsafe_and_offline_runner_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_health(monkeypatch, {"isolation": {"isolated": False}})
    with pytest.raises(policy.SandboxPolicyError) as exc:
        await policy.reserve_run(GROUP, USER, actor_is_admin=False)
    assert exc.value.reason == "sandbox_unsafe"

    _save(allow_unisolated=True)
    await policy.reserve_run(GROUP, USER, actor_is_admin=False)

    _set_health(monkeypatch, None, client_mod.SandboxError("offline", "连不上"))
    with pytest.raises(policy.SandboxPolicyError) as exc:
        await policy.reserve_run(GROUP, 7, actor_is_admin=False)
    assert exc.value.reason == "sandbox_offline"


def test_broken_settings_file_falls_back_to_defaults() -> None:
    path = policy.STATE_DIR / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert policy.load_settings() == policy.SandboxSettings()
    path.write_text(json.dumps({"timeout_seconds": 9999}), encoding="utf-8")
    policy.reset_state_for_tests()
    assert policy.load_settings().timeout_seconds == policy.SandboxSettings().timeout_seconds


def test_limits_clamp_requested_timeout() -> None:
    settings = policy.SandboxSettings(timeout_seconds=30)
    assert settings.limits(timeout_seconds=90)["timeout_seconds"] == 30
    assert settings.limits(timeout_seconds=0)["timeout_seconds"] == 1
    assert settings.limits()["timeout_seconds"] == 30


def test_usage_from_previous_day_is_reset() -> None:
    path = policy.STATE_DIR / "usage.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"day": "2000-01-01", "groups": {str(GROUP): 99}, "members": {}}), encoding="utf-8")
    assert policy.group_usage(GROUP) == 0


@pytest.mark.parametrize(
    ("settings", "admin", "health", "expected"),
    [
        ({}, False, HEALTHY, None),
        ({"enabled": False}, True, HEALTHY, "sandbox_closed"),
        ({"admin_only": True}, False, HEALTHY, "actor_not_admin"),
        ({}, False, None, "sandbox_offline"),
        ({}, False, {"isolation": {"isolated": False}}, "sandbox_unsafe"),
    ],
)
def test_exposure_reason(settings: dict[str, Any], admin: bool, health: dict[str, Any] | None, expected: str | None) -> None:  # noqa: FBT001
    _save(**settings)
    assert policy.exposure_reason(GROUP, actor_is_admin=admin, health=health, health_error=None) == expected


# ---- 发现门控 / 路由 ------------------------------------------------------


async def _discover(args: dict[str, Any]) -> dict[str, Any]:
    result = await tools.execute_tool_with_meta("discover_tools", args, context=_context(admin=True))
    return result.payload["result"]


@pytest.mark.asyncio
async def test_discovery_exposes_sandbox_family_when_healthy() -> None:
    result = await _discover({"family": "沙箱"})
    names = {item["name"] for item in result["tools"]}
    assert {"run_python", "list_sandbox_files", "sandbox_info"} <= names


@pytest.mark.asyncio
async def test_discovery_hides_sandbox_family_when_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_health(monkeypatch, None, client_mod.SandboxError("offline", "连不上"))
    result = await _discover({"family": "sandbox"})
    assert not any(item["family"] == "sandbox" for item in result["tools"])
    unavailable = {item["name"]: item["reason"] for item in result.get("unavailable", [])}
    # 模型据此如实说明“沙箱离线”，而不是回答“没有这个功能”。
    assert unavailable and set(unavailable.values()) == {"代码沙箱服务当前离线"}


@pytest.mark.asyncio
async def test_other_family_discovery_skips_health_check(monkeypatch: pytest.MonkeyPatch) -> None:
    async def explode(*, refresh: bool = False) -> Any:
        raise AssertionError("不应该检查沙箱健康")

    monkeypatch.setattr(client_mod, "cached_health", explode)
    result = await _discover({"family": "notice"})
    assert result["mode"] == "toolpack"


@pytest.mark.parametrize("alias", ["沙箱", "虚拟机", "Python", "vm", "画图", "code"])
def test_family_aliases(alias: str) -> None:
    assert router.normalize_tool_family(alias) == "sandbox"


def test_discovery_segment_types() -> None:
    assert router.discovery_segment_types({"run_python"}) == frozenset({"image", "record"})
    assert router.discovery_segment_types({"send_file", "nope"}) == frozenset()


# ---- 结果投影 ------------------------------------------------------------


def test_project_run_result_scrubs_and_truncates() -> None:
    stdout = "/var/lib/yawnbot-sandbox/workspaces/g1/data.csv\n" + "x" * 5000
    stderr = 'File "/opt/yawnbot-sandbox/venv/lib/python3.12/site-packages/pandas/core.py"\nValueError: bad'
    payload = handler.project_run_result(
        {"status": "error", "exit_code": 1, "stdout": stdout, "stderr": stderr, "duration_ms": 1500, "files": [{"path": "chart.png", "kind": "image", "size": 2048, "created": True}], "message": "失败"},
        max_chars=1000,
    )
    assert "/var/lib/yawnbot-sandbox" not in payload["stdout"]
    assert payload["stdout"].startswith("data.csv")
    assert "<lib>/pandas/core.py" in payload["stderr"]
    assert payload["output_truncated"] is True
    assert len(payload["stdout"]) < 1100
    assert payload["files"] == [{"ref": "sandbox:chart.png", "kind": "image", "size": "2.0KB", "new": True}]
    assert payload["ok"] is False and payload["exit_code"] == 1
    assert "image" in payload["next"] and "hint" in payload and payload["note"] == "失败"
    assert payload["duration"] == "1.5s"


def test_project_run_result_caps_file_list() -> None:
    files = [{"path": f"f{i}.txt", "kind": "text", "size": 1} for i in range(20)]
    payload = handler.project_run_result({"status": "ok", "files": files}, max_chars=500)
    assert len(payload["files"]) == 12 and payload["files_truncated"] is True
    assert "hint" not in payload


# ---- sandbox: 引用 --------------------------------------------------------


@pytest.mark.parametrize(
    ("ref", "expected"),
    [("sandbox:chart.png", "chart.png"), ("SANDBOX: out/./a.wav", "out/a.wav"), ("sandbox:/abs/x.png", "abs/x.png")],
)
def test_parse_sandbox_ref(ref: str, expected: str) -> None:
    assert refs.parse_sandbox_ref(ref) == expected


@pytest.mark.parametrize("ref", ["sandbox:", "sandbox:../etc/passwd", "sandbox:a/../../b", "sandbox:" + "a" * 600])
def test_parse_sandbox_ref_rejects(ref: str) -> None:
    with pytest.raises(ValueError, match="不合法"):
        refs.parse_sandbox_ref(ref)


@pytest.mark.asyncio
async def test_materialize_and_discard(fake_client: type[FakeClient]) -> None:
    fake_client.files["out/图 1.png"] = (b"\x89PNG", "image/png", "image")
    path = await refs.materialize("sandbox:out/图 1.png", GROUP, media_type="image", max_bytes=1024)
    assert path.read_bytes() == b"\x89PNG"
    assert refs.is_export_path(path)
    assert path.parent.parent.name == str(GROUP)
    assert fake_client.calls[-1][1] == ("g123456", "out/图 1.png")
    refs.discard(path)
    assert not path.parent.exists()


@pytest.mark.asyncio
async def test_materialize_checks_media_kind_and_missing(fake_client: type[FakeClient]) -> None:
    fake_client.files["song.mid"] = (b"MThd", "audio/midi", "midi")
    with pytest.raises(ValueError, match="不是音频"):
        await refs.materialize("sandbox:song.mid", GROUP, media_type="record", max_bytes=1024)
    with pytest.raises(tool_args.ToolError) as exc:
        await refs.materialize("sandbox:nope.png", GROUP, max_bytes=1024)
    assert exc.value.code == tool_args.ERROR_NOT_FOUND


def test_prune_exports_removes_stale() -> None:
    stale = refs.export_root() / str(GROUP) / "old"
    stale.mkdir(parents=True)
    (stale / "a.png").write_bytes(b"x")
    fresh = refs.export_root() / str(GROUP) / "new"
    fresh.mkdir()
    assert refs.prune_exports(older_than=-1) == 2
    assert not stale.exists() and not fresh.exists()


@pytest.mark.asyncio
async def test_outbound_refuses_direct_export_path() -> None:
    target = refs.export_root() / str(GROUP) / "abc" / "a.png"
    with pytest.raises(PermissionError, match="sandbox: 引用"):
        await outbound._resolve_media(str(target), "image", session=None, group_id=GROUP)


@pytest.mark.asyncio
async def test_outbound_materializes_sandbox_ref(fake_client: type[FakeClient]) -> None:
    fake_client.files["chart.png"] = (b"\x89PNG", "image/png", "image")
    path, temporary = await outbound._resolve_media("sandbox:chart.png", "image", session=None, group_id=GROUP)
    assert temporary is True and refs.is_export_path(path)


# ---- handler -------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_python_success_reports_quota(fake_client: type[FakeClient]) -> None:
    result = await handler.handle("run_python", {"code": "print('hi')", "purpose": "测试", "timeout_seconds": 10}, _context())
    payload = result.result
    assert payload["ok"] is True and payload["stdout"] == "hi"
    assert payload["runs_left_today"] == policy.SandboxSettings().member_daily_runs - 1
    _name, args, kwargs = fake_client.calls[-1]
    assert args[0] == "g123456"
    assert kwargs["limits"]["timeout_seconds"] == 10
    assert kwargs["meta"] == {"source": "agent", "group_id": GROUP, "user_id": USER, "purpose": "测试"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "code"), [("busy", "upstream"), ("rejected", "invalid_args")])
async def test_run_python_refunds_when_not_executed(fake_client: type[FakeClient], status: str, code: str) -> None:
    fake_client.run_result = {"status": status, "message": "满了"}
    with pytest.raises(tool_args.ToolError) as exc:
        await handler.handle("run_python", {"code": "1"}, _context())
    assert exc.value.code == getattr(tool_args, f"ERROR_{code.upper()}")
    assert policy.member_usage(GROUP, USER) == 0


@pytest.mark.asyncio
async def test_run_python_refunds_on_transport_error(fake_client: type[FakeClient]) -> None:
    fake_client.run_result = client_mod.SandboxError("offline", "断了")
    with pytest.raises(tool_args.ToolError) as exc:
        await handler.handle("run_python", {"code": "1"}, _context())
    assert exc.value.code == tool_args.ERROR_UPSTREAM
    assert policy.group_usage(GROUP) == 0


@pytest.mark.asyncio
async def test_run_python_maps_policy_errors(fake_client: type[FakeClient]) -> None:
    _save(member_daily_runs=0)
    with pytest.raises(tool_args.ToolError) as exc:
        await handler.handle("run_python", {"code": "1"}, _context())
    assert exc.value.code == tool_args.ERROR_QUOTA
    assert not fake_client.calls


@pytest.mark.asyncio
async def test_run_python_rejects_empty_code(fake_client: type[FakeClient]) -> None:
    with pytest.raises(tool_args.ToolError):
        await handler.handle("run_python", {"code": "   "}, _context())
    assert policy.group_usage(GROUP) == 0


@pytest.mark.asyncio
async def test_read_file_text_and_binary(fake_client: type[FakeClient]) -> None:
    fake_client.files["data.csv"] = (b"a,b\n1,2\n", "text/csv", "table")
    fake_client.files["chart.png"] = (b"\x89PNG", "image/png", "image")
    fake_client.files["book.xlsx"] = (b"PK", "application/vnd.ms-excel", "table")
    result = await handler.handle("read_sandbox_file", {"path": "sandbox:data.csv"}, _context())
    assert result.result["content"] == "a,b\n1,2"
    for path in ("chart.png", "book.xlsx"):
        with pytest.raises(tool_args.ToolError, match="不是文本"):
            await handler.handle("read_sandbox_file", {"path": path}, _context())
    with pytest.raises(tool_args.ToolError) as exc:
        await handler.handle("read_sandbox_file", {"path": "../x"}, _context())
    assert exc.value.code == tool_args.ERROR_INVALID_ARGS


@pytest.mark.asyncio
async def test_file_tools_respect_open_scope(fake_client: type[FakeClient]) -> None:
    _save(enabled=False)
    for name, args in (("list_sandbox_files", {}), ("read_sandbox_file", {"path": "a"}), ("delete_sandbox_file", {"path": "a"})):
        with pytest.raises(tool_args.ToolError) as exc:
            await handler.handle(name, args, _context(admin=True))
        assert exc.value.code == tool_args.ERROR_PERMISSION
    assert not fake_client.calls
    _save(admin_only=True)
    with pytest.raises(tool_args.ToolError):
        await handler.handle("list_sandbox_files", {}, _context(admin=False))
    listing = await handler.handle("list_sandbox_files", {}, _context(admin=True))
    assert listing.result["files"][0]["ref"] == "sandbox:a.png"


@pytest.mark.asyncio
async def test_delete_refunds_when_nothing_removed(fake_client: type[FakeClient]) -> None:
    fake_client.files["a.txt"] = (b"x", "text/plain", "text")
    removed = await handler.handle("delete_sandbox_file", {"path": "a.txt"}, _context())
    missing = await handler.handle("delete_sandbox_file", {"path": "b.txt"}, _context())
    assert removed.refund_quota is False
    assert missing.refund_quota is True


@pytest.mark.asyncio
async def test_info_reports_offline_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_health(monkeypatch, None, client_mod.SandboxError("offline", "连不上"))
    result = await handler.handle("sandbox_info", {}, _context())
    assert result.result["available"] is False
    assert "连不上" in result.result["reason"]


# ---- 说话核验 / 审计 -----------------------------------------------------


@pytest.mark.parametrize("text", ["代码已经跑完了，结果是 42。", "折线图已经画好了！", "我运行了代码，跑出来的结果在这"])
def test_sandbox_claim_requires_run_python(text: str) -> None:
    unverified = claims.check_action_claims(text, set())
    assert unverified.changed and "sandbox_run" in unverified.families
    assert not claims.check_action_claims(text, {"run_python"}).changed


def test_sandbox_claim_ignores_plans() -> None:
    assert not claims.check_action_claims("我可以帮你画个折线图。", set()).changed


# ---- 可靠性：冷却、重试、失败上报、终端 -----------------------------------------


@pytest.fixture
def no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler, "_BUSY_RETRY_SECONDS", 0.0)
    monkeypatch.setattr(handler, "_OFFLINE_RETRY_SECONDS", 0.0)


@pytest.mark.asyncio
async def test_short_cooldown_is_waited_out_instead_of_failing(monkeypatch: pytest.MonkeyPatch) -> None:
    _save(member_cooldown_seconds=2)
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(policy.asyncio, "sleep", fake_sleep)
    await policy.reserve_run(GROUP, USER, actor_is_admin=False)
    second = await policy.reserve_run(GROUP, USER, actor_is_admin=False)
    assert slept and 0 < slept[0] <= 2
    assert second.waited_seconds > 0


@pytest.mark.asyncio
async def test_skip_cooldown_and_refund_restores_previous_timestamp() -> None:
    _save(member_cooldown_seconds=60)
    await policy.reserve_run(GROUP, USER, actor_is_admin=False)
    # 同一回合的修正重跑不吃冷却。
    retry = await policy.reserve_run(GROUP, USER, actor_is_admin=False, skip_cooldown=True)
    retry.refund()
    with pytest.raises(policy.SandboxPolicyError) as exc:
        await policy.reserve_run(GROUP, USER, actor_is_admin=False)
    assert exc.value.reason == "cooldown"


@pytest.mark.asyncio
async def test_refund_clears_cooldown_when_code_never_ran() -> None:
    _save(member_cooldown_seconds=60)
    first = await policy.reserve_run(GROUP, USER, actor_is_admin=False)
    first.refund()
    await policy.reserve_run(GROUP, USER, actor_is_admin=False)


@pytest.mark.asyncio
async def test_second_run_in_same_turn_skips_cooldown(fake_client: type[FakeClient]) -> None:
    _save(member_cooldown_seconds=60)
    context = _context()
    await handler.handle("run_python", {"code": "1/0"}, context)
    result = await handler.handle("run_python", {"code": "print(1)"}, context)
    assert result.result["ok"] is True
    assert context.ledger.sandbox_runs == 2
    with pytest.raises(tool_args.ToolError) as exc:
        await handler.handle("run_python", {"code": "1"}, _context())
    assert exc.value.code == tool_args.ERROR_QUOTA


@pytest.mark.asyncio
async def test_busy_runner_is_retried_once(
    fake_client: type[FakeClient], no_retry_wait: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = [{"status": "busy", "message": "满了"}, {"status": "ok", "stdout": "2", "files": []}]

    async def run(self: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        FakeClient.calls.append(("run", args, kwargs))
        return results.pop(0)

    monkeypatch.setattr(fake_client, "run", run)
    result = await handler.handle("run_python", {"code": "1+1"}, _context())
    assert result.result["stdout"] == "2"
    assert len(fake_client.calls) == 2
    assert policy.member_usage(GROUP, USER) == 1


@pytest.mark.asyncio
async def test_offline_runner_is_retried_after_health_refresh(
    fake_client: type[FakeClient], no_retry_wait: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    refreshed: list[bool] = []

    async def fake_health(*, refresh: bool = False) -> tuple[Any, Any]:
        refreshed.append(refresh)
        return HEALTHY, None

    monkeypatch.setattr(handler, "cached_health", fake_health)
    outcomes: list[Any] = [client_mod.SandboxError("offline", "断了"), {"status": "ok", "stdout": "ok", "files": []}]

    async def run(self: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        item = outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(fake_client, "run", run)
    result = await handler.handle("run_python", {"code": "1"}, _context())
    assert result.result["ok"] is True
    assert True in refreshed


def test_failed_run_gets_error_summary_with_user_line() -> None:
    stderr = (
        "Traceback (most recent call last):\n"
        '  File "/tmp/yawnbot-sandbox-jobs/abcdef01/main.py", line 2, in <module>\n'
        "    import requests\n"
        "ModuleNotFoundError: No module named 'requests'\n"
    )
    payload = handler.project_run_result(
        {"status": "error", "stderr": stderr, "exit_code": 1, "duration_ms": 300},
        max_chars=3000,
        code="x = 1\nimport requests\n",
        time_limit=30,
    )
    assert payload["ok"] is False
    assert payload["error"]["line"] == 2
    assert payload["error"]["code"] == "import requests"
    assert "pip" in payload["error"]["fix"]
    assert payload["duration"] == "0.3s/30s"
    assert "/tmp/yawnbot" not in payload["stderr"]


def test_successful_run_drops_font_warnings() -> None:
    stderr = "UserWarning: Glyph 20013 (\\N{CJK}) missing from current font.\n  fig.savefig(x)\nreal message"
    payload = handler.project_run_result({"status": "ok", "stdout": "1", "stderr": stderr}, max_chars=3000)
    assert payload["stderr"] == "real message"


def test_status_hints_cover_resource_limits() -> None:
    payload = handler.project_run_result({"status": "memory_limit"}, max_chars=3000)
    assert "分块" in payload["hint"]


@pytest.mark.asyncio
async def test_run_shell_sends_shell_language(fake_client: type[FakeClient], monkeypatch: pytest.MonkeyPatch) -> None:
    _set_health(monkeypatch, {**HEALTHY, "languages": ["python", "shell"]})
    fake_client.run_result = {"status": "ok", "stdout": "3 notes.txt", "files": [], "language": "shell"}
    result = await handler.handle("run_shell", {"command": "wc -l notes.txt"}, _context())
    assert result.result["stdout"] == "3 notes.txt"
    _name, args, kwargs = fake_client.calls[-1]
    assert args[1] == "wc -l notes.txt"
    assert kwargs["language"] == "shell"


@pytest.mark.asyncio
async def test_run_shell_refused_by_old_runner(fake_client: type[FakeClient]) -> None:
    with pytest.raises(tool_args.ToolError) as exc:
        await handler.handle("run_shell", {"command": "ls"}, _context())
    assert exc.value.code == tool_args.ERROR_UNSUPPORTED
    assert "run_python" in (exc.value.hint or "")
    assert not fake_client.calls


def test_supports_language_and_client_body() -> None:
    assert client_mod.supports_language(None, "python")
    assert not client_mod.supports_language(HEALTHY, "shell")
    assert client_mod.supports_language({"languages": ["python", "shell"]}, "shell")


@pytest.mark.asyncio
async def test_client_only_sends_language_for_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies: list[dict[str, Any]] = []

    async def fake_request(self: Any, method: str, path: str, *, json_body: dict[str, Any], timeout: float) -> dict[str, Any]:
        bodies.append(json_body)
        return {"status": "ok"}

    monkeypatch.setattr(client_mod.SandboxClient, "_request", fake_request)
    sandbox = client_mod.SandboxClient()
    await sandbox.run("g1", "print(1)")
    await sandbox.run("g1", "ls", language="shell")
    assert "language" not in bodies[0]
    assert bodies[1]["language"] == "shell"


@pytest.mark.asyncio
async def test_discovery_hides_run_shell_on_old_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_mod, "peek_health", lambda: (HEALTHY, None, True))
    result = await _discover({"family": "sandbox"})
    names = {item["name"] for item in result["tools"]}
    assert "run_python" in names and "run_shell" not in names
    monkeypatch.setattr(client_mod, "peek_health", lambda: ({**HEALTHY, "languages": ["python", "shell"]}, None, True))
    result = await _discover({"family": "sandbox"})
    assert "run_shell" in {item["name"] for item in result["tools"]}


@pytest.mark.asyncio
async def test_failed_run_is_not_cached_by_ledger(fake_client: type[FakeClient]) -> None:
    fake_client.run_result = {"status": "error", "stderr": "NameError: name 'x' is not defined", "files": []}
    context = _context()
    first = await tools.execute_tool_with_meta("run_python", {"code": "x"}, context=context)
    second = await tools.execute_tool_with_meta("run_python", {"code": "x"}, context=context)
    assert len([call for call in fake_client.calls if call[0] == "run"]) == 2
    assert first.payload["ok"] is False and not second.payload.get("duplicate")
