# ruff: noqa: E501,F401,I001,TID252,PLR0912,PLR0913,PLR0915,PLR0917,C901,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100,PLR0911,SIM105
"""OneBot V11 消息归一化与嵌套引用解析。"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from nonebot.adapters.onebot.v11 import Message, MessageSegment

from .log import dbg, dbg_exc

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11.event import Reply

MAX_DEPTH = 4
MAX_FORWARD_NODES = 50
MAX_MEDIA_REFS = 20
MAX_TOTAL_BYTES = 256_000
FORWARD_EXPAND_DEADLINE = 20.0
MAX_REPLY_ENTRY_CHARS = 240
MAX_REPLY_TOTAL_CHARS = 720
MAX_REPLY_NICKNAME_CHARS = 24
MAX_FORWARD_PROMPT_NODES = 12
MAX_FORWARD_PROMPT_CHARS = 1800
_KIB = 1024


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
    media_refs: list[dict[str, Any]] = field(default_factory=list)
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
    # 仅用于本进程内执行追踪；不进入 as_dict/storage_dict，因此不会形成
    # 第二份长期消息副本。
    parse_trace: list[dict[str, Any]] = field(default_factory=list)
    # Process-only trigger metadata. It is intentionally excluded from as_dict/storage_dict.
    trigger_source: str | None = None
    trigger_signals: dict[str, bool] = field(default_factory=dict)

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

    def intent_text(self) -> str:
        """Return only user-authored text segments for deterministic routing.

        ``plain_text`` intentionally contains placeholders such as ``[图片]``
        and ``@123`` for model context. Those placeholders describe the input
        message and must not be mistaken for a request to send an image or @ a
        member when choosing outbound tool schemas.
        """

        return "".join(
            str(item.data.get("text") or item.text or "")
            for item in self.segments
            if item.type == "text"
        ).strip()

    def prompt_text(self) -> str:
        """生成不会把二进制或完整 API payload 带入提示词的文本。"""

        parts: list[str] = []
        if self.reply_chain:
            # 引用链存储顺序为直接引用→最深层；提示词按时间顺序渲染
            # （最深层在前），让模型先读到被引用的上下文再读当前正文。
            quoted: list[str] = []
            used = 0
            for entry in reversed(self.reply_chain):
                nickname = str(entry.get("nickname") or "未知用户")[
                    :MAX_REPLY_NICKNAME_CHARS
                ]
                text = str(entry.get("text") or "").strip() or "[非文本消息]"
                if len(text) > MAX_REPLY_ENTRY_CHARS:
                    text = text[:MAX_REPLY_ENTRY_CHARS] + "…"
                line = f"[引用消息 {nickname}: {text}]"
                if used + len(line) > MAX_REPLY_TOTAL_CHARS:
                    quoted.append("[更早的引用已省略]")
                    break
                used += len(line)
                quoted.append(line)
            parts.extend(quoted)
        if self.plain_text:
            parts.append(self.plain_text)
        if self.media_refs:
            labels = ", ".join(
                f"{index}:{_media_prompt_label(item)}"
                for index, item in enumerate(self.media_refs, start=1)
            )
            parts.append(f"[媒体: {labels}]")
        if self.forward_tree:
            lines: list[str] = []
            used = 0

            def visit(nodes: list[ForwardNode]) -> None:
                nonlocal used
                for node in nodes:
                    if len(lines) >= MAX_FORWARD_PROMPT_NODES or used >= MAX_FORWARD_PROMPT_CHARS:
                        return
                    nickname = str(node.nickname or "未知用户")[:MAX_REPLY_NICKNAME_CHARS]
                    content = " ".join(str(node.content or "").split())[:360]
                    if not content:
                        content = "[非文本消息]"
                    line = f"{nickname}: {content}"
                    remaining = MAX_FORWARD_PROMPT_CHARS - used
                    if len(line) > remaining:
                        line = line[:remaining]
                    if line:
                        lines.append(line)
                        used += len(line)
                    visit(node.children)

            visit(self.forward_tree)
            if lines:
                suffix = "\n[更多转发节点已省略]" if len(lines) >= MAX_FORWARD_PROMPT_NODES else ""
                parts.append("[转发摘要]\n" + "\n".join(lines) + suffix)
            else:
                parts.append("[包含转发消息]")
        return " ".join(parts) or "[非文本消息]"

    def storage_dict(self) -> dict[str, Any]:
        """Return a redacted form safe for long-lived database storage."""

        def _is_local_file_ref(text: str) -> bool:
            # 含 Windows 盘符路径（C:\...），本机运行平台就是 win32。
            return text.startswith(("http://", "https://", "/", "\\")) or bool(
                re.match(r"^[A-Za-z]:[\\/]", text)
            )

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
                        output[key] = (
                            "[redacted]" if _is_local_file_ref(text) else text[-96:]
                        )
                        continue
                    output[key] = redact(item)
                return output
            if isinstance(value, list):
                return [redact(item) for item in value]
            return value

        stored = redact(self.as_dict())

        def scrub_segments(value: Any) -> None:
            if isinstance(value, dict):
                segment_type = str(value.get("type") or "").strip().lower()
                if segment_type in {"json", "xml"} and "data" in value:
                    # 卡片正文可能包含任意 URL/富文本/嵌套 payload；长期存储只保留
                    # “收到过这种段”的事实，不保留原始卡片内容。
                    value["data"] = {}
                    value["text"] = "[JSON卡片]" if segment_type == "json" else "[XML卡片]"
                for item in value.values():
                    scrub_segments(item)
            elif isinstance(value, list):
                for item in value:
                    scrub_segments(item)

        scrub_segments(stored)
        return stored


def _safe_data(data: object) -> dict[str, Any]:
    return dict(data) if isinstance(data, dict) else {}


def _first_reply_id(segments: Any) -> int | None:
    """OneBot 11 每条消息至多一个 reply 段;取第一个可解析的引用 id。"""

    for seg in segments:
        if getattr(seg, "type", None) != "reply":
            continue
        data = _safe_data(getattr(seg, "data", None))
        raw = data.get("id") or data.get("message_id")
        try:
            return int(str(raw))
        except (TypeError, ValueError):
            continue
    return None


def _node_sender(node_data: dict[str, Any]) -> tuple[int | None, str]:
    """兼容 go-cqhttp/NapCat 转发节点: 发送者可能在顶层字段或嵌套 sender dict。"""

    sender = node_data.get("sender")
    sender = sender if isinstance(sender, dict) else {}
    raw_user = (
        node_data.get("user_id")
        or node_data.get("uin")
        or sender.get("user_id")
        or sender.get("uin")
    )
    try:
        user_id = int(str(raw_user)) if raw_user is not None else None
    except (TypeError, ValueError):
        user_id = None
    nickname = str(
        node_data.get("nickname")
        or node_data.get("name")
        or sender.get("nickname")
        or sender.get("card")
        or "未知用户"
    )
    return user_id, nickname


def _segment_placeholder(seg_type: str, data: dict[str, Any]) -> str:
    if seg_type == "at":
        return f"@{data.get('qq', '某人')}"
    if seg_type == "text":
        return str(data.get("text", ""))
    if seg_type == "image":
        return "[图片]"
    if seg_type == "file":
        name = str(data.get("name") or data.get("file") or "未命名")[:120]
        size = _safe_media_size(data.get("size"))
        return f"[文件: {name}{f' · {size}' if size else ''}]"
    if seg_type == "record":
        return "[语音]"
    if seg_type == "video":
        return "[视频]"
    if seg_type == "face":
        return f"[表情:{data.get('id', '')}]"
    if seg_type == "reply":
        # 引用内容由 reply_chain 单独携带，占位符只会给提示词引入噪音。
        return ""
    if seg_type in {"forward", "node"}:
        return "[转发消息]"
    if seg_type == "share":
        title = str(data.get("title") or "分享链接")[:120]
        content = str(data.get("content") or "").strip()[:160]
        return f"[分享: {title}{f' · {content}' if content else ''}]"
    if seg_type == "contact":
        kind = str(data.get("type") or "联系人")[:24]
        return f"[联系人卡片: {kind}]"
    if seg_type == "location":
        title = str(data.get("title") or "位置")[:80]
        content = str(data.get("content") or "").strip()[:120]
        lat = _safe_coordinate(data.get("lat"), -90, 90)
        lon = _safe_coordinate(data.get("lon"), -180, 180)
        coordinate = f" {lat},{lon}" if lat is not None and lon is not None else ""
        return f"[位置: {title}{coordinate}{f' · {content}' if content else ''}]"
    if seg_type == "music":
        title = str(data.get("title") or data.get("type") or "音乐")[:100]
        return f"[音乐: {title}]"
    if seg_type == "json":
        return "[JSON卡片]"
    if seg_type == "xml":
        return "[XML卡片]"
    return f"[{seg_type}]"


def _safe_media_size(value: object) -> str | None:
    try:
        size = int(str(value)) if value is not None else 0
    except (TypeError, ValueError):
        return None
    if size <= 0:
        return None
    if size < _KIB:
        return f"{size} B"
    if size < _KIB * _KIB:
        return f"{size / _KIB:.1f} KiB"
    return f"{size / (_KIB * _KIB):.1f} MiB"


def _safe_coordinate(value: object, minimum: float, maximum: float) -> str | None:
    try:
        coordinate = float(str(value))
    except (TypeError, ValueError):
        return None
    if coordinate < minimum or coordinate > maximum:
        return None
    return f"{coordinate:.5f}"


def _media_prompt_label(ref: dict[str, Any]) -> str:
    media_type = str(ref.get("type") or "media")
    source = str(ref.get("source") or "current")
    source_label = {"current": "当前", "reply": "引用", "forward": "转发"}.get(
        source, "相关"
    )
    if media_type == "file":
        name = str(ref.get("name") or "文件")[:80]
        size = _safe_media_size(ref.get("size"))
        return f"{source_label}文件 {name}{f' {size}' if size else ''}"
    return f"{source_label}{ {'image': '图片', 'record': '语音', 'video': '视频'}.get(media_type, media_type) }"


def _media_ref(
    seg_type: str, data: dict[str, Any], *, source: str = "current"
) -> dict[str, Any] | None:
    if seg_type not in {"image", "file", "record", "video"}:
        return None
    keys = ("url", "file", "file_id", "name", "size", "path")
    return {
        "type": seg_type,
        "source": source,
        **{key: data[key] for key in keys if key in data},
    }


def _append_media_refs(
    target: list[dict[str, Any]],
    refs: list[dict[str, Any]],
    *,
    source: str,
    source_message_id: int | None = None,
    source_user_id: int | None = None,
) -> None:
    """按当前消息→引用→转发的优先级收集可处理媒体，并保持总量有界。"""

    for raw in refs:
        if len(target) >= MAX_MEDIA_REFS:
            return
        item = dict(raw)
        item["source"] = source
        if source_message_id is not None:
            item["source_message_id"] = source_message_id
        if source_user_id is not None:
            item["source_user_id"] = source_user_id
        target.append(item)


def _retag_media_refs(
    refs: list[dict[str, Any]],
    *,
    source: str,
) -> list[dict[str, Any]]:
    return [{**dict(item), "source": source} for item in refs]


def _collect_forward_media(nodes: list[ForwardNode]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []

    def visit(node: ForwardNode) -> None:
        if len(output) >= MAX_MEDIA_REFS:
            return
        _append_media_refs(
            output,
            node.media_refs,
            source="forward",
            source_user_id=node.user_id,
        )
        for child in node.children:
            visit(child)

    for node in nodes:
        visit(node)
    return output


def normalize_message(
    message: Message | list[dict[str, Any]] | str,
) -> NormalizedMessage:
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
                items.append(
                    MessageSegment(
                        str(raw.get("type", "text")), _safe_data(raw.get("data"))
                    )
                )
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
        if not truncated and total_bytes + encoded_size > MAX_TOTAL_BYTES:
            truncated = True
            dbg(f"消息归一化: 文本累计超过 {MAX_TOTAL_BYTES} 字节,后续文本段被截断")
        # 超过文本上限后停止累计文本，但继续收集媒体引用：
        # 长引用文本后面的图片不应被静默丢弃。
        if not truncated:
            total_bytes += encoded_size
            node = SegmentNode(seg_type, data, raw_text)
            nodes.append(node)
            if raw_text:
                text_parts.append(raw_text)
        # 与媒体引用同理: 截断后出现的 at 段仍要计入 mentions。
        if seg_type == "at":
            try:
                mentions.append(int(data["qq"]))
            except (KeyError, TypeError, ValueError):
                pass
        if len(media) < MAX_MEDIA_REFS:
            ref = _media_ref(seg_type, data)
            if ref:
                media.append(ref)
    return NormalizedMessage(
        "".join(text_parts).strip(),
        nodes,
        media,
        mentions=mentions,
        truncated=truncated,
    )


def _message_from_api(data: dict[str, Any]) -> Message:
    # 结构化 message 数组是 OneBot 11 get_msg 的规范形态,优先使用,
    # 避免 CQ 字符串转义边界问题;raw_message 仅作最后回退。
    segments = data.get("message")
    if isinstance(segments, list):
        message = Message()
        for item in segments:
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
                dbg("跳过无法构建的嵌套消息段")
        if message:
            return message
        dbg("消息解析: 结构化 message 数组为空,回退 raw_message")
    if isinstance(segments, str) and segments:
        return Message(segments)
    raw = data.get("raw_message")
    if isinstance(raw, str) and raw:
        return Message(raw)
    return Message()


async def _call(bot: Any, action: str, **params: Any) -> dict[str, Any] | None:
    try:
        result = await asyncio.wait_for(bot.call_api(action, **params), timeout=2.0)
    except Exception:  # noqa: BLE001
        dbg_exc(
            f"消息解析调用 {action} 失败(超时 2s 或 API 错误),降级跳过 params={params}"
        )
        return None
    if not isinstance(result, dict):
        dbg(f"消息解析调用 {action} 返回非 dict 结果,忽略: {type(result).__name__}")
        return None
    return result


async def _expand_forward(
    bot: Any,
    node_data: dict[str, Any],
    *,
    depth: int,
    seen: set[int],
    budget: list[int],
    deadline: float,
) -> ForwardNode:
    user_id, nickname = _node_sender(node_data)
    content_data = node_data.get("content") or node_data.get("message") or []
    normalized = normalize_message(content_data)
    normalized.media_refs = _retag_media_refs(normalized.media_refs, source="forward")
    result = ForwardNode(
        user_id,
        nickname,
        normalized.prompt_text(),
        normalized.segments,
        media_refs=normalized.media_refs,
        depth=depth,
    )
    if depth >= MAX_DEPTH or budget[0] >= MAX_FORWARD_NODES:
        dbg(
            f"转发展开: 节点 {nickname!r} 达到深度上限({depth}/{MAX_DEPTH})"
            f"或预算上限({budget[0]}/{MAX_FORWARD_NODES}),停止深入"
        )
        return result
    result.children = await _expand_nested_nodes(
        bot, normalized.segments, depth + 1, seen, budget, deadline
    )
    return result


async def _expand_nested_nodes(
    bot: Any,
    segments: list[SegmentNode],
    depth: int,
    seen: set[int],
    budget: list[int],
    deadline: float,
) -> list[ForwardNode]:
    output: list[ForwardNode] = []
    for segment in segments:
        if (
            segment.type not in {"forward", "node"}
            or depth > MAX_DEPTH
            or budget[0] >= MAX_FORWARD_NODES
        ):
            continue
        if segment.type == "node":
            node_data = segment.data
            content = node_data.get("content") or node_data.get("message") or []
            normalized = normalize_message(content)
            normalized.media_refs = _retag_media_refs(
                normalized.media_refs, source="forward"
            )
            budget[0] += 1
            node_user_id, node_nickname = _node_sender(node_data)
            output.append(
                ForwardNode(
                    user_id=node_user_id,
                    nickname=node_nickname,
                    content=normalized.prompt_text(),
                    segments=normalized.segments,
                    media_refs=normalized.media_refs,
                    depth=depth,
                    children=await _expand_nested_nodes(
                        bot, normalized.segments, depth + 1, seen, budget, deadline
                    ),
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
        # 聚合截止时间：嵌套转发的串行 API 调用不得无限拖住处理器。
        if time.monotonic() > deadline:
            dbg(f"转发展开: 达到 {FORWARD_EXPAND_DEADLINE}s 截止时间,停止展开")
            break
        payload = await _call(bot, "get_forward_msg", id=mid)
        if not payload:
            dbg(f"转发展开: get_forward_msg id={mid} 无结果,跳过")
            continue
        dbg(
            f"转发展开: get_forward_msg id={mid} 成功,depth={depth} budget={budget[0]}/{MAX_FORWARD_NODES}"
        )
        messages = payload.get("messages") or payload.get("content") or []
        if isinstance(messages, dict):
            messages = [messages]
        for item in messages if isinstance(messages, list) else []:
            if not isinstance(item, dict) or budget[0] >= MAX_FORWARD_NODES:
                break
            budget[0] += 1
            output.append(
                await _expand_forward(
                    bot, item, depth=depth, seen=seen, budget=budget, deadline=deadline
                )
            )
    return output


async def parse_message(
    bot: Any,
    message: Message,
    *,
    reply: Reply | None = None,
    max_depth: int = MAX_DEPTH,
) -> NormalizedMessage:
    """归一化并尽力展开 reply/forward；任何外部 API 失败均可降级。"""

    parse_started = time.monotonic()
    normalize_started = time.monotonic()
    normalized = normalize_message(message)
    normalized.parse_trace.append(
        {
            "label": "消息段归一化",
            "duration_ms": (time.monotonic() - normalize_started) * 1000,
            "output": {
                "segments": len(normalized.segments),
                "media_refs": len(normalized.media_refs),
                "mentions": len(normalized.mentions),
                "truncated": normalized.truncated,
            },
        }
    )
    dbg(
        f"消息归一化完成: 段数={len(normalized.segments)} "
        f"media={len(normalized.media_refs)} mentions={normalized.mentions} "
        f"截断={normalized.truncated} 文本={normalized.plain_text!r}"
    )
    deadline = time.monotonic() + FORWARD_EXPAND_DEADLINE
    forward_started = time.monotonic()
    normalized.forward_tree = await _expand_nested_nodes(
        bot, normalized.segments, 1, set(), [0], deadline
    )
    forward_media_refs: list[dict[str, Any]] = []
    if normalized.forward_tree:
        # 先暂存，等 reply 媒体收集完成后再加入。当前消息→直接引用→转发
        # 是固定优先级，避免超长转发抢光 MAX_MEDIA_REFS 让“这张图呢”看不到
        # 用户真正引用的图片。
        forward_media_refs = _collect_forward_media(normalized.forward_tree)
        dbg(f"转发展开完成: 顶层节点 {len(normalized.forward_tree)} 个")
    normalized.parse_trace.append(
        {
            "label": "合并转发展开",
            "duration_ms": (time.monotonic() - forward_started) * 1000,
            "output": {
                "top_level_nodes": len(normalized.forward_tree),
                "forward_media_refs": len(forward_media_refs),
            },
        }
    )
    reply_started = time.monotonic()
    # OneBot 11 每条消息至多一个 reply 段,沿被引用消息自身的 reply 段逐层深入。
    # 注意: 适配器的 _check_reply 会先行拉取直接引用到 event.reply,并把 reply
    # 段从 event.message 中移除;此时优先用 event.reply 种入第一层,再跟随
    # reply.message 内更深的引用 id;仅当没有 event.reply 时才扫描顶层 reply 段
    # (适配器 get_msg 失败时 reply 段会保留在消息里)。
    current_id: int | None = None
    seen: set[int] = set()
    if reply is not None:
        try:
            reply_message_id: int | None = int(reply.message_id)
        except (TypeError, ValueError):
            reply_message_id = None
        if reply_message_id is not None:
            seen.add(reply_message_id)
        reply_message = reply.message or Message()
        reply_sender = getattr(reply, "sender", None)
        reply_normalized = normalize_message(reply_message)
        reply_normalized.media_refs = _retag_media_refs(
            reply_normalized.media_refs, source="reply"
        )
        normalized.reply_chain.append(
            {
                "message_id": reply_message_id,
                "user_id": getattr(reply_sender, "user_id", None),
                "nickname": str(getattr(reply_sender, "nickname", None) or "未知用户"),
                "text": reply_normalized.prompt_text(),
                "media_types": [
                    str(item.get("type") or "media")
                    for item in reply_normalized.media_refs
                ][:8],
            }
        )
        _append_media_refs(
            normalized.media_refs,
            reply_normalized.media_refs,
            source="reply",
            source_message_id=reply_message_id,
            source_user_id=getattr(reply_sender, "user_id", None),
        )
        current_id = _first_reply_id(reply_message)
        dbg(
            f"回复链展开: 由 event.reply 种入第 1 层 id={reply_message_id!r}, "
            f"next_id={current_id!r}"
        )
    else:
        # 从原始 Message(而非截断后的 segments)取顶层 reply id。
        current_id = _first_reply_id(message)
    while current_id is not None and len(normalized.reply_chain) < max_depth:
        if current_id in seen:
            dbg(f"回复链展开: 检测到循环引用 id={current_id},停止深入")
            break
        seen.add(current_id)
        if time.monotonic() > deadline:
            dbg(f"回复链展开: 达到 {FORWARD_EXPAND_DEADLINE}s 截止时间,停止展开")
            break
        payload = await _call(bot, "get_msg", message_id=current_id)
        if not payload:
            # 部分 OneBot 实现里 reply 段的 id 是 real_id,串行回退一次。
            payload = await _call(bot, "get_msg", real_id=current_id)
        if not payload:
            dbg(f"回复链展开: get_msg id={current_id} 无结果,链在此中断")
            break
        reply_message = _message_from_api(payload)
        reply_normalized = normalize_message(reply_message)
        reply_normalized.media_refs = _retag_media_refs(
            reply_normalized.media_refs, source="reply"
        )
        sender_data = payload.get("sender")
        sender = sender_data if isinstance(sender_data, dict) else {}
        normalized.reply_chain.append(
            {
                "message_id": current_id,
                "user_id": sender.get("user_id"),
                "nickname": sender.get("nickname") or "未知用户",
                "text": reply_normalized.prompt_text(),
                "media_types": [
                    str(item.get("type") or "media")
                    for item in reply_normalized.media_refs
                ][:8],
            }
        )
        _append_media_refs(
            normalized.media_refs,
            reply_normalized.media_refs,
            source="reply",
            source_message_id=current_id,
            source_user_id=(
                int(sender["user_id"])
                if str(sender.get("user_id") or "").isdigit()
                else None
            ),
        )
        # 扫归一化前的 Message,嵌套消息的文本截断不会隐藏下一跳。
        current_id = _first_reply_id(reply_message)
        dbg(
            f"回复链展开: 已解析 {len(normalized.reply_chain)}/{max_depth} 层, "
            f"next_id={current_id!r}"
        )
    _append_media_refs(
        normalized.media_refs,
        forward_media_refs,
        source="forward",
    )
    normalized.parse_trace.append(
        {
            "label": "回复链展开",
            "duration_ms": (time.monotonic() - reply_started) * 1000,
            "output": {
                "reply_depth": len(normalized.reply_chain),
                "media_refs_total": len(normalized.media_refs),
            },
        }
    )
    normalized.parse_trace.append(
        {
            "label": "消息解析完成",
            "duration_ms": (time.monotonic() - parse_started) * 1000,
            "output": {
                "reply_depth": len(normalized.reply_chain),
                "forward_nodes": len(normalized.forward_tree),
                "media_refs": len(normalized.media_refs),
            },
        }
    )
    dbg(
        f"消息解析完成: 回复链 {len(normalized.reply_chain)} 层, "
        f"转发树 {len(normalized.forward_tree)} 个顶层节点"
    )
    return normalized


__all__ = [
    "ForwardNode",
    "NormalizedMessage",
    "SegmentNode",
    "normalize_message",
    "parse_message",
]
