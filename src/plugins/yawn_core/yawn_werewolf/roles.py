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
    GUARD = "守卫"
    WOLF_KING = "狼王"
    WHITE_WOLF_KING = "白狼王"
    HIDDEN_WOLF = "隐狼"
    GRAVEKEEPER = "守墓人"


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
    WOLF_KING_SHOT = "WOLF_KING_SHOT"  # 狼王死亡开枪带走
    WHITE_WOLF_KING_BOOM = "WHITE_WOLF_KING_BOOM"  # 白狼王自爆带走


# 死因中文（复盘、身份公示、战绩用）
DEATH_CAUSE_CN: dict[DeathCause, str] = {
    DeathCause.WOLF_KILL: "夜间被刀",
    DeathCause.WITCH_POISON: "被女巫毒杀",
    DeathCause.VOTED: "被投票放逐",
    DeathCause.HUNTER_SHOT: "被猎人带走",
    DeathCause.SELF_DETONATION: "自爆",
    DeathCause.KNIGHT_KILL: "被骑士决斗",
    DeathCause.KNIGHT_DEATH: "决斗失败殉职",
    DeathCause.WOLF_KING_SHOT: "被狼王带走",
    DeathCause.WHITE_WOLF_KING_BOOM: "被白狼王带走",
}


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
    Role.GUARD: Faction.GOOD,
    Role.WOLF_KING: Faction.WOLF,
    Role.WHITE_WOLF_KING: Faction.WOLF,
    Role.HIDDEN_WOLF: Faction.WOLF,
    Role.GRAVEKEEPER: Faction.GOOD,
}

# 狼人阵营的全部角色：夜间一起睁眼出刀、白天均可 /自爆
WOLF_ROLES: frozenset[Role] = frozenset(
    role for role, faction in ROLE_FACTION.items() if faction is Faction.WOLF
)

# 死亡时可以开枪带走一人的角色（被毒杀除外；狼王自爆也不能开枪）
SHOOTER_ROLES: frozenset[Role] = frozenset({Role.HUNTER, Role.WOLF_KING})


def seer_sees_wolf(role: Role) -> bool:
    """预言家查验结果：隐狼与混血儿都查验为好人。"""
    return role in WOLF_ROLES and role is not Role.HIDDEN_WOLF


