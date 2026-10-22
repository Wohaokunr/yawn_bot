# ruff: noqa: E501, ARG001, TRY003, PLR2004
"""群聊 Agent 大改造的场景回归：参数校验、幂等、公告、文件、成员、撤回、发言清洗。"""

from __future__ import annotations

import importlib
import sys
import time
import typing
from dataclasses import replace
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
tool_args = importlib.import_module(f"{_PKG}.tool_args")
registry = importlib.import_module(f"{_PKG}.tool_registry")
router = importlib.import_module(f"{_PKG}.tool_router")
tool_support = importlib.import_module(f"{_PKG}.tool_support")
admin_handler = importlib.import_module(f"{_PKG}.tool_handlers.admin")
speech_format = importlib.import_module(f"{_PKG}.speech_format")
speech_claims = importlib.import_module(f"{_PKG}.speech_claims")
speech_quality = importlib.import_module(f"{_PKG}.speech_quality")
speech = importlib.import_module(f"{_PKG}.speech")
prompt = importlib.import_module(f"{_PKG}.prompt")
result_speech = importlib.import_module(f"{_PKG}.tool_result_speech")
safe_download = importlib.import_module(f"{_PKG}.safe_download")
capabilities = importlib.import_module(f"{_PKG}.capabilities")
route_models = importlib.import_module("src.plugins.yawn_core.webui.route_models")

GROUP_ID = 1
BOT_ID = 100
ACTOR_ID = 200
TARGET_ID = 300
ADMIN_CAPS = tools.BotGroupCapabilities(
    role="admin",
    can_manage=True,
    actions=frozenset(
        {
            "set_group_ban",
            "set_group_whole_ban",
            "_send_group_notice",
            "_get_group_notice",
            "_del_group_notice",
            "create_group_file_folder",
            "get_group_root_files",
            "get_group_files_by_folder",
            "get_group_file_system_info",
            "get_group_file_url",
            "upload_group_file",
            "rename_group_file",
            "get_group_member_list",
            "get_group_member_info",
            "set_group_special_title",
            "set_group_name",
            "delete_msg",
            "get_msg",
            "set_msg_emoji_like",
            "get_group_msg_history",
        }
    ),
)


class RecordingBot:
    """按 action 返回预设响应并记录调用。"""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.self_id = str(BOT_ID)
        self.responses = responses or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_api(self, action: str, **params: Any) -> Any:
        self.calls.append((action, params))
        response = self.responses.get(action)
        if callable(response):
            return response(**params)
        return response if response is not None else {}

    def actions(self, name: str) -> list[dict[str, Any]]:
        return [params for action, params in self.calls if action == name]


class QuotaSpy:
    def __init__(self) -> None:
        self.consumed = 0
        self.refunded = 0
        self.audits: list[dict[str, Any]] = []

    async def consume(self, *_args: Any, **_kwargs: Any) -> bool:
        self.consumed += 1
        return True

    async def refund(self, *_args: Any, **_kwargs: Any) -> None:
        self.refunded += 1

    async def audit(self, *args: Any, **kwargs: Any) -> None:
        self.audits.append({"args": args, **kwargs})


