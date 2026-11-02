# ruff: noqa: ANN001, ANN003, ARG001, ARG002, E501, F811, PLR2004
"""私聊文件读不到（2026-11-01，v0.5.3）：私聊不套群聊入箱类型、协议端链接放宽、Bot 端读文档兜底。"""

from __future__ import annotations

import importlib
import io
import zipfile
from types import SimpleNamespace
from typing import Any

import pytest
from test_private_agent import (  # noqa: F401  (fixture)
    NOW,
    USER,
    ChatMessage,
    ChatSession,
    _Bot,
    _message,
    _request,
    _script,
    maker,
)

_PKG = "src.plugins.yawn_core.yawn_agent"
inbox = importlib.import_module(f"{_PKG}.sandbox_inbox")
policy = importlib.import_module(f"{_PKG}.sandbox_policy")
pcomputer = importlib.import_module(f"{_PKG}.private_computer")
pagent = importlib.import_module(f"{_PKG}.private_agent")
ftext = importlib.import_module(f"{_PKG}.private_file_text")


def _docx(*paragraphs: str) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", f'<w:document xmlns:w="x"><w:body>{body}</w:body></w:document>')
    return buffer.getvalue()


def _pptx() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("ppt/slides/slide2.xml", "<p:sld><a:p><a:t>第二页</a:t></a:p></p:sld>")
        archive.writestr("ppt/slides/slide1.xml", "<p:sld><a:p><a:t>封面 &amp; 目录</a:t></a:p></p:sld>")
    return buffer.getvalue()


# ── 抽文字 ────────────────────────────────────────────────────────────────


def test_extract_docx_pptx_and_text() -> None:
    assert ftext.extract_text(_docx("干膜车间清洁 SOP", "第一步：断电"), "SOP.docx") == "干膜车间清洁 SOP\n第一步：断电"
    assert ftext.extract_text(_pptx(), "a.pptx") == "[第1页]\n封面 & 目录\n[第2页]\n第二页"
    assert ftext.extract_text("中文记录".encode("gb18030"), "note.txt") == "中文记录"
    assert ftext.extract_text(b"\x00\x01", "a.bin") is None
    assert ftext.extract_text(b"not a zip", "broken.docx") is None


def test_excerpt_truncates() -> None:
    text = ftext.excerpt("字" * 50, 10)
    assert text.startswith("字" * 10) and "共 50 字" in text


# ── 取文件链路 ────────────────────────────────────────────────────────────


class _ApiBot:
    def __init__(self, replies: dict[str, Any], errors: dict[str, Exception] | None = None) -> None:
        self.replies = replies
        self.errors = errors or {}
        self.calls: list[str] = []

    async def call_api(self, action: str, **params: Any) -> Any:
        self.calls.append(action)
        if action in self.errors:
            raise self.errors[action]
        return self.replies.get(action)


@pytest.mark.asyncio
async def test_missing_private_file_api_falls_back_to_get_file() -> None:
    """老 NapCat 没有 get_private_file_url：报“API not found”不能当成文件过期掐断链路。"""

    import base64

    error = RuntimeError("ActionFailed(status='failed', retcode=1404, message='API not found')")
    bot = _ApiBot({"get_file": {"base64": base64.b64encode(b"docx-bytes").decode()}}, {"get_private_file_url": error})
    attachment = inbox.Attachment(key="m1", media_type="file", source="current", ref={"file_id": "f1", "file": "SOP.docx"}, index=1)
    fetched = await inbox.fetch_file(bot, 0, attachment, max_bytes=1024 * 1024)
    assert fetched.data == b"docx-bytes"
    assert bot.calls == ["get_private_file_url", "get_file"]


@pytest.mark.asyncio
async def test_expired_file_still_reports_expired() -> None:
    bot = _ApiBot({}, {"get_private_file_url": RuntimeError("文件已过期")})
    attachment = inbox.Attachment(key="m1", media_type="file", source="current", ref={"file_id": "f1"}, index=1)
    with pytest.raises(inbox.InboxError) as info:
        await inbox.fetch_file(bot, 0, attachment, max_bytes=1024)
    assert info.value.reason == "expired"


def test_public_host() -> None:
    assert inbox.public_host("183.47.102.2")
    assert inbox.public_host("ftn.example.cn")
    for host in ("10.0.0.5", "127.0.0.1", "192.168.1.2", "localhost", "napcat", "[::1]", "x.internal"):
        assert not inbox.public_host(host), host


