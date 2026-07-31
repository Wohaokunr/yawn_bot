"""狼人杀角色定义、板子配置与身份卡文本。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal, Optional


class Role(str, Enum):
    """角色。value 为中文显示名，同时用作持久化标识。"""

    WEREWOLF = "狼人"
    VILLAGER = "村民"
    SEER = "预言家"
    WITCH = "女巫"
    HUNTER = "猎人"
    IDIOT = "白痴"
    HALFBLOOD = "混血儿"
    SILENT_ELDER = "禁言长老"
    KNIGHT = "骑士"


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
    KNIGHT_KILL = "KNIGHT_KILL"  # 骑士决斗致死（目标是狼人）
    KNIGHT_DEATH = "KNIGHT_DEATH"  # 骑士决斗到好人，骑士身亡


ROLE_FACTION: dict[Role, Faction] = {
    Role.WEREWOLF: Faction.WOLF,
    Role.VILLAGER: Faction.GOOD,
    Role.SEER: Faction.GOOD,
    Role.WITCH: Faction.GOOD,
    Role.HUNTER: Faction.GOOD,
    Role.IDIOT: Faction.GOOD,
    Role.HALFBLOOD: Faction.GOOD,  # 混血儿恒属民边，胜负条件另算（随主人）
    Role.SILENT_ELDER: Faction.GOOD,
    Role.KNIGHT: Faction.GOOD,
}

# 神职集合：屠边规则下狼人需屠尽的一侧（翻牌的白痴仍算存活神职）
GOD_ROLES: frozenset[Role] = frozenset(
    {Role.SEER, Role.WITCH, Role.HUNTER, Role.IDIOT, Role.KNIGHT, Role.SILENT_ELDER}
)

# 民边集合：屠边规则下狼人需屠尽的另一侧（混血儿算民边）
VILLAGER_SIDE_ROLES: frozenset[Role] = frozenset({Role.VILLAGER, Role.HALFBLOOD})

# ── 板子配置 ─────────────────────────────────────────────


@dataclass(frozen=True)
class BoardSpec:
    """板子配置：键名、各人数的角色构成、长老变种模式。

    silence_mode 仅长老板子非空："speech"=禁言长老，"vote"=禁票长老。
    """

    key: str
    counts: dict[int, dict[Role, int]]
    silence_mode: Optional[Literal["speech", "vote"]] = None

    def roles_summary(self) -> str:
        """按最大人数构成列出角色名（去重、保持配置顺序）。"""
        composition = self.counts[max(self.counts)]
        return "、".join(role.value for role in composition)

    def counts_summary(self) -> str:
        """支持的人数列表，如「9、10、11、12 人」。"""
        return "、".join(str(n) for n in sorted(self.counts))


BOARDS: dict[str, BoardSpec] = {
    "预女猎白": BoardSpec(
        key="预女猎白",
        counts={
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
        },
    ),
    "预女猎白混": BoardSpec(
        key="预女猎白混",
        counts={
            12: {
                Role.WEREWOLF: 4,
                Role.VILLAGER: 3,
                Role.SEER: 1,
                Role.WITCH: 1,
                Role.HUNTER: 1,
                Role.IDIOT: 1,
                Role.HALFBLOOD: 1,
            },
        },
    ),
    "禁言骑士": BoardSpec(
        key="禁言骑士",
        counts={
            12: {
                Role.WEREWOLF: 4,
                Role.VILLAGER: 4,
                Role.SEER: 1,
                Role.WITCH: 1,
                Role.KNIGHT: 1,
                Role.SILENT_ELDER: 1,
            },
        },
        silence_mode="speech",
    ),
    "禁票骑士": BoardSpec(
        key="禁票骑士",
        counts={
            12: {
                Role.WEREWOLF: 4,
                Role.VILLAGER: 4,
                Role.SEER: 1,
                Role.WITCH: 1,
                Role.KNIGHT: 1,
                Role.SILENT_ELDER: 1,
            },
        },
        silence_mode="vote",
    ),
}

DEFAULT_BOARD_KEY = "预女猎白"


def build_role_deck(board_key: str, player_count: int) -> list[Role]:
    """按板子与人数生成角色牌堆（未洗牌）。"""
    composition = BOARDS[board_key].counts[player_count]
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
    Role.HALFBLOOD: (
        "你属于民边。第一夜你会率先睁眼，私聊选择一位主人"
        "（回复 认主N，如 认主5），超时未选我会随机为你指定。\n"
        "你不知道主人的身份，主人也不知道被你选中。\n"
        "你的胜负随主人：主人所在阵营获胜，你才算获胜。"
    ),
    Role.SILENT_ELDER: (
        "每晚可以私聊禁言一位玩家（回复 禁言N，如 禁言5），或回复 过 放弃。\n"
        "被禁言的玩家次日发言阶段无法发言，禁言情况会与死讯同时公布。\n"
        "不能连续两晚禁言同一位玩家（放弃不算禁言）。"
    ),
    Role.KNIGHT: (
        "白天发言阶段（含平票 PK 发言期间），你可以在群内发送 /决斗N "
        "翻牌决斗一位玩家：\n"
        "决斗到狼人——该狼人立即死亡，白天流程终止直接进入黑夜；\n"
        "决斗到好人——双方身份公示，你死亡，白天流程继续。\n"
        "决斗成功后你仍存活且身份公开，之后的白天可以继续决斗。"
    ),
}

# 禁票长老变种文案（板子 silence_mode="vote" 时替换 SILENT_ELDER 的技能说明）
SILENT_ELDER_VOTE_SKILL_TEXT = (
    "每晚可以私聊封印一位玩家的投票权（回复 禁票N，如 禁票5），或回复 过 放弃。\n"
    "被禁票的玩家次日仍可发言，但放逐投票（含平票 PK 投票）无法投票，"
    "禁票情况会与死讯同时公布。\n"
    "不能连续两晚禁票同一位玩家（放弃不算禁票）。"
)


def build_role_card(
    seat: int,
    role: Role,
    player_count: int,
    *,
    silence_mode: Optional[Literal["speech", "vote"]] = None,
) -> str:
    """构建私聊发送的身份卡文本。"""
    faction = ROLE_FACTION[role]
    faction_name = "狼人阵营" if faction is Faction.WOLF else "好人阵营"
    if role is Role.SILENT_ELDER and silence_mode == "vote":
        skill_text = SILENT_ELDER_VOTE_SKILL_TEXT
    else:
        skill_text = ROLE_SKILL_TEXT[role]
    lines = [
        "═══ 狼人杀 · 身份卡 ═══",
        f"座位号：{seat} 号（本局共 {player_count} 人）",
        f"你的身份：{role.value}",
        f"你的阵营：{faction_name}",
        "─── 技能说明 ───",
        skill_text,
        "──────────────",
        "夜晚行动请私聊我完成；白天在群内按流程发言、投票。",
        "预祝你玩得开心~",
    ]
    return "\n".join(lines)
