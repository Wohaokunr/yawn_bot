"""「怪物」页：怪物列表 + 战斗数值表单。"""

from __future__ import annotations

from typing import Any, Optional

from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Label, OptionList
from textual.widgets.option_list import Option

from ..state import (  # noqa: TID252
    entity_label,
    generate_unique_id,
    get_list,
    new_monster_dict,
    rename_entity,
)
from ..widgets import (  # noqa: TID252
    ConfirmScreen,
    FieldChanged,
    IdInput,
    IntInput,
    LabeledInput,
    LabeledSelect,
    LabeledTextArea,
)
from . import EditorTab, move_item


def _str_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int_text(value: Any) -> str:
    return str(value) if isinstance(value, int) else ""


class MonstersTab(EditorTab):
    """怪物页。"""

    DEFAULT_CSS = """
    MonstersTab { height: 1fr; }
    MonstersTab Horizontal.-master { height: 1fr; }
    MonstersTab Vertical.-list-pane { width: 34; }
    MonstersTab Vertical.-form-pane { width: 1fr; }
    MonstersTab Horizontal.-row { height: 3; }
    MonstersTab Button { margin-right: 1; }
    MonstersTab OptionList.-main-list { height: 1fr; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._monster_list = OptionList(classes="-main-list")
        self._id = IdInput("怪物 id", "monster.id")
        self._name = LabeledInput(
            "名称 name", "monster.name", hint="/攻击 按名称子串匹配，怪物优先于 NPC"
        )
        self._hp = IntInput("生命值 hp", "monster.hp", hint="进入其所在场景时初始化")
        self._attack_skill = IntInput(
            "命中率 attack_skill（d100）", "monster.attack_skill"
        )
        self._attack_name = LabeledInput(
            "攻击描述名 attack_name", "monster.attack_name"
        )
        self._damage = LabeledInput(
            "伤害骰 damage", "monster.damage", hint="如 1d6；1 ≤ N ≤ 100，1 ≤ M ≤ 1000"
        )
        self._dodge = IntInput(
            "闪避对抗值 dodge", "monster.dodge", hint="留空 = 不会闪避"
        )
        self._on_death_clue = LabeledSelect(
            "死亡线索 on_death_clue",
            "monster.on_death_clue",
            [],
            badge="死亡后才可经 grant_clue 授予",
        )
        self._on_death_text = LabeledTextArea(
            "死亡播报 on_death_text", "monster.on_death_text"
        )
        self._monster_idx: Optional[int] = None

    def compose(self) -> Any:
        with Horizontal(classes="-master"):
            with Vertical(classes="-list-pane"):
                yield Label("[b]怪物列表[/b]", markup=True)
                yield self._monster_list
                with Horizontal(classes="-row"):
                    yield Button("新增", variant="primary", classes="-monster-add")
                    yield Button("删除", variant="error", classes="-monster-del")
                    yield Button("上移", classes="-monster-up")
                    yield Button("下移", classes="-monster-down")
            with VerticalScroll(classes="-form-pane"):
                yield Label(
                    "[dim]战斗数值由引擎结算，KP 永不进提示词[/dim]", markup=True
                )
                yield self._id
                yield self._name
                yield self._hp
                yield self._attack_skill
                yield self._attack_name
                yield self._damage
                yield self._dodge
                yield self._on_death_clue
                yield self._on_death_text

    def _monsters(self) -> list[Any]:
        return get_list(self.editor.draft.data, "monsters")

    def _current_monster(self) -> Optional[dict[str, Any]]:
        monsters = self._monsters()
        if self._monster_idx is None or not (0 <= self._monster_idx < len(monsters)):
            return None
        monster = monsters[self._monster_idx]
        return monster if isinstance(monster, dict) else None

    def _clue_options(self) -> list[tuple[str, str]]:
        return [
            (entity_label(c), str(c.get("id", "")))
            for c in get_list(self.editor.draft.data, "clues")
            if isinstance(c, dict)
        ]

    def refresh_tab(self, data: dict[str, Any]) -> None:
        monsters = get_list(data, "monsters")
        current_id = None
        monster = self._current_monster()
        if monster is not None:
            current_id = monster.get("id")
        self._monster_list.clear_options()
        for i, item in enumerate(monsters):
            if isinstance(item, dict):
                self._monster_list.add_option(Option(entity_label(item), id=str(i)))
        new_idx = None
        if current_id is not None:
            new_idx = next(
                (
                    i
                    for i, m in enumerate(monsters)
                    if isinstance(m, dict) and m.get("id") == current_id
                ),
                None,
            )
        if new_idx is None and monsters:
            new_idx = 0
        self._monster_idx = new_idx
        if new_idx is not None:
            self._monster_list.highlighted = new_idx
        self._fill_form()

    def _fill_form(self) -> None:
        monster = self._current_monster()
        if monster is None:
            return
        self._id.set_value(_str_text(monster.get("id")))
        self._name.set_value(_str_text(monster.get("name")))
        self._hp.set_value(_int_text(monster.get("hp")))
        self._attack_skill.set_value(_int_text(monster.get("attack_skill")))
        self._attack_name.set_value(_str_text(monster.get("attack_name", "攻击")))
        self._damage.set_value(_str_text(monster.get("damage")))
        self._dodge.set_value(_int_text(monster.get("dodge")))
        self._on_death_clue.set_options(
            self._clue_options(), monster.get("on_death_clue")
        )
        self._on_death_text.set_value(_str_text(monster.get("on_death_text")))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.control is self._monster_list and event.option.id is not None:
            self._monster_idx = int(str(event.option.id))
            self._fill_form()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        classes = event.button.classes
        data = self.editor.draft.data

        if "-monster-add" in classes:
            new_id = generate_unique_id(
                "new_monster",
                {str(m.get("id", "")) for m in self._monsters() if isinstance(m, dict)},
            )
            monsters = data.get("monsters")
            if not isinstance(monsters, list):
                monsters = []
                data["monsters"] = monsters
            monsters.append(new_monster_dict(new_id))
            self.editor.refresh_all()
        elif "-monster-del" in classes:
            monster = self._current_monster()
            if monster is None:
                return
            self._confirm_delete(monster)
        elif "-monster-up" in classes or "-monster-down" in classes:
            monsters = self._monsters()
            delta = -1 if "-monster-up" in classes else 1
            new_idx = move_item(monsters, self._monster_idx, delta)
            if new_idx is not None:
                self._monster_idx = new_idx
                self.editor.refresh_all()

    def _confirm_delete(self, monster: dict[str, Any]) -> None:
        ident = str(monster.get("id", "?"))

        def _delete(confirmed: Optional[bool]) -> None:  # noqa: FBT001
            if not confirmed:
                return
            monsters = self._monsters()
            if monster in monsters:
                monsters.remove(monster)
            for scene in get_list(self.editor.draft.data, "scenes"):
                members = scene.get("monsters") if isinstance(scene, dict) else None
                if isinstance(members, list) and ident in members:
                    members.remove(ident)
            self.editor.refresh_all()

        self.app.push_screen(
            ConfirmScreen(
                "删除怪物",
                f"确定删除怪物 {entity_label(monster)} 吗？"
                "scene.monsters 中的引用会一并移除。",
            ),
            _delete,
        )

    def on_field_changed(  # noqa: C901,PLR0912
        self, event: FieldChanged
    ) -> None:
        prefix, _, field = event.key.partition(".")
        if prefix != "monster":
            return
        monster = self._current_monster()
        if monster is None:
            return
        value = event.value
        if field == "id":
            old = str(monster.get("id", ""))
            new = str(value)
            if new == old or not new:
                return
            sites = rename_entity(self.editor.draft.data, "monster", old, new)
            self.editor.refresh_all()
            if len(sites) > 1:
                self.notify(f"已级联更新 {len(sites) - 1} 处引用")
            return
        if field in ("hp", "attack_skill"):
            if value is not None:
                monster[field] = value
        elif field == "dodge":
            if value is None:
                monster.pop("dodge", None)
            else:
                monster["dodge"] = value
        elif field == "on_death_clue":
            if value:
                monster["on_death_clue"] = value
            else:
                monster.pop("on_death_clue", None)
        else:
            monster[field] = value
            if field == "name":
                self._update_list_label()
        self.editor.on_data_changed()

    def _update_list_label(self) -> None:
        monster = self._current_monster()
        if monster is None or self._monster_idx is None:
            return
        self._monster_list.replace_option_prompt_at_index(
            self._monster_idx, entity_label(monster)
        )
