"""响应式布局、实体引用控件、搜索与深复制验收。"""

from __future__ import annotations

import copy

import pytest
from textual.widgets import Input, OptionList, TabbedContent

from tools.rpg_module_editor.app import ModuleEditorApp
from tools.rpg_module_editor.dialogs import SearchScreen
from tools.rpg_module_editor.schema_loader import ModuleDef, modules_dir
from tools.rpg_module_editor.state import (
    build_condition_tokens,
    build_reference_options,
    build_reference_options_for_field,
    duplicate_item,
    duplicate_npc,
    duplicate_scene,
    reference_descriptor,
    rename_entity,
    search_module,
)
from tools.rpg_module_editor.widgets import TokenPicker
from tools.rpg_module_editor.yaml_io import load_module_file

_YUZHAI = modules_dir() / "yuzhai_old_house.yaml"
_BASELINE_TIMELINE_WIDTH = 96
_SCENE_NESTED_MIN_HEIGHT = 18
_SCENE_PANE_MIN_HEIGHT = 14
_SCENE_LIST_MIN_HEIGHT = 8
_MAXIMIZED_TOOLBAR_MIN_WIDTH = 230
_MAXIMIZED_TAB_MIN_WIDTH = 220
_MAXIMIZED_TAB_MIN_HEIGHT = 65
_LAYOUT_CASES = [
    ((60, 24), "narrow short"),
    ((80, 30), "narrow short"),
    ((96, 36), "compact"),
    ((140, 44), "wide"),
    ((240, 80), "wide"),
]


def _option_ids(view: OptionList) -> set[str]:
    return {str(option.id) for option in view.options if option.id is not None}


def test_reference_catalog_search_and_duplicate_are_schema_neutral() -> None:
    data, _ = load_module_file(_YUZHAI)
    assert reference_descriptor("exit.to_scene") is not None
    presence_descriptor = reference_descriptor("scene.npcs")
    assert presence_descriptor is not None
    assert presence_descriptor.multiple is True
    assert reference_descriptor("opening") is None

    scene_ids = _option_ids_from_pairs(build_reference_options(data, "scene"))
    assert {"porch", "living_room", "basement"} <= scene_ids
    assert (
        _option_ids_from_pairs(build_reference_options_for_field(data, "start_scene"))
        == scene_ids
    )
    butler = next(npc for npc in data["npcs"] if npc["id"] == "butler")
    fact_ids = _option_ids_from_pairs(
        build_reference_options(data, "fact", context=butler)
    )
    assert fact_ids == {"butler_feeding_truth"}

    data["scenes"][1]["checks"][0]["unknown_note"] = "needle in unknown field"
    result = search_module(data, "needle")
    assert result[0].path == ("scenes", 1, "checks", 0, "unknown_note")
    assert result[0].tab_id == "tab-scenes"
    assert "检定点" in result[0].label

    items = [{"id": "clue", "payload": {"tags": ["old"]}}]
    new_index = duplicate_item(items, 0)
    assert new_index == 1
    assert items[1]["id"] == "clue_copy"
    items[1]["payload"]["tags"].append("new")
    assert items[0]["payload"]["tags"] == ["old"]

    scene_index = duplicate_scene(data, 1)
    assert scene_index is not None
    copied_scene = data["scenes"][scene_index]
    assert copied_scene["checks"][0]["id"] != data["scenes"][1]["checks"][0]["id"]

    npc_index = duplicate_npc(data, 0)
    assert npc_index == 1
    copied_npc = data["npcs"][npc_index]
    assert copied_npc["id"] != data["npcs"][0]["id"]
    assert copied_npc["facts"][0]["id"] != data["npcs"][0]["facts"][0]["id"]
    assert copied_npc["social_nodes"][0]["unlock_facts"] == [
        copied_npc["facts"][0]["id"]
    ]
    ModuleDef.model_validate(data)


def _option_ids_from_pairs(options: list[tuple[str, str]]) -> set[str]:
    return {value for _, value in options}


