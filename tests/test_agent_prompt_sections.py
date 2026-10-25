# 提示词分段：WebUI 覆盖（删除/替换/追加/前置/本群/草稿）、写入校验与完整提示词抓取。
# ruff: noqa: PLR2004, TC003
from __future__ import annotations

import contextlib
import importlib
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nonebot
import pytest

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if (
    nonebot.get_plugin("yawn_core") is None
    and nonebot.get_plugin("src.plugins.yawn_core") is None
):
    nonebot.load_from_toml("pyproject.toml")

sections = importlib.import_module("src.plugins.yawn_core.yawn_agent.prompt_sections")
prompt = importlib.import_module("src.plugins.yawn_core.yawn_agent.prompt")
capture = importlib.import_module("src.plugins.yawn_core.yawn_agent.prompt_capture")
tracing = importlib.import_module("src.plugins.yawn_core.yawn_agent.execution_trace")
tools_module = importlib.import_module("src.plugins.yawn_core.yawn_agent.tools")
turn_budget = importlib.import_module("src.plugins.yawn_core.yawn_agent.turn_budget")
llm = importlib.import_module("src.plugins.yawn_core.llm")
prompt_routes = importlib.import_module(
    "src.plugins.yawn_core.webui.agent_prompt_routes"
)
route_models = importlib.import_module("src.plugins.yawn_core.webui.route_models")

GROUP = 424242
PERSONA = {"name": "Yawn", "description": "一只犯困的猫"}


