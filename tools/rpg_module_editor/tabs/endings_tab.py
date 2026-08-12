"""「结局」页：结局列表（声明序=优先级）+ 表单。"""

from __future__ import annotations

from typing import Any, Optional

from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Label, OptionList
from textual.widgets.option_list import Option

from ..state import (  # noqa: TID252
    build_condition_tokens,
    duplicate_item,
    entity_label,
    get_list,
    new_ending_dict,
)
from ..validate import check_ending_condition  # noqa: TID252
from ..widgets import (  # noqa: TID252
    ConditionInput,
    ConfirmScreen,
    FieldChanged,
    IdInput,
    LabeledInput,
    LabeledSelect,
    LabeledTextArea,
)
from . import EditorTab

_ENTITY_PATH_PARTS = 2

_OUTCOME_OPTIONS = [
    ("好结局 good", "good"),
    ("坏结局 bad", "bad"),
    ("中性 neutral", "neutral"),
]
_OUTCOME_MARK = {"good": "✓", "bad": "✗", "neutral": "·"}

# engine._GENERIC_ENDINGS 的静态说明（不 import engine——会拖起整个 bot）
_GENERIC_ENDINGS_NOTE = """[dim]generic_endings 开启时，模组结局之后按此序扫描\
内置通用结局：
1. flag:arson>=4 纵火彩蛋（neutral）
2. flag:arson>=2 火灾（bad）
3. flag:murder 谋杀逮捕（bad）
4. flag:assault>=3 袭击制服（bad）
5. all_players_incapped 全军覆没（bad）
模组结局永远优先；声明序即优先级。[/dim]"""


def _str_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


