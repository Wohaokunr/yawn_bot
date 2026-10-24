"""执行历史：有界 JSONL 持久化 + 内存索引 + 统计。

历史只保存诊断所需的截断副本：代码前 20KB、stdout/stderr 各后 4KB。
它属于 runner 自己的状态目录，用户代码（工作区 uid）无权读取。
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from collections import Counter, deque
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

MAX_CODE_IN_HISTORY = 20 * 1024
MAX_OUTPUT_IN_HISTORY = 4 * 1024
_COMPACT_EVERY = 500


def _tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return "…" + text[-limit:]


class RunHistory:
    def __init__(self, path: Path, *, limit: int) -> None:
        self.path = path
        self.limit = limit
        self._lock = threading.Lock()
        self._items: deque[dict[str, Any]] = deque(maxlen=limit)
        self._appended = 0
        self._load()

    def _load(self) -> None:
        try:
            with self.path.open(encoding="utf-8") as handle:
                for line in handle:
                    with contextlib.suppress(ValueError):
                        item = json.loads(line)
                        if isinstance(item, dict) and item.get("run_id"):
                            self._items.append(item)
        except OSError:
            return

    def record(self, result: dict[str, Any], code: str) -> dict[str, Any]:
        entry = {
            "run_id": result.get("run_id"),
            "workspace": result.get("workspace"),
            "status": result.get("status"),
            "exit_code": result.get("exit_code"),
            "signal": result.get("signal"),
            "started_at": result.get("started_at"),
            "finished_at": round(time.time(), 3),
            "duration_ms": result.get("duration_ms", 0),
            "queue_ms": result.get("queue_ms", 0),
            "cpu_ms": result.get("cpu_ms", 0),
            "max_rss_kb": result.get("max_rss_kb", 0),
            "message": result.get("message", ""),
            "meta": result.get("meta") or {},
            "limits": result.get("limits") or {},
            "files": [
                {key: item.get(key) for key in ("path", "size", "kind", "created")}
                for item in (result.get("files") or [])[:20]
            ],
            "language": result.get("language") or "python",
            "code": code[:MAX_CODE_IN_HISTORY],
            "code_truncated": len(code) > MAX_CODE_IN_HISTORY,
            "code_lines": code.count("\n") + 1,
            "stdout": _tail(str(result.get("stdout") or ""), MAX_OUTPUT_IN_HISTORY),
            "stderr": _tail(str(result.get("stderr") or ""), MAX_OUTPUT_IN_HISTORY),
        }
        with self._lock:
            self._items.append(entry)
            self._appended += 1
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
                if self._appended % _COMPACT_EVERY == 0:
                    self._compact_locked()
            except OSError:
                pass
        return entry

    def _compact_locked(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            for item in self._items:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        os.replace(temporary, self.path)

    def list(
        self,
        *,
        workspace: str | None = None,
        status: str | None = None,
        query: str | None = None,
        limit: int = 50,
        before: float | None = None,
    ) -> list[dict[str, Any]]:
        needle = (query or "").strip().casefold()
        rows: list[dict[str, Any]] = []
        with self._lock:
            items = list(self._items)
        for item in reversed(items):
            if workspace and item.get("workspace") != workspace:
                continue
            if status and item.get("status") != status:
                continue
            if before is not None and float(item.get("started_at") or 0) >= before:
                continue
            if needle:
                haystack = " ".join(
                    str(item.get(key) or "")
                    for key in ("code", "stdout", "stderr", "message", "run_id")
                ).casefold()
                purpose = str((item.get("meta") or {}).get("purpose") or "").casefold()
                if needle not in haystack and needle not in purpose:
                    continue
            rows.append(_summary(item))
            if len(rows) >= limit:
                break
        return rows

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            for item in reversed(self._items):
                if item.get("run_id") == run_id:
                    return dict(item)
        return None

    def forget_workspace(self, workspace: str) -> int:
        with self._lock:
            kept = [item for item in self._items if item.get("workspace") != workspace]
            removed = len(self._items) - len(kept)
            self._items = deque(kept, maxlen=self.limit)
            if removed:
                with contextlib.suppress(OSError):
                    self._compact_locked()
        return removed

    def stats(self, *, since_seconds: int = 86400) -> dict[str, Any]:
        now = time.time()
        cutoff = now - since_seconds
        with self._lock:
            items = [item for item in self._items if float(item.get("started_at") or 0) >= cutoff]
        statuses = Counter(str(item.get("status")) for item in items)
        workspaces = Counter(str(item.get("workspace")) for item in items)
        durations = sorted(int(item.get("duration_ms") or 0) for item in items)
        hourly = [0] * 24
        hourly_errors = [0] * 24
        for item in items:
            age_hours = int((now - float(item.get("started_at") or now)) // 3600)
            if 0 <= age_hours < 24:
                bucket = 23 - age_hours
                hourly[bucket] += 1
                if item.get("status") != "ok":
                    hourly_errors[bucket] += 1

        def _percentile(fraction: float) -> int:
            if not durations:
                return 0
            index = min(int(len(durations) * fraction), len(durations) - 1)
            return durations[index]

        return {
            "window_seconds": since_seconds,
            "total": len(items),
            "by_status": dict(statuses),
            "by_workspace": dict(workspaces.most_common(10)),
            "duration_p50_ms": _percentile(0.5),
            "duration_p95_ms": _percentile(0.95),
            "cpu_ms_total": sum(int(item.get("cpu_ms") or 0) for item in items),
            "max_rss_kb": max((int(item.get("max_rss_kb") or 0) for item in items), default=0),
            "files_created": sum(
                sum(1 for row in item.get("files") or [] if row.get("created"))
                for item in items
            ),
            "hourly": hourly,
            "hourly_errors": hourly_errors,
        }


def _summary(item: dict[str, Any]) -> dict[str, Any]:
    code = str(item.get("code") or "")
    first_line = next((line.strip() for line in code.splitlines() if line.strip()), "")
    return {
        key: item.get(key)
        for key in (
            "run_id",
            "workspace",
            "status",
            "exit_code",
            "signal",
            "started_at",
            "duration_ms",
            "cpu_ms",
            "max_rss_kb",
            "message",
            "meta",
            "code_lines",
        )
    } | {
        "preview": first_line[:120],
        "file_count": len(item.get("files") or []),
    }


__all__ = ["RunHistory"]
