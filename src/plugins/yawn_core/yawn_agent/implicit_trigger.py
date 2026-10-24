# ruff: noqa: E501, PLR0911
"""不 @ 也能让 Bot 动手：叫名字、或者刚和 Bot 聊过的人接着提需求。

旧逻辑只有 @、回复 Bot、固定唤醒词三种触发；其余消息最多进短会话续聊，
而续聊回合没有任何工具。群友很少每句都 @ Bot，“小咪帮我画个图”“那再算一下
去年的”这类请求就只能得到一句空话。这里补两种隐式触发：

- ``name_call``：消息里叫了人设名或 Bot 昵称（不带 @）；
- ``conversation_intent``：发消息的人 3 分钟内刚明确找过 Bot，且这条消息没有
  @ 别人、没有回复别人。

两种情况都**不靠关键词判断意图**：只有路由模型（``tool_planner``，不带群上下文）
认为这条消息确实是在请 Bot 做事（``request``）时才触发；``conversation_intent``
还要求它需要工具包，纯闲聊继续交给短会话续聊去决定接不接。路由结果有缓存，
真正进入对话回合时同一条消息不会再算一次。路由没配置或失败时不触发。
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

from .dialogue_support import contains_word
from .log import dbg
from .tool_planner import ToolPlan, plan_tools
from .tool_preload import plan_signals

if TYPE_CHECKING:
    from .message_parser import NormalizedMessage

ENGAGEMENT_WINDOW_SECONDS = 180.0
_ENGAGEMENT_LIMIT = 2048
_MAX_IMPLICIT_TEXT = 600
# (bot_id, group_id, user_id) -> 最近一次明确找 Bot 的 monotonic 时间
_engaged: dict[tuple[int, int, int], float] = {}


def implicit_triggers_enabled() -> bool:
    raw = os.environ.get("AGENT_IMPLICIT_TRIGGER", "").strip().lower()
    return raw not in {"0", "off", "false", "no"}


def remember_engagement(
    bot_id: int, group_id: int, user_id: int, *, now: float | None = None
) -> None:
    """成员明确找了 Bot（@/回复/唤醒词/隐式触发）时记一笔。"""

    if len(_engaged) >= _ENGAGEMENT_LIMIT:
        cutoff = time.monotonic() - ENGAGEMENT_WINDOW_SECONDS
        for key in [key for key, at in _engaged.items() if at < cutoff]:
            _engaged.pop(key, None)
        if len(_engaged) >= _ENGAGEMENT_LIMIT:
            _engaged.clear()
    _engaged[(int(bot_id), int(group_id), int(user_id))] = (
        time.monotonic() if now is None else now
    )


def recently_engaged(
    bot_id: int, group_id: int, user_id: int, *, now: float | None = None
) -> bool:
    at = _engaged.get((int(bot_id), int(group_id), int(user_id)))
    if at is None:
        return False
    current = time.monotonic() if now is None else now
    return current - at <= ENGAGEMENT_WINDOW_SECONDS


def name_called(text: str, names: tuple[str, ...]) -> bool:
    """消息里是否直接叫了 Bot 的名字；名字由人设/昵称决定，不是意图关键词。"""

    folded = " ".join(str(text or "").casefold().split())
    return any(contains_word(folded, name) for name in names if name)


def _structurally_addressed_elsewhere(normalized: NormalizedMessage, bot_id: int) -> bool:
    """@ 了别人或回复的是别人的消息：这是在跟别人说话。"""

    if any(int(user_id) != int(bot_id) for user_id in normalized.mentions):
        return True
    if normalized.reply_chain:
        target = normalized.reply_chain[0].get("user_id")
        try:
            return target is not None and int(target) != int(bot_id)
        except (TypeError, ValueError):
            return True
    return False


async def resolve_implicit_trigger(
    normalized: NormalizedMessage,
    *,
    bot_id: int,
    group_id: int,
    user_id: int,
    call_names: tuple[str, ...],
) -> tuple[str | None, ToolPlan | None]:
    """返回 (触发来源, 路由结论)；不触发时来源为 None。"""

    if not implicit_triggers_enabled():
        return None, None
    text = normalized.intent_text().strip()
    if not text or len(text) > _MAX_IMPLICIT_TEXT:
        return None, None
    if _structurally_addressed_elsewhere(normalized, bot_id):
        return None, None
    called = name_called(text, call_names)
    engaged = recently_engaged(bot_id, group_id, user_id)
    if not called and not engaged:
        return None, None
    plan = await plan_tools(
        text,
        signals=plan_signals(
            has_reply=bool(normalized.reply_chain),
            has_media=bool(normalized.media_refs),
            mentions_others=False,
            media_refs=normalized.media_refs,
        ),
    )
    if not plan.planned or not plan.request:
        dbg(
            f"群 {group_id} 隐式触发未成立: called={called} engaged={engaged} "
            f"plan={plan.source}/{plan.request}"
        )
        return None, plan
    if called:
        return "name_call", plan
    if plan.toolpacks:
        return "conversation_intent", plan
    return None, plan


def reset_for_tests() -> None:
    _engaged.clear()


__all__ = [
    "ENGAGEMENT_WINDOW_SECONDS",
    "implicit_triggers_enabled",
    "name_called",
    "recently_engaged",
    "remember_engagement",
    "reset_for_tests",
    "resolve_implicit_trigger",
]
