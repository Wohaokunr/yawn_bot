"""validate / lint 诊断测试：坏夹具逐条命中。"""

from __future__ import annotations

import copy

import pytest

from tools.rpg_module_editor import state
from tools.rpg_module_editor.lint import run_lint
from tools.rpg_module_editor.schema_loader import ModuleDef, modules_dir
from tools.rpg_module_editor.validate import (
    check_condition,
    check_ending_condition,
    describe_loc,
    validate_structure,
)
from tools.rpg_module_editor.yaml_io import load_module_file


@pytest.fixture(scope="module")
def valid_data() -> dict:
    data, _ = load_module_file(modules_dir() / "yuzhai_old_house.yaml")
    return data


def _messages(issues: list) -> str:
    return "\n".join(f"{i.severity} {i.path_label} {i.message}" for i in issues)


def _p1_2_issues(issues: list) -> list:
    return [issue for issue in issues if issue.hint == "P1-2 可达性检查"]


def _p1_3_issues(issues: list) -> list:
    return [issue for issue in issues if issue.hint.startswith("P1-3")]


def test_valid_module_has_zero_errors(valid_data: dict) -> None:
    """核心不变式：编辑器诊断与 load_modules 的结论一致。"""
    ModuleDef.model_validate(valid_data)  # 引擎口径通过
    report = validate_structure(valid_data)
    assert report.module is not None
    assert not report.errors, _messages(report.issues)
    issues = report.issues + run_lint(valid_data)
    assert not [i for i in issues if i.severity == "ERROR"], _messages(issues)


def test_missing_required_field_maps_to_chinese(valid_data: dict) -> None:
    data = copy.deepcopy(valid_data)
    del data["scenes"][0]["narration"]
    report = validate_structure(data)
    assert report.module is None
    labels = [(i.path_label, i.message) for i in report.errors]
    assert any("场景" in p and "#1" in p and "narration" in p for p, _ in labels)
    assert any("必填字段缺失" in m for _, m in labels)


def test_dangling_reference_error(valid_data: dict) -> None:
    data = copy.deepcopy(valid_data)
    data["scenes"][0]["exits"][0]["condition"] = "clue:not_exist"
    report = validate_structure(data)
    assert any("未定义的线索" in i.message for i in report.errors)


def test_dangling_exit_error_stays_in_schema(valid_data: dict) -> None:
    data = copy.deepcopy(valid_data)
    data["scenes"][0]["exits"][0]["to_scene"] = "missing_scene"
    report = validate_structure(data)
    assert any("未定义的场景" in i.message for i in report.errors)


def test_trivially_true_ending_rejected(valid_data: dict) -> None:
    data = copy.deepcopy(valid_data)
    data["endings"][0]["condition"] = "always"
    report = validate_structure(data)
    assert any("恒真" in i.message for i in report.errors)


def test_loc_description_decorates_entities(valid_data: dict) -> None:
    text = describe_loc(("scenes", 1, "checks", 0, "san_loss"), valid_data)
    assert "场景" in text and "living_room" in text and "检定点" in text


def test_duplicate_ending_ids_caught_by_lint(valid_data: dict) -> None:
    """schema 漏检结局 id 重复——lint 必须补上（ERROR）。"""
    data = copy.deepcopy(valid_data)
    dup = copy.deepcopy(data["endings"][0])
    data["endings"].append(dup)
    issues = run_lint(data)
    assert any(i.severity == "ERROR" and "结局 id 重复" in i.message for i in issues)


def test_unknown_key_is_error(valid_data: dict) -> None:
    data = copy.deepcopy(valid_data)
    data["scenez"] = []
    issues = run_lint(data)
    assert any(
        i.severity == "ERROR" and "未知键" in i.message and "scenez" in i.message
        for i in issues
    )


def test_non_ascii_id_is_error(valid_data: dict) -> None:
    data = copy.deepcopy(valid_data)
    data["clues"][0]["id"] = "黄铜钥匙"
    issues = run_lint(data)
    assert any(i.severity == "ERROR" and "snake_case" in i.message for i in issues)


def test_digits_in_narration_warned(valid_data: dict) -> None:
    data = copy.deepcopy(valid_data)
    data["scenes"][0]["narration"] = "你有 3 次机会。"
    issues = run_lint(data)
    assert any("阿拉伯数字" in i.message for i in issues)


