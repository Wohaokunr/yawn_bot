# ruff: noqa: E501, PLR2004, ARG001, ARG002, RUF012
"""群聊附件入箱：取字节链路、命名去重、配额、自动入箱、工具与隐私清理。

全部用假 OneBot 与假 SandboxClient，不需要真正的沙箱容器。
"""

from __future__ import annotations

import base64
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
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
policy = importlib.import_module(f"{_PKG}.sandbox_policy")
client_mod = importlib.import_module(f"{_PKG}.sandbox_client")
inbox = importlib.import_module(f"{_PKG}.sandbox_inbox")
store = importlib.import_module(f"{_PKG}.sandbox_inbox_store")
inbox_tools = importlib.import_module(f"{_PKG}.tool_handlers.inbox")
sandbox_tools = importlib.import_module(f"{_PKG}.tool_handlers.sandbox")
claims = importlib.import_module(f"{_PKG}.speech_claims")
preload = importlib.import_module(f"{_PKG}.tool_preload")
parser = importlib.import_module(f"{_PKG}.message_parser")
tool_args = importlib.import_module(f"{_PKG}.tool_args")

GROUP = 123456
USER = 42
HEALTHY = {"ok": True, "isolation": {"isolated": True}, "accepting_runs": True, "packages": [], "python": "3.12", "max_upload_mb": 20}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
WAV = b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 32


@pytest.fixture(autouse=True)
def _state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(policy, "STATE_DIR", tmp_path / "state")
    policy.reset_state_for_tests()
    client_mod.reset_health_cache()

    async def fake_health(*, refresh: bool = False) -> tuple[Any, Any]:
        return HEALTHY, None

    for module in (policy, inbox, sandbox_tools, client_mod):
        monkeypatch.setattr(module, "cached_health", fake_health)


class FakeClient:
    written: dict[str, bytes] = {}
    existing: dict[str, int] = {}
    fail: Exception | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def list_files(self, workspace: str, *, prefix: str = "", limit: int = 200) -> dict[str, Any]:
        return {"items": [{"path": path, "size": size} for path, size in FakeClient.existing.items()], "total": len(FakeClient.existing)}

    async def write_file(self, workspace: str, path: str, data: bytes) -> dict[str, Any]:
        if FakeClient.fail is not None:
            raise FakeClient.fail
        FakeClient.written[path] = data
        return {"file": {"path": path, "size": len(data), "kind": "image" if path.endswith(".png") else "other"}}

    async def run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"status": "ok", "stdout": "done", "files": [], "run_id": "r1", "duration_ms": 10}


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> type[FakeClient]:
    FakeClient.written = {}
    FakeClient.existing = {}
    FakeClient.fail = None
    monkeypatch.setattr(inbox, "SandboxClient", FakeClient)
    monkeypatch.setattr(sandbox_tools, "SandboxClient", FakeClient)
    return FakeClient


class FakeBot:
    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_api(self, action: str, **params: Any) -> Any:
        self.calls.append((action, params))
        value = self.responses.get(action)
        if isinstance(value, Exception):
            raise value
        return value


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _context(bot: Any = None, *, attachments: tuple[Any, ...] = (), user: int | None = USER) -> Any:
    context = tools.ToolExecutionContext(
        bot=bot or FakeBot(),
        group_id=GROUP,
        actor_user_id=user,
        session=None,
        capabilities=tools.BotGroupCapabilities(role="member", can_manage=False, actions=frozenset()),
        actor_can_manage=False,
        privileged_allowlist=frozenset(),
    )
    context.attachments = attachments
    return context


# ---- 文件名、域名、类型 ------------------------------------------------------


def test_clean_name_strips_dirs_and_neutralizes_executables() -> None:
    assert inbox.clean_name("../../etc/passwd") == "passwd"
    assert inbox.clean_name("C:\\Users\\a\\报表 2026.xlsx") == "报表_2026.xlsx"
    assert inbox.clean_name("virus.exe") == "virus.exe.txt"
    assert inbox.clean_name("\x00\x01") == ""
    assert len(inbox.clean_name("长" * 300 + ".csv")) <= 80


def test_host_allowed_matches_suffix_but_not_lookalikes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_SANDBOX_INBOX_HOSTS", raising=False)
    assert inbox.host_allowed("multimedia.nt.qq.com.cn")
    assert inbox.host_allowed("njc-download.ftn.qq.com")
    assert not inbox.host_allowed("evilqq.com")
    assert not inbox.host_allowed("qq.com.evil.cn")
    monkeypatch.setenv("AGENT_SANDBOX_INBOX_HOSTS", "files.example.org")
    assert inbox.host_allowed("files.example.org")
    assert not inbox.host_allowed("njc-download.ftn.qq.com")


