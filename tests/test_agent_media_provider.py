from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nonebot
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_modules() -> tuple[Any, Any, Any, Any]:
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
    from src.plugins.yawn_core.yawn_agent import media_provider, media_store

    return media_provider, media_store, AgentMediaAsset, BotGroup


@pytest.mark.asyncio
async def test_deepseek_file_provider_upload_builds_file_block() -> None:
    media_provider, _media_store, _asset_model, _group_model = _load_modules()
    uploads: list[dict[str, object]] = []

    class Files:
        async def create(self, **kwargs: object) -> object:
            uploads.append(dict(kwargs))
            return SimpleNamespace(
                id="file-api-image-1",
                created_at=1_788_000_000,
                expires_at=1_788_086_400,
            )

        async def retrieve(self, file_id: str) -> object:
            return SimpleNamespace(id=file_id, bytes=5)

        async def delete(self, file_id: str) -> object:
            return SimpleNamespace(id=file_id, deleted=True)

    provider = media_provider.DeepSeekFileProvider(
        "deepseek",
        "deepseek:key-fingerprint",
        SimpleNamespace(files=Files()),
    )
    media = media_provider.MediaInput(
        kind="image",
        content_hash="a" * 64,
        mime_type="image/png",
        local_path=None,
        data=b"image",
    )

    uploaded = await provider.upload(media, expires_after_seconds=86_400)

    assert uploaded.remote_file_id == "file-api-image-1"
    assert uploaded.provider_scope == "deepseek:key-fingerprint"
    assert provider.build_content_block(uploaded) == {
        "type": "file",
        "file_id": "file-api-image-1",
    }
    assert uploads[0]["purpose"] == "user_data"
    assert uploads[0]["expires_after"] == {
        "anchor": "created_at",
        "seconds": 86_400,
    }


def test_provider_scope_changes_after_api_key_rotation() -> None:
    media_provider, _media_store, _asset_model, _group_model = _load_modules()

    first = media_provider.provider_scope("deepseek", "secret-a")
    second = media_provider.provider_scope("deepseek", "secret-b")

    assert first.startswith("deepseek:")
    assert second.startswith("deepseek:")
    assert first != second
    assert "secret-a" not in first
    assert "secret-b" not in second


