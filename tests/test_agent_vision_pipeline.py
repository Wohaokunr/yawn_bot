# ruff: noqa: E501, PLR2004, PLR0913, PLR0917, TRY003
"""识图链路回归：取图字节、过期刷新、失败原因、表情包、路由 image 标记、能力记忆、历史转述。"""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nonebot
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PNG = b"\x89PNG\r\n\x1a\nvision-fixture"


def _load() -> SimpleNamespace:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()
    if nonebot.get_plugin("yawn_core") is None and nonebot.get_plugin("src.plugins.yawn_core") is None:
        nonebot.load_from_toml("pyproject.toml")
    from src.plugins.yawn_core import llm
    from src.plugins.yawn_core.yawn_agent import (
        context_history,
        media,
        media_caption,
        media_context,
        media_fetch,
        media_memory,
        message_parser,
        tool_planner,
        turn_media,
    )

    media_fetch.reset_for_tests()
    media_memory.reset_for_tests()
    turn_media.reset_for_tests()
    return SimpleNamespace(
        llm=llm,
        context_history=context_history,
        media=media,
        media_caption=media_caption,
        media_context=media_context,
        media_fetch=media_fetch,
        media_memory=media_memory,
        message_parser=message_parser,
        tool_planner=tool_planner,
        turn_media=turn_media,
    )


class _Bot:
    """记录 OneBot 调用的假 Bot；``replies`` 按 action 给返回值或异常。"""

    self_id = 50001

    def __init__(self, replies: dict[str, Any]) -> None:
        self.replies = replies
        self.calls: list[str] = []

    async def call_api(self, action: str, **params: Any) -> Any:
        del params
        self.calls.append(action)
        reply = self.replies.get(action)
        if isinstance(reply, Exception):
            raise reply
        if reply is None:
            raise RuntimeError(f"unexpected {action}")
        return reply


# ── 取字节 ────────────────────────────────────────────────────────────────


def test_host_patterns_support_wildcards_and_default_covers_qpic() -> None:
    m = _load()
    fetch = m.media_fetch
    assert fetch.host_allowed("c2cpicdw.qpic.cn", ("*.qpic.cn",))
    assert fetch.host_allowed("qpic.cn", ("*.qpic.cn",))
    assert not fetch.host_allowed("qpic.cn.evil.example", ("*.qpic.cn",))
    assert not fetch.host_allowed("evilqpic.cn", ("*.qpic.cn",))
    assert not fetch.host_allowed("gchat.qpic.cn", ())
    assert "*.qpic.cn" in m.llm.LLMRoutingConfig().agent_media_allowed_hosts


@pytest.mark.asyncio
async def test_segment_base64_image_loads_without_network() -> None:
    m = _load()
    ref = {"type": "image", "file": "base64://" + base64.b64encode(PNG).decode()}
    diagnostic: dict[str, Any] = {}
    loaded = await m.media._load_bytes(None, ref, diagnostic=diagnostic)
    assert loaded is not None
    assert loaded[0] == PNG
    assert diagnostic["read_source"] == "segment_base64"


@pytest.mark.asyncio
async def test_get_image_base64_from_enable_local_file2url_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    m = _load()
    monkeypatch.setattr(m.media, "_allowed_hosts", frozenset)
    bot = _Bot({"get_image": {"file": "/app/.config/QQ/NapCat/temp/x.png", "base64": base64.b64encode(PNG).decode()}})
    diagnostic: dict[str, Any] = {}
    loaded = await m.media._load_bytes(bot, {"type": "image", "file": "abc.png", "url": "https://blocked.example/x"}, diagnostic=diagnostic)
    assert loaded is not None and loaded[0] == PNG
    assert diagnostic["read_source"] == "get_image_base64"
    assert bot.calls == ["get_image"]
    assert m.media_fetch.stats()["via"] == {"get_image_base64": 1}


