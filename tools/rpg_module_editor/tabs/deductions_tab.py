"""「推论」页：确定性联合推理规则编辑。"""

from __future__ import annotations

from typing import Any, Optional

from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Label, OptionList
from textual.widgets.option_list import Option

from ..state import (  # noqa: TID252
    build_reference_options_for_field,
    duplicate_item,
    entity_label,
    generate_unique_id,
    get_list,
    new_deduction_dict,
    rename_entity,
)
from ..widgets import (  # noqa: TID252
    ConfirmScreen,
    FieldChanged,
    IdInput,
    IntInput,
    LabeledInput,
    LabeledSwitch,
    LabeledTextArea,
    ReferenceListEditor,
    StrListEditor,
)
from . import EditorTab, move_item

_ENTITY_PATH_PARTS = 2


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _strings(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _keyword_text(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    return "\n".join(
        " | ".join(str(word) for word in group)
        for group in value
        if isinstance(group, list)
    )


def _parse_keyword_text(value: str) -> list[list[str]]:
    return [
        [word.strip() for word in line.split("|") if word.strip()]
        for line in value.splitlines()
        if any(word.strip() for word in line.split("|"))
    ]


class DeductionsTab(EditorTab):
    """确定性推论列表与表单。"""

    DEFAULT_CSS = """
    DeductionsTab { height: 1fr; }
    DeductionsTab Horizontal.-master { height: 1fr; }
    DeductionsTab Vertical.-list-pane { width: 34; }
    DeductionsTab Vertical.-form-pane { width: 1fr; }
    DeductionsTab Horizontal.-row { height: 3; }
    DeductionsTab Button { margin-right: 1; }
    DeductionsTab OptionList.-main-list { height: 1fr; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._list = OptionList(classes="-main-list")
        self._id = IdInput("推论 id", "deduction.id")
        self._name = LabeledInput("名称 name", "deduction.name")
        self._required = ReferenceListEditor(
            "必需线索 required_clues",
            "deduction.required_clues",
            badge="只能引用玩家持有或已公开的线索",
        )
        self._keywords = LabeledTextArea(
            "结论关键词 conclusion_keywords",
            "deduction.conclusion_keywords_text",
            badge="每行一组；同组同义词用 | 分隔；每组至少命中一个",
        )
        self._success = LabeledTextArea(
            "成功文案 success_text", "deduction.success_text"
        )
        self._failure = LabeledTextArea(
            "失败提示 failure_hint", "deduction.failure_hint"
        )
        self._flags = StrListEditor("解锁 flags", "deduction.unlock_flags")
        self._grants = ReferenceListEditor(
            "公开奖励线索 grant_clues", "deduction.grant_clues"
        )
        self._once = LabeledSwitch("只结算一次 once", "deduction.once", value=True)
        self._failure_cost = IntInput(
            "首次失败时间成本 failure_time_cost", "deduction.failure_time_cost"
        )
        self._index: Optional[int] = None

    def compose(self) -> Any:
        with Horizontal(classes="-master"):
            with Vertical(classes="-list-pane"):
                yield Label("[b]推论列表[/b]", markup=True)
                yield self._list
                with Horizontal(classes="-row"):
                    yield Button("新增", variant="primary", classes="-add")
                    yield Button("删除", variant="error", classes="-delete")
                    yield Button("上移", classes="-up")
                    yield Button("下移", classes="-down")
                    yield Button("复制", classes="-copy")
            with VerticalScroll(classes="-form-pane"):
                yield self._id
                yield self._name
                yield self._required
                yield self._keywords
                yield self._success
                yield self._failure
                yield self._flags
                yield self._grants
                yield self._once
                yield self._failure_cost

    def _items(self) -> list[Any]:
        return get_list(self.editor.draft.data, "deductions")

    def _current(self) -> Optional[dict[str, Any]]:
        items = self._items()
        if self._index is None or not (0 <= self._index < len(items)):
            return None
        item = items[self._index]
        return item if isinstance(item, dict) else None

    def locate_path(self, path: tuple[Any, ...]) -> None:
        if len(path) >= _ENTITY_PATH_PARTS and path[0] == "deductions":
            self._index = int(path[1])
            self._fill_form()

    def refresh_tab(self, data: dict[str, Any]) -> None:
        current = self._current()
        current_id = current.get("id") if current is not None else None
        items = get_list(data, "deductions")
        self._list.clear_options()
        for index, item in enumerate(items):
            if isinstance(item, dict):
                self._list.add_option(Option(entity_label(item), id=str(index)))
        self._index = next(
            (
                index
                for index, item in enumerate(items)
                if isinstance(item, dict) and item.get("id") == current_id
            ),
            0 if items else None,
        )
        if self._index is not None:
            self._list.highlighted = self._index
        self._fill_form()

    def _fill_form(self) -> None:
        item = self._current()
        if item is None:
            return
        data = self.editor.draft.data
        clue_options = build_reference_options_for_field(
            data, "deduction.required_clues"
        )
        self._required.set_reference_options(clue_options)
        self._grants.set_reference_options(clue_options)
        self._id.set_value(_text(item.get("id")))
        self._name.set_value(_text(item.get("name")))
        self._required.set_items(_strings(item.get("required_clues")))
        self._keywords.set_value(_keyword_text(item.get("conclusion_keywords")))
        self._success.set_value(_text(item.get("success_text")))
        self._failure.set_value(_text(item.get("failure_hint")))
        self._flags.set_items(_strings(item.get("unlock_flags")))
        self._grants.set_items(_strings(item.get("grant_clues")))
        self._once.set_value(bool(item.get("once", True)))
        self._failure_cost.set_value(str(item.get("failure_time_cost", 1)))

    def duplicate_current(self) -> bool:
        items = self._items()
        new_index = duplicate_item(
            items,
            self._index,
            id_scope={
                str(item.get("id", "")) for item in items if isinstance(item, dict)
            },
        )
        if new_index is None:
            return False
        self._index = new_index
        self.editor.refresh_all()
        self.editor.on_data_changed()
        return True

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.control is self._list and event.option.id is not None:
            self._index = int(str(event.option.id))
            self._fill_form()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        classes = event.button.classes
        items = self._items()
        if "-add" in classes:
            ident = generate_unique_id(
                "new_deduction",
                {str(item.get("id", "")) for item in items if isinstance(item, dict)},
            )
            if not isinstance(self.editor.draft.data.get("deductions"), list):
                self.editor.draft.data["deductions"] = items = []
            items.append(new_deduction_dict(ident))
            self._index = len(items) - 1
            self.editor.refresh_all()
        elif "-delete" in classes:
            item = self._current()
            if item is not None:
                self._confirm_delete(item)
        elif "-up" in classes or "-down" in classes:
            new_index = move_item(items, self._index, -1 if "-up" in classes else 1)
            if new_index is not None:
                self._index = new_index
                self.editor.refresh_all()
        elif "-copy" in classes:
            self.duplicate_current()

    def _confirm_delete(self, item: dict[str, Any]) -> None:
        def remove(confirmed: Optional[bool]) -> None:  # noqa: FBT001
            if not confirmed:
                return
            items = self._items()
            if item in items:
                index = items.index(item)
                items.remove(item)
                self._index = min(index, len(items) - 1) if items else None
            self.editor.refresh_all()

        self.app.push_screen(
            ConfirmScreen("删除推论", f"确定删除推论 {entity_label(item)} 吗？"),
            remove,
        )

    def on_field_changed(self, event: FieldChanged) -> None:
        prefix, _, field = event.key.partition(".")
        if prefix != "deduction":
            return
        item = self._current()
        if item is None:
            return
        if field == "id":
            old, new = str(item.get("id", "")), str(event.value)
            if new and new != old:
                rename_entity(self.editor.draft.data, "deduction", old, new)
                self.editor.refresh_all()
            return
        if field == "conclusion_keywords_text":
            item["conclusion_keywords"] = _parse_keyword_text(str(event.value))
        elif field == "failure_time_cost":
            item[field] = 0 if event.value is None else max(int(event.value), 0)
        else:
            item[field] = event.value
        if field == "name" and self._index is not None:
            self._list.replace_option_prompt_at_index(self._index, entity_label(item))
        self.editor.on_data_changed()