def test_san_check_hygiene_warned(valid_data: dict) -> None:
    data = copy.deepcopy(valid_data)
    san_check = data["scenes"][1]["checks"][0]
    assert san_check["skill"] == "san"
    san_check["priority"] = 0
    san_check["once"] = False
    issues = run_lint(data)
    joined = _messages(issues)
    assert "priority" in joined and "once" in joined


def test_time_fallback_ending_must_be_last(valid_data: dict) -> None:
    data = copy.deepcopy(valid_data)
    # dawn_breaks（time_after:06:00）本在最后；把它挪到最前
    endings = data["endings"]
    dawn = next(e for e in endings if e["id"] == "dawn_breaks")
    endings.remove(dawn)
    endings.insert(0, dawn)
    issues = run_lint(data)
    assert any("时间兜底结局" in i.message for i in issues)


def test_unreachable_scene_warned(valid_data: dict) -> None:
    data = copy.deepcopy(valid_data)
    data["scenes"].append({"id": "sealed_room", "name": "密室", "narration": "无"})
    issues = run_lint(data)
    assert any("不可达" in i.message for i in issues)


def test_condition_aware_graph_rejects_self_locked_scene(valid_data: dict) -> None:
    data = copy.deepcopy(valid_data)
    clue_id = "sealed_clue"
    data["clues"].append({"id": clue_id, "name": "密室线索", "text": "无"})
    living_room = next(
        scene for scene in data["scenes"] if scene["id"] == "living_room"
    )
    sealed = copy.deepcopy(living_room)
    sealed["id"] = "sealed_room"
    sealed["name"] = "密室"
    sealed["checks"][0]["id"] = "sealed_search"
    sealed["checks"][0]["clue"] = clue_id
    sealed["exits"] = []
    data["scenes"].append(sealed)
    living_room["exits"].append(
        {"to_scene": "sealed_room", "condition": f"clue:{clue_id}"}
    )

    issues = run_lint(data)

    assert any(
        issue.section == "场景"
        and "sealed_room" in issue.path_label
        and "不可达" in issue.message
        for issue in _p1_2_issues(issues)
    )


def test_unreachable_ending_and_missing_clue_source_are_reported(
    valid_data: dict,
) -> None:
    data = copy.deepcopy(valid_data)
    clue_id = "never_written"
    data["clues"].append({"id": clue_id, "name": "未写入线索", "text": "无"})
    data["endings"][0]["condition"] = f"clue:{clue_id}"

    issues = run_lint(data)

    assert any(
        issue.section == "条件"
        and clue_id in issue.message
        and "写入来源" in issue.message
        for issue in _p1_2_issues(issues)
    )
    assert any(
        issue.section == "结局"
        and "不可" in issue.message
        for issue in _p1_2_issues(issues)
    )


def test_all_declared_endings_unreachable_get_module_warning(valid_data: dict) -> None:
    data = copy.deepcopy(valid_data)
    for ending in data["endings"]:
        ending["condition"] = "clue:never_ending"

    issues = _p1_2_issues(run_lint(data))

    assert any(
        issue.section == "模组" and "没有任何声明结局可达" in issue.message
        for issue in issues
    )


def test_monster_dead_without_scene_source_is_reported(valid_data: dict) -> None:
    data = copy.deepcopy(valid_data)
    monster = copy.deepcopy(data["monsters"][0])
    monster["id"] = "unplaced_beast"
    monster["on_death_clue"] = None
    data["monsters"].append(monster)
    data["endings"][0]["condition"] = "monster_dead:unplaced_beast"

    issues = run_lint(data)

    assert any(
        issue.section == "条件"
        and "monster_dead:unplaced_beast" in issue.message
        for issue in _p1_2_issues(issues)
    )


def test_existing_modules_have_no_p1_2_false_positive() -> None:
    for path in sorted(modules_dir().glob("*.yaml")):
        data, _ = load_module_file(path)
        issues = _p1_2_issues(run_lint(data))
        assert not issues, f"{path.name}: {_messages(issues)}"


