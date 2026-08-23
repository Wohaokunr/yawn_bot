from __future__ import annotations

import importlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import nonebot
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if nonebot.get_plugin("yawn_core") is None:
    nonebot.load_from_toml("pyproject.toml")

auth = importlib.import_module("src.plugins.yawn_core.webui.auth")
app_module = importlib.import_module("src.plugins.yawn_core.webui.app")
service = importlib.import_module("src.plugins.yawn_core.webui.service")


@pytest.fixture(autouse=True)
def _webui_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth.config, "webui_admin_token", SecretStr("x" * 40))
    monkeypatch.setattr(auth.config, "webui_session_ttl_hours", 12)
    auth.reset_login_failures_for_tests()


def test_signed_session_rejects_tampering_and_expiry() -> None:
    cookie, session = auth.create_session(now=1_000)

    assert auth.verify_session(cookie, now=1_001) == session
    assert auth.verify_session(cookie + "tampered", now=1_001) is None
    assert auth.verify_session(cookie, now=session.expires_at) is None
    assert len(session.actor_fingerprint) == 16  # noqa: PLR2004


def test_login_rate_limit_and_reset() -> None:
    for index in range(5):
        assert not auth.check_admin_token("client", "wrong", now=float(index))
    assert not auth.login_allowed("client", now=5.0)
    assert auth.login_allowed("client", now=301.0)
    assert auth.check_admin_token("client", "x" * 40, now=301.0)
    assert auth.login_allowed("client", now=301.0)


def test_registered_fastapi_routes_serve_login_and_spa() -> None:
    app = FastAPI()
    app_module.register(app)
    client = TestClient(app)
    response = client.post("/webui/api/v1/auth/login", json={"token": "x" * 40})
    assert response.status_code == 200  # noqa: PLR2004
    assert response.json()["data"]["authenticated"] is True
    assert client.get("/webui").status_code == 200  # noqa: PLR2004


def test_agent_config_and_persona_validation() -> None:
    with pytest.raises(ValidationError):
        app_module.AgentConfigPatch.model_validate(
            {"version": None, "triggerMode": "always", "rawRetentionDays": 0}
        )
    body = app_module.AgentConfigPatch.model_validate(
        {
            "version": None,
            "triggerMode": "mention_only",
            "toolAllowlist": ["mute_member", "mute_member"],
        }
    )
    assert body.tool_allowlist == ["mute_member"]
    assert (
        app_module.AgentConfigPatch.model_validate(
            {"version": None, "crossGroupVisibility": "public_summary"}
        ).cross_group_visibility
        == "public_summary"
    )
    with pytest.raises(ValidationError):
        app_module.AgentConfigPatch.model_validate(
            {"version": None, "crossGroupVisibility": "all"}
        )

    with pytest.raises(ValidationError):
        app_module.AgentConfigPatch.model_validate(
            {"version": None, "proactiveActiveProbability": 1.5}
        )
    with pytest.raises(ValidationError):
        app_module.AgentConfigPatch.model_validate(
            {"version": None, "proactiveActiveWindowMinutes": 0}
        )
    proactive_patch = app_module.AgentConfigPatch.model_validate(
        {
            "version": None,
            "proactiveActiveEnabled": False,
            "proactiveActiveProbability": 0.1,
            "proactiveActiveWindowMinutes": 6,
        }
    )
    assert proactive_patch.proactive_active_enabled is False
    assert proactive_patch.proactive_active_probability == 0.1  # noqa: PLR2004
    assert proactive_patch.proactive_active_window_minutes == 6  # noqa: PLR2004

    with pytest.raises(ValidationError):
        app_module.PersonaPatch.model_validate(
            {"version": None, "enabled": True, "overrides": {"unknown": "x"}}
        )


def test_version_conflict_is_explicit() -> None:
    row = service.GroupAgentConfig(group_id=1)
    row.updated_at = None
    app_module.check_version(row, None)
    with pytest.raises(HTTPException) as exc_info:
        app_module.check_version(row, "stale")
    assert exc_info.value.status_code == 409  # noqa: PLR2004


class _FeatureSession:
    def __init__(self, row: Any = None) -> None:
        self.row = row
        self.added: list[Any] = []
        self.deleted: list[Any] = []

    async def get(self, _model: Any, _key: Any) -> Any:
        return self.row

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def delete(self, row: Any) -> None:
        self.deleted.append(row)


