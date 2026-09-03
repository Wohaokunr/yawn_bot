# ruff: noqa: PLR2004
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nonebot
import pytest
from sqlalchemy.exc import OperationalError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()
if (
    nonebot.get_plugin("yawn_core") is None
    and nonebot.get_plugin("src.plugins.yawn_core") is None
):
    nonebot.load_from_toml("pyproject.toml")

from src.plugins.yawn_core.yawn_agent import (
    capabilities,
    dialogue,
    media,
    outbound,
    proactive,
    reactions,
    tools,
)


class _StoredMessage:
    user_id = 201
    sender_name = "张三"
    normalized_text = "被引用的原消息"


class _Member:
    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self.group_nickname = f"成员{user_id}"


class _ValidationSession:
    def __init__(
        self,
        *,
        known_message: bool = True,
        known_members: set[int] | None = None,
    ) -> None:
        self.known_message = known_message
        self.known_members = known_members or set()

    async def scalar(self, _stmt: Any) -> Any:
        return _StoredMessage() if self.known_message else None

    async def get(self, _model: Any, key: tuple[int, int]) -> object | None:
        user_id = int(key[1])
        return _Member(user_id) if user_id in self.known_members else None


class _Bot:
    self_id = "100"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_api(self, action: str, **params: Any) -> dict[str, int]:
        self.calls.append((action, params))
        return {"message_id": -123}


@pytest.mark.asyncio
async def test_structured_message_renders_reply_at_text_and_face() -> None:
    session = _ValidationSession(known_members={300})
    prepared = await outbound.prepare_outbound_message(
        [
            {"type": "at", "user_id": 300},
            {"type": "text", "text": "看这个"},
            {"type": "reply", "message_id": -55},
            {"type": "face", "id": 14},
        ],
        session=session,
        group_id=1,
        actor_user_id=200,
    )

    assert [segment.type for segment in prepared.message] == [
        "reply",
        "at",
        "text",
        "face",
    ]
    assert prepared.message[0].data["id"] == "-55"
    assert prepared.message[1].data["qq"] == "300"
    assert prepared.message[2].data["text"] == "看这个"
    assert prepared.message[3].data["id"] == "14"
    assert prepared.normalized_text == "看这个"


@pytest.mark.asyncio
async def test_structured_message_allows_at_current_actor() -> None:
    prepared = await outbound.prepare_outbound_message(
        [{"type": "at", "user_id": 200}, {"type": "text", "text": "收到"}],
        session=None,
        group_id=1,
        actor_user_id=200,
    )

    assert [segment.type for segment in prepared.message] == ["at", "text"]


@pytest.mark.asyncio
async def test_structured_message_rejects_unknown_reply() -> None:
    with pytest.raises(ValueError, match="当前群已知的近期消息"):
        await outbound.prepare_outbound_message(
            [{"type": "reply", "message_id": 999}],
            session=_ValidationSession(known_message=False),
            group_id=1,
        )


@pytest.mark.asyncio
async def test_structured_message_rejects_unknown_member_and_at_all() -> None:
    with pytest.raises(ValueError, match="当前群已知成员"):
        await outbound.prepare_outbound_message(
            [{"type": "at", "user_id": 300}],
            session=_ValidationSession(),
            group_id=1,
            actor_user_id=200,
        )

    with pytest.raises(ValueError, match="user_id 必须是整数"):
        await outbound.prepare_outbound_message(
            [{"type": "at", "user_id": "all"}],
            session=_ValidationSession(),
            group_id=1,
            actor_user_id=200,
        )