@pytest.fixture
def quota(monkeypatch: pytest.MonkeyPatch) -> QuotaSpy:
    spy = QuotaSpy()

    async def probe(*_args: Any, **_kwargs: Any) -> Any:
        return ADMIN_CAPS

    async def allow(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def can_act(*_args: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(tools, "probe_group_capabilities", probe)
    monkeypatch.setattr(tools, "_check_tool_policy", allow)
    monkeypatch.setattr(tools, "_consume_admin_quota", spy.consume)
    monkeypatch.setattr(tools, "_refund_admin_quota", spy.refund)
    monkeypatch.setattr(tools, "_audit", spy.audit)
    monkeypatch.setattr(admin_handler, "target_can_be_muted", can_act)
    monkeypatch.setattr(admin_handler, "actor_can_act_on_target", can_act)
    return spy


def _context(bot: Any, *, session: Any = None) -> Any:
    return tools.ToolExecutionContext(
        bot=bot,
        group_id=GROUP_ID,
        actor_user_id=ACTOR_ID,
        session=session,
        capabilities=ADMIN_CAPS,
    )


async def _run(name: str, args: dict[str, Any], context: Any) -> dict[str, Any]:
    return (await tools.execute_tool_with_meta(name, args, context=context)).payload


# ---------------------------------------------------------------- A. 参数校验


def test_strict_bool_never_turns_false_string_into_true() -> None:
    assert tool_args.parse_strict_bool("false", field_name="enable") is False
    assert tool_args.parse_strict_bool("否", field_name="enable") is False
    assert tool_args.parse_strict_bool("true", field_name="enable") is True
    assert tool_args.parse_strict_bool(1, field_name="enable") is True
    with pytest.raises(tool_args.ToolArgumentError):
        tool_args.parse_strict_bool("maybe", field_name="enable")


def test_integer_parsing_accepts_digit_strings_and_rejects_fractions() -> None:
    assert tool_args.parse_int("123456", field_name="user_id") == 123456
    assert tool_args.parse_int(7.0, field_name="limit") == 7
    for bad in (1.5, True, "abc"):
        with pytest.raises(tool_args.ToolArgumentError):
            tool_args.parse_int(bad, field_name="user_id")


@pytest.mark.asyncio
async def test_false_string_disables_whole_group_mute(quota: QuotaSpy) -> None:
    bot = RecordingBot()
    payload = await _run("set_whole_group_mute", {"enable": "false"}, _context(bot))

    assert payload["ok"] is True
    assert bot.actions("set_group_whole_ban") == [{"group_id": GROUP_ID, "enable": False}]
    assert payload["result"] == {"whole_group_muted": False}


@pytest.mark.asyncio
async def test_invalid_arguments_never_reach_policy_or_quota(
    quota: QuotaSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy_calls = 0

    async def counting_policy(*_args: Any, **_kwargs: Any) -> None:
        nonlocal policy_calls
        policy_calls += 1

    monkeypatch.setattr(tools, "_check_tool_policy", counting_policy)
    bot = RecordingBot()
    context = _context(bot)

    unknown = await _run("mute_member", {"user_id": 5, "duration": 60, "why": "x"}, context)
    missing = await _run("mute_member", {"duration": 60}, context)
    too_long = await _run("create_group_announcement", {"content": "长" * 1001}, context)

    assert unknown["error_code"] == "invalid_args"
    assert "duration" in unknown["hint"]
    assert "find_group_member" in missing["hint"]
    assert "1000" in too_long["error"]
    assert policy_calls == 0
    assert quota.consumed == 0
    assert bot.calls == []


@pytest.mark.asyncio
async def test_same_turn_duplicate_write_reuses_first_result(quota: QuotaSpy) -> None:
    bot = RecordingBot()
    context = _context(bot)
    args = {"user_id": TARGET_ID, "duration": 600}

    first = await _run("mute_member", args, context)
    second = await _run("mute_member", dict(args), context)

    assert first["ok"] is True
    assert second["duplicate"] is True
    assert len(bot.actions("set_group_ban")) == 1
    assert quota.consumed == 1


@pytest.mark.asyncio
async def test_controlled_calls_per_turn_are_capped(quota: QuotaSpy) -> None:
    bot = RecordingBot()
    context = _context(bot)
    results = [
        await _run("mute_member", {"user_id": TARGET_ID + index, "duration": 60}, context)
        for index in range(tool_args.MAX_CONTROLLED_CALLS_PER_TURN + 1)
    ]

    assert all(item["ok"] for item in results[:-1])
    assert results[-1]["ok"] is False
    assert results[-1]["error_code"] == "quota"
    assert quota.consumed == tool_args.MAX_CONTROLLED_CALLS_PER_TURN


@pytest.mark.asyncio
async def test_failed_side_effect_reports_sanitized_upstream_error(quota: QuotaSpy) -> None:
    def explode(**_params: Any) -> Any:
        raise RuntimeError("upstream https://secret.example/x?sig=1 at /srv/bot/data/a.txt")

    bot = RecordingBot({"set_group_ban": explode})
    payload = await _run("mute_member", {"user_id": TARGET_ID, "duration": 60}, _context(bot))

    assert payload["ok"] is False
    assert payload["error_code"] == "upstream"
    assert "secret.example" not in payload["error"]
    assert "/srv/bot" not in payload["error"]
    assert quota.refunded == 1
    assert quota.audits[-1]["error_code"] == "upstream"
    assert quota.audits[-1]["duration_ms"] >= 0


def test_audit_safe_value_truncates_long_text() -> None:
    value = tool_args.audit_safe_value("字" * 500)
    assert isinstance(value, str)
    assert len(value) <= 201


def test_error_classification_covers_common_failures() -> None:
    assert tool_args.classify_tool_error(PermissionError("今日管理工具配额已用尽")) == "quota"
    assert tool_args.classify_tool_error(PermissionError("调用者没有群管理权限")) == "permission"
    assert tool_args.classify_tool_error(LookupError("x")) == "not_found"
    assert tool_args.classify_tool_error(ValueError("bad")) == "invalid_args"
    assert tool_args.classify_tool_error(RuntimeError("boom")) == "upstream"


# ---------------------------------------------------------------- B. 公告


@pytest.mark.asyncio
async def test_announcement_options_markup_and_notice_id(quota: QuotaSpy) -> None:
    bot = RecordingBot({"_get_group_notice": [], "_send_group_notice": {"notice_id": "n9"}})
    payload = await _run(
        "create_group_announcement",
        {"content": "## 通知\n**周六**开会[CQ:at,qq=all]", "pinned": "true", "confirm_required": True},
        _context(bot),
    )

    sent = bot.actions("_send_group_notice")[0]
    assert sent["content"] == "通知\n周六开会"
    assert sent["pinned"] == 1
    assert sent["confirm_required"] == 1
    assert "is_show_edit_card" not in sent
    assert payload["result"]["notice_id"] == "n9"
    assert payload["result"]["pinned"] is True


@pytest.mark.asyncio
async def test_duplicate_announcement_is_rejected_and_refunded(quota: QuotaSpy) -> None:
    bot = RecordingBot(
        {
            "_get_group_notice": [
                {"notice_id": "old", "publish_time": 1_700_000_000, "message": {"text": "周六晚上八点开会", "images": []}}
            ]
        }
    )
    payload = await _run("create_group_announcement", {"content": "周六晚上八点开会。"}, _context(bot))

    assert payload["ok"] is False
    assert payload["error_code"] == "duplicate"
    assert "old" in payload["hint"]
    assert bot.actions("_send_group_notice") == []
    assert quota.refunded == 1


@pytest.mark.asyncio
async def test_punctuation_only_announcement_is_rejected(quota: QuotaSpy) -> None:
    bot = RecordingBot({"_get_group_notice": []})
    payload = await _run("create_group_announcement", {"content": "！！。。"}, _context(bot))

    assert payload["ok"] is False
    assert bot.actions("_send_group_notice") == []


@pytest.mark.asyncio
async def test_notice_list_is_sorted_filtered_and_readable(quota: QuotaSpy) -> None:
    bot = RecordingBot(
        {
            "_get_group_notice": [
                {"notice_id": "a", "publish_time": 1_700_000_000, "message": {"text": "旧的活动公告"}},
                {"notice_id": "b", "publish_time": 1_800_000_000, "message": {"text": "新的活动公告", "images": [{}]}},
                {"notice_id": "c", "publish_time": 1_750_000_000, "message": "群规"},
            ]
        }
    )
    payload = await _run("list_group_notices", {"keyword": "活动", "limit": 5}, _context(bot))

    assert [item["notice_id"] for item in payload["result"]] == ["b", "a"]
    assert payload["result"][0]["content"] == "新的活动公告[图片x1]"
    assert payload["result"][0]["publish_time"].startswith("2027-01-15")
    assert all("_sort_time" not in item for item in payload["result"])


@pytest.mark.asyncio
async def test_delete_notice_requires_existing_notice(quota: QuotaSpy) -> None:
    bot = RecordingBot({"_get_group_notice": [{"notice_id": "keep", "message": "群规"}]})
    missing = await _run("delete_group_notice", {"notice_id": "nope"}, _context(bot))
    deleted = await _run("delete_group_notice", {"notice_id": "keep"}, _context(bot))

    assert missing["error_code"] == "not_found"
    assert deleted["result"]["content_preview"] == "群规"
    assert bot.actions("_del_group_notice") == [{"group_id": GROUP_ID, "notice_id": "keep"}]


# ---------------------------------------------------------------- C. 群文件


@pytest.fixture
def file_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "agent_files"
    (root / "docs").mkdir(parents=True)
    (root / "reactions").mkdir()
    (root / ".secret").mkdir()
    (root / "docs" / "群规.pdf").write_bytes(b"%PDF-1.4")
    (root / "notes.txt").write_text("hello", encoding="utf-8")
    (root / "tool.exe").write_bytes(b"MZ")
    (root / "reactions" / "smile.png").write_bytes(b"png")
    (root / ".secret" / "key.txt").write_text("k", encoding="utf-8")
    monkeypatch.setattr(tool_support, "_FILE_ROOT", root.resolve())
    return root


@pytest.mark.asyncio
async def test_list_agent_files_hides_unsafe_and_internal_files(
    quota: QuotaSpy, file_root: Path
) -> None:
    payload = await _run("list_agent_files", {}, _context(RecordingBot()))

    names = [item["file"] for item in payload["result"]["items"]]
    assert names == ["docs/群规.pdf", "notes.txt"]
    assert str(file_root) not in str(payload)


@pytest.mark.asyncio
async def test_send_file_uses_relative_name_folder_and_extension(
    quota: QuotaSpy, file_root: Path
) -> None:
    bot = RecordingBot({"get_group_root_files": {"files": [], "folders": [{"folder_id": "/d1", "folder_name": "资料"}]}})
    payload = await _run(
        "send_file",
        {"file": "docs/群规.pdf", "name": "../最新群规", "folder_id": "/d1"},
        _context(bot),
    )

    upload = bot.actions("upload_group_file")[0]
    assert upload["name"] == "最新群规.pdf"
    assert upload["folder"] == "/d1"
    assert Path(upload["file"]) == (file_root / "docs" / "群规.pdf").resolve()
    assert payload["result"] == {"uploaded": True, "name": "最新群规.pdf", "size": "8B", "folder": "资料"}


@pytest.mark.asyncio
async def test_send_file_rejects_scripts_missing_files_and_unknown_folders(
    quota: QuotaSpy, file_root: Path
) -> None:
    bot = RecordingBot({"get_group_root_files": {"files": [], "folders": []}})
    script = await _run("send_file", {"file": "tool.exe", "name": "tool.exe"}, _context(bot))
    renamed = await _run("send_file", {"file": "notes.txt", "name": "notes.bat"}, _context(bot))
    missing = await _run("send_file", {"file": "nope.pdf", "name": "nope.pdf"}, _context(bot))
    folder = await _run("send_file", {"file": "notes.txt", "name": "n", "folder_id": "/x"}, _context(bot))

    assert script["ok"] is False
    assert renamed["ok"] is False
    assert missing["error_code"] == "not_found"
    assert folder["error_code"] == "not_found"
    assert bot.actions("upload_group_file") == []
    assert quota.refunded == 4


@pytest.mark.asyncio
async def test_search_group_files_covers_first_level_folders(quota: QuotaSpy) -> None:
    bot = RecordingBot(
        {
            "get_group_root_files": {
                "files": [{"file_id": "f1", "file_name": "期末复习.pdf", "upload_time": 10, "file_size": 2048}],
                "folders": [{"folder_id": "d1", "folder_name": "课件"}],
            },
            "get_group_files_by_folder": {"files": [{"file_id": "f2", "file_name": "复习提纲.docx", "upload_time": 20}]},
        }
    )
    payload = await _run("search_group_files", {"keyword": "复习"}, _context(bot))

    files = payload["result"]["files"]
    assert [item["file_id"] for item in files] == ["f2", "f1"]
    assert files[0]["folder"] == "课件"
    assert files[1]["size"] == "2.0KB"


@pytest.mark.asyncio
async def test_file_space_link_default_busid_and_idempotent_folder(quota: QuotaSpy) -> None:
    bot = RecordingBot(
        {
            "get_group_file_system_info": {"file_count": 3, "limit_count": 10000, "used_space": 1024, "total_space": 4096},
            "get_group_file_url": {"url": "https://example.invalid/f"},
            "get_group_root_files": {"files": [], "folders": [{"folder_id": "/d", "folder_name": "资料"}]},
        }
    )
    context = _context(bot)
    space = await _run("get_group_file_space", {}, context)
    await _run("get_group_file_link", {"file_id": "f1"}, context)
    folder = await _run("create_group_folder", {"name": "资料"}, context)

    assert space["result"] == {"file_count": 3, "file_limit": 10000, "used": "1.0KB", "total": "4.0KB", "free": "3.0KB"}
    assert bot.actions("get_group_file_url")[0]["busid"] == 102
    assert folder["result"]["existed"] is True
    assert bot.actions("create_group_file_folder") == []
    assert quota.refunded == 1


@pytest.mark.asyncio
async def test_rename_rejects_path_characters(quota: QuotaSpy) -> None:
    bot = RecordingBot()
    payload = await _run(
        "rename_group_file",
        {"file_id": "f", "current_parent_directory": "/", "new_name": "../a.txt"},
        _context(bot),
    )

    assert payload["error_code"] == "invalid_args"
    assert bot.actions("rename_group_file") == []


class _Response:
    def __init__(self, *, location: str | None = None, length: int | None = None) -> None:
        self.is_redirect = location is not None
        self.headers = {"content-type": "application/pdf"}
        if location:
            self.headers["location"] = location
        if length is not None:
            self.headers["content-length"] = str(length)

    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self, _size: int) -> typing.AsyncIterator[bytes]:
        yield b"%PDF"


def _client_factory(responses: dict[str, _Response]) -> type:
    class Client:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            assert kwargs.get("follow_redirects") is False

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        def stream(self, _method: str, url: str) -> _Response:
            return responses[url]

    return Client


@pytest.mark.asyncio
async def test_download_redirect_cannot_escape_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = {
        "https://cdn.example/a.pdf": _Response(location="https://evil.example/a.pdf"),
        "https://cdn.example/b.pdf": _Response(location="/real/b.pdf"),
        "https://cdn.example/real/b.pdf": _Response(),
        "https://cdn.example/big.pdf": _Response(length=10_000),
    }
    monkeypatch.setattr(safe_download.httpx, "AsyncClient", _client_factory(responses))
    allowed = frozenset({"cdn.example"})

    with pytest.raises(PermissionError):
        await safe_download.stream_download("https://cdn.example/a.pdf", allowed_hosts=allowed, max_bytes=100, suffix=".pdf")
    with pytest.raises(ValueError, match="大小"):
        await safe_download.stream_download("https://cdn.example/big.pdf", allowed_hosts=allowed, max_bytes=100, suffix=".pdf")
    path, content_type = await safe_download.stream_download(
        "https://cdn.example/b.pdf", allowed_hosts=allowed, max_bytes=100, suffix=".pdf"
    )
    try:
        assert path.read_bytes() == b"%PDF"
        assert content_type == "application/pdf"
    finally:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------- D. 成员与管理

_MEMBERS = [
    {"user_id": BOT_ID, "nickname": "小明机器人", "role": "admin"},
    {"user_id": 1, "nickname": "小明", "card": "", "role": "owner"},
    {"user_id": 2, "nickname": "abc", "card": "小明同学", "role": "member"},
    {"user_id": 3, "nickname": "大明", "card": "", "role": "admin"},
    {"user_id": 4, "nickname": "路人", "card": "", "role": "member"},
]


@pytest.mark.asyncio
async def test_find_group_member_ranks_exact_then_prefix_and_skips_bot(quota: QuotaSpy) -> None:
    bot = RecordingBot({"get_group_member_list": _MEMBERS})
    payload = await _run("find_group_member", {"query": "@小明"}, _context(bot))

    items = payload["result"]["items"]
    assert [item["user_id"] for item in items] == [1, 2]
    assert items[0]["exact"] is True
    assert items[1]["nickname"] == "abc"
    assert payload["result"]["ambiguous"] is False


@pytest.mark.asyncio
async def test_admin_list_and_role_filter(quota: QuotaSpy) -> None:
    bot = RecordingBot({"get_group_member_list": _MEMBERS})
    admins = await _run("list_group_admins", {}, _context(bot))
    members = await _run("list_group_members", {"role": "member"}, _context(bot))

    assert [item["user_id"] for item in admins["result"]["items"]] == [1, BOT_ID, 3]
    assert [item["user_id"] for item in members["result"]["items"]] == [2, 4]


def test_unmute_and_bot_recall_follow_parent_allowlist() -> None:
    assert registry.allowlist_covers("unmute_member", {"mute_member"})
    assert registry.allowlist_covers("recall_bot_message", {"recall_message"})
    assert not registry.allowlist_covers("unmute_member", {"send_file"})
    schemas = tools.build_tool_schemas(
        ADMIN_CAPS,
        allow_admin_tools=True,
        privileged_allowlist={"mute_member"},
    )
    names = {item["function"]["name"] for item in schemas}
    assert {"mute_member", "unmute_member"} <= names
    assert "recall_message" not in names


@pytest.mark.asyncio
async def test_mute_returns_until_unmute_uses_zero_and_self_is_rejected(quota: QuotaSpy) -> None:
    bot = RecordingBot()
    context = _context(bot)
    muted = await _run("mute_member", {"user_id": TARGET_ID, "duration": 600}, context)
    unmuted = await _run("unmute_member", {"user_id": TARGET_ID}, context)
    myself = await _run("mute_member", {"user_id": BOT_ID, "duration": 60}, _context(bot))

    assert muted["result"]["until"]
    assert unmuted["result"] == {"user_id": TARGET_ID, "muted": False}
    assert bot.actions("set_group_ban")[-1]["duration"] == 0
    assert myself["error_code"] == "permission"


class _RecallSession:
    def __init__(self, message_id: int | None) -> None:
        self.message_id = message_id

    async def scalar(self, _stmt: Any) -> Any:
        return SimpleNamespace(message_id=self.message_id) if self.message_id else None

    async def rollback(self) -> None:
        return None


@pytest.mark.asyncio
async def test_recall_bot_message_defaults_to_latest_own_message(quota: QuotaSpy) -> None:
    bot = RecordingBot(
        {
            "get_msg": lambda message_id: {
                "message_id": message_id,
                "group_id": GROUP_ID,
                "user_id": BOT_ID if message_id == 55 else TARGET_ID,
                "time": int(time.time()),
            }
        }
    )
    own = await _run("recall_bot_message", {}, _context(bot, session=_RecallSession(55)))
    other = await _run("recall_bot_message", {"message_id": 66}, _context(bot, session=_RecallSession(None)))
    none = await _run("recall_bot_message", {}, _context(bot, session=_RecallSession(None)))

    assert own["result"] == {"message_id": 55, "recalled": True, "own_message": True}
    assert other["ok"] is False
    assert none["error_code"] == "not_found"
    assert bot.actions("delete_msg") == [{"message_id": 55}]


@pytest.mark.asyncio
async def test_recall_message_checks_target_hierarchy(
    quota: QuotaSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def cannot(*_args: Any, **_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(admin_handler, "target_can_be_muted", cannot)
    bot = RecordingBot({"get_msg": {"message_id": 7, "group_id": GROUP_ID, "user_id": TARGET_ID}})
    payload = await _run("recall_message", {"message_id": 7}, _context(bot))

    assert payload["ok"] is False
    assert bot.actions("delete_msg") == []


@pytest.mark.asyncio
async def test_title_bytes_and_single_line_names(
    quota: QuotaSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_caps = tools.BotGroupCapabilities(role="owner", can_manage=True, actions=ADMIN_CAPS.actions)

    async def owner_probe(*_args: Any, **_kwargs: Any) -> Any:
        return owner_caps

    monkeypatch.setattr(tools, "probe_group_capabilities", owner_probe)
    bot = RecordingBot({"get_group_member_info": {"user_id": TARGET_ID}})
    context = tools.ToolExecutionContext(
        bot=bot, group_id=GROUP_ID, actor_user_id=ACTOR_ID, session=None, capabilities=owner_caps
    )
    too_long = await tools.execute_tool_with_meta(
        "set_special_title", {"user_id": TARGET_ID, "special_title": "超级无敌宇宙第一"}, context=context
    )
    named = await tools.execute_tool_with_meta("set_group_name", {"group_name": "新\n群名\t测试"}, context=context)

    assert too_long.payload["ok"] is False
    assert "18" in too_long.payload["hint"]
    assert bot.actions("set_group_name")[0]["group_name"] == "新 群名 测试"
    assert named.payload["result"] == {"group_name": "新 群名 测试"}


@pytest.mark.asyncio
async def test_reaction_can_be_removed(quota: QuotaSpy) -> None:
    class Session:
        async def scalar(self, _stmt: Any) -> Any:
            return object()

    bot = RecordingBot()
    payload = await _run(
        "react_to_message",
        {"message_id": 9, "emoji_id": "66", "remove": True},
        _context(bot, session=Session()),
    )

    assert payload["result"]["reacted"] is False
    assert bot.actions("set_msg_emoji_like") == [{"message_id": 9, "emoji_id": "66", "set": False}]


# ---------------------------------------------------------------- E. 历史


@pytest.mark.asyncio
async def test_recent_messages_filter_by_user_and_keyword(quota: QuotaSpy) -> None:
    messages = [
        {"message_id": index, "user_id": 2 if index % 2 else 3, "time": 1_700_000_000 + index, "message": [{"type": "text", "data": {"text": f"第{index}句 {'作业' if index > 5 else '闲聊'}"}}]}
        for index in range(1, 11)
    ]
    messages[-1]["message"].insert(0, {"type": "reply", "data": {"id": "3"}})
    bot = RecordingBot({"get_group_msg_history": {"messages": messages}})
    payload = await _run(
        "get_recent_group_messages", {"count": 2, "user_id": 3, "keyword": "作业"}, _context(bot)
    )

    items = payload["result"]["items"]
    assert bot.actions("get_group_msg_history")[0]["count"] == 30
    assert [item["message_id"] for item in items] == [8, 10]
    assert items[-1]["reply_to_message_id"] == 3
    assert items[0]["time"].startswith("2023-11-15")


# ---------------------------------------------------------------- F. 发现与路由


@pytest.mark.asyncio
async def test_discovery_returns_args_and_prerequisites(monkeypatch: pytest.MonkeyPatch) -> None:
    async def probe(*_args: Any, **_kwargs: Any) -> Any:
        return ADMIN_CAPS

    monkeypatch.setattr(tools, "probe_group_capabilities", probe)
    result = await tools._discover_tools_for_actor(
        query="置顶一下这个公告",
        family=None,
        limit=5,
        bot=RecordingBot(),
        group_id=GROUP_ID,
        actor_user_id=ACTOR_ID,
        session=None,
        capabilities=ADMIN_CAPS,
        actor_can_manage=True,
        privileged_allowlist={"create_group_announcement", "mute_member"},
    )
    rows = {item["name"]: item for item in result["tools"]}

    assert "create_group_announcement" in rows
    notice = rows["create_group_announcement"]
    assert "content:string" in notice["args"]["required"]
    assert "pinned:boolean" in notice["args"]["optional"]
    assert notice["requires"]


def test_fuzzy_and_colloquial_routing() -> None:
    assert router.suggest_tool_families("帮我把这个公告置顶，再发个文件") == ("notice", "file")
    assert router.suggest_tool_families("") == ()
    assert "at" in router.select_dialogue_message_segment_types("提醒一下他交作业")
    assert "reply" in router.select_dialogue_message_segment_types("回他一下")
    assert router.MAX_TOOL_ROUNDS == 5


def test_webui_allowlist_literal_matches_controlled_tools() -> None:
    field = route_models.AgentConfigPatch.model_fields["tool_allowlist"]
    literal = typing.get_args(typing.get_args(field.annotation)[0])[0]
    assert set(typing.get_args(literal)) == set(registry.CONTROLLED_TOOLS)


# ---------------------------------------------------------------- G. 发言清洗


@pytest.mark.parametrize(
    ("raw", "expected", "code"),
    [
        ("### 结论\n**重点**在这", "结论\n重点在这", "markdown_heading"),
        ("- 第一\n- 第二", "· 第一\n· 第二", "markdown_list"),
        ("看 [文档](https://x.example/a)", "看 文档 https://x.example/a", "markdown_link"),
        ("| 名字 | 数量 |\n|---|---|\n| a | 1 |", "名字 / 数量\na / 1", "markdown_table"),
        ("真的吗？？？？？", "真的吗？？", "punctuation_run"),
        ("哈哈哈哈哈哈哈哈", "哈哈哈哈", "laughter_run"),
        ("好耶😂😂😂😂", "好耶😂😂", "emoji_run"),
        ("作为一个AI，我没有感情。不过可以聊聊。", "不过可以聊聊。", "ai_disclaimer"),
        ("答案是 42。希望对你有帮助！", "答案是 42。", "helpdesk_closing"),
        ("回复：在呢", "在呢", "role_label_prefix"),
        ('好了 {"ok": true, "result": 1} 搞定', "好了 搞定", "leaked_tool_json"),
        ("我读了 current_turn。答案是 42。", "答案是 42。", "leaked_internal_field"),
        ("小明（123456789）说得对", "小明说得对", "leaked_qq_number"),
        ("今天天气不错。今天天气不错。", "今天天气不错。", "duplicate_sentence"),
    ],
)
def test_qq_text_normalizer(raw: str, expected: str, code: str) -> None:
    result = speech_format.normalize_qq_text(raw)
    assert result.text == expected
    assert code in result.codes


def test_normalizer_keeps_code_blocks_and_plain_text() -> None:
    fenced = "```\n**keep** | a | b |\n```"
    assert speech_format.normalize_qq_text(fenced).text == fenced
    assert speech_format.normalize_qq_text("2*3*4=24").text == "2*3*4=24"
    assert speech_format.normalize_qq_text("正常的一句话。").codes == ()


def test_unverified_action_claims_are_removed_but_verified_kept() -> None:
    claimed = speech_claims.check_action_claims("好的。已经把他禁言十分钟了。", frozenset())
    verified = speech_claims.check_action_claims("已经把他禁言十分钟了。", frozenset({"mute_member"}))
    only_claim = speech_claims.check_action_claims("公告已经发布好了！", frozenset())
    reading = speech_claims.check_action_claims("我查了一下禁言列表，现在没人。", frozenset())
    question = speech_claims.check_action_claims("要不要我帮你禁言他？", frozenset())

    assert claimed.text == "好的。"
    assert claimed.families == ("mute",)
    assert verified.changed is False
    assert only_claim.text == speech_claims.UNVERIFIED_FALLBACK
    assert reading.changed is False
    assert question.changed is False


def test_finalize_applies_format_claims_and_quote_mention() -> None:
    plan = speech.speech_plan_from_text(
        "@小明 **好了**，已经撤回那条消息了。",
        scene="direct_reply",
        target=speech.SpeechTarget(reply_to_message_id=5),
    )
    unverified = speech_quality.finalize_speech_plan(replace(plan, verified_tools=()))
    no_check = speech_quality.finalize_speech_plan(plan)
    codes = {item.code for item in unverified.issues}

    assert unverified.text == speech_claims.UNVERIFIED_FALLBACK
    assert {"markdown_emphasis", "redundant_quote_mention", "unverified_action_claim"} <= codes
    assert no_check.text == "好了，已经撤回那条消息了。"


def test_plain_text_mention_is_flagged() -> None:
    plan = speech.speech_plan_from_text("叫 @小红 过来看看")
    final = speech_quality.finalize_speech_plan(plan)
    assert "plain_text_mention" in {item.code for item in final.issues}
    assert not speech_format.has_plain_text_mention("邮箱 a@b.example")


# ---------------------------------------------------------------- H. Prompt 与发言证据


def _tool(name: str) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "parameters": {}}}


def test_prompt_realtime_layer_carries_clock_role_and_toolpack_hint() -> None:
    persona = {"name": "Yawn"}
    turn = {
        "user_id": 2,
        "name": "甲",
        "content": "帮我发个公告",
        "trigger": "mention",
        "received_at": "2026-10-22T13:55:10+08:00",
    }
    messages, fingerprint = prompt.build_messages(
        persona=persona,
        tools=[_tool("send_message"), _tool("discover_tools"), _tool("create_group_announcement"), _tool("mute_member")],
        context={"group_id": 1},
        user_prompt="",
        current_turn=turn,
        bot_role="admin",
    )
    _plain, plain_fingerprint = prompt.build_messages(
        persona=persona, tools=[], context={"group_id": 1}, user_prompt="", current_turn=turn
    )
    joined = "\n".join(str(item["content"]) for item in messages)

    assert '"clock":{"now":"2026-10-22 13:55","weekday":"周四"}' in joined
    assert '"bot_role":"admin"' in joined
    assert '"suggested_toolpacks":["notice"]' in joined
    assert "书面、完整的公告正文" in joined
    assert "find_group_member" in joined
    assert "不渲染 Markdown" in joined
    assert fingerprint == plain_fingerprint


def test_tool_speech_evidence_is_specific() -> None:
    muted = result_speech.build_speech_evidence(
        "mute_member", {"ok": True, "result": {"until": "2026-10-22 14:05"}}
    )
    duplicate = result_speech.build_speech_evidence("mute_member", {"ok": True, "duplicate": True, "result": {}})
    failed = result_speech.build_speech_evidence(
        "send_file", {"ok": False, "error": "x", "error_code": "not_found", "hint": "先用 list_agent_files"}
    )
    notice = result_speech.build_speech_evidence(
        "create_group_announcement", {"ok": True, "result": {"pinned": True}}
    )

    assert "2026-10-22 14:05" in muted.summary
    assert "只算一次" in duplicate.summary
    assert failed.prompt_dict()["hint"] == "先用 list_agent_files"
    assert "not_found" in failed.summary
    assert "置顶" in notice.summary


def test_cached_bot_role_never_probes() -> None:
    capabilities.reset_capability_cache()
    assert capabilities.cached_bot_role(SimpleNamespace(self_id=BOT_ID), GROUP_ID) is None
