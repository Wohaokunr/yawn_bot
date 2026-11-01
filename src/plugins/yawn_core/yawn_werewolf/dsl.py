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
    (r"(?:过|跳过|空刀|空守|不救|不用药|pass)", ActionKind.SKIP, False),
    (r"(?:守护|守)\s*(\d+)\s*号?", ActionKind.GUARD, True),
    (r"自爆\s*(?:带走?)?\s*(\d+)\s*号?", ActionKind.SELF_DETONATE, True),
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
    "刀N / 查验N / 救 / 毒N / 守护N / 开枪N / 不开枪 / 过\n"
    "认主N / 禁言N（禁票N）/ 决斗N\n"
    "自爆 / 上警 / 退水 / 移交警徽N / 撕警徽\n"
    "说XXX（狼人讨论，转发给队友）"
)


# 中文数字座位（一号~二十号）→ 阿拉伯数字，方便手机输入
_CN_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_FULLWIDTH = str.maketrans("０１２３４５６７８９", "0123456789")  # noqa: RUF001
_TRAILING_PUNCT = "。.!！~～?？,，;；"  # noqa: RUF001


def _cn_number(match: "re.Match[str]") -> str:
    """把 "十二" / "三" / "二十" 等转换成数字字符串。"""
    text = match.group(0)
    if text == "十":
        return "10"
    if "十" in text:
        tens, _, ones = text.partition("十")
        value = (_CN_DIGITS.get(tens, 1) if tens else 1) * 10 + _CN_DIGITS.get(ones, 0)
        return str(value)
    return str(_CN_DIGITS.get(text, 0))


def normalize_text(text: str) -> str:
    """输入归一化：全角数字、中文数字、尾随标点。"""
    text = text.translate(_FULLWIDTH).strip().rstrip(_TRAILING_PUNCT).strip()
    return re.sub(r"[一二两三四五六七八九十]+(?=\s*号|$)", _cn_number, text)


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
    say_text = text
    text = normalize_text(text)
    order_match = re.fullmatch(r"排序\s*(\d+)\s*号?\s*(顺|逆)?", text)
    if order_match is not None:
        aux = "ccw" if order_match.group(2) == "逆" else "cw"
        return Action(
            ActionKind.ORDER,
            user_id,
            int(order_match.group(1)),
            aux,
        )
    say_match = re.fullmatch(r"(?:说|发言|讨论)\s*(.+)", say_text, re.DOTALL)
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
