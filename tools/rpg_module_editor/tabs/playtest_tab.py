"""固定种子试玩页：在编辑器草稿或磁盘版本上运行 P1-1。"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any, Optional

from textual import work
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    Input,
    Label,
    Select,
    TabbedContent,
    TabPane,
    TextArea,
)
from textual.worker import Worker, WorkerState

from tools.rpg_playtest import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_STATES,
    GENERIC_ENDINGS,
    SearchConfig,
    SearchResult,
    load_module,
    render_result_json,
    render_result_text,
    search_module,
    search_module_data,
)

from ..yaml_io import load_or_error  # noqa: TID252
from . import EditorTab

if TYPE_CHECKING:
    from pathlib import Path

_SOURCE_DRAFT = "draft"
_SOURCE_SAVED = "saved"
_TRACE_PANE = "playtest-trace-pane"
_JSON_PANE = "playtest-json-pane"


def _invalid_result(config: SearchConfig, error: object) -> SearchResult:
    return SearchResult(
        ok=False,
        reason="invalid_module",
        message=f"模组读取或校验失败：{error}",
        seed=config.seed,
        target_ending=config.ending_id,
        max_depth=config.max_depth,
        max_states=config.max_states,
    )


def _ending_options(data: Optional[dict[str, Any]]) -> list[tuple[str, str]]:
    """Build target options without requiring a valid schema first."""
    if not isinstance(data, dict):
        return []
    options: list[tuple[str, str]] = []
    seen: set[str] = set()
    endings = data.get("endings")
    if isinstance(endings, list):
        for item in endings:
            if not isinstance(item, dict):
                continue
            ending_id = item.get("id")
            if not isinstance(ending_id, str) or not ending_id or ending_id in seen:
                continue
            seen.add(ending_id)
            name = item.get("name")
            label = (
                f"{name}（{ending_id}）"
                if isinstance(name, str) and name
                else ending_id
            )
            options.append((label, ending_id))
    if data.get("generic_endings", True):
        for ending_id, condition, _outcome in GENERIC_ENDINGS:
            if ending_id in seen:
                continue
            seen.add(ending_id)
            options.append((f"通用：{ending_id}（{condition}）", ending_id))
    return options


def _parse_int(
    widget: Input, label: str, *, allow_blank: bool = False
) -> Optional[int]:
    text = widget.value.strip()
    if not text and allow_blank:
        return None
    if not text:
        raise ValueError(f"{label}不能为空")
    try:
        return int(text)
    except ValueError as error:
        raise ValueError(f"{label}必须是整数") from error


class PlaytestTab(EditorTab):
    """P1-1 固定种子、有界 BFS 试玩控制台。"""

    DEFAULT_CSS = """
    PlaytestTab { layout: vertical; }
    PlaytestTab .-intro { height: auto; color: $text-muted; margin-bottom: 1; }
    PlaytestTab .-settings { height: auto; min-height: 8; }
    PlaytestTab .-setting { width: 1fr; min-width: 12; margin-right: 1; }
    PlaytestTab .-setting Label { height: 1; color: $text-muted; }
    PlaytestTab Input { width: 1fr; min-width: 8; }
    PlaytestTab Select { width: 1fr; min-width: 16; }
    PlaytestTab .-source-note { height: 1; color: $text-muted; margin-bottom: 1; }
    PlaytestTab .-actions { height: 3; align-vertical: middle; }
    PlaytestTab .-actions Button { margin-right: 1; }
    PlaytestTab .-summary { height: auto; min-height: 2; padding: 0 1; }
    PlaytestTab .-freshness { height: 1; color: $warning; }
    PlaytestTab #playtest-output { height: 1fr; min-height: 10; }
    PlaytestTab #playtest-trace, PlaytestTab #playtest-json { height: 1fr; }
    Screen.narrow PlaytestTab .-settings {
        layout: vertical;
        overflow-y: auto;
        max-height: 15;
    }
    Screen.narrow PlaytestTab .-settings-row { layout: vertical; height: auto; }
    Screen.narrow PlaytestTab .-setting { width: 1fr; margin-right: 0; }
    Screen.short PlaytestTab .-intro { display: none; }
    Screen.short PlaytestTab .-settings { max-height: 11; }
    """

    def __init__(self) -> None:
        super().__init__()
        # Textual Input / TextArea 在构造时会访问当前 App 的 reactive context；
        # 这些控件必须延迟到 compose()，否则编辑器在测试或启动阶段尚未挂载时
        # 会触发 NoActiveAppError。
        self._source: Select
        self._ending: Select
        self._seed: Input
        self._players: Input
        self._max_depth: Input
        self._max_states: Input
        self._source_note: Label
        self._party_hint: Label
        self._summary: Label
        self._freshness: Label
        self._run_button: Button
        self._copy_button: Button
        self._trace: TextArea
        self._json: TextArea
        self._last_data: dict[str, Any] = {}
        self._result: Optional[SearchResult] = None
        self._result_json = ""
        self._result_source = _SOURCE_DRAFT
        self._result_revision = ""
        self._run_token = 0
        self._active_token = 0
        self._worker: Optional[Worker[SearchResult]] = None
        self._active_config: Optional[SearchConfig] = None
        self._refreshing = False

    def compose(self) -> Any:
        self._source = Select(
            [("当前草稿（内存）", _SOURCE_DRAFT)],
            prompt="选择数据源",
            allow_blank=False,
            value=_SOURCE_DRAFT,
            id="playtest-source",
        )
        self._ending = Select(
            [],
            prompt="选择目标结局",
            allow_blank=True,
            id="playtest-ending",
        )
        self._seed = Input("0", placeholder="整数 seed", id="playtest-seed")
        self._players = Input("", placeholder="留空=最少人数", id="playtest-players")
        self._max_depth = Input(
            str(DEFAULT_MAX_DEPTH), placeholder="步数上限", id="playtest-max-depth"
        )
        self._max_states = Input(
            str(DEFAULT_MAX_STATES), placeholder="状态数上限", id="playtest-max-states"
        )
        self._source_note = Label("", markup=False, classes="-source-note")
        self._party_hint = Label("", markup=False, classes="-source-note")
        self._summary = Label("尚未运行试玩。", markup=False, classes="-summary")
        self._freshness = Label("", markup=False, classes="-freshness")
        self._run_button = Button("运行试玩", variant="primary", id="playtest-run")
        self._copy_button = Button("复制 JSON", id="playtest-copy", disabled=True)
        self._trace = TextArea(
            "尚未运行试玩。",
            read_only=True,
            soft_wrap=False,
            show_line_numbers=False,
            id="playtest-trace",
        )
        self._json = TextArea(
            "",
            read_only=True,
            soft_wrap=False,
            show_line_numbers=True,
            id="playtest-json",
        )
        yield Label(
            "固定 seed 的离线有界搜索；不会启动 NoneBot、ORM、LLM，"
            "也不会修改在线 RPG。",
            classes="-intro",
        )
        with Vertical(classes="-settings"):
            with Horizontal(classes="-settings-row"):
                with Vertical(classes="-setting"):
                    yield Label("数据源")
                    yield self._source
                with Vertical(classes="-setting"):
                    yield Label("目标结局")
                    yield self._ending
                with Vertical(classes="-setting"):
                    yield Label("seed")
                    yield self._seed
                with Vertical(classes="-setting"):
                    yield Label("玩家人数")
                    yield self._players
            with Horizontal(classes="-settings-row"):
                with Vertical(classes="-setting"):
                    yield Label("最大深度")
                    yield self._max_depth
                with Vertical(classes="-setting"):
                    yield Label("最大状态数")
                    yield self._max_states
                with Vertical(classes="-setting"):
                    yield self._party_hint
        yield self._source_note
        with Horizontal(classes="-actions"):
            yield self._run_button
            yield self._copy_button
        yield self._summary
        yield self._freshness
        with TabbedContent(initial=_TRACE_PANE, id="playtest-output"):
            with TabPane("轨迹", id=_TRACE_PANE):
                yield self._trace
            with TabPane("JSON", id=_JSON_PANE):
                yield self._json

    def on_mount(self) -> None:
        self.refresh_tab(self.editor.draft.data)

    def _source_value(self) -> str:
        value = self._source.value
        return value if isinstance(value, str) else _SOURCE_DRAFT

    def _source_data(
        self, data: dict[str, Any], source: str
    ) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        if source == _SOURCE_DRAFT:
            return data, None
        path = self.editor.draft.path
        if path is None:
            return None, "当前模组尚未保存，不能使用磁盘版本。"
        saved, error = load_or_error(path)
        if saved is None:
            return None, f"磁盘版本读取失败：{error}"
        return saved, None

    def _refresh_source_options(self) -> None:
        current = self._source_value()
        options: list[tuple[str, str]] = [("当前草稿（内存）", _SOURCE_DRAFT)]
        if self.editor.draft.path is not None:
            options.append(("已保存文件", _SOURCE_SAVED))
        values = {value for _label, value in options}
        self._source.set_options(options)
        self._source.value = current if current in values else _SOURCE_DRAFT

    def _refresh_endings(self, data: Optional[dict[str, Any]]) -> None:
        current = self._ending.value
        options = _ending_options(data)
        values = {value for _label, value in options}
        self._ending.set_options(options)
        if isinstance(current, str) and current in values:
            self._ending.value = current
        elif options:
            self._ending.value = options[0][1]
        else:
            self._ending.value = Select.NULL

    def _refresh_staleness(self) -> None:
        if self._result is None or self._result_source != _SOURCE_DRAFT:
            self._freshness.update("")
            return
        if self.editor.draft.serialize() != self._result_revision:
            self._freshness.update("⚠ 结果基于试玩开始时的草稿；当前草稿已经发生变化。")
        else:
            self._freshness.update("")

    def refresh_tab(self, data: dict[str, Any]) -> None:
        self._last_data = data
        if not self.is_mounted:
            return
        self._refreshing = True
        try:
            self._refresh_source_options()
            source = self._source_value()
            source_data, error = self._source_data(data, source)
            if error:
                self._source_note.update(error)
            elif source == _SOURCE_DRAFT:
                self._source_note.update("草稿源：当前内存内容，未保存修改也会参与试玩。")
            else:
                self._source_note.update(f"磁盘源：{self.editor.draft.path}")
            self._refresh_endings(source_data if error is None else None)
            source_data = source_data or data
            minimum = source_data.get("min_players", "?")
            maximum = source_data.get("max_players", "?")
            self._party_hint.update(f"人数范围：{minimum}-{maximum}；人数留空使用最少人数")
        finally:
            self._refreshing = False
        self._refresh_staleness()

    def _config_from_inputs(self) -> SearchConfig:
        ending = self._ending.value
        if not isinstance(ending, str) or not ending:
            raise ValueError("请先选择目标结局")
        seed = _parse_int(self._seed, "seed")
        max_depth = _parse_int(self._max_depth, "最大深度")
        max_states = _parse_int(self._max_states, "最大状态数")
        players = _parse_int(self._players, "玩家人数", allow_blank=True)
        assert seed is not None and max_depth is not None and max_states is not None
        return SearchConfig(
            seed=seed,
            ending_id=ending,
            players=players,
            max_depth=max_depth,
            max_states=max_states,
        )

    def _set_running(self, *, running: bool) -> None:
        self._run_button.disabled = running
        self._source.disabled = running
        self._ending.disabled = running
        self._seed.disabled = running
        self._players.disabled = running
        self._max_depth.disabled = running
        self._max_states.disabled = running

    def _start_playtest(self) -> None:
        if self._worker is not None and not self._worker.is_finished:
            self.notify("试玩正在运行，请等待当前搜索完成。", severity="warning")
            return
        try:
            config = self._config_from_inputs()
        except ValueError as error:
            self._summary.update(f"试玩参数错误：{error}")
            return
        source = self._source_value()
        path = self.editor.draft.path
        if source == _SOURCE_SAVED and path is None:
            self._summary.update("试玩参数错误：当前模组尚未保存，不能使用磁盘版本。")
            return
        data = copy.deepcopy(self._last_data) if source == _SOURCE_DRAFT else None
        revision = self.editor.draft.serialize() if source == _SOURCE_DRAFT else ""
        self._run_token += 1
        self._active_token = self._run_token
        self._result = None
        self._result_json = ""
        self._result_source = source
        self._result_revision = revision
        self._active_config = config
        self._copy_button.disabled = True
        self._trace.load_text("试玩运行中，请稍候……")
        self._json.load_text("")
        self._summary.update(
            f"试玩运行中：seed={config.seed}，目标结局={config.ending_id}……"
        )
        self._freshness.update("")
        self._set_running(running=True)
        self._worker = self._run_search(
            self._active_token,
            source,
            data,
            path,
            config,
        )

    @work(
        name="rpg-playtest",
        group="rpg-playtest",
        exclusive=True,
        exit_on_error=False,
        thread=True,
    )
    def _run_search(
        self,
        _token: int,
        source: str,
        data: Optional[dict[str, Any]],
        path: Optional[Path],
        config: SearchConfig,
    ) -> SearchResult:
        if source == _SOURCE_SAVED:
            if path is None:
                return _invalid_result(config, "当前模组尚未保存")
            try:
                module = load_module(path)
            except Exception as error:  # noqa: BLE001
                return _invalid_result(config, error)
            return search_module(module, config)
        return search_module_data(data or {}, config)

    def _finish_result(self, result: SearchResult) -> None:
        self._result = result
        self._result_json = render_result_json(result, indent=2)
        self._trace.load_text(render_result_text(result))
        self._json.load_text(self._result_json)
        status = "试玩成功" if result.ok else f"试玩失败 [{result.reason}]"
        self._summary.update(
            f"{status}：{result.message}；seed={result.seed}；"
            f"探索 {result.explored_states} / 生成 {result.generated_states}；"
            f"目标结局={result.target_ending}"
        )
        self._copy_button.disabled = False
        self._set_running(running=False)
        self._refresh_staleness()

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if self._worker is None or event.worker is not self._worker:
            return
        if event.state is WorkerState.SUCCESS:
            result = event.worker.result
            if result is None:
                result = _invalid_result(
                    SearchConfig(seed=0, ending_id=""), "试玩 worker 没有返回结果"
                )
            self._finish_result(result)
        elif event.state is WorkerState.ERROR:
            config = self._active_config or SearchConfig(seed=0, ending_id="")
            self._finish_result(
                _invalid_result(config, event.worker.error or "试玩 worker 失败")
            )
        elif event.state is WorkerState.CANCELLED:
            self._summary.update("试玩已取消。")
            self._set_running(running=False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "playtest-run":
            self._start_playtest()
        elif event.button.id == "playtest-copy":
            if not self._result_json:
                self.notify("当前没有可复制的试玩结果。", severity="warning")
                return
            self.app.copy_to_clipboard(self._result_json)
            self.notify("JSON 已复制到 Textual 剪贴板。")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.control is self._source and not self._refreshing:
            self.refresh_tab(self._last_data)