@pytest.mark.parametrize(
    ("data", "name", "expected"),
    [
        (PNG, None, ".png"),
        (b"\xff\xd8\xff\xe0" + b"0" * 20, "x.png", ".jpg"),
        (WAV, None, ".wav"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", None, ".webp"),
        (b"\x02#!SILK_V3" + b"0" * 10, None, ".silk"),
        (b"PK\x03\x04" + b"0" * 20, "报表.xlsx", ".xlsx"),
        (b"PK\x03\x04" + b"0" * 20, "a.bin", ".zip"),
        (b"\x00\x00\x00\x18ftypmp42" + b"0" * 10, None, ".mp4"),
        (b"plain text", "notes.md", ".md"),
    ],
)
def test_sniff_extension(data: bytes, name: str | None, expected: str) -> None:
    assert inbox.sniff_extension(data, name) == expected


def test_inbox_filename_uses_content_hash_for_media_and_name_for_files() -> None:
    image = inbox.Attachment(key="1", media_type="image", source="current", ref={"type": "image"}, index=1)
    assert inbox.inbox_filename(image, PNG).startswith("图片_") and inbox.inbox_filename(image, PNG).endswith(".png")
    assert inbox.inbox_filename(image, PNG) == inbox.inbox_filename(image, PNG)
    doc = inbox.Attachment(key="2", media_type="file", source="current", ref={"type": "file", "name": "成绩表"}, index=2)
    assert inbox.inbox_filename(doc, b"PK\x03\x04" + b"0" * 20) == "成绩表.zip"


def test_attachment_name_never_exposes_paths_or_urls() -> None:
    for raw in ("/root/.config/QQ/a.jpg", "https://x/y.jpg", "C:\\a\\b.png", "base64://AAAA"):
        item = inbox.Attachment(key="1", media_type="file", source="current", ref={"type": "file", "file": raw}, index=1)
        assert item.name is None


def test_collect_current_keys_match_prompt_indexes() -> None:
    refs = [
        {"type": "image", "file": "a.jpg"},
        {"type": "face"},
        {"type": "file", "name": "a.csv", "source": "reply", "source_message_id": 99, "source_user_id": 7},
    ]
    items = inbox.collect_current(refs, message_id=10)
    assert [item.key for item in items] == ["1", "3"]
    assert items[0].message_id == 10
    assert items[1].message_id == 99 and items[1].sender_id == 7


def test_message_parser_maps_napcat_file_size_and_name() -> None:
    normalized = parser.normalize_message(
        [{"type": "file", "data": {"file": "数据.xlsx", "file_id": "/abc", "file_size": "2048"}}]
    )
    ref = normalized.media_refs[0]
    assert ref["size"] == "2048"
    assert inbox.collect_current(normalized.media_refs)[0].declared_size == 2048


# ---- 取字节 ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_prefers_segment_base64() -> None:
    item = inbox.Attachment(key="1", media_type="image", source="current", ref={"type": "image", "file": f"base64://{_b64(PNG)}"}, index=1)
    result = await inbox.fetch_bytes(None, GROUP, item, max_bytes=1024)
    assert result.data == PNG and result.via == "segment:base64"


@pytest.mark.asyncio
async def test_fetch_record_asks_onebot_for_mp3() -> None:
    bot = FakeBot({"get_record": {"file": "/napcat/tmp/a.mp3", "base64": _b64(b"ID3" + b"0" * 30)}})
    item = inbox.Attachment(key="1", media_type="record", source="current", ref={"type": "record", "file": "abc.amr"}, index=1)
    result = await inbox.fetch_bytes(bot, GROUP, item, max_bytes=1024)
    assert result.via == "get_record:base64"
    assert bot.calls[0] == ("get_record", {"file": "abc.amr", "out_format": "mp3"})


@pytest.mark.asyncio
async def test_fetch_group_file_uses_file_url(monkeypatch: pytest.MonkeyPatch) -> None:
    downloads: list[str] = []

    async def fake_download(url: str, max_bytes: int, attempts: Any) -> Any:
        downloads.append(url)
        return inbox._Fetch(b"a,b\n1,2\n", "url")

    monkeypatch.setattr(inbox, "_download", fake_download)
    bot = FakeBot({"get_group_file_url": {"url": "https://njc-download.ftn.qq.com/x"}})
    item = inbox.Attachment(key="1", media_type="file", source="current", ref={"type": "file", "file_id": "/f1", "name": "a.csv"}, index=1)
    result = await inbox.fetch_bytes(bot, GROUP, item, max_bytes=1024)
    assert result.data.startswith(b"a,b") and result.via == "group_file:url"
    assert bot.calls[0][1]["busid"] == 102


@pytest.mark.asyncio
async def test_fetch_rejects_declared_oversize_without_download() -> None:
    bot = FakeBot()
    item = inbox.Attachment(key="1", media_type="file", source="current", ref={"type": "file", "file_id": "/f", "size": 10_000}, index=1)
    with pytest.raises(inbox.InboxError) as exc:
        await inbox.fetch_bytes(bot, GROUP, item, max_bytes=1000)
    assert exc.value.reason == "too_large"
    assert bot.calls == []


@pytest.mark.asyncio
async def test_fetch_reports_unavailable_and_blocked_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_SANDBOX_INBOX_HOSTS", raising=False)
    item = inbox.Attachment(key="1", media_type="video", source="current", ref={"type": "video", "url": "https://evil.example/v.mp4"}, index=1)
    with pytest.raises(inbox.InboxError) as exc:
        await inbox.fetch_bytes(None, GROUP, item, max_bytes=1000)
    assert exc.value.reason == "host_blocked"
    plain = inbox.Attachment(key="1", media_type="image", source="current", ref={"type": "image", "file": "x.jpg"}, index=1)
    with pytest.raises(inbox.InboxError) as exc:
        await inbox.fetch_bytes(FakeBot({"get_image": None, "get_file": None}), GROUP, plain, max_bytes=1000)
    assert exc.value.reason == "unavailable"


@pytest.mark.asyncio
async def test_fetch_local_path_only_inside_configured_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "a.png").write_bytes(PNG)
    outside = tmp_path / "secret.png"
    outside.write_bytes(PNG)
    item = inbox.Attachment(key="1", media_type="image", source="current", ref={"type": "image", "path": str(shared / "a.png")}, index=1)
    with pytest.raises(inbox.InboxError):
        await inbox.fetch_bytes(None, GROUP, item, max_bytes=1000)
    monkeypatch.setenv("AGENT_SANDBOX_INBOX_LOCAL_ROOTS", str(shared))
    assert (await inbox.fetch_bytes(None, GROUP, item, max_bytes=1000)).via == "segment:shared_dir"
    sneaky = inbox.Attachment(key="1", media_type="image", source="current", ref={"type": "image", "path": str(shared / ".." / "secret.png")}, index=1)
    with pytest.raises(inbox.InboxError):
        await inbox.fetch_bytes(None, GROUP, sneaky, max_bytes=1000)