@pytest.mark.asyncio
async def test_trusted_download_allows_ip_link(monkeypatch, tmp_path) -> None:
    """QQ 私聊文件链接常是 IP 直连的 ftn_handler，不在域名白名单里。"""

    seen: dict[str, Any] = {}
    target = tmp_path / "x.inbox"
    target.write_bytes(b"data")

    async def fake_stream(url, **kwargs):
        seen["check"] = kwargs["host_check"]
        return target, None

    monkeypatch.setattr(inbox, "stream_download", fake_stream)
    url = "http://183.47.102.2:80/ftn_handler/abc"
    assert await inbox._download(url, 1024, inbox._Attempts()) is None
    fetched = await inbox._download(url, 1024, inbox._Attempts(), trusted=True)
    assert fetched is not None and fetched.size == 4
    assert seen["check"]("183.47.102.2") and not seen["check"]("10.0.0.1")


# ── 私聊入箱不受群聊“允许的附件类型”影响 ──────────────────────────────────


@pytest.mark.asyncio
async def test_private_import_ignores_group_inbox_types(monkeypatch) -> None:
    written: dict[str, Any] = {}
    settings = policy.SandboxSettings(inbox_enabled=False, inbox_types=["image"])

    class Client:
        async def write_file(self, workspace, path, data, **kwargs):
            written[path] = data

    async def ready():
        return True, None

    async def health(**kwargs):
        return {"max_upload_mb": 1024}, None

    async def fetch(bot, group_id, attachment, *, max_bytes):
        return inbox._Fetch(_docx("第一步：断电"), "test")

    monkeypatch.setattr(pcomputer, "load_settings", lambda: settings)
    monkeypatch.setattr(pcomputer, "computer_status", ready)
    monkeypatch.setattr(pcomputer, "cached_health", health)
    monkeypatch.setattr(pcomputer, "SandboxClient", Client)
    monkeypatch.setattr(pcomputer, "private_usage", lambda _uid: {"inbox_bytes": 0, "left": 1})
    monkeypatch.setattr(pcomputer, "record_private_inbox", lambda *_a: None)
    monkeypatch.setattr(inbox, "fetch_file", fetch)
    rows = await pcomputer.import_media(
        None, USER, [{"type": "file", "name": "tool.exe", "file_id": "f2"}, {"type": "file", "name": "SOP.docx", "file_id": "f1"}]
    )
    assert [row.get("ref") for row in rows] == ["sandbox:inbox/tool.exe", "sandbox:inbox/SOP.docx"]
    assert rows[1]["text_excerpt"] == "第一步：断电"
    assert set(written) == {"inbox/tool.exe", "inbox/SOP.docx"}


# ── 电脑用不了：Bot 端直接读文档 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_turn_reads_docx_when_computer_unavailable(maker, monkeypatch) -> None:
    async def offline():
        return False, "sandbox_offline"

    async def fetch(bot, group_id, attachment, *, max_bytes):
        return inbox._Fetch(_docx("干膜车间月度深度清洁", "第一步：断电挂牌"), "test")

    monkeypatch.setattr(pcomputer, "computer_status", offline)
    monkeypatch.setattr(inbox, "fetch_file", fetch)
    monkeypatch.setattr(pagent, "now_beijing", lambda: NOW)
    seen: list[dict] = []
    _script(monkeypatch, [_message("看到了，是清洁 SOP")], seen)
    async with maker() as session:
        chat = ChatSession(user_id=USER, group_id=None, created_at=NOW, updated_at=NOW)
        session.add(chat)
        await session.flush()
        session.add(ChatMessage(session_id=chat.id, role="user", content="[文件: SOP.docx]", created_at=NOW))
        await session.commit()
        request = _request(session, _Bot(), "[文件: SOP.docx]")
        request.media = [{"type": "file", "name": "SOP.docx", "file_id": "f1"}]
        assert await pagent.handle_private_turn(request) == "看到了，是清洁 SOP"
        history = await pagent._recent_history(session, USER)
    profile = seen[0]["messages"][-1]["content"]
    assert "第一步：断电挂牌" in profile and "in_computer" in profile
    assert "第一步：断电挂牌" in history[0]["content"]


@pytest.mark.asyncio
async def test_read_files_reports_unreadable_formats() -> None:
    rows = await ftext.read_files(SimpleNamespace(), [{"type": "file", "name": "a.pdf"}, {"type": "image", "file": "x"}])
    assert rows == [{"id": "m1", "type": "file", "name": "a.pdf", "failed": "这种格式没法直接读文字，需要虚拟电脑"}]
