# ruff: noqa: E501
"""群聊附件入箱的事件记录（WebUI「附件收件箱」与模型查已入箱文件共用）。

只是 ``data/agent_sandbox/inbox_log.jsonl`` 一个有界的 JSONL 文件：每次入箱
尝试一行，超过上限时整体截短。记录里只有群号、成员号、类型、清洗过的文件名、
大小、结果和工作区相对路径——**不记** URL、OneBot ``file`` 标识或本机路径。
成员退出 Agent 记忆时用 :func:`forget_member` 抹掉他相关的行。
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from collections import Counter
from typing import Any

from . import sandbox_policy
from .context import now_beijing

MAX_EVENTS = 1000
_TRIM_SLACK = 200
_lock = threading.Lock()
_EVENT_KEYS = (
    "at",
    "day",
    "group_id",
    "user_id",
    "sender_id",
    "message_id",
    "index",
    "media_type",
    "source",
    "name",
    "size",
    "status",
    "reason",
    "via",
    "path",
    "kind",
    "duration_ms",
    "trigger",
)


def _path() -> Any:
    # 每次现算：测试会 monkeypatch sandbox_policy.STATE_DIR。
    return sandbox_policy.STATE_DIR / "inbox_log.jsonl"


def _read_all() -> list[dict[str, Any]]:
    try:
        lines = _path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in lines:
        with contextlib.suppress(ValueError):
            item = json.loads(line)
            if isinstance(item, dict):
                events.append(item)
    return events


def _write_all(events: list[dict[str, Any]]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in events),
        encoding="utf-8",
    )
    temporary.replace(path)


def record_events(events: list[dict[str, Any]]) -> None:
    """追加若干条入箱事件；写失败只丢记录，不影响入箱本身。"""

    if not events:
        return
    now = time.time()
    day = now_beijing().strftime("%Y-%m-%d")
    rows = []
    for raw in events:
        row = {key: raw.get(key) for key in _EVENT_KEYS if raw.get(key) is not None}
        row.setdefault("at", round(now, 3))
        row.setdefault("day", day)
        rows.append(row)
    with _lock, contextlib.suppress(OSError):
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        existing = _read_all()
        if len(existing) > MAX_EVENTS + _TRIM_SLACK:
            _write_all(existing[-MAX_EVENTS:])


def recent_events(
    *,
    group_id: int | None = None,
    status: str | None = None,
    media_type: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """最新在前。"""

    with _lock:
        events = _read_all()
    output: list[dict[str, Any]] = []
    for item in reversed(events):
        if group_id is not None and item.get("group_id") != group_id:
            continue
        if status and item.get("status") != status:
            continue
        if media_type and item.get("media_type") != media_type:
            continue
        output.append(item)
        if len(output) >= limit:
            break
    return output


def imported_paths(group_id: int) -> dict[tuple[int, int], str]:
    """(消息 id, 附件序号) → 工作区路径；用于告诉模型“这个附件已经在电脑里了”。"""

    mapping: dict[tuple[int, int], str] = {}
    for item in reversed(recent_events(group_id=group_id, limit=MAX_EVENTS)):
        if item.get("status") not in {"imported", "reused"}:
            continue
        message_id = item.get("message_id")
        index = item.get("index")
        path = item.get("path")
        if isinstance(message_id, int) and isinstance(index, int) and isinstance(path, str):
            mapping[(message_id, index)] = path
    return mapping


def summary(*, day: str | None = None) -> dict[str, Any]:
    """今日（或指定日）的入箱统计：总数、成功/失败、按原因/类型/群。"""

    day = day or now_beijing().strftime("%Y-%m-%d")
    with _lock:
        events = [item for item in _read_all() if item.get("day") == day]
    statuses = Counter(str(item.get("status") or "unknown") for item in events)
    reasons = Counter(
        str(item.get("reason")) for item in events if item.get("status") == "failed" and item.get("reason")
    )
    types = Counter(str(item.get("media_type") or "other") for item in events)
    vias = Counter(str(item.get("via")) for item in events if item.get("via"))
    groups = Counter(str(item.get("group_id")) for item in events if item.get("group_id") is not None)
    return {
        "day": day,
        "total": len(events),
        "imported": statuses.get("imported", 0),
        "reused": statuses.get("reused", 0),
        "failed": statuses.get("failed", 0),
        "skipped": statuses.get("skipped", 0),
        "bytes": sum(
            int(item.get("size") or 0) for item in events if item.get("status") == "imported"
        ),
        "by_reason": dict(reasons.most_common()),
        "by_type": dict(types.most_common()),
        "by_via": dict(vias.most_common()),
        "by_group": dict(groups.most_common(20)),
    }


def forget_member(group_id: int, user_id: int) -> int:
    """隐私退出：删掉该成员在本群发起或发送的附件的入箱记录；返回删除行数。"""

    with _lock:
        events = _read_all()
        kept = [
            item
            for item in events
            if not (
                item.get("group_id") == group_id
                and user_id in {item.get("user_id"), item.get("sender_id")}
            )
        ]
        removed = len(events) - len(kept)
        if removed:
            with contextlib.suppress(OSError):
                _write_all(kept)
    return removed


def forget_paths(group_id: int, paths: set[str]) -> int:
    """工作区文件被删掉后，对应的“已入箱”记录不能再指向它。"""

    if not paths:
        return 0
    with _lock:
        events = _read_all()
        changed = 0
        for item in events:
            if item.get("group_id") == group_id and item.get("path") in paths and item.get("status") in {"imported", "reused"}:
                item["status"] = "removed"
                changed += 1
        if changed:
            with contextlib.suppress(OSError):
                _write_all(events)
    return changed


def forget_group_files(group_id: int) -> int:
    """工作区被清空：本群所有“已入箱”记录都不再指向真实文件。"""

    with _lock:
        events = _read_all()
        changed = 0
        for item in events:
            if item.get("group_id") == group_id and item.get("status") in {"imported", "reused"}:
                item["status"] = "removed"
                changed += 1
        if changed:
            with contextlib.suppress(OSError):
                _write_all(events)
    return changed


__all__ = [
    "MAX_EVENTS",
    "forget_group_files",
    "forget_member",
    "forget_paths",
    "imported_paths",
    "recent_events",
    "record_events",
    "summary",
]
