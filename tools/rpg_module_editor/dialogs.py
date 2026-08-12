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


class OpenFileScreen(ModalScreen[Optional[Path]]):
    """打开模组：目录树中双击 / 回车选中 .yaml 文件。"""

    DEFAULT_CSS = """
    OpenFileScreen {
        align: center middle;
        Vertical {
            width: 90;
            height: 80%;
            border: thick $primary;
            background: $surface;
            padding: 1 2;
        }
        Label { height: 1; color: $text-muted; }
        DirectoryTree { height: 1fr; }
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "cancel", "取消")]

    def __init__(self, root: Path) -> None:
        super().__init__()
        self._root = root if root.is_dir() else Path.cwd()

    def compose(self) -> Any:
        with Vertical() as box:
            box.border_title = "打开模组"
            yield Label("选中 .yaml 文件后回车打开（Esc 取消）")
            yield DirectoryTree(self._root)

    def on_directory_tree_file_selected(
        self, event: DirectoryTree.FileSelected
    ) -> None:
        self.dismiss(Path(event.path))

    def action_cancel(self) -> None:
        self.dismiss(None)


class SaveFileScreen(ModalScreen[Optional[Path]]):
    """另存为：目录树导航到目标目录，输入文件名保存。"""

    DEFAULT_CSS = """
    SaveFileScreen {
        align: center middle;
        Vertical {
            width: 90;
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
            yield Label("在树中进入目标目录（高亮目录即当前保存位置）")
            yield Label(f"保存到：{self._dir}", classes="-dir")
            yield DirectoryTree(self._root)
            with Horizontal():
                yield Input(value=self._default_name, placeholder="文件名（.yaml）")
                yield Button("保存", variant="primary", classes="-save")

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
        data = event.node.data
        if isinstance(data, Path):
            self._set_dir(data)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if "-save" not in event.button.classes:
            return
        name = self.query_one(Input).value.strip()
        if not name:
            self.notify("请输入文件名", severity="warning")
            return
        if "." not in name:
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