@pytest.mark.asyncio
async def test_group_feature_supports_three_state_override() -> None:
    existing = service.GroupFeature(group_id=1, feature="rpg", enabled=True)
    delete_session = _FeatureSession(existing)
    await service.set_group_feature(delete_session, 1, "rpg", None)
    assert delete_session.deleted == [existing]

    create_session = _FeatureSession()
    await service.set_group_feature(create_session, 1, "rpg", override=False)
    assert len(create_session.added) == 1
    assert create_session.added[0].enabled is False


def test_big_integer_identifiers_are_serialized_as_strings() -> None:
    row = service.AgentMemory(
        id=9_007_199_254_740_993,
        scope="group",
        group_id=9_007_199_254_740_992,
        subject_user_id=9_007_199_254_740_991,
        memory_type="summary",
        memory_key="key",
        content="content",
        evidence_message_ids=[],
        source_kind="manual",
        related_user_ids=[9_007_199_254_740_991],
        salience=0.5,
        confidence=0.5,
        visibility="group",
    )
    payload = service.serialize_memory(row)
    assert payload["id"] == "9007199254740993"
    assert payload["groupId"] == "9007199254740992"
    assert payload["subjectUserId"] == "9007199254740991"
    assert payload["sourceKind"] == "manual"
    assert payload["relatedUserIds"] == ["9007199254740991"]

    row.subject_user_id = 0
    assert service.serialize_memory(row)["subjectUserId"] is None


def test_iso_serializes_naive_datetime_as_beijing_time() -> None:
    # 全库约定 naive datetime 为北京时间；若误标为 UTC，前端显示会偏移 8 小时。
    assert service.iso(None) is None
    naive = datetime(2026, 8, 21, 12, 0, 0)  # noqa: DTZ001 被测对象就是 naive 约定
    assert service.iso(naive) == "2026-08-21T12:00:00+08:00"
    aware_utc = datetime(2026, 8, 21, 4, 0, 0, tzinfo=timezone.utc)
    assert service.iso(aware_utc) == "2026-08-21T04:00:00+00:00"


@pytest.mark.asyncio
async def test_games_live_reports_loaded_sub_plugins_available() -> None:
    # 回归：games.py 曾用单点相对导入（.yawn_werewolf）解析兄弟包，
    # 永远抛 ModuleNotFoundError，导致子插件全部启用时前端仍显示"子插件未加载"。
    games = importlib.import_module("src.plugins.yawn_core.webui.games")
    payload = (await games.get_live_games(None))["data"]

    assert payload["werewolf"]["available"] is True
    assert payload["rpg"]["available"] is True
    assert payload["werewolf"]["games"] == []
    assert payload["rpg"]["games"] == []
    # 必须读到子插件注册表的同一模块实例，否则实时对局永远为空。
    assert (
        games._werewolf_state()
        is sys.modules["src.plugins.yawn_core.yawn_werewolf.state"]
    )
    assert games._rpg_state() is sys.modules["src.plugins.yawn_core.yawn_rpg.state"]
    assert (
        games._werewolf_game_log()
        is sys.modules["src.plugins.yawn_core.yawn_werewolf.game_log"]
    )


def test_games_resolvers_retry_after_failure() -> None:
    # 回归：解析失败不得落缓存，否则子插件晚加载后页面无法自动恢复。
    games = importlib.import_module("src.plugins.yawn_core.webui.games")
    games._ww_state_resolved = False
    games._ww_state_module = None
    try:
        with pytest.MonkeyPatch.context() as patch:
            # 让真实解析抛错一次，验证失败后 resolved 仍为 False（下次会重试）。
            import builtins

            real_import = builtins.__import__

            def failing_import(name: str, *args: object, **kwargs: object) -> Any:
                if "yawn_werewolf" in name:
                    raise ImportError
                return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

            patch.setattr(builtins, "__import__", failing_import)
            assert games._werewolf_state() is None
            assert games._ww_state_resolved is False
    finally:
        games._ww_state_resolved = False
        games._ww_state_module = None
    # 真实环境里子插件已加载，下一次调用应恢复并缓存。
    assert (
        games._werewolf_state()
        is sys.modules["src.plugins.yawn_core.yawn_werewolf.state"]
    )
    assert games._ww_state_resolved is True


