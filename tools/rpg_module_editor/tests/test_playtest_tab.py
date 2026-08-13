"""试玩页 TUI 回归：草稿/磁盘数据源、异步结果和响应式布局。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from textual.widgets import Select, TabbedContent

from tools.rpg_module_editor.app import ModuleEditorApp
from tools.rpg_module_editor.schema_loader import modules_dir

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.pilot import Pilot

_YUZHAI = modules_dir() / "yuzhai_old_house.yaml"

pytestmark = pytest.mark.asyncio


async def _wait_until(
    pilot: Pilot,
    predicate: Callable[[], bool],
    *,
    attempts: int = 120,
) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError


async def test_playtest_tab_mounts_and_lists_endings() -> None:
    app = ModuleEditorApp(initial_path=_YUZHAI)
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        tab = app._playtest_tab
        assert tab.is_mounted
        assert tab._source.value == "draft"
        assert tab._ending.value == "truth_revealed"
        await pilot.press("f6")
        await pilot.pause()
        assert app.query_one(TabbedContent).active == "tab-playtest"


async def test_playtest_draft_runs_and_copies_json() -> None:
    app = ModuleEditorApp(initial_path=_YUZHAI)
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        tab = app._playtest_tab
        tab._seed.value = "0"
        tab._ending.value = "truth_revealed"
        tab._max_depth.value = "32"
        tab._max_states.value = "50000"
        tab._run_button.press()
        await _wait_until(pilot, lambda: tab._result is not None)
        assert tab._result is not None and tab._result.ok
        assert tab._result.final_ending is not None
        assert tab._result.final_ending["id"] == "truth_revealed"
        tab._copy_button.press()
        await pilot.pause()
        assert json.loads(app.clipboard) == tab._result.to_dict()


async def test_playtest_saved_source_ignores_invalid_unsaved_draft() -> None:
    app = ModuleEditorApp(initial_path=_YUZHAI)
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        tab = app._playtest_tab
        app.draft.data["id"] = "INVALID ID"
        app.on_data_changed()
        await pilot.pause()
        tab._source.value = "saved"
        await pilot.pause()
        assert tab._ending.value == "truth_revealed"
        tab._seed.value = "0"
        tab._max_depth.value = "32"
        tab._run_button.press()
        await _wait_until(pilot, lambda: tab._result is not None)
        assert tab._result is not None and tab._result.ok
        assert tab._result.module_id == "yuzhai_old_house"


async def test_playtest_reports_invalid_target_and_zero_search() -> None:
    app = ModuleEditorApp(initial_path=_YUZHAI)
    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        tab = app._playtest_tab
        tab._ending.set_options([("不存在", "missing_ending")])
        tab._ending.value = "missing_ending"
        tab._run_button.press()
        await _wait_until(pilot, lambda: tab._result is not None)
        assert tab._result is not None
        assert tab._result.reason == "unknown_ending"
        assert tab._result.explored_states == 0


@pytest.mark.parametrize("size", [(80, 32), (100, 36), (140, 44)])
async def test_playtest_tab_is_responsive(size: tuple[int, int]) -> None:
    app = ModuleEditorApp(initial_path=_YUZHAI)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-playtest"
        await pilot.pause()
        tab = app._playtest_tab
        assert tab.region.width > 0
        assert tab._trace.region.height > 0
        assert isinstance(tab._source, Select)


async def test_playtest_controls_remain_visible_on_wide_screen() -> None:
    """Settings rows must not consume all available height via a 1fr default."""
    app = ModuleEditorApp(initial_path=_YUZHAI)
    async with app.run_test(size=(179, 47)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-playtest"
        await pilot.pause()
        tab = app._playtest_tab
        assert tab._run_button.region.height > 0
        assert tab._copy_button.region.height > 0
        assert tab.query_one("#playtest-output").region.height > 0
        assert tab._run_button.region.bottom < app.size.height