@pytest.mark.asyncio
async def test_history_attachment_refreshes_via_get_msg() -> None:
    bot = FakeBot({"get_msg": {"message": [{"type": "image", "data": {"file": f"base64://{_b64(PNG)}"}}]}})
    item = inbox.Attachment(key="m5.1", media_type="image", source="history", ref={"type": "image", "file": "[redacted]"}, index=1, message_id=777)
    result = await inbox.fetch_bytes(bot, GROUP, item, max_bytes=1000)
    assert result.data == PNG
    assert bot.calls[0] == ("get_msg", {"message_id": 777})


# ---- 入箱 --------------------------------------------------------------------


def _image(key: str = "1", data: bytes = PNG) -> Any:
    return inbox.Attachment(key=key, media_type="image", source="current", ref={"type": "image", "file": f"base64://{_b64(data)}"}, index=int(key), message_id=10)


@pytest.mark.asyncio
async def test_import_writes_inbox_and_records_usage(fake_client: type[FakeClient]) -> None:
    outcomes = await inbox.import_attachments(None, GROUP, [_image()], actor_user_id=USER, trigger="tool")
    assert outcomes[0].status == "imported"
    path = outcomes[0].path
    assert path.startswith("inbox/图片_") and fake_client.written[path] == PNG
    assert policy.inbox_usage(GROUP) == {"files": 1, "bytes": len(PNG)}
    event = store.recent_events(group_id=GROUP)[0]
    assert event["status"] == "imported" and event["path"] == path and "url" not in event
    row = outcomes[0].model_row()
    assert row["ref"] == f"sandbox:{path}" and "PIL" in row["how"]