@pytest.fixture(autouse=True)
def isolated_store(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "prompt_overrides.json"
    sections.set_store_path(path)
    try:
        yield path
    finally:
        sections.set_store_path(None)


def _build(group_id: int | None = GROUP, **extra: Any) -> Any:
    return prompt.build_prompt(
        persona=PERSONA,
        tools=[],
        context={"group_id": group_id, "group_name": "猫窝"},
        user_prompt="喵？",
        **extra,
    )


def _all_text(messages: list[dict[str, Any]]) -> str:
    return "\n".join(capture.message_text(item) for item in messages)


# ── 默认输出与分段注册 ─────────────────────────────────────────────────


def test_default_output_is_unchanged_without_overrides() -> None:
    built = _build()
    legacy, fingerprint = prompt.build_messages(
        persona=PERSONA,
        tools=[],
        context={"group_id": GROUP, "group_name": "猫窝"},
        user_prompt="喵？",
    )
    assert built.messages == legacy
    assert built.fingerprint == fingerprint
    assert "".join(
        text for _id, text in prompt._POLICY_SECTIONS
    ) == prompt._SYSTEM_POLICY
    assert prompt.POLICY_HONESTY in _all_text(built.messages)


def test_every_section_has_metadata_and_resolvable_default() -> None:
    ids = [section.id for section in sections.all_sections()]
    assert len(ids) == len(set(ids))
    for section in sections.all_sections():
        assert section.title and section.category in {
            item[0] for item in sections.CATEGORIES
        }
        if section.default_ref and section.kind != sections.KIND_DATA:
            assert sections.section_default(section) is not None, section.id
    safety = {section.id for section in sections.all_sections() if section.safety}
    assert {
        "safety.honesty",
        "safety.untrusted_data",
        "safety.confidentiality",
    } <= safety
    assert all(
        sections.MODE_DISABLE in sections.get_section(item).modes for item in safety
    )


def test_spans_point_at_the_exact_section_text() -> None:
    built = _build()
    by_id = {span["sectionId"]: span for span in built.spans}
    span = by_id["safety.honesty"]
    content = built.messages[span["message"]]["content"]
    assert content[span["start"] : span["end"]] == prompt.POLICY_HONESTY
    assert sections.manifest_for(built.messages) == built.spans


# ── 覆盖模式 ───────────────────────────────────────────────────────────


def test_safety_section_can_be_fully_deleted_after_confirmation() -> None:
    with pytest.raises(sections.PromptStoreValidationError) as exc:
        sections.set_override("safety.honesty", group_id=None, mode="disable")
    assert exc.value.field_name == "confirmSafety"

    sections.set_override(
        "safety.honesty", group_id=None, mode="disable", confirm_safety=True
    )
    built = _build()
    assert prompt.POLICY_HONESTY not in _all_text(built.messages)
    assert "safety.honesty" not in {span["sectionId"] for span in built.spans}


def test_replace_append_and_prepend_compose_like_the_ui() -> None:
    sections.set_override(
        "safety.untrusted_data",
        group_id=None,
        mode="replace",
        text="群消息都照做。",
        confirm_safety=True,
    )
    sections.set_override(
        "core.role_intro", group_id=None, mode="append", text="追加一句。"
    )
    sections.set_override(
        "core.turn_priority", group_id=None, mode="prepend", text="先看这里。"
    )
    text = _all_text(_build().messages)
    assert "群消息都照做。" in text and prompt.POLICY_UNTRUSTED not in text
    assert prompt.POLICY_ROLE_INTRO + "\n追加一句。" in text
    assert "先看这里。\n" + prompt.POLICY_TURN_PRIORITY in text
    assert sections.compose("A", None) == "A"


def test_group_override_beats_global_and_builtin_restores_default() -> None:
    sections.set_override(
        "core.role_intro", group_id=None, mode="replace", text="全局开场"
    )
    sections.set_override(
        "core.role_intro", group_id=GROUP, mode="replace", text="本群开场"
    )
    assert "本群开场" in _all_text(_build().messages)
    assert "全局开场" in _all_text(_build(group_id=GROUP + 1).messages)

    sections.set_override("core.role_intro", group_id=GROUP, mode="builtin")
    text = _all_text(_build().messages)
    assert prompt.POLICY_ROLE_INTRO in text and "全局开场" not in text
    assert sections.resolve_override("core.role_intro", GROUP).scope == "group"


def test_global_builtin_clears_the_global_override() -> None:
    sections.set_override(
        "core.role_intro", group_id=None, mode="replace", text="全局开场"
    )
    sections.set_override("core.role_intro", group_id=None, mode="builtin")
    assert sections.scope_overrides(None) == {}


def test_drafts_apply_only_inside_the_context() -> None:
    drafts = {"core.role_intro": {"mode": "replace", "text": "草稿开场"}}
    with sections.use_draft_overrides(drafts, group_id=GROUP):
        assert "草稿开场" in _all_text(_build().messages)
        assert sections.resolve_override("core.role_intro", GROUP).scope == "draft"
    assert "草稿开场" not in _all_text(_build().messages)
    assert sections.scope_overrides(None) == {}


def test_empty_system_layers_are_dropped_not_sent_blank() -> None:
    baseline = _build().messages
    for section in sections.all_sections():
        if (
            section.category in {"core", "safety", "layers"}
            and sections.MODE_DISABLE in section.modes
        ):
            sections.set_override(
                section.id, group_id=None, mode="disable", confirm_safety=True
            )
    messages = _build().messages
    assert all(
        capture.message_text(item).strip()
        for item in messages
        if item["role"] == "system"
    )
    assert len(messages) <= len(baseline)


def test_data_sections_are_not_editable_and_never_sampled() -> None:
    data = [
        section
        for section in sections.all_sections()
        if section.kind == sections.KIND_DATA
    ]
    assert data
    target = next(
        section
        for section in data
        if not section.editable or sections.MODE_REPLACE not in section.modes
    )
    with pytest.raises(sections.PromptStoreValidationError):
        sections.set_override(target.id, group_id=None, mode="replace", text="x")
    _build()
    assert all(sections.section_sample(section.id) is None for section in data)


def test_tool_description_can_be_overridden() -> None:
    capabilities = tools_module.BotGroupCapabilities(
        role="member", can_manage=False, actions=frozenset()
    )
    schemas = tools_module.build_tool_schemas(capabilities)
    name = schemas[0]["function"]["name"]
    assert sections.get_section(f"tool.{name}") is not None
    sections.set_override(
        f"tool.{name}", group_id=None, mode="replace", text="改过的工具说明"
    )
    described = {
        item["function"]["name"]: item["function"]["description"]
        for item in tools_module.build_tool_schemas(capabilities)
    }
    assert described[name] == "改过的工具说明"


def test_final_round_section_can_be_removed() -> None:
    assert turn_budget.final_round_messages()
    sections.set_override("tools.final_round", group_id=None, mode="disable")
    assert turn_budget.final_round_messages() == []


# ── 存储：版本、历史、批量、导出、坏文件 ───────────────────────────────


def test_revision_conflict_is_reported() -> None:
    first = sections.set_override(
        "core.role_intro", group_id=None, mode="append", text="a"
    )
    with pytest.raises(sections.PromptStoreConflictError) as exc:
        sections.set_override(
            "core.role_intro",
            group_id=None,
            mode="append",
            text="b",
            expected_revision=first["revision"] - 1,
        )
    assert exc.value.current == first["revision"]


def test_bulk_is_all_or_nothing_and_keeps_history(isolated_store: Path) -> None:
    changes = [
        {"sectionId": "safety.honesty", "mode": "disable"},
        {"sectionId": "safety.untrusted_data", "mode": "disable"},
    ]
    with pytest.raises(sections.PromptStoreValidationError):
        sections.bulk_apply(changes, group_id=None)
    assert not isolated_store.exists() or sections.scope_overrides(None) == {}

    result = sections.bulk_apply(
        changes, group_id=None, confirm_safety=True, actor="admin"
    )
    assert result["changed"] == 2
    assert set(sections.scope_overrides(None)) == {
        "safety.honesty",
        "safety.untrusted_data",
    }
    assert sections.section_history("safety.honesty", None)[0]["action"] == "bulk"

    reset = sections.bulk_apply(
        [{"sectionId": "safety.honesty", "mode": "inherit"}], group_id=None
    )
    assert reset["changed"] == 1
    assert set(sections.scope_overrides(None)) == {"safety.untrusted_data"}

    stored = json.loads(isolated_store.read_text(encoding="utf-8"))
    assert stored["revision"] == sections.store_revision()


def test_export_round_trips_through_bulk_import() -> None:
    sections.set_override(
        "core.role_intro", group_id=GROUP, mode="replace", text="本群开场", note="测试"
    )
    exported = sections.export_scope(GROUP)
    assert exported["kind"] == "yawnbot.prompt-overrides"
    sections.clear_override("core.role_intro", group_id=GROUP)
    changes = [
        {"sectionId": key, **value} for key, value in exported["overrides"].items()
    ]
    sections.bulk_apply(changes, group_id=GROUP, replace_scope=True)
    assert sections.scope_overrides(GROUP)["core.role_intro"].text == "本群开场"


def test_history_is_bounded() -> None:
    for index in range(sections.MAX_HISTORY_PER_SECTION + 5):
        sections.set_override(
            "core.role_intro", group_id=None, mode="append", text=f"v{index}"
        )
    assert (
        len(sections.section_history("core.role_intro", None))
        == sections.MAX_HISTORY_PER_SECTION
    )


def test_corrupt_store_falls_back_to_defaults(isolated_store: Path) -> None:
    isolated_store.write_text("{not json", encoding="utf-8")
    sections.set_store_path(isolated_store)
    assert prompt.POLICY_HONESTY in _all_text(_build().messages)
    assert sections.store_snapshot()["loadError"]


def test_catalog_reports_effective_state() -> None:
    sections.set_override(
        "safety.honesty", group_id=GROUP, mode="disable", confirm_safety=True
    )
    _build()
    payload = sections.catalog(GROUP)
    rows = {item["id"]: item for item in payload["sections"]}
    assert rows["safety.honesty"]["effectiveMode"] == "disable"
    assert rows["safety.honesty"]["effectiveScope"] == "group"
    assert rows["safety.honesty"]["defaultText"] == prompt.POLICY_HONESTY
    assert payload["summary"]["safetyDisabled"] == 1
    assert sections.catalog(None)["summary"]["safetyDisabled"] == 0


# ── 完整提示词抓取 ─────────────────────────────────────────────────────


@contextlib.contextmanager
def _bound_trace(message_id: int) -> Iterator[Any]:
    trace = tracing.begin_execution_trace(
        GROUP,
        mode="dialogue",
        source="runtime",
        trigger_source="mention",
        actor_user_id=1,
        message_id=message_id,
    )
    token = tracing.bind_execution_trace(trace)
    try:
        yield trace
    finally:
        tracing.reset_execution_trace(token)
        tracing.finish_execution_trace(trace, outcome="completed")


def _record(messages: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "kind": "tools",
        "task": "agent_dialogue",
        "provider": "default",
        "profile": "default",
        "model": "test-model",
        "messages": messages,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "send_message",
                    "description": "发消息",
                    "parameters": {},
                },
            }
        ],
        "params": {"temperature": 0.5},
        "timeout": 30,
        "response": SimpleNamespace(
            id="resp",
            model="test-model",
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content="喵", tool_calls=None),
                )
            ],
            usage=None,
        ),
        "outcome": "success",
        "duration_ms": 12.0,
        **extra,
    }


