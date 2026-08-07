"""「YAML 源码」页：整份文档的文字编辑，应用时整体替换 dict 状态。"""

from __future__ import annotations

import contextlib
from typing import Any

from rich.markup import escape
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Label
from textual.widgets import TextArea as _TextArea

from ..yaml_io import (  # noqa: TID252
    ModuleParseError,
    dump_module_text,
    normalize_data,
    parse_yaml_text,
)
from . import EditorTab


class YamlTab(EditorTab):
    """源码页：与表单共享同一份 dict，切换时按需重渲染。"""

    DEFAULT_CSS = """
    YamlTab {
        layout: vertical;
        Horizontal { height: 3; }
        Button { margin-right: 1; }
        Label.-state { height: 1; color: $text-muted; padding: 0 1; }
        TextArea { height: 1fr; }
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._last_rendered = ""
        self._area = _TextArea("", soft_wrap=False, show_line_numbers=True)
        self._state = Label("", classes="-state")

    def compose(self) -> Any:
        with Vertical():
            with Horizontal():
                yield Button("应用到表单", variant="primary", classes="-apply")
                yield Button("从表单刷新", classes="-reload")
                yield self._state
            yield self._area

    @property
    def area(self) -> _TextArea:
        return self._area

    def _render_from_data(self, data: dict[str, Any]) -> None:
        text = dump_module_text(data)
        self._area.text = text
        self._last_rendered = text
        self._state.update("[dim]已与表单同步[/dim]")

    def refresh_tab(self, data: dict[str, Any]) -> None:
        current = self._area.text
        if not current or current == self._last_rendered:
            self._render_from_data(data)
        else:
            self._state.update(
                "[yellow]源码有未应用的修改：「应用到表单」后才会同步[/yellow]"
            )

    def on_mount(self) -> None:
        # tree-sitter 缺省则纯文本
        with contextlib.suppress(Exception):
            self._area.language = "yaml"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if "-apply" in event.button.classes:
            self.apply_to_form()
        elif "-reload" in event.button.classes:
            self._render_from_data(self.editor.draft.data)

    def apply_to_form(self) -> bool:
        """解析源码并整体替换状态；失败返回 False。"""
        try:
            data = normalize_data(parse_yaml_text(self._area.text))
        except ModuleParseError as e:
            self.notify(f"无法应用：{e}", severity="error", timeout=8)
            self._state.update(f"[red]{escape(str(e))}[/red]")
            return False
        self.editor.draft.replace_data(data)
        self._last_rendered = self._area.text
        self.editor.refresh_all()
        self.notify("源码已应用到全部表单")
        return True
