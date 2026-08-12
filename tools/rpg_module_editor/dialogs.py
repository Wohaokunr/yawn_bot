"""模态对话框：打开 / 另存为（DirectoryTree 实现）、新建、帮助。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Optional

from rich.markup import escape
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DirectoryTree, Input, Label, Markdown, OptionList
from textual.widgets.option_list import Option

from .schema_loader import modules_dir
from .yaml_io import load_or_error

if TYPE_CHECKING:
    from collections.abc import Iterable

    from textual.binding import BindingType

_HELP_MD = """
## 防剧透可见性速查（摘自 modules/README.md）

| 时机 | KP 能看到 |
|---|---|
| 整局一次（概览） | 模组前提（opening 前 150 字）、\
NPC 名册（public_desc + persona + knows）、全部结局与具名事件的名字 |
| 每回合（场景块） | 场景名 + narration、在场 NPC 名字 + public_desc + \
当前 activity、存活怪物名、已发现线索名、出口通行性布尔、调查员定性状态、\
时钟、近期群聊、已发生事件名 |
| 经 query_story 查询 | 结局/事件的 name + summary + 倾向（不返 condition） |
| 永不可见 | 检定成功/失败文案（结算前）、线索 text（永不进提示词）、\
出口条件、结局与事件条件、战斗数值、NPC secrets |

## 关键约定