@pytest.mark.asyncio
async def test_import_reuses_same_content_and_renames_collisions(fake_client: type[FakeClient]) -> None:
    first = (await inbox.import_attachments(None, GROUP, [_image()], actor_user_id=USER, trigger="tool"))[0]
    fake_client.existing = {first.path: len(PNG)}
    again = (await inbox.import_attachments(None, GROUP, [_image()], actor_user_id=USER, trigger="tool"))[0]
    assert again.status == "reused" and again.path == first.path
    fake_client.existing = {"inbox/a.csv": 3}
    doc = inbox.Attachment(key="1", media_type="file", source="current", ref={"type": "file", "name": "a.csv", "file": f"base64://{_b64(b'x,y,z')}"}, index=1)
    renamed = (await inbox.import_attachments(None, GROUP, [doc], actor_user_id=USER, trigger="tool"))[0]
    assert renamed.status == "imported" and renamed.path != "inbox/a.csv" and renamed.path.startswith("inbox/a_")


@pytest.mark.asyncio
async def test_import_skips_download_when_same_named_file_exists(fake_client: type[FakeClient]) -> None:
    fake_client.existing = {"inbox/报表.xlsx": 2048}
    bot = FakeBot()
    doc = inbox.Attachment(key="1", media_type="file", source="current", ref={"type": "file", "name": "报表.xlsx", "size": 2048, "file_id": "/x"}, index=1)
    outcome = (await inbox.import_attachments(bot, GROUP, [doc], actor_user_id=USER, trigger="tool"))[0]
    assert outcome.status == "reused" and bot.calls == []


@pytest.mark.asyncio
async def test_import_respects_type_switch_daily_cap_and_quota(fake_client: type[FakeClient]) -> None:
    policy.save_settings(policy.SandboxSettings(inbox_types=["file"]))
    skipped = (await inbox.import_attachments(None, GROUP, [_image()], actor_user_id=USER, trigger="tool"))[0]
    assert skipped.status == "skipped" and skipped.reason == "type_disabled"

    policy.save_settings(policy.SandboxSettings(inbox_group_daily_mb=1))
    policy.record_inbox_usage(GROUP, files=1, size=2 * 1024 * 1024)
    capped = (await inbox.import_attachments(None, GROUP, [_image()], actor_user_id=USER, trigger="tool"))[0]
    assert capped.reason == "daily_limit"

    policy.save_settings(policy.SandboxSettings())
    policy.reset_state_for_tests()
    fake_client.fail = client_mod.SandboxError("quota_exceeded", "满了", status=507)
    full = (await inbox.import_attachments(None, GROUP, [_image()], actor_user_id=USER, trigger="tool"))[0]
    assert full.reason == "quota"
    assert "delete_sandbox_file" in full.model_row()["hint"]


@pytest.mark.asyncio
async def test_one_failure_does_not_block_others(fake_client: type[FakeClient]) -> None:
    broken = inbox.Attachment(key="2", media_type="image", source="current", ref={"type": "image"}, index=2)
    outcomes = await inbox.import_attachments(None, GROUP, [_image(), broken], actor_user_id=USER, trigger="tool")
    assert [item.status for item in outcomes] == ["imported", "failed"]


# ---- 自动入箱 -----------------------------------------------------------------


def _turn(selected: set[str], refs: list[dict[str, Any]]) -> Any:
    rebuilt: list[bool] = []
    return SimpleNamespace(
        tool_context=_context(),
        normalized=SimpleNamespace(media_refs=refs),
        message_id=10,
        group_id=GROUP,
        selected_tool_names=frozenset(selected),
        context={},
        succeeded_tools=set(),
        rebuild_messages=lambda: rebuilt.append(True),
        rebuilt=rebuilt,
    )


@pytest.mark.asyncio
async def test_prepare_turn_imports_when_sandbox_loaded(fake_client: type[FakeClient]) -> None:
    turn = _turn({"run_python"}, [{"type": "image", "file": f"base64://{_b64(PNG)}"}])
    await inbox.prepare_turn_inbox(turn)
    payload = turn.context["sandbox_inbox"]
    assert payload["in_workspace"][0]["id"] == "1"
    assert turn.tool_context.inbox["1"].startswith("inbox/")
    assert "import_attachments" in turn.succeeded_tools and turn.rebuilt


