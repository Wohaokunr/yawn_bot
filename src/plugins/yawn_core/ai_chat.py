"""Yawn对话模块：基于小米 MiMo 模型的 AI 私聊对话。

功能：
- 流式接收 AI 回复并分段发送
- 多轮对话（自动加载历史上下文）
- 对话记录持久化至 SQLite
- 对话模式：持续聊天，无需重复输入命令
- 预留群聊接口（group_id 字段）
"""

import asyncio
import time
import weakref
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

from nonebot import logger
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata, on_command, on_message
from nonebot.rule import Rule
from nonebot_plugin_orm import async_scoped_session, get_session
from openai import AsyncOpenAI, OpenAIError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_scoped_session as _sa_scoped_session

from .chat_state import (
    enqueue,
    ensure_worker,
    enter_mode,
    exit_mode,
    get_state,
    is_in_mode,
    stop_worker,
)
from .data_models.chat_message import ChatMessage
from .data_models.chat_session import ChatSession
from .llm import _COMPLETION_CONCURRENCY
from .llm import ai_config as _ai_config
from .llm import get_client as _get_client
from .permission import check_feature_permission, require_feature
from .reply_chain import (
    format_chain_for_prompt,
    resolve_reply_chain,
)

# handler 的 DI 注入 scoped session，worker 用 get_session() 得普通会话；
# 辅助函数两者皆收
_DbSession = Union[AsyncSession, _sa_scoped_session[AsyncSession]]

