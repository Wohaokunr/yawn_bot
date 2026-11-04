# ruff: noqa: ANN001, E501, PLR2004, F811
"""私聊工具渐进式披露、工具精简、上下文瘦身（2026-11 降本）。"""

from __future__ import annotations

import importlib
import json

import pytest
from test_private_agent import (
    NOW,
    _Bot,
    _message,
    _request,
    _script,
    _tool_ctx,
    bridge,
    maker,  # noqa: F401 - pytest fixture
    pagent,
    ptools,
    tool_names,
)

_PKG = "src.plugins.yawn_core"
catalog = importlib.import_module(f"{_PKG}.yawn_agent.private_tool_catalog")
private_cache = importlib.import_module(f"{_PKG}.yawn_agent.private_cache")
pvoice = importlib.import_module(f"{_PKG}.yawn_agent.private_voice")
pworker = importlib.import_module(f"{_PKG}.yawn_agent.private_worker")
pmemory = importlib.import_module(f"{_PKG}.yawn_agent.private_memory")
sections = importlib.import_module(f"{_PKG}.yawn_agent.prompt_sections")
prompt = importlib.import_module(f"{_PKG}.yawn_agent.private_prompt")

FLAGS = {"is_superuser": False, "computer": True, "tasks": True, "manage": False}


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVATE_AGENT_DATA_DIR", str(tmp_path / "users"))
    private_cache.reset()
    yield
    private_cache.reset()


def _size(tools) -> int:
    return len(json.dumps(tools, ensure_ascii=False))


# ── 第一层：工具表 ─────────────────────────────────────────


def test_progressive_table_is_small_stable_and_complete() -> None:
    table, available = pagent.tool_table(**FLAGS)
    full = ptools.tool_schemas(**FLAGS)
    names = [item["function"]["name"] for item in table]
    # 只有直达工具 + open_tools + use_tool
    assert names[-2:] == ["open_tools", "use_tool"]
    assert set(names[:-2]) == {
        "delegate_task",
        "tasks",
        "run_code",
        "web_search",
        "recall",
    }
    # 不少于原来能用的工具：直达 + use_tool 能调的 == 全部可用
    assert (
        tool_names(table) - catalog.META_TOOLS
        == set(available)
        == {s["function"]["name"] for s in full}
    )
    # 至少省掉七成（实测约八成）
    assert _size(table) < _size(full) * 0.3
    # 逐字稳定：前缀缓存的硬规则
    assert json.dumps(pagent.tool_table(**FLAGS)[0], ensure_ascii=False) == json.dumps(
        table, ensure_ascii=False
    )


def test_every_tool_belongs_to_exactly_one_family() -> None:
    seen: dict[str, str] = {}
    for family in catalog.FAMILIES:
        for name in family.tools:
            assert name not in seen, f"{name} 同时在 {seen[name]} 和 {family.key}"
            seen[name] = family.key
    assert set(seen) == set(ptools.TOOLS_BY_NAME)


def test_index_lists_families_with_available_tools_only() -> None:
    table, _ = pagent.tool_table(**FLAGS)
    opener = next(item for item in table if item["function"]["name"] == "open_tools")
    text = opener["function"]["description"]
    assert "status（他的状态）" in text and "my_status" in text
    # 群管只给群主/管理员；超管工具不给普通人；电脑在时备用小工具箱不出现
    assert (
        "group_manage" not in text
        and "bot_status" not in text
        and "toolbox" not in text
    )
    enum = opener["function"]["parameters"]["properties"]["family"]["enum"]
    assert enum == [family.key for family in catalog.FAMILIES]


def test_toolbox_only_when_computer_is_off() -> None:
    on = {t.name for t in ptools.available_tools(computer=True)}
    off = {t.name for t in ptools.available_tools(computer=False)}
    assert "toolbox" not in on and "run_code" in on
    assert "toolbox" in off and "run_code" not in off