def test_observer_attaches_the_exact_request_to_the_trace() -> None:
    tracing.clear_execution_traces(GROUP)
    with _bound_trace(1) as trace:
        built = _build()
        capture.observe_request(_record(built.messages))
        capture.observe_request(
            _record([*built.messages, {"role": "assistant", "content": "工具调用"}])
        )

    payload = capture.trace_prompts(trace)
    assert [item["round"] for item in payload["prompts"]] == [1, 2]
    first = payload["prompts"][0]
    assert first["messages"] == built.messages
    assert first["toolNames"] == ["send_message"]
    assert first["params"] == {"temperature": 0.5}
    assert first["response"]["content"] == "喵"
    assert "safety.honesty" in {span["sectionId"] for span in first["sections"]}
    honesty = next(
        span for span in first["sections"] if span["sectionId"] == "safety.honesty"
    )
    assert honesty["safety"] is True
    summary = tracing.recent_execution_trace_summaries(GROUP)[0]
    assert summary["promptCount"] == 2
    assert tracing.execution_trace_object(GROUP, trace.trace_id) is trace


def test_capture_redacts_urls_paths_and_media() -> None:
    snapshot = capture.build_snapshot(
        _record(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "看 https://example.com/secret?token=1"
                            " 和 /home/alice/key.pem",
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64," + "QUFB" * 64
                            },
                        },
                    ],
                },
            ]
        ),
        round_index=1,
    )
    dumped = json.dumps(snapshot, ensure_ascii=False)
    assert "example.com/secret" not in dumped and "token=1" not in dumped
    assert "/home/alice" not in dumped
    assert "QUFBQUFB" not in dumped
    assert "[本机路径]" in dumped and "内嵌媒体" in dumped
    assert snapshot["redactions"] >= 3


