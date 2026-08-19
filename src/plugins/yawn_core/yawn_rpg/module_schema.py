"""模组数据模式：YAML 剧本模组的定义、校验与加载。

模组是 KP 工具调用的边界数据：KP 只能在模组定义的场景 /
线索 / NPC / 怪物 / 结局范围内调用工具，越界调用由引擎拒
绝。条件表达式（出口与结局）由 evaluate_condition 确定性
求值，AI 不参与。坏模组在加载时 warning 跳过，不影响其他
模组与插件加载。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

import yaml
from nonebot import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .charsheet import SKILLS

# 骰表达式：NdM(±K) 或纯整数，如 1d6 / 1d6+1 / 3
_DICE_EXPR_RE = re.compile(r"(\d+)d(\d+)([+-]\d+)?|\d+")

# 骰数与面数上限（与 dice.py 的同名常量保持一致）：
# 坏模组写 1d0 / 巨骰会在运行时崩溃或冻死事件循环
_MAX_DICE_COUNT = 100
_MAX_DICE_SIDES = 1000

# 检定点可用技能：san（理智检定）或技能表 key
_VALID_CHECK_SKILLS = frozenset({"san", *(s.key for s in SKILLS)})

# 结局倾向的合法取值（models.RPGGame.outcome 仅 String(8)）
_VALID_OUTCOMES = ("good", "bad", "neutral")
_RELATION_MIN = -100
_RELATION_MAX = 100


class CheckDifficulty(str, Enum):
    """检定难度（CoC 7版）：常规 / 困难(½) / 极难(⅕)。"""

    REGULAR = "regular"
    HARD = "hard"
    EXTREME = "extreme"


class CheckMode(str, Enum):
    """检定结算方式；缺省个人检定以兼容既有模组。"""

    INDIVIDUAL = "individual"
    TEAM = "team"


_SOCIAL_SKILLS = frozenset({"persuade", "fast_talk", "intimidate"})


class NPCFact(BaseModel):
    """只可经 NPC 社交解锁的私人情报。"""

    id: str
    name: str
    text: str

    @model_validator(mode="after")
    def _check_text(self) -> "NPCFact":
        if not self.id.strip() or not self.name.strip() or not self.text.strip():
            raise ValueError("NPC 情报的 id、name、text 均不能为空")  # noqa: TRY003
        return self


class SocialStrategy(BaseModel):
    """社交节点的一种交涉策略；技能与成败效果由系统确定性结算。"""

    skill: str
    difficulty: CheckDifficulty = CheckDifficulty.REGULAR
    name: str = ""
    success_rapport_delta: Optional[int] = None
    success_attitude_delta: Optional[int] = None
    failure_rapport_delta: Optional[int] = None
    failure_attitude_delta: Optional[int] = None
    success_text: Optional[str] = None
    failure_text: Optional[str] = None

    @model_validator(mode="after")
    def _check_skill(self) -> "SocialStrategy":
        if self.skill not in _SOCIAL_SKILLS:
            allowed = "、".join(sorted(_SOCIAL_SKILLS))
            raise ValueError(f"社交策略 skill 必须是 {allowed}")  # noqa: TRY003
        return self


class SocialNode(BaseModel):
    """NPC 的可重复尝试剧情诉求。"""

    id: str
    name: str
    # 只给路由器看的安全目标描述，不得写入隐藏奖励或机密正文
    goal: str
    strategies: list[SocialStrategy]
    requires_facts: list[str] = []
    min_rapport: int = -100
    min_attitude: int = -100
    max_attempts: int = 3
    retry_rapport_penalty: int = 2
    retry_attitude_penalty: int = 1
    success_rapport_delta: int = 15
    success_attitude_delta: int = 5
    failure_rapport_delta: int = -5
    failure_attitude_delta: int = -2
    success_text: str = ""
    failure_text: str = ""
    unlock_facts: list[str] = []
    private_clues: list[str] = []
    public_clues: list[str] = []
    success_flags: list[str] = []
    failure_flags: list[str] = []

    @model_validator(mode="after")
    def _check_fields(self) -> "SocialNode":
        if not self.id.strip() or not self.name.strip() or not self.goal.strip():
            raise ValueError("社交节点的 id、name、goal 均不能为空")  # noqa: TRY003
        if not self.strategies:
            raise ValueError(f"社交节点 {self.id} 至少需要一种策略")  # noqa: TRY003
        if self.max_attempts < 1:
            raise ValueError(  # noqa: TRY003
                f"社交节点 {self.id} 的 max_attempts 必须至少为 1"
            )
        if not _RELATION_MIN <= self.min_rapport <= _RELATION_MAX:
            raise ValueError(  # noqa: TRY003
                f"社交节点 {self.id} 的 min_rapport 必须在 -100~100 内"
            )
        if not _RELATION_MIN <= self.min_attitude <= _RELATION_MAX:
            raise ValueError(  # noqa: TRY003
                f"社交节点 {self.id} 的 min_attitude 必须在 -100~100 内"
            )
        for value_name in (
            "retry_rapport_penalty",
            "retry_attitude_penalty",
        ):
            if getattr(self, value_name) < 0:
                raise ValueError(  # noqa: TRY003
                    f"社交节点 {self.id} 的 {value_name} 不能为负数"
                )
        return self

    def strategy(self, skill: str) -> Optional[SocialStrategy]:
        """按技能查找策略。"""
        return next((item for item in self.strategies if item.skill == skill), None)


class CheckPoint(BaseModel):
    """场景检定点：玩家发言关键词命中或 KP 工具调用时触发。"""

    id: str
    # 技能 key（charsheet.SKILLS 的键）；"san" 表示理智检定
    skill: str
    difficulty: CheckDifficulty = CheckDifficulty.REGULAR
    mode: CheckMode = CheckMode.INDIVIDUAL
    # 团队检定成功所需人数；缺省为参与者过半。
    required_successes: Optional[int] = None
    # 玩家发言关键词（子串匹配，大小写不敏感）
    triggers: list[str] = []
    # 多个检定点同时命中时大者优先
    priority: int = 0
    # 整局最多触发一次
    once: bool = False
    # 成功/失败时由引擎播报的固定文案（KP 不参与数值与结果）
    success_text: str
    failure_text: str = ""
    # 成功时奖励的线索 id
    clue: Optional[str] = None
    # SAN 检定损失："成功侧/失败侧"，如 "1/1d6"；skill=san 时必填
    san_loss: Optional[str] = None
    # 失败伤害骰表达式，如 "1d3"
    damage_on_fail: Optional[str] = None
    # 本检定点结算消耗的游戏内分钟数（缺省用引擎的 check 默认值）
    time_cost: Optional[int] = None

    @model_validator(mode="after")
    def _check_fields(self) -> "CheckPoint":
        if self.required_successes is not None and self.required_successes < 1:
            msg = f"检定点 {self.id}：required_successes 必须至少为 1"
            raise ValueError(msg)
        if self.mode is CheckMode.INDIVIDUAL and self.required_successes is not None:
            msg = f"检定点 {self.id}：仅团队检定可设置 required_successes"
            raise ValueError(msg)
        if self.skill == "san" and not self.san_loss:
            msg = f"检定点 {self.id}：san 检定必须给出 san_loss"
            raise ValueError(msg)
        if self.san_loss is not None and not _is_san_loss(self.san_loss):
            msg = f"检定点 {self.id}：san_loss 格式应为 成功/失败（如 1/1d6）"
            raise ValueError(msg)
        if self.damage_on_fail and not _is_dice_expr(self.damage_on_fail):
            msg = f"检定点 {self.id}：damage_on_fail 骰表达式非法"
            raise ValueError(msg)
        return self


def _is_dice_expr(text: str) -> bool:
    """是否为合法骰表达式（NdM±K 或整数；骰数 / 面数有上限）。"""
    match = _DICE_EXPR_RE.fullmatch(text.strip())
    if match is None:
        return False
    if match.group(1) is None:  # 命中纯整数分支
        return True
    return (
        1 <= int(match.group(1)) <= _MAX_DICE_COUNT
        and 1 <= int(match.group(2)) <= _MAX_DICE_SIDES
    )


def _is_san_loss(text: str) -> bool:
    """是否为合法 SAN 损失表达式 "成功侧/失败侧"。"""
    left, sep, right = text.partition("/")
    if not sep:
        return False
    return all(p.strip() and _is_dice_expr(p.strip()) for p in (left, right))


# HH:MM 时刻（允许单位小时，如 6:00）
_HHMM_RE = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")


def _parse_hhmm(text: str) -> Optional[int]:
    """解析 HH:MM 为自当日 0:00 起的分钟数；非法返回 None。"""
    match = _HHMM_RE.fullmatch(text.strip())
    if match is None:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def _coerce_hhmm(value: object) -> object:
    """兼容 YAML 六十进制陷阱：未加引号的 21:00 会被 PyYAML（YAML 1.1）
    解析为整数 1260，这里还原为 "21:00"，避免整个模组以难懂的类型
    报错被跳过。其余类型原样交给后续校验。
    """
    if isinstance(value, int):
        hours, minutes = divmod(value, 60)
        return f"{hours}:{minutes:02d}"
    return value


def _in_window(t: int, frm: int, to: int) -> bool:
    """判断时刻 t 是否落在 [frm, to) 时段内（自 0:00 起的分钟数）。

    支持跨午夜（frm > to，如 23:00-02:00）；含起点不含终点；
    frm == to 视为全天窗口。行程时段与 time_between 条件词
    共用这一份实现，避免两处各写一套跨午夜逻辑。
    """
    if frm == to:
        return True
    if frm < to:
        return frm <= t < to
    return t >= frm or t < to


def _validate_condition(  # noqa: C901,PLR0911,PLR0912
    condition: Optional[str],
    scenes: set[str],
    monsters: set[str],
    clues: set[str],
    deductions: set[str] | None = None,
) -> Optional[str]:
    """校验条件表达式内的引用；合法返回 None，否则返回错误描述。

    与 evaluate_condition 的语法保持一致：把悬空引用拦在加载期，
    避免运行时保守判 False 造成无诊断的永久软锁。
    """
    if not condition:
        return None
    for part in condition.split("&"):
        term = part.strip()
        if not term:
            return "条件含空词条（& 拼接有误，如悬挂的 &）"
        if term in ("always", "all_players_incapped"):
            continue
        kind, sep, value = term.partition(":")
        if not sep or not value:
            return f"词条格式非法：{term!r}"
        if kind == "clue":
            if value not in clues:
                return f"引用了未定义的线索 {value!r}"
        elif kind == "clues":
            parts = [v for v in value.split("+") if v]
            if not parts:
                return f"clues 至少需要一个线索：{term!r}"
            missing = [v for v in parts if v not in clues]
            if missing:
                return f"引用了未定义的线索 {'、'.join(missing)}"
        elif kind == "monster_dead":
            if value not in monsters:
                return f"引用了未定义的怪物 {value!r}"
        elif kind == "deduction":
            if value not in (deductions or set()):
                return f"引用了未定义的推论 {value!r}"
        elif kind == "deductions":
            parts = [v for v in value.split("+") if v]
            if not parts:
                return f"deductions 至少需要一个推论：{term!r}"
            missing = [v for v in parts if v not in (deductions or set())]
            if missing:
                return f"引用了未定义的推论 {'、'.join(missing)}"
        elif kind == "scene":
            if value not in scenes:
                return f"引用了未定义的场景 {value!r}"
        elif kind in ("time_after", "time_before"):
            if _parse_hhmm(value) is None:
                return f"时间格式应为 HH:MM：{term!r}"
        elif kind == "time_between":
            left, tsep, right = value.partition("-")
            if (
                not tsep
                or _parse_hhmm(left.strip()) is None
                or _parse_hhmm(right.strip()) is None
            ):
                return f"time_between 格式应为 HH:MM-HH:MM：{term!r}"
        elif kind == "flag":
            # flag 是开放的运行时集合，只做语法校验
            name, ge, num = value.partition(">=")
            if not name:
                return f"flag 词条缺少名称：{term!r}"
            if ge and not (num.isascii() and num.isdigit()):
                return f"flag 计数阈值必须是整数：{term!r}"
        else:
            return f"未知的条件词条：{term!r}"
    return None


class Exit(BaseModel):
    """场景出口：condition 由引擎对 transition_scene 强制执行。"""

    to_scene: str
    # 条件表达式（见 evaluate_condition）；空=始终可通行
    condition: Optional[str] = None
    # 玩家 /前往 的匹配关键词（目标场景名默认可用，无需重复）
    keywords: list[str] = []
    # 条件满足时自动切景（不依赖 KP 与玩家指令）
    auto: bool = False
    # 切景时引擎播报的固定转场文案
    narration: str = ""
    # 走这条出口消耗的游戏内分钟数（缺省用引擎的 move 默认值）
    time_cost: Optional[int] = None


class Scene(BaseModel):
    """场景：KP 提示词与工具调用的边界单元。"""

    id: str
    name: str
    narration: str
    # 在场 NPC / 怪物 id（引用于模组级定义）
    npcs: list[str] = []
    monsters: list[str] = []
    checks: list[CheckPoint] = []
    exits: list[Exit] = []
    # KP 叙述失败时的兜底氛围文案
    idle_narration: Optional[str] = None


class ScheduleEntry(BaseModel):
    """NPC 行程条目：[from, to) 时段内 NPC 身在何处、在做何事。

    窗口支持跨午夜（from > to，如 23:00-02:00）；from == to
    视为全天窗口。同一 NPC 的条目按声明序匹配，第一条"条件
    成立且时钟落在窗口"的条目生效——作者需自行保证优先级。
    """

    # YAML 里写作 from:（from 是 Python 关键字，字段名用 frm）
    model_config = ConfigDict(populate_by_name=True)

    frm: str = Field(alias="from")
    to: str
    # NPC 所在场景 id；away=False 时必填
    scene: Optional[str] = None
    # NPC 正在做的事（公开 flavor，进 KP 局面提示）
    activity: str = ""
    # 条目生效条件（见 evaluate_condition）；空=始终生效
    condition: str = ""
    # True 表示外出（不在任何场景）；此时无需 scene
    away: bool = False

    @field_validator("frm", "to", mode="before")
    @classmethod
    def _coerce_times(cls, value: object) -> object:
        return _coerce_hhmm(value)

    @model_validator(mode="after")
    def _check_fields(self) -> "ScheduleEntry":
        if _parse_hhmm(self.frm) is None:
            msg = f"行程 from 时间格式非法：{self.frm!r}（应为 HH:MM）"
            raise ValueError(msg)
        if _parse_hhmm(self.to) is None:
            msg = f"行程 to 时间格式非法：{self.to!r}（应为 HH:MM）"
            raise ValueError(msg)
        if not self.away and not self.scene:
            msg = "行程条目未标记 away 时必须给出 scene"
            raise ValueError(msg)
        return self


class TimeConfig(BaseModel):
    """游戏内时钟配置：起始时刻 + 各行动类型耗时覆写。"""

    # 开局游戏内时刻 HH:MM
    start: str = "20:00"
    # 按行动类型覆写消耗分钟数（键如 say/talk/check/move/attack/wait）
    costs: dict[str, int] = {}

    @field_validator("start", mode="before")
    @classmethod
    def _coerce_start(cls, value: object) -> object:
        return _coerce_hhmm(value)

    @model_validator(mode="after")
    def _check_start(self) -> "TimeConfig":
        if _parse_hhmm(self.start) is None:
            msg = f"time.start 时间格式非法：{self.start!r}（应为 HH:MM）"
            raise ValueError(msg)
        return self

    @property
    def start_minutes(self) -> int:
        """起始时刻折算为自 0:00 起的分钟数（加载期已校验）。"""
        return _parse_hhmm(self.start) or 0


class NPC(BaseModel):
    """NPC：secrets 只进该 NPC 自己的智能体提示词（ai_npc，附不主动
    透露指令），永不进 KP 提示词。

    NPC 可被玩家攻击、会反击、可死亡（数值镜像 Monster，
    全部带默认值，旧模组不写也能用）。
    """

    id: str
    name: str
    # 给 KP 看的公开简介（安全信息；进 KP 开局概览）
    public_desc: str
    # NPC 对话人格（NPC 智能体的扮演依据；进 KP 开局概览）
    persona: str
    # 可透露给玩家的信息块（进 NPC 智能体提示词与 KP 开局概览）
    knows: list[str] = []
    # 绝密信息：只注入该 NPC 自己的智能体提示词（KP 永不可见）；
    # 加载校验确保其不出现在会进 KP 概览的字段里
    secrets: list[str] = []
    # LLM 失败时的罐头回复
    fallback_line: str = ""
    # ── 社交状态与剧情节点 ──
    # 初始值只作为运行时状态的种子；引擎会把关系钳制在 -100~100。
    initial_rapport: int = 0
    initial_attitude: int = 0
    facts: list[NPCFact] = []
    social_nodes: list[SocialNode] = []
    # ── 生活行程 ──
    # 行程条目（声明序=匹配优先级）；为空表示常驻 scene.npcs 所列场景
    schedule: list[ScheduleEntry] = []
    # ── 战斗数值（镜像 Monster；引擎结算，KP 不碰）──
    hp: int = 10
    attack_skill: int = 40
    attack_name: str = "攻击"
    damage: str = "1d3"
    dodge: int = 30
    on_death_clue: Optional[str] = None
    on_death_text: str = ""

    @model_validator(mode="after")
    def _no_secret_leak(self) -> "NPC":
        haystack = " ".join(
            (
                self.persona,
                self.public_desc,
                self.fallback_line,
                self.on_death_text,
                *self.knows,
                *(e.activity for e in self.schedule),
                *(node.name for node in self.social_nodes),
                *(node.goal for node in self.social_nodes),
                *(node.success_text for node in self.social_nodes),
                *(node.failure_text for node in self.social_nodes),
                *(
                    strategy.name
                    for node in self.social_nodes
                    for strategy in node.strategies
                ),
                *(
                    strategy.success_text or ""
                    for node in self.social_nodes
                    for strategy in node.strategies
                ),
                *(
                    strategy.failure_text or ""
                    for node in self.social_nodes
                    for strategy in node.strategies
                ),
            )
        )
        for secret in self.secrets:
            if secret and secret in haystack:
                msg = (
                    f"NPC {self.id}：secret 出现在 persona/公开信息中，"
                    "会随 KP 开局概览泄露给 KP"
                )
                raise ValueError(msg)
        for fact in self.facts:
            if fact.text in haystack or fact.name in haystack:
                msg = (
                    f"NPC {self.id}：私人情报 {fact.id} 出现在公开 NPC 字段中，"
                    "会随 KP 概览或 NPC 场景信息泄露"
                )
                raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _check_social_state(self) -> "NPC":
        if not _RELATION_MIN <= self.initial_rapport <= _RELATION_MAX:
            raise ValueError(  # noqa: TRY003
                f"NPC {self.id}：initial_rapport 必须在 -100~100 内"
            )
        if not _RELATION_MIN <= self.initial_attitude <= _RELATION_MAX:
            raise ValueError(  # noqa: TRY003
                f"NPC {self.id}：initial_attitude 必须在 -100~100 内"
            )
        fact_ids = [fact.id for fact in self.facts]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError(f"NPC {self.id}：facts id 不能重复")  # noqa: TRY003
        node_ids = [node.id for node in self.social_nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError(f"NPC {self.id}：social_nodes id 不能重复")  # noqa: TRY003
        for node in self.social_nodes:
            skills = [strategy.skill for strategy in node.strategies]
            if len(skills) != len(set(skills)):
                raise ValueError(  # noqa: TRY003
                    f"NPC {self.id} 社交节点 {node.id}：同一技能策略不能重复"
                )
        return self

    @model_validator(mode="after")
    def _check_damage(self) -> "NPC":
        if not _is_dice_expr(self.damage):
            msg = f"NPC {self.id}：damage 骰表达式非法：{self.damage!r}"
            raise ValueError(msg)
        return self


class Monster(BaseModel):
    """怪物：数值由引擎结算，KP 只能触发 monster_attack。"""

    id: str
    name: str
    hp: int
    # 攻击技能百分比
    attack_skill: int
    attack_name: str = "攻击"
    # 伤害骰表达式
    damage: str
    # 闪避百分比（玩家攻击时的对抗目标；None=无法闪避）
    dodge: Optional[int] = None
    on_death_clue: Optional[str] = None
    on_death_text: str = ""

    @model_validator(mode="after")
    def _check_damage(self) -> "Monster":
        if not _is_dice_expr(self.damage):
            msg = f"怪物 {self.id}：damage 骰表达式非法：{self.damage!r}"
            raise ValueError(msg)
        return self


class Clue(BaseModel):
    """线索：name 进 KP 提示词，text 仅发现时由引擎播报。"""

    id: str
    name: str
    text: str
    category: str = "线索"
    source_hint: str = ""


class Deduction(BaseModel):
    """由系统确定性裁决的团队推论。"""

    id: str
    name: str
    required_clues: list[str]
    conclusion_keywords: list[list[str]]
    success_text: str
    failure_hint: str = "现有证据还不足以支持这个结论。"
    unlock_flags: list[str] = []
    grant_clues: list[str] = []
    once: bool = True
    failure_time_cost: int = 5

    @model_validator(mode="after")
    def _check_fields(self) -> "Deduction":
        if not self.required_clues or len(self.required_clues) != len(
            set(self.required_clues)
        ):
            raise ValueError(  # noqa: TRY003
                f"推论 {self.id}：required_clues 必须非空且不可重复"
            )
        if not self.conclusion_keywords or any(
            not group or any(not word.strip() for word in group)
            for group in self.conclusion_keywords
        ):
            raise ValueError(  # noqa: TRY003
                f"推论 {self.id}：conclusion_keywords 必须包含非空关键词组"
            )
        if not self.unlock_flags and not self.grant_clues:
            raise ValueError(  # noqa: TRY003
                f"推论 {self.id}：至少需要一种可观察的推进效果"
            )
        if self.failure_time_cost < 0:
            raise ValueError(  # noqa: TRY003
                f"推论 {self.id}：failure_time_cost 不能为负数"
            )
        return self


class Ending(BaseModel):
    """结局：condition 满足时由引擎安全网或 end_session 触发。"""

    id: str
    condition: str
    text: str
    # good | bad | neutral
    outcome: str = "neutral"
    # 展示名（进 KP 开局概览与 query_story）；空时 display_name 回退 id
    name: str = ""
    # KP 向来龙去脉说明（导演指引）：仅 query_story 回执，绝不播报给玩家
    summary: str = ""

    @property
    def display_name(self) -> str:
        """展示名：未写 name 时回退 id。"""
        return self.name or self.id

    @model_validator(mode="after")
    def _check_outcome(self) -> "Ending":
        if self.outcome not in _VALID_OUTCOMES:
            msg = (
                f"结局 {self.id}：outcome 只能是 "
                f"{'/'.join(_VALID_OUTCOMES)}，收到 {self.outcome!r}"
            )
            raise ValueError(msg)
        return self


class PlotEvent(BaseModel):
    """具名剧情事件：condition 满足时由引擎记入 game.occurred_events。

    事件纯属 KP 上下文（概览列名、[已发生事件] 提示、query_story
    查说明），不触发任何机制。condition 复用 evaluate_condition 现有
    语法（无新词条，镜像契约四处不动）；空条件 = 序幕事件，开局
    首轮扫描即记。与结局不同，恒真条件无害（只是名单多一行），
    故加载期不做 is_trivially_true 拒绝。
    """

    id: str
    name: str
    # query_story 返回的 KP 向说明（导演指引，绝不播报给玩家）
    summary: str = ""
    condition: str = ""


class ModuleDef(BaseModel):
    """完整剧本模组定义。"""

    id: str
    name: str
    description: str = ""
    min_players: int = 1
    max_players: int = 6
    difficulty: str = "入门"
    opening: str
    start_scene: str
    scenes: list[Scene]
    npcs: list[NPC] = []
    monsters: list[Monster] = []
    clues: list[Clue] = []
    deductions: list[Deduction] = []
    endings: list[Ending] = []
    # 具名剧情事件（纯 KP 上下文，见 PlotEvent）
    events: list[PlotEvent] = []
    # 游戏内时钟（起始时刻与各行动耗时覆写）
    time: TimeConfig = TimeConfig()
    # 是否启用系统级通用结局（谋杀 / 纵火等极端行为的兜底结局）
    generic_endings: bool = True

    @model_validator(mode="after")
    def _check_refs(self) -> "ModuleDef":  # noqa: C901,PLR0912,PLR0915
        """交叉引用校验：任何悬空引用都让模组在加载时被跳过。"""
        if self.min_players > self.max_players:
            msg = (
                f"min_players({self.min_players}) 不能大于 "
                f"max_players({self.max_players})"
            )
            raise ValueError(msg)
        scene_ids = [s.id for s in self.scenes]
        npc_ids = [n.id for n in self.npcs]
        monster_ids = [m.id for m in self.monsters]
        clue_ids = [c.id for c in self.clues]
        event_ids = [e.id for e in self.events]
        deduction_ids = [d.id for d in self.deductions]
        for label, ids in (
            ("场景", scene_ids),
            ("NPC", npc_ids),
            ("怪物", monster_ids),
            ("线索", clue_ids),
            ("事件", event_ids),
            ("推论", deduction_ids),
        ):
            dup = {i for i in ids if ids.count(i) > 1}
            if dup:
                msg = f"{label} id 重复：{'、'.join(sorted(dup))}"
                raise ValueError(msg)
        if self.start_scene not in scene_ids:
            msg = f"start_scene {self.start_scene!r} 不在场景列表中"
            raise ValueError(msg)
        scenes = set(scene_ids)
        npcs = set(npc_ids)
        monsters = set(monster_ids)
        clues = set(clue_ids)
        deductions = set(deduction_ids)
        for deduction in self.deductions:
            missing_required = sorted(set(deduction.required_clues) - clues)
            missing_grants = sorted(set(deduction.grant_clues) - clues)
            if missing_required or missing_grants:
                missing = missing_required + missing_grants
                raise ValueError(  # noqa: TRY003
                    f"推论 {deduction.id} 引用了未定义的线索 {'、'.join(missing)}"
                )
        public_text = " ".join(
            [
                self.name,
                self.description,
                self.opening,
                *(
                    part
                    for scene in self.scenes
                    for part in (
                        scene.name,
                        scene.id,
                        scene.narration,
                        scene.idle_narration or "",
                        *(exit_.narration for exit_ in scene.exits),
                        *(
                            text
                            for check in scene.checks
                            for text in (check.success_text, check.failure_text)
                        ),
                    )
                ),
            ]
        )
        check_ids: list[str] = []
        for scene in self.scenes:
            for npc_id in scene.npcs:
                if npc_id not in npcs:
                    msg = f"场景 {scene.id} 引用了未定义的 NPC {npc_id!r}"
                    raise ValueError(msg)
            for mid in scene.monsters:
                if mid not in monsters:
                    msg = f"场景 {scene.id} 引用了未定义的怪物 {mid!r}"
                    raise ValueError(msg)
            for cp in scene.checks:
                check_ids.append(cp.id)
                if cp.skill not in _VALID_CHECK_SKILLS:
                    msg = (
                        f"场景 {scene.id} 检定点 {cp.id} 使用了未知技能 "
                        f"{cp.skill!r}（应为 san 或技能表 key）"
                    )
                    raise ValueError(msg)
                if cp.clue is not None and cp.clue not in clues:
                    msg = (
                        f"场景 {scene.id} 检定点 {cp.id} 奖励了未定义的线索 {cp.clue!r}"
                    )
                    raise ValueError(msg)
            for ex in scene.exits:
                if ex.to_scene not in scenes:
                    msg = f"场景 {scene.id} 出口指向未定义的场景 {ex.to_scene!r}"
                    raise ValueError(msg)
                err = _validate_condition(
                    ex.condition, scenes, monsters, clues, deductions
                )
                if err is not None:
                    msg = f"场景 {scene.id} 出口条件非法：{err}"
                    raise ValueError(msg)
        dup_checks = {i for i in check_ids if check_ids.count(i) > 1}
        if dup_checks:
            # fired_checks 是全局集合：id 撞车会让后一个场景的 once
            # 检定点被前一个场景的触发永久禁用
            msg = f"检定点 id 重复：{'、'.join(sorted(dup_checks))}"
            raise ValueError(msg)
        for monster in self.monsters:
            if monster.on_death_clue and monster.on_death_clue not in clues:
                msg = f"怪物 {monster.id} 死亡奖励了未定义的线索"
                raise ValueError(msg)
        for npc in self.npcs:
            if npc.on_death_clue and npc.on_death_clue not in clues:
                msg = f"NPC {npc.id} 死亡奖励了未定义的线索"
                raise ValueError(msg)
            for secret in npc.secrets:
                if secret and secret in public_text:
                    raise ValueError(  # noqa: TRY003
                        f"NPC {npc.id}：secret 出现在场景公开文案中"
                    )
            for fact in npc.facts:
                if fact.name in public_text or fact.text in public_text:
                    raise ValueError(  # noqa: TRY003
                        f"NPC {npc.id}：私人情报 {fact.id} 出现在场景公开文案中"
                    )
            fact_ids = {fact.id for fact in npc.facts}
            for node in npc.social_nodes:
                missing_facts = sorted(
                    (set(node.requires_facts) | set(node.unlock_facts)) - fact_ids
                )
                if missing_facts:
                    msg = (
                        f"NPC {npc.id} 社交节点 {node.id} 引用了未定义的情报 "
                        f"{'、'.join(missing_facts)}"
                    )
                    raise ValueError(msg)
                missing_clues = sorted(
                    (set(node.private_clues) | set(node.public_clues)) - clues
                )
                if missing_clues:
                    msg = (
                        f"NPC {npc.id} 社交节点 {node.id} 引用了未定义的线索 "
                        f"{'、'.join(missing_clues)}"
                    )
                    raise ValueError(msg)
            for entry in npc.schedule:
                if not entry.away and entry.scene not in scenes:
                    msg = f"NPC {npc.id} 行程引用了未定义的场景 {entry.scene!r}"
                    raise ValueError(msg)
                err = _validate_condition(
                    entry.condition, scenes, monsters, clues, deductions
                )
                if err is not None:
                    msg = f"NPC {npc.id} 行程条件非法：{err}"
                    raise ValueError(msg)
        for ending in self.endings:
            err = _validate_condition(
                ending.condition, scenes, monsters, clues, deductions
            )
            if err is not None:
                msg = f"结局 {ending.id} 条件非法：{err}"
                raise ValueError(msg)
            if is_trivially_true(ending.condition):
                msg = (
                    f"结局 {ending.id} 条件恒真（空或仅 always）："
                    "开局即会触发结局安全网、瞬间终局，加载期拒绝"
                )
                raise ValueError(msg)
        # 事件条件校验仿结局，但不拒恒真（序幕事件恒真无害，见 PlotEvent）
        for event in self.events:
            err = _validate_condition(
                event.condition, scenes, monsters, clues, deductions
            )
            if err is not None:
                msg = f"事件 {event.id} 条件非法：{err}"
                raise ValueError(msg)
        return self

    # ── 便捷查询（引擎与提示词构造使用）──────────────────

    def scene(self, scene_id: str) -> Optional[Scene]:
        """按 id 查场景。"""
        return next((s for s in self.scenes if s.id == scene_id), None)

    def npc(self, npc_id: str) -> Optional[NPC]:
        """按 id 查 NPC。"""
        return next((n for n in self.npcs if n.id == npc_id), None)

    def monster(self, monster_id: str) -> Optional[Monster]:
        """按 id 查怪物。"""
        return next((m for m in self.monsters if m.id == monster_id), None)

    def clue(self, clue_id: str) -> Optional[Clue]:
        """按 id 查线索。"""
        return next((c for c in self.clues if c.id == clue_id), None)

    def deduction(self, deduction_id: str) -> Optional[Deduction]:
        return next((d for d in self.deductions if d.id == deduction_id), None)

    # ── NPC 在场解析（时间 + 事件条件；死亡过滤在引擎层）────

    def npc_schedule_match(
        self,
        npc_id: str,
        time_of_day: int,
        ctx: ConditionContext,
    ) -> Optional[ScheduleEntry]:
        """取 NPC 命中的首条行程条目（条件成立 + 时钟落在窗口）。

        无行程或全不匹配返回 None。away 条目也会命中（由调用
        方决定如何使用其 activity，如离场播报的 flavor）。
        """
        npc = self.npc(npc_id)
        if npc is None:
            return None
        for entry in npc.schedule:
            if not evaluate_condition(entry.condition, ctx):
                continue
            frm = _parse_hhmm(entry.frm)
            to = _parse_hhmm(entry.to)
            # 加载期已校验，理论不可达；防御性跳过
            if frm is not None and to is not None and _in_window(time_of_day, frm, to):
                return entry
        return None

    def npc_presence(
        self,
        npc_id: str,
        time_of_day: int,
        ctx: ConditionContext,
    ) -> Optional[tuple[str, str]]:
        """解析 NPC 在给定时刻所在的场景与活动；不在场返回 None。

        空行程 → 静态 scene.npcs 成员关系（首个列入的场景，
        与无行程模组的旧行为一致）；非空行程 → 首条命中条目
        （away 视为不在场）；全不匹配 → 不在场。死亡过滤不
        在本层，由引擎的 Game 包装层负责。
        """
        npc = self.npc(npc_id)
        if npc is None:
            return None
        if not npc.schedule:
            for scene in self.scenes:
                if npc_id in scene.npcs:
                    return scene.id, ""
            return None
        entry = self.npc_schedule_match(npc_id, time_of_day, ctx)
        if entry is None or entry.away:
            return None
        return entry.scene or "", entry.activity

    def npcs_in_scene(
        self,
        scene_id: str,
        time_of_day: int,
        ctx: ConditionContext,
    ) -> list[tuple[NPC, str]]:
        """给定时刻在场于某场景的全部 NPC（按模组 npcs 声明序）。

        每个元素为 (NPC, activity)；activity 为空串表示无行程信息。
        """
        found: list[tuple[NPC, str]] = []
        for npc in self.npcs:
            presence = self.npc_presence(npc.id, time_of_day, ctx)
            if presence is not None and presence[0] == scene_id:
                found.append((npc, presence[1]))
        return found


# ── 条件表达式求值（确定性，AI 不参与）───────────────────


@dataclass
class ConditionContext:
    """条件求值所需的对局事实快照（由引擎组装）。"""

    clues: set[str] = field(default_factory=set)
    dead_monsters: set[str] = field(default_factory=set)
    current_scene: str = ""
    all_incapped: bool = False
    # 开局游戏内起始时刻（自 0:00 起的分钟数；时间词偏移基准）
    clock_start_minutes: int = 0
    # 自开局已流逝的游戏内分钟数（时间词求值基准，取代绝对钟面）
    elapsed_minutes: int = 0
    # 引擎记录的事件标记（名称 → 累计次数）
    flags: dict[str, int] = field(default_factory=dict)
    deductions: set[str] = field(default_factory=set)


def evaluate_condition(  # noqa: C901,PLR0911,PLR0912,PLR0915
    condition: Optional[str],
    ctx: ConditionContext,
) -> bool:
    """求值条件表达式；空条件视为 always。

    语法（可用 " & " 组合，须全部满足）：
    always / clue:<id> / clues:<a>+<b> / monster_dead:<id> /
    scene:<id> / all_players_incapped /
    time_after:HH:MM / time_before:HH:MM /
    time_between:HH:MM-HH:MM（含跨午夜）/
    flag:<name> / flag:<name>>=N。
    时间词按**剧本时间轴**求值：目标时刻折算为自开局流逝的分钟
    偏移 offset = (目标 - 开局时刻) mod 1440，与 elapsed 比较。
    21:00 开局的模组里 time_after:06:00 只在时钟推进到次日 06:00
    后成立——而非开局钟面读数已超过 06:00 时立刻成立。
    未知词条、空词条（悬挂 &）与非法格式保守判为不满足，
    避免作者笔误导致剧情失控。
    """
    if not condition or not condition.strip():
        return True
    for part in condition.split("&"):
        term = part.strip()
        if term == "always":
            continue
        if not term:
            return False  # 悬挂 & 产生的空词条保守判 False
        if term == "all_players_incapped":
            if not ctx.all_incapped:
                return False
            continue
        kind, _, value = term.partition(":")
        if kind == "clue" and value:
            if value not in ctx.clues:
                return False
        elif kind == "clues" and value:
            parts = [v for v in value.split("+") if v]
            # 要求至少一个非空词条：否则 "clues:+" 会因 all([]) 恒真
            if not parts or not all(v in ctx.clues for v in parts):
                return False
        elif kind == "monster_dead" and value:
            if value not in ctx.dead_monsters:
                return False
        elif kind == "deduction" and value:
            if value not in ctx.deductions:
                return False
        elif kind == "deductions" and value:
            parts = [v for v in value.split("+") if v]
            if not parts or not all(v in ctx.deductions for v in parts):
                return False
        elif kind == "scene" and value:
            if ctx.current_scene != value:
                return False
        elif kind == "time_after" and value:
            t = _parse_hhmm(value)
            if t is None:
                return False
            offset = (t - ctx.clock_start_minutes) % 1440
            if ctx.elapsed_minutes < offset:
                return False
        elif kind == "time_before" and value:
            t = _parse_hhmm(value)
            if t is None:
                return False
            offset = (t - ctx.clock_start_minutes) % 1440
            if ctx.elapsed_minutes >= offset:
                return False
        elif kind == "time_between" and value:
            left, tsep, right = value.partition("-")
            frm = _parse_hhmm(left.strip()) if tsep else None
            to = _parse_hhmm(right.strip()) if tsep else None
            if frm is None or to is None:
                return False
            frm_off = (frm - ctx.clock_start_minutes) % 1440
            to_off = (to - ctx.clock_start_minutes) % 1440
            # 偏移窗口按 elapsed 的日内位置匹配，保留每日重复语义
            if not _in_window(ctx.elapsed_minutes % 1440, frm_off, to_off):
                return False
        elif kind == "flag" and value:
            name, ge, num = value.partition(">=")
            count = ctx.flags.get(name, 0)
            if ge:
                try:
                    need = int(num)
                except ValueError:
                    return False
                if count < need:
                    return False
            elif count < 1:
                return False
        else:
            return False
    return True


def is_trivially_true(condition: Optional[str]) -> bool:
    """条件是否不依赖任何对局状态恒真（空 / 词条全为 always 或空）。

    供加载期拦截恒真的结局条件——这类结局会在开局第一次结局
    安全网扫描时立即触发，让游戏瞬间结束（出口 / 行程的空条件
    恒真是预期语义，不受此函数影响）。
    """
    if not condition or not condition.strip():
        return True
    return all(part.strip() in ("", "always") for part in condition.split("&"))


# ── 加载器 ────────────────────────────────────────────────

_MODULES_DIR = Path(__file__).parent / "modules"

_registry: dict[str, ModuleDef] = {}


def load_modules(directory: Optional[Path] = None) -> dict[str, ModuleDef]:
    """扫描目录下 *.yaml 并校验加载；坏模组 warning 跳过。"""
    found: dict[str, ModuleDef] = {}
    target = directory or _MODULES_DIR
    if not target.is_dir():
        logger.warning(f"跑团模组目录不存在：{target}")
        return found
    for path in sorted(target.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            module = ModuleDef.model_validate(raw)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"跑团模组 {path.name} 加载失败，已跳过：{e!r}")
            continue
        if module.id in found:
            logger.warning(f"跑团模组 id 重复：{module.id}（{path.name}），后者跳过")
            continue
        found[module.id] = module
        logger.info(
            f"跑团模组已加载：{module.name}（{module.id}，"
            f"{len(module.scenes)} 场景，{module.min_players}-{module.max_players} 人）"
        )
    _registry.clear()
    _registry.update(found)
    return found


def list_modules() -> list[ModuleDef]:
    """已加载模组列表（加载顺序）。"""
    return list(_registry.values())


def get_module(module_id: str) -> Optional[ModuleDef]:
    """按 id 查已加载模组。"""
    return _registry.get(module_id)