@pytest.mark.asyncio
async def test_prepare_turn_modes(fake_client: type[FakeClient]) -> None:
    refs = [{"type": "image", "file": f"base64://{_b64(PNG)}"}]
    plain = _turn({"send_message"}, refs)
    await inbox.prepare_turn_inbox(plain)
    assert "sandbox_inbox" not in plain.context
    assert len(plain.tool_context.attachments) == 1  # 仍记下附件，供之后的工具使用

    policy.save_settings(policy.SandboxSettings(inbox_auto_import="always"))
    always = _turn({"send_message"}, refs)
    await inbox.prepare_turn_inbox(always)
    assert "sandbox_inbox" in always.context

    policy.save_settings(policy.SandboxSettings(inbox_auto_import="off"))
    off = _turn({"run_python"}, refs)
    await inbox.prepare_turn_inbox(off)
    assert "sandbox_inbox" not in off.context


@pytest.mark.asyncio
async def test_prepare_turn_skips_closed_groups(fake_client: type[FakeClient]) -> None:
    policy.save_settings(policy.SandboxSettings(blocked_groups=[GROUP]))
    turn = _turn({"run_python"}, [{"type": "image", "file": f"base64://{_b64(PNG)}"}])
    await inbox.prepare_turn_inbox(turn)
    assert "sandbox_inbox" not in turn.context and fake_client.written == {}


# ---- 工具 --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_tool_defaults_to_current_attachments(fake_client: type[FakeClient]) -> None:
    context = _context(attachments=(_image(),))
    result = await inbox_tools.handle("import_attachments", {}, context)
    assert result.result["imported"][0]["path"].startswith("inbox/")
    assert context.inbox["1"]