@pytest.mark.asyncio
async def test_napcat_local_path_only_reports_enable_local_file2url(monkeypatch: pytest.MonkeyPatch) -> None:
    m = _load()
    monkeypatch.setattr(m.media, "_allowed_hosts", frozenset)
    bot = _Bot({
        "get_image": {"file": "/app/.config/QQ/NapCat/temp/x.png"},
        "get_file": {"file": "/app/.config/QQ/NapCat/temp/x.png"},
    })
    diagnostic: dict[str, Any] = {}
    loaded = await m.media._load_bytes(bot, {"type": "image", "file": "abc.png"}, diagnostic=diagnostic)
    assert loaded is None
    assert diagnostic["reason_code"] == "protocol_local_only"
    assert bot.calls == ["get_image", "get_file"]
    message, hint = m.media_fetch.reason_text("protocol_local_only")
    assert "enableLocalFile2Url" in hint
    assert message


@pytest.mark.asyncio
async def test_get_file_is_a_fallback_after_get_image_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    m = _load()
    monkeypatch.setattr(m.media, "_allowed_hosts", frozenset)
    bot = _Bot({"get_image": RuntimeError("boom"), "get_file": {"base64": base64.b64encode(PNG).decode()}})
    diagnostic: dict[str, Any] = {}
    loaded = await m.media._load_bytes(bot, {"type": "image", "file": "abc.png", "file_id": "fid"}, diagnostic=diagnostic)
    assert loaded is not None
    assert diagnostic["read_source"] == "get_file_base64"
    assert diagnostic["get_image_status"] == "failed"


@pytest.mark.asyncio
async def test_onebot_calls_have_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    m = _load()
    monkeypatch.setattr(m.media_fetch, "API_TIMEOUT_SECONDS", 0.05)

    class SlowBot:
        async def call_api(self, action: str, **params: Any) -> Any:
            del action, params
            await asyncio.sleep(5)

    started = asyncio.get_running_loop().time()
    assert await m.media_fetch.call_api(SlowBot(), "get_image", file="x") is None
    assert asyncio.get_running_loop().time() - started < 1


@pytest.mark.asyncio
async def test_expired_history_link_is_refreshed_with_get_msg(monkeypatch: pytest.MonkeyPatch) -> None:
    m = _load()
    fresh_url = "https://multimedia.nt.qq.com.cn/download?fresh=1"
    fetched: list[str] = []

    async def fetch_url(url: str) -> bytes | None:
        fetched.append(url)
        if "fresh" in url:
            return PNG
        m.media_fetch.note("expired")
        return None

    monkeypatch.setattr(m.media, "_fetch_url", fetch_url)
    bot = _Bot({
        "get_image": RuntimeError("已过期"),
        "get_file": RuntimeError("已过期"),
        "get_msg": {
            "message_id": 9,
            "message": [
                {"type": "text", "data": {"text": "看"}},
                {"type": "image", "data": {"file": "new.png", "url": fresh_url}},
            ],
        },
    })
    ref = {"type": "image", "source": "history", "source_message_id": 9, "image_index": 0, "file": "old.png", "url": "https://multimedia.nt.qq.com.cn/download?old=1"}
    diagnostic: dict[str, Any] = {}
    loaded = await m.media._load_bytes(bot, ref, diagnostic=diagnostic)
    assert loaded is not None
    assert diagnostic.get("refreshed") is True
    assert fetched[-1] == fresh_url
    assert "get_msg" in bot.calls


@pytest.mark.asyncio
async def test_https_handshake_failure_retries_over_http(monkeypatch: pytest.MonkeyPatch) -> None:
    m = _load()
    fetch = m.media_fetch
    seen: list[str] = []

    async def once(url: str, max_bytes: int) -> bytes | None:
        del max_bytes
        seen.append(url)
        if url.startswith("https://"):
            fetch.note("tls_failed")
            raise fetch._TLSFailedError
        return PNG

    monkeypatch.setattr(fetch, "_download_once", once)
    monkeypatch.setattr(m.llm.ai_config, "agent_media_allowed_hosts", "multimedia.nt.qq.com.cn")
    data = await fetch.download("https://multimedia.nt.qq.com.cn/download?x=1", 1024)
    assert data == PNG
    assert seen == ["https://multimedia.nt.qq.com.cn/download?x=1", "http://multimedia.nt.qq.com.cn/download?x=1"]

    seen.clear()
    monkeypatch.setattr(m.llm.ai_config, "agent_media_http_fallback", False, raising=False)
    assert await fetch.download("https://multimedia.nt.qq.com.cn/download?x=1", 1024) is None
    assert seen == ["https://multimedia.nt.qq.com.cn/download?x=1"]


