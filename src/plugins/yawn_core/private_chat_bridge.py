"""私聊 Agent 的桥：核心 ``ai_chat`` 与可选子插件 ``yawn_agent`` 之间的唯一接口。

核心不 import Agent。Agent 子插件加载时调用 :func:`register_private_handler`
登记处理函数；``ai_chat`` 每轮私聊先问这里有没有处理函数、是否开着，有就交给
Agent，Agent 返回 None（没配模型、出错、超额）时再退回原来的流式对话，私聊
永远有回应。

这里还放私聊 Agent 的配置和“自动接管”的成员选择（``/退出`` 后不再自动接，
``/对话`` 恢复），选择落在 JSON 文件里，重启不丢。
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime  # noqa: TC003
from pathlib import Path
from typing import Any, Optional

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
    # 每轮最多调用几轮工具
    private_agent_max_tool_rounds: int = Field(default=4, ge=1, le=8)
    # 私聊里每人每天最多联网搜索/读网页几次（0 关闭）；
    # 仍受 WebUI 网页搜索总开关与全站预算约束
    private_agent_web_daily: int = Field(default=10, ge=0, le=200)
    # 允许私聊 Agent 主动发消息（用户设的提醒、早安、签到提醒、生日、倒数日当天）
    private_agent_proactive: bool = True
    # 连发合并：对话队列里攒着的连续几条消息（等这么多毫秒）合成一轮回复；0 关闭
    private_agent_merge_window_ms: int = Field(default=1500, ge=0, le=10000)


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
    "PRIVATE_MEMORY_LABELS",
    "PRIVATE_MEMORY_SCOPE",
    "PRIVATE_MEMORY_TYPE",
    "PrivateAgentConfig",
    "PrivateHandler",
    "PrivateTurnRequest",
    "auto_paused",
    "auto_reply_available",
    "delete_private_memories",
    "get_private_handler",
    "private_agent_config",
    "private_memory_query",
    "register_private_handler",
    "reset_state_for_tests",
    "set_auto_paused",
]
