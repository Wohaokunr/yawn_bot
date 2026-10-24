# ruff: noqa: E501, PLR2004, ARG001, ARG005, TRY003
"""Agent 工具使用改进：进度提示、回合预算、沙箱输出整理、工具路由与隐式触发、推理等级。"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nonebot
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()
if nonebot.get_plugin("yawn_core") is None:
    nonebot.load_from_toml("pyproject.toml")

_PKG = "src.plugins.yawn_core.yawn_agent"
llm = importlib.import_module("src.plugins.yawn_core.llm")
progress = importlib.import_module(f"{_PKG}.progress_notice")
budget = importlib.import_module(f"{_PKG}.turn_budget")
output = importlib.import_module(f"{_PKG}.sandbox_output")
planner = importlib.import_module(f"{_PKG}.tool_planner")
preload = importlib.import_module(f"{_PKG}.tool_preload")
implicit = importlib.import_module(f"{_PKG}.implicit_trigger")
prompt = importlib.import_module(f"{_PKG}.prompt")
tools = importlib.import_module(f"{_PKG}.tools")
parser = importlib.import_module(f"{_PKG}.message_parser")


@pytest.fixture(autouse=True)
def _reset() -> None:
    planner.reset_cache_for_tests()
    implicit.reset_for_tests()


# ---- 进度提示 ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("我算一下", "我算一下"),
        ("  我  查查  ", "我 查查"),
        ("你要哪种图？", None),
        ("```python\nprint(1)```", None),
        ("看 https://x.example", None),
        ("- 第一步", None),
        ("很" * 41, None),
        ("", None),
    ],
)
def test_clean_preamble(text: str, expected: str | None) -> None:
    assert progress.clean_preamble(text) == expected


def test_progress_phases_follow_tools() -> None:
    state = progress.TurnProgress()
    state.enter_tool("run_shell", family="sandbox")
    assert state.phase == "sandbox" and state.running_slow_tool
    state.leave_tool()
    assert state.phase == "thinking" and not state.running_slow_tool
    state.enter_tool("search_group_history", family="history")
    assert state.phase == "lookup"
    state.enter_tool("mute_member", family="moderation")
    assert state.phase == "admin"


def test_progress_text_prefers_preamble_and_rotates() -> None:
    state = progress.TurnProgress(self_name="小咪")
    state.preamble = "我画一下"
    assert progress.progress_text(state, 1) == "我画一下"
    state.preamble = None
    state.phase = "thinking"
    lines = {progress.progress_text(state, 77) for _ in range(3)}
    assert len(lines) == 3
    assert all("{self}" not in line for line in lines)
    assert any("小咪" in line for line in lines)


async def _wait_notice(bot: Any, *, slow: bool, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setenv("AGENT_AI_WAIT_NOTICE_DELAY", "0.02")
    monkeypatch.setenv("AGENT_AI_WAIT_NOTICE_LONG_DELAY", "0.06")
    sent: list[str] = []

    async def send(_bot: Any, _group: int, text: str, *_args: Any, **_kwargs: Any) -> None:
        sent.append(text)

    state = progress.begin_progress()
    if slow:
        state.enter_tool("run_python", family="sandbox")
    progress.start_wait_notice(bot, 1, None, 555, send_unless_expired=send, debug=lambda _m: None, debug_exception=lambda _m: None)
    await asyncio.sleep(0.15)
    progress.cancel_wait_notice()
    return sent


class _ReactBot:
    self_id = "100"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_api(self, action: str, **kwargs: Any) -> None:
        self.calls.append((action, kwargs))


@pytest.mark.asyncio
async def test_wait_notice_reacts_instead_of_talking(monkeypatch: pytest.MonkeyPatch) -> None:
    caps = importlib.import_module(f"{_PKG}.capabilities")
    monkeypatch.setattr(caps, "local_group_capabilities", lambda _bot: SimpleNamespace(has=lambda name: True))
    bot = _ReactBot()
    sent = await _wait_notice(bot, slow=False, monkeypatch=monkeypatch)
    assert sent == []
    assert bot.calls == [("set_msg_emoji_like", {"message_id": 555, "emoji_id": progress.WAIT_NOTICE_EMOJI_ID})]


@pytest.mark.asyncio
async def test_wait_notice_adds_text_only_for_slow_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    caps = importlib.import_module(f"{_PKG}.capabilities")
    monkeypatch.setattr(caps, "local_group_capabilities", lambda _bot: SimpleNamespace(has=lambda name: True))
    sent = await _wait_notice(_ReactBot(), slow=True, monkeypatch=monkeypatch)
    assert len(sent) == 1
    assert sent[0] in {line.format(self="我") for line in progress._PHASE_LINES["sandbox"]}


@pytest.mark.asyncio
async def test_wait_notice_off_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_AI_WAIT_NOTICE_MODE", "off")
    assert await _wait_notice(SimpleNamespace(self_id="1"), slow=True, monkeypatch=monkeypatch) == []


# ---- 回合预算 ------------------------------------------------------------------


def test_argument_errors_distinguish_truncation() -> None:
    cut = budget.argument_error_payload('{"code": "print(1', ValueError("x"), finish_reason=None)
    assert cut["truncated"] is True and "截断" in cut["error"]
    by_reason = budget.argument_error_payload("{}", ValueError("x"), finish_reason="length")
    assert by_reason["truncated"] is True
    wrong = budget.argument_error_payload("code=1", ValueError("bad"), finish_reason="stop")
    assert "truncated" not in wrong and wrong["error_code"] == "invalid_args"


def test_budget_scales_with_sandbox_tools() -> None:
    assert budget.completion_timeout(["send_message"]) == budget.DEFAULT_COMPLETION_TIMEOUT
    assert budget.completion_timeout(["run_shell"]) == budget.LONG_ARGUMENT_COMPLETION_TIMEOUT
    assert budget.turn_seconds(["run_python"]) == budget.SANDBOX_TURN_SECONDS
    assert budget.turn_seconds([{"function": {"name": "send_message"}}]) == budget.DEFAULT_TURN_SECONDS
    assert budget.discovery_round_limit(3, 2, ["run_python"]) == 6
    assert budget.discovery_round_limit(3, 4, ["run_python"]) == budget.SANDBOX_TOOL_ROUNDS
    assert budget.discovery_round_limit(3, 2, ["search_group_history"]) == 4
    assert budget.discovery_deadline(10.0, 100.0, ["run_python"]) == 100.0 + budget.SANDBOX_EXTENSION_SECONDS
    assert budget.discovery_deadline(500.0, 100.0, ["run_python"]) == 500.0
    assert budget.discovery_deadline(10.0, 100.0, ["send_message"]) == 10.0
    assert budget.is_final_round(4, 5) and not budget.is_final_round(0, 1)


# ---- 沙箱输出整理 ----------------------------------------------------------------


def test_compact_traceback_keeps_user_frames() -> None:
    stderr = (
        "Traceback (most recent call last):\n"
        '  File "/job/main.py", line 3, in <module>\n'
        "    df.plot()\n"
        '  File "/lib/pandas/a.py", line 10, in plot\n'
        "    x()\n"
        '  File "/lib/pandas/b.py", line 20, in x\n'
        "    y()\n"
        '  File "/lib/pandas/c.py", line 30, in y\n'
        "    raise KeyError('col')\n"
        "KeyError: 'col'\n"
    )
    compact = output.compact_traceback(stderr)
    assert "main.py" in compact and "c.py" in compact
    assert "a.py" not in compact and "省略 2 层" in compact
    summary = output.error_summary(compact, "import pandas\n\ndf.plot()")
    assert summary == {"exception": "KeyError: 'col'", "line": 3, "code": "df.plot()", "fix": summary["fix"]}
    assert "columns" in summary["fix"]


def test_error_summary_handles_syntax_errors() -> None:
    stderr = '  File "/job/main.py", line 1\n    print(1\n         ^\nSyntaxError: \'(\' was never closed\n'
    summary = output.error_summary(stderr, "print(1")
    assert summary is not None and summary["line"] == 1 and "括号" in summary["fix"]
    assert output.error_summary("all good", None) is None


def test_trim_lines_cuts_on_line_boundaries() -> None:
    text = "\n".join(f"row {index}" for index in range(500))
    trimmed, cut = output.trim_lines(text, 400)
    assert cut and "省略中间" in trimmed and "共 500 行" in trimmed
    assert trimmed.splitlines()[0] == "row 0" and trimmed.splitlines()[-1] == "row 499"
    assert output.trim_lines("short", 400) == ("short", False)


# ---- 工具路由 ------------------------------------------------------------------


def test_router_prompt_has_only_the_message() -> None:
    messages = planner.build_router_messages("算一下 2 的 64 次方", signals={"reply": False, "media": True}, families=("sandbox", "history"))
    user = messages[1]["content"]
    assert "算一下" in user and '"media": true' in user and "reply" not in user
    assert "moderation" not in user


@pytest.mark.parametrize(
    ("raw", "packs", "wants"),
    [
        ('{"request": true, "toolpacks": ["sandbox", "bogus", "sandbox"], "segments": ["image", "nuke"]}', ("sandbox",), True),
        ('```json\n{"request": "true", "toolpacks": ["history"]}\n```', ("history",), True),
        ('{"request": false, "toolpacks": []}', (), False),
        ('{"toolpacks": ["sandbox","history","file","notice"]}', ("sandbox", "history", "file"), False),
    ],
)
def test_parse_router_reply(raw: str, packs: tuple[str, ...], wants: bool) -> None:  # noqa: FBT001
    plan = planner.parse_router_reply(raw, families=("sandbox", "history", "file", "notice"))
    assert plan is not None
    assert plan.toolpacks == packs and plan.request is wants
    assert plan.segments <= planner.PLANNABLE_SEGMENTS


@pytest.mark.parametrize("raw", ["", "随便聊聊", "[1, 2]", "{bad json"])
def test_parse_router_reply_rejects_garbage(raw: str) -> None:
    assert planner.parse_router_reply(raw, families=("sandbox",)) is None


@pytest.mark.asyncio
async def test_plan_tools_caches_and_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_complete(messages: list[Any], **kwargs: Any) -> str:
        calls.append(kwargs)
        return '{"request": true, "toolpacks": ["sandbox"], "segments": ["image"]}'

    monkeypatch.setattr(planner, "complete", fake_complete)
    first = await planner.plan_tools("画个折线图")
    second = await planner.plan_tools("  画个折线图\n")
    assert first.source == "model" and second.source == "cache"
    assert second.toolpacks == ("sandbox",) and second.segments == {"image"}
    assert len(calls) == 1
    assert calls[0]["task"] == "agent_router" and calls[0]["max_tokens"] == llm.NO_OUTPUT_LIMIT

    async def broken(messages: list[Any], **kwargs: Any) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(planner, "complete", broken)
    fallback = await planner.plan_tools("另一条")
    assert fallback.source == "fallback" and not fallback.planned and fallback.toolpacks == ()


@pytest.mark.asyncio
async def test_plan_tools_respects_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(planner.ai_config, "agent_router_enabled", False)
    plan = await planner.plan_tools("画个图")
    assert plan.reason == "disabled"


@pytest.mark.asyncio
async def test_preload_uses_discovery_permissions(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str | None] = []

    async def fake_discover(**kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs["family"])
        if kwargs["family"] == "sandbox":
            return {"tools": [{"name": "run_python", "permission": "state_write"}, {"name": "run_shell", "permission": "state_write"}]}
        return {"tools": []}

    monkeypatch.setattr(tools, "_discover_tools_for_actor", fake_discover)
    plan = planner.ToolPlan(request=True, toolpacks=("sandbox", "moderation", "history"), segments=frozenset({"at"}), source="model")
    caps = tools.BotGroupCapabilities(role="member", can_manage=False, actions=frozenset())
    result = await preload.preload_planned_tools(
        plan, bot=object(), group_id=1, actor_user_id=2, session=None, capabilities=caps, actor_can_manage=False, privileged_allowlist=frozenset()
    )
    assert seen == ["sandbox", "moderation"]
    assert result.names == {"run_python", "run_shell"}
    assert result.empty_packs == ("moderation",) and result.suggested == ("history",)
    assert {"at", "image", "record"} <= result.segments
    assert not result.requires_admin


@pytest.mark.asyncio
async def test_preload_skips_fallback_plans() -> None:
    caps = tools.BotGroupCapabilities(role="member", can_manage=False, actions=frozenset())
    result = await preload.preload_planned_tools(
        planner.FALLBACK_PLAN, bot=object(), group_id=1, actor_user_id=2, session=None, capabilities=caps, actor_can_manage=False, privileged_allowlist=frozenset()
    )
    assert result.names == frozenset()


def test_prompt_mentions_shell_rules_only_when_loaded() -> None:
    def tool(name: str) -> dict[str, Any]:
        return {"type": "function", "function": {"name": name, "parameters": {}}}

    with_shell = prompt.build_tool_guidance([tool("run_python"), tool("run_shell")])
    assert "bash" in with_shell and "cd" in with_shell
    assert "bash" not in prompt.build_tool_guidance([tool("run_python")])


# ---- 隐式触发 ------------------------------------------------------------------


def test_engagement_window(monkeypatch: pytest.MonkeyPatch) -> None:
    implicit.remember_engagement(1, 2, 3, now=100.0)
    assert implicit.recently_engaged(1, 2, 3, now=100.0 + implicit.ENGAGEMENT_WINDOW_SECONDS)
    assert not implicit.recently_engaged(1, 2, 3, now=101.0 + implicit.ENGAGEMENT_WINDOW_SECONDS)
    assert not implicit.recently_engaged(1, 2, 4, now=100.0)


def test_name_called_uses_word_boundaries() -> None:
    assert implicit.name_called("小咪帮我画个图", ("小咪",))
    assert implicit.name_called("yawn help", ("yawn",))
    assert not implicit.name_called("yawning cat", ("yawn",))
    assert not implicit.name_called("随便聊聊", ())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply", "engaged", "called", "expected"),
    [
        ({"request": True, "toolpacks": ["sandbox"]}, True, False, "conversation_intent"),
        ({"request": True, "toolpacks": []}, True, False, None),
        ({"request": True, "toolpacks": []}, False, True, "name_call"),
        ({"request": False, "toolpacks": ["sandbox"]}, True, True, None),
    ],
)
async def test_implicit_trigger_needs_router_agreement(
    monkeypatch: pytest.MonkeyPatch, reply: dict[str, Any], engaged: bool, called: bool, expected: str | None  # noqa: FBT001
) -> None:
    async def fake_plan(text: str, **kwargs: Any) -> Any:
        return planner.ToolPlan(request=reply["request"], toolpacks=tuple(reply["toolpacks"]), source="model")

    monkeypatch.setattr(implicit, "plan_tools", fake_plan)
    if engaged:
        implicit.remember_engagement(100, 1, 7)
    message = SimpleNamespace(intent_text=lambda: "小咪 算一下这组数据" if called else "算一下这组数据", mentions=[], reply_chain=[], media_refs=[])
    source, _plan = await implicit.resolve_implicit_trigger(message, bot_id=100, group_id=1, user_id=7, call_names=("小咪",))
    assert source == expected


@pytest.mark.asyncio
async def test_implicit_trigger_ignores_messages_for_others(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_plan(text: str, **kwargs: Any) -> Any:  # pragma: no cover - must not be called
        raise AssertionError("router should not run")

    monkeypatch.setattr(implicit, "plan_tools", fake_plan)
    implicit.remember_engagement(100, 1, 7)
    to_other = SimpleNamespace(intent_text=lambda: "帮我算一下", mentions=[8], reply_chain=[], media_refs=[])
    reply_other = SimpleNamespace(intent_text=lambda: "帮我算一下", mentions=[], reply_chain=[{"user_id": 8}], media_refs=[])
    stranger = SimpleNamespace(intent_text=lambda: "帮我算一下", mentions=[], reply_chain=[], media_refs=[])
    for message, user in ((to_other, 7), (reply_other, 7), (stranger, 9)):
        source, _plan = await implicit.resolve_implicit_trigger(message, bot_id=100, group_id=1, user_id=user, call_names=())
        assert source is None
    monkeypatch.setenv("AGENT_IMPLICIT_TRIGGER", "off")
    source, _plan = await implicit.resolve_implicit_trigger(stranger, bot_id=100, group_id=1, user_id=7, call_names=())
    assert source is None


# ---- 推理等级 ------------------------------------------------------------------


def test_reasoning_effort_kwargs_and_no_output_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    assert llm._max_tokens_kwargs(llm.NO_OUTPUT_LIMIT) == {}
    assert llm._max_tokens_kwargs(256) == {"max_tokens": 256}
    monkeypatch.setattr(llm.ai_config, "ai_default_reasoning_effort", "high")
    monkeypatch.setattr(llm.ai_config, "agent_dialogue_reasoning_effort", "inherit")
    assert llm.resolve_llm_request("agent_dialogue").reasoning_kwargs == {"reasoning_effort": "high"}
    monkeypatch.setattr(llm.ai_config, "agent_dialogue_reasoning_effort", "low")
    assert llm.resolve_llm_request("agent_dialogue").reasoning_kwargs == {"reasoning_effort": "low"}
    monkeypatch.setattr(llm.ai_config, "agent_dialogue_reasoning_effort", "auto")
    assert llm.resolve_llm_request("agent_dialogue").reasoning_kwargs == {}