def test_full_mode_and_direct_tools_are_configurable(monkeypatch) -> None:
    config = bridge.private_agent_config
    monkeypatch.setattr(config, "private_agent_tool_disclosure", "full")
    table, available = pagent.tool_table(**FLAGS)
    assert {t["function"]["name"] for t in table} == set(available)
    monkeypatch.setattr(config, "private_agent_tool_disclosure", "progressive")
    monkeypatch.setattr(config, "private_agent_direct_tools", "")
    strict, _ = pagent.tool_table(**FLAGS)
    assert [t["function"]["name"] for t in strict] == ["open_tools", "use_tool"]


def test_catalog_texts_are_registered_sections() -> None:
    for name in ("open_tools", "use_tool", "family.status", "family.computer"):
        assert sections.get_section(f"ptool.{name}") is not None


# ── 第二层：展开 ───────────────────────────────────────────


def test_params_text_is_compact_and_marks_required() -> None:
    notes = ptools.TOOLS_BY_NAME["notes"]
    text = catalog.params_text(notes.properties, notes.required)
    assert text.startswith("action*: add|list|search|delete|pin|unpin")
    assert "content: 文本≤300" in text
    assert len(text) < len(json.dumps(notes.properties, ensure_ascii=False)) / 2


@pytest.mark.asyncio
async def test_open_family_tool_and_repeat(maker) -> None:
    async with maker() as session:
        ctx = await _tool_ctx(session)
        ctx.available = frozenset(t.name for t in ptools.available_tools(**FLAGS))
        first = await ptools.execute_private_tool(
            "open_tools", {"family": "assistant"}, ctx
        )
        tools = [item["tool"] for item in first["result"]["tools"]]
        assert tools == ["notes", "todos", "mood", "chat_prefs"]
        assert all(item["args"] for item in first["result"]["tools"])
        again = await ptools.execute_private_tool(
            "open_tools", {"family": "assistant"}, ctx
        )
        assert "tools" not in again["result"]
        single = await ptools.execute_private_tool(
            "open_tools", {"tool": "my_overview"}, ctx
        )
        assert single["result"]["tools"][0]["tool"] == "my_status"
        as_family = await ptools.execute_private_tool(
            "open_tools", {"tool": "memory"}, ctx
        )
        assert as_family["result"]["family"] == "memory"
        hidden = await ptools.execute_private_tool(
            "open_tools", {"tool": "group_manage"}, ctx
        )
        assert hidden["ok"] is False
        bad = await ptools.execute_private_tool("open_tools", {"family": "nope"}, ctx)
        assert bad["error_code"] == "invalid_args"
    assert private_cache.tool_usage()["open"]["assistant"] == 2


# ── 第三层：调用 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_use_tool_variants_and_failures(maker) -> None:
    async with maker() as session:
        ctx = await _tool_ctx(session)
        ctx.available = frozenset(t.name for t in ptools.available_tools(**FLAGS))
        nested = await ptools.execute_private_tool(
            "use_tool",
            {"tool": "notes", "args": {"action": "add", "content": "买牛奶"}},
            ctx,
        )
        as_string = await ptools.execute_private_tool(
            "use_tool", {"tool": "notes", "args": json.dumps({"action": "list"})}, ctx
        )
        flat = await ptools.execute_private_tool(
            "use_tool", {"tool": "todos", "action": "list"}, ctx
        )
        assert nested["ok"] and as_string["ok"] and flat["ok"]
        assert "notes.add" in ctx.succeeded
        wrong = await ptools.execute_private_tool(
            "use_tool", {"tool": "todos", "args": {"oops": 1}}, ctx
        )
        assert (
            wrong["error_code"] == "invalid_args" and wrong["usage"]["tool"] == "todos"
        )
        # 签名只附一次（已经给过就不再重复塞）
        again = await ptools.execute_private_tool(
            "use_tool", {"tool": "todos", "args": {"oops": 1}}, ctx
        )
        assert "usage" not in again
        missing = await ptools.execute_private_tool("use_tool", {"args": {}}, ctx)
        assert missing["error_code"] == "invalid_args"
        nested_meta = await ptools.execute_private_tool(
            "use_tool", {"tool": "use_tool"}, ctx
        )
        assert nested_meta["ok"] is False
        blocked = await ptools.execute_private_tool(
            "use_tool", {"tool": "group_manage", "args": {"action": "groups"}}, ctx
        )
        assert blocked["error_code"] == "not_allowed"
        direct_hidden = await ptools.execute_private_tool("bot_status", {}, ctx)
        assert direct_hidden["error_code"] == "not_allowed"
        ctx.allowed_tools = frozenset({"tasks"})
        limited = await ptools.execute_private_tool(
            "use_tool", {"tool": "notes", "args": {"action": "list"}}, ctx
        )
        assert limited["error_code"] == "not_allowed"
    assert private_cache.tool_usage()["fixed"]["todos"] == 1


