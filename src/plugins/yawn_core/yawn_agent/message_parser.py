# ruff: noqa: E501,F401,I001,TID252,PLR0912,PLR0913,PLR0917,C901,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100,PLR0911,SIM105
"""OneBot V11 消息归一化与嵌套引用解析。"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from typing import Any

from nonebot import logger
from nonebot.adapters.onebot.v11 import Message, MessageSegment

MAX_DEPTH = 4
MAX_FORWARD_NODES = 50
MAX_MEDIA_REFS = 20
MAX_TOTAL_BYTES = 256_000


@dataclass(slots=True)
class SegmentNode:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    children: list["SegmentNode"] = field(default_factory=list)
    depth: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ForwardNode:
    user_id: int | None = None
    nickname: str = "未知用户"
    content: str = ""
    segments: list[SegmentNode] = field(default_factory=list)
    children: list["ForwardNode"] = field(default_factory=list)
    depth: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class NormalizedMessage:
    plain_text: str
    segments: list[SegmentNode]
    media_refs: list[dict[str, Any]] = field(default_factory=list)
    reply_chain: list[dict[str, Any]] = field(default_factory=list)
    forward_tree: list[ForwardNode] = field(default_factory=list)
    mentions: list[int] = field(default_factory=list)
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "plain_text": self.plain_text,
            "segments": [item.as_dict() for item in self.segments],
            "media_refs": self.media_refs,
            "reply_chain": self.reply_chain,
            "forward_tree": [item.as_dict() for item in self.forward_tree],
            "mentions": self.mentions,
            "truncated": self.truncated,
        }

    def prompt_text(self) -> str:
        """生成不会把二进制或完整 API payload 带入提示词的文本。"""

        parts = [self.plain_text] if self.plain_text else []
        if self.media_refs:
            labels = ", ".join(str(item.get("type", "media")) for item in self.media_refs)
            parts.append(f"[媒体: {labels}]")
        if self.reply_chain:
            parts.append("[包含引用消息]")
        if self.forward_tree:
            parts.append("[包含转发消息]")
        return " ".join(parts) or "[非文本消息]"

    def storage_dict(self) -> dict[str, Any]:
        """Return a redacted form safe for long-lived database storage."""

        def redact(value: Any) -> Any:
            if isinstance(value, dict):
                output: dict[str, Any] = {}
                for key, item in value.items():
                    if key in {"url", "path"}:
                        continue
                    if key == "file":
                        # Keep a short opaque identifier, never a local path or
                        # signed remote URL.
                        text = str(item)
                        output[key] = text[-96:] if not text.startswith(("http://", "https://", "/", "\\")) else "[redacted]"
                        continue
                    output[key] = redact(item)
                return output
            if isinstance(value, list):
                return [redact(item) for item in value]
            return value

        return redact(self.as_dict())


def _safe_data(data: object) -> dict[str, Any]:
    return dict(data) if isinstance(data, dict) else {}


def _segment_placeholder(seg_type: str, data: dict[str, Any]) -> str:
    if seg_type == "at":
        return f"@{data.get('qq', '某人')}"
    if seg_type == "text":
        return str(data.get("text", ""))
    if seg_type == "image":
        return "[图片]"
    if seg_type == "file":
        return f"[文件: {data.get('name') or data.get('file') or '未命名'}]"
    if seg_type == "record":
        return "[语音]"
    if seg_type == "video":
        return "[视频]"
    if seg_type == "face":
        return f"[表情:{data.get('id', '')}]"
    if seg_type == "reply":
        return "[回复]"
    if seg_type in {"forward", "node"}:
        return "[转发消息]"
    return f"[{seg_type}]"


def _media_ref(seg_type: str, data: dict[str, Any]) -> dict[str, Any] | None:
    if seg_type not in {"image", "file", "record", "video"}:
        return None
    keys = ("url", "file", "file_id", "name", "size", "path")
    return {"type": seg_type, **{key: data[key] for key in keys if key in data}}


def normalize_message(message: Message | list[dict[str, Any]] | str) -> NormalizedMessage:
    """同步归一化本地 Message；嵌套 API 数据由 :func:`parse_message` 处理。"""

    if isinstance(message, Message):
        items = list(message)
    elif isinstance(message, str):
        items = list(Message(message))
    else:
        items = []
        for raw in message:
            if not isinstance(raw, dict):
                continue
            try:
                items.append(MessageSegment(str(raw.get("type", "text")), _safe_data(raw.get("data"))))
            except Exception:  # noqa: BLE001
                continue

    nodes: list[SegmentNode] = []
    text_parts: list[str] = []
    media: list[dict[str, Any]] = []
    mentions: list[int] = []
    total_bytes = 0
    truncated = False
    for segment in items:
        seg_type = str(segment.type)
        data = _safe_data(segment.data)
        raw_text = _segment_placeholder(seg_type, data)
        encoded_size = len(raw_text.encode("utf-8", errors="ignore"))
        if total_bytes + encoded_size > MAX_TOTAL_BYTES:
            truncated = True
            break
        total_bytes += encoded_size
        node = SegmentNode(seg_type, data, raw_text)
        nodes.append(node)
        if raw_text:
            text_parts.append(raw_text)
        if seg_type == "at":
            try:
                mentions.append(int(data["qq"]))
            except (KeyError, TypeError, ValueError):
                pass
        if len(media) < MAX_MEDIA_REFS:
            ref = _media_ref(seg_type, data)
            if ref:
                media.append(ref)
    return NormalizedMessage("".join(text_parts).strip(), nodes, media, mentions=mentions, truncated=truncated)


def _message_from_api(data: dict[str, Any]) -> Message:
    raw = data.get("raw_message")
    if isinstance(raw, str) and raw:
        return Message(raw)
    segments = data.get("message", [])
    if isinstance(segments, str):
        return Message(segments)
    message = Message()
    for item in segments if isinstance(segments, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            message.append(
                MessageSegment(
                    str(item.get("type", "text")),
                    _safe_data(item.get("data")),
                )
            )
        except Exception:  # noqa: BLE001
            logger.debug("跳过无法构建的嵌套消息段")
    return message


async def _call(bot: Any, action: str, **params: Any) -> dict[str, Any] | None:
    try:
        result = await asyncio.wait_for(bot.call_api(action, **params), timeout=2.0)
    except Exception:  # noqa: BLE001
        return None
    return result if isinstance(result, dict) else None


async def _expand_forward(
    bot: Any,
    node_data: dict[str, Any],
    *,
    depth: int,
    seen: set[int],
    budget: list[int],
) -> ForwardNode:
    user = node_data.get("user_id") or node_data.get("uin")
    try:
        user_id = int(user) if user is not None else None
    except (TypeError, ValueError):
        user_id = None
    nickname = str(node_data.get("nickname") or node_data.get("name") or "未知用户")
    content_data = node_data.get("content") or node_data.get("message") or []
    normalized = normalize_message(content_data)
    result = ForwardNode(user_id, nickname, normalized.prompt_text(), normalized.segments, depth=depth)
    if depth >= MAX_DEPTH or budget[0] >= MAX_FORWARD_NODES:
        return result
    result.children = await _expand_nested_nodes(bot, normalized.segments, depth + 1, seen, budget)
    return result


async def _expand_nested_nodes(
    bot: Any,
    segments: list[SegmentNode],
    depth: int,
    seen: set[int],
    budget: list[int],
) -> list[ForwardNode]:
    output: list[ForwardNode] = []
    for segment in segments:
        if segment.type not in {"forward", "node"} or depth > MAX_DEPTH or budget[0] >= MAX_FORWARD_NODES:
            continue
        if segment.type == "node":
            node_data = segment.data
            content = node_data.get("content") or node_data.get("message") or []
            normalized = normalize_message(content)
            budget[0] += 1
            raw_user_id = node_data.get("user_id")
            try:
                node_user_id = int(str(raw_user_id)) if raw_user_id is not None else None
            except (TypeError, ValueError):
                node_user_id = None
            output.append(
                ForwardNode(
                    user_id=node_user_id,
                    nickname=str(node_data.get("nickname") or "未知用户"),
                    content=normalized.prompt_text(),
                    segments=normalized.segments,
                    depth=depth,
                    children=await _expand_nested_nodes(bot, normalized.segments, depth + 1, seen, budget),
                )
            )
            continue
        message_id = segment.data.get("id") or segment.data.get("message_id")
        try:
            mid = int(str(message_id))
        except (TypeError, ValueError):
            continue
        if mid in seen:
            continue
        seen.add(mid)
        payload = await _call(bot, "get_forward_msg", id=mid)
        if not payload:
            continue
        messages = payload.get("messages") or payload.get("content") or []
        if isinstance(messages, dict):
            messages = [messages]
        for item in messages if isinstance(messages, list) else []:
            if not isinstance(item, dict) or budget[0] >= MAX_FORWARD_NODES:
                break
            budget[0] += 1
            output.append(await _expand_forward(bot, item, depth=depth, seen=seen, budget=budget))
    return output


async def parse_message(bot: Any, message: Message, *, max_depth: int = MAX_DEPTH) -> NormalizedMessage:
    """归一化并尽力展开 reply/forward；任何外部 API 失败均可降级。"""

    normalized = normalize_message(message)
    normalized.forward_tree = await _expand_nested_nodes(bot, normalized.segments, 1, set(), [0])
    reply_ids = [node.data.get("id") for node in normalized.segments if node.type == "reply"]
    seen: set[int] = set()
    for raw_id in reply_ids[:max_depth]:
        try:
            message_id = int(str(raw_id))
        except (TypeError, ValueError):
            continue
        if message_id in seen:
            continue
        seen.add(message_id)
        payload = await _call(bot, "get_msg", message_id=message_id)
        if not payload:
            continue
        reply = normalize_message(_message_from_api(payload))
        sender_data = payload.get("sender")
        sender = sender_data if isinstance(sender_data, dict) else {}
        normalized.reply_chain.append({
            "message_id": message_id,
            "user_id": sender.get("user_id"),
            "nickname": sender.get("nickname") or "未知用户",
            "text": reply.prompt_text(),
        })
    return normalized


__all__ = [
    "ForwardNode",
    "NormalizedMessage",
    "SegmentNode",
    "normalize_message",
    "parse_message",
]


