"""QQ 消息嵌套 reply 链解析模块。

解析消息引用链，将上下文提供给 AI 提示词。
基于消息内容中的 reply 段逐层深入，
受限于 OneBot 实现是否返回嵌套 reply 段。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nonebot import logger
from nonebot.adapters.onebot.v11 import (
    Message,
    MessageEvent,
    MessageSegment,
)

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import Bot


@dataclass
class ReplyNode:
    """引用链中的一个节点。"""

    sender_name: str
    content: str
    depth: int


def _extract_text(msg: Message) -> str:
    """提取消息纯文本，排除 reply 段避免重复。"""
    filtered = Message(seg for seg in msg if seg.type != "reply")
    text = filtered.extract_plain_text().strip()
    return text or "[非文本消息]"


def _find_reply_id(msg: Message) -> int | None:
    """从 Message 中找到第一个 reply 段的 id。"""
    for seg in msg:
        if seg.type == "reply":
            return int(seg.data["id"])
    return None


def _message_from_api_data(msg_data: dict) -> Message:
    """从 get_msg 返回数据构建 Message。

    各 OneBot 实现的 message 字段是段数组
    （`[{type, data}]`），直接传给 Message() 会抛
    ValueError，因此优先使用 raw_message
    （CQ 码字符串）；无则逐段构建，单段失败跳过。
    """
    raw = msg_data.get("raw_message")
    if isinstance(raw, str) and raw:
        return Message(raw)

    segments = msg_data.get("message")
    if isinstance(segments, str):
        return Message(segments)
    if isinstance(segments, list):
        message = Message()
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            try:
                message.append(
                    MessageSegment(
                        seg.get("type", "text"),
                        seg.get("data") or {},
                    )
                )
            except Exception:  # noqa: BLE001
                logger.debug("跳过无法构建的消息段: %r", seg)
        return message

    return Message()


# get_msg 调用超时（秒）：并发双路调用，
# 失败快速回退，避免拖慢对话响应
_API_TIMEOUT = 2.0


async def _call_get_msg(bot: Bot, params: dict[str, int]) -> dict | None:
    """带超时调用 get_msg，失败返回 None。"""
    try:
        return await asyncio.wait_for(
            bot.call_api("get_msg", **params),
            timeout=_API_TIMEOUT,
        )
    except Exception:  # noqa: BLE001
        return None


async def _fetch_message_either(bot: Bot, message_id: int) -> dict | None:
    """并发以 real_id / message_id 两种方式获取消息。

    reply 段中的 id 在不同 OneBot 实现里
    对应 real_id 或 message_id，并发调用取先成功者，
    消除串行回退的双倍往返延迟。
    """
    pending = {
        asyncio.create_task(_call_get_msg(bot, {"real_id": message_id})),
        asyncio.create_task(_call_get_msg(bot, {"message_id": message_id})),
    }
    result: dict | None = None
    while pending and result is None:
        done, pending = await asyncio.wait(
            pending,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            if task.cancelled() or task.exception() is not None:
                continue
            value = task.result()
            if value:
                result = value
                break
    for task in pending:
        task.cancel()
    if result is None:
        logger.warning("获取消息失败: message_id=%d", message_id)
    return result


async def resolve_reply_chain_from_message(
    bot: Bot,
    message: Message,
    max_depth: int = 5,
    depth_offset: int = 0,
) -> list[ReplyNode]:
    """从 Message 对象中解析嵌套 reply 链。

    在消息内容中查找 reply 段，通过 get_msg API
    逐层获取被引用消息的内容。解析深度取决于
    OneBot 实现是否在消息中返回嵌套 reply 段。

    注意：reply 段的 id 在不同 OneBot 实现里
    对应 real_id 或 message_id，本函数并发尝试
    两种参数并取先成功者。

    Args:
        bot: OneBot Bot 实例。
        message: 待解析的 Message 对象。
        max_depth: 最大解析层数。
        depth_offset: 节点 depth 起始偏移量。

    Returns:
        从直接引用到最深层的有序节点列表。
        无 reply 段时返回空列表。
    """
    nodes: list[ReplyNode] = []
    seen: set[int] = set()
    current_message = message
    level = 0

    while level < max_depth:
        # 查找消息中的 reply 段
        nested_id = _find_reply_id(current_message)
        if nested_id is None:
            break

        if nested_id in seen:
            logger.debug("reply 链检测到循环引用: %d", nested_id)
            break
        seen.add(nested_id)

        # 并发尝试 real_id / message_id 两种参数
        msg_data = await _fetch_message_either(bot, nested_id)
        if msg_data is None:
            break

        try:
            sender_name = msg_data.get("sender", {}).get("nickname", "未知用户")
            msg = _message_from_api_data(msg_data)
            content = _extract_text(msg)
        except Exception as e:  # noqa: BLE001
            # 单层解析失败不波及已收集的上层节点
            logger.warning("解析 reply 层级失败: %s", e)
            break

        node_depth = depth_offset + level
        nodes.append(ReplyNode(sender_name, content, node_depth))
        logger.debug(
            "解析 reply 层级 %d: %s",
            node_depth,
            sender_name,
        )

        level += 1
        # 继续从获取的消息中查找更深层 reply 段
        current_message = msg

    return nodes


async def resolve_reply_chain(
    bot: Bot,
    event: MessageEvent,
    max_depth: int = 5,
) -> list[ReplyNode]:
    """解析消息中的嵌套 reply 链。

    从 event.reply 开始，逐层深入解析引用。
    第一层直接从 event.reply 提取，后续层级
    依赖消息内容中是否存在嵌套 reply 段。

    Returns:
        从直接引用到最深层的有序节点列表。
        无 reply 时返回空列表。
    """
    reply = getattr(event, "reply", None)
    if reply is None:
        return []

    nodes: list[ReplyNode] = []

    # 第一层：从 event.reply 对象提取
    sender_name = reply.sender.nickname or "未知用户"
    content = _extract_text(reply.message)

    nodes.append(ReplyNode(sender_name, content, 0))
    logger.debug("解析 reply 层级 0: %s", sender_name)

    # 后续层级：从 reply.message 中查找嵌套 reply 段；
    # 深层解析失败不影响第 0 层
    try:
        deeper = await resolve_reply_chain_from_message(
            bot,
            reply.message,
            max_depth=max_depth - 1,
            depth_offset=1,
        )
        nodes.extend(deeper)
    except Exception as e:  # noqa: BLE001
        logger.warning("深层 reply 链解析失败: %s", e)

    return nodes


def format_chain_for_prompt(
    nodes: list[ReplyNode],
) -> str:
    """将引用链格式化为提示词文本。

    Returns:
        格式化文本，无节点时返回空字符串。
    """
    if not nodes:
        return ""

    lines: list[str] = ["[引用消息链，从外到内嵌套]:"]
    for i, node in enumerate(nodes):
        label = f"[层级{node.depth + 1}]"
        text = f"{label} {node.sender_name}: {node.content}"
        if i == 0:
            lines.append(f"  {text}")
        else:
            lines.append(f"  └ {text}")

    return "\n".join(lines)