- **id 一律 ASCII snake_case**；中文只进 name 等展示字段。
- **时间加引号** `"21:00"`（YAML 1.1 会把裸时间解析成六十进制整数）。
- SAN 检点：`priority: 1` + `once: true` + 宽触发词；线索/伤害检点一律 `once: true`。
- NPC 行程按**声明序**匹配，全不匹配 = 不在场——务必以无条件兜底条目收尾。
- 结局声明序即优先级：具体结局在前，时间兜底结局最后；条件不可恒真。
- 播报类文案（narration/opening/检定文案/线索 text/结局 text）**不出现数字**。
- secrets 不得是 persona / public_desc / knows / 行程 activity 的子串（加载期拒载）。
"""


class ModuleDirectoryTree(DirectoryTree):
    """只显示目录与 YAML 模组文件的目录树。"""

    _MODULE_SUFFIXES: ClassVar[set[str]] = {".yaml", ".yml"}

    @staticmethod
    def _is_directory(path: Path) -> bool:
        try:
            return path.is_dir()
        except OSError:
            return False

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        """过滤掉非模组文件，避免误选日志、数据库等无关文件。"""
        for path in paths:
            if self._is_directory(path) or path.suffix.lower() in self._MODULE_SUFFIXES:
                yield path


def _node_path(node: Any) -> Optional[Path]:
    """从 DirectoryTree 节点提取路径（节点数据是带 path 的 DirEntry）。"""
    path = getattr(getattr(node, "data", None), "path", None)
    return path if isinstance(path, Path) else None


class OpenFileScreen(ModalScreen[Optional[Path]]):
    """打开模组：目录树中回车或按钮确认选中的 YAML 文件。"""

    DEFAULT_CSS = """
    OpenFileScreen {
        align: center middle;
        Vertical {
            width: 92%;
            max-width: 110;
            height: 80%;
            border: thick $primary;
            background: $surface;
            padding: 1 2;
        }
        Label { height: 1; color: $text-muted; overflow-x: hidden; }
        Label.-selection { color: $accent; }
        DirectoryTree { height: 1fr; }
        Horizontal.-actions { height: 3; align-horizontal: right; }
        Button { margin-left: 1; }
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "cancel", "取消")]

    def __init__(self, root: Path) -> None:
        super().__init__()
        self._root = root if root.is_dir() else Path.cwd()
        self._selected: Optional[Path] = None

    def compose(self) -> Any:
        with Vertical() as box:
            box.border_title = "打开模组"
            yield Label("只显示 .yaml / .yml；选中文件后按回车或点击「打开」")
            yield Label(f"浏览目录：{self._root}", classes="-dir")
            yield Label("尚未选择文件", classes="-selection")
            yield ModuleDirectoryTree(self._root, id="open-file-tree")
            with Horizontal(classes="-actions"):
                yield Button("取消", classes="-cancel")
                yield Button("打开", variant="primary", classes="-open", disabled=True)

    def on_mount(self) -> None:
        self.query_one(ModuleDirectoryTree).focus()

    def _set_selected(self, path: Optional[Path]) -> None:
        self._selected = path
        label = self.query_one(".-selection", Label)
        button = self.query_one(".-open", Button)
        if path is None:
            label.update("尚未选择文件")
        else:
            label.update(f"已选择：{path.name}")
        button.disabled = path is None

    def on_directory_tree_node_highlighted(
        self, event: DirectoryTree.NodeHighlighted
    ) -> None:
        path = _node_path(event.node)
        if path is None:
            return
        if ModuleDirectoryTree._is_directory(path):
            self.query_one(".-dir", Label).update(f"浏览目录：{path}")
            self._set_selected(None)
        else:
            self._set_selected(path)

    def on_directory_tree_directory_selected(
        self, event: DirectoryTree.DirectorySelected
    ) -> None:
        path = Path(event.path)
        self.query_one(".-dir", Label).update(f"浏览目录：{path}")
        self._set_selected(None)

    def on_directory_tree_file_selected(
        self, event: DirectoryTree.FileSelected
    ) -> None:
        path = Path(event.path)
        self._set_selected(path)
        self.dismiss(path)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        classes = event.button.classes
        if "-cancel" in classes:
            self.dismiss(None)
        elif "-open" in classes:
            if self._selected is None:
                self.notify("请先选择一个 YAML 模组文件", severity="warning")
            else:
                self.dismiss(self._selected)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SaveFileScreen(ModalScreen[Optional[Path]]):
    """另存为：目录树导航到目标目录，输入文件名保存。"""

    DEFAULT_CSS = """
    SaveFileScreen {
        align: center middle;
        Vertical {
            width: 92%;
            max-width: 110;
            height: 80%;
            border: thick $primary;
            background: $surface;
            padding: 1 2;
        }
        Label { height: 1; color: $text-muted; }
        Label.-dir { height: 1; color: $success; }
        DirectoryTree { height: 1fr; }
        Horizontal { height: 3; }
        Input { width: 1fr; }
        Button { margin-left: 1; }
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "cancel", "取消")]

    def __init__(self, root: Path, default_name: str) -> None:
        super().__init__()
        self._root = root if root.is_dir() else Path.cwd()
        self._dir = self._root
        self._default_name = default_name

    def compose(self) -> Any:
        with Vertical() as box:
            box.border_title = "另存为"
            yield Label("在树中选择目标目录；仅保存为 YAML 模组文件")
            yield Label(f"保存到：{self._dir}", classes="-dir")
            yield ModuleDirectoryTree(self._root, id="save-file-tree")
            with Horizontal():
                yield Input(value=self._default_name, placeholder="文件名（.yaml）")
                yield Button("保存", variant="primary", classes="-save")
                yield Button("取消", classes="-cancel")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def _dir_label(self) -> Label:
        return self.query_one(".-dir", Label)

    def _set_dir(self, path: Path) -> None:
        if path.is_dir():
            self._dir = path
            self._dir_label().update(f"保存到：{path}")

    def on_directory_tree_directory_selected(
        self, event: DirectoryTree.DirectorySelected
    ) -> None:
        self._set_dir(Path(event.path))

    def on_directory_tree_node_highlighted(
        self, event: DirectoryTree.NodeHighlighted
    ) -> None:
        path = _node_path(event.node)
        if path is not None and ModuleDirectoryTree._is_directory(path):
            self._set_dir(path)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if "-cancel" in event.button.classes:
            self.dismiss(None)
        elif "-save" in event.button.classes:
            self._save()

    def _save(self) -> None:
        """按输入框内容提交保存路径。"""
        name = self.query_one(Input).value.strip()
        if not name:
            self.notify("请输入文件名", severity="warning")
            return
        if "/" in name or "\\" in name or name in {".", ".."}:
            self.notify("文件名不能包含目录路径", severity="warning")
            return
        if Path(name).suffix.lower() not in ModuleDirectoryTree._MODULE_SUFFIXES:
            name += ".yaml"
        self.dismiss(self._dir / name)

    def action_cancel(self) -> None:
        self.dismiss(None)


class NewModuleScreen(ModalScreen[Optional[dict[str, Any]]]):
    """新建模组：空白骨架，或复制现有模组改 id。"""

    DEFAULT_CSS = """
    NewModuleScreen {
        align: center middle;
        OptionList { width: 76; height: 20; border: thick $primary; }
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "cancel", "取消")]

    _BLANK = "__blank__"

    def compose(self) -> Any:
        options = [Option("空白骨架（README 最小模板）", id=self._BLANK)]
        directory = modules_dir()
        if directory.is_dir():
            for path in sorted(directory.glob("*.yaml")):
                data, _ = load_or_error(path)
                if data is None:
                    continue
                name = data.get("name", "")
                ident = data.get("id", path.stem)
                options.append(Option(f"复制现有：{name}（{ident}）", id=str(path)))
        yield OptionList(*options)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        from .state import blank_module_dict, deep_copy_module, generate_unique_id

        option_id = str(event.option.id or "")
        if option_id == self._BLANK:
            self.dismiss(blank_module_dict())
            return
        data, err = load_or_error(Path(option_id))
        if data is None:
            self.notify(f"无法读取模板：{err}", severity="error")
            return
        source_id = str(data.get("id", "module"))
        existing = {source_id}
        new_id = generate_unique_id(f"{source_id}_copy", existing)
        data["name"] = f"{data.get('name', '')}（副本）"
        self.dismiss(deep_copy_module(data, new_id))

    def action_cancel(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    """F1 帮助：可见性边界速查与关键约定。"""

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
        VerticalScroll {
            width: 100;
            height: 86%;
            border: thick $primary;
            background: $surface;
            padding: 1 2;
        }
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "close", "关闭"),
        ("f1", "close", "关闭"),
    ]

    def compose(self) -> Any:
        with VerticalScroll() as box:
            box.border_title = "帮助"
            yield Markdown(_HELP_MD)
            yield Label(f"[dim]{escape('Esc / F1 关闭')}[/dim]")

    def action_close(self) -> None:
        self.dismiss(None)
