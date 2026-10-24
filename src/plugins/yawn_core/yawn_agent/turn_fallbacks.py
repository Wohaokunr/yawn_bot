# ruff: noqa: E501, PLR0911, RUF001, TC003
"""Agent 回合的纯本地 fallback；等待提示的实现见 ``progress_notice.py``。"""

from __future__ import annotations

import re
from datetime import datetime

from .progress_notice import (
    WAIT_NOTICE_DEFAULT_DELAY,
    WAIT_NOTICE_TASK,
    cancel_wait_notice,
    start_wait_notice,
    wait_notice_delay,
)

GREETING_WORDS = ("你好", "嗨", "hello", "hi", "早上好", "晚上好", "在吗", "在不在")
FALLBACK_NOTICES = (
    "现在有点忙，稍后再试～",
    "这会儿没接上思路，等我一下～",
    "暂时答不上来，过一会儿再问我吧～",
)
FALLBACK_CURSOR_LIMIT = 256
WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
# 慢回合反馈已移到 progress_notice.py（先贴表情、按进度说话）；这里保留旧名字。
WAIT_NOTICE = "稍等，我想想"
_FALLBACK_CURSOR: dict[int, int] = {}


def contains_word(text: str, word: str) -> bool:
    if not word:
        return False
    if re.fullmatch(r"[a-z0-9 ]+", word):
        return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) is not None
    return word in text


def deterministic_reply(text: str, *, now: datetime) -> str | None:
    """不查库、不调协议的确定性回复。"""

    normalized = " ".join(text.lower().split())
    if any(contains_word(normalized, word) for word in GREETING_WORDS):
        return "我在呀，有事直接说～"
    if "agent状态" in normalized or "群聊agent" in normalized:
        return "群聊 Agent 在线；复杂对话需要配置 AI_API_KEY。"
    if any(word in normalized for word in ("谢谢", "感谢", "多谢", "thanks", "thx")):
        return "不客气～"
    if any(word in normalized for word in ("几点", "现在时间", "什么时间", "报时", "时间是")):
        return f"现在是 {now:%H:%M}（北京时间）。"
    if any(word in normalized for word in ("今天几号", "今天日期", "现在日期", "星期几", "周几", "今天星期")):
        return f"今天是 {now:%Y年%m月%d日} {WEEKDAY_NAMES[now.weekday()]}。"
    if any(word in normalized for word in ("会什么", "能做什么", "有哪些功能", "帮助")):
        return "发 /help 可以看全部命令；群里直接 @ 我就能聊天。"
    if "记忆" in normalized:
        return "群记忆用 /Agent记忆 查看；想退出记录发 /Agent隐私。"
    if "画像" in normalized:
        return "人物画像用 /Agent画像 查看。"
    return None


def fallback_notice(group_id: int) -> str:
    index = _FALLBACK_CURSOR.get(group_id, -1) + 1
    if len(_FALLBACK_CURSOR) >= FALLBACK_CURSOR_LIMIT:
        _FALLBACK_CURSOR.clear()
    _FALLBACK_CURSOR[group_id] = index
    return FALLBACK_NOTICES[index % len(FALLBACK_NOTICES)]


__all__ = [
    "WAIT_NOTICE",
    "WAIT_NOTICE_DEFAULT_DELAY",
    "WAIT_NOTICE_TASK",
    "cancel_wait_notice",
    "contains_word",
    "deterministic_reply",
    "fallback_notice",
    "start_wait_notice",
    "wait_notice_delay",
]