@pytest.mark.asyncio
async def test_import_tool_falls_back_to_actor_recent(fake_client: type[FakeClient], monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_recent(session: Any, group_id: int, **kwargs: Any) -> list[Any]:
        seen.update(kwargs)
        item = _image("1")
        item.key, item.source, item.message_id = "m9.1", "history", 99
        return [item]

    monkeypatch.setattr(inbox_tools, "collect_recent", fake_recent)
    result = await inbox_tools.handle("import_attachments", {}, _context())
    assert seen["user_id"] == USER
    assert result.result["imported"][0]["id"] == "m9.1"


@pytest.mark.asyncio
async def test_import_tool_unknown_ids_and_no_actor(fake_client: type[FakeClient], monkeypatch: pytest.MonkeyPatch) -> None:
    async def nothing(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(inbox_tools, "collect_recent", nothing)
    with pytest.raises(tool_args.ToolError) as exc:
        await inbox_tools.handle("import_attachments", {"ids": ["m1.1"]}, _context(attachments=(_image(),)))
    assert exc.value.code == tool_args.ERROR_NOT_FOUND
    with pytest.raises(tool_args.ToolError):
        await inbox_tools.handle("import_attachments", {}, _context(user=None))


@pytest.mark.asyncio
async def test_list_tool_marks_imported(fake_client: type[FakeClient], monkeypatch: pytest.MonkeyPatch) -> None:
    async def nothing(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(inbox_tools, "collect_recent", nothing)
    context = _context(attachments=(_image(),))
    context.inbox["1"] = "inbox/图片_x.png"
    result = (await inbox_tools.handle("list_chat_attachments", {}, context)).result
    assert result["current_turn"][0]["in_workspace"] == "sandbox:inbox/图片_x.png"


@pytest.mark.asyncio
async def test_tools_respect_closed_sandbox(fake_client: type[FakeClient]) -> None:
    policy.save_settings(policy.SandboxSettings(enabled=False))
    with pytest.raises(tool_args.ToolError):
        await inbox_tools.handle("import_attachments", {}, _context(attachments=(_image(),)))


@pytest.mark.asyncio
async def test_run_python_auto_imports_pending_attachments(fake_client: type[FakeClient]) -> None:
    context = _context(attachments=(_image(),))
    result = await sandbox_tools.handle("run_python", {"code": "print(1)"}, context)
    assert result.result["attachments"]["items"][0]["path"].startswith("inbox/")
    again = await sandbox_tools.handle("run_python", {"code": "print(2)"}, context)
    assert "attachments" not in again.result


@pytest.mark.asyncio
async def test_import_group_file_tool(fake_client: type[FakeClient], monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_download(url: str, max_bytes: int, attempts: Any) -> Any:
        return inbox._Fetch(b"name,score\n", "url")

    monkeypatch.setattr(inbox, "_download", fake_download)
    bot = FakeBot({"get_group_file_url": {"url": "https://njc-download.ftn.qq.com/x"}})
    result = (await inbox_tools.handle("import_group_file", {"file_id": "/f", "name": "成绩.csv"}, _context(bot))).result
    assert result["imported"]["path"] == "inbox/成绩.csv"


# ---- 记录、隐私、声明、路由信号 -----------------------------------------------


def test_store_forget_member_and_paths() -> None:
    store.record_events(
        [
            {"group_id": GROUP, "user_id": USER, "status": "imported", "path": "inbox/a.png", "message_id": 1, "index": 1},
            {"group_id": GROUP, "user_id": 7, "sender_id": USER, "status": "imported", "path": "inbox/b.png", "message_id": 2, "index": 1},
            {"group_id": GROUP, "user_id": 7, "status": "failed", "reason": "expired"},
        ]
    )
    assert store.imported_paths(GROUP) == {(1, 1): "inbox/a.png", (2, 1): "inbox/b.png"}
    assert store.summary()["failed"] == 1
    assert store.forget_paths(GROUP, {"inbox/b.png"}) == 1
    assert (2, 1) not in store.imported_paths(GROUP)
    assert store.forget_member(GROUP, USER) == 2
    assert len(store.recent_events(group_id=GROUP)) == 1


def test_store_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "MAX_EVENTS", 10)
    monkeypatch.setattr(store, "_TRIM_SLACK", 2)
    store.record_events([{"group_id": GROUP, "status": "imported", "index": i} for i in range(20)])
    assert len(store.recent_events(limit=100)) == 10


def test_unverified_import_claim_is_removed() -> None:
    check = claims.check_action_claims("好的，我已经把图片放进电脑了。", set())
    assert check.changed and "sandbox_import" in check.families
    kept = claims.check_action_claims("好的，我已经把图片放进电脑了。", {"import_attachments"})
    assert not kept.changed


def test_router_signals_report_attachment_types_only() -> None:
    signals = preload.plan_signals(
        has_reply=False,
        has_media=True,
        mentions_others=False,
        media_refs=[{"type": "image", "url": "https://x"}, {"type": "record"}, {"type": "file", "name": "秘密.xlsx"}],
    )
    assert signals == {"reply": False, "media": True, "mentions_others": False, "image": True, "audio": True, "file": True}


# ── 1GB 级大文件：私聊走下载链接，不碰会回 base64 的接口 ──────────────


class _ApiBot:
    def __init__(self, replies: dict[str, Any]) -> None:
        self.replies = replies
        self.calls: list[str] = []

    async def call_api(self, action: str, **params: Any) -> Any:
        self.calls.append(action)
        return self.replies.get(action)


@pytest.mark.asyncio
async def test_private_big_file_uses_url_api_and_skips_base64(monkeypatch: pytest.MonkeyPatch) -> None:
    fetched: list[str] = []

    async def fake_download(url: str, max_bytes: int, attempts: Any) -> Any:
        fetched.append(url)
        return inbox._Fetch(b"apk-bytes", "url")

    monkeypatch.setattr(inbox, "_download", fake_download)
    bot = _ApiBot({"get_private_file_url": {"url": "https://example.com/a.apk"}})
    result = await inbox._fetch_via_api(
        bot, 0, "file", {"file_id": "f1", "file": "a.apk"}, 2 * 1024**3, inbox._Attempts(), big=True
    )
    assert result is not None and result.data == b"apk-bytes"
    assert bot.calls == ["get_private_file_url"] and fetched == ["https://example.com/a.apk"]

    empty = _ApiBot({})
    assert await inbox._fetch_via_api(
        empty, 0, "file", {"file_id": "f1", "file": "a.apk"}, 2 * 1024**3, inbox._Attempts(), big=True
    ) is None
    assert "get_file" not in empty.calls


def test_private_inbox_limit_defaults_to_1gb_and_clamps_to_runner() -> None:
    settings = policy.SandboxSettings()
    assert inbox.effective_max_bytes(settings, {"max_upload_mb": 4096}, private=True) == 1024 * 1024 * 1024
    assert inbox.effective_max_bytes(settings, {"max_upload_mb": 64}, private=True) == 64 * 1024 * 1024
    assert inbox.effective_max_bytes(settings, None) == settings.inbox_max_file_mb * 1024 * 1024