def test_unused_clue_warned(valid_data: dict) -> None:
    data = copy.deepcopy(valid_data)
    data["clues"].append({"id": "orphan_clue", "name": "孤儿线索", "text": "无"})
    issues = run_lint(data)
    assert any(
        "orphan_clue" in i.path_label or "orphan_clue" in i.message for i in issues
    )
    assert any("无任何获取途径" in i.message for i in issues)


def test_schedule_without_fallback_warned(valid_data: dict) -> None:
    data = copy.deepcopy(valid_data)
    butler = next(n for n in data["npcs"] if n["id"] == "butler")
    butler["schedule"] = [
        {
            "from": "21:00",
            "to": "23:30",
            "scene": "living_room",
            "condition": "flag:assault",
        }
    ]
    issues = run_lint(data)
    assert any("兜底条目" in i.message for i in issues)


def test_allday_entry_blocks_following_entries(valid_data: dict) -> None:
    data = copy.deepcopy(valid_data)
    butler = next(n for n in data["npcs"] if n["id"] == "butler")
    butler["schedule"] = [
        {"from": "00:00", "to": "00:00", "scene": "living_room"},
        {"from": "21:00", "to": "23:30", "scene": "study"},
    ]
    issues = run_lint(data)
    assert any("永不可达" in i.message for i in issues)


def test_condition_live_feedback(valid_data: dict) -> None:
    assert check_condition("clue:brass_key & monster_dead:ghoul", valid_data) is None
    assert "未定义的线索" in (check_condition("clue:nope", valid_data) or "")
    assert "空词条" in (check_condition("clue:brass_key &", valid_data) or "")
    assert check_ending_condition("always", valid_data) is not None
    assert check_ending_condition("clue:brass_key", valid_data) is None


def test_unknown_flag_warned(valid_data: dict) -> None:
    data = copy.deepcopy(valid_data)
    data["endings"][0]["condition"] = "flag:custom_flag"
    issues = run_lint(data)
    assert any("没有已知写入来源" in i.message for i in issues)


def test_social_node_declared_flag_is_known(valid_data: dict) -> None:
    data = copy.deepcopy(valid_data)
    butler = next(n for n in data["npcs"] if n["id"] == "butler")
    butler["social_nodes"][0]["success_flags"] = ["custom_flag"]
    data["endings"][0]["condition"] = "flag:custom_flag"
    issues = run_lint(data)
    assert not any("flag:custom_flag" in i.message for i in issues)


def test_multi_clue_condition_uses_all_declared_sources(valid_data: dict) -> None:
    data = copy.deepcopy(valid_data)
    data["clues"].append({"id": "second_clue", "name": "第二线索", "text": "无"})
    living_room = next(
        scene for scene in data["scenes"] if scene["id"] == "living_room"
    )
    living_room["checks"][0]["clue"] = "second_clue"
    data["endings"][0]["condition"] = "clues:brass_key+second_clue"

    issues = _p1_2_issues(run_lint(data))

    assert not any("clue:brass_key" in issue.message for issue in issues)
    assert not any("clue:second_clue" in issue.message for issue in issues)


def test_npc_death_flag_is_a_known_condition_source(valid_data: dict) -> None:
    data = copy.deepcopy(valid_data)
    data["endings"][0]["condition"] = "flag:npc_dead:butler"

    issues = _p1_2_issues(run_lint(data))

    assert not any("npc_dead:butler" in issue.message for issue in issues)


def test_schedule_gap_is_limited_to_playable_window(valid_data: dict) -> None:
    data = copy.deepcopy(valid_data)
    butler = next(n for n in data["npcs"] if n["id"] == "butler")
    butler["schedule"] = [
        {"from": "21:00", "to": "06:00", "scene": "living_room"},
    ]

    issues = _p1_3_issues(run_lint(data))

    assert not any("05:00→06:00" in issue.message for issue in issues)


def test_schedule_gap_and_cross_midnight_window_are_reported(
    valid_data: dict,
) -> None:
    data = copy.deepcopy(valid_data)
    butler = next(n for n in data["npcs"] if n["id"] == "butler")
    butler["schedule"] = [
        {"from": "21:00", "to": "23:30", "scene": "living_room"},
    ]

    issues = _p1_3_issues(run_lint(data))

    assert any(
        issue.severity == "WARNING"
        and "23:30→06:00" in issue.message
        and "可达场景" in issue.message
        for issue in issues
    )

    butler["schedule"] = [
        {"from": "21:00", "to": "02:00", "away": True},
        {"from": "02:00", "to": "06:00", "scene": "living_room"},
    ]
    assert not _p1_3_issues(run_lint(data))


