"""WebUI 写接口回归：乐观锁、显式 null、首次配置、广播与时间戳口径。"""

from __future__ import annotations

import asyncio
import importlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import nonebot
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if nonebot.get_plugin("yawn_core") is None:
    nonebot.load_from_toml("pyproject.toml")

route_models = importlib.import_module("src.plugins.yawn_core.webui.route_models")
route_helpers = importlib.import_module("src.plugins.yawn_core.webui.route_helpers")
service = importlib.import_module("src.plugins.yawn_core.webui.service")
hub_module = importlib.import_module("src.plugins.yawn_core.webui.hub")
audits = importlib.import_module("src.plugins.yawn_core.webui.audits")
config_routes = importlib.import_module(
    "src.plugins.yawn_core.webui.agent_config_routes"
)
persona_routes = importlib.import_module(
    "src.plugins.yawn_core.webui.agent_persona_routes"
)
config_store = importlib.import_module("src.plugins.yawn_core.yawn_agent.config_store")
group_agent_config = importlib.import_module(
    "src.plugins.yawn_core.data_models.group_agent_config"
)
GroupAgentConfig = group_agent_config.GroupAgentConfig


class _SessionFactory:
    def __init__(self, session: Any) -> None:
        self._session = session

    async def __aenter__(self) -> Any:
        return self._session

    async def __aexit__(self, *_args: object) -> None:
        return None


class _ConfigSession:
    """共享同一 GroupAgentConfig 行；commit 主动让出事件循环以制造交错。"""

    def __init__(self, row: Any) -> None:
        self.row = row
        self.added: list[Any] = []

    async def get(self, model: Any, key: Any) -> Any:
        if model.__name__ == "BotGroup":
            return service.BotGroup(group_id=key, group_name="测试群")
        if model is GroupAgentConfig:
            return self.row
        return None

    def add(self, row: Any) -> None:
        self.added.append(row)
        if isinstance(row, GroupAgentConfig):
            self.row = row

    async def commit(self) -> None:
        await asyncio.sleep(0.01)

    async def refresh(self, _row: Any) -> None:
        return None


def _persisted_config(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        column.name: (
            column.default.arg(None)
            if column.default is not None and callable(column.default.arg)
            else column.default.arg
            if column.default is not None
            else None
        )
        for column in GroupAgentConfig.__table__.columns
    }
    values.update(group_id=100, **overrides)
    return GroupAgentConfig(**values)


async def _no_notify(*_args: Any, **_kwargs: Any) -> None:
    return None


def test_patch_bodies_default_version_and_reject_explicit_null() -> None:
    # 首次创建配置/人设时前端没有版本号；缺省 version 不能 422。
    assert (
        route_models.AgentConfigPatch.model_validate({"dailyLimit": 3}).version is None
    )
    assert (
        route_models.MemoryPatchBody.model_validate({"salience": 0.5}).version is None
    )
    persona = route_models.PersonaPatch.model_validate(
        {"enabled": True, "card": {"presetId": "natural", "name": "Yawn"}}
    )
    assert persona.version is None

    for payload in ({"enabled": None}, {"dailyLimit": None}, {"toolAllowlist": None}):
        with pytest.raises(ValidationError) as exc_info:
            route_models.AgentConfigPatch.model_validate({"version": "v", **payload})
        assert exc_info.value.errors()[0]["loc"] == (next(iter(payload)),)
    for payload in ({"content": None}, {"salience": None}, {"confidence": None}):
        with pytest.raises(ValidationError):
            route_models.MemoryPatchBody.model_validate({"version": "v", **payload})
    # expiresInDays=null 表示永不过期，仍然合法。
    never = route_models.MemoryPatchBody.model_validate({"expiresInDays": None})
    assert never.model_dump(exclude_unset=True) == {"expires_in_days": None}


def test_unconfigured_agent_config_uses_model_default_allowlist() -> None:
    result = service.serialize_agent_config(None, 100)
    assert result["toolAllowlist"] == group_agent_config.default_tool_allowlist()
    assert "set_essence_message" in result["toolAllowlist"]


def test_content_versions_ignore_runtime_writes() -> None:
    row = _persisted_config()
    config_version = service.agent_config_version(row)
    persona_version = service.persona_content_version(row)
    # Bot 运行时写用量计数器/时间戳，不应让管理员的草稿 409。
    row.proactive_count = 9
    row.admin_tool_count = 3
    row.updated_at = datetime(2030, 1, 1)  # noqa: DTZ001
    assert service.agent_config_version(row) == config_version
    assert service.persona_content_version(row) == persona_version
    route_helpers.check_version(
        row, config_version, version_of=service.agent_config_version
    )

    row.daily_limit = 1
    assert service.agent_config_version(row) != config_version
    assert service.persona_content_version(row) == persona_version
    with pytest.raises(HTTPException) as exc_info:
        route_helpers.check_version(
            row, config_version, version_of=service.agent_config_version
        )
    assert exc_info.value.status_code == 409  # noqa: PLR2004

    memory = service.AgentMemory(content="喜欢猫", salience=0.5, confidence=0.9)
    memory_version = service.memory_version(memory)
    memory.updated_at = datetime(2030, 1, 1)  # noqa: DTZ001
    assert service.memory_version(memory) == memory_version
    assert service.serialize_memory(memory)["version"] == memory_version
    memory.content = "喜欢狗"
    assert service.memory_version(memory) != memory_version


