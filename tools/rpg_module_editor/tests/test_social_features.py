"""当前 RPG schema 的团队检定与 NPC 社交编辑验收。"""

from __future__ import annotations

import copy

import pytest
from textual.widgets import TabbedContent

from tools.rpg_module_editor.app import ModuleEditorApp
from tools.rpg_module_editor.lint import run_lint
from tools.rpg_module_editor.schema_loader import ModuleDef, modules_dir
from tools.rpg_module_editor.state import clue_referrers, rename_entity, rename_npc_fact
from tools.rpg_module_editor.validate import validate_structure
from tools.rpg_module_editor.yaml_io import (
    dump_module_text,
    load_module_file,
    normalize_data,
    parse_yaml_text,
)

_YUZHAI = modules_dir() / "yuzhai_old_house.yaml"
_TEAM_REQUIRED_SUCCESSES = 2
_BUTLER_FACT_COUNT = 1
_BUTLER_NODE_COUNT = 2
_BUTLER_STRATEGY_COUNT = 3
_EDITED_RAPPORT = 12
_EDITED_SUCCESS_DELTA = 20

@pytest.mark.asyncio
async def test_team_check_mode_roundtrip_and_individual_cleanup() -> None:
    app = ModuleEditorApp(initial_path=_YUZHAI)
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-scenes"
        await pilot.pause()
        scenes = app._scenes_tab
        scenes._scene_idx = 1
        scenes._fill_check_list()
        check = scenes._current_check()
        assert check is not None

        scenes._write_check_field("mode", "team")
        scenes._write_check_field("required_successes", _TEAM_REQUIRED_SUCCESSES)
        assert check["mode"] == "team"
        assert check["required_successes"] == _TEAM_REQUIRED_SUCCESSES
        ModuleDef.model_validate(app.draft.data)

        scenes._write_check_field("mode", "individual")
        assert check["mode"] == "individual"
        assert "required_successes" not in check
        ModuleDef.model_validate(app.draft.data)


@pytest.mark.asyncio
async def test_social_forms_edit_and_yaml_roundtrip() -> None:
    app = ModuleEditorApp(initial_path=_YUZHAI)
    async with app.run_test(size=(160, 52)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-npcs"
        await pilot.pause()
        npcs = app._npcs_tab
        npcs._npc_idx = 0
        npcs._fill_npc_form()
        social_tabs = npcs.query_one(TabbedContent)
        social_tabs.active = "tab-npc-social"
        await pilot.pause()

        social = npcs._social
        npc = npcs._current_npc()
        assert npc is not None and npc["id"] == "butler"
        assert social._fact_idx == 0
        assert social._node_idx == 0
        assert social._strategy_idx == 0
        assert social._fact_list.option_count == _BUTLER_FACT_COUNT
        assert social._node_list.option_count == _BUTLER_NODE_COUNT
        assert social._strategy_list.option_count == _BUTLER_STRATEGY_COUNT

        social._write_social_field("initial_rapport", _EDITED_RAPPORT)
        social._write_fact_field("name", "夜间守秘（编辑）")
        social._write_node_field("success_rapport_delta", _EDITED_SUCCESS_DELTA)
        social._write_strategy_field("difficulty", "hard")
        social._write_node_field("private_clues", ["brass_key"])
        assert npc["initial_rapport"] == _EDITED_RAPPORT
        assert npc["facts"][0]["name"] == "夜间守秘（编辑）"
        assert (
            npc["social_nodes"][0]["success_rapport_delta"]
            == _EDITED_SUCCESS_DELTA
        )
        assert npc["social_nodes"][0]["strategies"][0]["difficulty"] == "hard"
        ModuleDef.model_validate(app.draft.data)

        original_fact_text = npc["facts"][0]["text"]
        leak_text = str(npc["persona"]).splitlines()[0]
        social._fact_text.area.text = leak_text
        await pilot.pause()
        assert "✗" in str(npcs._leak_feedback.render())
        social._fact_text.area.text = original_fact_text
        await pilot.pause()
        await pilot.pause()
        assert npc["facts"][0]["text"] == original_fact_text
        assert "✓" in str(npcs._leak_feedback.render())
        ModuleDef.model_validate(app.draft.data)

        reloaded = normalize_data(parse_yaml_text(dump_module_text(app.draft.data)))
        assert ModuleDef.model_validate(reloaded).npcs[0].facts[0].name == (
            "夜间守秘（编辑）"
        )
        assert reloaded["npcs"][0]["social_nodes"][0]["private_clues"] == [
            "brass_key"
        ]


def test_nested_social_diagnostics_and_clue_graph() -> None:
    data, _ = load_module_file(_YUZHAI)
    butler = next(n for n in data["npcs"] if n["id"] == "butler")
    node = butler["social_nodes"][0]
    node["private_clues"] = ["brass_key"]
    node["nested_typo"] = True
    node["success_text"] = "你有 3 次机会。"

    issues = run_lint(data)
    assert any("nested_typo" in i.message for i in issues)
    assert any("阿拉伯数字" in i.message for i in issues)
    assert any("社交节点" in ref for ref in clue_referrers(data, "brass_key"))

    broken_fact = copy.deepcopy(data)
    broken_fact["npcs"][0]["social_nodes"][0]["requires_facts"] = [
        "missing_fact"
    ]
    fact_report = validate_structure(broken_fact)
    assert any("未定义的情报" in issue.message for issue in fact_report.errors)

    broken_clue = copy.deepcopy(data)
    broken_clue["npcs"][0]["social_nodes"][0]["public_clues"] = [
        "missing_clue"
    ]
    clue_report = validate_structure(broken_clue)
    assert any("未定义的线索" in issue.message for issue in clue_report.errors)

    duplicate = copy.deepcopy(data)
    duplicate["npcs"][0]["facts"].append(
        copy.deepcopy(duplicate["npcs"][0]["facts"][0])
    )
    duplicate_report = validate_structure(duplicate)
    assert any(
        "facts id 不能重复" in issue.message
        for issue in duplicate_report.errors
    )


def test_social_rename_cascades_and_schema_leak_feedback() -> None:
    data, _ = load_module_file(_YUZHAI)
    butler = next(n for n in data["npcs"] if n["id"] == "butler")
    node = butler["social_nodes"][0]
    node["private_clues"] = ["brass_key"]
    rename_entity(data, "clue", "brass_key", "brass_key_renamed")
    assert node["private_clues"] == ["brass_key_renamed"]
    rename_entity(data, "clue", "brass_key_renamed", "brass_key")

    old_fact = butler["facts"][0]["id"]
    new_fact = "butler_fact_renamed"
    rename_npc_fact(butler, old_fact, new_fact)
    assert butler["facts"][0]["id"] == new_fact
    assert new_fact in butler["social_nodes"][0]["unlock_facts"]
    assert new_fact in butler["social_nodes"][1]["requires_facts"]

    rename_npc_fact(butler, new_fact, old_fact)
    assert ModuleDef.model_validate(data).npcs[0].id == "butler"
