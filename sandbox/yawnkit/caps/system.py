"""system 包：这台虚拟电脑本身的情况（配置、已装的库与命令、磁盘占用、清理、可用能力自检）。"""

from __future__ import annotations

import importlib.metadata
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any

from . import CapError, cap, clip_list, human, ref


@cap("system.info")
def info() -> dict[str, Any]:
    usage = shutil.disk_usage(".")
    cpus = os.cpu_count()
    memory = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal"):
                memory = human(int(line.split()[1]) * 1024)
                break
    except OSError:
        pass
    return {
        "python": sys.version.split()[0],
        "os": platform.platform(),
        "cpus": cpus,
        "memory": memory,
        "disk_free": human(usage.free),
        "workspace": str(Path.cwd()),
    }


@cap("system.packages")
def packages(name: str = "") -> dict[str, Any]:
    rows = sorted(
        {
            f"{d.metadata['Name']}=={d.version}"
            for d in importlib.metadata.distributions()
            if d.metadata["Name"]
        },
        key=str.lower,
    )
    if name:
        rows = [r for r in rows if str(name).lower() in r.lower()]
    extra = Path(".pylib")
    return {
        **clip_list(rows, 200),
        "user_installed": sorted(
            p.name for p in extra.iterdir() if p.suffix == ".dist-info"
        )[:50]
        if extra.exists()
        else [],
    }


@cap("system.commands")
def commands() -> dict[str, Any]:
    """能在 run_shell 里用的常见命令有哪些。"""

    wanted = [
        "bash",
        "python3",
        "pip",
        "git",
        "curl",
        "wget",
        "zip",
        "unzip",
        "tar",
        "xz",
        "gzip",
        "jq",
        "tree",
        "file",
        "bc",
        "sed",
        "awk",
        "grep",
        "find",
        "sqlite3",
        "ffmpeg",
        "convert",
        "node",
    ]
    return {
        "available": [c for c in wanted if shutil.which(c)],
        "missing": [c for c in wanted if not shutil.which(c)],
    }


@cap("system.disk")
def disk(folder: str = ".", top: int = 15) -> dict[str, Any]:
    root = Path(str(folder).removeprefix("sandbox:") or ".")
    if root.is_absolute() or ".." in root.parts:
        raise CapError("路径要用工作区里的相对路径")
    sizes: dict[str, int] = {}
    total = 0
    for item in root.rglob("*"):
        if item.is_file() and not item.is_symlink():
            size = item.stat().st_size
            total += size
            first = item.relative_to(root).parts[0]
            sizes[first] = sizes.get(first, 0) + size
    ordered = sorted(sizes.items(), key=lambda kv: -kv[1])[: max(1, min(int(top), 100))]
    return {
        "total": human(total),
        "largest": [{"path": ref(root / k), "size": human(v)} for k, v in ordered],
    }


@cap("system.clean")
def clean(what: str = "cache") -> dict[str, Any]:
    """清理：cache（__pycache__/临时上传/matplotlib 缓存）、out（out/ 下全部产物）、pylib（自己装的库）。"""

    targets: list[Path] = []
    if what == "cache":
        targets = (
            [p for p in Path(".").rglob("__pycache__") if p.is_dir()]
            + list(Path(".").glob(".upload-*"))
            + [Path(".cache")]
        )
    elif what == "out":
        targets = [Path("out")]
    elif what == "pylib":
        targets = [Path(".pylib")]
    else:
        raise CapError("what 只能是 cache/out/pylib")
    freed = 0
    removed = 0
    for target in targets:
        if not target.exists():
            continue
        if target.is_dir():
            freed += sum(p.stat().st_size for p in target.rglob("*") if p.is_file())
            shutil.rmtree(target, ignore_errors=True)
        else:
            freed += target.stat().st_size
            target.unlink(missing_ok=True)
        removed += 1
    return {"removed": removed, "freed": human(freed)}


@cap("system.selftest")
def selftest() -> dict[str, Any]:
    """检查各能力包依赖的库是否都在。"""

    checks = {
        "image": "PIL",
        "pdf": "pypdf",
        "doc": "docx",
        "data": "pandas",
        "excel": "openpyxl",
        "chart": "matplotlib",
        "math": "sympy",
        "audio": "soundfile",
        "text": "jieba",
        "wordcloud": "wordcloud",
        "qrcode": "qrcode",
        "yaml": "yaml",
    }
    result = {}
    for pack, module in checks.items():
        try:
            __import__(module)
            result[pack] = True
        except ImportError:
            result[pack] = False
    return {"ok": all(result.values()), "packs": result}
