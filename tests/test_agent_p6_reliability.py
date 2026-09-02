# ruff: noqa: E501,PLR2004,TRY003
from __future__ import annotations

import asyncio
import gc
import sys
import weakref
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nonebot
import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.schema import UniqueConstraint

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

import src.plugins.yawn_core.llm as llm_module
from src.plugins.yawn_core.data_models.group_agent_message import GroupAgentMessage
from src.plugins.yawn_core.yawn_agent import (
    capabilities,
    collector,
    execution_trace,
    media,
    memory,
)


def test_group_message_identity_is_scoped_by_group() -> None:
    constraints = [
        item
        for item in GroupAgentMessage.__table__.constraints
        if isinstance(item, UniqueConstraint)
    ]
    target = next(
        item for item in constraints if item.name == "uq_agent_message_bot_group_message"
    )
    assert [column.name for column in target.columns] == [
        "bot_id",
        "group_id",
        "message_id",
    ]


@pytest.mark.asyncio
async def test_capability_status_caches_evict_with_primary_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Bot:
        self_id = 9

        async def call_api(self, _action: str, **_kwargs: Any) -> dict[str, str]:
            return {"role": "member"}

    capabilities.reset_capability_cache()
    monkeypatch.setattr(capabilities, "_MAX_CACHE_ENTRIES", 2)
    bot = Bot()
    for group_id in (1, 2, 3):
        await capabilities.probe_group_capabilities(bot, group_id)
    assert len(capabilities._capability_cache) == 2
    assert len(capabilities._capability_probe_status) == 2
    assert (9, 1) not in capabilities._capability_probe_status

    monkeypatch.setattr(capabilities, "_MAX_SEGMENT_CACHE_ENTRIES", 2)
    for group_id in (1, 2, 3):
        capabilities.mark_segment_unsupported(bot, group_id, "reply")
    assert len(capabilities._segment_unsupported_cache) == 2
    assert len(capabilities._segment_failure_status) == 2
    assert (9, 1, "reply") not in capabilities._segment_failure_status
    capabilities.reset_capability_cache()


def test_transient_lock_registries_do_not_retain_idle_groups() -> None:
    collector_key = (9, 81001)
    collector_lock = collector.group_lock(collector_key[1], collector_key[0])
    collector_ref = weakref.ref(collector_lock)
    assert collector._locks.get(collector_key) is collector_lock
    del collector_lock
    gc.collect()
    assert collector_ref() is None
    assert collector_key not in collector._locks

    memory_lock = memory._compaction_lock(81002)
    memory_ref = weakref.ref(memory_lock)
    assert memory._COMPACTION_LOCKS.get(81002) is memory_lock
    del memory_lock
    gc.collect()
    assert memory_ref() is None
    assert 81002 not in memory._COMPACTION_LOCKS


def test_execution_trace_group_registry_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_trace._recent_traces.clear()
    monkeypatch.setattr(execution_trace, "_MAX_TRACKED_GROUPS", 2)
    for group_id in (91, 92, 93):
        trace = execution_trace.begin_execution_trace(
            group_id,
            mode="dialogue",
            source="runtime",
        )
        execution_trace.finish_execution_trace(trace, outcome="completed")
    assert list(execution_trace._recent_traces) == [92, 93]
    assert execution_trace.recent_execution_traces(91) == []
    execution_trace._recent_traces.clear()


def test_llm_unexpected_sdk_error_degrades_instead_of_escaping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RaisingCompletions:
        async def create(self, **_kwargs: Any) -> None:
            raise RuntimeError("provider transport exploded")

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=RaisingCompletions())
    )
    monkeypatch.setattr(llm_module, "get_client", lambda _provider="default": fake_client)

    text = asyncio.run(
        llm_module.complete(
            [{"role": "user", "content": "hi"}],  # pyright: ignore[reportArgumentType]
            task="agent_dialogue",
        )
    )
    assert text is None

    result = asyncio.run(
        llm_module.complete_with_tools_result(
            [{"role": "user", "content": "hi"}],  # pyright: ignore[reportArgumentType]
            [],
            task="agent_dialogue",
        )
    )
    assert result.message is None
    assert result.outcome == "error"


class _CleanupRows:
    def __init__(self, path: str, *, empty: bool = False) -> None:
        self.path = path
        self.empty = empty

    def scalars(self) -> _CleanupRows:
        return self

    def all(self) -> list[Any]:
        if self.empty:
            return []
        return [SimpleNamespace(cache_path=self.path)]


class _CleanupDeleteResult:
    def __init__(self, rowcount: int = 1) -> None:
        self.rowcount = rowcount


class _CleanupSession:
    def __init__(self, path: str, events: list[str], *, fail_commit: bool) -> None:
        self.path = path
        self.events = events
        self.fail_commit = fail_commit
        self.execute_count = 0

    async def execute(self, _statement: Any) -> Any:
        self.execute_count += 1
        if self.execute_count == 1:
            # AgentMediaAsset select: this compatibility fixture represents a
            # database that only has one legacy AgentMediaCache row.
            return _CleanupRows(self.path, empty=True)
        if self.execute_count == 2:
            return _CleanupRows(self.path)
        if self.execute_count == 3:
            return _CleanupDeleteResult(1)
        return _CleanupDeleteResult(1)

    async def flush(self) -> None:
        self.events.append("flush")

    async def commit(self) -> None:
        self.events.append("commit")
        if self.fail_commit:
            raise SQLAlchemyError("commit failed")

    async def rollback(self) -> None:
        self.events.append("rollback")


@pytest.mark.asyncio
async def test_media_cleanup_never_unlinks_before_db_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(media, "unlink_cache_file", lambda _path: events.append("unlink"))
    removed = await media.cleanup_media_cache(
        _CleanupSession("safe-cache.png", events, fail_commit=False)
    )
    assert removed == 1
    assert events == ["flush", "flush", "commit", "unlink"]


@pytest.mark.asyncio
async def test_media_cleanup_commit_failure_keeps_disk_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(media, "unlink_cache_file", lambda _path: events.append("unlink"))
    with pytest.raises(SQLAlchemyError):
        await media.cleanup_media_cache(
            _CleanupSession("safe-cache.png", events, fail_commit=True)
        )
    assert events == ["flush", "flush", "commit", "rollback"]
    assert "unlink" not in events
