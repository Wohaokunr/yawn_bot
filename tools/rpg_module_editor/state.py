"""编辑器内存状态：以 dict 为唯一持久表示的草稿容器与实体操作。

键名一律用 YAML 侧形态（行程条目写作 ``from``）。pydantic 模型只在
validate/lint 层做只读校验，绝不回写——保证未知键在往返中存活。
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from .yaml_io import (
    dump_module_text,
    load_module_file,
    normalize_data,
    parse_yaml_text,
)

if TYPE_CHECKING:
    from pathlib import Path

# ── 新建模组骨架（来自 modules/README.md 的最小模板，已过加载校验）──

_SKELETON_YAML = """
id: my_module
name: 示例模组
description: 一份最小模组骨架
min_players: 1
max_players: 6
difficulty: 入门
time:
  start: "21:00"
generic_endings: true

opening: |
  开篇播报文案……

start_scene: entrance

scenes:
  - id: entrance
    name: 入口
    narration: |
      场景播报文案……
    idle_narration: 四周一片沉寂。
    checks:
      - id: entrance_search
        skill: spot_hidden
        once: true
        triggers: [搜索, 查看, 翻找]
        success_text: 你在门垫下摸到一把生锈的钥匙。
        failure_text: 门垫下只有积年的灰尘。
        clue: rusty_key
    exits:
      - to_scene: inner_room
        keywords: [内室, 里屋]
        condition: clue:rusty_key
        narration: 锈钥匙在锁孔里艰涩地转了一圈，门开了。

  - id: inner_room
    name: 内室
    narration: |
      内室昏暗……
    npcs: [guard]

npcs:
  - id: guard
    name: 守卫
    public_desc: 一个睡眼惺忪的守卫。
    persona: |
      你是这座宅子的守卫，胆小怕事……回复简短。
    knows: [这宅子很久没人来了]
    secrets: [你偷偷在墙缝里藏了违禁品]
    fallback_line: 嗯？什么？我什么都不知道。
    schedule:
      - from: "21:00"
        to: "23:00"
        scene: inner_room
        activity: 来回踱步
      - from: "23:00"
        to: "21:00"
        scene: inner_room
        activity: 靠着墙打盹

clues:
  - id: rusty_key
    name: 生锈钥匙
    text: 一把锈迹斑斑的钥匙，像是开内室门的。

endings:
  - id: truth_revealed
    condition: clue:rusty_key
    name: 真相大白
    outcome: good
    summary: 导演指引：调查员取得钥匙后达成……
    text: |
      ═══ 结局 · 真相大白 ═══
      结局播报文案……

events:
  - id: prologue
    name: 序幕
    summary: 导演指引：开局铺垫……
