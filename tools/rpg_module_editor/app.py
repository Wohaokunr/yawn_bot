"""模组编辑器主应用：TabbedContent 外壳 + 文件流 + 脏保护。

状态流约定：表单控件 → FieldChanged → 各 Tab 写回 draft.data →
on_data_changed（标题防抖）；dict → 控件只在 refresh_all /
切换选中实体时发生；YAML 页是唯一的「文本 → dict」入口（整体替换）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Optional

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.widgets import Header, TabbedContent, TabPane

from .dialogs import HelpScreen, NewModuleScreen, OpenFileScreen, SaveFileScreen
from .schema_loader import modules_dir
from .state import ModuleDraft
from .tabs.clues_tab import CluesTab
from .tabs.endings_tab import EndingsTab
from .tabs.events_tab import EventsTab
from .tabs.module_tab import ModuleTab
from .tabs.monsters_tab import MonstersTab
from .tabs.npcs_tab import NpcsTab
from .tabs.report_tab import ReportTab
from .tabs.scenes_tab import ScenesTab
from .tabs.yaml_tab import YamlTab
from .widgets import ConfirmScreen
from .yaml_io import default_header, load_or_error, save_module_file

if TYPE_CHECKING:
    from pathlib import Path

    from textual.timer import Timer

    from .tabs import EditorTab

_TAB_MODULE = "tab-module"
_TAB_SCENES = "tab-scenes"
_TAB_NPCS = "tab-npcs"
_TAB_MONSTERS = "tab-monsters"
_TAB_CLUES = "tab-clues"
_TAB_ENDINGS = "tab-endings"
_TAB_EVENTS = "tab-events"
_TAB_YAML = "tab-yaml"
_TAB_REPORT = "tab-report"


class ModuleEditorApp(App):
    """YawnBot 跑团模组编辑器。"""

    TITLE = "跑团模组编辑器"
    CSS_PATH = "app.tcss"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+s", "save", "保存", priority=True),
        Binding("ctrl+shift+s", "save_as", "另存为"),
        Binding("ctrl+o", "open", "打开"),
        Binding("ctrl+n", "new", "新建"),
        Binding("f5", "revalidate", "重新校验"),
        Binding("ctrl+q", "quit_guarded", "退出"),
        Binding("f1", "help", "帮助"),
    ]

    def __init__(self, initial_path: Optional[Path] = None) -> None:
        super().__init__()
        self.draft = ModuleDraft()
        self._initial_path = initial_path
        self._title_timer: Optional[Timer] = None
        self._module_tab = ModuleTab()
        self._scenes_tab = ScenesTab()
        self._npcs_tab = NpcsTab()
        self._monsters_tab = MonstersTab()
        self._clues_tab = CluesTab()
        self._endings_tab = EndingsTab()
        self._events_tab = EventsTab()
        self._yaml_tab = YamlTab()
        self._report_tab = ReportTab()
        # tab id → 控件；新增分区页在这里登记一行
        self._tabs: dict[str, EditorTab] = {
            _TAB_MODULE: self._module_tab,
            _TAB_SCENES: self._scenes_tab,
            _TAB_NPCS: self._npcs_tab,
            _TAB_MONSTERS: self._monsters_tab,
            _TAB_CLUES: self._clues_tab,
            _TAB_ENDINGS: self._endings_tab,
            _TAB_EVENTS: self._events_tab,
            _TAB_YAML: self._yaml_tab,
            _TAB_REPORT: self._report_tab,
        }
        self._tab_titles = {
            _TAB_MODULE: "模组",
            _TAB_SCENES: "场景",
            _TAB_NPCS: "NPC",
            _TAB_MONSTERS: "怪物",
            _TAB_CLUES: "线索",
            _TAB_ENDINGS: "结局",
            _TAB_EVENTS: "事件",
            _TAB_YAML: "YAML 源码",
            _TAB_REPORT: "校验",
        }

    # ── 生命周期 ──────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial=_TAB_MODULE):
            for tab_id, tab in self._tabs.items():
                with TabPane(self._tab_titles[tab_id], id=tab_id):
                    yield tab

    def on_mount(self) -> None:
        self._load_initial()
        self.refresh_all()

    def _load_initial(self) -> None:
        if self._initial_path is None:
            return
        data, err = load_or_error(self._initial_path)
        if data is None:
            self.notify(f"无法打开 {self._initial_path.name}：{err}", severity="error")
            return
        self._install_draft(ModuleDraft(data, path=self._initial_path))

    def _install_draft(self, draft: ModuleDraft) -> None:
        self.draft = draft
        self.refresh_all()

    # ── 状态同步 ──────────────────────────────────────────

    def refresh_all(self) -> None:
        """全部 Tab 从 dict 重填（只设值、不重建控件，不丢焦点）。"""
        for tab in self._tabs.values():
            tab.refresh_tab(self.draft.data)
        self._update_title()

    def on_data_changed(self) -> None:
        """表单写回后调用：防抖刷新标题。"""
        if self._title_timer is not None:
            self._title_timer.stop()
        self._title_timer = self.set_timer(0.3, self._update_title)

    def _update_title(self) -> None:
        self.sub_title = self.draft.display_title()

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        pane_id = event.pane.id or ""
        if pane_id in (_TAB_YAML, _TAB_REPORT):
            self._tabs[pane_id].refresh_tab(self.draft.data)

    # ── 文件流 ────────────────────────────────────────────

    def _write_to(self, path: Path) -> None:
        header = self.draft.header or default_header(
            self.draft.module_name or "未命名模组", self.draft.module_id or "unnamed"
        )
        try:
            save_module_file(path, self.draft.data, header)
        except OSError as e:
            self.notify(f"保存失败：{e}", severity="error", timeout=8)
            return
        self.draft.mark_saved(path)
        self._update_title()
        self.notify(f"已保存：{path}")

    def action_save(self) -> None:
        if self.draft.path is None:
            self.action_save_as()
            return
        self._write_to(self.draft.path)

    def action_save_as(self) -> None:
        default_name = f"{self.draft.module_id or 'module'}.yaml"
        self.push_screen(
            SaveFileScreen(modules_dir(), default_name), self._save_as_result
        )

    def _save_as_result(self, path: Optional[Path]) -> None:
        if path is None:
            return
        if path.exists():

            def _overwrite_if_confirmed(confirmed: Optional[bool]) -> None:  # noqa: FBT001
                if confirmed:
                    self._write_to(path)

            self.push_screen(
                ConfirmScreen("覆盖确认", f"{path.name} 已存在，确定覆盖吗？"),
                _overwrite_if_confirmed,
            )
            return
        self._write_to(path)

    def action_open(self) -> None:
        if self.draft.dirty:
            self.push_screen(
                ConfirmScreen(
                    "未保存的修改", "打开新文件将丢弃当前未保存的修改，继续吗？"
                ),
                self._open_if_confirmed,
            )
            return
        self._open_if_confirmed(True)  # noqa: FBT003

    def _open_if_confirmed(self, confirmed: Optional[bool]) -> None:  # noqa: FBT001
        if not confirmed:
            return
        root = modules_dir()
        if self.draft.path is not None:
            root = self.draft.path.parent
        self.push_screen(OpenFileScreen(root), self._open_result)

    def _open_result(self, path: Optional[Path]) -> None:
        if path is None:
            return
        data, err = load_or_error(path)
        if data is None:
            self.notify(f"无法打开：{err}", severity="error", timeout=8)
            return
        from .yaml_io import load_module_file

        _, header = load_module_file(path)
        self._install_draft(ModuleDraft(data, path=path, header=header))
        self.notify(f"已打开：{path.name}")

    def action_new(self) -> None:
        if self.draft.dirty:

            def _new_if_confirmed(confirmed: Optional[bool]) -> None:  # noqa: FBT001
                if confirmed:
                    self.push_screen(NewModuleScreen(), self._new_result)

            self.push_screen(
                ConfirmScreen("未保存的修改", "新建将丢弃当前未保存的修改，继续吗？"),
                _new_if_confirmed,
            )
            return
        self.push_screen(NewModuleScreen(), self._new_result)

    def _new_result(self, data: Optional[dict[str, Any]]) -> None:
        if data is None:
            return
        self._install_draft(ModuleDraft(data))
        self.notify("已新建模组，请修改 id 与名称后保存")

    # ── 其他动作 ──────────────────────────────────────────

    def action_revalidate(self) -> None:
        self._report_tab.refresh_tab(self.draft.data)
        self.query_one(TabbedContent).active = _TAB_REPORT

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_quit_guarded(self) -> None:
        if not self.draft.dirty:
            self.exit()
            return

        def _quit_if_confirmed(confirmed: Optional[bool]) -> None:  # noqa: FBT001
            if confirmed:
                self.exit()

        self.push_screen(
            ConfirmScreen("未保存的修改", "有未保存的修改，确定退出吗？"),
            _quit_if_confirmed,
        )