@pytest.mark.asyncio
async def test_download_refuses_hosts_outside_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    m = _load()
    monkeypatch.setattr(m.llm.ai_config, "agent_media_allowed_hosts", "gchat.qpic.cn")
    attempts = m.media_fetch.FetchAttempts()
    with m.media_fetch.tracking(attempts):
        assert await m.media_fetch.download("https://evil.example/x.png", 1024) is None
    assert attempts.primary() == "host_blocked"


def test_media_fetch_uses_safe_download_not_follow_redirects() -> None:
    source = (PROJECT_ROOT / "src/plugins/yawn_core/yawn_agent/media.py").read_text(encoding="utf-8")
    assert "follow_redirects=True" not in source
    fetch_source = (PROJECT_ROOT / "src/plugins/yawn_core/yawn_agent/media_fetch.py").read_text(encoding="utf-8")
    assert "stream_download" in fetch_source


@pytest.mark.asyncio
async def test_expired_link_is_not_passed_through_to_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    m = _load()

    async def fetch_url(url: str) -> bytes | None:
        del url
        m.media_fetch.note("expired")
        return None

    monkeypatch.setattr(m.media, "_fetch_url", fetch_url)
    diagnostics: list[dict[str, Any]] = []
    inputs, _captions, _digests = await m.media.prepare_media_inputs(
        None, 1, [{"type": "image", "url": "https://gchat.qpic.cn/expired"}], diagnostics=diagnostics
    )
    assert inputs == []
    assert diagnostics[-1]["status"] == "dropped_unavailable"
    assert diagnostics[-1]["reason"] == "expired"


def test_unavailable_note_explains_reason_without_links() -> None:
    m = _load()
    text = m.media_context._unavailable_explanation("expired")
    assert "重新发" in text
    assert "http" not in text
    assert m.media_context._unavailable_explanation("unknown_code") == ""


# ── 消息解析：表情包 ───────────────────────────────────────────────────────


def test_sticker_and_market_face_become_labelled_image_refs() -> None:
    m = _load()
    normalized = m.message_parser.normalize_message([
        {"type": "image", "data": {"file": "a.png", "url": "https://gchat.qpic.cn/a", "sub_type": 1, "summary": "[狗头]"}},
        {"type": "image", "data": {"file": "b.png", "url": "https://gchat.qpic.cn/b"}},
        {"type": "mface", "data": {"url": "https://gxh.vip.qq.com/x.gif", "summary": "[比心]"}},
    ])
    assert normalized.plain_text == "[表情包: 狗头][图片][表情包: 比心]"
    images = [item for item in normalized.media_refs if item["type"] == "image"]
    assert [item.get("sticker", False) for item in images] == [True, False, True]
    assert [item["image_index"] for item in images] == [0, 1, 2]
    assert "表情包" in normalized.prompt_text()


def test_generic_sticker_summary_is_not_repeated() -> None:
    m = _load()
    normalized = m.message_parser.normalize_message([
        {"type": "image", "data": {"file": "a.png", "sub_type": "1", "summary": "[动画表情]"}},
    ])
    assert normalized.plain_text == "[表情包]"


# ── 路由模型的 image 标记 ──────────────────────────────────────────────────


def test_router_reply_carries_image_flag() -> None:
    m = _load()
    families = tuple(m.tool_planner.TOOLPACK_CATALOG)
    plan = m.tool_planner.parse_router_reply('{"request": true, "toolpacks": [], "segments": [], "image": true}', families=families)
    assert plan is not None and plan.image is True
    assert plan.trace_dict()["image"] is True
    plain = m.tool_planner.parse_router_reply('{"request": true}', families=families)
    assert plain is not None and plain.image is False
    assert '"image"' in m.tool_planner._SYSTEM_PROMPT


def test_no_keyword_regex_gates_image_binding() -> None:
    m = _load()
    source = (PROJECT_ROOT / "src/plugins/yawn_core/yawn_agent/context_history.py").read_text(encoding="utf-8")
    assert "_MEDIA_QUERY_RE" not in source
    assert m.context_history.query_requests_media("刚才那张截图") is False
    assert m.context_history.query_requests_media("随便", media_hint=True) is True


