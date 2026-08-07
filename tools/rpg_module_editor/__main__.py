"""命令行入口：

uv run python -m tools.rpg_module_editor [path.yaml]   # 打开编辑器
uv run python -m tools.rpg_module_editor --check FILE  # 无界面校验报告
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path
from typing import Optional

_SEVERITY_ORDER = {"ERROR": 0, "WARNING": 1, "INFO": 2}


def _reconfigure_stdio() -> None:
    """Windows 控制台 UTF-8 守护（旧 conhost 下避免中文乱码崩溃）。"""
    for stream in (sys.stdout, sys.stderr):
        # 非标准流（无 reconfigure）直接放过
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(Exception):
            reconfigure(encoding="utf-8", errors="replace")


def run_check(path: Path) -> int:
    """无界面校验：结构错误（引擎口径）+ 写作规范诊断；有 ERROR 返回 1。"""
    from .lint import run_lint
    from .validate import validate_structure
    from .yaml_io import load_or_error

    data, error = load_or_error(path)
    if data is None:
        print(f"[ERROR] 读取失败：{error}")  # noqa: T201
        return 1
    report = validate_structure(data)
    issues = report.issues + run_lint(data)
    issues.sort(key=lambda i: _SEVERITY_ORDER.get(i.severity, 9))
    for issue in issues:
        hint = f"（{issue.hint}）" if issue.hint else ""
        print(  # noqa: T201
            f"[{issue.severity}] {issue.section} › {issue.path_label}："
            f"{issue.message}{hint}"
        )
    errors = sum(1 for i in issues if i.severity == "ERROR")
    warnings = sum(1 for i in issues if i.severity == "WARNING")
    infos = sum(1 for i in issues if i.severity == "INFO")
    verdict = "未通过" if errors else "通过"
    print(  # noqa: T201
        f"—— {path.name} 结构校验{verdict}："
        f"{errors} 错误 / {warnings} 警告 / {infos} 提示"
    )
    return 1 if errors else 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rpg_module_editor", description="YawnBot 跑团模组编辑器（TUI）"
    )
    parser.add_argument("path", nargs="?", help="要打开的模组 YAML 文件")
    parser.add_argument(
        "--check",
        metavar="FILE",
        help="无界面校验指定模组并打印报告（退出码反映错误数）",
    )
    args = parser.parse_args(argv)
    _reconfigure_stdio()
    if args.check:
        return run_check(Path(args.check))

    from .app import ModuleEditorApp

    app = ModuleEditorApp(initial_path=Path(args.path) if args.path else None)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
