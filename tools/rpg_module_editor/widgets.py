"""共享表单控件：统一以冒泡的 FieldChanged 把值写回 dict 状态。

所有控件都支持 ``set_value`` 静默填充（切换选中实体时用，不回弹
FieldChanged），避免「dict 写回聚焦控件」造成的光标/滚动抖动。
"""

from __future__ import annotations

import contextlib
import re
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Optional, cast

from rich.markup import escape
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Input, Label, OptionList, Select, Static, Switch
from textual.widgets import TextArea as _TextArea
from textual.widgets.option_list import Option

from .schema_loader import parse_hhmm

if TYPE_CHECKING:
    from textual.binding import BindingType

_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# _coerce_value 的返回值哨兵：本次变化不产生 FieldChanged
SKIP_EMIT = object()


class FieldChanged(Message):
    """表单字段值变化：key 是 dict 写入键，value 已按控件语义转型。

    Message 内建的 ``control`` 即发布消息的控件，无需自存。
    """

    def __init__(self, key: str, value: Any) -> None:
        super().__init__()
        self.key = key
        self.value = value


def _label_markup(label: str, badge: str) -> str:
    text = escape(label)
    if badge:
        text += f" [dim]· {escape(badge)}[/dim]"
    return text


class LabeledInput(Widget):
    """单行输入 + 标签（可挂防剧透可见性徽章）。"""

    DEFAULT_CSS = """
    LabeledInput {
        layout: vertical;
        height: auto;
        margin-bottom: 1;
        Label { height: 1; color: $text-muted; }
        Label.-invalid-note { color: $warning; }
        Input.-invalid { border: tall $error; }
    }
    """

    def __init__(  # noqa: PLR0913
        self,
        label: str,
        key: str,
        *,
        value: str = "",
        placeholder: str = "",
        badge: str = "",
        hint: str = "",
    ) -> None:
        super().__init__()
        self.field_key = key
        self._label_markup = _label_markup(label, badge)
        self._initial = value
        self._placeholder = placeholder
        self._hint = hint

    def compose(self) -> Any:
        yield Label(self._label_markup, markup=True)
        yield Input(value=self._initial, placeholder=self._placeholder)
        if self._hint:
            yield Label(f"[dim]{escape(self._hint)}[/dim]", markup=True)

    @property
    def input(self) -> Input:
        return self.query_one(Input)

    def on_input_changed(self, event: Input.Changed) -> None:
        # 注意：Textual 沿 MRO 调用每一层同名 handler，子类不得再覆写
        # 本方法（会双重触发），转型一律走 _coerce_value。
        if event.control is not self.input:
            return
        self.refresh_validity(event.value)
        value = self._coerce_value(event.value)
        if value is SKIP_EMIT:
            return
        self.post_message(FieldChanged(self.field_key, value))

    def _coerce_value(self, text: str) -> Any:
        """输入文本 → FieldChanged 载荷；子类覆写（返回 SKIP_EMIT 则不发）。"""
        return text

    def refresh_validity(self, value: str) -> None:
        """子类覆写：按当前值设置/清除 -invalid 样式。"""

    def set_value(self, value: str) -> None:
        # Changed 在赋值时同步入队：prevent 在发布点拦截，
        # 避免「程序填充」被当成用户编辑回写 dict。
        with self.input.prevent(Input.Changed):
            self.input.value = value
        self.refresh_validity(value)


class IdInput(LabeledInput):
    """ASCII snake_case id 输入框（实时合法性着色）。"""

    def refresh_validity(self, value: str) -> None:
        self.input.set_class(not bool(_ID_RE.match(value)), "-invalid")


class TimeInput(LabeledInput):
    """HH:MM 时刻输入框（实时合法性着色）。"""

    def refresh_validity(self, value: str) -> None:
        self.input.set_class(parse_hhmm(value) is None, "-invalid")


class IntInput(LabeledInput):
    """整数输入：值合法才发 FieldChanged；空串发 None（清空可选字段）。"""

    def _coerce_value(self, text: str) -> Any:
        stripped = text.strip()
        if not stripped:
            return None
        try:
            return int(stripped)
        except ValueError:
            return SKIP_EMIT  # 保留 dict 里最后一次合法值