@pytest.mark.asyncio
async def test_router_hint_restores_other_members_recent_image(monkeypatch: pytest.MonkeyPatch) -> None:
    m = _load()
    loaded: list[int] = []

    async def fake_rows(_session: Any, *, group_id: int, bot_id: int | None, message_ids: list[int]) -> dict[int, Any]:
        del group_id, bot_id
        loaded.extend(message_ids)
        return {mid: SimpleNamespace(media_refs=[{"type": "image", "asset_id": mid, "content_hash": str(mid) * 8}]) for mid in message_ids}

    async def fake_prepare(_bot: Any, _group_id: int, refs: list[dict[str, Any]], **_kwargs: Any) -> tuple[list[Any], list[Any], list[Any]]:
        return ([SimpleNamespace(content_hash=ref["content_hash"], asset_id=ref["asset_id"], source="history", source_message_id=ref["source_message_id"]) for ref in refs], [], [])

    monkeypatch.setattr(m.media_context, "_load_message_rows", fake_rows)
    monkeypatch.setattr(m.media_context, "prepare_media_inputs", fake_prepare)
    history = [
        {"message_id": 7, "user_id": 30001, "text": "[图片]", "media_types": ["image"], "minutes_ago": 30},
        {"message_id": 8, "user_id": 30001, "text": "[图片]", "media_types": ["image"], "minutes_ago": 1},
        {"message_id": 9, "user_id": 30002, "text": "哈哈", "minutes_ago": 0},
    ]
    without = await m.media_context.resolve_media_context(SimpleNamespace(self_id=1), None, 1, selected_history=history, query_text="这是啥", actor_user_id=20001)
    assert without == []
    with_hint = await m.media_context.resolve_media_context(SimpleNamespace(self_id=1), None, 1, selected_history=history, query_text="这是啥", actor_user_id=20001, media_hint=True)
    assert [item.source_message_id for item in with_hint] == [8]
    assert loaded == [8]


# ── 多模态能力记忆 ─────────────────────────────────────────────────────────


def test_unsupported_multimodal_is_remembered_for_auto_mode() -> None:
    m = _load()
    request = m.llm.LLMRequestConfig(task="agent_dialogue", profile="default", provider="p", model="text-only", thinking="auto", multimodal="auto")
    assert m.turn_media.effective_multimodal_mode(request) == "auto"
    m.turn_media.remember_multimodal_unsupported(request)
    assert m.turn_media.effective_multimodal_mode(request) == "unsupported"
    explicit = m.llm.LLMRequestConfig(task="agent_dialogue", profile="default", provider="p", model="other", thinking="auto", multimodal="supported")
    m.turn_media.remember_multimodal_unsupported(explicit)
    assert m.turn_media.effective_multimodal_mode(explicit) == "supported"


def test_vision_route_reports_none_without_any_image_model(monkeypatch: pytest.MonkeyPatch) -> None:
    m = _load()
    monkeypatch.setattr(m.turn_media, "vision_model_configured", lambda: False)
    request = m.llm.resolve_llm_request("agent_dialogue")
    m.turn_media.remember_multimodal_unsupported(request)
    route = m.turn_media.vision_route()
    if request.multimodal == "auto":
        assert route["route"] == "none"
        assert route["learned_unsupported"] is True


# ── 历史转述 ───────────────────────────────────────────────────────────────


def test_history_shows_caption_for_seen_image() -> None:
    m = _load()
    row = SimpleNamespace(
        group_id=1,
        segments=[],
        reply_chain=[],
        forward_tree=[],
        media_refs=[{"type": "image", "content_hash": "h" * 64}, {"type": "image", "content_hash": "s" * 64, "sticker": True}],
    )
    assert "image_notes" not in m.context_history.history_message_meta(row)
    m.media_memory.remember(1, "h" * 64, "一只橘猫趴在键盘上")
    m.media_memory.remember(1, "s" * 64, "熊猫头表示无语")
    meta = m.context_history.history_message_meta(row)
    assert meta["image_notes"] == ["图片转述: 一只橘猫趴在键盘上", "表情包转述: 熊猫头表示无语"]
    assert m.context_history._is_low_info({"text": "[图片]", "image_notes": meta["image_notes"]}) is False
    assert m.context_history._is_low_info({"text": "[表情包]"}) is True
    assert m.media_memory.lookup(2, "h" * 64) is None