"""


def blank_module_dict() -> dict[str, Any]:
    """新建模组的起点（README 最小骨架的深拷贝）。"""
    return normalize_data(parse_yaml_text(_SKELETON_YAML))


@dataclass
class ModuleDraft:
    """正在编辑的模组草稿。"""

    data: dict[str, Any] = field(default_factory=blank_module_dict)
    path: Optional[Path] = None
    header: str = ""
    baseline: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not self.baseline:
            self.baseline = self.serialize()

    def serialize(self) -> str:
        return dump_module_text(self.data, self.header)

    @property
    def dirty(self) -> bool:
        return self.serialize() != self.baseline

    @property
    def module_id(self) -> str:
        value = self.data.get("id", "")
        return value if isinstance(value, str) else ""

    @property
    def module_name(self) -> str:
        value = self.data.get("name", "")
        return value if isinstance(value, str) else ""

    def display_title(self) -> str:
        label = self.module_name or self.module_id or "未命名模组"
        filename = self.path.name if self.path else "（未保存）"
        mark = " ●" if self.dirty else ""
        return f"「{label}」{filename}{mark}"

    def mark_saved(self, path: Path) -> None:
        self.path = path
        self.baseline = self.serialize()

    def replace_data(self, data: dict[str, Any]) -> None:
        """整体替换数据（YAML 源码页应用 / 打开文件）。"""
        self.data = data

    def revert_to_saved(self) -> bool:
        """还原到磁盘版本；无可还原文件返回 False。"""
        if self.path is None:
            return False
        data, header = load_module_file(self.path)
        self.data = data
        self.header = header
        self.baseline = self.serialize()
        return True


# ── 规范实体构造器（键序对齐 README 字段表）─────────────


def new_scene_dict(scene_id: str) -> dict[str, Any]:
    return {"id": scene_id, "name": "", "narration": ""}


def new_check_dict(check_id: str) -> dict[str, Any]:
    return {
        "id": check_id,
        "skill": "spot_hidden",
        "once": True,
        "triggers": [],
        "success_text": "",
        "failure_text": "",
    }


def new_exit_dict(to_scene: str = "") -> dict[str, Any]:
    return {"to_scene": to_scene, "keywords": [], "narration": ""}


def new_npc_dict(npc_id: str) -> dict[str, Any]:
    return {
        "id": npc_id,
        "name": "",
        "public_desc": "",
        "persona": "",
        "knows": [],
        "secrets": [],
        "fallback_line": "",
        "schedule": [],
    }


def new_monster_dict(monster_id: str) -> dict[str, Any]:
    return {
        "id": monster_id,
        "name": "",
        "hp": 10,
        "attack_skill": 40,
        "attack_name": "攻击",
        "damage": "1d6",
    }


def new_clue_dict(clue_id: str) -> dict[str, Any]:
    return {"id": clue_id, "name": "", "text": ""}


def new_ending_dict(ending_id: str) -> dict[str, Any]:
    return {
        "id": ending_id,
        "condition": "",
        "name": "",
        "outcome": "neutral",
        "summary": "",
        "text": "",
    }


def new_event_dict(event_id: str) -> dict[str, Any]:
    return {"id": event_id, "name": "", "summary": "", "condition": ""}


def new_schedule_entry_dict(scene_id: str = "") -> dict[str, Any]:
    return {"from": "21:00", "to": "23:00", "scene": scene_id, "activity": ""}


def generate_unique_id(base: str, existing: set[str]) -> str:
    """生成形如 base / base_2 / base_3 的不重复 id。"""
    if base not in existing:
        return base
    n = 2
    while f"{base}_{n}" in existing:
        n += 1
    return f"{base}_{n}"


# ── 访问器 ────────────────────────────────────────────────

_LIST_SECTIONS = ("scenes", "npcs", "monsters", "clues", "endings", "events")


def get_list(data: dict[str, Any], key: str) -> list[Any]:
    """取模组的某个实体列表；缺失/类型不对时返回空列表。"""
    value = data.get(key)
    return value if isinstance(value, list) else []


def entity_ids(data: dict[str, Any], key: str) -> list[str]:
    """实体列表的 id 序列（跳过无 id 的畸形条目）。"""
    return [
        item["id"]
        for item in get_list(data, key)
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]


def entity_label(item: dict[str, Any]) -> str:
    """「name(id)」展示标签；缺省回退。"""
    ident = item.get("id", "?")
    name = item.get("name", "")
    return f"{name}({ident})" if name else str(ident)


def condition_fields(data: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """全部含 condition 字段的 (位置描述, 宿主 dict) 列表。"""
    found: list[tuple[str, dict[str, Any]]] = []
    for scene in get_list(data, "scenes"):
        if not isinstance(scene, dict):
            continue
        sid = scene.get("id", "?")
        for i, exit_ in enumerate(get_list(scene, "exits")):
            if isinstance(exit_, dict):
                found.append((f"场景〈{sid}〉出口 #{i + 1}", exit_))
    for npc in get_list(data, "npcs"):
        if not isinstance(npc, dict):
            continue
        nid = npc.get("id", "?")
        for i, entry in enumerate(get_list(npc, "schedule")):
            if isinstance(entry, dict):
                found.append((f"NPC〈{nid}〉行程 #{i + 1}", entry))
    for kind, label in (("endings", "结局"), ("events", "事件")):
        found.extend(
            (f"{label}〈{item.get('id', '?')}〉", item)
            for item in get_list(data, kind)
            if isinstance(item, dict)
        )
    return found


# ── 改名联动 ──────────────────────────────────────────────


def _rename_in_condition(condition: str, kind: str, old: str, new: str) -> str:
    """词条级替换条件表达式中的实体引用（避免子串误伤）。"""
    terms = []
    for raw in condition.split("&"):
        term = raw.strip()
        prefix, sep, value = term.partition(":")
        if sep:
            if (
                (kind == "scene" and prefix == "scene" and value == old)
                or (kind == "monster" and prefix == "monster_dead" and value == old)
                or (kind == "clue" and prefix == "clue" and value == old)
            ):
                term = f"{prefix}:{new}"
            elif kind == "clue" and prefix == "clues":
                parts = [new if v == old else v for v in value.split("+")]
                term = f"{prefix}:{'+'.join(parts)}"
        terms.append(term)
    return " & ".join(terms)


def rename_entity(  # noqa: C901,PLR0912,PLR0915
    data: dict[str, Any], kind: str, old: str, new: str
) -> list[str]:
    """级联改名实体 id；返回受影响位置描述列表（供确认对话框展示）。

    kind: scene / npc / monster / clue
    """
    sites: list[str] = []
    section = {
        "scene": "scenes",
        "npc": "npcs",
        "monster": "monsters",
        "clue": "clues",
    }[kind]
    for item in get_list(data, section):
        if isinstance(item, dict) and item.get("id") == old:
            item["id"] = new
            sites.append(f"{kind} 自身 id")
            break

    def maybe_rename_conditions() -> None:
        for where, host in condition_fields(data):
            cond = host.get("condition")
            if isinstance(cond, str) and old in cond:
                replaced = _rename_in_condition(cond, kind, old, new)
                if replaced != cond:
                    host["condition"] = replaced
                    sites.append(f"{where} 条件")

    if kind == "scene":
        if data.get("start_scene") == old:
            data["start_scene"] = new
            sites.append("start_scene")
        for scene in get_list(data, "scenes"):
            if not isinstance(scene, dict):
                continue
            for i, exit_ in enumerate(get_list(scene, "exits")):
                if isinstance(exit_, dict) and exit_.get("to_scene") == old:
                    exit_["to_scene"] = new
                    sites.append(f"场景〈{scene.get('id', '?')}〉出口 #{i + 1}")
        for npc in get_list(data, "npcs"):
            if not isinstance(npc, dict):
                continue
            for i, entry in enumerate(get_list(npc, "schedule")):
                if isinstance(entry, dict) and entry.get("scene") == old:
                    entry["scene"] = new
                    sites.append(f"NPC〈{npc.get('id', '?')}〉行程 #{i + 1}")
        maybe_rename_conditions()
    elif kind == "npc":
        for scene in get_list(data, "scenes"):
            if not isinstance(scene, dict):
                continue
            npcs = scene.get("npcs")
            if isinstance(npcs, list) and old in npcs:
                npcs[npcs.index(old)] = new
                sites.append(f"场景〈{scene.get('id', '?')}〉在场 NPC")
    elif kind == "monster":
        for scene in get_list(data, "scenes"):
            if not isinstance(scene, dict):
                continue
            monsters = scene.get("monsters")
            if isinstance(monsters, list) and old in monsters:
                monsters[monsters.index(old)] = new
                sites.append(f"场景〈{scene.get('id', '?')}〉在场怪物")
        maybe_rename_conditions()
    elif kind == "clue":
        for scene in get_list(data, "scenes"):
            if not isinstance(scene, dict):
                continue
            for i, check in enumerate(get_list(scene, "checks")):
                if isinstance(check, dict) and check.get("clue") == old:
                    check["clue"] = new
                    sites.append(f"场景〈{scene.get('id', '?')}〉检定点 #{i + 1}")
        for section_key in ("monsters", "npcs"):
            for item in get_list(data, section_key):
                if isinstance(item, dict) and item.get("on_death_clue") == old:
                    item["on_death_clue"] = new
                    sites.append(f"{section_key[:-1]}〈{item.get('id', '?')}〉死亡线索")
        maybe_rename_conditions()
    return sites


def deep_copy_module(data: dict[str, Any], new_id: str) -> dict[str, Any]:
    """复制现有模组作为新模组起点（换新 id，防跨文件撞车）。"""
    copied = copy.deepcopy(data)
    copied["id"] = new_id
    return copied


# 引擎发言关键词扫描 / 攻击行为写入的 flag（README「flags 由引擎写入」）
_ENGINE_FLAG_HINTS = (
    ("arson", "纵火关键词累计"),
    ("threat", "恐吓关键词累计"),
    ("destroy", "破坏关键词累计"),
    ("assault", "攻击 NPC 累计"),
    ("murder", "击杀 NPC"),
)


def clue_referrers(data: dict[str, Any], clue_id: str) -> list[str]:
    """线索引用者地图：哪些检定点/死亡奖励/条件引用了该线索。"""
    found: list[str] = []
    for scene in get_list(data, "scenes"):
        if not isinstance(scene, dict):
            continue
        where = f"场景〈{scene.get('id', '?')}〉"
        for i, check in enumerate(get_list(scene, "checks")):
            if isinstance(check, dict) and check.get("clue") == clue_id:
                found.append(
                    f"{where} 检定点 #{i + 1}〈{check.get('id', '?')}〉成功奖励"
                )
    for kind, label in (("monsters", "怪物"), ("npcs", "NPC")):
        found.extend(
            f"{label}〈{item.get('id', '?')}〉死亡奖励"
            for item in get_list(data, kind)
            if isinstance(item, dict) and item.get("on_death_clue") == clue_id
        )
    clue_term = re.compile(r"\bclues?:([a-z0-9_+]+)")
    for where, host in condition_fields(data):
        condition = host.get("condition")
        if not isinstance(condition, str):
            continue
        for match in clue_term.finditer(condition):
            if clue_id in match.group(1).split("+"):
                found.append(f"{where} 条件")
                break
    return found


def build_condition_tokens(data: dict[str, Any]) -> list[tuple[str, str]]:
    """按当前模组数据生成可插入的条件词条（label, token）。"""
    tokens: list[tuple[str, str]] = [
        ("恒真占位 always", "always"),
        ("全员倒地 all_players_incapped", "all_players_incapped"),
    ]
    tokens.extend(
        (f"线索已发现：{entity_label(clue)}", f"clue:{clue['id']}")
        for clue in get_list(data, "clues")
        if isinstance(clue, dict) and clue.get("id")
    )
    tokens.extend(
        (
            f"怪物已击杀：{entity_label(monster)}",
            f"monster_dead:{monster['id']}",
        )
        for monster in get_list(data, "monsters")
        if isinstance(monster, dict) and monster.get("id")
    )
    tokens.extend(
        (f"当前场景：{entity_label(scene)}", f"scene:{scene['id']}")
        for scene in get_list(data, "scenes")
        if isinstance(scene, dict) and scene.get("id")
    )
    for name, note in _ENGINE_FLAG_HINTS:
        tokens.append((f"引擎 flag：{name}（{note}）", f"flag:{name}"))
    tokens.extend(
        [
            ("时间晚于 06:00（示例）", "time_after:06:00"),
            ("时间早于 06:00（示例）", "time_before:06:00"),
            ("时间窗口 00:00-06:00（示例）", "time_between:00:00-06:00"),
            ("flag 计数阈值（示例）", "flag:arson>=2"),
        ]
    )
    return tokens


def iter_dict_strings(value: Any, path: str = "") -> Any:
    """遍历嵌套结构中的字符串（供 lint 使用）；yield (路径, 字符串)。"""
    if isinstance(value, dict):
        for k, v in value.items():
            yield from iter_dict_strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from iter_dict_strings(v, f"{path}[{i}]")
    elif isinstance(value, str):
        yield path, value


__all__ = [
    "ModuleDraft",
    "blank_module_dict",
    "build_condition_tokens",
    "clue_referrers",
    "condition_fields",
    "deep_copy_module",
    "entity_ids",
    "entity_label",
    "generate_unique_id",
    "get_list",
    "new_check_dict",
    "new_clue_dict",
    "new_ending_dict",
    "new_event_dict",
    "new_exit_dict",
    "new_monster_dict",
    "new_npc_dict",
    "new_scene_dict",
    "new_schedule_entry_dict",
    "rename_entity",
]
