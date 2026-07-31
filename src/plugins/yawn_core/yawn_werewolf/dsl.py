"""私聊行动 DSL 解析：自由文本 → Action。

从 commands.py 抽出独立成模块，供命令层与 ai_player 共用
（避免 commands→engine→ai_player→commands 循环导入）。
投票指令（投票N / 弃票）仅在 allow_votes=True 时解析——
人类私聊监听保持原行为，AI 驱动需要用它产出投票行动。
"""

import re
from typing import Optional

from .state import Action, ActionKind

_DM_PATTERNS: list[tuple[str, ActionKind, bool]] = [
    # (正则, 行动类型, 是否需要座位参数)
    (r"刀\s*(\d+)\s*号?", ActionKind.KILL, True),
    (r"(?:查验|验)\s*(\d+)\s*号?", ActionKind.CHECK, True),
    (r"救", ActionKind.SAVE, False),
    (r"毒\s*(\d+)\s*号?", ActionKind.POISON, True),
    (r"(?:开枪|带)\s*(\d+)\s*号?", ActionKind.SHOOT, True),
    (r"(?:不开枪|压枪)", ActionKind.NO_SHOOT, False),
    (r"(?:过|跳过)", ActionKind.SKIP, False),
    (r"自爆", ActionKind.SELF_DETONATE, False),
    (r"(?:上警|竞选)", ActionKind.RUN, False),
    (r"退水", ActionKind.WITHDRAW, False),
    (r"移交警徽\s*(\d+)\s*号?", ActionKind.PASS_BADGE, True),
    (r"撕警徽", ActionKind.TEAR_BADGE, False),
    (r"(?:认主|选主)\s*(\d+)\s*号?", ActionKind.CHOOSE_OWNER, True),
    (r"(?:禁言|禁票)\s*(\d+)\s*号?", ActionKind.SILENCE, True),
    (r"决斗\s*(\d+)\s*号?", ActionKind.DUEL, True),
]

# 仅 AI 驱动启用（allow_votes=True）：投票阶段行动
_VOTE_PATTERNS: list[tuple[str, ActionKind, bool]] = [
    (r"(?:投票|票)\s*(\d+)\s*号?", ActionKind.VOTE, True),
    (r"弃票", ActionKind.ABSTAIN, False),
]

_DM_HINT = (
    "无法识别的指令。可用格式：\n"
    "刀N / 查验N / 救 / 毒N / 开枪N / 不开枪 / 过\n"
    "认主N / 禁言N（禁票N）/ 决斗N\n"
    "自爆 / 上警 / 退水 / 移交警徽N / 撕警徽\n"
    "说XXX（狼人讨论，转发给队友）"
)


def parse_dm_action(
    text: str,
    user_id: int,
    *,
    allow_votes: bool = False,
) -> Optional[Action]:
    """解析私聊自由文本为行动；无法解析返回 None。"""
    text = text.lstrip("/").strip()
    if not text:
        return None
    order_match = re.fullmatch(r"排序\s*(\d+)\s*号?\s*(顺|逆)?", text)
    if order_match is not None:
        aux = "ccw" if order_match.group(2) == "逆" else "cw"
        return Action(
            ActionKind.ORDER,
            user_id,
            int(order_match.group(1)),
            aux,
        )
    say_match = re.fullmatch(r"(?:说|发言|讨论)\s*(.+)", text, re.DOTALL)
    if say_match is not None:
        return Action(
            ActionKind.SAY,
            user_id,
            None,
            say_match.group(1).strip(),
        )
    patterns = list(_DM_PATTERNS)
    if allow_votes:
        patterns.extend(_VOTE_PATTERNS)
    for pattern, kind, need_seat in patterns:
        match = re.fullmatch(pattern, text)
        if match is None:
            continue
        seat = int(match.group(1)) if need_seat else None
        return Action(kind, user_id, seat)
    return None
