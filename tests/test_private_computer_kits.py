# ruff: noqa: ANN001, ANN003, ANN201, ARG001, ARG002, E501, PLR2004, RUF012
"""虚拟电脑能力包：Bot 侧目录与沙箱能力一一对应、computer_kits 渐进式披露、
computer_do 的参数校验 / 代码生成 / 结果解析 / 联网开关，以及 APK 清单解析。"""

from __future__ import annotations

import importlib
import inspect
import json
import struct
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest
import test_private_agent  # noqa: F401 - 先初始化 nonebot 并加载插件

_PKG = "src.plugins.yawn_core"
kits = importlib.import_module(f"{_PKG}.yawn_agent.computer_kits")
computer = importlib.import_module(f"{_PKG}.yawn_agent.private_computer")
policy = importlib.import_module(f"{_PKG}.yawn_agent.sandbox_policy")
ptools = importlib.import_module(f"{_PKG}.yawn_agent.private_tools")

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = ROOT / "sandbox"
USER = 10001

HEALTHY = {
    "ok": True,
    "isolation": {"isolated": True},
    "accepting_runs": True,
    "packages": [],
    "python": "3.12",
    "network": {"control": True, "default_allowed": True, "block_private": True, "guarded": True},
}


def _sandbox_caps():
    if str(SANDBOX) not in sys.path:
        sys.path.insert(0, str(SANDBOX))
    return importlib.import_module("yawnkit.caps")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "STATE_DIR", tmp_path / "sandbox")
    policy.reset_state_for_tests()

    async def fake_health(*, refresh: bool = False):
        return HEALTHY, None

    for module in (policy, computer):
        monkeypatch.setattr(module, "cached_health", fake_health)
    yield
    policy.reset_state_for_tests()


# ── 目录与沙箱能力对齐 ─────────────────────────────────────────────


def test_catalog_matches_sandbox_registry_and_signatures() -> None:
    caps = _sandbox_caps()
    registry = caps.load_all()
    assert set(registry) == set(kits.CAPS)
    assert len(kits.CAPS) >= 100
    assert set(kits.PACKS) == set(caps.PACKS)
    assert kits.MARK == caps.MARK
    for name, fn in registry.items():
        properties, required = kits.schema_for(name)
        params = inspect.signature(fn).parameters
        assert list(properties) == list(params), name
        needed = [key for key, param in params.items() if param.default is inspect.Parameter.empty]
        assert sorted(required) == sorted(needed), name


def test_every_pack_has_capabilities_and_descriptions() -> None:
    listed = kits.list_packs()
    assert listed["total"] == len(kits.CAPS)
    assert {row["pack"] for row in listed["packs"]} == set(kits.PACKS)
    assert all(row["count"] > 0 for row in listed["packs"])
    for pack in kits.PACKS:
        opened = kits.open_pack(pack)
        assert opened is not None
        for row in opened["capabilities"]:
            assert row["does"] and row["name"].startswith(f"{pack}.")
    assert kits.open_pack("nope") is None
    # 枚举参数进 schema
    props, _ = kits.schema_for("image.flip")
    assert props["direction"]["enum"] == ["horizontal", "vertical"]


def test_toolkit_tools_are_registered_for_main_and_workers() -> None:
    names = {spec["name"] for spec in computer.COMPUTER_TOOL_SPECS}
    assert {"computer_kits", "computer_do"} <= names
    assert {"computer_kits", "computer_do"} <= ptools.COMPUTER_NAMES
    kits_tool = next(t for t in ptools.ALL_TOOLS if t.name == "computer_kits")
    assert kits_tool.writes is False


@pytest.mark.asyncio
async def test_computer_kits_list_and_open() -> None:
    listed = await computer.execute_computer_tool("computer_kits", {"action": "list"}, USER)
    assert listed["total"] == len(kits.CAPS)
    opened = await computer.execute_computer_tool("computer_kits", {"action": "open", "pack": "APK"}, USER)
    assert opened["pack"] == "apk"
    with pytest.raises(computer.ToolError) as caught:
        await computer.execute_computer_tool("computer_kits", {"action": "open", "pack": "xyz"}, USER)
    assert "image" in (caught.value.hint or "")


# ── computer_do ─────────────────────────────────────────────────


class _Client:
    codes: list[str] = []
    stdout = ""
    files: list[dict[str, Any]] = []
    networks: list[Any] = []

    async def run(self, workspace, code, **kwargs):
        _Client.codes.append(code)
        _Client.networks.append(kwargs.get("network"))
        return {"status": "ok", "exit_code": 0, "stdout": _Client.stdout, "files": _Client.files}


