"""建卡私聊 DSL：自由文本 -> Action。

独立模块（不导入 engine / commands，避免循环导入），供私聊
监听器使用。语法按 re.fullmatch 匹配，无法识别统一返回
None，由调用方回复 _DM_HINT。
"""

from __future__ import annotations

import re
from typing import Optional

from .charsheet import resolve_skill
from .state import Action, ActionKind

_DM_HINT = (
    "建卡期间可用指令：\n"
    "重掷（整卡重掷）｜加点 侦查 20 或 侦查+20\n"
    "减点同理｜重置（清空已加的点）\n"
    "查看（重发角色卡）｜确认（锁定角色卡）"
)

_CONFIRM_RE = re.compile(r"确认|确定|ok", re.IGNORECASE)
_ADD_CMD_RE = re.compile(r"加点\s*(\S+?)\s+(\d+)")
_SUB_CMD_RE = re.compile(r"减点\s*(\S+?)\s+(\d+)")
_ADD_SHORT_RE = re.compile(r"(\S+?)\+(\d+)")
_SUB_SHORT_RE = re.compile(r"(\S+?)-(\d+)")


def _skill_action(
    kind: ActionKind,
    skill_text: str,
    points: str,
    user_id: int,
) -> Optional[Action]:
    """解析技能加点/减点；技能名无法识别返回 None。"""
    skill = resolve_skill(skill_text)
    if skill is None:
        return None
    return Action(kind, user_id, int(points), skill.key)


def parse_card_action(text: str, user_id: int) -> Optional[Action]:  # noqa: PLR0911
    """解析建卡私聊文本；无法识别返回 None。"""
    s = text.strip().lstrip("/")
    if not s:
        return None
    if _CONFIRM_RE.fullmatch(s):
        return Action(ActionKind.CONFIRM_CARD, user_id)
    if s in ("重掷", "reroll"):
        return Action(ActionKind.REROLL, user_id)
    if s == "重置":
        return Action(ActionKind.RESET_SKILLS, user_id)
    if s in ("查看", "角色卡", "卡"):
        return Action(ActionKind.SHOW_CARD, user_id)
    match = _ADD_CMD_RE.fullmatch(s)
    if match:
        return _skill_action(
            ActionKind.ADD_SKILL,
            match.group(1),
            match.group(2),
            user_id,
        )
    match = _SUB_CMD_RE.fullmatch(s)
    if match:
        return _skill_action(
            ActionKind.SUB_SKILL,
            match.group(1),
            match.group(2),
            user_id,
        )
    match = _ADD_SHORT_RE.fullmatch(s)
    if match:
        return _skill_action(
            ActionKind.ADD_SKILL,
            match.group(1),
            match.group(2),
            user_id,
        )
    match = _SUB_SHORT_RE.fullmatch(s)
    if match:
        return _skill_action(
            ActionKind.SUB_SKILL,
            match.group(1),
            match.group(2),
            user_id,
        )
    return None
