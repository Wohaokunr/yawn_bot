"""在沙箱子进程里执行用户代码的引导脚本（以工作区 uid 运行）。

职责：
- 把工作区目录放进 ``sys.path``，让用户能 import 自己写的模块；
- 预先配置 matplotlib（Agg 后端、中文字体、紧凑保存），``plt.show()``
  与执行结束时自动把没保存过的图存成 PNG，模型不必记得 ``savefig``；
- 像 notebook 一样回显最后一个表达式的值；
- 打印只包含用户代码帧的 traceback，不暴露引导脚本内部细节。

本文件只用标准库，独立于 runner 包运行：``python -I bootstrap.py main.py TAG``。
"""

from __future__ import annotations

import ast
import importlib.abc
import importlib.machinery
import linecache
import os
import sys
import traceback
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from types import ModuleType

USER_FILENAME = "main.py"
MAX_ECHO_CHARS = 4000

_saved_figures: set[int] = set()
_figure_counter = [0]


def _figure_name(tag: str) -> str:
    _figure_counter[0] += 1
    return f"figure-{tag}-{_figure_counter[0]}.png"


def _patch_matplotlib_figure(module: ModuleType) -> None:
    figure_class = getattr(module, "Figure", None)
    if figure_class is None or getattr(figure_class, "_yawn_patched", False):
        return
    original = figure_class.savefig

    def savefig(self: Any, *args: Any, **kwargs: Any) -> Any:
        _saved_figures.add(id(self))
        return original(self, *args, **kwargs)

    figure_class.savefig = savefig
    figure_class._yawn_patched = True


def _save_open_figures(tag: str, *, close: bool) -> list[str]:
    pyplot = sys.modules.get("matplotlib.pyplot")
    if pyplot is None:
        return []
    names: list[str] = []
    for number in list(pyplot.get_fignums()):
        figure = pyplot.figure(number)
        if id(figure) not in _saved_figures and figure.get_axes():
            name = _figure_name(tag)
            try:
                figure.savefig(name)
                names.append(name)
            except Exception as exc:  # noqa: BLE001 - 保存失败只提示
                print(f"[sandbox] 自动保存图表失败: {exc}", file=sys.stderr)
        if close:
            pyplot.close(figure)
    return names


def _patch_pyplot(module: ModuleType, tag: str) -> None:
    if getattr(module, "_yawn_patched", False):
        return

    def show(*_args: Any, **_kwargs: Any) -> None:
        for name in _save_open_figures(tag, close=True):
            print(f"[图表已保存] {name}")

    setattr(module, "show", show)  # noqa: B010 - 运行时打补丁
    setattr(module, "_yawn_patched", True)  # noqa: B010


class _PostImportFinder(importlib.abc.MetaPathFinder):
    """在 matplotlib 真正被用户导入时才打补丁，不拖慢不画图的代码。"""

    def __init__(self, tag: str) -> None:
        self.tag = tag

    def find_spec(self, fullname: str, path: Any, target: Any = None) -> Any:  # noqa: ARG002 - meta path 协议签名
        if fullname not in {"matplotlib.figure", "matplotlib.pyplot"}:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        loader = spec.loader
        original_exec = getattr(loader, "exec_module", None)
        if original_exec is None:
            return spec
        tag = self.tag

        def exec_module(module: ModuleType) -> None:
            original_exec(module)
            if fullname == "matplotlib.figure":
                _patch_matplotlib_figure(module)
            else:
                _patch_pyplot(module, tag)

        loader.exec_module = exec_module  # type: ignore[method-assign]
        return spec


def _configure_pandas() -> None:
    pandas = sys.modules.get("pandas")
    if pandas is None:
        return
    try:
        pandas.set_option("display.max_rows", 30)
        pandas.set_option("display.max_columns", 20)
        pandas.set_option("display.width", 120)
    except Exception:  # noqa: BLE001 - 只是显示偏好
        pass


def _echo(value: Any) -> None:
    if value is None:
        return
    _configure_pandas()
    try:
        text = repr(value)
    except Exception as exc:  # noqa: BLE001
        text = f"<无法显示结果: {exc}>"
    if len(text) > MAX_ECHO_CHARS:
        text = text[:MAX_ECHO_CHARS] + f"… (共 {len(text)} 字符，已截断)"
    print(text)


def _print_user_traceback(exc: BaseException) -> None:
    trace = traceback.TracebackException.from_exception(exc)
    # 去掉引导脚本自身的帧，只保留用户代码与库代码。
    frames = [frame for frame in trace.stack if frame.filename != __file__]
    trace.stack = traceback.StackSummary.from_list(frames)
    sys.stderr.write("".join(trace.format()))


def run_user_code(source: str, tag: str) -> int:
    linecache.cache[USER_FILENAME] = (
        len(source),
        None,
        [line + "\n" for line in source.splitlines()],
        USER_FILENAME,
    )
    namespace: dict[str, Any] = {"__name__": "__main__", "__file__": USER_FILENAME}
    try:
        tree = ast.parse(source, filename=USER_FILENAME)
        last_expr: ast.expr | None = None
        last_stmt = tree.body[-1] if tree.body else None
        if isinstance(last_stmt, ast.Expr):
            tree.body.pop()
            last_expr = last_stmt.value
        exec(compile(tree, USER_FILENAME, "exec"), namespace)
        if last_expr is not None:
            expression = ast.Expression(last_expr)
            _echo(eval(compile(expression, USER_FILENAME, "eval"), namespace))
    except SystemExit as exc:
        code = exc.code
        _finish(tag)
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return 1
    except SyntaxError as exc:
        sys.stderr.write("".join(traceback.format_exception_only(type(exc), exc)))
        return 1
    except BaseException as exc:  # noqa: BLE001 - 用户代码的任何异常都要报告
        _print_user_traceback(exc)
        _finish(tag)
        return 1
    _finish(tag)
    return 0


def _finish(tag: str) -> None:
    for name in _save_open_figures(tag, close=True):
        print(f"[图表已保存] {name}")
    sys.stdout.flush()


def _prepare_environment(tag: str) -> None:
    workspace = os.getcwd()
    lib_dir = os.environ.get("YAWN_SANDBOX_LIB", "")
    sys.path[:0] = [workspace, *([lib_dir] if lib_dir else [])]
    sys.meta_path.insert(0, _PostImportFinder(tag))


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: bootstrap.py CODE_FILE TAG", file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8") as handle:
        source = handle.read()
    tag = "".join(char for char in argv[2] if char.isalnum())[:12] or "run"
    _prepare_environment(tag)
    return run_user_code(source, tag)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