def test_memory_create_body_validates_type_and_expiry() -> None:
    body = app_module.MemoryCreateBody.model_validate(
        {
            "type": "manual",
            "key": "群规",
            "content": "晚上十一点后保持安静",
            "subjectUserId": 123,
            "salience": 0.9,
            "expiresInDays": None,
        }
    )
    assert body.subject_user_id == 123  # noqa: PLR2004
    assert body.expires_in_days is None

    with pytest.raises(ValidationError):
        app_module.MemoryCreateBody.model_validate(
            {"type": "unknown", "key": "k", "content": "c"}
        )
    with pytest.raises(ValidationError):
        app_module.MemoryCreateBody.model_validate(
            {"type": "manual", "key": "k", "content": "c", "expiresInDays": 0}
        )
    with pytest.raises(ValidationError):
        app_module.MemoryCreateBody.model_validate(
            {"type": "manual", "key": "", "content": "c"}
        )


def test_memory_patch_body_requires_updatable_field() -> None:
    updates = app_module.MemoryPatchBody.model_validate(
        {"version": "v1", "salience": 0.8, "expiresInDays": None}
    ).model_dump(exclude_unset=True, exclude={"version"})
    assert updates == {"salience": 0.8, "expires_in_days": None}

    empty = app_module.MemoryPatchBody.model_validate({"version": "v1"})
    assert empty.model_dump(exclude_unset=True, exclude={"version"}) == {}


def test_privacy_patch_body_accepts_camel_alias() -> None:
    assert app_module.PrivacyPatchBody.model_validate({"optedOut": True}).opted_out


def test_serialize_relation_and_agent_message_as_strings() -> None:
    relation = service.AgentRelation(
        id=9_007_199_254_740_993,
        group_id=9_007_199_254_740_992,
        subject_user_id=9_007_199_254_740_991,
        object_user_id=9_007_199_254_740_990,
        relation_type="mentions",
        source_kind="manual",
        note="管理员录入",
        confidence=0.55,
        evidence_count=3,
        last_seen_at=datetime(2026, 8, 21, 12, 0, 0),  # noqa: DTZ001
    )
    payload = service.serialize_relation(relation)
    assert payload["id"] == "9007199254740993"
    assert payload["subjectUserId"] == "9007199254740991"
    assert payload["sourceKind"] == "manual"
    assert payload["note"] == "管理员录入"
    assert payload["evidenceCount"] == 3  # noqa: PLR2004

    message = service.GroupAgentMessage(
        id=9_007_199_254_740_989,
        bot_id=1,
        message_id=42,
        group_id=9_007_199_254_740_992,
        user_id=9_007_199_254_740_991,
        sender_name="阿眠",
        role="member",
        normalized_text="你好",
        received_at=datetime(2026, 8, 21, 12, 0, 0),  # noqa: DTZ001
        expires_at=datetime(2026, 8, 28, 12, 0, 0),  # noqa: DTZ001
    )
    message_payload = service.serialize_agent_message(message)
    assert message_payload["userId"] == "9007199254740991"
    assert message_payload["receivedAt"] == "2026-08-21T12:00:00+08:00"


def test_relation_create_body_validates_fields() -> None:
    body = app_module.RelationCreateBody.model_validate(
        {
            "subjectUserId": 111,
            "objectUserId": 222,
            "type": "朋友",
            "note": "常一起开黑",
        }
    )
    assert body.subject_user_id == 111  # noqa: PLR2004
    assert body.confidence == 0.9  # noqa: PLR2004

    with pytest.raises(ValidationError):
        app_module.RelationCreateBody.model_validate(
            {"subjectUserId": 0, "objectUserId": 222, "type": "好友"}
        )
    with pytest.raises(ValidationError):
        app_module.RelationCreateBody.model_validate(
            {
                "subjectUserId": 111,
                "objectUserId": 222,
                "type": "好友",
                "confidence": 1.5,
            }
        )
    with pytest.raises(ValidationError):
        app_module.RelationCreateBody.model_validate(
            {"subjectUserId": 111, "objectUserId": 222, "type": ""}
        )


def test_relation_patch_body_requires_updatable_field() -> None:
    updates = app_module.RelationPatchBody.model_validate(
        {"note": "新备注", "confidence": 0.8}
    ).model_dump(exclude_unset=True)
    assert updates == {"note": "新备注", "confidence": 0.8}

    empty = app_module.RelationPatchBody.model_validate({})
    assert empty.model_dump(exclude_unset=True) == {}


class _ScalarResult:
    def __init__(self, values: list[Any]) -> None:
        self._values = values

    def scalars(self) -> "_ScalarResult":
        return self

    def all(self) -> list[Any]:
        return self._values