class LabeledTextArea(Widget):
    """多行文本编辑（旁白 / 人格 / 文案等长文本）。"""

    DEFAULT_CSS = """
    LabeledTextArea {
        layout: vertical;
        height: auto;
        margin-bottom: 1;
        Label { height: 1; color: $text-muted; }
        TextArea { height: 6; }
        TextArea.-tall { height: 9; }
        Label.-counter { height: 1; color: $text-muted; }
    }
    """

    def __init__(  # noqa: PLR0913
        self,
        label: str,
        key: str,
        *,
        value: str = "",
        badge: str = "",
        tall: bool = False,
        counter_limit: Optional[int] = None,
        counter_note: str = "",
    ) -> None:
        super().__init__()
        self.field_key = key
        self._label_markup = _label_markup(label, badge)
        self._initial = value
        self._tall = tall
        self._counter_limit = counter_limit
        self._counter_note = counter_note

    def compose(self) -> Any:
        yield Label(self._label_markup, markup=True)
        area = _TextArea(self._initial, soft_wrap=True)
        if self._tall:
            area.add_class("-tall")
        yield area
        if self._counter_limit is not None:
            yield Label("", classes="-counter")

    @property
    def area(self) -> _TextArea:
        return self.query_one(_TextArea)

    def _counter_label(self) -> Optional[Label]:
        try:
            return self.query_one(".-counter", Label)
        except Exception:  # noqa: BLE001 —— 未启用计数
            return None

    def _update_counter(self, text: str) -> None:
        label = self._counter_label()
        if label is None or self._counter_limit is None:
            return
        total = len(text)
        note = f"｜{self._counter_note}" if self._counter_note else ""
        label.update(
            f"[dim]已输入 {total} 字（前 {self._counter_limit} 字{note}）[/dim]"
        )

    def on_mount(self) -> None:
        self._update_counter(self.area.text)

    def on_text_area_changed(self, event: _TextArea.Changed) -> None:
        if event.text_area is not self.area:
            return
        text = event.text_area.text
        self._update_counter(text)
        self.post_message(FieldChanged(self.field_key, text))

    def set_value(self, value: str) -> None:
        with self.area.prevent(_TextArea.Changed):
            self.area.text = value
        self._update_counter(value)


class LabeledSwitch(Widget):
    """开关（once / auto / generic_endings / away 等布尔字段）。"""

    DEFAULT_CSS = """
    LabeledSwitch {
        layout: horizontal;
        height: auto;
        margin-bottom: 1;
        Switch { margin-right: 1; }
        Label { height: 1; padding: 1 0; color: $text-muted; }
    }
    """

    def __init__(
        self, label: str, key: str, *, value: bool = False, badge: str = ""
    ) -> None:
        super().__init__()
        self.field_key = key
        self._label_markup = _label_markup(label, badge)
        self._initial = value

    def compose(self) -> Any:
        yield Switch(value=self._initial)
        yield Label(self._label_markup, markup=True)

    @property
    def switch(self) -> Switch:
        return self.query_one(Switch)

    def on_switch_changed(self, event: Switch.Changed) -> None:
        self.post_message(FieldChanged(self.field_key, event.value))

    def set_value(self, value: bool) -> None:  # noqa: FBT001
        with self.switch.prevent(Switch.Changed):
            self.switch.value = value


