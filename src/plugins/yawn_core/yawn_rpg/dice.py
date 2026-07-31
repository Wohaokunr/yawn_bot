"""骰子与检定：d100 技能检定分级、骰表达式求值。

纯函数模块，不持有状态。所有"数值"（掷骰、伤害、SAN 损失）
只经这里产生——AI 无权决定数值，只能通过工具请求系统掷骰。
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from enum import Enum

from .module_schema import CheckDifficulty

# 骰表达式：NdM(±K) 或纯整数
_DICE_RE = re.compile(r"(\d+)d(\d+)([+-]\d+)?")
_INT_RE = re.compile(r"\d+")

# 骰数与面数上限：AI 工具参数与模组数据都不被信任，
# 1d0 会让 randint 抛错、999999999d6 会同步冻死事件循环
# （与 module_schema 的同名常量保持一致）
_MAX_DICE_COUNT = 100
_MAX_DICE_SIDES = 1000

# 大失败判定的技能分界线：技能≥50 时 96-100 为大失败，否则仅 100
_FUMBLE_SKILL_THRESHOLD = 50


class CheckTier(str, Enum):
    """d100 检定结果等级（CoC 7版）。"""

    CRITICAL = "critical"  # 大成功（01）
    EXTREME = "extreme"  # 极难成功（≤ 技能/5）
    HARD = "hard"  # 困难成功（≤ 技能/2）
    REGULAR = "regular"  # 常规成功（≤ 技能值）
    FAILURE = "failure"  # 失败
    FUMBLE = "fumble"  # 大失败（技能<50 时 100；否则 96-100）


# 视为"成功"的等级集合
SUCCESS_TIERS: frozenset[CheckTier] = frozenset(
    {
        CheckTier.CRITICAL,
        CheckTier.EXTREME,
        CheckTier.HARD,
        CheckTier.REGULAR,
    }
)


@dataclass(frozen=True)
class CheckResult:
    """一次 d100 检定的完整结果。"""

    roll: int
    skill_value: int
    difficulty: CheckDifficulty
    tier: CheckTier

    @property
    def success(self) -> bool:
        """是否达到要求难度的成功。"""
        if self.tier is CheckTier.FUMBLE:
            return False
        if self.tier is CheckTier.CRITICAL:
            return True
        if self.difficulty is CheckDifficulty.EXTREME:
            return self.tier is CheckTier.EXTREME
        if self.difficulty is CheckDifficulty.HARD:
            return self.tier in (CheckTier.EXTREME, CheckTier.HARD)
        return self.tier in SUCCESS_TIERS

    def describe(self, skill_name: str) -> str:
        """渲染系统播报文案，如「〔检定〕侦查：d100=23/60 成功」。"""
        names = {
            CheckTier.CRITICAL: "大成功",
            CheckTier.EXTREME: "极难成功",
            CheckTier.HARD: "困难成功",
            CheckTier.REGULAR: "成功",
            CheckTier.FAILURE: "失败",
            CheckTier.FUMBLE: "大失败",
        }
        diff_names = {
            CheckDifficulty.REGULAR: "",
            CheckDifficulty.HARD: "（困难）",
            CheckDifficulty.EXTREME: "（极难）",
        }
        return (
            f"〔检定〕{skill_name}{diff_names[self.difficulty]}："
            f"d100={self.roll}/{self.skill_value} {names[self.tier]}"
        )


def roll_d100() -> int:
    """掷一次 d100（1-100）。"""
    return random.randint(1, 100)


def classify_roll(roll: int, skill_value: int) -> CheckTier:
    """按 CoC 7版规则给 d100 点数分级。"""
    if roll == 1:
        return CheckTier.CRITICAL
    fumble_threshold = 100 if skill_value < _FUMBLE_SKILL_THRESHOLD else 96
    if roll >= fumble_threshold:
        return CheckTier.FUMBLE
    if roll <= skill_value // 5:
        return CheckTier.EXTREME
    if roll <= skill_value // 2:
        return CheckTier.HARD
    if roll <= skill_value:
        return CheckTier.REGULAR
    return CheckTier.FAILURE


def skill_check(
    skill_value: int,
    difficulty: CheckDifficulty = CheckDifficulty.REGULAR,
) -> CheckResult:
    """执行一次技能检定：掷 d100 并按难度判定成功与否。"""
    roll = roll_d100()
    return CheckResult(
        roll=roll,
        skill_value=skill_value,
        difficulty=difficulty,
        tier=classify_roll(roll, skill_value),
    )


def _dice_in_bounds(count: str, sides: str) -> bool:
    """骰数 / 面数是否在安全范围内。"""
    return 1 <= int(count) <= _MAX_DICE_COUNT and 1 <= int(sides) <= _MAX_DICE_SIDES


def is_valid_dice_expr(text: str) -> bool:
    """是否为合法骰表达式（NdM±K 或纯整数；骰数 / 面数有上限）。"""
    s = text.strip()
    if _INT_RE.fullmatch(s) is not None:
        return True
    match = _DICE_RE.fullmatch(s)
    return match is not None and _dice_in_bounds(match.group(1), match.group(2))


def roll_dice(expr: str) -> int:
    """求值骰表达式：1d6 / 2d6+3 / 5；非法表达式抛 ValueError。"""
    s = expr.strip()
    if _INT_RE.fullmatch(s):
        return int(s)
    match = _DICE_RE.fullmatch(s)
    if match is None:
        msg = f"非法骰表达式：{expr!r}"
        raise ValueError(msg)
    count, sides, modifier = match.groups()
    if not _dice_in_bounds(count, sides):
        msg = f"骰表达式超出范围：{expr!r}"
        raise ValueError(msg)
    total = sum(random.randint(1, int(sides)) for _ in range(int(count)))
    if modifier:
        total += int(modifier)
    return max(total, 0)


def roll_san_loss(san_loss: str, *, success: bool) -> int:
    """求值 SAN 损失表达式 "成功侧/失败侧"（如 1/1d6）。"""
    left, sep, right = san_loss.partition("/")
    if not sep:
        msg = f"非法 SAN 损失表达式：{san_loss!r}"
        raise ValueError(msg)
    side = left.strip() if success else right.strip()
    if not side:
        return 0
    return roll_dice(side)
