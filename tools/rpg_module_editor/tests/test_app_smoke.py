"""应用级冒烟测试：Textual run_test() 无头驱动整个编辑器。

核心不变式：编辑器对模组的判定与引擎 load_modules 口径一致——
校验页零 ERROR 的模组必须能通过 ModuleDef.model_validate。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import yaml
from textual.widgets import TabbedContent

from tools.rpg_module_editor.app import ModuleEditorApp
from tools.rpg_module_editor.schema_loader import ModuleDef, modules_dir
from tools.rpg_module_editor.tabs.report_tab import collect_issues
from tools.rpg_module_editor.widgets import ConfirmScreen

if TYPE_CHECKING:
    from textual.pilot import Pilot
    from textual.widget import Widget

if TYPE_CHECKING:
    from pathlib import Path

_YUZHAI = modules_dir() / "yuzhai_old_house.yaml"

_ALL_TABS = (
    "tab-module",
    "tab-scenes",
    "tab-npcs",
    "tab-monsters",
    "tab-clues",
    "tab-endings",
    "tab-events",
    "tab-yaml",
    "tab-report",
)

pytestmark = pytest.mark.asyncio


async def _wait_mounted(pilot: Pilot, widget: Widget) -> None:
    """惰性挂载的 TabPane 内容需要若干轮 pause 才就绪；编辑前先等待。"""
    for _ in range(50):
        if widget.is_mounted:
            return
        await pilot.pause()
    msg = "控件始终未挂载"
    raise AssertionError(msg)


async def test_all_tabs_mount_and_zero_errors() -> None:
    """遍历全部 Tab 无异常；yuzhai 校验零 ERROR（与引擎口径一致）。"""
    app = ModuleEditorApp(initial_path=_YUZHAI)
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        tc = app.query_one(TabbedContent)
        for tab_id in _ALL_TABS:
            tc.active = tab_id
            await pilot.pause()
        ModuleDef.model_validate(app.draft.data)
        issues, structure_ok = collect_issues(app.draft.data)
        errors = [i for i in issues if i.severity == "ERROR"]
        assert structure_ok and not errors, [(i.path_label, i.message) for i in errors]


async def test_edit_dirty_and_save_roundtrip(tmp_path: Path) -> None:
    """编辑 → 脏标记 → 保存 → 引擎口径复核。"""
    app = ModuleEditorApp(initial_path=_YUZHAI)
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        assert not app.draft.dirty
        app._module_tab._description.input.value = "被冒烟测试修改的简介"
        await pilot.pause()
        assert app.draft.data["description"] == "被冒烟测试修改的简介"
        assert app.draft.dirty
        target = tmp_path / "roundtrip.yaml"
        app._write_to(target)
        await pilot.pause()
        assert not app.draft.dirty
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
        assert ModuleDef.model_validate(raw).description == "被冒烟测试修改的简介"


async def test_yaml_source_tab_apply(tmp_path: Path) -> None:  # noqa: ARG001
    """YAML 源码页：文本编辑 → 应用 → 状态整体替换。"""
    app = ModuleEditorApp(initial_path=_YUZHAI)
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        tc = app.query_one(TabbedContent)
        tc.active = "tab-yaml"
        await pilot.pause()
        text = app._yaml_tab.area.text
        assert "id: yuzhai_old_house" in text
        for scene_id in ("porch", "living_room", "study", "basement"):
            assert scene_id in text
        # 源码小改（改简介）再应用
        edited = text.replace(
            "description: 一封来信将你们引向雨夜的沈家旧宅。",
            "description: 源码页直改的简介。",
        )
        assert edited != text
        await _wait_mounted(pilot, app._yaml_tab.area)
        app._yaml_tab.area.text = edited
        await pilot.pause()
        assert app._yaml_tab.apply_to_form()
        await pilot.pause()
        assert str(app.draft.data["description"]).startswith("源码页直改的简介。")
        ModuleDef.model_validate(app.draft.data)


async def test_scene_edit_and_rename_cascade() -> None:
    """场景表单编辑写回；改名级联 start_scene 与出口引用。"""
    app = ModuleEditorApp(initial_path=_YUZHAI)
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-scenes"
        await pilot.pause()
        st = app._scenes_tab
        st._scene_idx = 1
        st._fill_scene_form()
        st._fill_check_list()
        await pilot.pause()
        scene = st._current_scene()
        assert scene is not None and scene["id"] == "living_room"
        # 文本编辑写回 + priority 保持 int（MRO 双 handler 回归）
        await _wait_mounted(pilot, st._check_form._success.area)
        st._check_form._success.area.text = "冒烟改写的成功文案"
        await pilot.pause()
        assert scene["checks"][0]["success_text"] == "冒烟改写的成功文案"
        prio = scene["checks"][0]["priority"]
        assert prio == 1 and isinstance(prio, int)
        # 改名级联：study → study_room
        data = app.draft.data
        study_idx = next(
            i for i, s in enumerate(data["scenes"]) if s.get("id") == "study"
        )
        st._scene_idx = study_idx
        st._fill_scene_form()
        st._scene_id.input.value = "study_room"
        await pilot.pause()
        to_scenes = [
            e.get("to_scene") for s in data["scenes"] for e in s.get("exits", [])
        ]
        assert "study_room" in to_scenes and "study" not in to_scenes
        ModuleDef.model_validate(data)


async def test_npc_schedule_and_secret_leak() -> None:
    """NPC 行程编辑写回；机密泄露实时检查。"""
    app = ModuleEditorApp(initial_path=_YUZHAI)
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-npcs"
        await pilot.pause()
        nt = app._npcs_tab
        nt._npc_idx = 0
        nt._fill_npc_form()
        nt._fill_schedule()
        await pilot.pause()
        npc = nt._current_npc()
        assert npc is not None and npc["id"] == "butler"
        # 行程页激活后编辑 activity
        nt.query_one(TabbedContent).active = "tab-npc-schedule"
        await pilot.pause()
        await _wait_mounted(pilot, nt._schedule_form._activity.input)
        nt._schedule_form._activity.input.value = "冒烟改写的活动"
        await pilot.pause()
        assert npc["schedule"][0]["activity"] == "冒烟改写的活动"
        # 把机密复制进 knows → 泄露告警
        secret = str(npc["secrets"][0])
        nt._write_npc_field("knows", [*npc.get("knows", []), secret])
        await pilot.pause()
        assert "✗" in str(nt._leak_feedback.render())
        # 还原 → 合法
        nt._write_npc_field("knows", [k for k in npc["knows"] if k != secret])
        await pilot.pause()
        ModuleDef.model_validate(app.draft.data)


async def test_quit_guard_shows_confirm() -> None:
    """脏状态下 ctrl+q 弹确认框；取消后应用仍在。"""
    app = ModuleEditorApp(initial_path=_YUZHAI)
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        app._module_tab._description.input.value = "制造脏状态"
        await pilot.pause()
        assert app.draft.dirty
        await pilot.press("ctrl+q")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmScreen)


async def test_new_module_skeleton_is_clean() -> None:
    """新建（骨架）初始即结构合法、无 ERROR。"""
    app = ModuleEditorApp()
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        ModuleDef.model_validate(app.draft.data)
        issues, ok = collect_issues(app.draft.data)
        assert ok
        assert not [i for i in issues if i.severity == "ERROR"]