class LabeledSelect(Widget):
    """下拉选择；外部值（如 difficulty 自由文本）以「自定义：X」项保留。"""

    DEFAULT_CSS = """
    LabeledSelect {
        layout: vertical;
        height: auto;
        margin-bottom: 1;
        Label { height: 1; color: $text-muted; }
    }
    """

    def __init__(  # noqa: PLR0913
        self,
        label: str,
        key: str,
        options: list[tuple[str, Any]],
        *,
        value: Any = None,
        allow_blank: bool = True,
        badge: str = "",
    ) -> None:
        super().__init__()
        self.field_key = key
        self._label_markup = _label_markup(label, badge)
        self._options = options
        self._initial = value
        self._allow_blank = allow_blank

    def compose(self) -> Any:
        yield Label(self._label_markup, markup=True)
        options = self._with_foreign(self._options, self._initial)
        if self._initial is None:
            yield Select(options, allow_blank=self._allow_blank)
        else:
            yield Select(options, allow_blank=self._allow_blank, value=self._initial)

    @property
    def select(self) -> Select:
        return self.query_one(Select)

    @staticmethod
    def _with_foreign(
        options: list[tuple[str, Any]], current: Any
    ) -> list[tuple[str, Any]]:
        result = list(options)
        if current not in (None, "") and current not in {v for _, v in result}:
            result.append((f"自定义：{current}", current))
        return result

    def _assign_value(self, value: Any) -> None:
        # 值不在选项里已由 _with_foreign 兜底
        with self.select.prevent(Select.Changed), contextlib.suppress(Exception):
            self.select.value = Select.BLANK if value is None else value

    def set_options(self, options: list[tuple[str, Any]], current: Any = None) -> None:
        """id 集合变化时刷新选项，保留当前值。"""
        self._options = options
        value = current if current is not None else self.select.value
        if value == Select.BLANK:
            value = None
        with self.select.prevent(Select.Changed):
            self.select.set_options(self._with_foreign(options, value))
            with contextlib.suppress(Exception):
                self.select.value = Select.BLANK if value is None else value

    def set_value(self, value: Any) -> None:
        self._assign_value(value)

    def on_select_changed(self, event: Select.Changed) -> None:
        value = None if event.value == Select.BLANK else event.value
        self.post_message(FieldChanged(self.field_key, value))


