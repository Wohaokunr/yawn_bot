"""yaml_io 序列化管线测试：以「雨夜旧宅」为黄金回环夹具。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import yaml

from tools.rpg_module_editor import yaml_io
from tools.rpg_module_editor.schema_loader import ModuleDef, modules_dir

if TYPE_CHECKING:
    from pathlib import Path

_YUZHAI = modules_dir() / "yuzhai_old_house.yaml"


@pytest.fixture(scope="module")
def yuzhai_data() -> dict:
    data, _ = yaml_io.load_module_file(_YUZHAI)
    return data


def test_yuzhai_round_trip_semantic_equal(yuzhai_data: dict) -> None:
    """加载 → 输出 → 再加载：模型级语义完全一致（默认值裁剪不算差异）。"""
    text = yaml_io.dump_module_text(yuzhai_data)
    reloaded = yaml_io.normalize_data(yaml_io.parse_yaml_text(text))
    original_model = ModuleDef.model_validate(yuzhai_data)
    reloaded_model = ModuleDef.model_validate(reloaded)
    assert reloaded_model.model_dump(by_alias=True) == original_model.model_dump(
        by_alias=True
    )


def test_dump_is_idempotent_fixpoint(yuzhai_data: dict) -> None:
    """二次输出与首次输出逐字节一致（脏检测的稳定基线）。"""
    first = yaml_io.dump_module_text(yuzhai_data)
    reloaded = yaml_io.normalize_data(yaml_io.parse_yaml_text(first))
    assert yaml_io.dump_module_text(reloaded) == first


def test_times_stay_quoted(yuzhai_data: dict) -> None:
    """时间字符串必须带引号输出（YAML 1.1 六十进制陷阱）。"""
    text = yaml_io.dump_module_text(yuzhai_data)
    assert "'21:00'" in text or '"21:00"' in text
    reloaded = yaml.safe_load(text)
    assert reloaded["time"]["start"] == "21:00"
    entry = reloaded["npcs"][0]["schedule"][0]
    assert entry["from"] == "21:00"


def test_multiline_strings_use_literal_block(yuzhai_data: dict) -> None:
    text = yaml_io.dump_module_text(yuzhai_data)
    assert "narration: |" in text
    assert "opening: |" in text


def test_defaults_are_stripped(yuzhai_data: dict) -> None:
    """与模型默认一致的键在输出中省略（紧凑化）。"""
    text = yaml_io.dump_module_text(yuzhai_data)
    assert "generic_endings:" not in text  # 默认 true
    assert "once: false" not in text
    assert "hp: 10" not in text  # NPC 默认生命值
    assert "priority: 0" not in text


def test_header_preserved(yuzhai_data: dict) -> None:
    header, _ = yaml_io.split_header(_YUZHAI.read_text(encoding="utf-8"))
    assert header.startswith("# 入门模组")
    text = yaml_io.dump_module_text(yuzhai_data, header)
    assert text.startswith(header)
    reloaded = yaml_io.normalize_data(yaml_io.parse_yaml_text(text))
    assert ModuleDef.model_validate(reloaded).model_dump(
        by_alias=True
    ) == ModuleDef.model_validate(yuzhai_data).model_dump(by_alias=True)


def test_unknown_keys_survive_round_trip(yuzhai_data: dict) -> None:
    """未知键不得被静默丢掉（pydantic 会忽略，dict 状态必须保留）。"""
    data = dict(yuzhai_data)
    data["custom_note"] = "作者备注"
    text = yaml_io.dump_module_text(data)
    reloaded = yaml_io.normalize_data(yaml_io.parse_yaml_text(text))
    assert reloaded["custom_note"] == "作者备注"


def test_output_is_lf_only(yuzhai_data: dict) -> None:
    text = yaml_io.dump_module_text(yuzhai_data)
    assert "\r" not in text


def test_sexagesimal_int_repaired() -> None:
    """裸写 21:00 被 PyYAML 读成 1260，规范化须还原为 HH:MM。"""
    data = yaml_io.normalize_data(
        {
            "time": {"start": 1260},
            "npcs": [
                {"schedule": [{"from": 1260, "to": 1380, "scene": "s"}]},
            ],
        }
    )
    assert data["time"]["start"] == "21:00"
    assert data["npcs"][0]["schedule"][0]["from"] == "21:00"
    assert data["npcs"][0]["schedule"][0]["to"] == "23:00"


def test_trailing_newlines_stripped() -> None:
    data = yaml_io.normalize_data({"opening": "第一段\n第二段\n\n"})
    assert data["opening"] == "第一段\n第二段"


def test_parse_error_reports_position_in_chinese() -> None:
    with pytest.raises(yaml_io.ModuleParseError, match="YAML 解析失败"):
        yaml_io.parse_yaml_text("a: [1, 2\n")


def test_non_mapping_rejected() -> None:
    with pytest.raises(yaml_io.ModuleParseError, match="映射"):
        yaml_io.parse_yaml_text("- just\n- a\n- list\n")


def test_save_and_reload(tmp_path: Path, yuzhai_data: dict) -> None:
    target = tmp_path / "out.yaml"
    yaml_io.save_module_file(target, yuzhai_data, "# 测试头部")
    raw = target.read_bytes()
    assert b"\r" not in raw
    data, header = yaml_io.load_module_file(target)
    assert header == "# 测试头部"
    # 保存结果必须能过引擎校验，且与原件模型级等价
    assert ModuleDef.model_validate(data).model_dump(
        by_alias=True
    ) == ModuleDef.model_validate(yuzhai_data).model_dump(by_alias=True)


def test_schedule_entry_alias_stripped_consistently() -> None:
    """行程条目默认字段（activity/condition/away/scene=None）被裁剪。"""
    data = {
        "id": "m",
        "name": "M",
        "opening": "开局",
        "start_scene": "s",
        "scenes": [{"id": "s", "name": "S", "narration": "旁白"}],
        "npcs": [
            {
                "id": "n",
                "name": "N",
                "public_desc": "p",
                "persona": "q",
                "schedule": [
                    {"from": "08:00", "to": "20:00", "scene": "s", "away": False},
                ],
            },
        ],
    }
    text = yaml_io.dump_module_text(data)
    assert "away:" not in text
    assert "activity:" not in text
    assert "condition:" not in text
    assert yaml_io.normalize_data(yaml_io.parse_yaml_text(text)) == (
        yaml_io.strip_defaults(data, ModuleDef)
    )
