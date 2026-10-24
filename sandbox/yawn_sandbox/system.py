"""容器自检：隔离状态（网络/降权/只读根/no_new_privs）与 cgroup 资源读数。"""

from __future__ import annotations

import contextlib
import os
import platform
import shutil
import sys
import time
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from typing import Any

_CGROUP = Path("/sys/fs/cgroup")
_STARTED_AT = time.time()

# 展示在 WebUI / 告诉模型的“主要库”；其余依赖只计数。
HIGHLIGHT_PACKAGES = (
    "numpy",
    "pandas",
    "matplotlib",
    "scipy",
    "sympy",
    "seaborn",
    "scikit-learn",
    "statsmodels",
    "pillow",
    "networkx",
    "openpyxl",
    "tabulate",
    "mido",
    "pretty_midi",
    "soundfile",
    "pydub",
    "music21",
    "wordcloud",
    "jieba",
    "qrcode",
    "python-dateutil",
    "pyyaml",
)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _read_int(path: Path) -> int | None:
    text = _read(path)
    if text is None or text == "max":
        return None
    try:
        return int(text.split()[0])
    except (ValueError, IndexError):
        return None


def _proc_status_field(name: str) -> str | None:
    text = _read(Path("/proc/self/status"))
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith(f"{name}:"):
            return line.split(":", 1)[1].strip()
    return None


def network_interfaces() -> list[str]:
    root = Path("/sys/class/net")
    if not root.is_dir():
        return []
    return sorted(item.name for item in root.iterdir() if item.name != "lo")


def isolation_report(*, drop_privileges: bool, socket_mode: str) -> dict[str, Any]:
    interfaces = network_interfaces()
    readonly_root = False
    with contextlib.suppress(OSError):
        readonly_root = bool(os.statvfs("/").f_flag & os.ST_RDONLY)
    cap_eff = _proc_status_field("CapEff") or ""
    no_new_privs = _proc_status_field("NoNewPrivs") == "1"
    checks = {
        "network_disabled": not interfaces,
        "privilege_drop": drop_privileges,
        "readonly_root": readonly_root,
        "no_new_privileges": no_new_privs,
        # runner 不能是 PID 1：用户代码留下的孤儿进程会过继给 PID 1，没人回收的
        # 僵尸会一直占着该群 uid 的进程配额（compose 里 init: true 解决）。
        "child_reaper": os.getpid() != 1,
    }
    return {
        "isolated": checks["network_disabled"] and checks["privilege_drop"],
        "checks": checks,
        "network_interfaces": interfaces,
        "capabilities": cap_eff,
        "seccomp": _proc_status_field("Seccomp"),
        "transport": socket_mode,
    }


def resource_report(state_dir: Path) -> dict[str, Any]:
    memory_current = _read_int(_CGROUP / "memory.current")
    memory_max = _read_int(_CGROUP / "memory.max")
    memory_peak = _read_int(_CGROUP / "memory.peak")
    pids_current = _read_int(_CGROUP / "pids.current")
    pids_max = _read_int(_CGROUP / "pids.max")
    cpu_usage_usec: int | None = None
    cpu_stat = _read(_CGROUP / "cpu.stat")
    if cpu_stat:
        for line in cpu_stat.splitlines():
            if line.startswith("usage_usec"):
                with contextlib.suppress(ValueError):
                    cpu_usage_usec = int(line.split()[1])
    cpu_quota: float | None = None
    cpu_max = _read(_CGROUP / "cpu.max")
    if cpu_max:
        quota, _, period = cpu_max.partition(" ")
        if quota != "max":
            with contextlib.suppress(ValueError, ZeroDivisionError):
                cpu_quota = round(int(quota) / int(period or "100000"), 2)
    disk: dict[str, int] = {}
    with contextlib.suppress(OSError):
        usage = shutil.disk_usage(state_dir)
        disk = {"total": usage.total, "used": usage.used, "free": usage.free}
    load: list[float] = []
    with contextlib.suppress(OSError, AttributeError):
        load = [round(item, 2) for item in os.getloadavg()]
    return {
        "memory": {"current": memory_current, "max": memory_max, "peak": memory_peak},
        "pids": {"current": pids_current, "max": pids_max},
        "cpu": {
            "usage_usec": cpu_usage_usec,
            "quota_cores": cpu_quota,
            "count": os.cpu_count(),
            "load": load,
        },
        "disk": disk,
        "uptime_seconds": int(time.time() - _STARTED_AT),
    }


@lru_cache(maxsize=1)
def installed_packages() -> dict[str, Any]:
    versions: dict[str, str] = {}
    for distribution in metadata.distributions():
        name = str(distribution.metadata["Name"] or "").strip()
        if name:
            versions[name.lower()] = distribution.version
    highlights = [
        {"name": name, "version": versions[name]}
        for name in HIGHLIGHT_PACKAGES
        if name in versions
    ]
    return {
        "python": platform.python_version(),
        "implementation": sys.implementation.name,
        "highlights": highlights,
        "total": len(versions),
        "all": dict(sorted(versions.items())),
    }


__all__ = [
    "HIGHLIGHT_PACKAGES",
    "installed_packages",
    "isolation_report",
    "network_interfaces",
    "resource_report",
]