@pytest.fixture
def fake_client(monkeypatch):
    _Client.codes = []
    _Client.networks = []
    _Client.files = []
    _Client.stdout = ""
    monkeypatch.setattr(computer, "SandboxClient", _Client)
    return _Client


def _marked(payload: dict[str, Any], noise: str = "") -> str:
    return f"{noise}\n{kits.MARK}{json.dumps(payload, ensure_ascii=False)}{kits.MARK}\n"


@pytest.mark.asyncio
async def test_computer_do_runs_capability_and_returns_result(fake_client) -> None:
    fake_client.stdout = _marked({"ok": True, "result": {"width": 800}}, noise="[音乐已保存] x")
    fake_client.files = [{"path": "out/a_800.png", "kind": "image", "size": 10}]
    result = await computer.execute_computer_tool(
        "computer_do",
        {"capability": "image.resize", "args": {"path": "sandbox:inbox/a.png", "width": "800"}},
        USER,
    )
    assert result["ok"] is True and result["result"] == {"width": 800}
    assert result["files"] and "图片" in result["next"]
    code = fake_client.codes[0]
    assert "from yawnkit.caps import run" in code
    # 参数经过 schema 规范化（字符串数字转整数、去掉 sandbox: 前缀）
    assert '\\"width\\": 800' in code and "inbox/a.png" in code and "sandbox:" not in code
    assert fake_client.networks == [True]


@pytest.mark.asyncio
async def test_computer_do_accepts_json_string_args_and_single_item_lists(fake_client) -> None:
    fake_client.stdout = _marked({"ok": True, "result": {}})
    await computer.execute_computer_tool(
        "computer_do",
        {"capability": "pdf.merge", "args": json.dumps({"paths": "a.pdf"})},
        USER,
    )
    assert '[\\"a.pdf\\"]' in fake_client.codes[0]


@pytest.mark.asyncio
async def test_computer_do_reports_capability_error_and_missing_result(fake_client) -> None:
    fake_client.stdout = _marked({"ok": False, "error": "找不到文件：a.png"})
    failed = await computer.execute_computer_tool(
        "computer_do", {"capability": "image.info", "args": {"path": "a.png"}}, USER
    )
    assert failed["ok"] is False and "找不到" in failed["error"] and "open image" in failed["hint"]
    fake_client.stdout = "Killed"
    lost = await computer.execute_computer_tool(
        "computer_do", {"capability": "image.info", "args": {"path": "a.png"}}, USER
    )
    assert lost["ok"] is False and "不要编造" in lost["hint"]


@pytest.mark.asyncio
async def test_computer_do_validates_before_running(fake_client) -> None:
    with pytest.raises(computer.ToolError) as unknown:
        await computer.execute_computer_tool("computer_do", {"capability": "image.blur"}, USER)
    assert "computer_kits open image" in (unknown.value.hint or "")
    with pytest.raises(computer.ToolError):
        await computer.execute_computer_tool("computer_do", {"capability": "image.info", "args": {}}, USER)
    with pytest.raises(computer.ToolError):
        await computer.execute_computer_tool(
            "computer_do", {"capability": "image.info", "args": {"path": "a", "bogus": 1}}, USER
        )
    with pytest.raises(computer.ToolError):
        await computer.execute_computer_tool(
            "computer_do", {"capability": "image.flip", "args": {"path": "a", "direction": "diagonal"}}, USER
        )
    assert fake_client.codes == []


@pytest.mark.asyncio
async def test_net_pack_respects_private_network_switch(fake_client) -> None:
    policy.save_settings(policy.SandboxSettings(private_network=False))
    with pytest.raises(computer.ToolError) as caught:
        await computer.execute_computer_tool(
            "computer_do", {"capability": "net.download", "args": {"url": "https://example.com/a.zip"}}, USER
        )
    assert caught.value.code == "permission_denied" or "联网" in str(caught.value)
    assert fake_client.codes == []
    info = await computer.computer_info(USER)
    assert info["network"]["online"] is False


@pytest.mark.asyncio
async def test_computer_info_reports_network_and_toolkits() -> None:
    info = await computer.computer_info(USER)
    assert info["network"]["online"] is True
    assert str(len(kits.CAPS)) in info["toolkits"]
    assert any("net.download" in tip for tip in info["tips"])


def test_generated_code_survives_tricky_strings(tmp_path) -> None:
    """生成的代码在真实解释器里跑：引号、换行、反斜杠、中文都原样传到能力里。"""

    tricky = 'a"b\'c\\n\n中文 """ {x}'
    code = computer._cap_code("dev.encode", {"text": tricky, "kind": "hex"})
    done = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(SANDBOX), "PATH": "/usr/bin:/bin"},
        check=False,
        timeout=60,
    )
    parsed = computer._parse_cap_output(done.stdout)
    assert parsed is not None and parsed["ok"], done.stderr
    assert bytes.fromhex(parsed["result"]["text"]).decode() == tricky


