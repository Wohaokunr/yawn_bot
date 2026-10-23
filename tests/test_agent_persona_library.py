# ruff: noqa: E501, PLR2004, TRY003
"""人设库（v3）：模板 CRUD、唯一全局人设、缓存刷新与群人设的回退顺序。"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nonebot
import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if nonebot.get_plugin("yawn_core") is None:
    nonebot.load_from_toml("pyproject.toml")

persona = importlib.import_module("src.plugins.yawn_core.yawn_agent.persona")
library = importlib.import_module("src.plugins.yawn_core.yawn_agent.persona_library")
template_models = importlib.import_module(
    "src.plugins.yawn_core.data_models.agent_persona_template"
)


@pytest.fixture(autouse=True)
def _reset_global_cache() -> Any:
    persona.set_global_persona_card(None)
    yield
    persona.set_global_persona_card(None)


async def _session_factory() -> tuple[Any, Any]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    table = template_models.AgentPersonaTemplate.__table__
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: table.metadata.create_all(
                sync_connection, tables=[table]
            )
        )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _card(**kwargs: Any) -> Any:
    return persona.PersonaCard(**kwargs)


def test_migration_creates_persona_library_after_memory_access() -> None:
    migration = importlib.import_module(
        "data.nonebot_plugin_orm.migrations.yawn_core.b8d0f2a4c6e9_agent_persona_library"
    )
    assert migration.revision == "b8d0f2a4c6e9"
    assert migration.down_revision == "a7c3e9f1b5d2"
    columns = template_models.AgentPersonaTemplate.__table__.columns
    assert {"name", "card", "is_global", "revision"} <= set(columns.keys())


@pytest.mark.asyncio
async def test_create_template_rejects_duplicate_names_case_insensitively() -> None:
    engine, factory = await _session_factory()
    async with factory() as session:
        await library.create_template(session, name="  猫娘  ", card=_card(name="喵"))
        await session.commit()
        with pytest.raises(library.PersonaLibraryError) as exc_info:
            await library.create_template(session, name="猫娘", card=_card())
        assert exc_info.value.status == 409
        with pytest.raises(library.PersonaLibraryError):
            await library.create_template(session, name="   ", card=_card())
        rows = await library.list_templates(session)
        assert [row.name for row in rows] == ["猫娘"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_only_one_template_can_be_global_and_cache_follows_it() -> None:
    engine, factory = await _session_factory()
    async with factory() as session:
        first = await library.create_template(
            session, name="A", card=_card(name="甲"), is_global=True
        )
        await session.commit()
        await library.refresh_global_persona(session)
        assert persona.global_persona().source == persona.PERSONA_SOURCE_GLOBAL
        assert persona.global_persona().card.name == "甲"

        second = await library.create_template(
            session, name="B", card=_card(name="乙"), is_global=True
        )
        await session.commit()
        await session.refresh(first)
        assert first.is_global is False
        assert second.is_global is True
        await library.refresh_global_persona(session)
        assert persona.global_persona().card.name == "乙"
        assert persona.global_persona().library_name == "B"

        # 全局人设排在列表最前，方便命令里用序号选择。
        rows = await library.list_templates(session)
        assert rows[0].name == "B"
    await engine.dispose()


@pytest.mark.asyncio
async def test_update_template_bumps_revision_and_checks_version() -> None:
    engine, factory = await _session_factory()
    async with factory() as session:
        row = await library.create_template(session, name="A", card=_card())
        await session.commit()
        assert library.serialize_template(row)["version"] == "1"

        same = await library.update_template(session, row.id, version="1", name="A")
        assert same.revision == 1

        updated = await library.update_template(
            session, row.id, version="1", card=_card(warmth=4), is_global=True
        )
        await session.commit()
        assert updated.revision == 2
        assert library.template_card(updated).warmth == 4

        with pytest.raises(library.PersonaLibraryError) as exc_info:
            await library.update_template(session, row.id, version="1", name="C")
        assert exc_info.value.status == 409
        with pytest.raises(library.PersonaLibraryError) as missing:
            await library.update_template(session, 999, version=None, name="C")
        assert missing.value.status == 404
    await engine.dispose()


@pytest.mark.asyncio
async def test_delete_global_template_falls_back_to_builtin() -> None:
    engine, factory = await _session_factory()
    async with factory() as session:
        row = await library.create_template(
            session, name="A", card=_card(name="甲"), is_global=True
        )
        await session.commit()
        await library.refresh_global_persona(session)
        assert persona.global_persona().card.name == "甲"

        with pytest.raises(library.PersonaLibraryError):
            await library.delete_template(session, row.id, version="7")
        assert await library.delete_template(session, row.id, version="1") is True
        await session.commit()
        await library.refresh_global_persona(session)
        assert persona.global_persona().source == persona.PERSONA_SOURCE_BUILTIN
    await engine.dispose()


@pytest.mark.asyncio
async def test_find_template_by_name_accepts_index_and_name() -> None:
    engine, factory = await _session_factory()
    async with factory() as session:
        await library.create_template(session, name="beta", card=_card())
        await library.create_template(session, name="alpha", card=_card())
        await session.commit()
        by_index = await library.find_template_by_name(session, "1")
        assert by_index is not None
        assert by_index.name == "alpha"
        by_name = await library.find_template_by_name(session, "BETA")
        assert by_name is not None
        assert by_name.name == "beta"
        assert await library.find_template_by_name(session, "9") is None
        assert await library.find_template_by_name(session, "") is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_library_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(library, "MAX_LIBRARY_ITEMS", 2)
    engine, factory = await _session_factory()
    async with factory() as session:
        await library.create_template(session, name="A", card=_card())
        await library.create_template(session, name="B", card=_card())
        with pytest.raises(library.PersonaLibraryError, match="最多"):
            await library.create_template(session, name="C", card=_card())
    await engine.dispose()


@pytest.mark.asyncio
async def test_refresh_global_persona_survives_missing_table() -> None:
    class _BrokenSession:
        async def scalar(self, *_args: object) -> object:
            raise SQLAlchemyError("no such table")

    persona.set_global_persona_card(_card(name="旧缓存"), name="旧")
    await library.refresh_global_persona(_BrokenSession())
    assert persona.global_persona().source == persona.PERSONA_SOURCE_BUILTIN


def test_group_persona_resolution_order_group_then_global_then_builtin() -> None:
    inherit = SimpleNamespace(
        group_id=1, persona_enabled=False, persona_version=1, persona_profile={}
    )
    assert persona.resolve_persona_source(inherit).source == persona.PERSONA_SOURCE_BUILTIN

    persona.set_global_persona_card(_card(name="全局喵"), name="全局")
    resolved = persona.resolve_persona_source(inherit)
    assert resolved.source == persona.PERSONA_SOURCE_GLOBAL
    assert resolved.card.name == "全局喵"
    assert persona.resolve_persona(inherit)["name"] == "全局喵"

    custom = SimpleNamespace(
        group_id=2, persona_enabled=False, persona_version=1, persona_profile={}
    )
    persona.apply_persona_card(custom, _card(name="本群喵"), enabled=True)
    assert persona.resolve_persona_source(custom).source == persona.PERSONA_SOURCE_GROUP
    assert persona.effective_persona_card(custom).name == "本群喵"

    # 关闭本群自定义后保留草稿，但运行时回到全局人设。
    custom.persona_enabled = False
    assert persona.effective_persona_card(custom).name == "全局喵"
    assert persona.group_persona_card(custom).name == "本群喵"


def test_migration_upgrade_and_downgrade_are_idempotent() -> None:
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration = importlib.import_module(
        "data.nonebot_plugin_orm.migrations.yawn_core.b8d0f2a4c6e9_agent_persona_library"
    )
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
            migration.upgrade()
            inspector = sa.inspect(connection)
            assert "yawn_core_agentpersonatemplate" in inspector.get_table_names()
            indexes = {index["name"] for index in inspector.get_indexes("yawn_core_agentpersonatemplate")}
            assert "ix_yawn_core_agentpersonatemplate_is_global" in indexes
            migration.downgrade()
            migration.downgrade()
            assert "yawn_core_agentpersonatemplate" not in sa.inspect(connection).get_table_names()
    engine.dispose()
