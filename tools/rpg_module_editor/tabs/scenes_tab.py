"""「场景」页：场景列表 + 场景表单 + 检定点 / 出口 / 在场成员子编辑。"""

from __future__ import annotations

from typing import Any, Optional

from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    Label,
    OptionList,
    SelectionList,
    TabbedContent,
    TabPane,
)
from textual.widgets.option_list import Option

from ..schema_loader import SKILLS  # noqa: TID252
from ..state import (  # noqa: TID252
    build_condition_tokens,
    entity_label,
    generate_unique_id,
    get_list,
    new_check_dict,
    new_exit_dict,
    new_scene_dict,
    rename_entity,
)
from ..validate import check_condition  # noqa: TID252
from ..widgets import (  # noqa: TID252
    ConditionInput,
    ConfirmScreen,
    FieldChanged,
    IdInput,
    IntInput,
    LabeledInput,
    LabeledSelect,
    LabeledSwitch,
    LabeledTextArea,
    StrListEditor,
)
from . import EditorTab, move_item

_SKILL_OPTIONS = [("SAN 理智检定", "san")] + [
    (f"{skill.name}（{skill.key}）", skill.key) for skill in SKILLS
]
_DIFFICULTY_OPTIONS = [
    ("常规 regular", "regular"),
    ("困难 hard（技能值 ×½）", "hard"),
    ("极难 extreme（技能值 ×⅕）", "extreme"),
]
_CHECK_MODE_OPTIONS = [
    ("个人 individual", "individual"),
    ("团队 team", "team"),
]


def _int_text(value: Any) -> str:
    return str(value) if isinstance(value, int) else ""


def _str_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


class CheckForm(VerticalScroll):
    """检定点表单（键前缀 check.*，由场景页路由写回）。"""

    def __init__(self) -> None:
        super().__init__()
        self._id = IdInput("检定点 id", "check.id", badge="跨场景全局唯一")
        self._skill = LabeledSelect("技能 skill", "check.skill", _SKILL_OPTIONS)
        self._difficulty = LabeledSelect(
            "难度 difficulty", "check.difficulty", _DIFFICULTY_OPTIONS
        )
        self._mode = LabeledSelect(
            "结算方式 mode",
            "check.mode",
            _CHECK_MODE_OPTIONS,
            allow_blank=False,
            badge="团队检定会聚合当前场景玩家的成功人数",
        )
        self._required_successes = IntInput(
            "团队所需成功人数 required_successes",
            "check.required_successes",
            hint="仅 mode=team 可填；留空按参与者过半计算",
        )
        self._priority = IntInput(
            "优先级 priority",
            "check.priority",
            hint="多检定点同时命中时高者优先；SAN 检点建议 1",
        )
        self._once = LabeledSwitch(
            "整局至多触发一次 once", "check.once", badge="线索/伤害/SAN 检点务必开启"
        )
        self._san_loss = LabeledInput(
            "SAN 损失 san_loss",
            "check.san_loss",
            hint="「成功侧/失败侧」骰表达式，如 1/1d6；skill 为 san 时必填",
        )
        self._damage = LabeledInput(
            "失败伤害 damage_on_fail",
            "check.damage_on_fail",
            hint="骰表达式，如 1d3；可留空",
        )
        self._time_cost = IntInput(
            "耗时覆写 time_cost（分钟）",
            "check.time_cost",
            hint="留空用引擎 check 默认",
        )
        self._clue = LabeledSelect("成功奖励线索 clue", "check.clue", [])
        self._success = LabeledTextArea(
            "成功文案 success_text",
            "check.success_text",
            badge="仅结算时逐字播报，KP 不可见",
        )
        self._failure = LabeledTextArea(
            "失败文案 failure_text",
            "check.failure_text",
            badge="仅结算时逐字播报，KP 不可见",
        )
        self._triggers = StrListEditor(
            "触发关键词 triggers",
            "check.triggers",
            hint="子串匹配、大小写不敏感；每句发言至多触发一个检定点",
        )

    def compose(self) -> Any:
        yield self._id
        yield self._skill
        yield self._difficulty
        yield self._mode
        yield self._required_successes
        yield self._priority
        yield self._once
        yield self._san_loss
        yield self._damage
        yield self._time_cost
        yield self._clue
        yield self._success
        yield self._failure
        yield self._triggers

    def fill(self, check: dict[str, Any], clue_options: list[tuple[str, str]]) -> None:
        self._id.set_value(_str_text(check.get("id")))
        self._skill.set_value(check.get("skill"))
        self._difficulty.set_value(check.get("difficulty", "regular"))
        self._mode.set_value(check.get("mode", "individual"))
        self._required_successes.set_value(_int_text(check.get("required_successes")))
        self._priority.set_value(_int_text(check.get("priority", 0)))
        self._once.set_value(bool(check.get("once", False)))
        self._san_loss.set_value(_str_text(check.get("san_loss")))
        self._damage.set_value(_str_text(check.get("damage_on_fail")))
        self._time_cost.set_value(_int_text(check.get("time_cost")))
        self._clue.set_options(clue_options, check.get("clue"))
        self._success.set_value(_str_text(check.get("success_text")))
        self._failure.set_value(_str_text(check.get("failure_text")))
        triggers = check.get("triggers")
        self._triggers.set_items(
            [str(t) for t in triggers] if isinstance(triggers, list) else []
        )