# ── 沙箱能力本身（只测纯标准库的）───────────────────────────────


def test_stdlib_capabilities_run(tmp_path, monkeypatch) -> None:
    caps = _sandbox_caps()
    monkeypatch.chdir(tmp_path)
    Path("a.txt").write_text("hello\nworld\nhello\n", encoding="utf-8")
    assert caps.call("files.wc", {"path": "a.txt"})["ok"]
    assert caps.call("dev.base_convert", {"value": "ff", "from_base": 16})["result"]["dec"] == "255"
    assert caps.call("text.dedupe_lines", {"path": "a.txt"})["result"]["after"] == 2
    assert caps.call("archive.zip", {"paths": ["a.txt"]})["ok"]
    assert caps.call("archive.test", {"path": "out/打包.zip"})["ok"]
    assert caps.call("dev.jwt_decode", {"token": "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjF9.x"})["result"]["expired"] is True
    escaped = caps.call("files.head", {"path": "../etc/passwd"})
    assert escaped["ok"] is False and "相对路径" in escaped["error"]
    assert caps.call("nope.nothing", {})["ok"] is False
    assert "参数不对" in caps.call("files.wc", {"bogus": 1})["error"]


def _axml(package: str, version: str, permissions: list[str]) -> bytes:
    """造一个最小的二进制 AndroidManifest.xml（UTF-16 字符串池 + 元素）。"""

    strings = ["manifest", "package", "versionName", "uses-permission", "name", package, version, *permissions]
    offsets, blob = [], b""
    for text in strings:
        offsets.append(len(blob))
        encoded = text.encode("utf-16-le")
        blob += struct.pack("<H", len(text)) + encoded + b"\x00\x00"
    blob += b"\x00" * (-len(blob) % 4)
    header = 28
    pool = struct.pack("<IIIII", len(strings), 0, 0, header + 4 * len(strings), 0)
    body = pool + struct.pack(f"<{len(strings)}I", *offsets) + blob
    pool_chunk = struct.pack("<HHI", 0x0001, header, 8 + len(body)) + body

    def element(name: int, attrs: list[tuple[int, int]]) -> tuple[bytes, bytes]:
        ext = struct.pack("<iiHHHHHH", -1, name, 20, 20, len(attrs), 0, 0, 0)
        attr_bytes = b"".join(struct.pack("<iiiHBBI", -1, key, value, 8, 0, 0x03, value) for key, value in attrs)
        start = struct.pack("<HHI", 0x0102, 16, 16 + len(ext) + len(attr_bytes)) + struct.pack("<Ii", 1, -1) + ext + attr_bytes
        end = struct.pack("<HHI", 0x0103, 16, 24) + struct.pack("<Ii", 1, -1) + struct.pack("<ii", -1, name)
        return start, end

    root_start, root_end = element(0, [(1, 5), (2, 6)])
    perms = b""
    for index in range(len(permissions)):
        start, end = element(3, [(4, 7 + index)])
        perms += start + end
    chunks = pool_chunk + root_start + perms + root_end
    return struct.pack("<HHI", 0x0003, 8, 8 + len(chunks)) + chunks


def test_apk_manifest_parser(tmp_path, monkeypatch) -> None:
    caps = _sandbox_caps()
    monkeypatch.chdir(tmp_path)
    manifest = _axml("com.example.demo", "1.2.3", ["android.permission.CAMERA", "android.permission.INTERNET"])
    with zipfile.ZipFile("demo.apk", "w") as archive:
        archive.writestr("AndroidManifest.xml", manifest)
        archive.writestr("classes.dex", b"\x00https://api.example.com/v1\x00")
        archive.writestr("lib/arm64-v8a/libflutter.so", b"x")
    info = caps.call("apk.info", {"path": "demo.apk"})
    assert info["ok"], info
    assert info["result"]["package"] == "com.example.demo"
    assert info["result"]["version_name"] == "1.2.3"
    assert info["result"]["dangerous_permissions"] == ["CAMERA"]
    assert info["result"]["abis"] == ["arm64-v8a"]
    perms = caps.call("apk.permissions", {"path": "demo.apk"})["result"]
    assert perms["dangerous"] == 1 and len(perms["permissions"]) == 2
    assert caps.call("apk.native_libs", {"path": "demo.apk"})["result"]["frameworks"] == ["Flutter"]
    assert "https://api.example.com/v1" in caps.call("apk.strings", {"path": "demo.apk"})["result"]["items"]
