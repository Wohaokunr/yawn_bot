"""编辑器分区页基类与各 Tab 实现。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from textual.widget import Widget

if TYPE_CHECKING:
    from ..app import ModuleEditorApp  # noqa: TID252
    from ..widgets import FieldChanged  # noqa: TID252


class EditorTab(Widget):
    """Tab 基类：refresh 从 dict 状态重填控件（不重建控件树）。"""

    def __init__(self) -> None:
        super().__init__()
        self._populated = False

    @property
    def editor(self) -> "ModuleEditorApp":
        return cast("ModuleEditorApp", self.app)

    def refresh_tab(self, data: dict[str, Any]) -> None:
        """从 dict 重填本页控件；子类实现。"""

    def on_field_changed(self, event: FieldChanged) -> None:
        """子类覆写：把 FieldChanged 写回 draft.data。"""
