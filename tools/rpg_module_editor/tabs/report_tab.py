"""「校验」页：引擎结构错误 + 写作规范诊断汇总。"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.containers import Vertical
from textual.widgets import Button, Label, OptionList
from textual.widgets.option_list import Option

from ..lint import run_lint  # noqa: TID252
from ..validate import (  # noqa: TID252
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    Issue,
    validate_structure,
)
from . import EditorTab

_SEVERITY_STYLE = {
    SEVERITY_ERROR: "bold red",
    SEVERITY_WARNING: "yellow",
    SEVERITY_INFO: "dim cyan",
}
_SEVERITY_ORDER = {SEVERITY_ERROR: 0, SEVERITY_WARNING: 1, SEVERITY_INFO: 2}


def collect_issues(data: dict[str, Any]) -> tuple[list[Issue], bool]:
    """运行全部诊断；返回 (问题列表, 结构是否通过)。"""
    report = validate_structure(data)
    issues = sorted(
        report.issues + run_lint(data),
        key=lambda i: _SEVERITY_ORDER.get(i.severity, 9),
    )
    return issues, report.module is not None


class ReportTab(EditorTab):
    """诊断报告页（激活或 F5 时刷新）。"""

    DEFAULT_CSS = """
    ReportTab {
        layout: vertical;
        Vertical { height: 3; }
        Label.-summary { height: 1; padding: 0 1; }
        OptionList { height: 1fr; }
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._summary = Label("", classes="-summary")
        self._list = OptionList()

    def compose(self) -> Any:
        with Vertical():
            yield Button("重新校验（F5）", classes="-refresh")
            yield self._summary
        yield self._list

    def refresh_tab(self, data: dict[str, Any]) -> None:
        issues, structure_ok = collect_issues(data)
        errors = sum(1 for i in issues if i.severity == SEVERITY_ERROR)
        warnings = sum(1 for i in issues if i.severity == SEVERITY_WARNING)
        infos = sum(1 for i in issues if i.severity == SEVERITY_INFO)
        verdict = (
            "[bold green]✓ 结构校验通过[/bold green]"
            if structure_ok
            else "[bold red]✗ 结构校验未通过[/bold red]"
        )
        self._summary.update(
            f"{verdict}｜[red]{errors} 错误[/red] · "
            f"[yellow]{warnings} 警告[/yellow] · [cyan]{infos} 提示[/cyan]"
        )
        self._list.clear_options()
        if not issues:
            self._list.add_option(Option("没有发现任何问题。"))
            return
        for issue in issues:
            line = Text.assemble(
                (f"[{issue.severity}] ", _SEVERITY_STYLE.get(issue.severity, "")),
                (f"{issue.section} › {issue.path_label}：", "bold"),
                (issue.message, ""),
            )
            if issue.hint:
                line.append(f"（{issue.hint}）", "dim")
            self._list.add_option(Option(line))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if "-refresh" in event.button.classes:
            self.refresh_tab(self.editor.draft.data)