@pytest.mark.asyncio
async def test_media_store_reuses_remote_file_only_in_same_provider_scope() -> None:
    media_provider, media_store, asset_model, group_model = _load_modules()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(group_model.__table__.create)
        await connection.run_sync(asset_model.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    media = media_provider.MediaInput(
        kind="image",
        content_hash="b" * 64,
        mime_type="image/png",
        local_path=None,
        data=b"image",
    ).bind_provider(
        provider="deepseek",
        provider_scope="deepseek:scope-a",
        remote_file_id="file-api-reusable",
    )

    async with factory() as session:
        session.add(group_model(group_id=10001, group_name="test"))
        await session.flush()
        await media_store.save_remote_media(
            session,
            group_id=10001,
            media=media,
            size_bytes=5,
            ttl_seconds=86_400,
        )
        await session.commit()

    async with factory() as session:
        reused = await media_store.reusable_remote_media(
            session,
            group_id=10001,
            media=media,
            provider="deepseek",
            provider_scope="deepseek:scope-a",
        )
        rotated = await media_store.reusable_remote_media(
            session,
            group_id=10001,
            media=media,
            provider="deepseek",
            provider_scope="deepseek:scope-b",
        )

    assert reused is not None
    assert reused.remote_file_id == "file-api-reusable"
    assert rotated is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_files_api_failure_falls_back_to_file_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media_provider, _media_store, _asset_model, _group_model = _load_modules()
    from src.plugins.yawn_core.yawn_agent import media as media_module

    class Files:
        async def create(self, **_kwargs: object) -> object:
            raise RuntimeError

    provider = media_provider.DeepSeekFileProvider(
        "deepseek",
        "deepseek:test-scope",
        SimpleNamespace(files=Files()),
    )
    monkeypatch.setattr(media_module, "_provider_for_task", lambda _task: provider)
    item = media_provider.MediaInput(
        kind="image",
        content_hash="c" * 64,
        mime_type="image/png",
        local_path=None,
        data=b"small-image",
    )

    resolutions = await media_module.build_media_resolutions(
        [item], task="agent_dialogue", group_id=10001
    )

    assert len(resolutions) == 1
    assert resolutions[0].status == "inline_file_ready"
    assert resolutions[0].transport == "file_data"
    assert resolutions[0].reason == "files_api_upload_failed:RuntimeError"
    assert resolutions[0].block is not None
    assert resolutions[0].block["type"] == "file"
    assert str(resolutions[0].block["file_data"]).startswith("data:image/png;base64,")
    assert "file_id" not in resolutions[0].block


@pytest.mark.asyncio
async def test_large_files_api_failure_falls_back_to_trusted_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media_provider, _media_store, _asset_model, _group_model = _load_modules()
    from src.plugins.yawn_core.yawn_agent import media as media_module

    class Files:
        async def create(self, **_kwargs: object) -> object:
            raise RuntimeError

    provider = media_provider.DeepSeekFileProvider(
        "deepseek",
        "deepseek:test-scope",
        SimpleNamespace(files=Files()),
    )
    monkeypatch.setattr(media_module, "_provider_for_task", lambda _task: provider)
    large = tmp_path / "large.png"
    with large.open("wb") as handle:
        handle.seek(media_module.INLINE_MEDIA_MAX_BYTES)
        handle.write(b"x")
    item = media_provider.MediaInput(
        kind="image",
        content_hash="d" * 64,
        mime_type="image/png",
        local_path=large,
        data=None,
        source_url="https://gchat.qpic.cn/trusted-image",
    )

    resolutions = await media_module.build_media_resolutions(
        [item], task="agent_dialogue", group_id=10001
    )

    assert resolutions[0].status == "image_url_ready"
    assert resolutions[0].transport == "trusted_url"
    assert resolutions[0].block == {
        "type": "image_url",
        "image_url": {"url": "https://gchat.qpic.cn/trusted-image"},
    }


@pytest.mark.asyncio
async def test_transport_failure_uses_caption_then_explicit_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media_provider, _media_store, _asset_model, _group_model = _load_modules()
    from src.plugins.yawn_core.yawn_agent import media as media_module

    provider = media_provider.DeepSeekFileProvider(
        "deepseek",
        "deepseek:test-scope",
        SimpleNamespace(files=SimpleNamespace()),
    )
    monkeypatch.setattr(media_module, "_provider_for_task", lambda _task: provider)
    with_caption = media_provider.MediaInput(
        kind="image",
        content_hash="e" * 64,
        mime_type="image/png",
        local_path=None,
        data=None,
        source="history",
    )
    missing = media_provider.MediaInput(
        kind="image",
        content_hash="f" * 64,
        mime_type="image/png",
        local_path=None,
        data=None,
        source="history",
    )

    resolutions = await media_module.build_media_resolutions(
        [with_caption, missing],
        task="agent_dialogue",
        group_id=10001,
        cached_captions={with_caption.content_hash: "缓存里看到一台机器"},
    )

    assert resolutions[0].status == "caption_ready"
    assert resolutions[0].caption == "缓存里看到一台机器"
    assert resolutions[0].block is None
    assert resolutions[1].status == "unavailable"
    assert resolutions[1].block is None
    assert resolutions[1].reason == "image_bytes_and_trusted_url_unavailable"


@pytest.mark.asyncio
async def test_cleanup_finalizes_only_after_remote_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _media_provider, _media_store, asset_model, group_model = _load_modules()
    from src.plugins.yawn_core.data_models.agent_media_cache import AgentMediaCache
    from src.plugins.yawn_core.yawn_agent import media as media_module

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(group_model.__table__.create)
        await connection.run_sync(asset_model.__table__.create)
        await connection.run_sync(AgentMediaCache.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cache_path = tmp_path / "expired.png"
    cache_path.write_bytes(b"cached")
    ref = ("deepseek", "deepseek:test-scope", "file-api-expired")
    unlinked: list[str] = []

    async def remote_delete(refs: list[tuple[str, str, str]]) -> Any:
        assert refs == [ref]
        return media_module.RemoteDeleteReport(cleaned=(ref,), failed=())

    monkeypatch.setattr(
        media_module,
        "delete_remote_media_files_detailed",
        remote_delete,
    )
    monkeypatch.setattr(
        media_module,
        "unlink_cache_file",
        unlinked.append,
    )

    async with factory() as session:
        session.add(group_model(group_id=12001, group_name="cleanup"))
        await session.flush()
        asset = asset_model(
            group_id=12001,
            content_hash="1" * 64,
            media_type="image",
            mime_type="image/png",
            size_bytes=6,
            cache_path=str(cache_path),
            provider=ref[0],
            provider_scope=ref[1],
            remote_file_id=ref[2],
            remote_expires_at=now + timedelta(days=1),
            expires_at=now - timedelta(seconds=1),
            status="uploaded",
        )
        session.add(asset)
        await session.commit()
        asset_id = int(asset.id)

        removed = await media_module.cleanup_media_cache(session, now=now)
        row = await session.scalar(
            select(asset_model).where(asset_model.id == asset_id)
        )

    assert removed == 1
    assert row is not None
    assert row.status == "expired"
    assert row.remote_file_id is None
    assert row.cache_path is None
    assert unlinked == [str(cache_path)]
    await engine.dispose()


@pytest.mark.asyncio
async def test_cleanup_remote_failure_keeps_retry_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _media_provider, _media_store, asset_model, group_model = _load_modules()
    from src.plugins.yawn_core.data_models.agent_media_cache import AgentMediaCache
    from src.plugins.yawn_core.yawn_agent import media as media_module

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(group_model.__table__.create)
        await connection.run_sync(asset_model.__table__.create)
        await connection.run_sync(AgentMediaCache.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cache_path = tmp_path / "pending.png"
    cache_path.write_bytes(b"cached")
    ref = ("deepseek", "deepseek:test-scope", "file-api-pending")
    unlinked: list[str] = []

    async def remote_delete(_refs: list[tuple[str, str, str]]) -> Any:
        return media_module.RemoteDeleteReport(cleaned=(), failed=(ref,))

    monkeypatch.setattr(
        media_module,
        "delete_remote_media_files_detailed",
        remote_delete,
    )
    monkeypatch.setattr(
        media_module,
        "unlink_cache_file",
        unlinked.append,
    )

    async with factory() as session:
        session.add(group_model(group_id=12002, group_name="cleanup-pending"))
        await session.flush()
        asset = asset_model(
            group_id=12002,
            content_hash="2" * 64,
            media_type="image",
            mime_type="image/png",
            size_bytes=6,
            cache_path=str(cache_path),
            provider=ref[0],
            provider_scope=ref[1],
            remote_file_id=ref[2],
            remote_expires_at=now + timedelta(days=1),
            expires_at=now - timedelta(seconds=1),
            status="uploaded",
        )
        session.add(asset)
        await session.commit()
        asset_id = int(asset.id)

        removed = await media_module.cleanup_media_cache(session, now=now)
        row = await session.scalar(
            select(asset_model).where(asset_model.id == asset_id)
        )

    assert removed == 0
    assert row is not None
    assert row.status == "cleanup_pending"
    assert row.remote_file_id == ref[2]
    assert row.cache_path == str(cache_path)
    assert unlinked == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_remote_delete_404_is_treated_as_cleaned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _media_provider, _media_store, _asset_model, _group_model = _load_modules()
    from src.plugins.yawn_core.yawn_agent import media as media_module

    class MissingFileError(Exception):
        status_code = 404

    class Files:
        async def delete(self, _file_id: str) -> object:
            raise MissingFileError

    ref = ("deepseek", "deepseek:test-scope", "file-api-gone")
    monkeypatch.setattr(
        media_module,
        "resolve_provider",
        lambda _provider: ("https://api.deepseek.com", "secret"),
    )
    monkeypatch.setattr(
        media_module,
        "provider_scope",
        lambda _provider, _key: "deepseek:test-scope",
    )
    monkeypatch.setattr(
        media_module,
        "get_client",
        lambda _provider: SimpleNamespace(files=Files()),
    )

    report = await media_module.delete_remote_media_files_detailed([ref])

    assert report.cleaned == (ref,)
    assert report.failed == ()


@pytest.mark.asyncio
async def test_cleanup_keeps_shared_cache_path_while_remote_asset_is_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _media_provider, _media_store, asset_model, group_model = _load_modules()
    from src.plugins.yawn_core.data_models.agent_media_cache import AgentMediaCache
    from src.plugins.yawn_core.yawn_agent import media as media_module

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(group_model.__table__.create)
        await connection.run_sync(asset_model.__table__.create)
        await connection.run_sync(AgentMediaCache.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cache_path = tmp_path / "shared.png"
    cache_path.write_bytes(b"shared")
    remote_ref = ("deepseek", "deepseek:test-scope", "file-api-shared")
    unlinked: list[str] = []

    async def remote_delete(_refs: list[tuple[str, str, str]]) -> Any:
        return media_module.RemoteDeleteReport(cleaned=(), failed=(remote_ref,))

    monkeypatch.setattr(
        media_module,
        "delete_remote_media_files_detailed",
        remote_delete,
    )
    monkeypatch.setattr(
        media_module,
        "unlink_cache_file",
        unlinked.append,
    )

    async with factory() as session:
        session.add(group_model(group_id=12003, group_name="shared-cache"))
        await session.flush()
        local_asset = asset_model(
            group_id=12003,
            content_hash="3" * 64,
            media_type="image",
            mime_type="image/png",
            size_bytes=6,
            cache_path=str(cache_path),
            provider="local",
            provider_scope="local",
            expires_at=now - timedelta(seconds=1),
            status="ready",
        )
        remote_asset = asset_model(
            group_id=12003,
            content_hash="3" * 64,
            media_type="image",
            mime_type="image/png",
            size_bytes=6,
            cache_path=str(cache_path),
            provider=remote_ref[0],
            provider_scope=remote_ref[1],
            remote_file_id=remote_ref[2],
            remote_expires_at=now + timedelta(days=1),
            expires_at=now - timedelta(seconds=1),
            status="uploaded",
        )
        session.add_all([local_asset, remote_asset])
        await session.commit()
        local_id = int(local_asset.id)
        remote_id = int(remote_asset.id)

        removed = await media_module.cleanup_media_cache(session, now=now)
        local_row = await session.scalar(
            select(asset_model).where(asset_model.id == local_id)
        )
        remote_row = await session.scalar(
            select(asset_model).where(asset_model.id == remote_id)
        )

    assert removed == 1
    assert local_row is not None and local_row.status == "expired"
    assert remote_row is not None and remote_row.status == "cleanup_pending"
    assert remote_row.cache_path == str(cache_path)
    assert unlinked == []
    assert cache_path.exists()
    await engine.dispose()
