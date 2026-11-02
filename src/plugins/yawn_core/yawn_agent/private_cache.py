# ruff: noqa: E501, C901, PLR0912, PLR0913
"""私聊 Agent 的前缀缓存：锚定历史窗口、前缀指纹诊断、命中统计。

服务商（DeepSeek 等）的前缀缓存只认“从第一个字节起完全相同”的部分，所以私聊请求按
“越稳定越靠前”排列（见 ``private_agent.build_messages``）::

    system#1 规则与人设（几乎不变）
    system#2 他的长期资料（记忆整理后才变）
    历史（锚定窗口：起点很多轮不动）
    当前消息
    system#3 本轮资料（时间、待办、刚做完的事……每轮都变，放最后）

这里提供三样东西：

- ``anchored_window``：从历史里挑出这轮要带的部分。起点“锚定”——没超字符/条数预算就不挪；
  超了一次挪掉一大块（``drop_ratio``），而不是每轮挪一条（那样每轮第一条历史都不同，
  历史部分永远命中不了）。早先的超长消息按确定的规则只留头尾，同一条每轮裁出来一样。
- ``note_prefix``：记下每位用户每条链路上一轮的分层指纹，这轮对比出“哪一层变了”，
  写进 Trace，方便排查缓存为什么没命中。
- ``record_usage``：按链路累计服务商报告的命中/未命中 token，WebUI 可看。

状态全在进程内（重启后第一轮重新锚定，属可接受代价）。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

# 锚定状态最多记多少位用户（超过就丢最久没用的）。
MAX_USERS = 2048
# 每条链路保留最近多少次请求的命中明细。
RECENT_CALLS = 40
_CLIP_MARK = "\n…（中间省略 {n} 字）…\n"

_LOCK = threading.Lock()
# 用户 → (锚点指纹, 上一轮锚点到末尾有几条)。后者用来在内容重复（“嗯”“好”）时认出真正的那一条：
# 历史只会往后长，锚点离末尾的距离只增不减。
_ANCHORS: dict[int, tuple[str, int]] = {}
_PREFIXES: dict[tuple[int, str], dict[str, str]] = {}


def _fingerprint(item: dict[str, Any]) -> str:
    raw = f"{item.get('role')}\x00{item.get('content')}"
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


ANCHOR_SPAN = 3


def _anchor_key(items: list[dict[str, Any]], index: int) -> str:
    """锚点 = 起点起连续三条的指纹（“嗯”“好”这类短句单独一条很容易重复）。"""

    return ":".join(
        _fingerprint(items[position])
        for position in range(index, min(index + ANCHOR_SPAN, len(items)))
    )


def clip_message(text: str, limit: int) -> str:
    """超长消息只留头尾（头 2/3、尾 1/3）；同一条输入永远裁成同样的结果。"""

    value = str(text or "")
    if limit <= 0 or len(value) <= limit:
        return value
    head = (limit * 2) // 3
    tail = limit - head
    omitted = len(value) - head - tail
    return value[:head] + _CLIP_MARK.format(n=omitted) + value[-tail:]


@dataclass(slots=True)
class Window:
    messages: list[dict[str, str]]
    start: int
    moved: bool
    reason: str
    chars: int
    dropped: int = 0


def _fit_start(sizes: list[int], budget: int, max_count: int) -> int:
    """从新往旧装，返回在字符与条数预算内的最早起点（至少保留最后一条）。"""

    used = 0
    start = len(sizes)
    for index in range(len(sizes) - 1, -1, -1):
        if start < len(sizes) and (
            used + sizes[index] > budget or len(sizes) - index > max_count
        ):
            break
        used += sizes[index]
        start = index
    return start


def _user_start(items: list[dict[str, Any]], start: int) -> int:
    while start < len(items) - 1 and items[start].get("role") != "user":
        start += 1
    return start


def anchored_window(
    user_id: int | None,
    history: list[dict[str, Any]],
    *,
    budget: int,
    max_count: int,
    drop_ratio: float = 0.4,
    message_chars: int = 1500,
) -> Window:
    """挑出本轮要带的历史（保证以 user 开头），起点尽量和上一轮一样。

    ``user_id`` 为 None 时不记状态（每次都按预算从新往旧装，等同旧行为）。
    """

    items: list[dict[str, str]] = []
    last = len(history) - 1
    for index, item in enumerate(history):
        content = str(item.get("content") or "")
        if index != last:
            content = clip_message(content, message_chars)
        items.append({"role": str(item.get("role")), "content": content})
    if not items:
        return Window([], 0, moved=False, reason="empty", chars=0)
    sizes = [len(item["content"]) for item in items]
    budget = max(int(budget), 1)
    max_count = max(int(max_count), 2)
    start: int | None = None
    reason = "fresh"
    if user_id is not None:
        with _LOCK:
            anchor = _ANCHORS.get(user_id)
        if anchor:
            key, tail = anchor
            # 从后往前找“离末尾比上次远”的匹配（历史只会往后长）；同样长（汇报轮、
            # 等他说完后接话）时才接受和上次一样远的。
            for strict in (True, False):
                for index in range(len(items) - 1, -1, -1):
                    distance = len(items) - index
                    if (distance > tail if strict else distance == tail) and _anchor_key(
                        items, index
                    ) == key:
                        start = index
                        break
                if start is not None:
                    break
            if start is None:
                reason = "anchor_gone"
            elif sum(sizes[start:]) > budget or len(items) - start > max_count:
                reason = "over_budget"
                start = None
            else:
                reason = "kept"
    moved = start is None
    if start is None:
        if reason != "over_budget":
            # 第一次 / 重启后 / 换了新对话：按满预算装。
            start = _fit_start(sizes, budget, max_count)
        else:
            # 一次挪一大块：留出 drop_ratio 的余量，接下来很多轮起点都不用动。
            keep = 1.0 - min(max(float(drop_ratio), 0.2), 0.8)
            start = _fit_start(sizes, int(budget * keep), max(int(max_count * keep), 2))
    start = _user_start(items, start)
    kept = items[start:]
    if kept and kept[0]["role"] != "user":
        kept = []
    if user_id is not None and kept:
        key = _anchor_key(items, start)
        with _LOCK:
            _ANCHORS.pop(user_id, None)
            _ANCHORS[user_id] = (key, len(items) - start)
            while len(_ANCHORS) > MAX_USERS:
                _ANCHORS.pop(next(iter(_ANCHORS)))
    return Window(
        kept,
        start,
        moved,
        reason,
        sum(len(item["content"]) for item in kept),
        dropped=start,
    )


def forget_user(user_id: int) -> None:
    """清掉这位用户的锚点与指纹（清空数据、/新对话 后调用）。"""

    with _LOCK:
        _ANCHORS.pop(user_id, None)
        for key in [key for key in _PREFIXES if key[0] == user_id]:
            _PREFIXES.pop(key, None)


# ── 分层指纹 ────────────────────────────────────────────────────────────


def layer_digest(value: Any) -> str:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(value.encode("utf-8", "replace")).hexdigest()[:12]


# 从前往后的层次；某层变了，它后面的层即使没变也命中不了。
LAYERS = ("system", "tools", "profile", "history")


def note_prefix(user_id: int, pipeline: str, layers: dict[str, str]) -> dict[str, Any]:
    """和这位用户这条链路上一次请求比：第一处变化在哪一层。"""

    key = (int(user_id), pipeline)
    with _LOCK:
        previous = _PREFIXES.get(key)
        _PREFIXES[key] = dict(layers)
        while len(_PREFIXES) > MAX_USERS * 4:
            _PREFIXES.pop(next(iter(_PREFIXES)))
    if previous is None:
        return {"first": True, "changed": None, "stable_layers": 0}
    stable = 0
    changed = None
    for name in LAYERS:
        if previous.get(name) != layers.get(name):
            changed = name
            break
        stable += 1
    return {"first": False, "changed": changed, "stable_layers": stable}


# ── 命中统计 ────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _Stats:
    calls: int = 0
    reported: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    miss_tokens: int = 0
    recent: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=RECENT_CALLS)
    )


_STATS: dict[str, _Stats] = {}


def record_usage(
    pipeline: str,
    result: Any,
    *,
    user_id: int | None = None,
    round_index: int = 0,
    changed: str | None = None,
) -> dict[str, Any]:
    """累计一次请求的前缀缓存命中；服务商没报 usage 时只计次数。"""

    prompt = getattr(result, "prompt_tokens", None)
    cached = getattr(result, "cached_tokens", None)
    miss = getattr(result, "cache_miss_tokens", None)
    entry: dict[str, Any] = {
        "at": int(time.time()),
        "round": int(round_index),
    }
    if user_id is not None:
        entry["user_id"] = int(user_id)
    if changed:
        entry["changed"] = changed
    with _LOCK:
        stats = _STATS.setdefault(pipeline, _Stats())
        stats.calls += 1
        if isinstance(cached, int) or isinstance(miss, int):
            stats.reported += 1
            hit = int(cached or 0)
            missed = (
                int(miss) if isinstance(miss, int) else max(int(prompt or 0) - hit, 0)
            )
            stats.cached_tokens += hit
            stats.miss_tokens += missed
            stats.prompt_tokens += (
                int(prompt) if isinstance(prompt, int) else hit + missed
            )
            entry.update({"cached": hit, "miss": missed})
            total = hit + missed
            entry["hit_rate"] = round(hit / total, 3) if total else None
        stats.recent.append(entry)
    return entry


def usage_snapshot() -> dict[str, Any]:
    """各链路的累计命中率与最近明细（WebUI 用）。"""

    with _LOCK:
        pipelines = {}
        total_hit = total_miss = 0
        for name, stats in sorted(_STATS.items()):
            total = stats.cached_tokens + stats.miss_tokens
            total_hit += stats.cached_tokens
            total_miss += stats.miss_tokens
            pipelines[name] = {
                "calls": stats.calls,
                "reported": stats.reported,
                "promptTokens": stats.prompt_tokens,
                "cachedTokens": stats.cached_tokens,
                "missTokens": stats.miss_tokens,
                "hitRate": round(stats.cached_tokens / total, 4) if total else None,
                "recent": list(stats.recent)[-10:],
            }
        grand = total_hit + total_miss
        return {
            "pipelines": pipelines,
            "cachedTokens": total_hit,
            "missTokens": total_miss,
            "hitRate": round(total_hit / grand, 4) if grand else None,
            "anchoredUsers": len(_ANCHORS),
        }


def reset() -> None:
    """测试用：清空全部状态。"""

    with _LOCK:
        _ANCHORS.clear()
        _PREFIXES.clear()
        _STATS.clear()


__all__ = [
    "LAYERS",
    "Window",
    "anchored_window",
    "clip_message",
    "forget_user",
    "layer_digest",
    "note_prefix",
    "record_usage",
    "reset",
    "usage_snapshot",
]