class EndingsTab(EditorTab):
    """结局页。"""

    DEFAULT_CSS = """
    EndingsTab { height: 1fr; }
    EndingsTab Horizontal.-master { height: 1fr; }
    EndingsTab Vertical.-list-pane { width: 38; }
    EndingsTab Vertical.-form-pane { width: 1fr; }
    EndingsTab Horizontal.-row { height: 3; }
    EndingsTab Button { margin-right: 1; }
    EndingsTab OptionList.-main-list { height: 1fr; }
    EndingsTab Label.-note { height: auto; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._ending_list = OptionList(classes="-main-list")
        self._id = IdInput("结局 id", "ending.id", hint="end_session 工具的合法取值")
        self._name = LabeledInput(
            "展示名 name", "ending.name", badge="进概览与 query_story；空回退 id"
        )
        self._outcome = LabeledSelect(
            "倾向 outcome", "ending.outcome", _OUTCOME_OPTIONS
        )
        self._condition = ConditionInput(
            "触发条件 condition",
            "ending.condition",
            validator=lambda cond: check_ending_condition(cond, self.editor.draft.data),
            tokens_provider=self._tokens,
            badge="不可恒真（空/仅 always 会被拒载）",
        )
        self._summary = LabeledTextArea(
            "来龙去脉 summary",
            "ending.summary",
            badge="仅 KP 经 query_story 查询可见，绝不播报；按导演指引写",
        )
        self._text = LabeledTextArea(
            "终局播报 text",
            "ending.text",
            tall=True,
            badge="逐字播报；惯例 ═══ 结局 · X ═══ 标题行",
        )
        self._ending_idx: Optional[int] = None

    def compose(self) -> Any:
        with Horizontal(classes="-master"):
            with Vertical(classes="-list-pane"):
                yield Label("[b]结局列表（声明序 = 优先级）[/b]", markup=True)
                yield self._ending_list
                with Horizontal(classes="-row"):
                    yield Button("新增", variant="primary", classes="-ending-add")
                    yield Button("删除", variant="error", classes="-ending-del")
                    yield Button("上移=提权", classes="-ending-up")
                    yield Button("下移=降权", classes="-ending-down")
                    yield Button("复制", classes="-ending-copy")
                yield Label(_GENERIC_ENDINGS_NOTE, markup=True, classes="-note")
            with VerticalScroll(classes="-form-pane"):
                yield Label(
                    "[dim]时间兜底结局（time_after/time_between）请声明在最后，"
                    "否则会遮蔽其后声明的结局[/dim]",
                    markup=True,
                )
                yield self._id
                yield self._name
                yield self._outcome
                yield self._condition
                yield self._summary
                yield self._text

    def _endings(self) -> list[Any]:
        return get_list(self.editor.draft.data, "endings")

    def _current_ending(self) -> Optional[dict[str, Any]]:
        endings = self._endings()
        if self._ending_idx is None or not (0 <= self._ending_idx < len(endings)):
            return None
        ending = endings[self._ending_idx]
        return ending if isinstance(ending, dict) else None

    def locate_path(self, path: tuple[Any, ...]) -> None:
        if len(path) >= _ENTITY_PATH_PARTS and path[0] == "endings":
            self._ending_idx = int(path[1])
            self._fill_form()

    def duplicate_current(self) -> bool:
        endings = self._endings()
        new_idx = duplicate_item(
            endings,
            self._ending_idx,
            id_scope={
                str(item.get("id", "")) for item in endings if isinstance(item, dict)
            },
        )
        if new_idx is None:
            return False
        self._ending_idx = new_idx
        self.editor.refresh_all()
        self.editor.on_data_changed()
        return True

    def _tokens(self) -> list[tuple[str, str]]:
        return build_condition_tokens(self.editor.draft.data)

    def refresh_tab(self, data: dict[str, Any]) -> None:
        endings = get_list(data, "endings")
        current_id = None
        ending = self._current_ending()
        if ending is not None:
            current_id = ending.get("id")
        self._ending_list.clear_options()
        for i, item in enumerate(endings):
            if isinstance(item, dict):
                mark = _OUTCOME_MARK.get(str(item.get("outcome", "neutral")), "·")
                name = str(item.get("name") or item.get("id", "?"))
                self._ending_list.add_option(
                    Option(
                        f"#{i + 1} {mark} {name}（{item.get('id', '?')}）", id=str(i)
                    )
                )
        new_idx = None
        if current_id is not None:
            new_idx = next(
                (
                    i
                    for i, e in enumerate(endings)
                    if isinstance(e, dict) and e.get("id") == current_id
                ),
                None,
            )
        if new_idx is None and endings:
            new_idx = 0
        self._ending_idx = new_idx
        if new_idx is not None:
            self._ending_list.highlighted = new_idx
        self._fill_form()

    def _fill_form(self) -> None:
        ending = self._current_ending()
        if ending is None:
            return
        self._id.set_value(_str_text(ending.get("id")))
        self._name.set_value(_str_text(ending.get("name")))
        self._outcome.set_value(ending.get("outcome", "neutral"))
        self._condition.set_value(_str_text(ending.get("condition")))
        self._summary.set_value(_str_text(ending.get("summary")))
        self._text.set_value(_str_text(ending.get("text")))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.control is self._ending_list and event.option.id is not None:
            self._ending_idx = int(str(event.option.id))
            self._fill_form()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        classes = event.button.classes
        data = self.editor.draft.data

        def move(index: Optional[int], delta: int) -> None:
            endings = self._endings()
            if index is None or not (0 <= index + delta < len(endings)):
                return
            endings[index], endings[index + delta] = (
                endings[index + delta],
                endings[index],
            )
            self._ending_idx = index + delta

        if "-ending-add" in classes:
            endings = data.get("endings")
            if not isinstance(endings, list):
                endings = []
                data["endings"] = endings
            endings.append(
                new_ending_dict(
                    f"ending_{len([e for e in endings if isinstance(e, dict)]) + 1}"
                )
            )
            self.refresh_tab(data)
            self.editor.on_data_changed()
        elif "-ending-del" in classes:
            ending = self._current_ending()
            if ending is None:
                return
            self._confirm_delete(ending)
        elif "-ending-up" in classes:
            move(self._ending_idx, -1)
            self.refresh_tab(data)
            self.editor.on_data_changed()
        elif "-ending-down" in classes:
            move(self._ending_idx, 1)
            self.refresh_tab(data)
            self.editor.on_data_changed()
        elif "-ending-copy" in classes:
            self.duplicate_current()

    def _confirm_delete(self, ending: dict[str, Any]) -> None:
        def _delete(confirmed: Optional[bool]) -> None:  # noqa: FBT001
            if not confirmed:
                return
            endings = self._endings()
            if ending in endings:
                endings.remove(ending)
            self.editor.refresh_all()

        self.app.push_screen(
            ConfirmScreen("删除结局", f"确定删除结局 {entity_label(ending)} 吗？"),
            _delete,
        )

    def on_field_changed(self, event: FieldChanged) -> None:
        prefix, _, field = event.key.partition(".")
        if prefix != "ending":
            return
        ending = self._current_ending()
        if ending is None:
            return
        value = event.value
        if field == "condition":
            text = str(value).strip()
            ending["condition"] = text
            self.refresh_tab(self.editor.draft.data)
        else:
            ending[field] = value
            if field in ("id", "name", "outcome"):
                self.refresh_tab(self.editor.draft.data)
        self.editor.on_data_changed()
