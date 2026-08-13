"""Stable human-readable and JSON output for fixed-seed playtests."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .simulator import SearchResult


_ACTION_LABELS = {
    "auto_move": "自动移动",
    "move": "移动",
    "check": "检定",
    "social": "社交",
    "wait": "等待",
    "attack": "攻击",
    "monster_attack": "反击",
    "pass": "跳过回合",
}
_TIER_LABELS = {
    "critical": "大成功",
    "extreme": "极难成功",
    "hard": "困难成功",
    "regular": "成功",
    "failure": "失败",
    "fumble": "大失败",
}


def _step_header(step: dict[str, Any]) -> str:
    action = _ACTION_LABELS.get(str(step["action"]), str(step["action"]))
    actor = str(step["actor"]) if step.get("actor") else "系统"
    target = f" → {step['target']}" if step.get("target") else ""
    return f"{int(step['index']):02d}  {action} · {actor}{target}"


def _step_location(step: dict[str, Any]) -> str:
    before = str(step["scene_before"])
    after = str(step["scene_after"])
    location = f"{before} → {after}" if before != after else f"{before}（未移动）"
    elapsed_before = int(step["elapsed_before"])
    elapsed_after = int(step["elapsed_after"])
    duration = elapsed_after - elapsed_before
    return (
        f"    场景  {location}    时间  "
        f"{elapsed_before}m → {elapsed_after}m（+{duration}m）"
    )


def _render_roll(roll: dict[str, Any]) -> str:
    kind = str(roll.get("kind", ""))
    if kind == "d100":
        player = str(roll.get("player", ""))
        skill = str(roll.get("skill", ""))
        roll_value = f"{roll.get('roll', '')}/{roll.get('value', '')}"
        tier = _TIER_LABELS.get(str(roll.get("tier", "")), str(roll.get("tier", "")))
        label = "闪避" if skill == "dodge" else "检定"
        return f"    {label}  {player} · {skill} · {roll_value} · {tier}"
    if kind == "damage":
        return (
            f"    伤害  {roll.get('expression', '')} → {roll.get('result', '')}"
        )
    expression = roll.get("expression", "")
    result = roll.get("result", "")
    suffix = f" {expression}" if expression else ""
    return f"    骰点  {kind}{suffix} → {result}"


def _render_changes(step: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if step.get("clues_added"):
        clues = "、".join(str(clue) for clue in step["clues_added"])
        lines.append(f"    线索  +{clues}")
    if step.get("flags_changed"):
        flags = "、".join(
            f"{key}={value}" for key, value in step["flags_changed"].items()
        )
        lines.append(f"    flag  {flags}")
    return lines


def _render_list(label: str, values: Any) -> str:
    if not values:
        return f"  {label}  （无）"
    if isinstance(values, dict):
        rendered = "、".join(
            f"{key}={value}" for key, value in sorted(values.items())
        )
    else:
        rendered = "、".join(str(value) for value in values)
    return f"  {label}  {rendered}"


def render_result_text(result: "SearchResult") -> str:
    """Render a stable, scannable human-readable trace for CLI and TUI."""
    if not result.ok:
        return (
            f"试玩失败 [{result.reason}]：{result.message}\n"
            f"搜索状态  已探索 {result.explored_states} · "
            f"已生成 {result.generated_states}"
        )
    lines = [
        f"轨迹 · {len(result.steps)} 步",
        f"模组  『{result.module_name}』  {result.module_id}",
        f"参数  seed={result.seed}    目标结局={result.target_ending}",
        f"队伍  {'、'.join(player['name'] for player in result.players)}",
        "─" * 48,
        "",
    ]
    for step in result.steps:
        lines.append(_step_header(step))
        lines.append(_step_location(step))
        lines.extend(_render_roll(roll) for roll in step["rolls"])
        lines.extend(_render_changes(step))
        lines.append("")
    ending = result.final_ending or {}
    lines.extend(
        [
            "结局",
            f"  {ending.get('name', '')}（{ending.get('id', '')}）",
            f"  最终位置  {result.final_scene or '（未知）'} · "
            f"{result.elapsed_minutes}m",
            _render_list("线索", result.clues),
            _render_list("flag", result.flags),
            _render_list("事件", result.events),
            f"  搜索状态  已探索 {result.explored_states} · "
            f"已生成 {result.generated_states}",
        ]
    )
    return "\n".join(lines)


def render_result_json(
    result: "SearchResult", *, indent: Optional[int] = None
) -> str:
    """Render stable JSON for clipboard, files, and CLI consumers."""
    return json.dumps(
        result.to_dict(), ensure_ascii=False, sort_keys=True, indent=indent
    )
