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
from pydantic import BaseModel, model_validator

# 骰表达式：NdM(±K) 或纯整数，如 1d6 / 1d6+1 / 3
_DICE_EXPR_RE = re.compile(r"\d+d\d+([+-]\d+)?|\d+")


class CheckDifficulty(str, Enum):
    """检定难度（CoC 7版）：常规 / 困难(½) / 极难(⅕)。"""

    REGULAR = "regular"
    HARD = "hard"
    EXTREME = "extreme"


class CheckPoint(BaseModel):
    """场景检定点：玩家发言关键词命中或 KP 工具调用时触发。"""

    id: str
    # 技能 key（charsheet.SKILLS 的键）；"san" 表示理智检定
    skill: str
    difficulty: CheckDifficulty = CheckDifficulty.REGULAR
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

    @model_validator(mode="after")
    def _check_fields(self) -> "CheckPoint":
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
    """是否为合法骰表达式（NdM±K 或整数）。"""
    return _DICE_EXPR_RE.fullmatch(text.strip()) is not None


def _is_san_loss(text: str) -> bool:
    """是否为合法 SAN 损失表达式 "成功侧/失败侧"。"""
    left, sep, right = text.partition("/")
    if not sep:
        return False
    return all(p.strip() and _is_dice_expr(p.strip()) for p in (left, right))


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


class NPC(BaseModel):
    """NPC：secrets 永不注入任何提示词。"""

    id: str
    name: str
    # 给 KP 看的公开简介（安全信息）
    public_desc: str
    # NPC 对话人格（speak_as_npc 的扮演依据）
    persona: str
    # 可透露给玩家的信息块（进 NPC 相关提示）
    knows: list[str] = []
    # 绝密信息：仅用于作者自述，加载校验确保其不泄露进提示词
    secrets: list[str] = []
    # LLM 失败时的罐头回复
    fallback_line: str = ""

    @model_validator(mode="after")
    def _no_secret_leak(self) -> "NPC":
        haystack = " ".join((self.persona, self.public_desc, *self.knows))
        for secret in self.secrets:
            if secret and secret in haystack:
                msg = f"NPC {self.id}：secret 出现在 persona/公开信息中，会泄露给 AI"
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


class Clue(BaseModel):
    """线索：name 进 KP 提示词，text 仅发现时由引擎播报。"""

    id: str
    name: str
    text: str


class Ending(BaseModel):
    """结局：condition 满足时由引擎安全网或 end_session 触发。"""

    id: str
    condition: str
    text: str
    # good | bad | neutral
    outcome: str = "neutral"


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
    endings: list[Ending] = []

    @model_validator(mode="after")
    def _check_refs(self) -> "ModuleDef":  # noqa: C901,PLR0912
        """交叉引用校验：任何悬空引用都让模组在加载时被跳过。"""
        scene_ids = [s.id for s in self.scenes]
        npc_ids = [n.id for n in self.npcs]
        monster_ids = [m.id for m in self.monsters]
        clue_ids = [c.id for c in self.clues]
        for label, ids in (
            ("场景", scene_ids),
            ("NPC", npc_ids),
            ("怪物", monster_ids),
            ("线索", clue_ids),
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
                if cp.clue is not None and cp.clue not in clues:
                    msg = (
                        f"场景 {scene.id} 检定点 {cp.id} 奖励了未定义的线索 {cp.clue!r}"
                    )
                    raise ValueError(msg)
            for ex in scene.exits:
                if ex.to_scene not in scenes:
                    msg = f"场景 {scene.id} 出口指向未定义的场景 {ex.to_scene!r}"
                    raise ValueError(msg)
        for monster in self.monsters:
            if monster.on_death_clue and monster.on_death_clue not in clues:
                msg = f"怪物 {monster.id} 死亡奖励了未定义的线索"
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


# ── 条件表达式求值（确定性，AI 不参与）───────────────────


@dataclass
class ConditionContext:
    """条件求值所需的对局事实快照（由引擎组装）。"""

    clues: set[str] = field(default_factory=set)
    dead_monsters: set[str] = field(default_factory=set)
    current_scene: str = ""
    all_incapped: bool = False


def evaluate_condition(  # noqa: C901,PLR0911,PLR0912
    condition: Optional[str],
    ctx: ConditionContext,
) -> bool:
    """求值条件表达式；空条件视为 always。

    语法（可用 " & " 组合，须全部满足）：
    always / clue:<id> / clues:<a>+<b> / monster_dead:<id> /
    scene:<id> / all_players_incapped。
    未知词条保守判为不满足，避免作者笔误导致剧情失控。
    """
    if not condition or not condition.strip():
        return True
    for part in condition.split("&"):
        term = part.strip()
        if term in ("", "always"):
            continue
        if term == "all_players_incapped":
            if not ctx.all_incapped:
                return False
            continue
        kind, _, value = term.partition(":")
        if kind == "clue" and value:
            if value not in ctx.clues:
                return False
        elif kind == "clues" and value:
            if not all(v in ctx.clues for v in value.split("+") if v):
                return False
        elif kind == "monster_dead" and value:
            if value not in ctx.dead_monsters:
                return False
        elif kind == "scene" and value:
            if ctx.current_scene != value:
                return False
        else:
            return False
    return True


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