class ExitForm(VerticalScroll):
    """出口表单（键前缀 exit.*）。"""

    def __init__(self, tokens_provider: Any) -> None:
        super().__init__()
        self._to_scene = LabeledSelect("目标场景 to_scene", "exit.to_scene", [])
        self._condition = ConditionInput(
            "通行条件 condition",
            "exit.condition",
            validator=lambda cond: check_condition(cond, self._data_provider()),
            tokens_provider=tokens_provider,
            badge="KP 只见可通行/不可通行布尔",
        )
        self._auto = LabeledSwitch(
            "条件满足自动切景 auto", "exit.auto", badge="只用于无条件序幕走廊"
        )
        self._narration = LabeledInput(
            "通行播报 narration",
            "exit.narration",
            hint="上锁出口建议写开锁瞬间的描述；不出现数字",
        )
        self._time_cost = IntInput(
            "耗时覆写 time_cost（分钟）", "exit.time_cost", hint="留空用引擎 move 默认"
        )
        self._keywords = StrListEditor(
            "/前往 同义词 keywords",
            "exit.keywords",
            hint="目标场景名永远可匹配，这里只补别名",
        )
        self._data: dict[str, Any] = {}

    def _data_provider(self) -> dict[str, Any]:
        return self._data

    def compose(self) -> Any:
        yield self._to_scene
        yield self._condition
        yield self._auto
        yield self._narration
        yield self._time_cost
        yield self._keywords

    def fill(
        self,
        exit_data: dict[str, Any],
        scene_options: list[tuple[str, str]],
        data: dict[str, Any],
    ) -> None:
        self._data = data
        self._to_scene.set_options(scene_options, exit_data.get("to_scene"))
        self._condition.set_value(_str_text(exit_data.get("condition")))
        self._auto.set_value(bool(exit_data.get("auto", False)))
        self._narration.set_value(_str_text(exit_data.get("narration")))
        self._time_cost.set_value(_int_text(exit_data.get("time_cost")))
        keywords = exit_data.get("keywords")
        self._keywords.set_items(
            [str(k) for k in keywords] if isinstance(keywords, list) else []
        )


