"""Human-readable playtest trace formatting tests."""

from __future__ import annotations

from tools.rpg_playtest.output import render_result_text
from tools.rpg_playtest.simulator import SearchResult


def test_render_result_text_groups_step_details_for_scanning() -> None:
    result = SearchResult(
        ok=True,
        reason="success",
        message="",
        module_id="demo",
        module_name="示例模组",
        seed=7,
        target_ending="good",
        final_ending={"id": "good", "name": "好结局", "outcome": "good"},
        final_scene="station",
        elapsed_minutes=12,
        clues=["key"],
        flags={"opened": 1},
        events=["door_opened"],
        players=[{"name": "调查员1"}],
        steps=[
            {
                "index": 1,
                "action": "attack",
                "actor": "调查员1",
                "target": "monster:cart",
                "scene_before": "station",
                "scene_after": "station",
                "elapsed_before": 5,
                "elapsed_after": 8,
                "rolls": [
                    {
                        "kind": "d100",
                        "player": "调查员1",
                        "skill": "brawl",
                        "roll": 9,
                        "value": 25,
                        "tier": "hard",
                    },
                    {"kind": "damage", "expression": "1d3", "result": 2},
                ],
                "clues_added": ["key"],
                "flags_changed": {"opened": 1},
            }
        ],
        explored_states=3,
        generated_states=4,
    )

    text = render_result_text(result)

    assert "轨迹 · 1 步" in text
    assert "01  攻击 · 调查员1 → monster:cart" in text
    assert "场景  station（未移动）    时间  5m → 8m（+3m）" in text
    assert "检定  调查员1 · brawl · 9/25 · 困难成功" in text
    assert "伤害  1d3 → 2" in text
    assert "结局" in text
    assert "最终位置  station · 12m" in text
    assert "搜索状态  已探索 3 · 已生成 4" in text
