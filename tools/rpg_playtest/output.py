"""Stable human-readable and JSON output for fixed-seed playtests."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .simulator import SearchResult


def render_result_text(result: "SearchResult") -> str:
    """Render the same compact trace used by the standalone CLI."""
    if not result.ok:
        return (
            f"试玩失败 [{result.reason}]：{result.message}\n"
            f"已探索 {result.explored_states} 个状态，"
            f"生成 {result.generated_states} 个状态。"
        )
    lines = [
        f"模组：『{result.module_name}』（{result.module_id}）",
        f"seed={result.seed}  目标结局={result.target_ending}",
        f"玩家：{'、'.join(player['name'] for player in result.players)}",
        "",
    ]
    for step in result.steps:
        actor = f" {step['actor']}" if step["actor"] else ""
        target = f" -> {step['target']}" if step["target"] else ""
        lines.append(
            f"{step['index']:02d}. {step['action']}{actor}{target} "
            f"[{step['scene_before']} -> {step['scene_after']}; "
            f"{step['elapsed_before']}m -> {step['elapsed_after']}m]"
        )
        for roll in step["rolls"]:
            if roll["kind"] == "d100":
                lines.append(
                    f"    d100 {roll['player']} {roll['skill']}: "
                    f"{roll['roll']}/{roll['value']} {roll['tier']}"
                )
            else:
                lines.append(
                    f"    {roll['kind']} {roll.get('expression', '')}: "
                    f"{roll.get('result', '')}"
                )
        if step["clues_added"]:
            lines.append("    线索 +" + "、".join(step["clues_added"]))
        if step["flags_changed"]:
            rendered = "、".join(
                f"{key}={value}" for key, value in step["flags_changed"].items()
            )
            lines.append("    flag " + rendered)
    ending = result.final_ending or {}
    lines.extend(
        [
            "",
            f"达成结局：{ending.get('name', '')}（{ending.get('id', '')}）",
            f"搜索统计：探索 {result.explored_states}，生成 {result.generated_states}",
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
