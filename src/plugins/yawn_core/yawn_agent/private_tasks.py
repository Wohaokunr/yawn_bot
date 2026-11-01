# ruff: noqa: TID252, C901, DTZ007, PLR2004, PLR0912, PLR0913, PLR0915, TRY003, E501
"""私聊主从多智能体的任务管理器：主智能体派活，子智能体在后台做，做完汇报。

借鉴 dsh-superagent 的做法：主会话只负责路由和对话，每件事一个独立的执行会话
（子智能体，上下文隔离）；执行有并发槽，超出排队；结果只有完成 / 等你回答 / 失败
三种结局；状态先存后做，重启不会假装还在跑。

- 任务记录存在个人数据 JSON 的 ``tasks``（最近 ``MAX_TASKS`` 个），不建表；
- 并发：每人同时运行 ``PRIVATE_AGENT_TASK_PER_USER`` 个、全站 ``…_TASK_SLOTS`` 个，
  其余排队；每人排队最多同样多个，再多拒绝；每人每天 ``…_TASK_DAILY`` 个；
- 子智能体每做完一步调用 ``report_progress``，这里直接发一条模板进度消息（不过模型，
  不占回合锁；免打扰时段不发，每个任务最多 ``MAX_PROGRESS_MESSAGES`` 条）；
- 结束后交给登记的汇报函数（``private_agent.report_task``）：拿回合锁，由主智能体用
  人设转述；免打扰时段完成且离派活超过 ``QUIET_GRACE`` 的结果先压着，免打扰结束再发；
- ``ask_user`` → 等你回答：子智能体上下文留在内存（``CONTEXT_TTL``），用户回答后带着
  原上下文续做；上下文丢了（重启/过期）就把回答并进任务单重做；
- Bot 启动时，``queued``/``running`` 的任务记为失败（重启中断），不自动重跑。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from ..private_chat_bridge import private_agent_config
from . import private_store as store
from .context import now_beijing
from .log import dbg, dbg_exc
from .tool_args import (
    ERROR_INVALID_ARGS,
    ERROR_NOT_FOUND,
    ERROR_PERMISSION,
    ERROR_QUOTA,
    ToolError,
)

MAX_TASKS = 20
MAX_PROGRESS_MESSAGES = 20
CONTEXT_TTL = timedelta(hours=24)
QUIET_GRACE = timedelta(minutes=10)
ACTIVE = frozenset({"queued", "running"})
OPEN = frozenset({"queued", "running", "blocked"})
STATUS_LABELS = {
    "queued": "排队中",
    "running": "进行中",
    "blocked": "等你回答",
    "done": "已完成",
    "failed": "没做成",
    "cancelled": "已取消",
}

Reporter = Callable[[Any, int, dict[str, Any]], Awaitable[bool]]
Notifier = Callable[[Any, int, str], Awaitable[bool]]

_reporter: Reporter | None = None
_notifier: Notifier | None = None
_running: dict[str, asyncio.Task[Any]] = {}
_contexts: dict[str, tuple[list[dict[str, Any]], float]] = {}
_bots: dict[int, Any] = {}
_user_sems: dict[int, asyncio.Semaphore] = {}
_global_sem: asyncio.Semaphore | None = None
_global_size = 0


def register_reporter(
    reporter: Reporter | None, notifier: Notifier | None = None
) -> None:
    global _reporter, _notifier  # noqa: PLW0603
    _reporter = reporter
    _notifier = notifier


def tasks_enabled() -> bool:
    return bool(
        private_agent_config.private_agent_enabled
        and private_agent_config.private_agent_tasks_enabled
    )


def _key(user_id: int, task_id: str) -> str:
    return f"{int(user_id)}:{task_id}"


def _fmt(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _parse(value: Any) -> datetime | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def _global() -> asyncio.Semaphore:
    global _global_sem, _global_size  # noqa: PLW0603
    size = int(private_agent_config.private_agent_task_slots)
    if _global_sem is None or _global_size != size:
        _global_sem = asyncio.Semaphore(size)
        _global_size = size
    return _global_sem


def _user_sem(user_id: int) -> asyncio.Semaphore:
    size = int(private_agent_config.private_agent_task_per_user)
    sem = _user_sems.get(user_id)
    if sem is None or getattr(sem, "_yawn_size", None) != size:
        sem = asyncio.Semaphore(size)
        sem._yawn_size = size  # type: ignore[attr-defined]
        _user_sems[user_id] = sem
    return sem


# ── 记录读写 ──────────────────────────────────────────────────────────────


def _find(record: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    for item in record.get("tasks", []):
        if item.get("id") == task_id:
            return item
    return None


def _update_task(user_id: int, task_id: str, **fields: Any) -> dict[str, Any] | None:
    def change(record: dict[str, Any]) -> dict[str, Any] | None:
        item = _find(record, task_id)
        if item is None:
            return None
        item.update(fields)
        return dict(item)

    return store.update(user_id, change)


def get_task(user_id: int, task_id: str) -> dict[str, Any] | None:
    item = _find(store.load(user_id), task_id)
    return dict(item) if item else None


def _trim(record: dict[str, Any]) -> None:
    tasks = record["tasks"]
    while len(tasks) > MAX_TASKS:
        old = next((item for item in tasks if item.get("status") not in OPEN), None)
        if old is None:
            break
        tasks.remove(old)


def view(task: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """给模型 / WebUI 看的任务视图（不含子智能体内部数据）。"""

    now = now or now_beijing()
    row: dict[str, Any] = {
        "id": task.get("id"),
        "title": task.get("title"),
        "status": STATUS_LABELS.get(str(task.get("status")), task.get("status")),
        "state": task.get("status"),
    }
    started = _parse(task.get("started_at"))
    finished = _parse(task.get("finished_at"))
    if started:
        seconds = int(((finished or now) - started).total_seconds())
        row["elapsed"] = (
            f"{seconds // 60} 分 {seconds % 60} 秒"
            if seconds >= 60
            else f"{seconds} 秒"
        )
    if task.get("member"):
        row["member"] = task.get("member")
    if task.get("project"):
        row["project"] = task.get("project")
    if (
        task.get("depends_on")
        and task.get("status") == "queued"
        and not task.get("spawned")
    ):
        row["waiting_for_teammates"] = task.get("depends_on")
    plan = task.get("plan") or []
    if plan:
        done = sum(1 for step in plan if step.get("done"))
        row["plan"] = f"{done}/{len(plan)} 步"
    progress = task.get("progress") or []
    if progress:
        row["last_progress"] = progress[-1].get("note")
    if task.get("status") == "blocked":
        row["waiting_for_you"] = task.get("question")
    if task.get("status") == "done":
        row["summary"] = task.get("summary")
    if task.get("status") == "failed":
        row["reason"] = task.get("error")
    return row


def list_tasks(user_id: int, *, limit: int = 10) -> list[dict[str, Any]]:
    tasks = list(store.load(user_id).get("tasks", []))
    return [view(item) for item in reversed(tasks)][:limit]


def context_digest(user_id: int, now: datetime) -> list[dict[str, Any]]:
    """私聊上下文里的 tasks：所有没结束的 + 最近 6 小时内结束的（最多 5 个）。"""

    rows = []
    for item in reversed(store.load(user_id).get("tasks", [])):
        if item.get("status") in OPEN:
            rows.append(view(item, now))
            continue
        finished = _parse(item.get("finished_at"))
        if finished and now - finished <= timedelta(hours=6):
            row = view(item, now)
            row.pop("summary", None)
            rows.append(row)
    return rows[:5]


# ── 派活 ──────────────────────────────────────────────────────────────────


def start_task(
    bot: Any,
    user_id: int,
    *,
    title: str,
    goal: str,
    brief: str,
    toolsets: list[str],
    save_to_notes: bool,
    now: datetime,
) -> dict[str, Any]:
    """登记一个任务并在后台开跑；返回给主智能体的结果。超限抛 ``ToolError``。"""

    if not tasks_enabled():
        raise ToolError(
            "管理员没有开放小助手任务",
            code=ERROR_PERMISSION,
            hint="自己尽力回答，或如实说做不了",
        )
    per_user = int(private_agent_config.private_agent_task_per_user)
    daily = int(private_agent_config.private_agent_task_daily)
    today = now.date().isoformat()

    def create(record: dict[str, Any]) -> dict[str, Any]:
        tasks = record["tasks"]
        active = [item for item in tasks if item.get("status") in ACTIVE]
        if len(active) >= per_user * 2:
            raise ToolError(
                f"他已经有 {len(active)} 个任务在做或在排队了",
                code=ERROR_QUOTA,
                hint="先告诉他等前面的做完，或问他要不要取消某个",
            )
        flags = record["flags"]
        used = int(flags.get("task_used") or 0) if flags.get("task_day") == today else 0
        if daily and used >= daily:
            raise ToolError(
                f"今天的小助手任务已经用完了（{daily} 个）",
                code=ERROR_QUOTA,
                hint="如实说明，可以自己尽力回答",
            )
        flags["task_day"] = today
        flags["task_used"] = used + 1
        task = {
            "id": f"t{store.next_id(record)}",
            "title": title,
            "goal": goal,
            "brief": brief,
            "toolsets": toolsets,
            "save_to_notes": bool(save_to_notes),
            "status": "queued",
            "created_at": _fmt(now),
            "progress": [],
        }
        tasks.append(task)
        _trim(record)
        return {
            "task": dict(task),
            "running": len([i for i in active if i.get("status") == "running"]),
            "queued": len(active),
        }

    created = store.update(user_id, create)
    task = created["task"]
    _bots[user_id] = bot
    _spawn(user_id, task["id"])
    dbg(f"私聊用户 {user_id} 派出子任务 {task['id']} 「{title}」 tools={toolsets}")
    payload: dict[str, Any] = {
        "task_id": task["id"],
        "title": title,
        "status": "已派出",
    }
    if created["queued"] >= per_user:
        payload["status"] = "排队中"
        payload["note"] = f"他前面还有 {created['queued']} 个任务，轮到才会开始"
    payload["next"] = (
        "简短告诉他交给小助手了，小助手每做完一步会自己发进度，做完你会收到结果再转述；不要现在编结果"
    )
    return payload


def _spawn(user_id: int, task_id: str, *, answer: str | None = None) -> None:
    key = _key(user_id, task_id)
    runner = asyncio.create_task(_drive(user_id, task_id, answer=answer))
    _running[key] = runner
    runner.add_done_callback(
        lambda _t, k=key: _running.pop(k, None) if _running.get(k) is _t else None
    )


async def _drive(user_id: int, task_id: str, *, answer: str | None = None) -> None:
    from .private_worker import run_worker

    timeout = float(private_agent_config.private_agent_task_timeout_seconds)
    max_rounds = int(private_agent_config.private_agent_task_max_rounds)
    try:
        async with _user_sem(user_id), _global():
            task = get_task(user_id, task_id)
            if task is None or task.get("status") != "queued":
                return
            now = now_beijing()
            task = (
                _update_task(
                    user_id,
                    task_id,
                    status="running",
                    started_at=task.get("started_at") or _fmt(now),
                )
                or task
            )
            key = _key(user_id, task_id)
            saved = _contexts.pop(key, None)
            messages = (
                saved[0]
                if saved and time.time() - saved[1] < CONTEXT_TTL.total_seconds()
                else None
            )
            if answer and messages is None:
                # 上下文没了（重启或过期）：把回答并进任务单，从头做。
                task["brief"] = (
                    f"{task.get('brief') or ''}\n用户补充：{answer}".strip()[:2000]
                )
                _update_task(user_id, task_id, brief=task["brief"])

            async def progress(note: str, step: int | None, total: int | None) -> None:
                await _on_progress(user_id, task_id, note, step, total)

            try:
                outcome = await asyncio.wait_for(
                    run_worker(
                        task,
                        user_id=user_id,
                        now=now,
                        max_rounds=max_rounds,
                        messages=messages,
                        answer=answer if messages is not None else None,
                        on_progress=progress,
                        continuations=int(
                            private_agent_config.private_agent_task_continuations
                        ),
                    ),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                last = (get_task(user_id, task_id) or {}).get("progress") or []
                note = f"，做到：{last[-1]['note']}" if last else ""
                _finish(
                    user_id,
                    task_id,
                    status="failed",
                    error=f"超过 {int(timeout // 60)} 分钟没做完{note}",
                )
            else:
                fields: dict[str, Any] = {
                    "status": outcome.status,
                    "rounds": int(task.get("rounds") or 0) + outcome.rounds,
                    "seen_refs": task.get("seen_refs") or [],
                }
                if outcome.status == "blocked":
                    fields["question"] = outcome.question
                    _contexts[key] = (outcome.messages, time.time())
                    _prune_contexts()
                    _update_task(user_id, task_id, **fields)
                else:
                    fields.update(
                        summary=outcome.summary,
                        details=outcome.details,
                        files=outcome.files,
                        error=outcome.error,
                    )
                    _finish(user_id, task_id, **fields)
    except asyncio.CancelledError:
        current = get_task(user_id, task_id)
        if current and current.get("status") in OPEN:
            _finish(user_id, task_id, status="cancelled")
        raise
    except Exception:  # noqa: BLE001
        dbg_exc(f"私聊用户 {user_id} 子任务 {task_id} 异常")
        _finish(user_id, task_id, status="failed", error="小助手出了点问题")
    final = get_task(user_id, task_id)
    if final is None or final.get("status") == "cancelled":
        if final is not None and final.get("project"):
            await _advance_project(user_id, str(final["project"]))
        return
    if final.get("project") and final.get("status") in {"done", "failed"}:
        # 项目成员不单独汇报：推进依赖它的队友，全部结束后整体汇报一次。
        await _advance_project(user_id, str(final["project"]))
        return
    if final.get("status") == "done" and final.get("save_to_notes"):
        _save_note(user_id, final)
    await deliver_report(user_id, task_id)


def _finish(user_id: int, task_id: str, **fields: Any) -> None:
    fields.setdefault("finished_at", _fmt(now_beijing()))
    _update_task(user_id, task_id, **fields)


def _prune_contexts() -> None:
    cutoff = time.time() - CONTEXT_TTL.total_seconds()
    for key in [k for k, (_m, at) in _contexts.items() if at < cutoff]:
        _contexts.pop(key, None)


def _save_note(user_id: int, task: dict[str, Any]) -> None:
    from .private_organizer import notes

    files = " ".join(task.get("files") or [])
    content = f"「{task.get('title')}」{task.get('summary') or ''}"
    if files:
        content = f"{content}（文件：{files}）"
    try:
        notes(
            user_id,
            {"action": "add", "content": content[:300], "tag": "小助手"},
            now_beijing(),
        )
        _update_task(user_id, task["id"], saved_note=True)
    except Exception as exc:  # noqa: BLE001
        dbg(f"私聊用户 {user_id} 子任务结果存笔记失败: {exc}")
        _update_task(user_id, task["id"], saved_note=False)


# ── 多成员项目：分工、依赖、整体汇报 ─────────────────────────────────────

MAX_MEMBERS = 6
MAX_PROJECTS = 10
TERMINAL = frozenset({"done", "failed", "cancelled"})


def _find_project(record: dict[str, Any], project_id: str) -> dict[str, Any] | None:
    for item in record.get("projects", []):
        if item.get("id") == project_id:
            return item
    return None


def _clean_members(raw: Any) -> list[dict[str, Any]]:
    from .private_organizer import clean_text

    if not isinstance(raw, list) or not 2 <= len(raw) <= MAX_MEMBERS:
        raise ToolError(
            f"members 要有 2~{MAX_MEMBERS} 个成员",
            code=ERROR_INVALID_ARGS,
            hint="一个人就能做完的用 delegate_task",
        )
    members: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ToolError(f"第 {index} 个成员格式不对", code=ERROR_INVALID_ARGS)
        name = clean_text(item.get("name") or f"成员{index}", limit=12, field="成员名")
        if name in names or name.lower() in {"main", "all"}:
            raise ToolError(f"成员名 {name} 重复或不能用", code=ERROR_INVALID_ARGS)
        names.add(name)
        tools = sorted(
            {
                str(t)
                for t in item.get("tools") or []
                if str(t) in {"web", "computer", "toolbox"}
            }
        )
        if not tools:
            raise ToolError(
                f"成员 {name} 的 tools 只能选 web/computer/toolbox",
                code=ERROR_INVALID_ARGS,
            )
        members.append(
            {
                "name": name,
                "goal": clean_text(item.get("goal"), limit=600, field=f"{name} 的目标"),
                "tools": tools,
                "depends_on": [
                    str(dep).strip()
                    for dep in item.get("depends_on") or []
                    if str(dep).strip()
                ],
            }
        )
    for member in members:
        unknown = [
            dep
            for dep in member["depends_on"]
            if dep not in names or dep == member["name"]
        ]
        if unknown:
            raise ToolError(
                f"{member['name']} 依赖的 {'、'.join(unknown)} 不是别的成员",
                code=ERROR_INVALID_ARGS,
                hint="depends_on 写其他成员的 name",
            )
    # 有环就没人能开工。
    order: list[str] = []
    pending = {m["name"]: set(m["depends_on"]) for m in members}
    while pending:
        ready = [name for name, deps in pending.items() if deps <= set(order)]
        if not ready:
            raise ToolError(
                "成员之间的依赖成了环",
                code=ERROR_INVALID_ARGS,
                hint="去掉互相等待的依赖",
            )
        order.extend(sorted(ready))
        for name in ready:
            pending.pop(name)
    return members


def start_project(
    bot: Any,
    user_id: int,
    *,
    title: str,
    goal: str,
    brief: str,
    members: Any,
    save_to_notes: bool,
    now: datetime,
) -> dict[str, Any]:
    """登记一个多成员项目：每个成员一个任务，没有依赖的先开工。"""

    if not tasks_enabled():
        raise ToolError(
            "管理员没有开放小助手任务",
            code=ERROR_PERMISSION,
            hint="自己尽力回答，或如实说做不了",
        )
    cleaned = _clean_members(members)
    per_user = int(private_agent_config.private_agent_task_per_user)
    daily = int(private_agent_config.private_agent_task_daily)
    today = now.date().isoformat()

    def create(record: dict[str, Any]) -> dict[str, Any]:
        tasks = record["tasks"]
        active = [item for item in tasks if item.get("status") in ACTIVE]
        if len(active) + len(cleaned) > per_user * 2:
            raise ToolError(
                f"他已经有 {len(active)} 个任务在做或在排队了，装不下 {len(cleaned)} 个成员",
                code=ERROR_QUOTA,
                hint="先等前面的做完，或把项目拆小",
            )
        flags = record["flags"]
        used = int(flags.get("task_used") or 0) if flags.get("task_day") == today else 0
        if daily and used + len(cleaned) > daily:
            raise ToolError(
                f"今天的小助手任务额度不够了（每天 {daily} 个）",
                code=ERROR_QUOTA,
                hint="如实说明",
            )
        flags["task_day"] = today
        flags["task_used"] = used + len(cleaned)
        project_id = f"P{store.next_id(record)}"
        ids: dict[str, str] = {}
        for member in cleaned:
            ids[member["name"]] = f"t{store.next_id(record)}"
        roster = "、".join(
            f"{m['name']}（{ids[m['name']]}）：{m['goal'][:40]}" for m in cleaned
        )
        for member in cleaned:
            tasks.append(
                {
                    "id": ids[member["name"]],
                    "title": f"{title}·{member['name']}"[:24],
                    "goal": member["goal"],
                    "brief": (
                        f"{brief}\n这是项目「{title}」（总目标：{goal}）里你负责的部分。"
                        f"团队成员：{roster}。"
                    ).strip()[:2000],
                    "toolsets": member["tools"],
                    "save_to_notes": False,
                    "status": "queued",
                    "created_at": _fmt(now),
                    "progress": [],
                    "project": project_id,
                    "member": member["name"],
                    "depends_on": [ids[dep] for dep in member["depends_on"]],
                }
            )
        projects = record.setdefault("projects", [])
        projects.append(
            {
                "id": project_id,
                "title": title,
                "goal": goal,
                "members": [ids[m["name"]] for m in cleaned],
                "status": "running",
                "created_at": _fmt(now),
                "save_to_notes": bool(save_to_notes),
            }
        )
        del projects[:-MAX_PROJECTS]
        _trim(record)
        return {"project_id": project_id, "ids": ids}

    created = store.update(user_id, create)
    _bots[user_id] = bot
    started = []
    for member in cleaned:
        if not member["depends_on"]:
            task_id = created["ids"][member["name"]]
            _update_task(user_id, task_id, spawned=True)
            _spawn(user_id, task_id)
            started.append(member["name"])
    dbg(
        f"私聊用户 {user_id} 开了项目 {created['project_id']} 「{title}」 成员 {list(created['ids'])}"
    )
    return {
        "project_id": created["project_id"],
        "title": title,
        "members": [
            {
                "name": name,
                "task_id": task_id,
                "status": "已开工" if name in started else "等队友",
            }
            for name, task_id in created["ids"].items()
        ],
        "next": "简短告诉他已经组了个小队分头去做，做完会一起汇报；中途他补充要求就用 tasks message 转给对应成员",
    }


def _dependency_brief(record: dict[str, Any], task: dict[str, Any]) -> str:
    parts = []
    for dep_id in task.get("depends_on") or []:
        dep = _find(record, dep_id)
        if dep is None:
            continue
        name = dep.get("member") or dep_id
        if dep.get("status") == "done":
            files = " ".join(dep.get("files") or [])
            parts.append(
                f"【{name} 的成果】{dep.get('summary') or ''}\n{(dep.get('details') or '')[:1500]}"
                + (f"\n文件：{files}" if files else "")
            )
        else:
            parts.append(
                f"【{name} 没做成】{dep.get('error') or dep.get('status')}；你自己想办法补上缺的部分"
            )
    return "\n".join(parts)


async def _advance_project(user_id: int, project_id: str) -> None:
    """有成员结束：依赖都结束了的成员开工；全部结束就整体汇报一次。"""

    def plan(record: dict[str, Any]) -> tuple[list[str], bool]:
        project = _find_project(record, project_id)
        if project is None:
            return [], False
        members = [
            item
            for item in record.get("tasks", [])
            if item.get("project") == project_id
        ]
        ready: list[str] = []
        for item in members:
            if item.get("status") != "queued" or item.get("spawned"):
                continue
            deps = [_find(record, dep) for dep in item.get("depends_on") or []]
            if all(dep is None or dep.get("status") in TERMINAL for dep in deps):
                extra = _dependency_brief(record, item)
                if extra:
                    item["brief"] = f"{item.get('brief') or ''}\n{extra}".strip()[:6000]
                item["spawned"] = True
                ready.append(str(item["id"]))
        finished = bool(members) and all(
            item.get("status") in TERMINAL for item in members
        )
        if finished and project.get("status") == "running":
            project["status"] = "finished"
            project["finished_at"] = _fmt(now_beijing())
        else:
            finished = False
        return ready, finished

    ready, finished = store.update(user_id, plan)
    for task_id in ready:
        _spawn(user_id, task_id)
    if finished:
        await deliver_project_report(user_id, project_id)


def project_summary(user_id: int, project_id: str) -> dict[str, Any] | None:
    """把项目各成员的结果合成一个“任务”，交给主智能体汇报。"""

    record = store.load(user_id)
    project = _find_project(record, project_id)
    if project is None:
        return None
    members = [
        item for item in record.get("tasks", []) if item.get("project") == project_id
    ]
    done = [item for item in members if item.get("status") == "done"]
    files: list[str] = []
    for item in done:
        for ref in item.get("files") or []:
            if ref not in files:
                files.append(ref)
    lines = []
    for item in members:
        label = item.get("member") or item.get("id")
        if item.get("status") == "done":
            lines.append(f"{label}：{item.get('summary') or ''}")
        else:
            lines.append(
                f"{label}：没做成（{item.get('error') or STATUS_LABELS.get(str(item.get('status')))}）"
            )
    # 有依赖关系时，最后一个完成的成员通常是负责汇总的那个。
    lead = done[-1] if done else None
    return {
        "id": project_id,
        "title": project.get("title"),
        "status": "done" if done else "failed",
        "summary": (lead or {}).get("summary") or "；".join(lines)[:600],
        "details": (
            "\n".join(lines) + ("\n" + (lead or {}).get("details", "") if lead else "")
        )[:2500],
        "files": files[:6],
        "error": "成员都没做成" if not done else "",
        "project": True,
        "save_to_notes": project.get("save_to_notes"),
    }


async def deliver_project_report(user_id: int, project_id: str) -> bool:
    summary = project_summary(user_id, project_id)
    if summary is None or _reporter is None:
        return False
    if summary["status"] == "done" and summary.get("save_to_notes"):
        with contextlib.suppress(Exception):
            from .private_organizer import notes

            notes(
                user_id,
                {
                    "action": "add",
                    "content": f"「{summary['title']}」{summary['summary']}"[:300],
                    "tag": "小助手",
                },
                now_beijing(),
            )
    try:
        return bool(await _reporter(_bots.get(user_id), user_id, summary))
    except Exception:  # noqa: BLE001
        dbg_exc(f"私聊用户 {user_id} 项目 {project_id} 汇报失败")
        return False


def list_projects(user_id: int) -> list[dict[str, Any]]:
    record = store.load(user_id)
    rows = []
    for project in reversed(record.get("projects", [])):
        members = [
            view(item)
            for item in record.get("tasks", [])
            if item.get("project") == project.get("id")
        ]
        rows.append(
            {
                "id": project.get("id"),
                "title": project.get("title"),
                "status": project.get("status"),
                "members": members,
            }
        )
    return rows


def message_task(bot: Any, user_id: int, target: str, text: str) -> dict[str, Any]:
    """主智能体给小助手发消息（target 是任务编号、成员名或 all）。"""

    from . import private_team

    result = private_team.post(user_id, private_team.MAIN, target, text, bot=bot)
    return {**result, "note": "它下一步就能看到；要它回话时它会发到 helper_messages"}


# ── 进度与汇报 ────────────────────────────────────────────────────────────


def _quiet(user_id: int, now: datetime) -> bool:
    from .private_organizer import in_quiet_hours

    try:
        return in_quiet_hours(store.load(user_id).get("prefs") or {}, now)
    except Exception:  # noqa: BLE001
        return False


def progress_text(
    task: dict[str, Any], note: str, step: int | None, total: int | None
) -> str:
    counter = ""
    if step and total and step <= total:
        counter = f" {step}/{total}"
    elif step:
        counter = f" 第{step}步"
    return f"「{task.get('title')}」{counter}：{note}"


async def _on_progress(
    user_id: int, task_id: str, note: str, step: int | None, total: int | None
) -> None:
    now = now_beijing()

    def add(record: dict[str, Any]) -> tuple[dict[str, Any], int] | None:
        item = _find(record, task_id)
        if item is None:
            return None
        item.setdefault("progress", []).append(
            {"at": _fmt(now), "note": note, "step": step, "total": total}
        )
        item["progress"] = item["progress"][-MAX_PROGRESS_MESSAGES:]
        sent = int(item.get("progress_sent") or 0)
        if sent < MAX_PROGRESS_MESSAGES:
            item["progress_sent"] = sent + 1
        return dict(item), sent

    added = store.update(user_id, add)
    if added is None:
        return
    task, sent = added
    if sent >= MAX_PROGRESS_MESSAGES or _quiet(user_id, now) or _notifier is None:
        return
    text = progress_text(task, note, step, total)
    try:
        await _notifier(_bots.get(user_id), user_id, text)
    except Exception:  # noqa: BLE001
        dbg_exc(f"私聊用户 {user_id} 子任务进度发送失败(忽略)")


async def deliver_report(user_id: int, task_id: str, *, force: bool = False) -> bool:
    """把结束（或等回答）的任务交给主智能体转述；免打扰中先压着。"""

    task = get_task(user_id, task_id)
    if (
        task is None
        or task.get("status") not in {"done", "failed", "blocked"}
        or task.get("reported_state") == task.get("status")
    ):
        return False
    now = now_beijing()
    created = _parse(task.get("created_at")) or now
    if not force and _quiet(user_id, now) and now - created > QUIET_GRACE:
        _update_task(user_id, task_id, held=True)
        dbg(f"私聊用户 {user_id} 子任务 {task_id} 在免打扰时段完成，结束后再汇报")
        return False
    if _reporter is None:
        return False
    try:
        ok = await _reporter(_bots.get(user_id), user_id, task)
    except Exception:  # noqa: BLE001
        dbg_exc(f"私聊用户 {user_id} 子任务 {task_id} 汇报失败")
        ok = False
    if ok:
        _update_task(
            user_id,
            task_id,
            held=False,
            reported_state=task.get("status"),
            reported_at=_fmt(now),
        )
    return ok


async def flush_held(now: datetime) -> int:
    """调度器每 20 秒调一次：免打扰结束后补发压着的汇报。"""

    sent = 0
    for user_id in store.known_user_ids():
        if _quiet(user_id, now):
            continue
        record = store.load(user_id)
        for item in record.get("tasks", []):
            if item.get("held") and await deliver_report(
                user_id, str(item.get("id")), force=True
            ):
                sent += 1
    return sent


# ── 用户侧操作 ────────────────────────────────────────────────────────────


def cancel_task(user_id: int, task_id: str) -> dict[str, Any]:
    task = get_task(user_id, task_id)
    if task is None:
        raise ToolError(
            f"没有任务 {task_id}", code=ERROR_NOT_FOUND, hint="先用 tasks list 看编号"
        )
    if task.get("status") not in OPEN:
        return {
            "task_id": task_id,
            "status": STATUS_LABELS.get(str(task.get("status"))),
            "note": "任务已经结束了",
        }
    key = _key(user_id, task_id)
    runner = _running.pop(key, None)
    _contexts.pop(key, None)
    _finish(user_id, task_id, status="cancelled")
    if runner is not None and not runner.done():
        runner.cancel()
    if task.get("project"):
        # 项目成员被取消：等它的队友照常开工（会被告知它没做成），全部结束后整体汇报。
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(
                _advance_project(user_id, str(task["project"]))
            )
    return {"task_id": task_id, "status": "已取消"}


def cancel_all(user_id: int) -> int:
    count = 0
    for item in store.load(user_id).get("tasks", []):
        if item.get("status") in OPEN:
            with contextlib.suppress(ToolError):
                cancel_task(user_id, str(item["id"]))
                count += 1
    return count


def answer_task(bot: Any, user_id: int, task_id: str, answer: str) -> dict[str, Any]:
    task = get_task(user_id, task_id)
    if task is None:
        raise ToolError(
            f"没有任务 {task_id}", code=ERROR_NOT_FOUND, hint="先用 tasks list 看编号"
        )
    if task.get("status") != "blocked":
        raise ToolError("这个任务没有在等他回答", code=ERROR_INVALID_ARGS)
    _update_task(
        user_id,
        task_id,
        status="queued",
        question="",
        answer=answer[:500],
        reported_state=None,
    )
    _bots[user_id] = bot
    _spawn(user_id, task_id, answer=answer)
    return {"task_id": task_id, "status": "已转交，小助手接着做"}


def recover_on_startup() -> int:
    """进程刚起来时：上次没跑完的任务一律记失败（重启中断）。"""

    count = 0
    now = _fmt(now_beijing())
    for user_id in store.known_user_ids():

        def fix(record: dict[str, Any]) -> int:
            changed = 0
            for item in record.get("tasks", []):
                if item.get("status") in ACTIVE:
                    item.update(
                        status="failed",
                        error="Bot 重启，任务中断了",
                        finished_at=now,
                        reported_state="failed",
                    )
                    changed += 1
                elif item.get("status") == "blocked":
                    # 上下文在内存里已经没了，回答时会从头做。
                    item["context_lost"] = True
            return changed

        try:
            count += store.update(user_id, fix)
        except Exception:  # noqa: BLE001
            dbg_exc(f"私聊子任务恢复失败 用户 {user_id}")
    return count


def stats_today(now: datetime) -> dict[str, int]:
    today = now.date().isoformat()
    counts = {"created": 0, "running": 0, "queued": 0, "failed": 0, "done": 0}
    for user_id in store.known_user_ids():
        for item in store.load(user_id).get("tasks", []):
            if not str(item.get("created_at") or "").startswith(today):
                continue
            counts["created"] += 1
            status = str(item.get("status"))
            if status in counts:
                counts[status] += 1
    return counts


async def shutdown() -> None:
    for runner in list(_running.values()):
        runner.cancel()
    for runner in list(_running.values()):
        with contextlib.suppress(BaseException):
            await runner


def reset_state_for_tests() -> None:
    global _global_sem, _global_size  # noqa: PLW0603
    _running.clear()
    _contexts.clear()
    _bots.clear()
    _user_sems.clear()
    _global_sem = None
    _global_size = 0


__all__ = [
    "MAX_MEMBERS",
    "STATUS_LABELS",
    "answer_task",
    "cancel_all",
    "cancel_task",
    "context_digest",
    "deliver_project_report",
    "deliver_report",
    "flush_held",
    "get_task",
    "list_projects",
    "list_tasks",
    "message_task",
    "progress_text",
    "project_summary",
    "recover_on_startup",
    "register_reporter",
    "start_project",
    "start_task",
    "stats_today",
    "tasks_enabled",
    "view",
]