class _SnapshotSession(_ConfigSession):
    """每个会话读到独立的行副本，commit 后才写回“数据库”，模拟真实隔离。"""

    def __init__(self, db: dict[str, Any]) -> None:
        super().__init__(_persisted_config(**db))
        self.db = db

    async def commit(self) -> None:
        await asyncio.sleep(0.01)
        self.db["daily_limit"] = self.row.daily_limit


@pytest.mark.asyncio
async def test_concurrent_stale_config_writers_cannot_both_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db: dict[str, Any] = {"daily_limit": 30}
    monkeypatch.setattr(
        config_routes, "get_session", lambda: _SessionFactory(_SnapshotSession(db))
    )
    monkeypatch.setattr(config_routes.hub, "notify_change", _no_notify)
    stale = service.agent_config_version(_persisted_config(**db))

    async def write(limit: int) -> Any:
        body = route_models.AgentConfigPatch.model_validate(
            {"version": stale, "dailyLimit": limit}
        )
        return await config_routes.patch_agent_config(100, body, None)

    results = await asyncio.gather(write(5), write(6), return_exceptions=True)
    conflicts = [item for item in results if isinstance(item, HTTPException)]
    assert len(conflicts) == 1
    assert conflicts[0].status_code == 409  # noqa: PLR2004
    assert db["daily_limit"] == 5  # noqa: PLR2004


@pytest.mark.asyncio
async def test_first_persona_save_initialises_persona_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _ConfigSession(None)
    monkeypatch.setattr(persona_routes, "get_session", lambda: _SessionFactory(session))
    monkeypatch.setattr(persona_routes.hub, "notify_change", _no_notify)
    body = route_models.PersonaPatch.model_validate(
        {
            "enabled": True,
            "card": {"presetId": "quiet_observer", "name": "Yawn", "warmth": 1},
        }
    )

    result = await persona_routes.put_agent_persona(100, body, None)

    assert session.row is not None
    assert session.row.persona_version == 2  # noqa: PLR2004
    assert session.row.persona_enabled is True
    assert result["data"]["version"] == service.persona_content_version(session.row)


@pytest.mark.asyncio
async def test_get_or_create_config_conflict_only_rolls_back_savepoint() -> None:
    existing = GroupAgentConfig(group_id=100)

    class _Nested:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_args: object) -> bool:
            return False

    class _Session:
        def __init__(self) -> None:
            self.gets = 0
            self.rolled_back = False

        async def get(self, _model: Any, _key: Any) -> Any:
            self.gets += 1
            return None if self.gets == 1 else existing

        def add(self, _row: Any) -> None:
            return None

        def begin_nested(self) -> _Nested:
            return _Nested()

        async def flush(self) -> None:
            raise config_store.IntegrityError("insert", {}, Exception("dup"))

        async def rollback(self) -> None:
            # 整体 rollback 会丢掉调用方已暂存的 GroupFeature 变更。
            self.rolled_back = True

    session = _Session()
    assert await config_store.get_or_create_config(session, 100) is existing
    assert session.rolled_back is False


@pytest.mark.asyncio
async def test_broadcast_drops_stalled_client_without_blocking_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hub_module, "_SEND_TIMEOUT_SECONDS", 0.05)

    class _Socket:
        def __init__(self, *, stall: bool) -> None:
            self.stall = stall
            self.sent: list[Any] = []
            self.closed: int | None = None

        async def send_json(self, payload: Any) -> None:
            if self.stall:
                await asyncio.Event().wait()
            self.sent.append(payload)

        async def close(self, code: int = 1000) -> None:
            self.closed = code

    hub = hub_module.WebUIHub()
    stalled = _Socket(stall=True)
    healthy = _Socket(stall=False)
    hub._clients[stalled] = asyncio.Lock()
    hub._clients[healthy] = asyncio.Lock()

    await asyncio.wait_for(hub.broadcast({"type": "ping"}), timeout=1)

    assert healthy.sent == [{"type": "ping"}]
    assert stalled not in hub._clients
    assert healthy in hub._clients
    assert stalled.closed == 1011  # noqa: PLR2004


@pytest.mark.asyncio
async def test_agent_audit_failed_filter_matches_error_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[Any] = []

    class _Result:
        def scalars(self) -> "_Result":
            return self

        def all(self) -> list[Any]:
            return []

    class _Session:
        async def scalar(self, stmt: Any) -> int:
            statements.append(stmt)
            return 0

        async def execute(self, stmt: Any) -> _Result:
            statements.append(stmt)
            return _Result()

    monkeypatch.setattr(audits, "get_session", lambda: _SessionFactory(_Session()))
    await audits.get_agent_audits(
        None, page=1, page_size=20, group_id=1, result="failed"
    )

    compiled = str(statements[0].compile(compile_kwargs={"literal_binds": True}))
    for value in ("'error'", "'send_failed'"):
        assert value in compiled
    assert "'failed'" not in compiled


def test_model_timestamp_defaults_use_naive_beijing_time() -> None:
    column = GroupAgentConfig.__table__.c.updated_at
    expected = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
    for default in (column.default, column.onupdate):
        assert default is not None
        value = default.arg(None)
        assert value.tzinfo is None
        assert abs((value - expected).total_seconds()) < 60  # noqa: PLR2004
