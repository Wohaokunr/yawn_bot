"""模组编辑器主应用：TabbedContent 外壳 + 文件流 + 脏保护。

状态流约定：表单控件 → FieldChanged → 各 Tab 写回 draft.data →
on_data_changed（标题防抖）；dict → 控件只在 refresh_all /
切换选中实体时发生；YAML 页是唯一的「文本 → dict」入口（整体替换）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Optional

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.widgets import Button, Footer, Header, Label, TabbedContent, TabPane

from .dialogs import (
    HelpScreen,
    NewModuleScreen,
    OpenFileScreen,
    SaveFileScreen,
    SearchScreen,
)
from .schema_loader import modules_dir
from .state import ModuleDraft, SearchResult
from .tabs.clues_tab import CluesTab
from .tabs.endings_tab import EndingsTab
from .tabs.events_tab import EventsTab
from .tabs.module_tab import ModuleTab
from .tabs.monsters_tab import MonstersTab
from .tabs.npcs_tab import NpcsTab
from .tabs.playtest_tab import PlaytestTab
from .tabs.report_tab import ReportTab
from .tabs.scenes_tab import ScenesTab
from .tabs.yaml_tab import YamlTab
from .widgets import ConfirmScreen
from .yaml_io import default_header, load_or_error, save_module_file

if TYPE_CHECKING:
    from pathlib import Path

    from textual import events
    from textual.screen import Screen
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
_TAB_PLAYTEST = "tab-playtest"
_WIDE_WIDTH = 130
_COMPACT_WIDTH = 90
_SHORT_HEIGHT = 32


class ModuleEditorApp(App):
    """YawnBot 跑团模组编辑器。"""

    TITLE = "跑团模组编辑器"
    CSS_PATH = "app.tcss"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+s", "save", "保存", priority=True),
        Binding("ctrl+shift+s", "save_as", "另存为"),
        Binding("ctrl+o", "open", "打开"),
        Binding("ctrl+n", "new", "新建"),
        Binding("ctrl+tab", "next_tab", "下一页"),
        Binding("ctrl+shift+tab", "previous_tab", "上一页"),
        Binding("f5", "revalidate", "重新校验"),
        Binding("f6", "playtest", "试玩"),
        Binding("ctrl+q", "quit_guarded", "退出"),
        Binding("f1", "help", "帮助"),
        Binding("ctrl+f", "search", "搜索"),
        Binding("ctrl+d", "duplicate", "复制当前", show=False),
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
        self._playtest_tab = PlaytestTab()
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
            _TAB_PLAYTEST: self._playtest_tab,
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
            _TAB_PLAYTEST: "试玩",
        }
        self._layout_mode = "wide"
        self._base_screen: Optional[Screen] = None

    # ── 生命周期 ──────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="editor-toolbar"):
            yield Button("新建", id="toolbar-new")
            yield Button("打开", id="toolbar-open", variant="primary")
            yield Button("保存", id="toolbar-save", variant="success")
            yield Button("另存为", id="toolbar-save-as")
            yield Button("校验", id="toolbar-validate")
            yield Button("试玩", id="toolbar-playtest", variant="primary")
            yield Button("搜索", id="toolbar-search")
            yield Button("帮助", id="toolbar-help")
            yield Label("快捷键见底部", classes="-toolbar-hint")
            yield Label("", id="-layout-status", classes="-layout-status")
        with TabbedContent(initial=_TAB_MODULE):
            for tab_id, tab in self._tabs.items():
                with TabPane(self._tab_titles[tab_id], id=tab_id):
                    yield tab
        yield Footer()

    def on_mount(self) -> None:
        self._base_screen = self.screen
        self._load_initial()
        self.refresh_all()
        self._update_layout(self.size.width, self.size.height)

    def on_resize(self, event: events.Resize) -> None:
        self._update_layout(event.size.width, event.size.height)

    def _update_layout(self, width: int, height: int) -> None:
        """按真实 viewport 切换 CSS 布局，兼容最大化与窄终端。"""
        # Windows 终端最大化时可能先发一个 0×0 的过渡 resize；不要让这个
        # 瞬时尺寸把主界面切到不可恢复的窄屏布局。
        if width <= 0 or height <= 0:
            return
        if width >= _WIDE_WIDTH:
            mode = "wide"
        elif width >= _COMPACT_WIDTH:
            mode = "compact"
        else:
            mode = "narrow"
        if height < _SHORT_HEIGHT:
            mode += " short"
        self._layout_mode = mode
        target = self._base_screen or self.screen
        try:
            for class_name in ("wide", "compact", "narrow", "short"):
                target.remove_class(class_name)
            for class_name in mode.split():
                target.add_class(class_name)
            target.query_one("#-layout-status", Label).update(
                f"{width}×{height} · {mode.replace(' ', ' / ')}"
            )
        except NoMatches:  # pragma: no cover - compose 尚未完成时的 resize
            return

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
        self._playtest_tab.refresh_tab(self.draft.data)

    def _update_title(self) -> None:
        self.sub_title = self.draft.display_title()

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        pane_id = event.pane.id or ""
        if pane_id in (_TAB_YAML, _TAB_REPORT, _TAB_PLAYTEST):
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
        data, header = load_or_error(path)
        if data is None:
            self.notify(f"无法打开：{header}", severity="error", timeout=8)
            return
        self._install_draft(ModuleDraft(data, path=path, header=header or ""))
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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """处理顶部工具栏动作；分区页按钮仍由各自 Tab 处理。"""
        actions = {
            "toolbar-new": self.action_new,
            "toolbar-open": self.action_open,
            "toolbar-save": self.action_save,
            "toolbar-save-as": self.action_save_as,
            "toolbar-validate": self.action_revalidate,
            "toolbar-playtest": self.action_playtest,
            "toolbar-search": self.action_search,
            "toolbar-help": self.action_help,
        }
        action = actions.get(event.button.id or "")
        if action is not None:
            action()

    def _switch_tab(self, delta: int) -> None:
        tabbed = self.query_one(TabbedContent)
        tab_ids = list(self._tabs)
        try:
            current = tab_ids.index(tabbed.active)
        except ValueError:
            current = 0
        tabbed.active = tab_ids[(current + delta) % len(tab_ids)]

    def action_next_tab(self) -> None:
        self._switch_tab(1)

    def action_previous_tab(self) -> None:
        self._switch_tab(-1)

    def action_revalidate(self) -> None:
        self._report_tab.refresh_tab(self.draft.data)
        self.query_one(TabbedContent).active = _TAB_REPORT

    def action_playtest(self) -> None:
        self._playtest_tab.refresh_tab(self.draft.data)
        self.query_one(TabbedContent).active = _TAB_PLAYTEST

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_search(self) -> None:
        self.push_screen(SearchScreen(self.draft.data), self._search_result)

    def _search_result(self, result: Optional[SearchResult]) -> None:
        if result is None:
            return
        tab = self._tabs.get(result.tab_id)
        if tab is None:
            return
        self.query_one(TabbedContent).active = result.tab_id
        tab.locate_path(result.path)

    def action_duplicate(self) -> None:
        tabbed = self.query_one(TabbedContent)
        tab = self._tabs.get(tabbed.active)
        if tab is None or not tab.duplicate_current():
            self.notify("当前区域没有可复制的条目", severity="warning")

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