@pytest.mark.asyncio
@pytest.mark.parametrize(("size", "expected"), _LAYOUT_CASES)
async def test_layout_modes_cover_small_and_maximized_viewports(
    size: tuple[int, int], expected: str
) -> None:
    app = ModuleEditorApp(initial_path=_YUZHAI)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        assert app._layout_mode == expected
        status = app.query_one("#-layout-status")
        assert f"{size[0]}" in str(status.render())
        assert f"{size[1]}" in str(status.render())


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [case[0] for case in _LAYOUT_CASES])
async def test_scene_nested_panels_keep_a_usable_height_on_each_layout(
    size: tuple[int, int],
) -> None:
    app = ModuleEditorApp(initial_path=_YUZHAI)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-scenes"
        await pilot.pause()

        scenes = app._scenes_tab
        nested = scenes.query_one(TabbedContent)
        assert nested.size.height >= _SCENE_NESTED_MIN_HEIGHT
        assert scenes.query_one(".-scene-form-pane").max_scroll_y >= 0

        for pane_id in ("tab-checks", "tab-exits", "tab-presence"):
            nested.active = pane_id
            await pilot.pause()
            pane = scenes.query_one(f"#{pane_id}")
            assert pane.size.height >= _SCENE_PANE_MIN_HEIGHT

        presence = scenes.query_one("#tab-presence .-presence-pane")
        assert presence.size.height >= _SCENE_PANE_MIN_HEIGHT
        assert scenes._npc_presence.size.height > 0
        assert scenes._monster_presence.size.height > 0

        if "narrow" in app._layout_mode:
            master = scenes.query_one(".-master")
            assert str(master.styles.layout) == "<vertical>"
            assert (
                scenes.query_one(".-scene-list-pane").size.height
                >= _SCENE_LIST_MIN_HEIGHT
            )
            assert scenes.query_one(".-scene-form-pane").size.height > 0


@pytest.mark.asyncio
async def test_maximize_updates_the_base_screen_without_collapsing_the_editor() -> None:
    app = ModuleEditorApp(initial_path=_YUZHAI)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        app.action_new()
        await pilot.pause()

        await pilot.resize_terminal(240, 80)
        await pilot.pause()
        await pilot.pause()

        assert app._layout_mode == "wide"
        assert app._base_screen is not None
        assert "wide" in app._base_screen.classes

        # 最大化期间即使有弹窗，尺寸状态也必须写回主 Screen；关闭弹窗后
        # 工具栏和当前 Tab 不能只剩下一个居中的按钮。
        app.pop_screen()
        await pilot.pause()
        toolbar = app.query_one("#editor-toolbar")
        assert toolbar.size.width >= _MAXIMIZED_TOOLBAR_MIN_WIDTH
        assert app._module_tab.size.width >= _MAXIMIZED_TAB_MIN_WIDTH
        assert app._module_tab.size.height >= _MAXIMIZED_TAB_MIN_HEIGHT
        assert all(
            button.size.width > 0
            for button in app.query("#editor-toolbar Button")
        )


@pytest.mark.asyncio
async def test_timeline_expands_to_actual_maximized_control_width() -> None:
    app = ModuleEditorApp(initial_path=_YUZHAI)
    async with app.run_test(size=(240, 80)) as pilot:
        app.query_one(TabbedContent).active = "tab-npcs"
        await pilot.pause()
        app._npcs_tab.query_one(TabbedContent).active = "tab-npc-schedule"
        await pilot.pause()
        coverage = app._npcs_tab._coverage
        assert coverage._timeline_width() == coverage.content_size.width
        assert coverage._timeline_width() > _BASELINE_TIMELINE_WIDTH


