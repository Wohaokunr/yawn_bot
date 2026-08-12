"""「事件」页：具名剧情事件（纯 KP 上下文，不触发机制）。"""

from __future__ import annotations

from typing import Any, Optional

from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Label, OptionList
from textual.widgets.option_list import Option

from ..state import (  # noqa: TID252
    build_condition_tokens,
    entity_label,
    get_list,
    new_event_dict,
)
from ..validate import check_condition  # noqa: TID252
from ..widgets import (  # noqa: TID252
    ConditionInput,
    ConfirmScreen,
    FieldChanged,
    IdInput,
    LabeledInput,
    LabeledTextArea,
)
from . import EditorTab


def _str_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


class EventsTab(EditorTab):
    """事件页。"""

    DEFAULT_CSS = """
    EventsTab { height: 1fr; }
    EventsTab Horizontal.-master { height: 1fr; }
    EventsTab Vertical.-list-pane { width: 34; }
    EventsTab Vertical.-form-pane { width: 1fr; }
    EventsTab Horizontal.-row { height: 3; }
    EventsTab Button { margin-right: 1; }
    EventsTab OptionList.-main-list { height: 1fr; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._event_list = OptionList(classes="-main-list")
        self._id = IdInput("事件 id", "event.id")
        self._name = LabeledInput(
            "名称 name", "event.name", badge="进概览与「[已发生事件]」"
        )
        self._summary = LabeledTextArea(
            "导演指引 summary",
            "event.summary",
            badge="仅 KP 经 query_story 查询可见，绝不播报",
        )
        self._condition = ConditionInput(
            "触发条件 condition",
            "event.condition",
            validator=lambda cond: check_condition(cond, self.editor.draft.data),
            tokens_provider=self._tokens,
            badge="空条件 = 序幕事件（开局首轮即记）；恒真无害",
        )
        self._event_idx: Optional[int] = None

    def compose(self) -> Any:
        with Horizontal(classes="-master"):
            with Vertical(classes="-list-pane"):
                yield Label("[b]事件列表[/b]", markup=True)
                yield self._event_list
                with Horizontal(classes="-row"):
                    yield Button("新增", variant="primary", classes="-event-add")
                    yield Button("删除", variant="error", classes="-event-del")
                    yield Button("上移", classes="-event-up")
                    yield Button("下移", classes="-event-down")
            with VerticalScroll(classes="-form-pane"):
                yield Label(
                    "[dim]事件纯属 KP 上下文：条件满足时记入「已发生事件」，"
                    "进 KP 提示词与概览，不触发任何机制。[/dim]",
                    markup=True,
                )
                yield self._id
                yield self._name
                yield self._summary
                yield self._condition

    def _events(self) -> list[Any]:
        return get_list(self.editor.draft.data, "events")

    def _current_event(self) -> Optional[dict[str, Any]]:
        events = self._events()
        if self._event_idx is None or not (0 <= self._event_idx < len(events)):
            return None
        event = events[self._event_idx]
        return event if isinstance(event, dict) else None

    def _tokens(self) -> list[tuple[str, str]]:
        return build_condition_tokens(self.editor.draft.data)

    def refresh_tab(self, data: dict[str, Any]) -> None:
        events = get_list(data, "events")
        current_id = None
        event = self._current_event()
        if event is not None:
            current_id = event.get("id")
        self._event_list.clear_options()
        for i, item in enumerate(events):
            if isinstance(item, dict):
                self._event_list.add_option(Option(entity_label(item), id=str(i)))
        new_idx = None
        if current_id is not None:
            new_idx = next(
                (
                    i
                    for i, e in enumerate(events)
                    if isinstance(e, dict) and e.get("id") == current_id
                ),
                None,
            )
        if new_idx is None and events:
            new_idx = 0
        self._event_idx = new_idx
        if new_idx is not None:
            self._event_list.highlighted = new_idx
        self._fill_form()

    def _fill_form(self) -> None:
        event = self._current_event()
        if event is None:
            return
        self._id.set_value(_str_text(event.get("id")))
        self._name.set_value(_str_text(event.get("name")))
        self._summary.set_value(_str_text(event.get("summary")))
        self._condition.set_value(_str_text(event.get("condition")))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.control is self._event_list and event.option.id is not None:
            self._event_idx = int(str(event.option.id))
            self._fill_form()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        classes = event.button.classes
        data = self.editor.draft.data

        def move(index: Optional[int], delta: int) -> None:
            events = self._events()
            if index is None or not (0 <= index + delta < len(events)):
                return
            events[index], events[index + delta] = events[index + delta], events[index]
            self._event_idx = index + delta

        if "-event-add" in classes:
            events = data.get("events")
            if not isinstance(events, list):
                events = []
                data["events"] = events
            events.append(
                new_event_dict(
                    f"event_{len([e for e in events if isinstance(e, dict)]) + 1}"
                )
            )
            self.refresh_tab(data)
            self.editor.on_data_changed()
        elif "-event-del" in classes:
            event_item = self._current_event()
            if event_item is None:
                return
            self._confirm_delete(event_item)
        elif "-event-up" in classes:
            move(self._event_idx, -1)
            self.refresh_tab(data)
            self.editor.on_data_changed()
        elif "-event-down" in classes:
            move(self._event_idx, 1)
            self.refresh_tab(data)
            self.editor.on_data_changed()

    def _confirm_delete(self, event_item: dict[str, Any]) -> None:
        def _delete(confirmed: Optional[bool]) -> None:  # noqa: FBT001
            if not confirmed:
                return
            events = self._events()
            if event_item in events:
                events.remove(event_item)
            self.editor.refresh_all()

        self.app.push_screen(
            ConfirmScreen("删除事件", f"确定删除事件 {entity_label(event_item)} 吗？"),
            _delete,
        )

    def on_field_changed(self, event: FieldChanged) -> None:
        prefix, _, field = event.key.partition(".")
        if prefix != "event":
            return
        event_item = self._current_event()
        if event_item is None:
            return
        value = event.value
        if field == "condition":
            event_item["condition"] = str(value).strip()
        else:
            event_item[field] = value
            if field in ("id", "name"):
                self._update_list_label()
        self.editor.on_data_changed()

    def _update_list_label(self) -> None:
        event_item = self._current_event()
        if event_item is None or self._event_idx is None:
            return
        self._event_list.replace_option_prompt_at_index(
            self._event_idx, entity_label(event_item)
        )
