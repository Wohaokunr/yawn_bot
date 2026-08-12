"""编辑器分区页基类与各 Tab 实现。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional, cast

from textual.widget import Widget

if TYPE_CHECKING:
    from ..app import ModuleEditorApp  # noqa: TID252
    from ..widgets import FieldChanged  # noqa: TID252


class EditorTab(Widget):
    """Tab 基类：refresh 从 dict 状态重填控件（不重建控件树）。"""

    def __init__(self) -> None:
        # Textual 的 CSS 类型选择器不会把基类 ``EditorTab`` 当作子类匹配。
        # 显式挂一个共享 class，响应式样式才能覆盖所有具体 Tab。
        super().__init__(classes="editor-tab")
        self._populated = False

    @property
    def editor(self) -> "ModuleEditorApp":
        return cast("ModuleEditorApp", self.app)

    def refresh_tab(self, data: dict[str, Any]) -> None:
        """从 dict 重填本页控件；子类实现。"""

    def on_field_changed(self, event: FieldChanged) -> None:
        """子类覆写：把 FieldChanged 写回 draft.data。"""

    def locate_path(self, path: tuple[Any, ...]) -> None:
        """将全局搜索结果定位到本页；子类按自己的嵌套索引实现。"""

    def duplicate_current(self) -> bool:
        """复制当前选中项；返回是否完成复制。"""
        return False


def move_item(items: list[Any], index: Optional[int], delta: int) -> Optional[int]:
    """移动列表项并返回新索引；边界或无选中时不修改并返回 ``None``。"""
    if index is None or not (0 <= index < len(items)):
        return None
    target = index + delta
    if not (0 <= target < len(items)):
        return None
    items[index], items[target] = items[target], items[index]
    return target
