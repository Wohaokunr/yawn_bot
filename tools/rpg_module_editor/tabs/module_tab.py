"""「模组」页：顶层元信息 + opening + 时钟配置。"""

from __future__ import annotations

import contextlib
from typing import Any

from textual.containers import Horizontal, VerticalScroll

from ..state import build_reference_options_for_field  # noqa: TID252
from ..widgets import (  # noqa: TID252
    FieldChanged,
    IdInput,
    IntInput,
    LabeledInput,
    LabeledSelect,
    LabeledSwitch,
    LabeledTextArea,
    TimeInput,
)
from . import EditorTab

_COST_DEFAULTS = {"say": 5, "talk": 10, "check": 10, "move": 10, "attack": 5, "wait": 0}


class ModuleTab(EditorTab):
    """模组级字段编辑。"""

    def __init__(self) -> None:
        super().__init__()
        self._id = IdInput("模组 id", "id", badge="ASCII snake_case，跨文件唯一")
        self._name = LabeledInput("中文名称", "name")
        self._description = LabeledInput(
            "一句话简介", "description", badge="列表面板展示"
        )
        self._difficulty = LabeledInput(
            "难度", "difficulty", hint="自由文本，仅列表面板展示"
        )
        self._min_players = IntInput("最少人数", "min_players")
        self._max_players = IntInput("最多人数（报名上限）", "max_players")
        self._start_scene = LabeledSelect("起始场景 start_scene", "start_scene", [])
        self._opening = LabeledTextArea(
            "开局播报 opening",
            "opening",
            tall=True,
            badge="KP概览取前 150 字作前提",
            counter_limit=150,
            counter_note="进 KP 概览",
        )
        self._time_start = TimeInput("开局时刻 time.start", "time.start")
        self._cost_inputs: dict[str, IntInput] = {
            key: IntInput(f"{key}（默认 {default}）", f"costs.{key}")
            for key, default in _COST_DEFAULTS.items()
        }
        self._generic_endings = LabeledSwitch(
            "启用通用结局安全网 generic_endings",
            "generic_endings",
            badge="谋杀/纵火/全员倒地等极端行为兜底",
        )

    def compose(self) -> Any:
        with VerticalScroll():
            yield self._id
            yield self._name
            yield self._description
            yield self._difficulty
            with Horizontal():
                yield self._min_players
                yield self._max_players
            yield self._start_scene
            yield self._opening
            yield self._time_start
            for key in _COST_DEFAULTS:
                yield self._cost_inputs[key]
            yield self._generic_endings

    def locate_path(self, path: tuple[Any, ...]) -> None:
        if not path:
            return
        controls = {
            "id": self._id,
            "name": self._name,
            "description": self._description,
            "difficulty": self._difficulty,
            "min_players": self._min_players,
            "max_players": self._max_players,
            "start_scene": self._start_scene,
            "opening": self._opening,
            "generic_endings": self._generic_endings,
        }
        control = controls.get(str(path[0]))
        if control is None:
            return
        with contextlib.suppress(Exception):  # pragma: no cover - jump is best effort
            target = getattr(control, "input", None)
            if target is None:
                target = getattr(control, "area", None)
            if target is None:
                target = getattr(control, "switch", control)
            target.focus()

    def refresh_tab(self, data: dict[str, Any]) -> None:
        def text(key: str) -> str:
            value = data.get(key, "")
            return value if isinstance(value, str) else ""

        def integer(key: str) -> str:
            value = data.get(key)
            return str(value) if isinstance(value, int) else ""

        self._id.set_value(text("id"))
        self._name.set_value(text("name"))
        self._description.set_value(text("description"))
        self._difficulty.set_value(text("difficulty"))
        self._min_players.set_value(integer("min_players"))
        self._max_players.set_value(integer("max_players"))

        scene_options = build_reference_options_for_field(data, "start_scene")
        start_scene = data.get("start_scene")
        self._start_scene.set_options(
            scene_options, start_scene if isinstance(start_scene, str) else None
        )

        self._opening.set_value(text("opening"))
        time_block = data.get("time")
        time_block = time_block if isinstance(time_block, dict) else {}
        start = time_block.get("start", "")
        self._time_start.set_value(start if isinstance(start, str) else "")
        costs = time_block.get("costs")
        costs = costs if isinstance(costs, dict) else {}
        for key, widget in self._cost_inputs.items():
            value = costs.get(key)
            widget.set_value(str(value) if isinstance(value, int) else "")
        generic = data.get("generic_endings", True)
        self._generic_endings.set_value(bool(generic))

    def on_field_changed(self, event: FieldChanged) -> None:
        data = self.editor.draft.data
        key, value = event.key, event.value
        if key.startswith("costs."):
            cost_key = key.split(".", 1)[1]
            time_block = data.setdefault("time", {})
            if not isinstance(time_block, dict):
                return
            costs = time_block.setdefault("costs", {})
            if not isinstance(costs, dict):
                return
            if value is None:
                costs.pop(cost_key, None)
            else:
                costs[cost_key] = value
        elif key == "time.start":
            time_block = data.setdefault("time", {})
            if isinstance(time_block, dict):
                time_block["start"] = value
        elif key in ("min_players", "max_players"):
            if value is not None:
                data[key] = value
        else:
            data[key] = value
        self.editor.on_data_changed()
