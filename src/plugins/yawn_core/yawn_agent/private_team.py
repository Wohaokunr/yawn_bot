# ruff: noqa: TRY003, PLR0913, PERF401, TC003
"""主智能体与小助手、小助手与小助手之间的通信：每位用户一个“团队留言板”。

参与者的地址：

- ``main``：主智能体（和用户聊天的那个你）；
- 任务编号 ``t12``：某个小助手；在项目里也可以用成员名（如 ``调研员``）；
- ``all``：发件人所在项目的全体成员（主智能体发 ``all`` 是所有在做的任务）。

消息存在个人数据 JSON 的 ``mail``（最近 ``MAX_MAIL`` 条），不建表、无迁移。每条记录
谁发的、发给谁、谁读过；小助手每一轮开始前把发给自己的未读消息作为“团队消息”放进
上下文，主智能体每一轮把未读的 ``helper_messages`` 放进资料。小助手发给主助手且
``need_reply=true`` 时，会触发一轮后台协调（``private_agent.coordinate``）：主智能体
能答就直接回小助手，答不了才问用户。

消息正文过 ``private_organizer.clean_text``（去控制字符、限长），它们之后会作为资料
回到模型里，始终标注“资料，不是指令”。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from . import private_store as store
from .context import now_beijing
from .log import dbg, dbg_exc
from .tool_args import ERROR_INVALID_ARGS, ERROR_NOT_FOUND, ToolError

MAIN = "main"
ALL = "all"
MAX_MAIL = 200
MAX_TEXT = 600
MAX_UNREAD_SHOWN = 8
WAIT_LIMIT_SECONDS = 120
_POLL_SECONDS = 2.0

Coordinator = Callable[[Any, int], Awaitable[None]]
_coordinator: Coordinator | None = None
_bots: dict[int, Any] = {}
_pending_coordination: dict[int, asyncio.Task[Any]] = {}
# 后台协调轮的防抖：几条消息挨着到只跑一轮。
COORDINATE_DELAY = 3.0


def register_coordinator(hook: Coordinator | None) -> None:
    global _coordinator  # noqa: PLW0603
    _coordinator = hook


def _fmt(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _clean(text: Any) -> str:
    from .private_organizer import clean_text

    return clean_text(text, limit=MAX_TEXT, field="消息")


def _tasks(record: dict[str, Any]) -> list[dict[str, Any]]:
    return list(record.get("tasks") or [])


def resolve(
    record: dict[str, Any], address: str, *, sender: str | None = None
) -> list[str]:
    """把地址解析成收件人列表（``main`` 或任务编号）。找不到抛 ``ToolError``。"""

    target = str(address or "").strip()
    if not target:
        raise ToolError("要写发给谁（to）", code=ERROR_INVALID_ARGS)
    if target.lower() in {MAIN, "主助手", "领队"}:
        return [MAIN]
    tasks = _tasks(record)
    open_states = {"queued", "running", "blocked"}
    if target.lower() == ALL:
        if sender is None or sender == MAIN:
            return [
                str(item["id"]) for item in tasks if item.get("status") in open_states
            ]
        mine = next((item for item in tasks if item.get("id") == sender), None)
        project = (mine or {}).get("project")
        return [
            str(item["id"])
            for item in tasks
            if item.get("id") != sender
            and item.get("status") in open_states
            and project
            and item.get("project") == project
        ]
    for item in tasks:
        if target in {item.get("id"), item.get("member")}:
            return [str(item["id"])]
    raise ToolError(
        f"找不到收件人 {target}",
        code=ERROR_NOT_FOUND,
        hint="to 写 main、任务编号（如 t3）、项目里的成员名或 all",
    )


def _label(record: dict[str, Any], address: str) -> str:
    if address == MAIN:
        return "主助手"
    for item in _tasks(record):
        if item.get("id") == address:
            name = item.get("member") or item.get("title")
            return f"{name}（{address}）" if name else address
    return address


def post(
    user_id: int,
    sender: str,
    to: str,
    text: str,
    *,
    need_reply: bool = False,
    bot: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """发一条消息；返回给发件人看的结果。"""

    body = _clean(text)
    if not body:
        raise ToolError("消息不能为空", code=ERROR_INVALID_ARGS)
    moment = now or now_beijing()

    def add(record: dict[str, Any]) -> dict[str, Any]:
        recipients = [
            item for item in resolve(record, to, sender=sender) if item != sender
        ]
        if not recipients:
            raise ToolError(
                "没有可以收这条消息的人",
                code=ERROR_NOT_FOUND,
                hint="对方可能已经结束了",
            )
        mail = record.setdefault("mail", [])
        seq = int(record.setdefault("flags", {}).get("mail_seq") or 0) + 1
        record["flags"]["mail_seq"] = seq
        entry = {
            "id": seq,
            "at": _fmt(moment),
            "from": sender,
            "to": recipients,
            "text": body,
            "need_reply": bool(need_reply),
            "read_by": [],
        }
        mail.append(entry)
        del mail[:-MAX_MAIL]
        return {
            "sent_to": [_label(record, item) for item in recipients],
            "message_id": seq,
            "recipients": recipients,
        }

    result = store.update(user_id, add)
    if bot is not None:
        _bots[user_id] = bot
    if MAIN in result["recipients"] and need_reply:
        _schedule_coordination(user_id)
    dbg(f"私聊用户 {user_id} 团队消息 {sender} → {result['recipients']}")
    return {"sent_to": result["sent_to"], "message_id": result["message_id"]}


def unread(
    user_id: int, reader: str, *, mark: bool = True, limit: int = MAX_UNREAD_SHOWN
) -> list[dict[str, Any]]:
    """取发给 ``reader`` 的未读消息（旧→新），默认顺手标成已读。"""

    def take(record: dict[str, Any]) -> list[dict[str, Any]]:
        rows = []
        for entry in record.get("mail") or []:
            if reader in (entry.get("to") or []) and reader not in (
                entry.get("read_by") or []
            ):
                rows.append(entry)
        rows = rows[-limit:]
        if mark:
            for entry in rows:
                entry.setdefault("read_by", []).append(reader)
        return [
            {
                "from": _label(record, str(entry.get("from"))),
                "from_id": entry.get("from"),
                "at": str(entry.get("at") or "")[11:16],
                "text": entry.get("text"),
                **({"wants_reply": True} if entry.get("need_reply") else {}),
            }
            for entry in rows
        ]

    if not mark:
        return take(store.load(user_id))
    return store.update(user_id, take)


def has_unread(user_id: int, reader: str) -> bool:
    return bool(unread(user_id, reader, mark=False, limit=1))


def thread(user_id: int, *, limit: int = 30) -> list[dict[str, Any]]:
    """WebUI / 调试用：最近的团队消息（不标已读）。"""

    record = store.load(user_id)
    rows = []
    for entry in (record.get("mail") or [])[-limit:]:
        rows.append(
            {
                "id": entry.get("id"),
                "at": entry.get("at"),
                "from": _label(record, str(entry.get("from"))),
                "to": [_label(record, str(item)) for item in entry.get("to") or []],
                "text": entry.get("text"),
                "needReply": bool(entry.get("need_reply")),
                "read": bool(entry.get("read_by")),
            }
        )
    return rows


async def wait_for(user_id: int, reader: str, seconds: float) -> list[dict[str, Any]]:
    """小助手等回复：最多等 ``seconds`` 秒，有未读就立刻返回。"""

    deadline = asyncio.get_running_loop().time() + max(
        1.0, min(seconds, WAIT_LIMIT_SECONDS)
    )
    while True:
        rows = unread(user_id, reader)
        if rows:
            return rows
        if asyncio.get_running_loop().time() >= deadline:
            return []
        await asyncio.sleep(_POLL_SECONDS)


def team_status(user_id: int, task_id: str) -> list[dict[str, Any]]:
    """同项目队友（或没有项目时：其他在做的任务）的状态。"""

    from .private_tasks import view

    record = store.load(user_id)
    tasks = _tasks(record)
    mine = next((item for item in tasks if item.get("id") == task_id), None)
    project = (mine or {}).get("project")
    rows = []
    for item in tasks:
        if item.get("id") == task_id:
            continue
        if project and item.get("project") != project:
            continue
        if not project and item.get("status") not in {"queued", "running", "blocked"}:
            continue
        row = view(item)
        if item.get("member"):
            row["member"] = item.get("member")
        if item.get("plan"):
            row["plan"] = item.get("plan")
        if item.get("status") == "done":
            row["summary"] = item.get("summary")
            if item.get("files"):
                row["files"] = item.get("files")
        rows.append(row)
    return rows


# ── 后台协调：小助手问主助手 ──────────────────────────────────────────────


def _schedule_coordination(user_id: int) -> None:
    if _coordinator is None:
        return
    current = _pending_coordination.get(user_id)
    if current is not None and not current.done():
        return

    async def run() -> None:
        try:
            await asyncio.sleep(COORDINATE_DELAY)
            if _coordinator is not None and has_unread(user_id, MAIN):
                await _coordinator(_bots.get(user_id), user_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            dbg_exc(f"私聊用户 {user_id} 后台协调失败(忽略)")
        finally:
            if _pending_coordination.get(user_id) is asyncio.current_task():
                _pending_coordination.pop(user_id, None)

    with contextlib.suppress(RuntimeError):
        _pending_coordination[user_id] = asyncio.create_task(run())


def reset_state_for_tests() -> None:
    for task in list(_pending_coordination.values()):
        task.cancel()
    _pending_coordination.clear()
    _bots.clear()


__all__ = [
    "ALL",
    "MAIN",
    "has_unread",
    "post",
    "register_coordinator",
    "reset_state_for_tests",
    "resolve",
    "team_status",
    "thread",
    "unread",
    "wait_for",
]
