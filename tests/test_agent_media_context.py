from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nonebot
import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIRST_GROUP_ID = 10001
SECOND_GROUP_ID = 10002


def _load_modules() -> dict[str, Any]:
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

    from src.plugins.yawn_core.data_models.agent_media_asset import AgentMediaAsset
    from src.plugins.yawn_core.data_models.bot_group import BotGroup
    from src.plugins.yawn_core.data_models.group_agent_message import GroupAgentMessage
    from src.plugins.yawn_core.yawn_agent import (
        context_history,
        media,
        media_context,
        prompt,
        tools,
    )

    return {
        "AgentMediaAsset": AgentMediaAsset,
        "BotGroup": BotGroup,
        "GroupAgentMessage": GroupAgentMessage,
        "context_history": context_history,
        "media": media,
        "media_context": media_context,
        "prompt": prompt,
        "tools": tools,
    }


async def _media_db(modules: dict[str, Any]) -> tuple[Any, Any]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(modules["BotGroup"].__table__.create)
        await connection.run_sync(modules["AgentMediaAsset"].__table__.create)
        await connection.run_sync(modules["GroupAgentMessage"].__table__.create)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_same_hash_provider_scope_uploads_once_across_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modules = _load_modules()
    media_module = modules["media"]
    engine, factory = await _media_db(modules)
    uploads: list[str] = []

    class Provider:
        provider_id = "deepseek"
        provider_scope = "deepseek:scope"

        async def upload(self, item: Any, *, expires_after_seconds: int) -> Any:
            del expires_after_seconds
            uploads.append(item.content_hash)
            return item.bind_provider(
                provider=self.provider_id,
                provider_scope=self.provider_scope,
                remote_file_id="file-api-once",
            )

        def build_content_block(self, item: Any) -> dict[str, Any]:
            return {"type": "file", "file_id": item.remote_file_id}

    monkeypatch.setattr(media_module, "_provider_for_task", lambda _task: Provider())
    item = media_module.MediaInput(
        kind="image",
        content_hash="c" * 64,
        mime_type="image/png",
        local_path=None,
        data=b"same-image",
    )

    async with factory() as session:
        session.add_all(
            [
                modules["BotGroup"](group_id=FIRST_GROUP_ID, group_name="a"),
                modules["BotGroup"](group_id=SECOND_GROUP_ID, group_name="b"),
            ]
        )
        await session.commit()
        first, _ = await media_module.build_media_content_blocks(
            [item], task="agent_dialogue", group_id=FIRST_GROUP_ID, session=session
        )
        await session.commit()
        second, _ = await media_module.build_media_content_blocks(
            [item], task="agent_dialogue", group_id=SECOND_GROUP_ID, session=session
        )
        await session.commit()

    assert first == [{"type": "file", "file_id": "file-api-once"}]
    assert second == first
    assert uploads == [item.content_hash]

    remote_ref = [("deepseek", "deepseek:scope", "file-api-once")]
    async with factory() as session:
        assert await modules["media"].media_store.unreferenced_remote_refs(
            session, remote_ref
        ) == []
        await session.execute(
            delete(modules["AgentMediaAsset"]).where(
                modules["AgentMediaAsset"].group_id == FIRST_GROUP_ID
            )
        )
        await session.commit()
        assert await modules["media"].media_store.unreferenced_remote_refs(
            session, remote_ref
        ) == []
        await session.execute(
            delete(modules["AgentMediaAsset"]).where(
                modules["AgentMediaAsset"].group_id == SECOND_GROUP_ID
            )
        )
        await session.commit()
        assert await modules["media"].media_store.unreferenced_remote_refs(
            session, remote_ref
        ) == remote_ref
    await engine.dispose()