class ScenesTab(EditorTab):
    """场景页：左侧场景列表，右侧表单 + 检定点/出口/在场成员。"""

    DEFAULT_CSS = """
    ScenesTab { height: 1fr; }
    ScenesTab Horizontal.-master { height: 1fr; }
    ScenesTab Vertical.-list-pane { width: 34; }
    ScenesTab Vertical.-form-pane { width: 1fr; }
    ScenesTab Horizontal.-row { height: 3; }
    ScenesTab Button { margin-right: 1; }
    ScenesTab OptionList.-main-list { height: 1fr; }
    ScenesTab OptionList.-sub-list { width: 34; height: 1fr; }
    ScenesTab Horizontal.-subedit { height: 1fr; }
    ScenesTab Label.-note { height: auto; color: $warning; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._scene_list = OptionList(classes="-main-list")
        self._scene_id = IdInput("场景 id", "scene.id")
        self._scene_name = LabeledInput(
            "场景名称", "scene.name", hint="进场景标题、KP 提示词与 /前往 匹配"
        )
        self._scene_narration = LabeledTextArea(
            "进入播报 narration",
            "scene.narration",
            tall=True,
            badge="每次进入时播报；不要出现数字",
        )
        self._scene_idle = LabeledInput(
            "兜底旁白 idle_narration",
            "scene.idle_narration",
            hint="AI 关闭/失败且无关键词触发时的确定性兜底；留空用通用罐头文案",
        )
        self._check_list = OptionList(classes="-sub-list")
        self._check_form = CheckForm()
        self._exit_list = OptionList(classes="-sub-list")
        self._exit_form = ExitForm(tokens_provider=self._tokens)
        self._npc_presence = SelectionList()
        self._monster_presence = SelectionList()
        self._scene_idx: Optional[int] = None
        self._check_idx: Optional[int] = None
        self._exit_idx: Optional[int] = None

    # ── 布局 ──────────────────────────────────────────────

    def compose(self) -> Any:
        with Horizontal(classes="-master"):
            with Vertical(classes="-list-pane"):
                yield Label("[b]场景列表[/b]", markup=True)
                yield self._scene_list
                with Horizontal(classes="-row"):
                    yield Button("新增", variant="primary", classes="-scene-add")
                    yield Button("删除", variant="error", classes="-scene-del")
                    yield Button("上移", classes="-scene-up")
                    yield Button("下移", classes="-scene-down")
            with Vertical(classes="-form-pane"):
                yield self._scene_id
                yield self._scene_name
                yield self._scene_narration
                yield self._scene_idle
                with TabbedContent(initial="tab-checks"):
                    with (
                        TabPane("检定点", id="tab-checks"),
                        Horizontal(classes="-subedit"),
                    ):
                        with Vertical(classes="-list-pane"):
                            yield self._check_list
                            with Horizontal(classes="-row"):
                                yield Button(
                                    "新增", variant="primary", classes="-check-add"
                                )
                                yield Button(
                                    "删除", variant="error", classes="-check-del"
                                )
                                yield Button("上移", classes="-check-up")
                                yield Button("下移", classes="-check-down")
                        yield self._check_form
                    with (
                        TabPane("出口", id="tab-exits"),
                        Horizontal(classes="-subedit"),
                    ):
                        with Vertical(classes="-list-pane"):
                            yield self._exit_list
                            with Horizontal(classes="-row"):
                                yield Button(
                                    "新增", variant="primary", classes="-exit-add"
                                )
                                yield Button(
                                    "删除", variant="error", classes="-exit-del"
                                )
                                yield Button("上移", classes="-exit-up")
                                yield Button("下移", classes="-exit-down")
                        yield self._exit_form
                    with TabPane("在场成员", id="tab-presence"), VerticalScroll():
                        yield Label(
                            "[b]NPC[/b]（有行程表的 NPC 忽略此列表，见 NPC 页）",
                            markup=True,
                        )
                        yield self._npc_presence
                        yield Label(
                            "[b]怪物[/b]（进入场景时按怪物 HP 初始化）", markup=True
                        )
                        yield self._monster_presence

    # ── 数据访问 ──────────────────────────────────────────

    def _scenes(self) -> list[Any]:
        return get_list(self.editor.draft.data, "scenes")

    def _current_scene(self) -> Optional[dict[str, Any]]:
        scenes = self._scenes()
        if self._scene_idx is None or not (0 <= self._scene_idx < len(scenes)):
            return None
        scene = scenes[self._scene_idx]
        return scene if isinstance(scene, dict) else None

    def _current_check(self) -> Optional[dict[str, Any]]:
        scene = self._current_scene()
        if scene is None:
            return None
        checks = get_list(scene, "checks")
        if self._check_idx is None or not (0 <= self._check_idx < len(checks)):
            return None
        check = checks[self._check_idx]
        return check if isinstance(check, dict) else None

    def _current_exit(self) -> Optional[dict[str, Any]]:
        scene = self._current_scene()
        if scene is None:
            return None
        exits = get_list(scene, "exits")
        if self._exit_idx is None or not (0 <= self._exit_idx < len(exits)):
            return None
        exit_ = exits[self._exit_idx]
        return exit_ if isinstance(exit_, dict) else None

    def _scene_options(self) -> list[tuple[str, str]]:
        return [
            (entity_label(s), str(s.get("id", "")))
            for s in self._scenes()
            if isinstance(s, dict)
        ]

    def _clue_options(self) -> list[tuple[str, str]]:
        return [
            (entity_label(c), str(c.get("id", "")))
            for c in get_list(self.editor.draft.data, "clues")
            if isinstance(c, dict)
        ]

    def _tokens(self) -> list[tuple[str, str]]:
        return build_condition_tokens(self.editor.draft.data)

    # ── 重填 ──────────────────────────────────────────────

    def refresh_tab(self, data: dict[str, Any]) -> None:
        scenes = get_list(data, "scenes")
        current_id = None
        scene = self._current_scene()
        if scene is not None:
            current_id = scene.get("id")

        self._scene_list.clear_options()
        for i, item in enumerate(scenes):
            if isinstance(item, dict):
                self._scene_list.add_option(Option(entity_label(item), id=str(i)))

        new_idx: Optional[int] = None
        if current_id is not None:
            new_idx = next(
                (
                    i
                    for i, s in enumerate(scenes)
                    if isinstance(s, dict) and s.get("id") == current_id
                ),
                None,
            )
        if new_idx is None and scenes:
            new_idx = 0
        self._scene_idx = new_idx
        if new_idx is not None:
            self._scene_list.highlighted = new_idx
        self._fill_scene_form()
        self._fill_check_list()
        self._fill_exit_list()
        self._fill_presence()

    def _fill_scene_form(self) -> None:
        scene = self._current_scene()
        if scene is None:
            self._scene_id.set_value("")
            self._scene_name.set_value("")
            self._scene_narration.set_value("")
            self._scene_idle.set_value("")
            return
        self._scene_id.set_value(_str_text(scene.get("id")))
        self._scene_name.set_value(_str_text(scene.get("name")))
        self._scene_narration.set_value(_str_text(scene.get("narration")))
        self._scene_idle.set_value(_str_text(scene.get("idle_narration")))

    def _fill_check_list(self, selected_idx: Optional[int] = None) -> None:
        scene = self._current_scene()
        checks = get_list(scene, "checks") if scene else []
        if selected_idx is None:
            selected_idx = self._check_idx
        self._check_list.clear_options()
        for i, check in enumerate(checks):
            if isinstance(check, dict):
                label = str(check.get("id", f"检定点 #{i + 1}"))
                if check.get("once"):
                    label += " · once"
                self._check_list.add_option(Option(label, id=str(i)))
        if checks:
            self._check_idx = min(max(selected_idx or 0, 0), len(checks) - 1)
            self._check_list.highlighted = self._check_idx
        else:
            self._check_idx = None
        self._fill_check_form()

    def _fill_check_form(self) -> None:
        check = self._current_check()
        if check is None:
            return
        self._check_form.fill(check, self._clue_options())

    def _fill_exit_list(self, selected_idx: Optional[int] = None) -> None:
        scene = self._current_scene()
        exits = get_list(scene, "exits") if scene else []
        if selected_idx is None:
            selected_idx = self._exit_idx
        self._exit_list.clear_options()
        for i, exit_ in enumerate(exits):
            if isinstance(exit_, dict):
                target = exit_.get("to_scene", "?")
                mark = " 🔒" if exit_.get("condition") else ""
                self._exit_list.add_option(
                    Option(f"#{i + 1} → {target}{mark}", id=str(i))
                )
        if exits:
            self._exit_idx = min(max(selected_idx or 0, 0), len(exits) - 1)
            self._exit_list.highlighted = self._exit_idx
        else:
            self._exit_idx = None
        self._fill_exit_form()

    def _fill_exit_form(self) -> None:
        exit_ = self._current_exit()
        if exit_ is None:
            return
        self._exit_form.fill(exit_, self._scene_options(), self.editor.draft.data)

    def _fill_presence(self) -> None:
        data = self.editor.draft.data
        scene = self._current_scene()
        scene_npcs = get_list(scene, "npcs") if scene else []
        scene_monsters = get_list(scene, "monsters") if scene else []
        npc_options = [
            (entity_label(n), str(n.get("id", "")), n.get("id") in scene_npcs)
            for n in get_list(data, "npcs")
            if isinstance(n, dict)
        ]
        monster_options = [
            (entity_label(m), str(m.get("id", "")), m.get("id") in scene_monsters)
            for m in get_list(data, "monsters")
            if isinstance(m, dict)
        ]
        # 程序化重建选项不得触发 SelectedChanged 回写
        with self._npc_presence.prevent(SelectionList.SelectedChanged):
            self._npc_presence.clear_options()
            if npc_options:
                self._npc_presence.add_options(npc_options)
        with self._monster_presence.prevent(SelectionList.SelectedChanged):
            self._monster_presence.clear_options()
            if monster_options:
                self._monster_presence.add_options(monster_options)

    # ── 选择切换 ──────────────────────────────────────────

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is None:
            return
        index = int(str(event.option.id))
        if event.control is self._scene_list:
            self._scene_idx = index
            self._fill_scene_form()
            self._fill_check_list()
            self._fill_exit_list()
            self._fill_presence()
        elif event.control is self._check_list:
            self._check_idx = index
            self._fill_check_form()
        elif event.control is self._exit_list:
            self._exit_idx = index
            self._fill_exit_form()

    # ── 按钮：增删改序 ────────────────────────────────────

    def on_button_pressed(  # noqa: C901,PLR0911,PLR0912,PLR0915
        self, event: Button.Pressed
    ) -> None:
        classes = event.button.classes
        data = self.editor.draft.data

        if "-scene-add" in classes:
            new_id = generate_unique_id(
                "new_scene",
                {str(s.get("id", "")) for s in self._scenes() if isinstance(s, dict)},
            )
            scenes = data.get("scenes")
            if not isinstance(scenes, list):
                scenes = []
                data["scenes"] = scenes
            scenes.append(new_scene_dict(new_id))
            self.editor.refresh_all()
        elif "-scene-del" in classes:
            scene = self._current_scene()
            if scene is None:
                return
            self._confirm_delete_scene(scene)
        elif "-scene-up" in classes or "-scene-down" in classes:
            scenes = self._scenes()
            delta = -1 if "-scene-up" in classes else 1
            new_idx = move_item(scenes, self._scene_idx, delta)
            if new_idx is not None:
                self._scene_idx = new_idx
                self.editor.refresh_all()
        elif "-check-add" in classes:
            scene = self._current_scene()
            if scene is None:
                return
            all_ids = [
                str(c.get("id", ""))
                for s in self._scenes()
                if isinstance(s, dict)
                for c in get_list(s, "checks")
                if isinstance(c, dict)
            ]
            check_id = generate_unique_id("new_check", set(all_ids))
            scene.setdefault("checks", []).append(new_check_dict(check_id))
            self._fill_check_list(len(get_list(scene, "checks")) - 1)
            self.editor.on_data_changed()
        elif "-check-del" in classes:
            scene = self._current_scene()
            check = self._current_check()
            if scene is None or check is None or self._check_idx is None:
                return
            del get_list(scene, "checks")[self._check_idx]
            self._fill_check_list(self._check_idx)
            self.editor.on_data_changed()
        elif "-check-up" in classes or "-check-down" in classes:
            scene = self._current_scene()
            if scene is None:
                return
            checks = get_list(scene, "checks")
            delta = -1 if "-check-up" in classes else 1
            new_idx = move_item(checks, self._check_idx, delta)
            if new_idx is not None:
                self._fill_check_list(new_idx)
                self.editor.on_data_changed()
        elif "-exit-add" in classes:
            scene = self._current_scene()
            if scene is None:
                return
            first_scene = next(
                (str(s.get("id", "")) for s in self._scenes() if isinstance(s, dict)),
                "",
            )
            scene.setdefault("exits", []).append(new_exit_dict(first_scene))
            self._fill_exit_list(len(get_list(scene, "exits")) - 1)
            self.editor.on_data_changed()
        elif "-exit-del" in classes:
            scene = self._current_scene()
            if scene is None or self._exit_idx is None:
                return
            exits = get_list(scene, "exits")
            if 0 <= self._exit_idx < len(exits):
                del exits[self._exit_idx]
            self._fill_exit_list(self._exit_idx)
            self.editor.on_data_changed()
        elif "-exit-up" in classes or "-exit-down" in classes:
            scene = self._current_scene()
            if scene is None:
                return
            exits = get_list(scene, "exits")
            delta = -1 if "-exit-up" in classes else 1
            new_idx = move_item(exits, self._exit_idx, delta)
            if new_idx is not None:
                self._fill_exit_list(new_idx)
                self.editor.on_data_changed()

    def _confirm_delete_scene(self, scene: dict[str, Any]) -> None:
        ident = str(scene.get("id", "?"))
        body = (
            f"确定删除场景 {entity_label(scene)} 吗？\n\n"
            f"指向 {ident} 的出口 / 行程 / 条件引用将悬空（校验会报 ERROR），"
            "start_scene 若指向它会自动改为首个场景。"
        )

        def _delete(confirmed: Optional[bool]) -> None:  # noqa: FBT001
            if not confirmed:
                return
            data = self.editor.draft.data
            scenes = self._scenes()
            if scene in scenes:
                scenes.remove(scene)
            if data.get("start_scene") == ident:
                first = next(
                    (str(s.get("id", "")) for s in scenes if isinstance(s, dict)), ""
                )
                data["start_scene"] = first
            self.editor.refresh_all()

        self.app.push_screen(ConfirmScreen("删除场景", body), _delete)

    # ── 字段写回 ──────────────────────────────────────────

    def on_field_changed(self, event: FieldChanged) -> None:
        prefix, _, field = event.key.partition(".")
        value = event.value
        if prefix == "scene":
            self._write_scene_field(field, value)
        elif prefix == "check":
            self._write_check_field(field, value)
        elif prefix == "exit":
            self._write_exit_field(field, value)

    def _write_scene_field(self, field: str, value: Any) -> None:
        scene = self._current_scene()
        if scene is None:
            return
        if field == "id":
            old = str(scene.get("id", ""))
            new = str(value)
            if new == old or not new:
                return
            sites = rename_entity(self.editor.draft.data, "scene", old, new)
            self.editor.refresh_all()
            if len(sites) > 1:
                self.notify(f"已级联更新 {len(sites) - 1} 处引用")
            return
        scene[field] = value
        if field == "name":
            self._update_scene_list_label()
        self.editor.on_data_changed()

    def _update_scene_list_label(self) -> None:
        scene = self._current_scene()
        if scene is None or self._scene_idx is None:
            return
        self._scene_list.replace_option_prompt_at_index(
            self._scene_idx, entity_label(scene)
        )

    def _write_check_field(self, field: str, value: Any) -> None:
        check = self._current_check()
        if check is None:
            return
        if field in ("priority", "time_cost", "required_successes"):
            if (
                field == "required_successes"
                and check.get("mode", "individual") == "individual"
            ):
                check.pop(field, None)
                self._check_form._required_successes.set_value("")
                self.editor.on_data_changed()
                return
            if value is None:
                check.pop(field, None)
            else:
                check[field] = value
        elif field == "mode":
            mode = str(value or "individual")
            check["mode"] = mode
            if mode == "individual":
                check.pop("required_successes", None)
                self._check_form._required_successes.set_value("")
        elif field == "clue":
            if value:
                check["clue"] = value
            else:
                check.pop("clue", None)
        else:
            check[field] = value
            if field == "id":
                self._fill_check_list()
        self.editor.on_data_changed()

    def _write_exit_field(self, field: str, value: Any) -> None:
        exit_ = self._current_exit()
        if exit_ is None:
            return
        if field == "time_cost":
            if value is None:
                exit_.pop(field, None)
            else:
                exit_["time_cost"] = value
        elif field == "condition":
            text = str(value).strip()
            if text:
                exit_["condition"] = text
            else:
                exit_.pop("condition", None)
        else:
            exit_[field] = value
        if field in ("to_scene", "condition"):
            self._fill_exit_list()
        self.editor.on_data_changed()

    # ── 在场成员 ──────────────────────────────────────────

    def on_selection_list_selected_changed(
        self, event: SelectionList.SelectedChanged
    ) -> None:
        scene = self._current_scene()
        if scene is None:
            return
        selected = [str(v) for v in event.selection_list.selected]
        if event.control is self._npc_presence:
            scene["npcs"] = selected
        elif event.control is self._monster_presence:
            scene["monsters"] = selected
        self.editor.on_data_changed()
