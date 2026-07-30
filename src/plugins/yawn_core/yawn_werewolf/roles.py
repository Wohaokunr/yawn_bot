"""狼人杀角色定义、板子配置与身份卡文本。"""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    """角色。value 为中文显示名，同时用作持久化标识。"""

    WEREWOLF = "狼人"
    VILLAGER = "村民"
    SEER = "预言家"
    WITCH = "女巫"
    HUNTER = "猎人"
    IDIOT = "白痴"


class Faction(str, Enum):
    """阵营。value 用作持久化标识。"""

    WOLF = "wolf"
    GOOD = "good"


class DeathCause(str, Enum):
    """死因。value 用作持久化标识。"""

    WOLF_KILL = "WOLF_KILL"  # 狼人刀杀
    WITCH_POISON = "WITCH_POISON"  # 女巫毒杀
    VOTED = "VOTED"  # 白天放逐
    HUNTER_SHOT = "HUNTER_SHOT"  # 猎人开枪带走
    SELF_DETONATION = "SELF_DETONATION"  # 狼人自爆


ROLE_FACTION: dict[Role, Faction] = {
    Role.WEREWOLF: Faction.WOLF,
    Role.VILLAGER: Faction.GOOD,
    Role.SEER: Faction.GOOD,
    Role.WITCH: Faction.GOOD,
    Role.HUNTER: Faction.GOOD,
    Role.IDIOT: Faction.GOOD,
}

# 神职集合：屠边规则下狼人需屠尽的一侧（翻牌的白痴仍算存活神职）
GOD_ROLES: frozenset[Role] = frozenset({Role.SEER, Role.WITCH, Role.HUNTER, Role.IDIOT})

# 各人数的角色配置：人数 -> {角色: 数量}
ROLE_COMPOSITION: dict[int, dict[Role, int]] = {
    9: {
        Role.WEREWOLF: 3,
        Role.VILLAGER: 3,
        Role.SEER: 1,
        Role.WITCH: 1,
        Role.HUNTER: 1,
    },
    10: {
        Role.WEREWOLF: 3,
        Role.VILLAGER: 4,
        Role.SEER: 1,
        Role.WITCH: 1,
        Role.HUNTER: 1,
    },
    11: {
        Role.WEREWOLF: 3,
        Role.VILLAGER: 4,
        Role.SEER: 1,
        Role.WITCH: 1,
        Role.HUNTER: 1,
        Role.IDIOT: 1,
    },
    12: {
        Role.WEREWOLF: 4,
        Role.VILLAGER: 4,
        Role.SEER: 1,
        Role.WITCH: 1,
        Role.HUNTER: 1,
        Role.IDIOT: 1,
    },
}


def build_role_deck(player_count: int) -> list[Role]:
    """按人数生成角色牌堆（未洗牌）。"""
    composition = ROLE_COMPOSITION[player_count]
    deck: list[Role] = []
    for role, count in composition.items():
        deck.extend([role] * count)
    return deck


# ── 身份卡文本 ────────────────────────────────────────────

ROLE_SKILL_TEXT: dict[Role, str] = {
    Role.WEREWOLF: (
        "每晚与其他狼人一起私聊选择一名玩家击杀（回复 刀N，如 刀3）。\n"
        "夜里可回复 说XXX（如 说刀5），我会把你的发言转发给其他狼人。\n"
        "白天你可以私聊或在群内发送 /自爆 亮明身份自尽，"
        "立即终止当天流程直接进入夜晚。"
    ),
    Role.VILLAGER: "没有特殊技能，靠发言和投票找出狼人。",
    Role.SEER: (
        "每晚可以私聊查验一名玩家的身份（回复 查验N，如 查验5），"
        "我会告诉你 TA 是狼人还是好人。"
    ),
    Role.WITCH: (
        "你有一瓶解药和一瓶毒药，各限用一次，且全程不可自救。\n"
        "每晚我会私聊告诉你被刀的玩家，你可以回复：\n"
        "救（救活被刀的玩家）、毒N（毒杀 N 号）或 过（不使用）。"
    ),
    Role.HUNTER: (
        "当你被狼人刀杀或被投票放逐时（被女巫毒死除外），"
        "可以私聊开枪带走一名玩家（回复 开枪N），"
        "或回复 不开枪。"
    ),
    Role.IDIOT: (
        "当你被投票放逐时，可以翻开身份免于一死，但从此失去投票权，也不能再被投票。"
    ),
}


def build_role_card(seat: int, role: Role, player_count: int) -> str:
    """构建私聊发送的身份卡文本。"""
    faction = ROLE_FACTION[role]
    faction_name = "狼人阵营" if faction is Faction.WOLF else "好人阵营"
    lines = [
        "═══ 狼人杀 · 身份卡 ═══",
        f"座位号：{seat} 号（本局共 {player_count} 人）",
        f"你的身份：{role.value}",
        f"你的阵营：{faction_name}",
        "─── 技能说明 ───",
        ROLE_SKILL_TEXT[role],
        "──────────────",
        "夜晚行动请私聊我完成；白天在群内按流程发言、投票。",
        "预祝你玩得开心~",
    ]
    return "\n".join(lines)
