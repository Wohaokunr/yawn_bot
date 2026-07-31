"""人物卡：CoC 7版角色生成、派生值、技能表与加点校验。

人物卡由系统生成（掷骰全随机），玩家只能在私聊里有限地
重掷整卡与微调技能点；HP/SAN 的当前值由引擎维护，卡片
只保存初始上限。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional

# ── 属性 ──────────────────────────────────────────────────

# 属性 key → 中文名
ATTR_CN: dict[str, str] = {
    "str": "力量",
    "con": "体质",
    "siz": "体型",
    "dex": "敏捷",
    "app": "外貌",
    "int": "智力",
    "pow": "意志",
    "edu": "教育",
    "luck": "幸运",
}

# 3d6×5 与 (2d6+6)×5 两组属性（CoC 7版）
_3D6_ATTRS: tuple[str, ...] = ("str", "con", "dex", "app", "pow", "luck")
_2D6P6_ATTRS: tuple[str, ...] = ("siz", "int", "edu")

# 随机角色名池（系统建卡时分配）
CHAR_NAMES: tuple[str, ...] = (
    "沈砚",
    "顾清让",
    "白棠",
    "江绪",
    "温如言",
    "程聿",
    "苏槐",
    "陆听澜",
    "纪岚",
    "许知更",
    "乔晚",
    "裴照",
    "宁霜",
    "贺兰",
    "钟意",
    "唐既白",
)


def roll_attributes() -> dict[str, int]:
    """掷一套 CoC 7版属性（×5 百分比尺度）。"""
    attrs: dict[str, int] = {}
    for key in _3D6_ATTRS:
        attrs[key] = sum(random.randint(1, 6) for _ in range(3)) * 5
    for key in _2D6P6_ATTRS:
        attrs[key] = (sum(random.randint(1, 6) for _ in range(2)) + 6) * 5
    return attrs


# STR+SIZ 伤害加值（DB）查表：(上限, 加值)
_DB_TABLE: tuple[tuple[int, str], ...] = (
    (64, "-2"),
    (84, "-1"),
    (124, "0"),
    (164, "+1d4"),
    (204, "+1d6"),
)


def damage_bonus(attributes: dict[str, int]) -> str:
    """按 STR+SIZ 查伤害加值（DB）。"""
    total = attributes.get("str", 50) + attributes.get("siz", 50)
    for threshold, bonus in _DB_TABLE:
        if total <= threshold:
            return bonus
    return "+2d6"


def derived_hp(attributes: dict[str, int]) -> int:
    """生命值上限 = (CON+SIZ)/10。"""
    return max((attributes.get("con", 50) + attributes.get("siz", 50)) // 10, 1)


def derived_san(attributes: dict[str, int]) -> int:
    """理智上限 = POW。"""
    return attributes.get("pow", 50)


def derived_mp(attributes: dict[str, int]) -> int:
    """魔法值上限 = POW/5。"""
    return attributes.get("pow", 50) // 5


# ── 技能 ──────────────────────────────────────────────────


@dataclass(frozen=True)
class SkillDef:
    """技能定义：base=-1 表示由属性派生（如闪避=DEX/2）。"""

    key: str
    name: str
    base: int
    aliases: tuple[str, ...] = ()


SKILLS: tuple[SkillDef, ...] = (
    SkillDef("library", "图书馆", 20, ("圖書館",)),
    SkillDef("listen", "聆听", 20),
    SkillDef("spot_hidden", "侦查", 25, ("偵查", "搜索")),
    SkillDef("psychology", "心理学", 10),
    SkillDef("persuade", "说服", 10),
    SkillDef("fast_talk", "话术", 5, ("快速交谈",)),
    SkillDef("intimidate", "恐吓", 15),
    SkillDef("stealth", "潜行", 20),
    SkillDef("lockpicking", "锁匠", 1, ("开锁",)),
    SkillDef("mech_repair", "机械维修", 10),
    SkillDef("elec_repair", "电气维修", 10),
    SkillDef("first_aid", "急救", 30),
    SkillDef("medicine", "医学", 1),
    SkillDef("climb", "攀爬", 20),
    SkillDef("swim", "游泳", 20),
    SkillDef("jump", "跳跃", 20),
    SkillDef("brawl", "斗殴", 25, ("格斗",)),
    SkillDef("firearms", "射击", 20, ("手枪",)),
    SkillDef("dodge", "闪避", -1),
    SkillDef("drive", "驾驶", 20),
    SkillDef("cthulhu_mythos", "克苏鲁神话", 0),
)

_SKILL_BY_KEY: dict[str, SkillDef] = {s.key: s for s in SKILLS}
_SKILL_BY_NAME: dict[str, SkillDef] = {}
for _skill in SKILLS:
    _SKILL_BY_NAME[_skill.name] = _skill
    for _alias in _skill.aliases:
        _SKILL_BY_NAME[_alias] = _skill


def resolve_skill(text: str) -> Optional[SkillDef]:
    """按 key / 中文名 / 别名解析技能。"""
    s = text.strip()
    if s in _SKILL_BY_KEY:
        return _SKILL_BY_KEY[s]
    return _SKILL_BY_NAME.get(s)


def base_skill_values(attributes: dict[str, int]) -> dict[str, int]:
    """按属性计算技能初始值表。"""
    values: dict[str, int] = {}
    for skill in SKILLS:
        if skill.base >= 0:
            values[skill.key] = skill.base
        else:
            values[skill.key] = attributes.get("dex", 50) // 2
    return values


def skill_pool_size(
    attributes: dict[str, int],
    cfg_pool: Optional[int],
) -> int:
    """可自由分配的技能点总量：配置优先，否则 CoC 惯例 INT×2。"""
    if cfg_pool is not None:
        return cfg_pool
    return attributes.get("int", 50) * 2


def spent_points(adjustments: dict[str, int]) -> int:
    """已花费的点数（只计正向加点，减点归还额度）。"""
    return sum(v for v in adjustments.values() if v > 0)


def validate_adjustment(  # noqa: PLR0911,PLR0913
    attributes: dict[str, int],
    adjustments: dict[str, int],
    skill_key: str,
    delta: int,
    *,
    pool: int,
    cap: int,
) -> Optional[str]:
    """校验一次加点/减点；合法返回 None，否则返回错误描述。"""
    skill = _SKILL_BY_KEY.get(skill_key)
    if skill is None:
        return f"未知技能：{skill_key}"
    if skill.key == "cthulhu_mythos":
        return "克苏鲁神话不能在建卡时提升"
    if delta == 0:
        return "调整值不能为 0"
    base = base_skill_values(attributes)[skill_key]
    current = base + adjustments.get(skill_key, 0)
    new_value = current + delta
    if new_value < base:
        return f"{skill.name}不能低于初始值 {base}"
    if new_value > cap:
        return f"建卡期间单项技能不能超过 {cap}"
    if delta > 0:
        remaining = pool - spent_points(adjustments)
        if delta > remaining:
            return f"可分配点数不足（剩余 {remaining} 点）"
    return None


# ── 角色卡 ────────────────────────────────────────────────


@dataclass
class CharacterSheet:
    """一张角色卡：属性 + 技能加点调整。当前 HP/SAN 由引擎维护。"""

    name: str
    attributes: dict[str, int]
    # 技能加点增量（skill_key -> delta），建卡期由玩家调整
    adjustments: dict[str, int] = field(default_factory=dict)

    def skill_values(self) -> dict[str, int]:
        """技能最终值表（初始 + 加点）。"""
        values = base_skill_values(self.attributes)
        for key, delta in self.adjustments.items():
            if key in values:
                values[key] += delta
        return values

    def skill_value(self, skill_key: str) -> Optional[int]:
        """单技能最终值；未知技能返回 None。"""
        return self.skill_values().get(skill_key)

    @property
    def max_hp(self) -> int:
        """生命值上限。"""
        return derived_hp(self.attributes)

    @property
    def max_san(self) -> int:
        """理智上限。"""
        return derived_san(self.attributes)


def random_char_name(used: set[str]) -> str:
    """从名字池取一个未使用的角色名。"""
    for name in CHAR_NAMES:
        if name not in used:
            return name
    return f"调查员{len(used) + 1}"


def reroll_sheet(sheet: CharacterSheet) -> None:
    """整卡重掷：重掷属性并清空加点。"""
    sheet.attributes = roll_attributes()
    sheet.adjustments.clear()


def render_card(
    sheet: CharacterSheet,
    *,
    pool: int,
    rerolls_left: int,
    confirmed: bool,
) -> str:
    """渲染私聊角色卡文本。"""
    a = sheet.attributes
    values = sheet.skill_values()
    lines = [
        "═══ 角色卡 ═══",
        f"姓名：{sheet.name}",
        f"力量 {a['str']}  体质 {a['con']}  体型 {a['siz']}",
        f"敏捷 {a['dex']}  外貌 {a['app']}  智力 {a['int']}",
        f"意志 {a['pow']}  教育 {a['edu']}  幸运 {a['luck']}",
        f"HP {sheet.max_hp}  SAN {sheet.max_san}  "
        f"MP {derived_mp(a)}  DB {damage_bonus(a)}",
        "─── 技能 ───",
    ]
    skill_lines = [
        f"{_SKILL_BY_KEY[key].name} {values[key]}"
        for key in values
        if key != "cthulhu_mythos"
    ]
    lines.extend(
        "  ".join(skill_lines[i : i + 4]) for i in range(0, len(skill_lines), 4)
    )
    lines.append("──────────────")
    if confirmed:
        lines.append("角色卡已锁定。")
    else:
        spent = spent_points(sheet.adjustments)
        lines.append(
            f"可分配技能点：{pool - spent}/{pool}"
            f"（单项上限见群内说明）\n"
            f"加点 侦查 20 或 侦查+20｜减点同理｜重掷（剩 {rerolls_left} 次）\n"
            "查看 重发本卡｜确认 锁定角色卡"
        )
    return "\n".join(lines)