def test_schedule_condition_without_source_is_never_effective(
    valid_data: dict,
) -> None:
    data = copy.deepcopy(valid_data)
    butler = next(n for n in data["npcs"] if n["id"] == "butler")
    butler["schedule"] = [
        {
            "from": "21:00",
            "to": "23:00",
            "scene": "living_room",
            "condition": "flag:never_written",
        }
    ]

    issues = _p1_3_issues(run_lint(data))

    assert any(
        issue.section == "NPC"
        and "行程 #1" in issue.path_label
        and "不会生效" in issue.message
        for issue in issues
    )


def test_schedule_targeting_unreachable_scene_is_reported(valid_data: dict) -> None:
    data = copy.deepcopy(valid_data)
    butler = next(n for n in data["npcs"] if n["id"] == "butler")
    butler["schedule"] = [
        {"from": "21:00", "to": "23:00", "scene": "sealed_room"},
    ]

    issues = _p1_3_issues(run_lint(data))

    assert any(
        issue.section == "NPC"
        and "sealed_room" in issue.message
        and "不可达" in issue.message
        for issue in issues
    )


def test_private_secret_collision_reports_source_and_public_sink(
    valid_data: dict,
) -> None:
    data = copy.deepcopy(valid_data)
    butler = next(n for n in data["npcs"] if n["id"] == "butler")
    secret = "only the butler knows this sentence"
    butler["secrets"] = [secret]
    butler["schedule"][0]["activity"] = f"守着油灯，{secret}"

    issues = _p1_3_issues(run_lint(data))

    assert any(
        issue.severity == "ERROR"
        and "secrets #1" in issue.path_label
        and "行程 #1 › activity" in issue.message
        for issue in issues
    )


def test_private_fact_collision_checks_broadcast_and_social_text(
    valid_data: dict,
) -> None:
    data = copy.deepcopy(valid_data)
    butler = next(n for n in data["npcs"] if n["id"] == "butler")
    fact_text = "the private fact must never be broadcast"
    butler["facts"] = [{"id": "hidden_fact", "name": "隐秘事实", "text": fact_text}]
    data["endings"][0]["text"] = f"终局文案：{fact_text}"
    butler["social_nodes"][0]["goal"] = f"公开目标：{fact_text}"

    issues = _p1_3_issues(run_lint(data))

    assert any(
        "私人情报 #1" in issue.path_label
        and "结局" in issue.message
        for issue in issues
    )
    assert any(
        "私人情报 #1" in issue.path_label
        and "社交节点 #1 › goal" in issue.message
        for issue in issues
    )


def test_private_text_collision_normalizes_scene_and_clue_sinks(
    valid_data: dict,
) -> None:
    data = copy.deepcopy(valid_data)
    butler = next(n for n in data["npcs"] if n["id"] == "butler")
    secret = "Private Secret Token"
    butler["secrets"] = [secret]
    data["scenes"][0]["narration"] = "private   secret token appears here"
    fact_text = "另一段个人情报正文"
    butler["facts"] = [{"id": "hidden_fact", "name": "隐秘事实", "text": fact_text}]
    data["clues"][0]["text"] = f"线索中不应出现：{fact_text}"

    issues = _p1_3_issues(run_lint(data))

    assert any("场景" in issue.message for issue in issues)
    assert any("线索" in issue.message for issue in issues)


def test_existing_modules_have_no_p1_3_errors() -> None:
    for path in sorted(modules_dir().glob("*.yaml")):
        data, _ = load_module_file(path)
        issues = _p1_3_issues(run_lint(data))
        errors = [issue for issue in issues if issue.severity == "ERROR"]
        assert not errors, f"{path.name}: {_messages(errors)}"


def test_skeleton_template_is_clean() -> None:
    """README 最小骨架：结构通过且无 ERROR（WARNING 允许）。"""
    data = state.blank_module_dict()
    report = validate_structure(data)
    assert report.module is not None, _messages(report.issues)
    issues = report.issues + run_lint(data)
    assert not [i for i in issues if i.severity == "ERROR"], _messages(issues)