def test_capture_truncates_huge_requests() -> None:
    huge = "猫" * (capture.MAX_SNAPSHOT_CHARS + 5000)
    snapshot = capture.build_snapshot(
        _record([{"role": "user", "content": huge}]), round_index=1
    )
    assert snapshot["truncated"] is True
    assert snapshot["omittedChars"] > 0
    assert (
        len(json.dumps(snapshot["messages"], ensure_ascii=False))
        <= capture.MAX_SNAPSHOT_CHARS + 1000
    )


def test_capture_is_bounded_per_trace_and_can_be_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _bound_trace(2) as trace:
        for _ in range(capture.MAX_SNAPSHOTS_PER_TRACE + 3):
            capture.observe_request(_record([{"role": "user", "content": "喵"}]))
    assert len(trace.prompts) == capture.MAX_SNAPSHOTS_PER_TRACE
    assert trace.prompts_dropped == 3

    monkeypatch.setenv("AGENT_TRACE_PROMPT_CAPTURE", "off")
    assert capture.capture_enabled() is False
    with _bound_trace(3) as other:
        capture.observe_request(_record([{"role": "user", "content": "喵"}]))
    assert other.prompts == []


def test_background_agent_requests_go_to_a_small_buffer() -> None:
    assert tracing.current_execution_trace() is None
    capture.clear_background_prompts()
    capture.observe_request(
        _record([{"role": "user", "content": "整理记忆"}], task="agent_memory")
    )
    capture.observe_request(
        _record([{"role": "user", "content": "别的插件"}], task="core_chat")
    )
    prompts = capture.background_prompts()
    assert [item["task"] for item in prompts] == ["agent_memory"]
    assert capture.clear_background_prompts() == 1


