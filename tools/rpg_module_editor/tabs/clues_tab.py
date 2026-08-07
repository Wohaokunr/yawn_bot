"""「线索」页：线索列表 + 表单 + 引用者地图。"""

from __future__ import annotations

from typing import Any, Optional

from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Label, OptionList
from textual.widgets.option_list import Option

from ..state import (  # noqa: TID252
    clue_referrers,
    entity_label,
    generate_unique_id,
    get_list,
    new_clue_dict,
    rename_entity,
)
from ..widgets import (  # noqa: TID252
    ConfirmScreen,
    FieldChanged,
    IdInput,
    LabeledInput,
    LabeledTextArea,
)
from . import EditorTab


def _str_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


class CluesTab(EditorTab):
    """线索页。"""

    DEFAULT_CSS = """
    CluesTab { height: 1fr; }
    CluesTab Horizontal.-master { height: 1fr; }
    CluesTab Vertical.-list-pane { width: 34; }
    CluesTab Vertical.-form-pane { width: 1fr; }
    CluesTab Horizontal.-row { height: 3; }
    CluesTab Button { margin-right: 1; }
    CluesTab OptionList.-main-list { height: 1fr; }
    CluesTab Label.-refs { height: auto; color: $text-muted; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._clue_list = OptionList(classes="-main-list")
        self._id = IdInput("线索 id", "clue.id")
        self._name = LabeledInput(
            "名称 name",
            "clue.name",
            badge="进 KP 场景块「[已发现线索]」（只有名字+id）",
        )
        self._text = LabeledTextArea(
            "发现播报 text",
            "clue.text",
            tall=True,
            badge="仅发现时播报一次，永不进任何提示词",
        )
        self._refs = Label("", classes="-refs")
        self._clue_idx: Optional[int] = None

    def compose(self) -> Any:
        with Horizontal(classes="-master"):
            with Vertical(classes="-list-pane"):
                yield Label("[b]线索列表[/b]", markup=True)
                yield self._clue_list
                with Horizontal(classes="-row"):
                    yield Button("新增", variant="primary", classes="-clue-add")
                    yield Button("删除", variant="error", classes="-clue-del")
                    yield Button("上移", classes="-clue-up")
                    yield Button("下移", classes="-clue-down")
            with VerticalScroll(classes="-form-pane"):
                yield self._id
                yield self._name
                yield self._text
                yield self._refs

    def _clues(self) -> list[Any]:
        return get_list(self.editor.draft.data, "clues")

    def _current_clue(self) -> Optional[dict[str, Any]]:
        clues = self._clues()
        if self._clue_idx is None or not (0 <= self._clue_idx < len(clues)):
            return None
        clue = clues[self._clue_idx]
        return clue if isinstance(clue, dict) else None

    def refresh_tab(self, data: dict[str, Any]) -> None:
        clues = get_list(data, "clues")
        current_id = None
        clue = self._current_clue()
        if clue is not None:
            current_id = clue.get("id")
        self._clue_list.clear_options()
        for i, item in enumerate(clues):
            if isinstance(item, dict):
                self._clue_list.add_option(Option(entity_label(item), id=str(i)))
        new_idx = None
        if current_id is not None:
            new_idx = next(
                (
                    i
                    for i, c in enumerate(clues)
                    if isinstance(c, dict) and c.get("id") == current_id
                ),
                None,
            )
        if new_idx is None and clues:
            new_idx = 0
        self._clue_idx = new_idx
        if new_idx is not None:
            self._clue_list.highlighted = new_idx
        self._fill_form()

    def _fill_form(self) -> None:
        clue = self._current_clue()
        if clue is None:
            self._refs.update("")
            return
        self._id.set_value(_str_text(clue.get("id")))
        self._name.set_value(_str_text(clue.get("name")))
        self._text.set_value(_str_text(clue.get("text")))
        self._refresh_refs()

    def _refresh_refs(self) -> None:
        clue = self._current_clue()
        if clue is None:
            return
        ident = str(clue.get("id", ""))
        referrers = clue_referrers(self.editor.draft.data, ident)
        if referrers:
            lines = "\n".join(f"· {r}" for r in referrers)
            self._refs.update(f"[b]获取途径（{len(referrers)}）[/b]\n{lines}")
        else:
            self._refs.update(
                "[yellow]该线索没有任何获取途径（检定点/死亡奖励/条件引用均未见）[/yellow]"
            )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.control is self._clue_list and event.option.id is not None:
            self._clue_idx = int(str(event.option.id))
            self._fill_form()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        classes = event.button.classes
        data = self.editor.draft.data

        def move(items: list[Any], index: Optional[int], delta: int) -> None:
            if index is None or not (0 <= index + delta < len(items)):
                return
            items[index], items[index + delta] = items[index + delta], items[index]

        if "-clue-add" in classes:
            new_id = generate_unique_id(
                "new_clue",
                {str(c.get("id", "")) for c in self._clues() if isinstance(c, dict)},
            )
            clues = data.get("clues")
            if not isinstance(clues, list):
                clues = []
                data["clues"] = clues
            clues.append(new_clue_dict(new_id))
            self.editor.refresh_all()
        elif "-clue-del" in classes:
            clue = self._current_clue()
            if clue is None:
                return
            self._confirm_delete(clue)
        elif "-clue-up" in classes or "-clue-down" in classes:
            clues = self._clues()
            delta = -1 if "-clue-up" in classes else 1
            move(clues, self._clue_idx, delta)
            if self._clue_idx is not None:
                self._clue_idx += delta
            self.editor.refresh_all()

    def _confirm_delete(self, clue: dict[str, Any]) -> None:
        ident = str(clue.get("id", "?"))
        referrers = clue_referrers(self.editor.draft.data, ident)
        warn = (
            f"\n\n注意：仍有 {len(referrers)} 处引用该线索，删除后校验会报 ERROR。"
            if referrers
            else ""
        )

        def _delete(confirmed: Optional[bool]) -> None:  # noqa: FBT001
            if not confirmed:
                return
            clues = self._clues()
            if clue in clues:
                clues.remove(clue)
            self.editor.refresh_all()

        self.app.push_screen(
            ConfirmScreen("删除线索", f"确定删除线索 {entity_label(clue)} 吗？{warn}"),
            _delete,
        )

    def on_field_changed(self, event: FieldChanged) -> None:
        prefix, _, field = event.key.partition(".")
        if prefix != "clue":
            return
        clue = self._current_clue()
        if clue is None:
            return
        value = event.value
        if field == "id":
            old = str(clue.get("id", ""))
            new = str(value)
            if new == old or not new:
                return
            sites = rename_entity(self.editor.draft.data, "clue", old, new)
            self.editor.refresh_all()
            if len(sites) > 1:
                self.notify(f"已级联更新 {len(sites) - 1} 处引用")
            return
        clue[field] = value
        if field == "name":
            self._update_list_label()
        self.editor.on_data_changed()

    def _update_list_label(self) -> None:
        clue = self._current_clue()
        if clue is None or self._clue_idx is None:
            return
        self._clue_list.replace_option_prompt_at_index(
            self._clue_idx, entity_label(clue)
        )