def test_caption_memory_respects_off_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    m = _load()
    monkeypatch.setattr(m.llm.ai_config, "agent_image_auto_caption", "off", raising=False)
    m.media_memory.remember(1, "h" * 64, "x")
    assert m.media_memory.size() == 0
    assert m.turn_media.schedule_auto_caption(1, []) == 0


@pytest.mark.asyncio
async def test_auto_caption_all_mode_captions_in_background(monkeypatch: pytest.MonkeyPatch) -> None:
    m = _load()
    monkeypatch.setattr(m.llm.ai_config, "agent_image_auto_caption", "all", raising=False)
    monkeypatch.setattr(m.turn_media, "vision_model_configured", lambda: True)

    async def caption(group_id: int, media: Any, *, session: Any = None) -> str:
        del group_id, session
        return f"转述 {media.content_hash[:4]}"

    monkeypatch.setattr(m.turn_media, "caption_media", caption)
    media = m.media.MediaInput(kind="image", content_hash="c" * 64, mime_type="image/png", local_path=None, data=PNG)
    assert m.turn_media.schedule_auto_caption(5, [media]) == 1
    assert m.turn_media.schedule_auto_caption(5, [media]) == 0  # 已在转述中
    for _ in range(20):
        await asyncio.sleep(0)
        if m.media_memory.lookup(5, "c" * 64):
            break
    assert m.media_memory.lookup(5, "c" * 64) == "转述 cccc"
    assert m.turn_media.schedule_auto_caption(5, [media]) == 0  # 已经转述过


def test_auto_caption_hourly_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    m = _load()
    monkeypatch.setattr(m.turn_media, "AUTO_CAPTION_PER_GROUP_HOURLY", 2)
    assert m.turn_media._auto_caption_allowed(9)
    assert m.turn_media._auto_caption_allowed(9)
    assert not m.turn_media._auto_caption_allowed(9)


@pytest.mark.asyncio
async def test_describe_images_runs_in_parallel_and_numbers_multiple_images(monkeypatch: pytest.MonkeyPatch) -> None:
    m = _load()
    monkeypatch.setattr(m.media_caption, "vision_model_configured", lambda: True)
    running = 0
    peak = 0

    async def caption(group_id: int, normalized: Any, media: Any, session: Any, config: Any, diagnostics: Any = None) -> str:
        nonlocal running, peak
        del group_id, normalized, session, config, diagnostics
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return f"图{media.content_hash[0]}"

    monkeypatch.setattr(m.media_caption, "_caption_single_image", caption)
    inputs = [m.media.MediaInput(kind="image", content_hash=ch * 64, mime_type="image/png", local_path=None, data=PNG) for ch in "ab"]
    normalized = m.message_parser.NormalizedMessage(plain_text="这是啥", segments=[])
    text = await m.media_caption.describe_images(1, normalized, inputs, None, SimpleNamespace(media_cache_enabled=False), [], [item.content_hash for item in inputs])
    assert peak == 2
    assert "[第1张图片转述] 图a" in text
    assert "[第2张图片转述] 图b" in text
    assert m.media_memory.lookup(1, "a" * 64) == "图a"


# ── WebUI 诊断 ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vision_status_endpoint_reports_reasons_without_identifiers() -> None:
    m = _load()
    from src.plugins.yawn_core.webui import agent_config_routes

    m.media_fetch.record("failed", reason="protocol_local_only")
    m.media_fetch.record("loaded", via="url")
    payload = (await agent_config_routes.get_agent_vision_status(None))["data"]  # pyright: ignore[reportArgumentType]
    assert payload["stats"]["failed"] == 1
    assert payload["stats"]["loaded"] == 1
    assert payload["reasons"][0]["code"] == "protocol_local_only"
    assert any("enableLocalFile2Url" in item for item in payload["advice"])
    assert payload["route"] in {"direct", "caption", "none"}
    assert "http" not in str(payload["reasons"])
