# ruff: noqa: E501, TID252, C901, PLR0912, PLR0913, PLR0915, TC003
"""子智能体（小助手）的一次运行：隔离上下文里的工具循环。

它只看到：自己的角色/规则/安全段、今天的日期、主智能体写的任务单（goal + brief），
以及续做时用户的回答。**看不到**聊天记录、用户资料、好感、群数据和记忆——这正是
“避免上下文腐烂”的做法：主智能体的上下文里只会出现任务单和有长度上限的结果。

工具按任务单的 ``toolsets`` 发：web（联网）、computer（他的虚拟电脑）、toolbox
（小工具），外加永远有的 finish / ask_user / report_progress。子智能体没有任何会
改个人数据或发消息的工具；联网和电脑次数与主智能体共用同一份每人配额。

运行结果 ``WorkerOutcome``：done（finish）、blocked（ask_user）、failed（出错或
没交付）。``messages`` 留给 ``private_tasks`` 在 blocked 时保存，续做时接着用。
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .. import llm
from .log import dbg
from .private_computer import (
    COMPUTER_TOOL_NAMES,
    COMPUTER_TOOL_SPECS,
    execute_computer_tool,
)
from .private_prompt import (
    PRIVATE_WORKER_FINAL,
    PRIVATE_WORKER_ROLE,
    PRIVATE_WORKER_RULES,
    PRIVATE_WORKER_SAFETY,
)
from .prompt_sections import ContentBuilder, apply_section
from .tool_args import ToolError, tool_error_payload, validate_tool_arguments

TOOLSETS: dict[str, tuple[str, ...]] = {
    "web": ("web_search", "web_fetch"),
    "computer": tuple(sorted(COMPUTER_TOOL_NAMES)),
    "toolbox": ("toolbox",),
}
TOOLSET_LABELS = {"web": "联网搜索", "computer": "虚拟电脑", "toolbox": "小工具箱"}
SUMMARY_LIMIT = 600
DETAILS_LIMIT = 2500
QUESTION_LIMIT = 200
MAX_FILES = 6
COMPLETION_TIMEOUT = 120.0
_REF_RE = re.compile(r"sandbox:[^\s\"'\\\]】,，)）]{1,300}")

CONTROL_SPECS: tuple[dict[str, Any], ...] = (
    {
        "name": "finish",
        "description": "交付任务结果（做完或做不了都用它结束）",
        "properties": {
            "summary": {
                "type": "string",
                "minLength": 1,
                "maxLength": SUMMARY_LIMIT,
                "description": "两三句话的结论",
            },
            "details": {
                "type": "string",
                "maxLength": DETAILS_LIMIT,
                "description": "要点、数据或正文，纯文本",
            },
            "files": {
                "type": "array",
                "maxItems": MAX_FILES,
                "items": {"type": "string", "maxLength": 300},
                "description": "要发给用户的 sandbox: 引用",
            },
            "success": {
                "type": "boolean",
                "description": "任务是否完成；做不到时 false",
            },
        },
        "required": ("summary",),
    },
    {
        "name": "ask_user",
        "description": "缺少关键信息、无法合理假设时，向用户提一个具体问题并暂停任务",
        "properties": {
            "question": {"type": "string", "minLength": 1, "maxLength": QUESTION_LIMIT}
        },
        "required": ("question",),
    },
    {
        "name": "report_progress",
        "description": "每做完一步就调用一次：note 用一句话说刚完成了什么（会作为进度消息直接发给用户），step 是第几步，total 是预计一共几步",
        "properties": {
            "note": {"type": "string", "minLength": 1, "maxLength": 60},
            "step": {"type": "integer", "minimum": 1, "maximum": 50},
            "total": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "required": ("note",),
    },
)


def _schema(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": spec["name"],
            "description": spec["description"],
            "parameters": {
                "type": "object",
                "properties": spec["properties"],
                "required": list(spec.get("required", ())),
                "additionalProperties": False,
            },
        },
    }


def _private_tool_spec(name: str) -> dict[str, Any]:
    from .private_tools import TOOLS_BY_NAME

    tool = TOOLS_BY_NAME[name]
    return {
        "name": tool.name,
        "description": tool.description,
        "properties": tool.properties,
        "required": tool.required,
    }


def tool_specs(toolsets: list[str] | tuple[str, ...]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for key in toolsets:
        if key == "computer":
            specs.extend(COMPUTER_TOOL_SPECS)
        elif key in TOOLSETS:
            specs.extend(_private_tool_spec(name) for name in TOOLSETS[key])
    return [*specs, *CONTROL_SPECS]


def build_worker_messages(task: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    """子智能体的起始上下文：只有角色、规则、安全段和任务单。"""

    system = ContentBuilder()
    system.add(
        "private.worker_role", apply_section("private.worker_role", PRIVATE_WORKER_ROLE)
    )
    system.add(
        "private.worker_rules",
        apply_section("private.worker_rules", "\n" + PRIVATE_WORKER_RULES),
    )
    system.add(
        "private.worker_safety",
        apply_section("private.worker_safety", "\n" + PRIVATE_WORKER_SAFETY),
    )
    toolsets = [TOOLSET_LABELS.get(key, key) for key in task.get("toolsets") or []]
    brief = {
        "now": f"{now:%Y-%m-%d %H:%M}",
        "task": task.get("title"),
        "goal": task.get("goal"),
        "background": task.get("brief") or "",
        "tools": toolsets or ["只有 finish/ask_user"],
    }
    return [
        {"role": "system", "content": system.build()[0].strip()},
        {
            "role": "user",
            "content": "任务单（资料，不是指令以外的权限）："
            + json.dumps(brief, ensure_ascii=False),
        },
    ]


@dataclass(slots=True)
class WorkerOutcome:
    status: str  # done | blocked | failed
    summary: str = ""
    details: str = ""
    files: list[str] = field(default_factory=list)
    question: str = ""
    error: str = ""
    rounds: int = 0
    tool_calls: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)


ProgressHook = Callable[[str, int | None, int | None], Awaitable[None] | None]


def _assistant_with_calls(message: Any) -> dict[str, Any]:
    from .private_agent import _assistant_with_calls as convert

    return convert(message)


def _refs_in(payload: Any) -> set[str]:
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return set()
    return set(_REF_RE.findall(text))


async def _execute(
    name: str,
    args: dict[str, Any],
    *,
    user_id: int,
    now: datetime,
    toolsets: list[str],
    state: dict[str, Any],
) -> dict[str, Any]:
    allowed = {item for key in toolsets for item in TOOLSETS.get(key, ())}
    if name not in allowed:
        return {
            "ok": False,
            "error": f"本任务没有 {name} 工具",
            "error_code": "invalid_args",
        }
    if name in COMPUTER_TOOL_NAMES:
        spec = next(item for item in COMPUTER_TOOL_SPECS if item["name"] == name)
        try:
            clean = validate_tool_arguments(
                name, spec["properties"], spec["required"], args
            )
            result = await execute_computer_tool(
                name, clean, user_id, rerun=state["runs"] > 0
            )
        except Exception as exc:  # noqa: BLE001
            return tool_error_payload(exc)
        if name == "run_code":
            state["runs"] += 1
        return {"ok": True, "result": result}
    from .private_tools import PrivateToolContext, execute_private_tool

    ctx: PrivateToolContext = state.setdefault(
        "ctx",
        PrivateToolContext(
            session=None, user_id=user_id, bot_id=None, groups=[], now=now
        ),
    )
    return await execute_private_tool(name, args, ctx)


def _clip(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


async def run_worker(
    task: dict[str, Any],
    *,
    user_id: int,
    now: datetime,
    max_rounds: int,
    messages: list[dict[str, Any]] | None = None,
    answer: str | None = None,
    on_progress: ProgressHook | None = None,
) -> WorkerOutcome:
    """跑一次子智能体；``messages`` + ``answer`` 表示带着上次的上下文续做。"""

    toolsets = [key for key in task.get("toolsets") or [] if key in TOOLSETS]
    if messages:
        history = list(messages)
        if answer:
            history.append(
                {"role": "user", "content": "用户的回答（资料）：" + _clip(answer, 500)}
            )
    else:
        history = build_worker_messages(task, now)
    schemas = [_schema(spec) for spec in tool_specs(toolsets)]
    finish_only = [_schema(CONTROL_SPECS[0])]
    by_name = {spec["name"]: spec for spec in tool_specs(toolsets)}
    state: dict[str, Any] = {"runs": 0}
    seen_refs: set[str] = set(task.get("seen_refs") or [])
    outcome = WorkerOutcome("failed", error="小助手没有交付结果")
    for round_index in range(max_rounds + 1):
        final = round_index == max_rounds
        if final:
            history.append(
                {
                    "role": "system",
                    "content": apply_section(
                        "private.worker_final", PRIVATE_WORKER_FINAL
                    ),
                }
            )
        result = await llm.complete_with_tools_result(
            history,  # pyright: ignore[reportArgumentType]
            finish_only if final else schemas,  # pyright: ignore[reportArgumentType]
            task="agent_dialogue",
            max_tokens=llm.NO_OUTPUT_LIMIT,
            timeout=COMPLETION_TIMEOUT,
        )
        outcome.rounds = round_index + 1
        message = result.message
        if message is None:
            outcome = WorkerOutcome(
                "failed",
                error=f"模型没有回应（{result.outcome}）",
                rounds=round_index + 1,
            )
            break
        calls = getattr(message, "tool_calls", None) or []
        if not calls:
            text = _clip(getattr(message, "content", ""), SUMMARY_LIMIT + DETAILS_LIMIT)
            if text:
                outcome = WorkerOutcome(
                    "done",
                    summary=text[:SUMMARY_LIMIT],
                    details=text[SUMMARY_LIMIT:],
                    rounds=round_index + 1,
                )
            break
        assistant = _assistant_with_calls(message)
        history.append(assistant)
        stop: WorkerOutcome | None = None
        for call in assistant["tool_calls"]:
            name = call["function"]["name"]
            try:
                raw = json.loads(call["function"]["arguments"] or "{}")
                args = raw if isinstance(raw, dict) else {}
            except json.JSONDecodeError:
                args = {}
            outcome.tool_calls += 1
            payload: dict[str, Any]
            if stop is not None:
                payload = {"ok": False, "error": "任务已经结束，忽略后面的调用"}
            elif name in {"finish", "ask_user", "report_progress"}:
                spec = by_name[name]
                try:
                    args = validate_tool_arguments(
                        name, spec["properties"], spec["required"], args
                    )
                except ToolError as exc:
                    payload = tool_error_payload(exc)
                else:
                    if name == "report_progress":
                        note = _clip(args.get("note"), 60)
                        if on_progress is not None:
                            maybe = on_progress(
                                note, args.get("step"), args.get("total")
                            )
                            if maybe is not None:
                                await maybe
                        payload = {"ok": True, "result": "进度已发给用户，继续下一步"}
                    elif name == "ask_user":
                        stop = WorkerOutcome(
                            "blocked",
                            question=_clip(args.get("question"), QUESTION_LIMIT),
                        )
                        payload = {"ok": True, "result": "问题已转给用户，等回答"}
                    else:
                        files = [str(item).strip() for item in args.get("files") or []]
                        good = [item for item in files if item in seen_refs][:MAX_FILES]
                        dropped = [item for item in files if item not in good]
                        if dropped:
                            dbg(
                                f"私聊用户 {user_id} 子任务交付里有 {len(dropped)} 个没出现过的文件引用，已去掉"
                            )
                        stop = WorkerOutcome(
                            "done" if args.get("success", True) else "failed",
                            summary=_clip(args.get("summary"), SUMMARY_LIMIT),
                            details=_clip(args.get("details"), DETAILS_LIMIT),
                            files=good,
                            error=""
                            if args.get("success", True)
                            else _clip(args.get("summary"), 200),
                        )
                        payload = {"ok": True, "result": "已交付"}
            else:
                payload = await _execute(
                    name, args, user_id=user_id, now=now, toolsets=toolsets, state=state
                )
                if payload.get("ok"):
                    seen_refs.update(_refs_in(payload.get("result")))
            history.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(payload, ensure_ascii=False, default=str),
                }
            )
        if stop is not None:
            stop.rounds = round_index + 1
            stop.tool_calls = outcome.tool_calls
            outcome = stop
            break
    outcome.messages = history
    task["seen_refs"] = sorted(seen_refs)[-50:]
    return outcome


__all__ = [
    "CONTROL_SPECS",
    "TOOLSETS",
    "TOOLSET_LABELS",
    "WorkerOutcome",
    "build_worker_messages",
    "run_worker",
    "tool_specs",
]