_chat_locks: weakref.WeakValueDictionary[int, asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


def _chat_lock(user_id: int) -> asyncio.Lock:
    lock = _chat_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _chat_locks[user_id] = lock
    return lock


def _record_stream_metric(outcome: str, started: float) -> None:
    """记录流式 AI 请求；监控故障不能改变消息发送语义。"""

    try:
        from .metrics import record_ai_degradation, record_ai_request

        record_ai_request(
            "chat_stream",
            outcome,
            max(time.perf_counter() - started, 0.0),
        )
        if outcome != "success":
            record_ai_degradation("chat", outcome)
    except Exception:  # noqa: BLE001
        logger.debug("流式 AI 指标更新失败", exc_info=True)

__plugin_meta__ = PluginMetadata(
    name="Yawn对话",
    description="基于 AI 的智能对话",
    usage=(
        "发送 /对话 进入对话模式，直接聊天；"
        "发送 /退出 退出对话。"
        "也可 /对话 <内容> 一次性对话"
    ),
    extra={
        "commands": [
            {
                "name": "Yawn对话",
                "aliases": ["对话", "ai对话", "AI对话"],
                "description": "AI智能对话",
                "feature": "ai_chat",
                "scope": "private",
                "superuser": False,
            },
            {
                "name": "新对话",
                "aliases": ["新建对话", "重置对话"],
                "description": "重置对话上下文",
                "feature": "ai_chat",
                "scope": "private",
                "superuser": False,
            },
            {
                "name": "退出",
                "aliases": ["退出对话", "结束对话"],
                "description": "退出对话模式",
                "feature": "ai_chat",
                "scope": "private",
                "superuser": False,
            },
        ],
    },
)

logger.info("Yawn对话模块已加载")

# ── AI 客户端配置 ─────────────────────────────────────────
# 配置（AIChatConfig）与惰性 AsyncOpenAI 客户端由 llm.py 统一管理，
# 与狼人杀 AI 共用。

# 测试和部署方可显式覆盖；默认在首次请求时从 llm.py 获取客户端。
_client: Optional[AsyncOpenAI] = None

# ── 对话参数 ──────────────────────────────────────────────

# 上下文窗口：最多加载最近 N 条消息作为历史
_MAX_HISTORY_MESSAGES = 20
# 单条消息最大字符数（超出则分段发送）
_SEGMENT_CHAR_LIMIT = 1500
# 流式分段发送：已生成文本达到该长度且段落/整句成型即发出
_STREAM_FLUSH_MIN = 200
# 流式读取空闲超时（秒）：超过该时长无新分块则中止
_STREAM_IDLE_TIMEOUT = 60
_STREAM_ACQUIRE_TIMEOUT = 5
# 系统提示词
_SYSTEM_PROMPT = (
    "你是 YawnBot，一个友好、有趣的 QQ 聊天机器人。请用简洁自然的中文回复用户。"
)
# 时间间隔阈值（秒），超过此值在提示词中注入时间
_GAP_THRESHOLD = 20 * 60  # 20 分钟

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

exit_mode_cmd = on_command(
    "退出",
    aliases={"退出对话", "结束对话"},
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
                cut = remaining.rfind(sep, 0, _SEGMENT_CHAR_LIMIT)
                if cut > 0:
                    cut += 1  # 包含分隔符
                    break
        if cut <= 0:
            # 最后硬切
            cut = _SEGMENT_CHAR_LIMIT

        segments.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")

    return segments


async def _get_or_create_session(
    session: _DbSession,
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
        logger.info(f"为用户 {user_id} 创建新对话会话 #{chat_session.id}")

    return chat_session


async def _load_history(
    session: _DbSession,
    session_id: int,
    time_prefix: str = "",
) -> list[dict[str, str]]:
    """加载会话历史消息，构建 OpenAI messages 格式。"""
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
    # 截断后首条可能是 assistant 消息，部分 API 会拒绝
    # 非 user 开头的对话，丢弃前导非 user 消息
    first_user = next(
        (i for i, msg in enumerate(messages) if msg.role == "user"),
        None,
    )
    messages = messages[first_user:] if first_user is not None else []

    system_content = _SYSTEM_PROMPT
    if time_prefix:
        system_content = f"{time_prefix}\n{system_content}"
    history: list[dict[str, str]] = [
        {"role": "system", "content": system_content},
    ]
    history.extend({"role": msg.role, "content": msg.content} for msg in messages)
    return history


async def _stream_and_send(
    bot: Bot,
    event: MessageEvent,
    history: list[dict[str, str]],
) -> Optional[str]:
    """在全局 AI 并发额度内执行流式对话。"""
    started = time.perf_counter()
    acquired = False
    try:
        await asyncio.wait_for(
            _COMPLETION_CONCURRENCY.acquire(),
            timeout=_STREAM_ACQUIRE_TIMEOUT,
        )
        acquired = True
    except asyncio.TimeoutError:
        _record_stream_metric("concurrency_timeout", started)
        await bot.send(
            event,
            MessageSegment.text("当前 AI 请求较多，请稍后再试~"),
        )
        return None

    try:
        return await _stream_and_send_impl(bot, event, history)
    finally:
        if acquired:
            _COMPLETION_CONCURRENCY.release()


async def _stream_and_send_impl(  # noqa: C901, PLR0912, PLR0915
    bot: Bot,
    event: MessageEvent,
    history: list[dict[str, str]],
) -> Optional[str]:
    """流式调用 AI，边生成边发送已完成段落，返回完整回复文本。

    段落/整句成型即发送，用户无需等待全文生成。
    无有效内容（调用失败或超时且无任何生成）时发送致歉提示并返回 None。
    致歉提示仅发送、不持久化，避免污染对话上下文。
    """

    started = time.perf_counter()
    delivered: list[str] = []

    async def _flush(piece: str) -> None:
        piece = piece.strip()
        if not piece:
            return
        for seg in _split_message(piece):
            await bot.send(event, MessageSegment.text(seg))
            delivered.append(seg)

    pending = ""  # 已生成但尚未发送的文本
    timed_out = False
    delivery_failed = False
    stream_error = False

    try:
        llm_client = _client or _get_client()
        if llm_client is None:
            _record_stream_metric("not_configured", started)
            await bot.send(
                event,
                MessageSegment.text("抱歉，AI 服务尚未配置，请联系管理员~"),
            )
            return None
        stream = await llm_client.chat.completions.create(
            model=_ai_config.ai_model,
            messages=history,  # type: ignore[arg-type]
            stream=True,
            max_tokens=_ai_config.ai_max_tokens,
        )
    except OpenAIError as e:
        _record_stream_metric("error", started)
        logger.error(f"AI 调用失败: {e}")
        await bot.send(
            event,
            MessageSegment.text("抱歉，AI 服务暂时不可用，请稍后再试~"),
        )
        return None

    try:
        chunk_iter = stream.__aiter__()
        while True:
            try:
                # 逐分块空闲超时：卡死可及时发现，长回复不受总时长误杀
                chunk = await asyncio.wait_for(
                    chunk_iter.__anext__(),
                    timeout=_STREAM_IDLE_TIMEOUT,
                )
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                logger.error("AI 流式响应超时（无新分块）")
                timed_out = True
                break

            # 部分兼容 API 在流结束时发送空 choices chunk
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if not delta.content:
                continue

            pending += delta.content

            # 段落成型（出现空行且已有足够长度）→ 立即发送
            idx = pending.rfind("\n\n")
            if idx != -1 and idx + 2 >= _STREAM_FLUSH_MIN:
                await _flush(pending[: idx + 2])
                pending = pending[idx + 2 :]
            elif len(pending) >= _SEGMENT_CHAR_LIMIT:
                # 硬上限兜底
                await _flush(pending)
                pending = ""
            elif len(pending) >= _STREAM_FLUSH_MIN and pending[-1] in "。！？!?":
                # 整句成型即发送
                await _flush(pending)
                pending = ""
    except OpenAIError as e:
        stream_error = True
        logger.error(f"AI 流中断: {e}")
    except Exception as e:  # noqa: BLE001
        # 发送可能在拆分后的任意一段失败。保留此前已经送达的片段，
        # 丢弃尚未送达的 pending，避免重试导致重复消息或污染历史。
        delivery_failed = True
        pending = ""
        logger.warning(f"AI 流式消息发送中断: {e!r}")
    finally:
        try:
            await stream.close()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"关闭 AI 流失败: {e!r}")

    # 发送剩余尾部文本
    if not delivery_failed:
        try:
            await _flush(pending)
        except Exception as e:  # noqa: BLE001
            delivery_failed = True
            logger.warning(f"AI 尾部消息发送失败: {e!r}")

    text = "\n".join(delivered).strip()
    if not text:
        if delivery_failed:
            _record_stream_metric("delivery_failed", started)
            return None
        _record_stream_metric(
            "timeout" if timed_out else "error" if stream_error else "empty",
            started,
        )
        await bot.send(
            event,
            MessageSegment.text(
                "抱歉，AI 响应超时了，请稍后再试~"
                if timed_out
                else "抱歉，AI 服务暂时不可用，请稍后再试~"
            ),
        )
        return None
    if delivery_failed:
        _record_stream_metric("delivery_failed_partial", started)
    elif stream_error:
        _record_stream_metric("error_partial", started)
    elif timed_out:
        _record_stream_metric("timeout_partial", started)
    else:
        _record_stream_metric("success", started)
    if timed_out:
        await bot.send(event, MessageSegment.text("（生成超时，以上为部分内容）"))
    return text


# ── 核心处理函数 ──────────────────────────────────────────


async def _process_chat(
    bot: Bot,
    event: MessageEvent,
    user_id: int,
    session: _DbSession,
    user_input: str,
) -> None:
    """处理一次对话：保存消息、调用 AI、流式发送回复。"""
    # 群聊拦截
    if isinstance(event, GroupMessageEvent):
        await bot.send(
            event,
            MessageSegment.text("群聊对话功能即将上线，请先在私聊中使用~"),
        )
        return

    # 非文本消息（图片/语音等）守卫：
    # 避免空内容入库并发送给 AI
    if not user_input:
        await bot.send(event, MessageSegment.text("暂时只能处理文本消息哦~"))
        return

    # 获取或创建会话
    chat_session = await _get_or_create_session(session, user_id, None)

    # 时间间隔检测
    time_prefix = ""
    if chat_session.updated_at:
        gap = (_now_bj() - chat_session.updated_at).total_seconds()
        if gap > _GAP_THRESHOLD:
            now = _now_bj()
            time_prefix = (
                f"[系统提示：当前时间为 "
                f"{now:%Y-%m-%d %H:%M}，"
                f"距离上一次对话已超过{_GAP_THRESHOLD // 60}分钟]"
            )

    # 解析 reply 链
    reply_context = ""
    try:
        nodes = await resolve_reply_chain(bot, event)
        if nodes:
            reply_context = format_chain_for_prompt(nodes)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"reply 链解析失败: {e}")

    # 构建增强内容
    enhanced_content = user_input
    if reply_context:
        enhanced_content = f"{reply_context}\n\n[用户当前消息]: {user_input}"

    # 保存用户消息并先行提交：
    # 即使后续 AI 调用失败或被取消，用户消息也不丢失
    user_msg = ChatMessage(
        session_id=chat_session.id,
        role="user",
        content=enhanced_content,
        created_at=_now_bj(),
    )
    session.add(user_msg)

    # 自动设置会话标题（首条消息）
    if chat_session.title is None:
        chat_session.title = user_input[:50]

    # 更新会话时间
    chat_session.updated_at = _now_bj()
    await session.commit()

    # 加载历史并流式调用 AI（边生成边发送）
    history = await _load_history(session, chat_session.id, time_prefix)
    reply_text = await _stream_and_send(bot, event, history)

    # 仅持久化有效回复；致歉提示不入库，避免污染上下文
    if reply_text:
        ai_msg = ChatMessage(
            session_id=chat_session.id,
            role="assistant",
            content=reply_text,
            created_at=_now_bj(),
        )
        session.add(ai_msg)
        await session.commit()


async def _run_user_chat(  # noqa: PLR0913
    bot: Bot,
    event: MessageEvent,
    user_id: int,
    session: _DbSession,
    user_input: str,
    *,
    refresh_session: bool = False,
) -> None:
    async with _chat_lock(user_id):
        if refresh_session:
            await session.rollback()
        await _process_chat(bot, event, user_id, session, user_input)


async def _worker_process_chat(
    bot: Bot,
    event: MessageEvent,
    user_id: int,
    override_text: Optional[str] = None,
) -> None:
    """Worker 回调：使用独立 DB 会话处理消息。

    override_text 非 None 时作为用户输入
    （命令事件的纯文本含命令前缀，需覆盖）。
    """
    async with get_session() as session:
        if override_text is not None:
            user_input = override_text
        else:
            user_input = event.get_plaintext().strip()
        await _run_user_chat(bot, event, user_id, session, user_input)


async def _reset_chat_session(session: _DbSession, user_id: int) -> int:
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

    new_session = ChatSession(
        user_id=user_id,
        group_id=None,
        created_at=_now_bj(),
        updated_at=_now_bj(),
    )
    session.add(new_session)
    await session.flush()
    new_session_id = new_session.id
    await session.commit()
    return new_session_id


async def _reset_user_chat(session: _DbSession, user_id: int) -> int:
    async with _chat_lock(user_id):
        await stop_worker(user_id)
        await session.rollback()
        return await _reset_chat_session(session, user_id)


# ── 事件处理 ──────────────────────────────────────────────


@ai_chat_cmd.handle()
async def handle_ai_chat(
    bot: Bot,
    event: MessageEvent,
    session: async_scoped_session,
    args: Message = CommandArg(),
    _perm: None = require_feature("ai_chat"),  # pyright: ignore[reportArgumentType]
) -> None:
    """处理对话命令：进入模式或一次性对话。"""
    if isinstance(event, GroupMessageEvent):
        await ai_chat_cmd.finish("群聊对话功能即将上线，请先在私聊中使用~")

    user_id = int(event.get_user_id())
    user_input = args.extract_plain_text().strip()

    if not user_input:
        # 无参数 → 进入对话模式
        if is_in_mode(user_id):
            await ai_chat_cmd.finish(
                "你已经在对话模式中啦~\n直接发消息即可聊天\n发送 /退出 退出对话模式"
            )
        enter_mode(user_id)
        ensure_worker(user_id, _worker_process_chat)
        await ai_chat_cmd.finish(
            "已进入对话模式，直接发消息即可聊天~\n发送 /退出 退出对话模式"
        )

    # 有参数且已在对话模式 → 投入 worker 队列串行处理，
    # 避免与在途消息并发写同一会话
    state = get_state(user_id)
    if state is not None and state.in_mode:
        if not enqueue(state, (bot, event, user_input)):
            await ai_chat_cmd.finish("当前对话消息较多，请稍后再试~")
        ensure_worker(user_id, _worker_process_chat)
        await ai_chat_cmd.finish()

    # 有参数且非模式 → 一次性对话（向后兼容）
    await _run_user_chat(
        bot,
        event,
        user_id,
        session,
        user_input,
        refresh_session=True,
    )
    await ai_chat_cmd.finish()


@new_session_cmd.handle()
async def handle_new_session(
    event: MessageEvent,
    session: async_scoped_session,
    _perm: None = require_feature("ai_chat"),  # pyright: ignore[reportArgumentType]
) -> None:
    """重置对话：将当前会话标记删除并新建。"""
    if isinstance(event, GroupMessageEvent):
        await new_session_cmd.finish("群聊对话功能即将上线，请先在私聊中使用~")

    user_id = int(event.get_user_id())

    # 先停掉 worker，避免在途消息写进即将软删除的会话
    new_sess_id = await _reset_user_chat(session, user_id)

    logger.info(f"用户 {user_id} 重置了对话，新会话 #{new_sess_id}")

    # 仍在对话模式则重启 worker：
    # 队列剩余消息在新会话上继续处理
    if is_in_mode(user_id):
        ensure_worker(user_id, _worker_process_chat)

    await new_session_cmd.finish(
        "已开启全新对话，之前的上下文已清除~\n"
        "发送 /对话 进入对话模式，或 /对话 <内容> 聊天！"
    )


@exit_mode_cmd.handle()
async def handle_exit_mode(
    event: MessageEvent,
) -> None:
    """退出对话模式。"""
    # 退出是无害操作，不受功能开关约束：
    # 被禁用的用户也必须能退出对话模式
    if isinstance(event, GroupMessageEvent):
        await exit_mode_cmd.finish("群聊对话功能即将上线，请先在私聊中使用~")
    user_id = int(event.get_user_id())
    if exit_mode(user_id):
        await exit_mode_cmd.finish(
            "已退出对话模式~\n对话记录已保存，下次 /对话 继续聊！"
        )
    await exit_mode_cmd.finish("你当前不在对话模式中哦~")


# ── 对话模式消息监听 ─────────────────────────────────────


async def _is_chat_mode_msg(
    event: MessageEvent,
) -> bool:
    """判断是否为对话模式下的普通消息。"""
    if isinstance(event, GroupMessageEvent):
        return False
    user_id = int(event.get_user_id())
    if not is_in_mode(user_id):
        return False
    # 命令消息不拦截
    text = event.get_plaintext().strip()
    return not text.startswith("/")


chat_mode_listener = on_message(
    rule=Rule(_is_chat_mode_msg),
    priority=0,
    block=True,
)


@chat_mode_listener.handle()
async def _handle_chat_mode_msg(
    bot: Bot,
    event: MessageEvent,
    session: async_scoped_session,
) -> None:
    """对话模式下将普通消息投入队列。"""
    user_id = int(event.get_user_id())

    # 权限检查：功能关闭时自动退出模式，
    # 防止绕过功能开关持续消耗 AI
    if not await check_feature_permission(user_id, None, "ai_chat", session):
        exit_mode(user_id)
        logger.info(f"用户 {user_id} 无 ai_chat 权限，已自动退出对话模式")
        await bot.send(
            event,
            MessageSegment.text("功能「Yawn对话」当前未开启哦~ 已为你退出对话模式"),
        )
        return

    state = get_state(user_id)
    if state is None:
        return
    if not enqueue(state, (bot, event, None)):
        await bot.send(
            event,
            MessageSegment.text("当前对话消息较多，请稍后再试~"),
        )
        return
    ensure_worker(user_id, _worker_process_chat)
