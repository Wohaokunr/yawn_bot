"""狼人杀房规：报名阶段由房主调整的可选规则。

房规只在报名阶段可改（命令层写入，与 signup_names 同属报名阶段
内存写入例外），发牌后锁定，引擎只读。一群的最近一次房规会被
记住，下次开房自动沿用（见 state.remember_room_prefs）。
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Literal, Optional

WinMode = Literal["edge", "city"]
NightLastWords = Literal["first", "all", "none"]
TieMode = Literal["pk", "peace"]


@dataclass(frozen=True)
class HouseRules:
    """单局房规（不可变；修改时整体替换）。"""

    # 是否进行警长竞选
    sheriff: bool = True
    # 女巫首夜可以自救（其余夜晚始终不可自救）
    witch_self_save_first_night: bool = False
    # 女巫同一夜可以同时使用解药和毒药
    witch_double_potion: bool = False
    # 同一人同夜被守护又被解药救 → 死亡（同守同救）
    guard_save_conflict: bool = True
    # 出局时公布身份（关闭后只在终局统一公布；猎人/骑士等主动亮牌照常）
    reveal_on_death: bool = True
    # 胜负判定：edge=屠边（神职或平民全灭），city=屠城（好人全灭）
    win_mode: WinMode = "edge"
    # 夜间死者遗言：first=仅首夜，all=每夜，none=无
    night_last_words: NightLastWords = "first"
    # 投票窗口内允许改票（以最后一票为准）
    allow_change_vote: bool = False
    # 平票处理：pk=PK 发言后重投，peace=直接平安日
    tie_mode: TieMode = "pk"
    # 每人发言时长覆盖（秒，None=沿用全局配置）
    speech_seconds: Optional[int] = None
    # 投票时长覆盖（秒，None=沿用全局配置）
    vote_seconds: Optional[int] = None
    # 投票时公开每一票的去向（关闭则只在计票汇总里公布）
    open_ballot: bool = True


DEFAULT_RULES = HouseRules()

# 预设：/房规 新手|标准|竞技|快速
PRESETS: dict[str, HouseRules] = {
    "标准": DEFAULT_RULES,
    "新手": HouseRules(
        witch_self_save_first_night=True,
        night_last_words="all",
        allow_change_vote=True,
        guard_save_conflict=False,
    ),
    "竞技": HouseRules(
        reveal_on_death=False,
        night_last_words="first",
        open_ballot=False,
    ),
    "快速": HouseRules(
        sheriff=False,
        night_last_words="none",
        tie_mode="peace",
        speech_seconds=60,
        vote_seconds=45,
    ),
}

_BOOL_TRUE = {"开", "开启", "是", "on", "true", "1", "允许", "要"}
_BOOL_FALSE = {"关", "关闭", "否", "off", "false", "0", "禁止", "不要"}

# 中文键名 → (字段, 说明)
RULE_KEYS: dict[str, tuple[str, str]] = {
    "警长": ("sheriff", "是否竞选警长（开/关）"),
    "首夜自救": ("witch_self_save_first_night", "女巫首夜可自救（开/关）"),
    "双药": ("witch_double_potion", "女巫同夜可双药（开/关）"),
    "同守同救": ("guard_save_conflict", "同守同救则死（开/关）"),
    "翻牌": ("reveal_on_death", "出局公布身份（开/关）"),
    "胜负": ("win_mode", "屠边 / 屠城"),
    "遗言": ("night_last_words", "夜间死者遗言：首夜 / 每夜 / 无"),
    "改票": ("allow_change_vote", "投票窗口内可改票（开/关）"),
    "平票": ("tie_mode", "PK / 平安日"),
    "发言时长": ("speech_seconds", "每人发言秒数（30-300，默认=全局配置）"),
    "投票时长": ("vote_seconds", "投票秒数（20-180，默认=全局配置）"),
    "明票": ("open_ballot", "投票时实时公布去向（开/关）"),
}

_ENUM_INPUT: dict[str, dict[str, str]] = {
    "win_mode": {"屠边": "edge", "屠城": "city", "edge": "edge", "city": "city"},
    "night_last_words": {
        "首夜": "first",
        "每夜": "all",
        "全程": "all",
        "无": "none",
        "没有": "none",
        "first": "first",
        "all": "all",
        "none": "none",
    },
    "tie_mode": {
        "pk": "pk",
        "PK": "pk",
        "平安日": "peace",
        "peace": "peace",
        "不pk": "peace",
    },
}

_INT_RANGES: dict[str, tuple[int, int]] = {
    "speech_seconds": (30, 300),
    "vote_seconds": (20, 180),
}


def set_rule(  # noqa: C901,PLR0911,PLR0912
    rules: HouseRules, key: str, value: str
) -> tuple[HouseRules, str]:
    """按中文键修改一项房规；返回 (新房规, 结果说明)。

    失败时返回原房规与以「×」开头的错误说明。
    """
    entry = RULE_KEYS.get(key.strip())
    if entry is None:
        return rules, f"× 没有「{key}」这项房规。可调：{'、'.join(RULE_KEYS)}"
    field_name, desc = entry
    raw = value.strip()
    current = getattr(rules, field_name)
    if field_name in _ENUM_INPUT:
        mapped = _ENUM_INPUT[field_name].get(raw)
        if mapped is None:
            return rules, f"× 「{key}」可选：{desc}"
        new_value: object = mapped
    elif field_name in _INT_RANGES:
        low, high = _INT_RANGES[field_name]
        if raw in {"默认", "default", "重置"}:
            new_value = None
        elif raw.rstrip("秒").isdigit():
            seconds = int(raw.rstrip("秒"))
            if not low <= seconds <= high:
                return rules, f"× 「{key}」需在 {low}-{high} 秒之间"
            new_value = seconds
        else:
            return rules, f"× 「{key}」格式：数字秒数或“默认”"
    elif isinstance(current, bool):
        if raw.lower() in _BOOL_TRUE:
            new_value = True
        elif raw.lower() in _BOOL_FALSE:
            new_value = False
        else:
            return rules, f"× 「{key}」请填 开 或 关"
    else:  # pragma: no cover - 字段类型均已覆盖
        return rules, "× 不支持的房规类型"
    updated = replace(rules, **{field_name: new_value})
    return updated, f"已设置 {key}：{_render_value(field_name, new_value)}"


def _render_value(field_name: str, value: object) -> str:  # noqa: PLR0911
    """单项房规的中文值。"""
    if value is None:
        return "默认"
    if isinstance(value, bool):
        return "开" if value else "关"
    if field_name == "win_mode":
        return "屠城" if value == "city" else "屠边"
    if field_name == "night_last_words":
        return {"first": "首夜", "all": "每夜", "none": "无"}[str(value)]
    if field_name == "tie_mode":
        return "PK" if value == "pk" else "平安日"
    if field_name in _INT_RANGES:
        return f"{value} 秒"
    return str(value)


def describe(rules: HouseRules) -> list[str]:
    """全部房规的一行一项展示。"""
    lines: list[str] = []
    for key, (field_name, _) in RULE_KEYS.items():
        lines.append(f"{key}：{_render_value(field_name, getattr(rules, field_name))}")
    return lines


def diff_from_default(rules: HouseRules) -> list[str]:
    """与标准房规不同的项（身份卡与开局播报只展示差异）。"""
    lines: list[str] = []
    for key, (field_name, _) in RULE_KEYS.items():
        value = getattr(rules, field_name)
        if value != getattr(DEFAULT_RULES, field_name):
            lines.append(f"{key}：{_render_value(field_name, value)}")
    return lines


def preset_name(rules: HouseRules) -> Optional[str]:
    """与某个预设完全一致时返回预设名。"""
    for name, preset in PRESETS.items():
        if preset == rules:
            return name
    return None


def as_dict(rules: HouseRules) -> dict[str, object]:
    """WebUI / 日志用的字典形式。"""
    return {f.name: getattr(rules, f.name) for f in fields(rules)}