@pytest.mark.asyncio
async def test_legacy_names_still_work(maker) -> None:
    async with maker() as session:
        ctx = await _tool_ctx(session)
        overview = await ptools.execute_private_tool("my_overview", {}, ctx)
        groups = await ptools.execute_private_tool("list_my_groups", {}, ctx)
        remembered = await ptools.execute_private_tool("what_you_remember", {}, ctx)
        detail = await ptools.execute_private_tool(
            "affinity_detail", {"view": "progress"}, ctx
        )
        guide = await ptools.execute_private_tool(
            "webui_onboarding", {"action": "cancel"}, ctx
        )
    assert "relationship" in overview["result"]
    assert groups["result"]["groups"]
    assert "told_you_in_private" in remembered["result"]
    assert "level" in detail["result"]
    assert guide["ok"] or guide["error_code"] == "permission"
    assert "my_status.overview" in ctx.succeeded and "my_status.groups" in ctx.succeeded


@pytest.mark.asyncio
async def test_my_status_board_needs_group_when_ambiguous(maker) -> None:
    async with maker() as session:
        ctx = await _tool_ctx(session)
        missing = await ptools.execute_private_tool("my_status", {"view": "board"}, ctx)
        board = await ptools.execute_private_tool(
            "my_status", {"view": "board", "group": "开着的群"}, ctx
        )
    assert missing["error_code"] == "invalid_args"
    assert board["ok"] and "today_order" in board["result"]
    assert "my_status.board" in ctx.succeeded


def test_claim_aliases_follow_merged_names() -> None:
    assert pagent._CLAIM_ALIASES["my_status.checkin"] == ("get_my_checkin",)
    assert "checkin_status" not in pagent._CLAIM_ALIASES


# ── 主回合：经 use_tool 办事、同轮去重 ─────────────────────


@pytest.mark.asyncio
async def test_turn_uses_family_tool_and_dedupes_repeat_reads(
    maker, monkeypatch
) -> None:
    seen: list[dict] = []
    _script(
        monkeypatch,
        [
            _message(calls=[("open_tools", {"family": "status"})]),
            _message(
                calls=[("use_tool", {"tool": "my_status", "args": {"view": "checkin"}})]
            ),
            _message(
                calls=[("use_tool", {"tool": "my_status", "args": {"view": "checkin"}})]
            ),
            _message(calls=[("use_tool", {"tool": "checkin", "args": {}})]),
            _message(
                calls=[("use_tool", {"tool": "my_status", "args": {"view": "checkin"}})]
            ),
            _message("【节奏:整段】签好啦"),
        ],
        seen,
    )
    monkeypatch.setattr(pagent, "now_beijing", lambda: NOW)

    async with maker() as session:
        bot = _Bot()
        text = await pagent.handle_private_turn(_request(session, bot, text="帮我签到"))
    assert text == "签好啦"
    tool_results = [
        json.loads(m["content"])
        for m in seen[-1]["messages"]
        if m.get("role") == "tool"
    ]
    assert tool_results[0]["result"]["family"] == "status"
    assert tool_results[2].get("same_as_before") is True  # 同样的查询不再执行
    assert tool_results[4].get("same_as_before") is None  # 签到之后再查：照常执行
    # 工具表每轮一样（前缀缓存）
    assert all(item["tools"] == seen[0]["tools"] for item in seen)
    usage = private_cache.tool_usage()
    assert usage["use"]["my_status"] == 3 and usage["use"]["checkin"] == 1


