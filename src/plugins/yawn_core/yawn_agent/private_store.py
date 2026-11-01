# ruff: noqa: E501, TC003
"""私聊 Agent 的个人数据：笔记、待办、提醒、倒数日、心情、聊天偏好。

每位用户一个 JSON 文件（``data/private_agent/users/<QQ>.json``），不建表、不需要迁移；
写入走临时文件再替换，进程内一把锁串行化（私聊回合和后台调度器都会写）。

这里只放存取和条目上限，业务校验在 ``private_tools_ext``，调度在
``private_scheduler``。所有文本入库前由调用方过 ``memory_hygiene``。
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from .log import dbg

T = TypeVar("T")

# 每类条目的上限：超出时拒绝新增（笔记/待办/提醒/倒数日）或丢最旧（心情）。
LIMITS: dict[str, int] = {
    "notes": 100,
    "todos": 100,
    "reminders": 30,
    "countdowns": 20,
    "moods": 120,
}
SECTIONS = ("notes", "todos", "reminders", "countdowns", "moods")

_LOCK = threading.RLock()


def data_dir() -> Path:
    raw = os.environ.get("PRIVATE_AGENT_DATA_DIR", "").strip()
    return Path(raw) if raw else Path("data") / "private_agent" / "users"


def _path(user_id: int) -> Path:
    return data_dir() / f"{int(user_id)}.json"


def empty_record() -> dict[str, Any]:
    return {
        "version": 1,
        "next_id": 1,
        "prefs": {},
        "notes": [],
        "todos": [],
        "reminders": [],
        "countdowns": [],
        "moods": [],
        "flags": {},
        # 子智能体任务（private_tasks）：最近 MAX_TASKS 个，旧的自动丢弃
        "tasks": [],
    }


def _normalize(raw: Any) -> dict[str, Any]:
    record = empty_record()
    if not isinstance(raw, dict):
        return record
    for key, default in record.items():
        value = raw.get(key, default)
        if isinstance(default, list) and isinstance(value, list):
            record[key] = [item for item in value if isinstance(item, dict)]
        elif (isinstance(default, dict) and isinstance(value, dict)) or (isinstance(default, int) and isinstance(value, int)):
            record[key] = value
    return record


def load(user_id: int) -> dict[str, Any]:
    """读一位用户的数据；文件不存在或损坏时返回空记录（损坏只记日志，不抛）。"""

    path = _path(user_id)
    with _LOCK:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return empty_record()
        except (OSError, ValueError):
            dbg(f"私聊个人数据损坏，按空处理: {path}")
            return empty_record()
    return _normalize(raw)


def save(user_id: int, record: dict[str, Any]) -> None:
    path = _path(user_id)
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        tmp.replace(path)


def update(user_id: int, fn: Callable[[dict[str, Any]], T]) -> T:
    """读-改-写一次，``fn`` 原地修改记录并返回结果；整个过程持锁。"""

    with _LOCK:
        record = load(user_id)
        result = fn(record)
        save(user_id, record)
        return result


def next_id(record: dict[str, Any]) -> int:
    value = int(record.get("next_id") or 1)
    record["next_id"] = value + 1
    return value


def find(record: dict[str, Any], section: str, item_id: int) -> dict[str, Any] | None:
    for item in record.get(section, []):
        if int(item.get("id") or 0) == int(item_id):
            return item
    return None


def delete_all(user_id: int) -> bool:
    """删掉这位用户的整份个人数据；返回是否真的删了文件。"""

    path = _path(user_id)
    with _LOCK:
        try:
            path.unlink()
        except FileNotFoundError:
            return False
    return True


def known_user_ids() -> list[int]:
    """有个人数据文件的用户（调度器扫描用）。"""

    try:
        names = [p.stem for p in data_dir().glob("*.json")]
    except OSError:
        return []
    return sorted(int(name) for name in names if name.isdigit())


def counts(record: dict[str, Any]) -> dict[str, int]:
    return {section: len(record.get(section, [])) for section in SECTIONS}


__all__ = [
    "LIMITS",
    "SECTIONS",
    "counts",
    "data_dir",
    "delete_all",
    "empty_record",
    "find",
    "known_user_ids",
    "load",
    "next_id",
    "save",
    "update",
]
