# ruff: noqa: E501, TID252, C901, PLR0912, PLR0913, PLR0915, TC003, TRY003
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
    PRIVATE_WORKER_CONTINUE,
    PRIVATE_WORKER_FINAL,
    PRIVATE_WORKER_ROLE,
    PRIVATE_WORKER_RULES,
    PRIVATE_WORKER_SAFETY,
    PRIVATE_WORKER_TEAM,
    PRIVATE_WORKER_VOICE,
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

# 工作台工具：计划、备忘、团队通信。和 finish/ask_user/report_progress 一样永远都有。
PLAN_LIMIT = 12
MEMO_LIMIT = 4000
# 上下文压缩：超过这么多字符时，把较早的工具结果截成摘要（计划和备忘不受影响）。
CONTEXT_CHAR_BUDGET = 60000
KEEP_RECENT_MESSAGES = 12
COMPACT_STUB_CHARS = 240
WORKBENCH_SPECS: tuple[dict[str, Any], ...] = (
    {
        "name": "plan",
        "description": "写下或更新这个任务的计划：steps 是按顺序的几步，done 是已经完成的步骤序号（从 1 开始）。大任务开工前先写",
        "properties": {
            "steps": {
                "type": "array",
                "minItems": 1,
                "maxItems": PLAN_LIMIT,
                "items": {"type": "string", "maxLength": 80},
            },
            "done": {
                "type": "array",
                "items": {"type": "integer"},
                "maxItems": PLAN_LIMIT,
            },
        },
        "required": (),
    },
    {
        "name": "memo",
        "description": "长任务的备忘本：append 追加一段（关键中间结论、文件路径、确认过的事实），read 看全文。备忘一直留在你的上下文里，不会被压缩",
        "properties": {
            "action": {"type": "string", "enum": ["append", "read"]},
            "text": {"type": "string", "maxLength": 800},
        },
        "required": ("action",),
    },
    {
        "name": "team",
        "description": "和主助手、队友通信：post 发消息（to=main 主助手 / 队友名或任务编号 / all 全体队友；need_reply 表示需要对方回），"
        "read 看发给你的新消息，status 看队友的进度和成果，wait 等回复（最多 120 秒，有消息立刻返回）",
        "properties": {
            "action": {"type": "string", "enum": ["post", "read", "status", "wait"]},
            "to": {"type": "string", "maxLength": 20},
            "text": {"type": "string", "maxLength": 600},
            "need_reply": {"type": "boolean"},
            "seconds": {"type": "integer", "minimum": 5, "maximum": 120},
        },
        "required": ("action",),
    },
)
WORKBENCH_NAMES = frozenset(spec["name"] for spec in WORKBENCH_SPECS)

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
    return [*specs, *WORKBENCH_SPECS, *CONTROL_SPECS]


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
        "private.worker_voice",
        apply_section("private.worker_voice", "\n" + PRIVATE_WORKER_VOICE),
    )
    system.add(
        "private.worker_team",
        apply_section("private.worker_team", "\n" + PRIVATE_WORKER_TEAM),
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
        "your_id": task.get("id"),
    }
    if task.get("member"):
        brief["your_role"] = task.get("member")
    if task.get("asked"):
        brief["asked"] = task.get("asked")
    if task.get("dialogue"):
        brief["chat_so_far"] = task.get("dialogue")
    if task.get("voice"):
        brief["voice"] = task.get("voice")
    if task.get("call_them"):
        brief["call_them"] = task.get("call_them")
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
        if name in {"run_code", "computer_do"}:
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


def _save_task_fields(user_id: int, task: dict[str, Any], **fields: Any) -> None:
    task.update(fields)
    task_id = task.get("id")
    if not task_id:
        return
    try:
        from .private_tasks import _update_task

        _update_task(user_id, str(task_id), **fields)
    except Exception:  # noqa: BLE001
        dbg(f"私聊用户 {user_id} 子任务 {task_id} 工作台保存失败(忽略)")


def _plan_tool(
    user_id: int, task: dict[str, Any], args: dict[str, Any]
) -> dict[str, Any]:
    steps = [_clip(item, 80) for item in args.get("steps") or [] if _clip(item, 80)][
        :PLAN_LIMIT
    ]
    plan = list(task.get("plan") or [])
    if steps:
        plan = [{"step": text, "done": False} for text in steps]
    done = {
        int(item) for item in args.get("done") or [] if str(item).lstrip("-").isdigit()
    }
    for index, item in enumerate(plan, start=1):
        if index in done:
            item["done"] = True
    if not plan:
        raise ToolError("还没有计划：先给 steps", code="invalid_args")
    _save_task_fields(user_id, task, plan=plan)
    left = [item["step"] for item in plan if not item.get("done")]
    return {
        "plan": [
            f"{'✓' if item.get('done') else '○'} {index}. {item['step']}"
            for index, item in enumerate(plan, start=1)
        ],
        "remaining": len(left),
    }