@pytest.mark.asyncio
async def test_reference_controls_filter_keep_custom_values_and_write_back() -> None:
    app = ModuleEditorApp(initial_path=_YUZHAI)
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        start_scene = app._module_tab._start_scene
        start_scene.set_options(
            build_reference_options(app.draft.data, "scene"), "future"
        )
        assert "future" in _option_ids(start_scene.options_view)
        assert "未找到" in str(start_scene.query_one(".-foreign").render())

        start_scene.input.value = "living"
        await pilot.pause()
        assert "living_room" in _option_ids(start_scene.options_view)

        app.draft.data["start_scene"] = "porch"
        rename_entity(app.draft.data, "scene", "porch", "porch_renamed")
        app.refresh_all()
        await pilot.pause()
        assert "porch_renamed" in _option_ids(start_scene.options_view)

        scenes = app._scenes_tab
        scenes._scene_idx = 1
        scenes._fill_presence()
        npc_presence = scenes._npc_presence
        npc_presence.set_items(["future_npc"])
        npc_presence.set_reference_options(
            build_reference_options(app.draft.data, "npc")
        )
        suggestions = npc_presence.suggestions_view
        assert suggestions is not None
        assert "future_npc" in _option_ids(suggestions)

        npc_presence._toggle_reference("neighbor")
        await pilot.pause()
        assert "neighbor" in app.draft.data["scenes"][1]["npcs"]

        npcs = app._npcs_tab
        npcs._npc_idx = 0
        npcs._fill_npc_form()
        social = npcs._social
        social._requires_facts.set_items(["future_fact"])
        social._requires_facts.set_reference_options(
            build_reference_options(app.draft.data, "fact", context=npcs._current_npc())
        )
        fact_suggestions = social._requires_facts.suggestions_view
        assert fact_suggestions is not None
        assert "future_fact" in _option_ids(fact_suggestions)


@pytest.mark.asyncio
async def test_condition_picker_and_global_search_navigate_nested_entries() -> None:
    app = ModuleEditorApp(initial_path=_YUZHAI)
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        tokens = build_condition_tokens(app.draft.data)
        app.push_screen(TokenPicker(tokens))
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, TokenPicker)
        picker.query_one(Input).value = "monster_dead"
        await pilot.pause()
        prompts = [
            str(option.prompt) for option in picker.query_one(OptionList).options
        ]
        assert prompts and all("monster_dead" in prompt for prompt in prompts)
        picker.action_cancel()
        await pilot.pause()

        app.push_screen(SearchScreen(app.draft.data))
        await pilot.pause()
        search = app.screen
        assert isinstance(search, SearchScreen)
        search.query_one(Input).value = "monster_dead:ghoul"
        await pilot.pause()
        assert search._results
        result = next(item for item in search._results if item.tab_id == "tab-npcs")
        app.pop_screen()
        app._search_result(result)
        await pilot.pause()
        assert app.query_one(TabbedContent).active == "tab-npcs"
        assert app._npcs_tab._npc_idx == 0
        assert app.query_one("#tab-npcs").query_one(TabbedContent).active == (
            "tab-npc-schedule"
        )
        assert app._npcs_tab._entry_idx == 1


@pytest.mark.asyncio
async def test_nested_duplicate_is_independent_and_selects_new_item() -> None:
    app = ModuleEditorApp(initial_path=_YUZHAI)
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-scenes"
        await pilot.pause()
        scenes = app._scenes_tab
        scenes.query_one(TabbedContent).active = "tab-checks"
        scenes._scene_idx = 1
        scenes._fill_check_list(0)
        original = copy.deepcopy(scenes._current_check())
        assert original is not None
        assert scenes.duplicate_current() is True
        copied = scenes._current_check()
        assert copied is not None and copied["id"] != original["id"]
        copied.setdefault("triggers", []).append("independent")
        assert "independent" not in original.get("triggers", [])

        app.query_one(TabbedContent).active = "tab-npcs"
        await pilot.pause()
        npcs = app._npcs_tab
        npcs._npc_idx = 0
        npcs._fill_npc_form()
        npcs.query_one(TabbedContent).active = "tab-npc-social"
        await pilot.pause()
        social = npcs._social
        social._node_idx = 0
        social._strategy_idx = None
        social._fill_nodes(0)
        original_node = copy.deepcopy(social._current_node())
        assert original_node is not None
        assert social.duplicate_node() is True
        copied_node = social._current_node()
        assert copied_node is not None and copied_node["id"] != original_node["id"]
        copied_node["strategies"][0]["name"] = "only copied"
        assert original_node["strategies"][0].get("name") != "only copied"
