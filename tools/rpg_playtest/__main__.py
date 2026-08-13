"""CLI for the fixed-seed RPG module playtester."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from .simulator import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_STATES,
    SearchConfig,
    SearchResult,
    load_module,
    search_module,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rpg_playtest",
        description="固定种子搜索 YawnBot RPG 模组的目标结局轨迹",
    )
    parser.add_argument("module", type=Path, help="要试玩的模组 YAML 文件")
    parser.add_argument("--seed", required=True, type=int, help="固定随机种子")
    parser.add_argument("--ending", required=True, help="要搜索的结局 id")
    parser.add_argument("--players", type=int, help="玩家数，默认取模组 min_players")
    parser.add_argument(
        "--max-depth",
        type=int,
        default=DEFAULT_MAX_DEPTH,
        help=f"最大轨迹步数（默认 {DEFAULT_MAX_DEPTH}）",
    )
    parser.add_argument(
        "--max-states",
        type=int,
        default=DEFAULT_MAX_STATES,
        help=f"最大生成状态数（默认 {DEFAULT_MAX_STATES}）",
    )
    parser.add_argument("--json", action="store_true", help="输出稳定 JSON")
    return parser


def _invalid_module(args: argparse.Namespace, error: Exception) -> SearchResult:
    return SearchResult(
        ok=False,
        reason="invalid_module",
        message=f"模组读取或校验失败：{error}",
        seed=args.seed,
        target_ending=args.ending,
        max_depth=args.max_depth,
        max_states=args.max_states,
    )


def _render_text(result: SearchResult) -> str:
    if not result.ok:
        return (
            f"试玩失败 [{result.reason}]：{result.message}\n"
            f"已探索 {result.explored_states} 个状态，"
            f"生成 {result.generated_states} 个状态。"
        )
    lines = [
        f"模组：《{result.module_name}》（{result.module_id}）",
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


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        module = load_module(args.module)
    except Exception as error:  # noqa: BLE001
        result = _invalid_module(args, error)
    else:
        result = search_module(
            module,
            SearchConfig(
                seed=args.seed,
                ending_id=args.ending,
                players=args.players,
                max_depth=args.max_depth,
                max_states=args.max_states,
            ),
        )
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))  # noqa: T201
    elif result.ok:
        print(_render_text(result))  # noqa: T201
    else:
        print(_render_text(result), file=sys.stderr)  # noqa: T201
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