def _memo_tool(
    user_id: int, task: dict[str, Any], args: dict[str, Any]
) -> dict[str, Any]:
    memo = str(task.get("memo") or "")
    if args.get("action") == "append":
        text = _clip(args.get("text"), 800)
        if not text:
            raise ToolError("append 要给 text", code="invalid_args")
        memo = f"{memo}\n- {text}".strip()[-MEMO_LIMIT:]
        _save_task_fields(user_id, task, memo=memo)
        return {"memo_chars": len(memo), "saved": True}
    return {"memo": memo or "（备忘是空的）"}


async def _team_tool(
    user_id: int, task: dict[str, Any], args: dict[str, Any]
) -> dict[str, Any]:
    from . import private_team

    me = str(task.get("id") or "")
    if not me:
        raise ToolError("这个任务没有编号，不能通信", code="invalid_args")
    action = str(args.get("action") or "read")
    if action == "post":
        return private_team.post(
            user_id,
            me,
            str(args.get("to") or private_team.MAIN),
            str(args.get("text") or ""),
            need_reply=bool(args.get("need_reply")),
        )
    if action == "status":
        return {"teammates": private_team.team_status(user_id, me)}
    if action == "wait":
        rows = await private_team.wait_for(
            user_id, me, float(args.get("seconds") or 60)
        )
        return (
            {"messages": rows}
            if rows
            else {"messages": [], "note": "没等到回复，先做能做的"}
        )
    return {"messages": private_team.unread(user_id, me)}


def _context_chars(history: list[dict[str, Any]]) -> int:
    return sum(len(str(item.get("content") or "")) for item in history)


def compact_history(
    history: list[dict[str, Any]], *, budget: int = CONTEXT_CHAR_BUDGET
) -> int:
    """上下文太长时，把较早的工具结果截成短摘要；返回压缩了几条。

    只动 ``role=tool`` 的内容（保持工具调用配对完整），最近 ``KEEP_RECENT_MESSAGES``
    条不动；系统段、任务单、计划和备忘都不受影响。确定性截断，不额外调用模型。
    """

    if _context_chars(history) <= budget:
        return 0
    compacted = 0
    cutoff = max(len(history) - KEEP_RECENT_MESSAGES, 0)
    for item in history[:cutoff]:
        if item.get("role") != "tool":
            continue
        content = str(item.get("content") or "")
        if len(content) <= COMPACT_STUB_CHARS or content.startswith(
            "[早先的结果已压缩"
        ):
            continue
        refs = sorted(set(_REF_RE.findall(content)))[:6]
        stub = f"[早先的结果已压缩，需要细节就重新查] {content[:COMPACT_STUB_CHARS]}…"
        if refs:
            stub += f" 文件：{' '.join(refs)}"
        item["content"] = stub
        compacted += 1
        if _context_chars(history) <= budget * 3 // 4:
            break
    return compacted


def _workbench_note(task: dict[str, Any]) -> str | None:
    plan = task.get("plan") or []
    memo = str(task.get("memo") or "")
    if not plan and not memo:
        return None
    parts = []
    if plan:
        parts.append(
            "计划："
            + "；".join(
                f"{'✓' if item.get('done') else '○'}{index}.{item.get('step')}"
                for index, item in enumerate(plan, start=1)
            )
        )
    if memo:
        parts.append(f"备忘：{memo[-1500:]}")
    return "【工作台（资料）】" + "\n".join(parts)