def test_tool_content_is_compact_and_bounded() -> None:
    payload = {
        "ok": True,
        "result": {"a": None, "b": [], "c": {}, "d": "", "e": 0, "f": False, "g": "x"},
    }
    assert (
        pagent.tool_content(payload) == '{"ok":true,"result":{"e":0,"f":false,"g":"x"}}'
    )
    long = pagent.tool_content({"ok": True, "result": "字" * 5000}, 2000)
    assert len(long) < 2100 and "省略" in long


def test_handoff_findings_show_inner_tool_name() -> None:
    messages = [
        {"role": "user", "content": "查一下"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "c1",
                    "function": {
                        "name": "use_tool",
                        "arguments": json.dumps(
                            {"tool": "web_fetch", "args": {"target": "w1"}}
                        ),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "正文"},
    ]
    assert pvoice.handoff_findings(messages).startswith("[web_fetch]")


# ── 后台与记忆 ─────────────────────────────────────────────


def test_infer_toolsets_prefers_computer_over_toolbox() -> None:
    assert pvoice.infer_toolsets(computer=True) == ["computer", "web"]
    assert pvoice.infer_toolsets(computer=False) == ["toolbox", "web"]


def test_worker_swaps_toolbox_for_computer(monkeypatch) -> None:
    computer = importlib.import_module(f"{_PKG}.yawn_agent.private_computer")
    monkeypatch.setattr(computer, "computer_ready", lambda: True)
    assert pworker._effective_toolsets(["toolbox", "web"]) == ["web", "computer"]
    assert pworker._effective_toolsets(["computer", "toolbox"]) == ["computer"]
    monkeypatch.setattr(computer, "computer_ready", lambda: False)
    assert pworker._effective_toolsets(["toolbox", "web"]) == ["toolbox", "web"]


def test_worker_tool_results_are_bounded() -> None:
    text = pworker._worker_tool_content({"ok": True, "result": "x" * 50000})
    assert len(text) < pworker.WORKER_TOOL_RESULT_CHARS + 200


def test_worker_compaction_goes_to_half_budget() -> None:
    history = [{"role": "tool", "content": "y" * 1000} for _ in range(40)]
    history += [
        {"role": "user", "content": "z"} for _ in range(pworker.KEEP_RECENT_MESSAGES)
    ]
    pworker.compact_history(history, budget=20000)
    assert pworker._context_chars(history) <= 10000 + 1000


def test_memory_window_and_prompts_mention_code_for_calc() -> None:
    assert pmemory.CONSOLIDATE_WINDOW == 20 > pmemory.CONSOLIDATE_EVERY
    assert "run_code" in prompt.PRIVATE_TOOLKITS and "别心算" in prompt.PRIVATE_TOOLKITS
    # 旧说法“在群里替他发言、管理群做不到”和现有的转告/群管工具矛盾，已删掉
    assert "在群里替他发言、管理群" not in prompt.PRIVATE_RULES


def test_call_key_skips_writes_and_random_tools() -> None:
    assert pagent._call_key("use_tool", {"tool": "my_status", "args": {"view": "checkin"}}) is not None
    assert pagent._call_key("web_search", {"query": "天气"}) is not None
    assert pagent._call_key("use_tool", {"tool": "notes", "args": {"action": "list"}}) is None
    assert pagent._call_key("use_tool", {"tool": "toolbox", "args": {"op": "dice"}}) is None
    assert pagent._call_key("open_tools", {"family": "web"}) is None
    # 旧名和新名算同一个调用
    assert pagent._call_key("my_overview", {}) == pagent._call_key("my_status", {"view": "overview"})


def test_worker_read_keys() -> None:
    assert pworker._read_key("web_fetch", {"target": "w1"}) is not None
    assert pworker._read_key("computer_files", {"action": "list"}) is not None
    assert pworker._read_key("computer_files", {"action": "delete", "path": "a"}) is None
    assert pworker._read_key("run_code", {"code": "1"}) is None
    assert pworker._read_key("toolbox", {"op": "coin"}) is None