@pytest.mark.asyncio
async def test_history_image_is_lazy_and_restored_by_asset_id(tmp_path: Path) -> None:
    modules = _load_modules()
    engine, factory = await _media_db(modules)
    image_path = tmp_path / "history.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nasset")
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    async with factory() as session:
        session.add(modules["BotGroup"](group_id=20001, group_name="history"))
        await session.flush()
        asset = modules["AgentMediaAsset"](
            group_id=20001,
            content_hash="d" * 64,
            media_type="image",
            mime_type="image/png",
            size_bytes=image_path.stat().st_size,
            cache_path=str(image_path),
            provider="local",
            provider_scope="local",
            expires_at=now + timedelta(days=7),
            status="ready",
        )
        session.add(asset)
        await session.flush()
        session.add(
            modules["GroupAgentMessage"](
                bot_id=30001,
                message_id=88,
                group_id=20001,
                user_id=40001,
                sender_name="小明",
                normalized_text="[图片]",
                segments=[],
                reply_chain=[],
                forward_tree=[],
                media_refs=[
                    {
                        "type": "image",
                        "source": "current",
                        "asset_id": asset.id,
                        "content_hash": asset.content_hash,
                    }
                ],
                received_at=now,
                expires_at=now + timedelta(days=7),
            )
        )
        await session.commit()

        bot = SimpleNamespace(self_id=30001)
        hidden = await modules["media_context"].resolve_media_context(
            bot,
            session,
            20001,
            selected_history=[{"message_id": 88, "text": "[图片]"}],
            query_text="今天聊点别的",
        )
        restored = await modules["media_context"].resolve_media_context(
            bot,
            session,
            20001,
            selected_history=[{"message_id": 88, "text": "[图片]"}],
            query_text="刚才那张图还有什么细节？",
        )

    assert hidden == []
    assert len(restored) == 1
    assert restored[0].content_hash == "d" * 64
    assert restored[0].local_path == image_path
    assert restored[0].source == "history"
    await engine.dispose()


@pytest.mark.asyncio
async def test_materialized_ref_reuses_asset_after_original_handle_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modules = _load_modules()
    engine, factory = await _media_db(modules)
    image_path = tmp_path / "received.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nreceived")
    monkeypatch.setattr(modules["media"], "_safe_roots", lambda: (tmp_path.resolve(),))
    calls: list[str] = []

    class Bot:
        async def call_api(self, action: str, **params: Any) -> dict[str, Any]:
            del params
            calls.append(action)
            assert action == "get_image"
            return {"file": str(image_path)}

    async with factory() as session:
        session.add(modules["BotGroup"](group_id=21001, group_name="persist"))
        await session.commit()
        ref = {"type": "image", "source": "current", "file": "qq-original"}
        first, _captions, _digests = await modules["media"].prepare_media_inputs(
            Bot(),
            21001,
            [ref],
            session=session,
            cache_enabled=False,
            asset_ttl_seconds=604800,
        )
        await session.commit()

        assert ref.get("asset_id") is not None
        assert ref.get("content_hash") == first[0].content_hash
        calls.clear()

        class OfflineBot:
            async def call_api(self, action: str, **params: Any) -> dict[str, Any]:
                del action, params
                raise AssertionError

        second, _captions, _digests = await modules["media"].prepare_media_inputs(
            OfflineBot(),
            21001,
            [dict(ref)],
            session=session,
            cache_enabled=False,
        )

    assert calls == []
    assert second[0].content_hash == first[0].content_hash
    assert second[0].local_path is not None
    await engine.dispose()


def test_image_reference_query_keeps_image_only_history_candidate() -> None:
    modules = _load_modules()
    media_message_id = 2
    selection = modules["context_history"].select_context_messages(
        [
            {
                "message_id": 1,
                "user_id": 10,
                "text": "普通聊天",
                "minutes_ago": 3,
            },
            {
                "message_id": media_message_id,
                "user_id": 10,
                "text": "[图片]",
                "media_types": ["image"],
                "minutes_ago": 2,
            },
        ],
        query_text="刚才那张截图报什么错？",
    )

    assert any(item["message_id"] == media_message_id for item in selection.messages)
    trace = next(
        item for item in selection.trace if item["message_id"] == media_message_id
    )
    assert trace["selected"] is True
    assert trace["reason"] == "media_reference"