# 神职集合：屠边规则下狼人需屠尽的一侧（翻牌的白痴仍算存活神职）
GOD_ROLES: frozenset[Role] = frozenset(
    {
        Role.SEER,
        Role.WITCH,
        Role.HUNTER,
        Role.IDIOT,
        Role.KNIGHT,
        Role.SILENT_ELDER,
        Role.GUARD,
        Role.GRAVEKEEPER,
    }
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
    # 一句话介绍（/板子 列表与 WebUI 展示）
    description: str = ""
    # 难度标签：新手 / 进阶 / 高阶
    level: str = "进阶"

    def composition_lines(self) -> list[str]:
        """逐人数列出角色构成，如「9 人：狼人×3、村民×3、预言家…」。"""
        lines: list[str] = []
        for count in sorted(self.counts):
            parts = [
                f"{role.value}×{n}" if n > 1 else role.value
                for role, n in self.counts[count].items()
            ]
            lines.append(f"{count} 人：{'、'.join(parts)}")
        return lines

    def roles_summary(self) -> str:
        """按最大人数构成列出角色名（去重、保持配置顺序）。"""
        composition = self.counts[max(self.counts)]
        return "、".join(role.value for role in composition)

    def counts_summary(self) -> str:
        """支持的人数列表，如「9、10、11、12 人」。"""
        return "、".join(str(n) for n in sorted(self.counts))

    def all_roles(self) -> frozenset[Role]:
        """全部人数配置的角色并集（报名阶段人数未定，选身份请求校验用）。"""
        roles: set[Role] = set()
        for composition in self.counts.values():
            roles.update(composition)
        return frozenset(roles)


BOARDS: dict[str, BoardSpec] = {
    "新手局": BoardSpec(
        key="新手局",
        description="6-8 人小局：只有预言家、女巫（8 人加猎人），规则最少，新手友好",
        level="新手",
        counts={
            6: {
                Role.WEREWOLF: 2,
                Role.VILLAGER: 2,
                Role.SEER: 1,
                Role.WITCH: 1,
            },
            7: {
                Role.WEREWOLF: 2,
                Role.VILLAGER: 3,
                Role.SEER: 1,
                Role.WITCH: 1,
            },
            8: {
                Role.WEREWOLF: 3,
                Role.VILLAGER: 2,
                Role.SEER: 1,
                Role.WITCH: 1,
                Role.HUNTER: 1,
            },
        },
    ),
    "预女猎白": BoardSpec(
        key="预女猎白",
        description="经典标准局：预言家、女巫、猎人、白痴（11 人起）",
        level="进阶",
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
    "预女猎守": BoardSpec(
        key="预女猎守",
        description="守卫替换白痴：守卫每晚守护一人，同守同救会让人倒牌",
        level="进阶",
        counts={
            10: {
                Role.WEREWOLF: 3,
                Role.VILLAGER: 3,
                Role.SEER: 1,
                Role.WITCH: 1,
                Role.HUNTER: 1,
                Role.GUARD: 1,
            },
            12: {
                Role.WEREWOLF: 4,
                Role.VILLAGER: 4,
                Role.SEER: 1,
                Role.WITCH: 1,
                Role.HUNTER: 1,
                Role.GUARD: 1,
            },
        },
    ),
    "预女猎白混": BoardSpec(
        key="预女猎白混",
        description="加入混血儿：首夜认主，胜负随主人阵营",
        level="高阶",
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
    "狼王守卫": BoardSpec(
        key="狼王守卫",
        description="狼王死亡可开枪，好人有守卫与猎人对抗",
        level="高阶",
        counts={
            12: {
                Role.WEREWOLF: 3,
                Role.WOLF_KING: 1,
                Role.VILLAGER: 4,
                Role.SEER: 1,
                Role.WITCH: 1,
                Role.HUNTER: 1,
                Role.GUARD: 1,
            },
        },
    ),
    "白狼王骑士": BoardSpec(
        key="白狼王骑士",
        description="白狼王可白天自爆带人，骑士可翻牌决斗，节奏极快",
        level="高阶",
        counts={
            12: {
                Role.WEREWOLF: 3,
                Role.WHITE_WOLF_KING: 1,
                Role.VILLAGER: 4,
                Role.SEER: 1,
                Role.WITCH: 1,
                Role.GUARD: 1,
                Role.KNIGHT: 1,
            },
        },
    ),
    "隐狼守墓": BoardSpec(
        key="隐狼守墓",
        description="隐狼被查验为好人，守墓人每晚得知昨日放逐者的阵营",
        level="高阶",
        counts={
            12: {
                Role.WEREWOLF: 3,
                Role.HIDDEN_WOLF: 1,
                Role.VILLAGER: 4,
                Role.SEER: 1,
                Role.WITCH: 1,
                Role.HUNTER: 1,
                Role.GRAVEKEEPER: 1,
            },
        },
    ),
    "禁言骑士": BoardSpec(
        key="禁言骑士",
        description="禁言长老每晚封一人次日发言，配合骑士决斗",
        level="高阶",
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
        description="禁票长老每晚封一人次日投票，配合骑士决斗",
        level="高阶",
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


def suggest_boards(player_count: int) -> list[str]:
    """支持该人数的板子键名（报名人数不匹配时给出建议）。"""
    return [key for key, spec in BOARDS.items() if player_count in spec.counts]


def resolve_board_key(text: str) -> Optional[str]:
    """板子名宽松匹配：精确 > 唯一前缀/包含；无匹配或有歧义返回 None。"""
    name = text.strip()
    if name in BOARDS:
        return name
    hits = [key for key in BOARDS if name and (key.startswith(name) or name in key)]
    return hits[0] if len(hits) == 1 else None


DEFAULT_BOARD_KEY = "预女猎白"


def build_role_deck(board_key: str, player_count: int) -> list[Role]:
    """按板子与人数生成角色牌堆（未洗牌）。"""
    composition = BOARDS[board_key].counts[player_count]
    deck: list[Role] = []
    for role, count in composition.items():
        deck.extend([role] * count)
    return deck


# 角色常用别称（/选身份、/狼人规则 等宽松输入）
ROLE_ALIASES: dict[str, Role] = {
    "狼": Role.WEREWOLF,
    "狼人": Role.WEREWOLF,
    "民": Role.VILLAGER,
    "平民": Role.VILLAGER,
    "预言": Role.SEER,
    "预": Role.SEER,
    "女": Role.WITCH,
    "猎": Role.HUNTER,
    "白": Role.IDIOT,
    "混": Role.HALFBLOOD,
    "长老": Role.SILENT_ELDER,
    "禁票长老": Role.SILENT_ELDER,
    "守": Role.GUARD,
    "黑狼王": Role.WOLF_KING,
    "白狼": Role.WHITE_WOLF_KING,
    "守墓": Role.GRAVEKEEPER,
}


def parse_role(text: str) -> Optional[Role]:
    """按中文名或常用别称解析角色（去首尾空白），无匹配返回 None。"""
    name = text.strip()
    for role in Role:
        if role.value == name:
            return role
    return ROLE_ALIASES.get(name)


# ── 身份卡文本 ────────────────────────────────────────────

ROLE_SKILL_TEXT: dict[Role, str] = {
    Role.WEREWOLF: (
        "每晚与其他狼人一起私聊选择一名玩家击杀（回复 刀N，如 刀3），"
        "或回复 过 明确选择空刀。\n"
        "夜里可回复 说XXX（如 说刀5），我会把你的发言转发给其他狼人。\n"
        "白天你可以私聊或在群内发送 /自爆 亮明身份自尽，"
        "立即终止当天流程直接进入夜晚。"
    ),
    Role.GUARD: (
        "每晚可以私聊守护一名玩家（回复 守护N，如 守护3；可以守自己），"
        "被守护者当夜不会被狼人刀死；或回复 过 空守。\n"
        "不能连续两晚守护同一名玩家（空守不算）。\n"
        "注意：同一人同时被守护和被女巫解药救，会因“同守同救”而死亡（房规可关）。"
    ),
    Role.WOLF_KING: (
        "你是狼王，与狼队友一起夜间出刀（刀N / 过 / 说XXX）。\n"
        "当你出局时（被女巫毒死和自爆除外），可以私聊开枪带走一名玩家"
        "（回复 开枪N），或回复 不开枪。"
    ),
    Role.WHITE_WOLF_KING: (
        "你是白狼王，与狼队友一起夜间出刀（刀N / 过 / 说XXX）。\n"
        "白天发言阶段，你可以在群内发送 /自爆 N 亮明身份，"
        "并带走 N 号玩家一同出局，随后立即进入黑夜。\n"
        "普通的 /自爆（不带座位）只会让你自己出局。"
    ),
    Role.HIDDEN_WOLF: (
        "你是隐狼，与狼队友一起夜间出刀（刀N / 过 / 说XXX）。\n"
        "预言家查验你时，结果会显示为“好人”——好好利用这层伪装。"
    ),
    Role.GRAVEKEEPER: (
        "从第二晚起，每晚我会私聊告诉你：前一个白天被投票放逐的玩家"
        "是好人还是狼人（当天无人被放逐则没有信息）。你无需回复任何指令。"
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


# 各阵营的胜利条件（身份卡与规则说明用）
WIN_CONDITION_TEXT: dict[Faction, str] = {
    Faction.WOLF: "狼人阵营：屠边——神职全灭或平民全灭即获胜（房规可改为屠城）",
    Faction.GOOD: "好人阵营：把所有狼人放逐或击杀即获胜",
}

# 给新手的一句话打法提示（身份卡末尾）
ROLE_TIPS: dict[Role, str] = {
    Role.WEREWOLF: "白天把自己伪装成好人，可以悍跳预言家，别和队友互踩穿帮。",
    Role.VILLAGER: "认真听发言、记票型，跟可信的神职走，别乱跳身份。",
    Role.SEER: "尽早留下查验信息（金水/查杀），遗言要报全查验。",
    Role.WITCH: "解药救关键人，毒药留给确认的狼；首夜救人是常见打法。",
    Role.HUNTER: "死后开枪要谨慎，带走最像狼的人，别误伤好人。",
    Role.IDIOT: "被投出去翻牌免死，所以敢于站边、敢于发言。",
    Role.HALFBLOOD: "先观察，再暗中帮主人所在的阵营。",
    Role.SILENT_ELDER: "禁掉最会带节奏的人，但别连续两晚同一人。",
    Role.KNIGHT: "有八成把握再决斗，决斗成功直接入夜。",
    Role.GUARD: "首夜常见守预言家或空守；别和女巫同守同救。",
    Role.WOLF_KING: "被投出时开枪带走神职能扭转局面。",
    Role.WHITE_WOLF_KING: "在关键时刻自爆带走预言家或警长。",
    Role.HIDDEN_WOLF: "大胆接受查验，你会被验成好人。",
    Role.GRAVEKEEPER: "用守墓信息校验预言家的真假，适时跳出来带队。",
}


def role_guide(role: Role, *, silence_mode: Optional[str] = None) -> str:
    """单个角色的规则说明（/狼人规则 角色名）。"""
    if role is Role.SILENT_ELDER and silence_mode == "vote":
        skill = SILENT_ELDER_VOTE_SKILL_TEXT
    else:
        skill = ROLE_SKILL_TEXT[role]
    faction = ROLE_FACTION[role]
    faction_name = "狼人阵营" if faction is Faction.WOLF else "好人阵营"
    kind = (
        "神职" if role in GOD_ROLES else ("狼人" if faction is Faction.WOLF else "平民")
    )
    boards = "、".join(k for k, spec in BOARDS.items() if role in spec.all_roles())
    return "\n".join(
        [
            f"═══ 角色说明 · {role.value} ═══",
            f"阵营：{faction_name}（{kind}）",
            "─── 技能 ───",
            skill,
            "─── 打法提示 ───",
            ROLE_TIPS.get(role, ""),
            f"出现在板子：{boards or '无'}",
        ]
    )


# 白天常用指令速查（所有身份通用；身份卡与 help 之外的唯一入门教学）
_DAY_COMMAND_CHEATSHEET: tuple[str, ...] = (
    "/上警、/退水：竞选警长 / 退出竞选",
    "/投票 N（或 /投票 @某人）、/弃票：投票 / 放弃投票",
    "/过：提前结束自己的发言",
    "/排序 N 顺|逆（警长）：决定发言顺序",
    "/移交警徽 N、/撕警徽（死亡警长）：处置警徽",
    "/狼人状态：查看存活、警长与剩余时间",
)


def build_role_card(  # noqa: PLR0913
    seat: int,
    role: Role,
    player_count: int,
    *,
    silence_mode: Optional[Literal["speech", "vote"]] = None,
    roster: Optional[list[tuple[int, str]]] = None,
    rule_notes: Optional[list[str]] = None,
) -> str:
    """构建私聊发送的身份卡文本。

    roster 为 (座位, 显示名) 列表，全体玩家收到相同的一份，
    不标注 AI 座位。首行须保持 "═══ 狼人杀 · 身份卡 ═══"
    （AI 驱动按此头跳过卡片，见 ai_player._ROLE_CARD_HEADER）。
    """
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
        f"胜利条件：{WIN_CONDITION_TEXT[faction]}"
        if role is not Role.HALFBLOOD
        else "胜利条件：主人所在阵营获胜",
        "─── 技能说明 ───",
        skill_text,
        f"小提示：{ROLE_TIPS[role]}",
        "─── 白天常用指令 ───",
        *_DAY_COMMAND_CHEATSHEET,
    ]
    if rule_notes:
        lines.append("─── 本局房规 ───")
        lines.extend(rule_notes)
    if roster:
        lines.append("─── 本局名单 ───")
        lines.extend(f"{r_seat}号：{name}" for r_seat, name in roster)
    lines.extend(
        (
            "──────────────",
            "夜晚行动请私聊我完成；白天在群内按流程发言、投票。",
            "预祝你玩得开心~",
        )
    )
    return "\n".join(lines)
