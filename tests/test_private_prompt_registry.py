# ruff: noqa: ANN001, ANN201, E501, PLR2004
"""私聊提示词注册表：本人作用域、解析顺序、目录/链路/体检、提升与清空、按用户抓取、预览。"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import nonebot
import pytest
import pytest_asyncio
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

_PKG = "src.plugins.yawn_core.yawn_agent"
sections = importlib.import_module(f"{_PKG}.prompt_sections")
registry = importlib.import_module(f"{_PKG}.private_prompt_registry")
capture = importlib.import_module(f"{_PKG}.prompt_capture")
pp = importlib.import_module(f"{_PKG}.private_persona")
ptools = importlib.import_module(f"{_PKG}.private_tools")
persona = importlib.import_module(f"{_PKG}.persona")
Model = importlib.import_module("nonebot_plugin_orm").Model
BotUser = importlib.import_module("src.plugins.yawn_core.data_models.bot_user").BotUser

USER = 8101
OTHER = 8102


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVATE_AGENT_DATA_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("PRIVATE_AGENT_PERSONA_POLICY_PATH", str(tmp_path / "policy.json"))
    sections.set_store_path(tmp_path / "prompts.json")
    pp.reset_policy_cache()
    capture.clear_private_prompts()
    capture.set_private_capture_paused(False)
    yield
    capture.clear_private_prompts()
    capture.set_private_capture_paused(False)
    sections.set_store_path(None)
    pp.reset_policy_cache()


# ── 本人作用域 ────────────────────────────────────────────────────────


def test_user_scope_only_accepts_private_sections():
    with pytest.raises(sections.PromptStoreValidationError):
        sections.set_override("core.role_intro", group_id=None, user_id=USER, mode="append", text="x")
    with pytest.raises(sections.PromptStoreValidationError):
        sections.set_override("private.rules", group_id=1, user_id=USER, mode="append", text="x")
    result = sections.set_override("private.rules", group_id=None, user_id=USER, mode="append", text="多用颜文字")
    assert result["override"]["mode"] == "append"
    assert sections.user_scopes() == {USER: sections.scope_overrides(None, user_id=USER)}


def test_resolution_order_user_then_global():
    sections.set_override("private.rules", group_id=None, mode="append", text="全局追加")
    sections.set_override("private.rules", group_id=None, user_id=USER, mode="append", text="本人追加")
    base = "规则"
    assert sections.apply_section_preview("private.rules", base, user_id=USER).endswith("本人追加")
    assert sections.apply_section_preview("private.rules", base, user_id=OTHER).endswith("全局追加")
    # 未显式传 user_id 时跟随当前私聊回合绑定的用户。
    with sections.use_private_user(USER, pipeline="turn"):
        assert sections.apply_section("private.rules", base).endswith("本人追加")
        assert sections.current_private_pipeline() == "turn"
    assert sections.current_private_user() is None
    # 本人 builtin：这位用户回到代码默认，不继承全局。
    sections.set_override("private.rules", group_id=None, user_id=USER, mode="builtin")
    assert sections.apply_section_preview("private.rules", base, user_id=USER) == base


def test_group_turns_ignore_user_scope():
    sections.set_override("private.rules", group_id=None, user_id=USER, mode="disable")
    with sections.use_private_user(USER):
        assert sections.apply_section_preview("core.role_intro", "群聊角色") == "群聊角色"


def test_private_tool_descriptions_are_overridable_per_user():
    sections.set_override("ptool.notes", group_id=None, user_id=USER, mode="replace", text="记小本本")
    with sections.use_private_user(USER):
        schema = ptools.TOOLS_BY_NAME["notes"].schema()
    assert schema["function"]["description"] == "记小本本"
    assert ptools.TOOLS_BY_NAME["notes"].schema()["function"]["description"] != "记小本本"


def test_draft_scopes_layer_global_and_user():
    with sections.use_draft_scopes(
        {
            "global": {"private.rules": {"mode": "append", "text": "G"}},
            sections.user_scope_key(USER): {"private.role": {"mode": "replace", "text": "U"}},
        }
    ):
        assert sections.apply_section_preview("private.rules", "R", user_id=USER).endswith("G")
        assert sections.apply_section_preview("private.role", "R", user_id=USER) == "U"
        assert sections.apply_section_preview("private.role", "R", user_id=OTHER) == "R"


def test_group_catalog_no_longer_lists_private_sections():
    data = sections.catalog(None)
    assert not any(sections.is_private_section(row["id"]) for row in data["sections"])
    assert data["privateSections"] > 30


# ── 目录、链路、体检 ──────────────────────────────────────────────────


def test_catalog_rows_cover_every_private_section_and_pipeline():
    sections.set_override("private.rules", group_id=None, user_id=USER, mode="append", text="x")
    data = registry.catalog(USER)
    ids = {row["id"] for row in data["sections"]}
    assert "private.persona_scope" in ids
    assert "ptool.my_persona" in ids
    assert all(sections.is_private_section(item) for item in ids)
    rules = next(row for row in data["sections"] if row["id"] == "private.rules")
    assert rules["userOverride"]["mode"] == "append"
    assert rules["effectiveScope"] == "user"
    assert rules["userOverrideCount"] == 1
    assert {pipe["key"] for pipe in data["pipelines"]} == {"turn", "report", "coordinate", "worker", "memory"}
    for pipe in data["pipelines"]:
        for step in pipe["steps"]:
            assert step["sectionId"] in ids, step
    turn = next(pipe for pipe in data["pipelines"] if pipe["key"] == "turn")
    assert turn["tokens"]["always"] > 0
    assert "ptool.my_persona" in turn["toolSections"]
    assert data["summary"]["userOverrides"] == 1


def test_every_private_section_is_in_some_pipeline():
    for section in registry.private_sections():
        assert registry.pipelines_of(section.id), section.id


def test_catalog_without_user_shows_global_only():
    sections.set_override("private.rules", group_id=None, user_id=USER, mode="disable")
    with sections.use_private_user(USER):
        data = registry.catalog(None)
    rules = next(row for row in data["sections"] if row["id"] == "private.rules")
    assert rules["effectiveMode"] == "default"
    assert rules["userOverride"] is None


def test_health_flags_safety_placeholders_and_noops():
    sections.set_override("private.safety", group_id=None, mode="disable", confirm_safety=True)
    default_role = sections.section_default(sections.get_section("private.role"))
    sections.set_override("private.role", group_id=None, user_id=USER, mode="replace", text=default_role)
    sections.set_override(
        "private.attachment_excerpt", group_id=None, user_id=USER, mode="replace", text="只有标题 {title}"
    )
    issues = registry.health(USER)
    codes = {(item["code"], item["sectionId"]) for item in issues}
    assert ("safety_changed", "private.safety") in codes
    assert ("same_as_default", "private.role") in codes
    assert ("missing_placeholder", "private.attachment_excerpt") in codes
    assert issues[0]["level"] == "danger"


def test_promote_and_reset_user():
    sections.set_override("private.rules", group_id=None, user_id=USER, mode="append", text="大家都要")
    sections.set_override("private.role", group_id=None, user_id=USER, mode="append", text="只给他")
    result = registry.promote_user(USER, ["private.rules"])
    assert result["promoted"] == ["private.rules"]
    assert sections.scope_overrides(None)["private.rules"].text == "大家都要"
    assert set(sections.scope_overrides(None, user_id=USER)) == {"private.role"}
    users = registry.users_with_overrides()
    assert users[0]["userId"] == str(USER)
    registry.reset_user(USER)
    assert sections.scope_overrides(None, user_id=USER) == {}
    assert registry.users_with_overrides() == []


def test_user_scope_capacity(monkeypatch):
    monkeypatch.setattr(sections, "MAX_USER_SCOPES", 1)
    sections.set_override("private.rules", group_id=None, user_id=USER, mode="append", text="a")
    with pytest.raises(sections.PromptStoreValidationError):
        sections.set_override("private.rules", group_id=None, user_id=OTHER, mode="append", text="b")


# ── 按用户抓取 ────────────────────────────────────────────────────────


def _record(task: str = "agent_dialogue"):
    return {
        "task": task,
        "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}],
        "tools": [],
        "params": {},
        "outcome": "ok",
        "duration_ms": 1.0,
    }


def test_private_requests_go_to_per_user_ring():
    pp.set_custom(USER, persona.PersonaCard(name="小橘"))
    with sections.use_private_user(USER, pipeline="turn"):
        capture.observe_request(_record())
        capture.observe_request(_record())
    with sections.use_private_user(OTHER, pipeline="memory"):
        capture.observe_request(_record("agent_memory"))
    mine = capture.private_prompts(USER)
    assert len(mine) == 2
    assert mine[0]["pipeline"] == "turn"
    assert mine[0]["round"] == 2
    assert mine[0]["persona"]["source"] == pp.SOURCE_USER
    assert capture.private_prompt_users()[0]["userId"] == str(OTHER)
    assert all(item.get("userId") is None for item in capture.background_prompts())
    assert capture.clear_private_prompts(USER) == 2
    assert capture.private_prompts(USER) == []


def test_private_ring_is_bounded_and_pausable(monkeypatch):
    monkeypatch.setattr(capture, "MAX_PRIVATE_USERS", 2)
    for uid in (1, 2, 3):
        with sections.use_private_user(uid):
            capture.observe_request(_record())
    assert {row["userId"] for row in capture.private_prompt_users()} == {"2", "3"}
    with sections.use_private_user(USER):
        for _ in range(capture.MAX_PRIVATE_SNAPSHOTS_PER_USER + 3):
            capture.observe_request(_record())
    assert len(capture.private_prompts(USER)) == capture.MAX_PRIVATE_SNAPSHOTS_PER_USER
    capture.set_private_capture_paused(True)
    capture.clear_private_prompts()
    with sections.use_private_user(USER):
        capture.observe_request(_record())
    assert capture.private_prompts(USER) == []


# ── 预览 ──────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Model.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(BotUser(user_id=USER, nickname="小明", affinity=0))
        await session.commit()
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_preview_applies_drafts_and_persona_without_saving(db):
    snapshot = await registry.preview(
        db,
        USER,
        message="今天好累",
        global_drafts={"private.rules": {"mode": "append", "text": "【全局草稿】"}},
        user_drafts={"private.role": {"mode": "prepend", "text": "【本人草稿】"}},
        persona_card=persona.PersonaCard(name="草稿猫"),
        tasks=False,
        allow_silence=False,
    )
    system = snapshot["messages"][0]["content"]
    assert "【全局草稿】" in system
    assert "【本人草稿】" in system
    assert "草稿猫" in system
    assert snapshot["messages"][-1]["content"] == "今天好累"
    assert snapshot["persona"]["source"] == "draft"
    assert snapshot["outcome"] == "not_sent"
    assert snapshot["toolNames"]
    spans = {span["sectionId"] for span in snapshot["sections"]}
    assert {"private.role", "private.rules", "private.persona_scope"} <= spans
    # 什么都没写进存储，也没进抓取缓冲。
    assert sections.scope_overrides(None) == {}
    assert capture.private_prompts(USER) == []