@pytest.mark.asyncio
async def test_structured_message_rejects_unsafe_shapes() -> None:
    with pytest.raises(ValueError, match="不支持字段"):
        await outbound.prepare_outbound_message(
            [{"type": "text", "text": "hi", "cq": "[CQ:at,qq=all]"}],
            session=None,
            group_id=1,
        )

    with pytest.raises(ValueError, match="最多只能引用一条"):
        await outbound.prepare_outbound_message(
            [
                {"type": "reply", "message_id": 1},
                {"type": "reply", "message_id": 2},
            ],
            session=_ValidationSession(),
            group_id=1,
        )

    with pytest.raises(ValueError, match="最多 3 个媒体段"):
        await outbound.prepare_outbound_message(
            [{"type": "image", "file": f"{index}.png"} for index in range(4)],
            session=None,
            group_id=1,
        )


@pytest.mark.asyncio
async def test_structured_message_supports_local_image_rps_dice_and_poke(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    image = tmp_path / "meme.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    monkeypatch.setenv("AGENT_FILE_ROOT", str(tmp_path))

    prepared = await outbound.prepare_outbound_message(
        [
            {"type": "image", "file": str(image)},
            {"type": "rps"},
            {"type": "dice"},
            {"type": "poke", "poke_type": "126", "poke_id": "2001"},
        ],
        session=None,
        group_id=1,
    )

    assert [segment.type for segment in prepared.message] == [
        "image",
        "rps",
        "dice",
        "poke",
    ]


@pytest.mark.asyncio
async def test_send_prepared_outbound_extracts_negative_id() -> None:
    bot = _Bot()
    prepared = outbound.prepare_text_message("hello")

    result = await outbound.send_prepared_outbound(bot, 42, prepared)

    assert result.sent is True
    assert result.message_id == -123
    assert result.segment_types == ("text",)
    assert bot.calls[0][0] == "send_group_msg"
    assert bot.calls[0][1]["group_id"] == 42


def test_send_message_schema_is_capability_gated_and_strict() -> None:
    with_send = capabilities.BotGroupCapabilities(
        role="member",
        can_manage=False,
        actions=frozenset({"send_group_msg"}),
    )
    without_send = capabilities.BotGroupCapabilities(
        role="member",
        can_manage=False,
        actions=frozenset(),
    )

    schemas = tools.build_tool_schemas(with_send)
    send_schema = next(
        item for item in schemas if item["function"]["name"] == "send_message"
    )
    segment_schema = send_schema["function"]["parameters"]["properties"]["segments"]
    assert segment_schema["maxItems"] == outbound.MAX_OUTBOUND_SEGMENTS
    assert segment_schema["items"]["additionalProperties"] is False
    assert "send_message" not in {
        item["function"]["name"] for item in tools.build_tool_schemas(without_send)
    }


@pytest.mark.asyncio
async def test_execute_send_message_marks_round_as_user_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = capabilities.BotGroupCapabilities(
        role="member",
        can_manage=False,
        actions=frozenset({"send_group_msg"}),
    )
    bot = _Bot()

    async def probe(*_args: object, **_kwargs: object) -> Any:
        return current

    monkeypatch.setattr(tools, "probe_group_capabilities", probe)
    result = await tools.execute_tool(
        "send_message",
        {"segments": [{"type": "text", "text": "结构化发送"}]},
        bot=bot,
        group_id=1,
        actor_user_id=200,
        session=None,
        capabilities=current,
    )

    assert result["ok"] is True
    assert result["sent"] is True
    assert result["result"]["segment_types"] == ["text"]
    assert dialogue._visible_tool_send_ends_turn(result) is True
    assert len(bot.calls) == 1


@pytest.mark.asyncio
async def test_reaction_library_searches_by_tag_and_sends_indexed_image(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "agent_files"
    reaction_dir = root / "reactions"
    reaction_dir.mkdir(parents=True)
    image = reaction_dir / "speechless.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    (reaction_dir / "index.json").write_text(
        json.dumps(
            {
                "reactions": [
                    {
                        "id": "speechless_01",
                        "file": "speechless.png",
                        "tags": ["无语", "沉默"],
                        "description": "无语摊手",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_FILE_ROOT", str(root))

    found = reactions.search_reactions("无语")
    assert found[0]["reaction_id"] == "speechless_01"
    assert "file" not in found[0]

    prepared = await outbound.prepare_outbound_message(
        [{"type": "reaction", "reaction_id": "speechless_01"}],
        session=None,
        group_id=1,
    )
    assert [segment.type for segment in prepared.message] == ["image"]
    assert prepared.media_refs == (
        {"type": "image", "reaction_id": "speechless_01", "source": "reaction"},
    )
    assert "speechless.png" not in repr(prepared.segment_records)


class _CacheSession:
    async def scalar(self, _stmt: Any) -> int:
        return 1


@pytest.mark.asyncio
async def test_received_image_reuse_defaults_to_deny_and_can_be_same_group_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent_root = tmp_path / "agent_files"
    cache_root = agent_root / "media_cache"
    agent_root.mkdir()
    cache_root.mkdir()
    image = cache_root / "received.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    monkeypatch.setenv("AGENT_FILE_ROOT", str(agent_root))
    monkeypatch.setattr(media.ai_config, "agent_media_cache_dir", str(cache_root))
    monkeypatch.delenv("AGENT_RECEIVED_MEDIA_REUSE", raising=False)

    with pytest.raises(PermissionError, match="默认禁止复用"):
        await media.validate_outbound_image_path(
            image, group_id=1, session=_CacheSession()
        )

    monkeypatch.setenv("AGENT_RECEIVED_MEDIA_REUSE", "same_group")
    resolved = await media.validate_outbound_image_path(
        image, group_id=1, session=_CacheSession()
    )
    assert resolved == image.resolve()


@pytest.mark.asyncio
async def test_forward_builder_uses_known_messages_and_resolves_custom_identity(
) -> None:
    session = _ValidationSession(known_message=True, known_members={300})
    prepared = await outbound.prepare_forward_message(
        [
            {"type": "message", "message_id": -55},
            {"type": "custom", "user_id": 300, "content": "补充一条"},
        ],
        session=session,
        group_id=1,
    )

    assert len(prepared.nodes) == 2
    assert prepared.nodes[0].data["id"] == "-55"
    assert prepared.nodes[1].data["user_id"] == "300"
    assert prepared.nodes[1].data["nickname"] == "成员300"
    assert prepared.forward_tree[0]["source"] == "reference"
    assert prepared.forward_tree[1]["source"] == "custom"

    with pytest.raises(ValueError, match="不支持字段"):
        await outbound.prepare_forward_message(
            [
                {
                    "type": "custom",
                    "user_id": 300,
                    "nickname": "伪造昵称",
                    "content": "不能自己填身份",
                }
            ],
            session=session,
            group_id=1,
        )


@pytest.mark.asyncio
async def test_send_forward_returns_unified_send_result() -> None:
    session = _ValidationSession(known_message=True)
    bot = _Bot()
    result = await outbound.send_forward_message(
        bot,
        42,
        [{"type": "message", "message_id": -55}],
        session=session,
    )

    assert result.sent is True
    assert result.message_id == -123
    assert result.segment_types == ("forward",)
    assert result.forward_tree[0]["message_id"] == -55
    assert bot.calls[0][0] == "send_group_forward_msg"


def test_bot_message_meta_exposes_structure_without_paths() -> None:
    row = SimpleNamespace(
        role="bot",
        segments=[
            {"type": "reply", "data": {"id": "-55"}},
            {"type": "at", "data": {"qq": "300"}},
            {"type": "image", "data": {"file": "[redacted]"}},
        ],
        reply_chain=[{"message_id": -55, "text": "原消息"}],
        media_refs=[
            {
                "type": "image",
                "reaction_id": "speechless_01",
                "source": "reaction",
                "file": "C:/private/never-expose.png",
            }
        ],
        forward_tree=[{"source": "reference"}, {"source": "custom"}],
    )

    meta = dialogue._bot_message_meta(row)  # pyright: ignore[reportArgumentType]
    assert meta == {
        "segment_types": ["reply", "at", "image"],
        "mentions": [300],
        "reply_to": [-55],
        "media": [{"type": "image", "reaction_id": "speechless_01"}],
        "forward_nodes": 2,
    }
    assert "private" not in repr(meta)


class _UnsupportedReplyBot(_Bot):
    async def call_api(self, action: str, **params: Any) -> dict[str, int]:
        self.calls.append((action, params))
        if len(self.calls) == 1:
            message = "unsupported reply message segment"
            raise RuntimeError(message)
        return {"message_id": -321}


@pytest.mark.asyncio
async def test_backend_unsupported_segment_degrades_to_at_text_and_is_cached() -> None:
    capabilities.reset_capability_cache()
    bot = _UnsupportedReplyBot()
    audit_session = _AuditSession()
    prepared = await outbound.prepare_outbound_message(
        [
            {"type": "reply", "message_id": -55},
            {"type": "face", "id": 14},
            {"type": "text", "text": "看这个"},
        ],
        session=_ValidationSession(known_message=True),
        group_id=1,
    )

    result = await outbound.send_prepared_outbound(
        bot, 1, prepared, session=audit_session, source="test"
    )

    assert result.sent is True
    assert result.outcome == "degraded_to_text"
    assert result.degraded_from == "reply+face+text"
    assert result.segment_types == ("at", "text")
    assert result.message_id == -321
    assert [segment.type for segment in bot.calls[0][1]["message"]] == [
        "reply",
        "face",
        "text",
    ]
    assert [segment.type for segment in bot.calls[1][1]["message"]] == ["at", "text"]
    assert bot.calls[1][1]["message"][0].data["qq"] == "201"

    before = len(bot.calls)
    again = await outbound.send_prepared_outbound(
        bot, 1, prepared, session=audit_session, source="test"
    )
    assert again.outcome == "degraded_to_text"
    assert len(bot.calls) == before + 1
    assert [segment.type for segment in bot.calls[-1][1]["message"]] == ["at", "text"]
    outcomes = [row.result for row in audit_session.added]
    assert outcomes.count("unsupported_segment") == 1
    assert outcomes.count("degraded_to_text") == 2
    capabilities.reset_capability_cache()


def test_segment_capability_matrix_keeps_risky_types_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENT_OPTIONAL_MESSAGE_SEGMENTS", raising=False)
    bot = SimpleNamespace(self_id="100")
    default_caps = capabilities.get_segment_capabilities(bot, 1)
    assert {"text", "reply", "at", "face", "image"} <= default_caps.exposed_types
    assert {"share", "contact", "location", "music"}.isdisjoint(
        default_caps.exposed_types
    )

    monkeypatch.setenv("AGENT_OPTIONAL_MESSAGE_SEGMENTS", "share,music")
    enabled_caps = capabilities.get_segment_capabilities(bot, 1)
    action_caps = capabilities.BotGroupCapabilities(
        role="member",
        can_manage=False,
        actions=frozenset({"send_group_msg"}),
    )
    schema = next(
        item
        for item in tools.build_tool_schemas(
            action_caps, segment_capabilities=enabled_caps
        )
        if item["function"]["name"] == "send_message"
    )
    exposed = set(
        schema["function"]["parameters"]["properties"]["segments"]["items"]
        ["properties"]["type"]["enum"]
    )
    assert {"share", "music"} <= exposed
    forbidden = {
        "contact",
        "location",
        "xml",
        "json",
        "anonymous",
        "at_all",
        "raw_cq",
    }
    assert forbidden.isdisjoint(exposed)


@pytest.mark.asyncio
async def test_forward_rejects_nodes_over_limit() -> None:
    nodes = [
        {"type": "message", "message_id": index + 1}
        for index in range(outbound.MAX_FORWARD_NODES + 1)
    ]
    with pytest.raises(ValueError, match="合并转发最多"):
        await outbound.prepare_forward_message(nodes, session=None, group_id=1)


class _FakeMediaResponse:
    def __init__(self) -> None:
        self.headers = {"content-type": "image/png"}

    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self, _size: int):
        yield b"\x89PNG\r\n\x1a\nfixture"


class _FakeMediaClient:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def stream(self, *_args: object, **_kwargs: object) -> _FakeMediaResponse:
        return _FakeMediaResponse()


@pytest.mark.asyncio
async def test_remote_image_requires_whitelisted_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_FILE_ALLOWED_HOSTS", "cdn.example")
    monkeypatch.setattr(outbound.httpx, "AsyncClient", _FakeMediaClient)

    with pytest.raises(PermissionError, match="域名不在白名单"):
        await outbound._download_allowed_media(
            "https://evil.example/meme.png", "image"
        )

    path = await outbound._download_allowed_media(
        "https://cdn.example/meme.png", "image"
    )
    try:
        assert path.is_file()
        assert path.read_bytes().startswith(b"\x89PNG")
    finally:
        path.unlink(missing_ok=True)


class _PersistSession:
    def __init__(self) -> None:
        self.added: list[Any] = []

    async def scalar(self, _stmt: Any) -> None:
        return None

    def add(self, row: Any) -> None:
        self.added.append(row)


class _PostSendFailureSession:
    def __init__(self) -> None:
        self.commit_calls = 0
        self.rollback_calls = 0

    async def commit(self) -> None:
        self.commit_calls += 1

    async def rollback(self) -> None:
        self.rollback_calls += 1


@pytest.mark.asyncio
async def test_confirmed_send_is_not_reclassified_when_post_send_db_state_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _send_success(*_args: Any, **_kwargs: Any) -> outbound.SendResult:
        return outbound.SendResult(
            sent=True,
            message_id=1469749875,
            normalized_text="正常发送",
            segment_types=("text",),
            segments=({"type": "text", "data": {"text": "正常发送"}},),
        )

    async def _persist_failure(*_args: Any, **_kwargs: Any) -> None:
        raise OperationalError(  # noqa: TRY003
            "select bot reply", {}, RuntimeError("database locked")
        )

    marked: list[dict[str, Any]] = []

    def _mark_reply(*_args: Any, **kwargs: Any) -> None:
        marked.append(kwargs)

    monkeypatch.setattr(dialogue, "_send_unless_expired", _send_success)
    monkeypatch.setattr(dialogue, "persist_bot_reply", _persist_failure)
    monkeypatch.setattr(dialogue, "mark_bot_reply", _mark_reply)

    session = _PostSendFailureSession()
    config = SimpleNamespace(
        short_conversation_enabled=True,
        persona_enabled=False,
        raw_retention_days=7,
        recent_response_fingerprints=[],
        last_response_fingerprint=None,
        last_response_input_fingerprint=None,
        last_response_at=None,
        last_agent_at=None,
        active_topic=None,
        context_epoch=0,
    )
    normalized = dialogue.NormalizedMessage(plain_text="看看这个", segments=[])
    bot = SimpleNamespace(self_id="100")
    trace = dialogue.begin_execution_trace(
        1,
        mode="dialogue",
        source="runtime",
        trigger_source="mention",
    )
    token = dialogue.bind_execution_trace(trace)
    try:
        await dialogue._finalize_reply(
            bot,
            1,
            config,
            session,
            normalized,
            "正常发送",
            "看看这个",
            None,
            123,
        )
        dialogue.finish_execution_trace(trace, outcome="completed", store=False)
    finally:
        dialogue.reset_execution_trace(token)

    assert trace.status == "completed"
    assert trace.outcome == "completed"
    assert session.commit_calls == 0
    assert session.rollback_calls == 1
    assert len(marked) == 1
    state_events = [event for event in trace.events if event.phase == "state"]
    assert len(state_events) == 1
    assert state_events[0].label == "回复后状态提交"
    assert state_events[0].status == "degraded"
    assert state_events[0].output["delivery_state"] == "confirmed_success"
    assert state_events[0].output["error_type"] == "OperationalError"


@pytest.mark.asyncio
async def test_bot_compound_message_persists_full_structure() -> None:
    session = _PersistSession()
    segments = [
        {"type": "reply", "data": {"id": "-55"}},
        {"type": "at", "data": {"qq": "300"}},
        {"type": "text", "data": {"text": "收到"}, "text": "收到"},
    ]
    reply_chain = [
        {"message_id": -55, "user_id": 201, "nickname": "张三", "text": "原话"}
    ]

    await dialogue.persist_bot_reply(
        session,
        100,
        1,
        -999,
        "收到",
        7,
        segments=segments,
        reply_chain=reply_chain,
    )

    assert len(session.added) == 1
    row = session.added[0]
    assert row.role == "bot"
    assert row.message_id == -999
    assert row.segments == segments
    assert row.reply_chain == reply_chain


@pytest.mark.asyncio
async def test_proactive_message_can_quote_recent_message() -> None:
    decision = proactive._ProactiveDecision(
        action="speak",
        text="接着说",
        topic="当前话题",
        reason="自然接话",
        segments=(
            {"type": "reply", "message_id": -55},
            {"type": "text", "text": "接着说"},
        ),
    )
    prepared = await proactive._prepare_proactive_message(
        decision,
        session=_ValidationSession(known_message=True),
        group_id=1,
    )

    assert [segment.type for segment in prepared.message] == ["reply", "text"]
    assert prepared.reply_chain[0]["message_id"] == -55


@pytest.mark.asyncio
async def test_short_conversation_structured_reply_prepares_compound_message() -> None:
    decision = proactive._decide_proactive_reply(
        '{"action":"speak","speak":true,"topic":"续聊","reason":"继续回应",'
        '"message":{"segments":['
        '{"type":"reply","message_id":-55},'
        '{"type":"at","user_id":300},'
        '{"type":"text","text":"我接着说"},'
        '{"type":"face","id":14}]}}'
    )
    prepared = await proactive._prepare_proactive_message(
        decision,
        session=_ValidationSession(known_message=True, known_members={300}),
        group_id=1,
    )

    assert [segment.type for segment in prepared.message] == [
        "reply",
        "at",
        "text",
        "face",
    ]
    assert decision.should_speak is True


class _AuditSession:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


@pytest.mark.asyncio
async def test_outbound_audit_redacts_share_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_OPTIONAL_MESSAGE_SEGMENTS", "share")
    monkeypatch.setenv("AGENT_SHARE_ALLOWED_HOSTS", "safe.example")
    session = _AuditSession()
    bot = _Bot()

    result = await outbound.send_outbound_message(
        bot,
        1,
        [
            {
                "type": "share",
                "url": "https://safe.example/private?token=secret",
                "title": "安全链接",
            }
        ],
        session=session,
        source="test",
    )

    assert result.message_type == "share"
    assert result.outcome == "success"
    assert session.added
    audit = session.added[-1]
    assert audit.result == "success"
    assert "safe.example" not in repr(audit.arguments)
    assert "secret" not in repr(audit.arguments)


# ── 慢回合等待提示 ────────────────────────────────────────────────


def _wait_notice_sends(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """捕获等待提示实际走到 sender 的调用（含 session/来源等副作用参数）。"""

    sends: list[dict[str, Any]] = []

    async def _capture(
        _bot: Any,
        group_id: int,
        prepared: Any,
        *,
        session: Any = None,
        actor_user_id: int | None = None,
        source: str = "agent",
    ) -> outbound.SendResult:
        sends.append(
            {
                "group_id": group_id,
                "text": prepared.normalized_text,
                "session": session,
                "actor_user_id": actor_user_id,
                "source": source,
            }
        )
        return outbound.SendResult(
            sent=True,
            message_id=1,
            normalized_text=prepared.normalized_text,
            segment_types=("text",),
        )

    monkeypatch.setattr(dialogue, "send_prepared_outbound", _capture)
    return sends


@pytest.mark.asyncio
async def test_wait_notice_is_not_sent_when_turn_finishes_before_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """快回合不该出现等待提示。"""

    sends = _wait_notice_sends(monkeypatch)
    monkeypatch.setenv("AGENT_AI_WAIT_NOTICE_DELAY", "0.2")
    bot = SimpleNamespace(self_id="100")

    dialogue._start_wait_notice(bot, 1, None, 123)
    await asyncio.sleep(0.02)
    dialogue._cancel_wait_notice()
    await asyncio.sleep(0.25)

    assert sends == []


@pytest.mark.asyncio
async def test_wait_notice_fires_once_on_slow_turn_without_touching_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """慢回合只提示一次；不写消息历史、不带 session（因此不落审计行）。"""

    sends = _wait_notice_sends(monkeypatch)
    monkeypatch.setenv("AGENT_AI_WAIT_NOTICE_DELAY", "0.05")
    persisted: list[Any] = []

    async def _persist(*args: Any, **kwargs: Any) -> None:
        persisted.append((args, kwargs))

    monkeypatch.setattr(dialogue, "persist_bot_reply", _persist)
    bot = SimpleNamespace(self_id="100")

    dialogue._start_wait_notice(bot, 1, None, 123)
    await asyncio.sleep(0.25)

    assert len(sends) == 1
    assert sends[0]["text"] == dialogue._WAIT_NOTICE
    assert sends[0]["session"] is None
    assert persisted == []
    # 提示发过之后不再重复。
    await asyncio.sleep(0.1)
    assert len(sends) == 1


@pytest.mark.asyncio
async def test_wait_notice_is_skipped_when_trigger_already_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """触发在排队期间过期时，等待提示不能发出去。"""

    sends = _wait_notice_sends(monkeypatch)
    monkeypatch.setenv("AGENT_AI_WAIT_NOTICE_DELAY", "0.05")
    monkeypatch.setattr(dialogue, "is_pending_trigger_expired", lambda _at: True)
    bot = SimpleNamespace(self_id="100")

    dialogue._start_wait_notice(bot, 1, 1.0, 123)
    await asyncio.sleep(0.2)

    assert sends == []


@pytest.mark.asyncio
async def test_visible_send_cancels_pending_wait_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正文发送会顺带取消未发出的等待提示，避免提示压在正文后面。"""

    sends = _wait_notice_sends(monkeypatch)
    monkeypatch.setenv("AGENT_AI_WAIT_NOTICE_DELAY", "0.15")
    bot = SimpleNamespace(self_id="100")

    dialogue._start_wait_notice(bot, 1, None, 123)
    await asyncio.sleep(0.02)
    await dialogue._send_unless_expired(bot, 1, "正文", None, label="正文发送")
    await asyncio.sleep(0.25)

    assert [item["text"] for item in sends] == ["正文"]


@pytest.mark.asyncio
async def test_wait_notice_disabled_by_non_positive_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """阈值 <=0 表示关闭该提示。"""

    sends = _wait_notice_sends(monkeypatch)
    monkeypatch.setenv("AGENT_AI_WAIT_NOTICE_DELAY", "0")
    bot = SimpleNamespace(self_id="100")

    dialogue._start_wait_notice(bot, 1, None, 123)
    await asyncio.sleep(0.1)

    assert sends == []
    assert dialogue._WAIT_NOTICE_TASK.get() is None


def test_wait_notice_delay_falls_back_on_invalid_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_AI_WAIT_NOTICE_DELAY", "abc")
    assert dialogue._wait_notice_delay() == dialogue._WAIT_NOTICE_DEFAULT_DELAY
    monkeypatch.delenv("AGENT_AI_WAIT_NOTICE_DELAY", raising=False)
    assert dialogue._wait_notice_delay() == dialogue._WAIT_NOTICE_DEFAULT_DELAY
    monkeypatch.setenv("AGENT_AI_WAIT_NOTICE_DELAY", "2.5")
    assert dialogue._wait_notice_delay() == 2.5
