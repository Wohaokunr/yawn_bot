"""私聊 Agent 的桥：核心 ``ai_chat`` 与可选子插件 ``yawn_agent`` 之间的唯一接口。

核心不 import Agent。Agent 子插件加载时调用 :func:`register_private_handler`
登记处理函数；``ai_chat`` 每轮私聊先问这里有没有处理函数、是否开着，有就交给
Agent，Agent 返回 None（没配模型、出错、超额）时再退回原来的流式对话，私聊
永远有回应。

这里还放私聊 Agent 的配置和“自动接管”的成员选择（``/退出`` 后不再自动接，
``/对话`` 恢复），选择落在 JSON 文件里，重启不丢。
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime  # noqa: TC003
from pathlib import Path
from typing import Any, Literal, Optional

from nonebot import get_plugin_config, logger
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from .data_models.agent_memory import AgentMemory
from .data_models.chat_message import ChatMessage
from .data_models.chat_session import ChatSession

# 私聊记忆在 AgentMemory 里的标记：不挂群、scope=private，永远不进群聊提示词。
PRIVATE_MEMORY_SCOPE = "private"
PRIVATE_MEMORY_TYPE = "private_fact"
# 私聊记忆的类别名（给模型和 WebUI 看的中文）。
PRIVATE_MEMORY_LABELS: dict[str, str] = {
    "display_name": "昵称",
    "preferred_address": "希望的称呼",
    "hobby": "爱好",
    "preference": "喜好",
    "dislike": "讨厌的",
    "skill": "擅长",
    "current_status": "近期状态",
}


class PrivateAgentConfig(BaseModel):
    """私聊 Agent 配置，字段从 .env / 环境变量读取。"""

    # 私聊交给 Agent（人设、工具、跨群关系）；关闭后退回原来的纯对话
    private_agent_enabled: bool = True
    # 好友私聊不用先发 /对话，直接由 Agent 回复
    private_agent_auto_reply: bool = True
    # 自动接管只对好友生效；临时会话（群里点头像发起的）仍需 /对话
    private_agent_friends_only: bool = True
    # 每人每天最多多少轮私聊 Agent（0 表示不限），超出后退回纯对话
    private_agent_daily_turns: int = Field(default=120, ge=0, le=10000)
    # 每轮最多调用几轮工具（大活交给小助手，主智能体也能多走几步）
    private_agent_max_tool_rounds: int = Field(default=8, ge=1, le=20)
    # 私聊里每人每天最多联网搜索/读网页几次（0 关闭）；
    # 仍受 WebUI 网页搜索总开关与全站预算约束
    private_agent_web_daily: int = Field(default=10, ge=0, le=200)
    # 允许私聊 Agent 主动发消息（用户设的提醒、早安、签到提醒、生日、倒数日当天）
    private_agent_proactive: bool = True
    # 连发合并：对话队列里攒着的连续几条消息（等这么多毫秒）合成一轮回复；0 关闭
    private_agent_merge_window_ms: int = Field(default=1500, ge=0, le=10000)
    # 发言判断：Agent 可以选择这轮不说话（他话没说完 / 不需要回），想回复时
    # 他又发了新消息就把这条让给下一轮一起回，避免连发几条就刷屏。
    private_agent_turn_taking: bool = True
    # 选择“等他说完”后最多等几秒；这期间他没再发消息就自己接上话。
    private_agent_wait_seconds: int = Field(default=25, ge=5, le=600)
    # 私聊里收图片/文件/语音：交给 Agent（可放进他的虚拟电脑），关闭后仍只收文字
    private_agent_accept_media: bool = True
    # 主从多智能体：主 Agent 可以把耗时任务派给隔离上下文的子智能体在后台做
    private_agent_tasks_enabled: bool = True
    # 每人同时最多几个子任务在跑（排队的不算）
    private_agent_task_per_user: int = Field(default=6, ge=1, le=50)
    # 全站同时最多几个子任务在跑；其余排队
    private_agent_task_slots: int = Field(default=30, ge=1, le=200)
    # 每人每天最多派几个子任务（0 表示不限）
    private_agent_task_daily: int = Field(default=90, ge=0, le=5000)
    # 每个子任务最多调用几轮工具
    private_agent_task_max_rounds: int = Field(default=30, ge=2, le=100)
    # 子任务轮次用完还没交付时最多再续几段（每段同样多轮，仍受总时限约束）
    private_agent_task_continuations: int = Field(default=2, ge=0, le=10)
    # 每个子任务最长运行多少秒（不含排队），超时按失败汇报
    private_agent_task_timeout_seconds: int = Field(default=1800, ge=30, le=7200)
    # 后台进度消息的样子：natural 像本人随口报一声（默认）；
    # template 是旧的「标题」 n/m：… 模板
    private_agent_progress_style: Literal["natural", "template"] = "natural"
    # 同一个人两条进度消息至少隔几秒（太近的只记录不发；派活那句回复也算一条）；0 不限
    private_agent_progress_min_gap_seconds: int = Field(default=10, ge=0, le=600)
    # 主 Agent 一轮里自己调了这么多次重活工具（联网/读网页/跑代码/能力包）还没答完，
    # 就提醒它放到后台接着做；0 不提醒
    private_agent_handoff_after: int = Field(default=3, ge=0, le=20)
    # 主 Agent 工具轮次用完还在调工具时，自动把这件事放到后台接着做
    # （而不是用半截结果硬答）
    private_agent_auto_handoff: bool = True
    # 几件后台的事前后脚做完时，等这么多秒合成一次汇报；0 不等
    private_agent_report_batch_seconds: float = Field(default=2.0, ge=0, le=60)


private_agent_config = get_plugin_config(PrivateAgentConfig)


@dataclass(slots=True)
class PrivateTurnRequest:
    """交给私聊 Agent 的一轮。``history`` 是已入库的近期对话（含本条），不含 system。"""

    bot: Any
    event: Any
    user_id: int
    user_input: str
    raw_text: str
    history: list[dict[str, str]]
    session: Any
    time_note: str = ""
    nickname: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)
    # 本轮（含连发合并进来的）消息里的图片/文件/语音/视频段：{"type", **data}
    media: list[dict[str, Any]] = field(default_factory=list)


# 返回实际送达的文本；None 表示没处理，调用方退回纯对话。
PrivateHandler = Callable[[PrivateTurnRequest], Awaitable[Optional[str]]]

_handler: PrivateHandler | None = None


def register_private_handler(handler: PrivateHandler | None) -> None:
    """Agent 子插件登记（或传 None 注销）私聊处理函数。"""

    global _handler  # noqa: PLW0603
    _handler = handler
    if handler is not None:
        logger.info("私聊 Agent 已接入 Yawn对话")


def get_private_handler() -> PrivateHandler | None:
    """开着且有处理函数时返回它。"""

    if not private_agent_config.private_agent_enabled:
        return None
    return _handler


def auto_reply_available() -> bool:
    return (
        private_agent_config.private_agent_enabled
        and private_agent_config.private_agent_auto_reply
        and _handler is not None
    )


# ── 私聊媒体 ─────────────────────────────────────────────────

PRIVATE_MEDIA_TYPES = ("image", "file", "record", "video")
_MEDIA_LABELS = {"image": "图片", "file": "文件", "record": "语音", "video": "视频"}
MAX_MEDIA_PER_TURN = 8


def event_media(event: Any) -> list[dict[str, Any]]:
    """事件里的图片/文件/语音/视频段（``{"type": ..., **data}``）；没有返回空列表。"""

    try:
        message = event.get_message()
    except Exception:  # noqa: BLE001
        return []
    media: list[dict[str, Any]] = []
    for segment in message:
        kind = str(getattr(segment, "type", "") or "")
        if kind not in PRIVATE_MEDIA_TYPES:
            continue
        data = dict(getattr(segment, "data", None) or {})
        media.append({**data, "type": kind})
        if len(media) >= MAX_MEDIA_PER_TURN:
            break
    return media


def media_accepted() -> bool:
    """私聊 Agent 开着、有处理函数且允许收媒体。"""

    return (
        private_agent_config.private_agent_enabled
        and private_agent_config.private_agent_accept_media
        and _handler is not None
    )


def is_sticker_media(item: dict[str, Any]) -> bool:
    """NapCat 用 sub_type=1 标记表情包图片（与 message_parser._is_sticker 一致）。"""

    if str(item.get("type") or "") != "image":
        return False
    sub_type = item.get("sub_type")
    if sub_type is None:
        sub_type = item.get("subType")
    return str(sub_type or "") == "1"


def describe_media(media: list[dict[str, Any]]) -> str:
    """给历史和模型看的占位：[图片] [表情包] [文件: 报表.xlsx]。"""

    labels = []
    for item in media:
        if is_sticker_media(item):
            labels.append("[表情包]")
            continue
        label = _MEDIA_LABELS.get(str(item.get("type")), "附件")
        name = str(item.get("name") or "") if item.get("type") == "file" else ""
        labels.append(f"[{label}: {name[:40]}]" if name else f"[{label}]")
    return " ".join(labels)


# ── 每位用户的回合锁 ─────────────────────────────────────────
# 对话 worker 的一轮和子任务完成后的汇报轮都要持有它，保证同一个人的私聊里
# 不会两轮交错发消息、交错写会话。

_turn_locks: dict[int, asyncio.Lock] = {}


def user_turn_lock(user_id: int) -> asyncio.Lock:
    lock = _turn_locks.get(int(user_id))
    if lock is None:
        if len(_turn_locks) > 4096:  # noqa: PLR2004
            for key in [k for k, v in _turn_locks.items() if not v.locked()]:
                _turn_locks.pop(key, None)
        lock = asyncio.Lock()
        _turn_locks[int(user_id)] = lock
    return lock


# ── 成员对自动接管的选择 ─────────────────────────────────────

_PREF_LOCK = threading.Lock()
_paused_cache: set[int] | None = None


def _prefs_path() -> Path:
    raw = os.environ.get("PRIVATE_AGENT_STATE_PATH", "").strip()
    return Path(raw) if raw else Path("data") / "private_agent" / "state.json"


def _load_paused() -> set[int]:
    global _paused_cache  # noqa: PLW0603
    if _paused_cache is not None:
        return _paused_cache
    path = _prefs_path()
    paused: set[int] = set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        paused = {
            int(item) for item in raw.get("auto_paused", []) if str(item).isdigit()
        }
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001
        logger.warning(f"私聊 Agent 状态文件损坏，按默认处理: {path}")
    _paused_cache = paused
    return paused


def _save_paused(paused: set[int]) -> None:
    path = _prefs_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"auto_paused": sorted(paused)}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError:
        logger.warning(f"私聊 Agent 状态写入失败: {path}")


def auto_paused(user_id: int) -> bool:
    with _PREF_LOCK:
        return int(user_id) in _load_paused()


def auto_paused_user_ids() -> frozenset[int]:
    """当前暂停了自动回复的用户（WebUI 状态条计数用）。"""

    return frozenset(_load_paused())


def set_auto_paused(user_id: int, *, paused: bool) -> bool:
    """记录成员是否暂停自动接管；返回状态是否有变化。"""

    with _PREF_LOCK:
        current = set(_load_paused())
        before = int(user_id) in current
        if paused:
            current.add(int(user_id))
        else:
            current.discard(int(user_id))
        if before == paused:
            return False
        global _paused_cache  # noqa: PLW0603
        _paused_cache = current
        _save_paused(current)
        return True


# ── 存储查询（Agent 与 WebUI 共用，核心不依赖子插件） ─────────


async def turns_today(session: Any, user_id: int, now: datetime) -> int:
    """今天 Bot 在私聊里回了这个人几次（assistant 消息数）。"""

    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(
        await session.scalar(
            select(func.count())
            .select_from(ChatMessage)
            .join(ChatSession, ChatSession.id == ChatMessage.session_id)
            .where(
                ChatSession.user_id == user_id,
                ChatSession.group_id.is_(None),
                ChatMessage.role == "assistant",
                ChatMessage.created_at >= start,
            )
        )
        or 0
    )


def private_memory_query(user_id: int) -> Any:
    return select(AgentMemory).where(
        AgentMemory.scope == PRIVATE_MEMORY_SCOPE,
        AgentMemory.group_id.is_(None),
        AgentMemory.subject_user_id == user_id,
        AgentMemory.memory_type == PRIVATE_MEMORY_TYPE,
    )


async def delete_private_memories(
    session: Any, user_id: int, *, key: str | None = None
) -> int:
    """删某人的私聊记忆（可只删一个类别）。不提交。"""

    stmt = delete(AgentMemory).where(
        AgentMemory.scope == PRIVATE_MEMORY_SCOPE,
        AgentMemory.group_id.is_(None),
        AgentMemory.subject_user_id == user_id,
    )
    if key:
        stmt = stmt.where(AgentMemory.memory_key == key)
    result = await session.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0)


def reset_state_for_tests() -> None:
    global _handler, _paused_cache  # noqa: PLW0603
    _handler = None
    _paused_cache = None


__all__ = [
    "MAX_MEDIA_PER_TURN",
    "PRIVATE_MEDIA_TYPES",
    "PRIVATE_MEMORY_LABELS",
    "PRIVATE_MEMORY_SCOPE",
    "PRIVATE_MEMORY_TYPE",
    "PrivateAgentConfig",
    "PrivateHandler",
    "PrivateTurnRequest",
    "auto_paused",
    "auto_reply_available",
    "delete_private_memories",
    "describe_media",
    "event_media",
    "get_private_handler",
    "is_sticker_media",
    "media_accepted",
    "private_agent_config",
    "private_memory_query",
    "register_private_handler",
    "reset_state_for_tests",
    "set_auto_paused",
    "user_turn_lock",
]