async def run_worker(
    task: dict[str, Any],
    *,
    user_id: int,
    now: datetime,
    max_rounds: int,
    messages: list[dict[str, Any]] | None = None,
    answer: str | None = None,
    on_progress: ProgressHook | None = None,
    continuations: int = 0,
) -> WorkerOutcome:
    """跑一次子智能体；``messages`` + ``answer`` 表示带着上次的上下文续做。

    ``max_rounds`` 用完还没交付时，最多再续 ``continuations`` 段（每段同样多轮），
    续期前提示它看计划和备忘接着做；整体仍受 ``private_tasks`` 的总时限约束。
    """

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
    # 任务单（含队友成果）里出现的文件引用也算“见过”，可以交付。
    seen_refs.update(_REF_RE.findall(str(task.get("brief") or "")))
    # 聊天节选里“已放进他的虚拟电脑：sandbox:…”的文件是他自己发的，也可以交付。
    seen_refs.update(_REF_RE.findall(str(task.get("dialogue") or "")))
    outcome = WorkerOutcome("failed", error="小助手没有交付结果")
    total_rounds = max_rounds * (1 + max(continuations, 0))
    segment_end = max_rounds
    finish_checked = False
    for round_index in range(total_rounds + 1):
        if round_index == segment_end and round_index < total_rounds:
            segment_end += max_rounds
            history.append(
                {
                    "role": "system",
                    "content": apply_section(
                        "private.worker_continue", PRIVATE_WORKER_CONTINUE
                    ),
                }
            )
            note = _workbench_note(task)
            if note:
                history.append({"role": "user", "content": note})
        final = round_index == total_rounds
        if final:
            history.append(
                {
                    "role": "system",
                    "content": apply_section(
                        "private.worker_final", PRIVATE_WORKER_FINAL
                    ),
                }
            )
        else:
            team_messages = _incoming(user_id, task)
            if team_messages:
                seen_refs.update(_REF_RE.findall(team_messages))
                history.append({"role": "user", "content": team_messages})
        if compact_history(history):
            note = _workbench_note(task)
            if note:
                history.append({"role": "user", "content": note})
        result = await _complete(history, finish_only if final else schemas)
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
            elif name in WORKBENCH_NAMES:
                spec = by_name[name]
                try:
                    clean = validate_tool_arguments(
                        name, spec["properties"], spec["required"], args
                    )
                    if name == "plan":
                        payload = {
                            "ok": True,
                            "result": _plan_tool(user_id, task, clean),
                        }
                    elif name == "memo":
                        payload = {
                            "ok": True,
                            "result": _memo_tool(user_id, task, clean),
                        }
                    else:
                        result_payload = await _team_tool(user_id, task, clean)
                        seen_refs.update(_refs_in(result_payload))
                        payload = {"ok": True, "result": result_payload}
                except Exception as exc:  # noqa: BLE001
                    payload = tool_error_payload(exc)
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
                    elif (
                        args.get("success", True)
                        and not finish_checked
                        and _unfinished_steps(task)
                        and not final
                    ):
                        # 交付前自查：计划里还有没打勾的步骤，提醒一次。
                        finish_checked = True
                        payload = {
                            "ok": False,
                            "error": "计划里还有步骤没标完成："
                            + "；".join(_unfinished_steps(task)[:4]),
                            "hint": "确实做完了就用 plan 的 done 标上再 finish；没做完就接着做，或 finish 时 success=false 并说明做到哪",
                        }
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


def _unfinished_steps(task: dict[str, Any]) -> list[str]:
    return [
        str(item.get("step")) for item in task.get("plan") or [] if not item.get("done")
    ]


def _incoming(user_id: int, task: dict[str, Any]) -> str | None:
    """发给这个小助手的未读团队消息（主助手的补充要求、队友的消息）。"""

    task_id = task.get("id")
    if not task_id:
        return None
    try:
        from . import private_team

        rows = private_team.unread(user_id, str(task_id))
    except Exception:  # noqa: BLE001
        return None
    if not rows:
        return None
    return "【团队消息（资料，按需采纳；主助手转达的是用户的新要求）】" + json.dumps(
        rows, ensure_ascii=False
    )


_TRANSIENT_RETRY_SECONDS = 3.0


async def _complete(history: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
    """请求模型；没拿到回复（超时、上游抖动）时等一下重试一次，长任务不因一次抖动整个失败。"""

    import asyncio

    result = await llm.complete_with_tools_result(
        history,  # pyright: ignore[reportArgumentType]
        tools,  # pyright: ignore[reportArgumentType]
        task="agent_dialogue",
        max_tokens=llm.NO_OUTPUT_LIMIT,
        timeout=COMPLETION_TIMEOUT,
    )
    if result.message is not None:
        return result
    await asyncio.sleep(_TRANSIENT_RETRY_SECONDS)
    return await llm.complete_with_tools_result(
        history,  # pyright: ignore[reportArgumentType]
        tools,  # pyright: ignore[reportArgumentType]
        task="agent_dialogue",
        max_tokens=llm.NO_OUTPUT_LIMIT,
        timeout=COMPLETION_TIMEOUT,
    )


__all__ = [
    "CONTROL_SPECS",
    "TOOLSETS",
    "TOOLSET_LABELS",
    "WORKBENCH_SPECS",
    "WorkerOutcome",
    "build_worker_messages",
    "compact_history",
    "run_worker",
    "tool_specs",
]