class TokenPicker(ModalScreen[Optional[str]]):
    """条件词条插入面板：从当前模组真实 id 生成可插入词条。"""

    DEFAULT_CSS = """
    TokenPicker {
        align: center middle;
        OptionList { width: 62; height: 18; border: thick $primary; }
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "cancel", "取消")]

    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        super().__init__()
        self._tokens = tokens

    def compose(self) -> Any:
        options = [Option(label, id=token) for label, token in self._tokens]
        yield OptionList(*options)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(cast("Optional[str]", event.option.id))

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConditionInput(Widget):
    """条件表达式输入：实时引用校验 + 从真实 id 插入词条。"""

    DEFAULT_CSS = """
    ConditionInput {
        layout: vertical;
        height: auto;
        margin-bottom: 1;
        Horizontal { height: auto; }
        Input { width: 1fr; }
        Button { margin-left: 1; }
        Label.-feedback { height: 1; }
        Label.-ok { color: $success; }
        Label.-bad { color: $error; }
    }
    """

    def __init__(  # noqa: PLR0913
        self,
        label: str,
        key: str,
        validator: Callable[[str], Optional[str]],
        tokens_provider: Callable[[], list[tuple[str, str]]],
        *,
        value: str = "",
        badge: str = "",
    ) -> None:
        super().__init__()
        self.field_key = key
        self._label_markup = _label_markup(label, badge)
        self._validator = validator
        self._tokens_provider = tokens_provider
        self._initial = value

    def compose(self) -> Any:
        yield Label(self._label_markup, markup=True)
        with Horizontal():
            yield Input(
                value=self._initial, placeholder="如 clue:rusty_key & flag:arson"
            )
            yield Button("插入词条", variant="primary", classes="-insert-token")
        yield Label("", classes="-feedback")

    @property
    def input(self) -> Input:
        return self.query_one(Input)

    def _feedback(self) -> Label:
        return self.query_one(".-feedback", Label)

    def refresh_feedback(self) -> None:
        condition = self.input.value.strip()
        label = self._feedback()
        if not condition:
            label.remove_class("-ok", "-bad")
            label.update("[dim]空条件[/dim]")
            return
        err = self._validator(condition)
        if err is None:
            label.set_classes("-feedback -ok")
            label.update("✓ 条件引用合法")
        else:
            label.set_classes("-feedback -bad")
            label.update(f"✗ {err}")

    def on_mount(self) -> None:
        self.refresh_feedback()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.control is not self.input:
            return
        self.refresh_feedback()
        self.post_message(FieldChanged(self.field_key, event.value))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if "-insert-token" not in event.button.classes:
            return
        tokens = self._tokens_provider()
        if not tokens:
            self.notify(
                "没有可插入的词条：先定义线索 / 场景 / 怪物", severity="warning"
            )
            return
        self.app.push_screen(TokenPicker(tokens), self._insert_token)

    def _insert_token(self, token: Optional[str]) -> None:
        if token is None:
            return
        current = self.input.value.strip()
        self.input.value = token if not current else f"{current} & {token}"
        self.input.focus()

    def set_value(self, value: str) -> None:
        with self.input.prevent(Input.Changed):
            self.input.value = value
        self.refresh_feedback()


class StrListEditor(Widget):
    """字符串列表编辑（knows / secrets / triggers / keywords）：增删改序。"""

    DEFAULT_CSS = """
    StrListEditor {
        layout: vertical;
        height: auto;
        margin-bottom: 1;
        Label { height: 1; color: $text-muted; }
        OptionList { height: 6; }
        Horizontal { height: auto; }
        Input { width: 1fr; }
        Horizontal.-buttons { height: 3; }
        Button { margin-right: 1; }
    }
    """

    def __init__(
        self, label: str, key: str, *, badge: str = "", hint: str = ""
    ) -> None:
        super().__init__()
        self.field_key = key
        self._label_markup = _label_markup(label, badge)
        self._hint = hint
        self._items: list[str] = []

    def compose(self) -> Any:
        yield Label(self._label_markup, markup=True)
        yield OptionList()
        with Horizontal():
            yield Input(placeholder="输入新条目后按「添加」")
            yield Button("添加", variant="primary", classes="-add")
        with Horizontal(classes="-buttons"):
            yield Button("删除", variant="error", classes="-delete")
            yield Button("上移", classes="-up")
            yield Button("下移", classes="-down")
        if self._hint:
            yield Label(f"[dim]{escape(self._hint)}[/dim]", markup=True)

    @property
    def list_view(self) -> OptionList:
        return self.query_one(OptionList)

    def _refresh_options(self) -> None:
        # 勿命名为 _render：会遮蔽 Widget._render 导致渲染崩溃
        view = self.list_view
        view.clear_options()
        for i, item in enumerate(self._items):
            view.add_option(Option(f"{i + 1}. {item}", id=str(i)))

    def _emit(self) -> None:
        self.post_message(FieldChanged(self.field_key, list(self._items)))

    def set_items(self, items: list[str]) -> None:
        self._items = list(items)
        if self.is_mounted:
            self._refresh_options()

    def on_mount(self) -> None:
        self._refresh_options()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        classes = event.button.classes
        input_box = self.query_one(Input)
        highlighted = self.list_view.highlighted
        if "-add" in classes:
            text = input_box.value.strip()
            if text:
                self._items.append(text)
                input_box.value = ""
                self._refresh_options()
                self._emit()
        elif (
            "-delete" in classes
            and highlighted is not None
            and 0 <= highlighted < len(self._items)
        ):
            del self._items[highlighted]
            self._refresh_options()
            self._emit()
        elif "-up" in classes and highlighted and 0 < highlighted < len(self._items):
            self._items[highlighted - 1], self._items[highlighted] = (
                self._items[highlighted],
                self._items[highlighted - 1],
            )
            self._refresh_options()
            self.list_view.highlighted = highlighted - 1
            self._emit()
        elif (
            "-down" in classes
            and highlighted is not None
            and 0 <= highlighted < len(self._items) - 1
        ):
            self._items[highlighted], self._items[highlighted + 1] = (
                self._items[highlighted + 1],
                self._items[highlighted],
            )
            self._refresh_options()
            self.list_view.highlighted = highlighted + 1
            self._emit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if text:
            self._items.append(text)
            self.input_new_value()
            self._refresh_options()
            self._emit()

    def input_new_value(self) -> None:
        self.query_one(Input).value = ""


class ConfirmScreen(ModalScreen[bool]):
    """通用确认对话框（退出脏保护 / 改名级联确认等）。"""

    DEFAULT_CSS = """
    ConfirmScreen {
        align: center middle;
        VerticalScroll {
            width: 70;
            max-height: 24;
            border: thick $primary;
            background: $surface;
            padding: 1 2;
        }
        Horizontal { height: 3; margin-top: 1; align-horizontal: right; }
        Button { margin-left: 2; }
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "cancel", "取消")]

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> Any:
        with VerticalScroll() as box:
            box.border_title = self._title
            yield Static(self._body)
            with Horizontal():
                yield Button("取消", variant="default", classes="-cancel")
                yield Button("确认", variant="warning", classes="-confirm")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss("-confirm" in event.button.classes)

    def action_cancel(self) -> None:
        self.dismiss(False)  # noqa: FBT003
