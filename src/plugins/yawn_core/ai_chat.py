"""Yawn对话模块：基于小米 MiMo 模型的 AI 私聊对话。

功能：
- 流式接收 AI 回复并分段发送
- 多轮对话（自动加载历史上下文）
- 对话记录持久化至 SQLite
- 预留群聊接口（group_id 字段）
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from nonebot import logger
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.dependencies import Dependent
from nonebot.params import CommandArg
from nonebot.plugin import on_command
from nonebot_plugin_orm import async_scoped_session
from openai import AsyncOpenAI, OpenAIError
from sqlalchemy import select

from .data_models.chat_message import ChatMessage
from .data_models.chat_session import ChatSession
from .permission import require_feature

logger.info("Yawn对话模块已加载")

# ── AI 客户端配置 ─────────────────────────────────────────

_AI_BASE_URL = "https://token-plan-cn.xiaomimimo.com/v1"
_AI_API_KEY = (
    "tp-cnvbscmew57f45m23dmnuet028kppbp4g1vxurun9jsdv9qs"
)
_AI_MODEL = "mimo-v2.5-pro"

_client = AsyncOpenAI(
    api_key=_AI_API_KEY,
    base_url=_AI_BASE_URL,
)

# ── 对话参数 ──────────────────────────────────────────────

# 上下文窗口：最多加载最近 N 条消息作为历史
_MAX_HISTORY_MESSAGES = 20
# 单条消息最大字符数（超出则分段发送）
_SEGMENT_CHAR_LIMIT = 1500
# 系统提示词
_SYSTEM_PROMPT = (
    "你是 YawnBot，一个友好、有趣的 QQ 聊天机器人。"
    "请用简洁自然的中文回复用户。"
)

# ── 北京时间工具 ──────────────────────────────────────────

_BJ_TZ = timezone(timedelta(hours=8))


def _now_bj() -> datetime:
    """获取当前北京时间（naive）。"""
    return datetime.now(_BJ_TZ).replace(tzinfo=None)


# ── 命令匹配器 ────────────────────────────────────────────

ai_chat_cmd = on_command(
    "Yawn对话",
    aliases={"对话", "ai对话", "AI对话"},
    priority=5,
    block=True,
)

new_session_cmd = on_command(
    "新对话",
    aliases={"新建对话", "重置对话"},
    priority=5,
    block=True,
)


# ── 辅助函数 ──────────────────────────────────────────────


def _split_message(text: str) -> list[str]:
    """将长文本按段落/句子/长度分段。"""
    if len(text) <= _SEGMENT_CHAR_LIMIT:
        return [text]

    segments: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= _SEGMENT_CHAR_LIMIT:
            segments.append(remaining)
            break

        # 优先在换行处切割
        cut = remaining.rfind("\n", 0, _SEGMENT_CHAR_LIMIT)
        if cut <= 0:
            # 其次在句号/问号/感叹号处切割
            for sep in ("。", "！", "？", ".", "!", "?"):
                cut = remaining.rfind(
                    sep, 0, _SEGMENT_CHAR_LIMIT
                )
                if cut > 0:
                    cut += 1  # 包含分隔符
                    break
        if cut <= 0:
            # 最后硬切
            cut = _SEGMENT_CHAR_LIMIT

        segments.append(remaining[:cut])
        remaining = remaining[cut:]

    return segments


async def _get_or_create_session(
    session: async_scoped_session,
    user_id: int,
    group_id: Optional[int] = None,
) -> ChatSession:
    """获取用户最近活跃的会话，不存在则新建。"""
    stmt = (
        select(ChatSession)
        .where(
            ChatSession.user_id == user_id,
            ChatSession.group_id == group_id,
            ChatSession.is_deleted == False,  # noqa: E712
        )
        .order_by(ChatSession.updated_at.desc().nullslast())
        .limit(1)
    )
    result = await session.execute(stmt)
    chat_session = result.scalar_one_or_none()

    if chat_session is None:
        chat_session = ChatSession(
            user_id=user_id,
            group_id=group_id,
            created_at=_now_bj(),
            updated_at=_now_bj(),
        )
        session.add(chat_session)
        # 仅 flush 获取自增 id，最终由 handler 统一 commit
        await session.flush()
        logger.info(
            f"为用户 {user_id} 创建新对话会话 "
            f"#{chat_session.id}"
        )

    return chat_session


async def _load_history(
    session: async_scoped_session,
    session_id: int,
) -> list[dict[str, str]]:
    """加载会话历史消息，构建 OpenAI messages 格式。"""
    # 依赖 SQLAlchemy autoflush：刚 add 的用户消息
    # 会在执行 select 前自动 flush，从而包含在结果中
    stmt = (
        select(ChatMessage)
        .where(
            ChatMessage.session_id == session_id,
            ChatMessage.is_deleted == False,  # noqa: E712
        )
        .order_by(ChatMessage.id.desc())
        .limit(_MAX_HISTORY_MESSAGES)
    )
    result = await session.execute(stmt)
    messages = result.scalars().all()

    # 反转为时间正序
    messages = list(reversed(messages))

    history: list[dict[str, str]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
    ]
    history.extend(
        {"role": msg.role, "content": msg.content}
        for msg in messages
    )
    return history


async def _stream_chat(
    history: list[dict[str, str]],
) -> str:
    """调用 AI 接口（流式），返回完整回复文本。"""
    chunks: list[str] = []

    stream = await _client.chat.completions.create(
        model=_AI_MODEL,
        messages=history,  # type: ignore[arg-type]
        stream=True,
    )

    async for chunk in stream:
        # 部分兼容 API 在流结束时发送空 choices chunk
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta.content:
            chunks.append(delta.content)

    return "".join(chunks)


# ── 事件处理 ──────────────────────────────────────────────


@ai_chat_cmd.handle()
async def handle_ai_chat(
    event: MessageEvent,
    session: async_scoped_session,
    args: Message = CommandArg(),
    _perm: Dependent = require_feature("ai_chat"),
) -> None:
    """处理用户对话请求。"""
    # 群聊中暂不可用（预留）
    if isinstance(event, GroupMessageEvent):
        await ai_chat_cmd.finish(
            "群聊对话功能即将上线，请先在私聊中使用~"
        )

    user_input = args.extract_plain_text().strip()
    if not user_input:
        await ai_chat_cmd.finish(
            "用法：/对话 <你想说的话>\n"
            "例如：/对话 今天天气怎么样？\n"
            "发送 /新对话 可重置对话上下文"
        )

    user_id = int(event.get_user_id())
    group_id: Optional[int] = None

    # 获取或创建会话
    chat_session = await _get_or_create_session(
        session, user_id, group_id
    )

    # 保存用户消息
    user_msg = ChatMessage(
        session_id=chat_session.id,
        role="user",
        content=user_input,
        created_at=_now_bj(),
    )
    session.add(user_msg)

    # 自动设置会话标题（首条消息）
    if chat_session.title is None:
        chat_session.title = user_input[:50]

    # 加载历史并调用 AI
    history = await _load_history(session, chat_session.id)

    try:
        reply_text = await _stream_chat(history)
    except OpenAIError as e:
        # OpenAIError 覆盖 SDK 所有异常，包括网络超时
        # （内部包装为 APIConnectionError，是 OpenAIError 子类）
        logger.error(f"AI 调用失败: {e}")
        await ai_chat_cmd.finish(
            "抱歉，AI 服务暂时不可用，请稍后再试~"
        )

    if not reply_text.strip():
        reply_text = "（AI 未返回有效回复）"

    # 保存 AI 回复
    ai_msg = ChatMessage(
        session_id=chat_session.id,
        role="assistant",
        content=reply_text,
        created_at=_now_bj(),
    )
    session.add(ai_msg)

    # 更新会话时间
    chat_session.updated_at = _now_bj()
    await session.commit()

    # 分段发送回复
    segments = _split_message(reply_text)
    for seg in segments:
        await ai_chat_cmd.send(
            MessageSegment.text(seg)
        )

    await ai_chat_cmd.finish()


@new_session_cmd.handle()
async def handle_new_session(
    event: MessageEvent,
    session: async_scoped_session,
    _perm: Dependent = require_feature("ai_chat"),
) -> None:
    """重置对话：将当前会话标记删除并新建。"""
    if isinstance(event, GroupMessageEvent):
        await new_session_cmd.finish(
            "群聊对话功能即将上线，请先在私聊中使用~"
        )

    user_id = int(event.get_user_id())

    # 软删除当前会话
    stmt = (
        select(ChatSession)
        .where(
            ChatSession.user_id == user_id,
            ChatSession.group_id == None,  # noqa: E711
            ChatSession.is_deleted == False,  # noqa: E712
        )
        .order_by(ChatSession.updated_at.desc().nullslast())
        .limit(1)
    )
    result = await session.execute(stmt)
    old_session = result.scalar_one_or_none()

    if old_session is not None:
        old_session.is_deleted = True

    # 创建新会话
    new_sess = ChatSession(
        user_id=user_id,
        group_id=None,
        created_at=_now_bj(),
        updated_at=_now_bj(),
    )
    session.add(new_sess)
    # flush 获取自增 id 后缓存，避免 commit 后惰性加载
    await session.flush()
    new_sess_id = new_sess.id
    await session.commit()

    logger.info(
        f"用户 {user_id} 重置了对话，新会话 #{new_sess_id}"
    )
    await new_session_cmd.finish(
        "已开启全新对话，之前的上下文已清除~\n"
        "发送 /对话 <内容> 开始聊天吧！"
    )
