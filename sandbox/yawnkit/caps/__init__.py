"""yawnkit.caps：虚拟电脑里的现成能力（能力包），Bot 的 ``computer_do`` 工具调用它们。

每个能力是一个带名字的函数（``包名.能力名``），参数是关键字参数，返回可 JSON 化的
dict。Bot 生成的代码只有一行::

    from yawnkit.caps import run; run("image.resize", {"path": "a.png", "width": 800})

``run`` 把结果包在 ``<<YAWNKIT_RESULT>>`` 标记里打印出来，Bot 据此解析；生成的文件
照常由 runner 的文件快照报告给 Bot。参数说明（给模型看的）在 Bot 侧的
``yawn_agent/computer_kits.py``，两边的能力名由测试保证一致。

约定：
- 路径都是工作区里的相对路径；输出默认放在 ``out/`` 下，可用 ``output`` 指定；
- 出错抛 ``CapError``（会变成 ``{"ok": false, "error": ...}``），其他异常同样兜住；
- 结果里的长列表/长文本自己截断，别把几 MB 的内容打印出来。
"""

from __future__ import annotations

import importlib
import json
import sys
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

MARK = "<<YAWNKIT_RESULT>>"
PACKS = (
    "files",
    "archive",
    "net",
    "image",
    "pdf",
    "doc",
    "data",
    "text",
    "dev",
    "audio",
    "apk",
    "math",
    "system",
)
_MODULES = {
    "files": "files",
    "archive": "archive",
    "net": "net",
    "image": "image",
    "pdf": "pdf",
    "doc": "doc",
    "data": "data",
    "text": "text",
    "dev": "dev",
    "audio": "audio",
    "apk": "apk",
    "math": "mathx",
    "system": "system",
}
REGISTRY: dict[str, Callable[..., dict[str, Any]]] = {}
MAX_TEXT = 4000
MAX_ITEMS = 60


class CapError(Exception):
    """能力的预期错误（文件不存在、参数不合理……），消息直接给模型看。"""


def cap(
    name: str,
) -> Callable[[Callable[..., dict[str, Any]]], Callable[..., dict[str, Any]]]:
    def register(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        REGISTRY[name] = fn
        return fn

    return register


def load_all() -> dict[str, Callable[..., dict[str, Any]]]:
    for module in _MODULES.values():
        importlib.import_module(f"{__name__}.{module}")
    return REGISTRY


def names() -> list[str]:
    return sorted(load_all())


# ── 共用小工具 ────────────────────────────────────────────────────────────


def src(path: Any, *, kind: str = "文件") -> Path:
    text = str(path or "").strip()
    text = text.removeprefix("sandbox:")
    if not text:
        raise CapError(f"缺少{kind}路径")
    target = Path(text)
    if target.is_absolute() or ".." in target.parts:
        raise CapError("路径要用工作区里的相对路径")
    if not target.exists():
        raise CapError(f"找不到{kind}：{text}")
    return target


def dest(output: Any, default: str) -> Path:
    """输出路径：给了 output 就用它（目录则拼上默认文件名），否则放在 out/ 下。"""

    text = str(output or "").strip()
    text = text.removeprefix("sandbox:")
    if text:
        target = Path(text)
        if target.is_absolute() or ".." in target.parts:
            raise CapError("输出路径要用工作区里的相对路径")
        if text.endswith("/") or target.is_dir():
            target = target / Path(default).name
    else:
        target = Path("out") / default
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def ref(path: Path | str) -> str:
    return f"sandbox:{Path(path).as_posix()}"


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def clip_text(text: str, limit: int = MAX_TEXT) -> dict[str, Any]:
    return {"text": text[:limit], "chars": len(text), "truncated": len(text) > limit}


def clip_list(items: list[Any], limit: int = MAX_ITEMS) -> dict[str, Any]:
    return {
        "items": items[:limit],
        "count": len(items),
        "truncated": len(items) > limit,
    }


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "gb18030", "big5", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


# ── 入口 ──────────────────────────────────────────────────────────────────


def call(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    registry = load_all()
    fn = registry.get(name)
    if fn is None:
        return {"ok": False, "error": f"没有能力 {name}"}
    try:
        result = fn(**(args or {}))
    except CapError as exc:
        return {"ok": False, "error": str(exc)}
    except TypeError as exc:
        return {"ok": False, "error": f"参数不对：{exc}"}
    except Exception as exc:  # noqa: BLE001 - 能力失败要变成结构化结果
        frames = traceback.extract_tb(exc.__traceback__)
        where = (
            f"{Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else ""
        )
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            "where": where,
        }
    return {"ok": True, "result": result}


def run(name: str, args: dict[str, Any] | None = None) -> None:
    payload = call(name, args)
    text = json.dumps(payload, ensure_ascii=False, default=str)
    sys.stdout.write(f"\n{MARK}{text}{MARK}\n")
    sys.stdout.flush()


__all__ = [
    "MARK",
    "PACKS",
    "REGISTRY",
    "CapError",
    "as_list",
    "call",
    "cap",
    "clip_list",
    "clip_text",
    "dest",
    "human",
    "load_all",
    "names",
    "read_text",
    "ref",
    "run",
    "src",
]