class _FakeRelationSession:
    """覆盖关系边端点用到的 get/execute/scalar/add/commit/refresh 路径。"""

    def __init__(
        self,
        *,
        opted_out: set[int] | None = None,
        conflict: bool = False,
        existing: Any = None,
    ) -> None:
        self.opted_out = opted_out or set()
        self.conflict = conflict
        self.existing = existing
        self.added: list[Any] = []
        self.committed = False

    async def get(self, model: Any, key: Any) -> Any:
        if model.__name__ == "BotGroup" and key == 100:  # noqa: PLR2004
            return app_module.BotGroup(group_id=key, group_name="测试群")
        return None

    async def execute(self, _stmt: Any) -> _ScalarResult:
        return _ScalarResult(list(self.opted_out))

    async def scalar(self, _stmt: Any) -> Any:
        return self.existing

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        if self.conflict:
            raise app_module.IntegrityError("insert", {}, Exception("dup"))

    async def rollback(self) -> None:
        return None

    async def refresh(self, _row: Any) -> None:
        return None


class _FakeSessionFactory:
    def __init__(self, session: Any) -> None:
        self._session = session

    async def __aenter__(self) -> Any:
        return self._session

    async def __aexit__(self, *_args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_create_relation_normalizes_type_and_marks_manual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeRelationSession()
    monkeypatch.setattr(
        app_module, "get_session", lambda: _FakeSessionFactory(session)
    )

    create_body = app_module.RelationCreateBody.model_validate(
        {
            "subjectUserId": 111,
            "objectUserId": 222,
            "type": "对象",
            "note": " 官宣过 ",
        }
    )
    result = await app_module.create_relation(100, create_body, None)

    assert len(session.added) == 1
    edge = session.added[0]
    assert edge.relation_type == "情侣"
    assert edge.source_kind == "manual"
    assert edge.note == "官宣过"
    assert result["data"]["sourceKind"] == "manual"


@pytest.mark.asyncio
async def test_create_relation_rejects_same_endpoints_and_opted_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeRelationSession()
    monkeypatch.setattr(app_module, "get_session", lambda: _FakeSessionFactory(session))

    with pytest.raises(HTTPException) as same:
        await app_module.create_relation(
            100,
            app_module.RelationCreateBody.model_validate(
                {"subjectUserId": 111, "objectUserId": 111, "type": "好友"}
            ),
            None,
        )
    assert same.value.status_code == 422  # noqa: PLR2004

    privacy_session = _FakeRelationSession(opted_out={222})
    monkeypatch.setattr(
        app_module, "get_session", lambda: _FakeSessionFactory(privacy_session)
    )
    with pytest.raises(HTTPException) as opted_out:
        await app_module.create_relation(
            100,
            app_module.RelationCreateBody.model_validate(
                {"subjectUserId": 111, "objectUserId": 222, "type": "好友"}
            ),
            None,
        )
    assert opted_out.value.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_create_relation_conflict_returns_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeRelationSession(conflict=True)
    monkeypatch.setattr(app_module, "get_session", lambda: _FakeSessionFactory(session))

    with pytest.raises(HTTPException) as conflict:
        await app_module.create_relation(
            100,
            app_module.RelationCreateBody.model_validate(
                {"subjectUserId": 111, "objectUserId": 222, "type": "好友"}
            ),
            None,
        )
    assert conflict.value.status_code == 409  # noqa: PLR2004


@pytest.mark.asyncio
async def test_update_relation_only_touches_note_and_confidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = service.AgentRelation(
        id=7,
        group_id=100,
        subject_user_id=111,
        object_user_id=222,
        relation_type="好友",
        source_kind="auto",
        note="",
        confidence=0.5,
        evidence_count=1,
        last_seen_at=datetime(2026, 8, 21, 12, 0, 0),  # noqa: DTZ001
    )
    session = _FakeRelationSession(existing=existing)
    monkeypatch.setattr(
        app_module, "get_session", lambda: _FakeSessionFactory(session)
    )

    patch_body = app_module.RelationPatchBody.model_validate(
        {"note": "常一起开黑", "confidence": 0.85}
    )
    result = await app_module.update_relation(100, 7, patch_body, None)

    assert existing.relation_type == "好友"
    assert existing.note == "常一起开黑"
    assert existing.confidence == 0.85  # noqa: PLR2004
    assert result["data"]["note"] == "常一起开黑"


class _FakeGraphSession:
    """图谱端点用到的 get/execute 路径：按查询目标模型分发结果。"""

    def __init__(
        self,
        *,
        opted_out: set[int] | None = None,
        relations: list[Any] | None = None,
        members: list[tuple[Any, Any]] | None = None,
    ) -> None:
        self.opted_out = opted_out or set()
        self.relations = relations or []
        self.members = members or []

    async def get(self, model: Any, key: Any) -> Any:
        if model.__name__ == "BotGroup" and key == 100:  # noqa: PLR2004
            return app_module.BotGroup(group_id=key, group_name="测试群")
        return None

    async def execute(self, stmt: Any) -> _ScalarResult:
        entity = stmt.column_descriptions[0]["entity"]
        name = getattr(entity, "__name__", str(entity))
        if name == "AgentPrivacy":
            return _ScalarResult(list(self.opted_out))
        if name == "AgentRelation":
            # 模拟 SQL 的 not_in 过滤：隐私退出成员参与的边不出现在结果中。
            rows = [
                row
                for row in self.relations
                if row.subject_user_id not in self.opted_out
                and row.object_user_id not in self.opted_out
            ]
            return _ScalarResult(rows)
        return _ScalarResult(list(self.members))


def _graph_relation(
    relation_id: int, subject: int, target: int, relation_type: str = "好友"
) -> Any:
    return service.AgentRelation(
        id=relation_id,
        group_id=100,
        subject_user_id=subject,
        object_user_id=target,
        relation_type=relation_type,
        source_kind="auto",
        note="",
        confidence=0.8,
        evidence_count=1,
        last_seen_at=datetime(2026, 8, 21, 12, 0, 0),  # noqa: DTZ001
    )


@pytest.mark.asyncio
async def test_relation_graph_merges_members_and_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relations = [
        _graph_relation(1, 111, 222),
        _graph_relation(2, 333, 111, "对立"),
        # 444 已隐私退出，含其的边应被过滤。
        _graph_relation(3, 111, 444),
    ]
    members = [
        (
            service.UserGroup(group_id=100, user_id=111, role="member"),
            service.BotUser(user_id=111, nickname="小明"),
        ),
        (
            service.UserGroup(group_id=100, user_id=222, role="admin"),
            service.BotUser(user_id=222, nickname="小红"),
        ),
        # 555 是无边成员，应标记为未连接。
        (
            service.UserGroup(group_id=100, user_id=555, role="member"),
            service.BotUser(user_id=555, nickname="小刚"),
        ),
        # 333 已不在成员表（退群残留），仍需补节点避免边悬空。
    ]
    session = _FakeGraphSession(
        opted_out={444}, relations=relations, members=members
    )
    monkeypatch.setattr(app_module, "get_session", lambda: _FakeSessionFactory(session))

    result = await app_module.get_relation_graph(100, None)

    data = result["data"]
    assert [edge["id"] for edge in data["edges"]] == ["1", "2"]
    assert all(isinstance(edge["subjectUserId"], str) for edge in data["edges"])
    nodes = {node["userId"]: node for node in data["nodes"]}
    assert set(nodes) == {"111", "222", "333", "555"}
    assert nodes["111"]["nickname"] == "小明"
    assert nodes["111"]["degree"] == 2  # noqa: PLR2004
    assert nodes["111"]["linked"] is True
    assert nodes["222"]["role"] == "admin"
    assert nodes["333"]["nickname"] == ""
    assert nodes["333"]["linked"] is True
    assert nodes["333"]["degree"] == 1
    assert nodes["555"]["linked"] is False
    assert nodes["555"]["degree"] == 0
    assert data["meta"] == {"relationTruncated": False, "memberTruncated": False}


@pytest.mark.asyncio
async def test_relation_graph_marks_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relations = [
        _graph_relation(index, 1000 + index, 9000 + index)
        for index in range(service.RELATION_GRAPH_LIMIT + 1)
    ]
    session = _FakeGraphSession(relations=relations)
    monkeypatch.setattr(app_module, "get_session", lambda: _FakeSessionFactory(session))

    result = await app_module.get_relation_graph(100, None)

    data = result["data"]
    assert len(data["edges"]) == service.RELATION_GRAPH_LIMIT
    assert data["meta"]["relationTruncated"] is True
    assert data["meta"]["memberTruncated"] is False
