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
    assert any("非引擎写入的 flag" in i.message for i in issues)


def test_skeleton_template_is_clean() -> None:
    """README 最小骨架：结构通过且无 ERROR（WARNING 允许）。"""
    data = state.blank_module_dict()
    report = validate_structure(data)
    assert report.module is not None, _messages(report.issues)
    issues = report.issues + run_lint(data)
    assert not [i for i in issues if i.severity == "ERROR"], _messages(issues)
