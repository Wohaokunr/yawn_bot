# ruff: noqa: E501, PLR2004
"""把每次真实发给模型的完整请求挂进执行追踪。

``llm.py`` 在每次非流式补全结束后通知观察者，这里把 *实际发出去的* messages、tools、
请求参数和模型回复复制一份，挂到当前 ``ExecutionTrace`` 上（WebUI「完整提示词」）。

约束（见 AGENTS.md「执行追踪」）：

- 只在内存里、有硬上限：每条 Trace 最多 ``MAX_SNAPSHOTS_PER_TRACE`` 次请求，单次请求
  最多 ``MAX_SNAPSHOT_CHARS`` 字符，全进程最多 ``MAX_TRACES_WITH_PROMPTS`` 条 Trace 保留
  完整提示词，超出时最旧的只保留摘要。
- 图片/文件内容块只留类型、大小和摘要哈希，不留 base64 或链接；文本里的链接与本机路径
  按 Trace 同一规则脱敏，并在快照里记下脱敏了几处。除此之外逐字保留。
- ``AGENT_TRACE_PROMPT_CAPTURE=off`` 时完全不抓取。抓取失败绝不影响对话。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import uuid
from collections import OrderedDict, deque
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from .context import now_beijing
from .context_budget import estimate_tokens
from .execution_trace import ExecutionTrace, current_execution_trace
from .prompt_sections import (
    current_private_pipeline,
    current_private_user,
    get_section,
    locate_sections,
    manifest_for,
    resolve_override,
)

MAX_SNAPSHOTS_PER_TRACE = 16
MAX_SNAPSHOT_CHARS = 320_000
MAX_TRACES_WITH_PROMPTS = 48
MAX_BACKGROUND_SNAPSHOTS = 16
MAX_RESPONSE_CHARS = 60_000
# 私聊请求按用户分开保存：每人最近几次、最多几位用户（最久没请求的先丢）。
MAX_PRIVATE_SNAPSHOTS_PER_USER = 8
MAX_PRIVATE_USERS = 64
_URL_RE = re.compile(r"https?://[^\s\]\[\)\(\}\{<>\"'，。、]+", re.IGNORECASE)
_WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:[\\/][^\s\]\[\)\(\}\{<>\"'，。]+")
_POSIX_PATH_RE = re.compile(
    r"(?<![\w:./])/(?:home|root|mnt|tmp|var|opt|usr|etc|srv|data|app|Users)/[^\s\]\[\)\(\}\{<>\"'，。]+"
)
_DATA_URL_RE = re.compile(r"^data:([\w/+.-]+)?;base64,", re.IGNORECASE)
_MEDIA_KEYS = frozenset({"url", "image_url", "file_data", "data", "input_audio"})

_lock = threading.Lock()
_holders: OrderedDict[str, ExecutionTrace] = OrderedDict()
_background: deque[dict[str, Any]] = deque(maxlen=MAX_BACKGROUND_SNAPSHOTS)
_private: OrderedDict[int, deque[dict[str, Any]]] = OrderedDict()
_private_paused = False


def capture_enabled() -> bool:
    return os.environ.get("AGENT_TRACE_PROMPT_CAPTURE", "full").strip().lower() not in {
        "off",
        "false",
        "0",
        "none",
        "disabled",
    }


class _Redactor:
    def __init__(self) -> None:
        self.count = 0

    def text(self, value: str) -> str:
        def url(match: re.Match[str]) -> str:
            self.count += 1
            host = urlsplit(match.group(0)).hostname or "?"
            return f"[链接:{host[:60]}]"

        def path(_match: re.Match[str]) -> str:
            self.count += 1
            return "[本机路径]"

        text = value.replace("\x00", "")
        text = _URL_RE.sub(url, text)
        text = _WINDOWS_PATH_RE.sub(path, text)
        return _POSIX_PATH_RE.sub(path, text)

    def media(self, value: Any) -> str:
        """图片/文件块：只留类型、大小和哈希。"""

        self.count += 1
        raw = value.get("url") if isinstance(value, Mapping) else value
        text = str(raw or "")
        match = _DATA_URL_RE.match(text)
        if match:
            payload = text[match.end() :]
            try:
                size = len(base64.b64decode(payload[:4_000_000], validate=False))
            except (ValueError, TypeError):
                size = len(payload) * 3 // 4
            digest = hashlib.sha256(payload.encode("ascii", "ignore")).hexdigest()[:12]
            return f"[内嵌媒体 {match.group(1) or 'binary'} · {size / 1024:.1f}KB · sha256:{digest}]"
        if text.startswith(("http://", "https://")):
            return f"[媒体链接:{urlsplit(text).hostname or '?'}]"
        return f"[媒体 {len(text)} 字符]"


def _jsonable(value: Any, redactor: _Redactor, *, depth: int = 0, key: str = "") -> Any:
    if depth > 12:
        return "[嵌套过深]"
    if hasattr(value, "model_dump"):
        try:
            value = value.model_dump(exclude_none=True)
        except Exception:  # noqa: BLE001
            value = str(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redactor.text(value)
    if isinstance(value, Mapping):
        part_type = str(value.get("type") or "")
        output: dict[str, Any] = {}
        for item_key, item in value.items():
            name = str(item_key)
            if part_type and part_type != "text" and name in _MEDIA_KEYS:
                output[name] = redactor.media(item)
            else:
                output[name] = _jsonable(item, redactor, depth=depth + 1, key=name)
        return output
    if isinstance(value, (list, tuple)):
        return [_jsonable(item, redactor, depth=depth + 1, key=key) for item in value]
    return redactor.text(str(value))


def message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, Mapping) and part.get("type") == "text"
        )
    return str(content or "")


def _shrink(messages: list[dict[str, Any]], tools_chars: int) -> tuple[int, bool]:
    """总字符超限时从最长的文本开始截断，并就地写明截掉了多少。"""

    def total() -> int:
        return tools_chars + sum(len(message_text(item)) for item in messages)

    omitted = 0
    guard = 0
    while total() > MAX_SNAPSHOT_CHARS and guard < 64:
        guard += 1
        longest = max(messages, key=lambda item: len(message_text(item)), default=None)
        if longest is None:
            break
        text = message_text(longest)
        keep = max(len(text) - (total() - MAX_SNAPSHOT_CHARS) - 64, len(text) // 2, 200)
        if keep >= len(text):
            break
        cut = len(text) - keep
        omitted += cut
        replacement = f"{text[:keep]}…[超出单次快照上限，截掉 {cut} 字符]"
        if isinstance(longest.get("content"), list):
            parts = [
                part
                for part in longest["content"]
                if not (isinstance(part, Mapping) and part.get("type") == "text")
            ]
            longest["content"] = [{"type": "text", "text": replacement}, *parts]
        else:
            longest["content"] = replacement
    return omitted, omitted > 0


def _response_payload(response: Any, redactor: _Redactor) -> dict[str, Any] | None:
    if response is None:
        return None
    choices = list(getattr(response, "choices", None) or [])
    choice = choices[0] if choices else None
    message = getattr(choice, "message", None)
    content = str(getattr(message, "content", "") or "")
    reasoning = ""
    for name in ("reasoning_content", "reasoning"):
        value = getattr(message, name, None)
        if value is None:
            extra = getattr(message, "model_extra", None) or {}
            value = extra.get(name) if isinstance(extra, Mapping) else None
        if isinstance(value, str) and value.strip():
            reasoning = value
            break
    calls = []
    for call in list(getattr(message, "tool_calls", None) or []):
        function = getattr(call, "function", None)
        calls.append(
            {
                "id": str(getattr(call, "id", "") or ""),
                "name": str(getattr(function, "name", "") or ""),
                "arguments": redactor.text(
                    str(getattr(function, "arguments", "") or "")
                )[:MAX_RESPONSE_CHARS],
            }
        )
    usage = getattr(response, "usage", None)
    usage_payload: dict[str, Any] = {}
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = getattr(usage, name, None)
        if isinstance(value, int):
            usage_payload[name] = value
    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", None)
    if isinstance(cached, int):
        usage_payload["cached_tokens"] = cached
    reasoning_details = getattr(usage, "completion_tokens_details", None)
    reasoning_tokens = getattr(reasoning_details, "reasoning_tokens", None)
    if isinstance(reasoning_tokens, int):
        usage_payload["reasoning_tokens"] = reasoning_tokens
    return {
        "id": str(getattr(response, "id", "") or "") or None,
        "model": str(getattr(response, "model", "") or "") or None,
        "finishReason": str(getattr(choice, "finish_reason", "") or "") or None,
        "content": redactor.text(content)[:MAX_RESPONSE_CHARS],
        "contentChars": len(content),
        "reasoning": redactor.text(reasoning)[:MAX_RESPONSE_CHARS]
        if reasoning
        else None,
        "reasoningChars": len(reasoning) if reasoning else 0,
        "toolCalls": calls,
        "usage": usage_payload,
    }


def _message_stats(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stats = []
    for index, message in enumerate(messages):
        text = message_text(message)
        content = message.get("content")
        media = sum(
            1
            for part in (content if isinstance(content, list) else [])
            if isinstance(part, Mapping) and part.get("type") not in {None, "text"}
        )
        stats.append(
            {
                "index": index,
                "role": str(message.get("role") or ""),
                "chars": len(text),
                "tokens": estimate_tokens(text) if text else 0,
                "media": media,
                "toolCalls": len(list(message.get("tool_calls") or [])),
            }
        )
    return stats


def _annotate(
    spans: list[dict[str, Any]], group_id: int | None
) -> list[dict[str, Any]]:
    output = []
    for span in spans:
        section_id = str(span.get("sectionId") or "")
        section = get_section(section_id)
        resolved = resolve_override(section_id, group_id)
        output.append(
            {
                **span,
                "title": section.title if section is not None else section_id,
                "category": section.category if section is not None else None,
                "safety": bool(section.safety) if section is not None else False,
                "mode": resolved.override.mode
                if resolved.override is not None
                else "default",
                "scope": resolved.scope,
            }
        )
    return output


def build_snapshot(
    record: Mapping[str, Any], *, round_index: int, group_id: int | None = None
) -> dict[str, Any]:
    redactor = _Redactor()
    raw_messages = list(record.get("messages") or [])
    messages = [
        item if isinstance(item, dict) else {"role": "unknown", "content": str(item)}
        for item in _jsonable(raw_messages, redactor)
    ]
    tools = _jsonable(list(record.get("tools") or []), redactor)
    tools_json = json.dumps(tools, ensure_ascii=False, separators=(",", ":"))
    omitted, truncated = _shrink(messages, len(tools_json))
    spans = locate_sections(messages, known=manifest_for(raw_messages))
    params = _jsonable(dict(record.get("params") or {}), redactor)
    tool_names = [
        str(item.get("function", {}).get("name") or "")
        for item in tools
        if isinstance(item, Mapping)
    ]
    message_stats = _message_stats(messages)
    return {
        "id": uuid.uuid4().hex[:12],
        "round": round_index,
        "capturedAt": now_beijing().isoformat(timespec="milliseconds"),
        "kind": record.get("kind"),
        "task": record.get("task"),
        "provider": record.get("provider"),
        "profile": record.get("profile"),
        "model": record.get("model"),
        "outcome": record.get("outcome"),
        "durationMs": round(float(record.get("duration_ms") or 0.0), 1),
        "timeoutSeconds": record.get("timeout"),
        "params": params,
        "messages": messages,
        "messageStats": message_stats,
        "tools": tools,
        "toolNames": tool_names,
        "sections": _annotate(spans, group_id),
        "response": _response_payload(record.get("response"), redactor),
        "totals": {
            "messages": len(messages),
            "chars": sum(item["chars"] for item in message_stats),
            "tokens": sum(item["tokens"] for item in message_stats),
            "toolsChars": len(tools_json) if tools else 0,
            "toolsTokens": estimate_tokens(tools_json) if tools else 0,
            "byRole": {
                role: sum(
                    item["tokens"] for item in message_stats if item["role"] == role
                )
                for role in dict.fromkeys(item["role"] for item in message_stats)
            },
        },
        "redactions": redactor.count,
        "truncated": truncated,
        "omittedChars": omitted,
    }


def _hold(trace: ExecutionTrace) -> None:
    with _lock:
        _holders[trace.trace_id] = trace
        _holders.move_to_end(trace.trace_id)
        while len(_holders) > MAX_TRACES_WITH_PROMPTS:
            _old_id, old = _holders.popitem(last=False)
            if old.prompts:
                old.prompts_evicted = len(old.prompts)
                old.prompts.clear()


def observe_request(record: dict[str, Any]) -> None:
    """``llm`` 请求观察者：有活动 Trace 时挂到 Trace，Agent 后台任务进全局小缓冲。"""

    if not capture_enabled():
        return
    task = str(record.get("task") or "")
    trace = current_execution_trace()
    private_user = current_private_user()
    if trace is None and private_user is not None:
        _observe_private(record, private_user)
        return
    if trace is None:
        if not task.startswith("agent_"):
            return
        snapshot = build_snapshot(record, round_index=0)
        with _lock:
            _background.appendleft(snapshot)
        return
    if len(trace.prompts) >= MAX_SNAPSHOTS_PER_TRACE:
        trace.prompts_dropped += 1
        return
    round_index = sum(1 for item in trace.prompts if item.get("task") == task) + 1
    trace.prompts.append(
        build_snapshot(record, round_index=round_index, group_id=trace.group_id)
    )
    _hold(trace)


def _persona_source(user_id: int) -> dict[str, Any] | None:
    try:
        from .private_persona import resolve_user_persona

        resolved = resolve_user_persona(user_id)
    except Exception:  # noqa: BLE001
        return None
    return {
        "source": resolved.source,
        "label": resolved.label,
        "name": resolved.card.name,
        "templateName": resolved.template_name,
    }


def _observe_private(record: dict[str, Any], user_id: int) -> None:
    """私聊链路（主回合/汇报/协调/小助手/记忆整理）的请求进这位用户自己的环形缓冲。"""

    if _private_paused:
        return
    pipeline = current_private_pipeline()
    with _lock:
        ring = _private.get(user_id)
        round_index = (
            sum(
                1
                for item in list(ring or [])[:6]
                if item.get("pipeline") == pipeline
                and item.get("task") == record.get("task")
            )
            + 1
        )
    snapshot = build_snapshot(record, round_index=round_index)
    snapshot["userId"] = str(user_id)
    snapshot["pipeline"] = pipeline
    snapshot["persona"] = _persona_source(user_id)
    with _lock:
        ring = _private.get(user_id)
        if ring is None:
            ring = deque(maxlen=MAX_PRIVATE_SNAPSHOTS_PER_USER)
            _private[user_id] = ring
        ring.appendleft(snapshot)
        _private.move_to_end(user_id)
        while len(_private) > MAX_PRIVATE_USERS:
            _private.popitem(last=False)


def private_capture_paused() -> bool:
    return _private_paused


def set_private_capture_paused(paused: bool) -> bool:  # noqa: FBT001
    """WebUI 开关：暂停后私聊请求不再抓取（不影响群聊 Trace）。"""

    global _private_paused  # noqa: PLW0603
    _private_paused = bool(paused)
    return _private_paused


def private_prompt_users() -> list[dict[str, Any]]:
    """有抓取记录的私聊用户，最近请求的在前。"""

    with _lock:
        rows = [
            {
                "userId": str(uid),
                "count": len(ring),
                "lastAt": ring[0].get("capturedAt") if ring else None,
                "lastPipeline": ring[0].get("pipeline") if ring else None,
            }
            for uid, ring in _private.items()
        ]
    rows.reverse()
    return rows


def private_prompts(user_id: int) -> list[dict[str, Any]]:
    with _lock:
        return list(_private.get(int(user_id)) or [])


def clear_private_prompts(user_id: int | None = None) -> int:
    with _lock:
        if user_id is None:
            count = sum(len(ring) for ring in _private.values())
            _private.clear()
            return count
        ring = _private.pop(int(user_id), None)
        return len(ring or [])


def capture_unsent(  # noqa: PLR0913
    trace: ExecutionTrace,
    *,
    task: str,
    model: str | None,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """调试“只生成快照”时没有真实请求：按同一格式记一份“将要发送”的请求。"""

    snapshot = build_snapshot(
        {
            "kind": "unsent",
            "task": task,
            "model": model,
            "messages": messages,
            "tools": tools,
            "params": dict(params or {}),
            "outcome": "not_sent",
            "duration_ms": 0.0,
        },
        round_index=1,
        group_id=trace.group_id,
    )
    if len(trace.prompts) < MAX_SNAPSHOTS_PER_TRACE:
        trace.prompts.append(snapshot)
    return snapshot


def trace_prompts(trace: ExecutionTrace) -> dict[str, Any]:
    return {
        "traceId": trace.trace_id,
        "captureEnabled": capture_enabled(),
        "prompts": list(trace.prompts),
        "evicted": trace.prompts_evicted,
        "dropped": trace.prompts_dropped,
        "limits": {
            "perTrace": MAX_SNAPSHOTS_PER_TRACE,
            "snapshotChars": MAX_SNAPSHOT_CHARS,
            "tracesWithPrompts": MAX_TRACES_WITH_PROMPTS,
        },
    }


def background_prompts() -> list[dict[str, Any]]:
    with _lock:
        return list(_background)


def clear_background_prompts() -> int:
    with _lock:
        count = len(_background)
        _background.clear()
        return count


def install() -> None:
    from ..llm import add_request_observer  # noqa: TID252

    add_request_observer(observe_request)


__all__ = [
    "MAX_PRIVATE_SNAPSHOTS_PER_USER",
    "MAX_PRIVATE_USERS",
    "MAX_SNAPSHOTS_PER_TRACE",
    "MAX_SNAPSHOT_CHARS",
    "background_prompts",
    "build_snapshot",
    "capture_enabled",
    "capture_unsent",
    "clear_background_prompts",
    "clear_private_prompts",
    "install",
    "message_text",
    "observe_request",
    "private_capture_paused",
    "private_prompt_users",
    "private_prompts",
    "set_private_capture_paused",
    "trace_prompts",
]