@pytest.mark.asyncio
async def test_tool_history_media_uses_unified_resolver_and_is_hidden_from_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modules = _load_modules()
    engine, factory = await _media_db(modules)
    image_path = tmp_path / "tool.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\ntool")
    monkeypatch.setattr(modules["media"], "_safe_roots", lambda: (tmp_path.resolve(),))
    calls: list[str] = []

    class Bot:
        self_id = 50001

        async def call_api(self, action: str, **params: Any) -> dict[str, Any]:
            del params
            calls.append(action)
            assert action == "get_image"
            return {"file": str(image_path)}

    raw = {
        "message_id": 123,
        "user_id": 60001,
        "sender": {"nickname": "用户"},
        "message": [
            {"type": "text", "data": {"text": "看这个"}},
            {"type": "image", "data": {"file": "qq-image-id", "url": "https://gchat.qpic.cn/signed"}},
        ],
    }
    compact = modules["tools"]._compact_onebot_message(raw)
    assert compact["media_types"] == ["image"]
    assert "_agent_media_refs" in compact
    public = modules["media_context"].strip_internal_media_metadata(compact)
    assert "_agent_media_refs" not in public
    assert "gchat.qpic.cn" not in str(public)

    async with factory() as session:
        session.add(modules["BotGroup"](group_id=50002, group_name="tool"))
        await session.commit()
        resolved = await modules["media_context"].resolve_media_context(
            Bot(),
            session,
            50002,
            tool_results=[{"ok": True, "result": {"items": [compact]}}],
            query_text="这张图是什么？",
        )
        await session.commit()

    assert len(resolved) == 1
    assert calls == ["get_image"]
    assert resolved[0].content_hash
    assert resolved[0].asset_id is not None
    assert resolved[0].source_message_id == int(raw["message_id"])
    projected = modules["media_context"].project_tool_result_media(
        {"ok": True, "result": {"items": [compact]}},
        resolved,
    )
    projected_item = projected["result"]["items"][0]
    assert projected_item["media"] == [
        {
            "type": "image",
            "available": True,
            "asset_id": resolved[0].asset_id,
        }
    ]
    assert "media_types" not in projected_item
    assert "_agent_media_refs" not in projected_item
    assert "qq-image-id" not in str(projected)
    assert "gchat.qpic.cn" not in str(projected)
    assert "file-api" not in str(projected)
    await engine.dispose()


@pytest.mark.asyncio
async def test_media_projection_puts_historical_file_only_in_user_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modules = _load_modules()
    media_context = modules["media_context"]

    async def fake_resolve(*_args: Any, **_kwargs: Any) -> list[Any]:
        return [
            modules["media"].MediaResolution(
                status="remote_file_ready",
                content_hash="f" * 64,
                source="history",
                block={"type": "file", "file_id": "file-api-history"},
                remote_file_id="file-api-history",
                transport="reused_file",
            )
        ]

    monkeypatch.setattr(media_context, "build_media_resolutions", fake_resolve)
    projection = await media_context.project_context_for_llm(
        [],
        task="agent_dialogue",
        group_id=10001,
    )
    messages, _fingerprint = modules["prompt"].build_messages(
        persona={},
        tools=[],
        context={},
        user_prompt="那个东西是什么？",
        media_inputs=projection.content_blocks,
    )

    media_roles = [
        str(message.get("role"))
        for message in messages
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") in {"file", "image_url"}
    ]
    assert media_roles == ["user"]
    assert all(
        not isinstance(message.get("content"), list)
        for message in messages
        if message.get("role") in {"system", "assistant"}
    )
    assert "历史、回复或工具查询图片" in str(projection.content_blocks[0])


def test_media_diagnostics_redact_transport_secrets() -> None:
    modules = _load_modules()
    public = modules["media_context"].public_media_diagnostics(
        [
            {
                "status": "caption_ready",
                "url": "https://gchat.qpic.cn/signed-secret",
                "load_hint": "C:/private/cache/image.png",
                "_caption": "private caption",
                "content_hash": "abc123",
                "source": "history",
            }
        ]
    )
    assert public == [
        {
            "status": "caption_ready",
            "content_hash": "abc123",
            "source": "history",
        }
    ]


def test_media_diagnostics_merge_materialization_and_provider_stages() -> None:
    modules = _load_modules()
    digest = "d92a73b0cafe" + "0" * 52
    public = modules["media_context"].public_media_diagnostics(
        [
            {
                "index": 0,
                "status": "loaded",
                "source": "tool",
                "source_message_id": 123,
                "asset_id": 456,
                "onebot_file": True,
                "get_image_status": "success",
                "image_read_status": "success",
                "local_cache": "materialized",
                "mime": "image/jpeg",
                "size_bytes": 390144,
                "content_hash": digest[:12],
                "_content_hash": digest,
            },
            {
                "index": 0,
                "status": "remote_file_ready",
                "source": "tool",
                "source_message_id": 123,
                "asset_id": 456,
                "content_hash": digest[:12],
                "_content_hash": digest,
                "provider": "deepseek",
                "model": "vision-model",
                "remote_file_status": "hit",
                "file_id_hint": "file-****9fa2",
                "remote_ttl_seconds": 565200,
                "input_type": "file",
                "delivered_to_model": True,
            },
        ]
    )
    assert len(public) == 1
    item = public[0]
    assert item["materialization_status"] == "loaded"
    assert item["status"] == "remote_file_ready"
    assert item["get_image_status"] == "success"
    assert item["remote_file_status"] == "hit"
    assert item["file_id_hint"] == "file-****9fa2"
    assert item["delivered_to_model"] is True