def test_llm_observers_are_isolated_from_failures() -> None:
    seen: list[dict[str, Any]] = []

    def broken(_record: dict[str, Any]) -> None:
        raise RuntimeError("boom")

    llm.add_request_observer(broken)
    llm.add_request_observer(seen.append)
    try:
        llm._notify_request_observers({"task": "agent_dialogue"})
    finally:
        llm.remove_request_observer(broken)
        llm.remove_request_observer(seen.append)
    assert seen == [{"task": "agent_dialogue"}]


# ── 接口 ───────────────────────────────────────────────────────────────


@pytest.fixture
def no_group_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ok(_group_id: int | None) -> None:
        return None

    async def _quiet(_group_id: int | None) -> None:
        return None

    monkeypatch.setattr(prompt_routes, "_check_group", _ok)
    monkeypatch.setattr(prompt_routes, "_notify", _quiet)


SESSION = SimpleNamespace(actor_fingerprint="admin-1")


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_group_checks")
async def test_routes_put_reject_conflict_and_delete() -> None:
    from fastapi import HTTPException

    body = route_models.PromptOverrideBody.model_validate(
        {"scope": "global", "mode": "disable", "text": ""}
    )
    with pytest.raises(HTTPException) as rejected:
        await prompt_routes.put_prompt_override("safety.honesty", body, SESSION)
    assert rejected.value.status_code == 422

    ok_body = route_models.PromptOverrideBody.model_validate(
        {
            "scope": "global",
            "mode": "disable",
            "confirmSafety": True,
            "expectedRevision": 0,
        }
    )
    saved = await prompt_routes.put_prompt_override("safety.honesty", ok_body, SESSION)
    assert saved["data"]["revision"] == 1

    with pytest.raises(HTTPException) as conflict:
        await prompt_routes.put_prompt_override("safety.honesty", ok_body, SESSION)
    assert conflict.value.status_code == 409

    with pytest.raises(HTTPException) as unknown:
        await prompt_routes.put_prompt_override("nope.section", ok_body, SESSION)
    assert unknown.value.status_code == 404

    catalog = await prompt_routes.get_prompt_catalog(SESSION, None)
    assert catalog["data"]["summary"]["safetyDisabled"] == 1
    removed = await prompt_routes.delete_prompt_override(
        "safety.honesty", SESSION, "global", None, 1
    )
    assert removed["data"]["changed"] is True
    history = await prompt_routes.get_prompt_history(
        "safety.honesty", SESSION, "global", None
    )
    assert [item["action"] for item in history["data"]] == ["clear", "set"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_group_checks")
async def test_route_bulk_export_and_status() -> None:
    bulk = route_models.PromptBulkBody.model_validate(
        {
            "scope": "group",
            "groupId": GROUP,
            "confirmSafety": True,
            "changes": [{"sectionId": "safety.honesty", "mode": "disable"}],
        }
    )
    result = await prompt_routes.bulk_prompt_overrides(bulk, SESSION)
    assert result["data"]["changed"] == 1
    exported = await prompt_routes.export_prompt_overrides(SESSION, "group", GROUP)
    assert list(exported["data"]["overrides"]) == ["safety.honesty"]
    status = await prompt_routes.get_prompt_status(SESSION)
    assert status["data"]["capture"]["perTrace"] == capture.MAX_SNAPSHOTS_PER_TRACE
    assert status["data"]["scopes"] == {str(GROUP): 1}


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_group_checks")
async def test_route_trace_prompts() -> None:
    from fastapi import HTTPException

    with _bound_trace(4) as trace:
        capture.observe_request(_record([{"role": "user", "content": "喵"}]))
    payload = await prompt_routes.get_trace_prompts(GROUP, trace.trace_id, SESSION)
    assert payload["data"]["prompts"][0]["messages"] == [
        {"role": "user", "content": "喵"}
    ]
    with pytest.raises(HTTPException) as missing:
        await prompt_routes.get_trace_prompts(GROUP, "missing", SESSION)
    assert missing.value.status_code == 404
