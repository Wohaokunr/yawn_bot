"""单次执行的资源限额：请求值 → 夹到 runner 上限 → 子进程 rlimit。"""

from __future__ import annotations

import resource
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Ceilings

DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MEMORY_MB = 1024
DEFAULT_OUTPUT_BYTES = 64 * 1024
DEFAULT_FILE_MB = 32
DEFAULT_PROCESSES = 32
DEFAULT_OPEN_FILES = 128
DEFAULT_DISK_QUOTA_MB = 200
DEFAULT_MAX_FILES = 2000


@dataclass(frozen=True, slots=True)
class RunLimits:
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    cpu_seconds: int = DEFAULT_TIMEOUT_SECONDS
    memory_mb: int = DEFAULT_MEMORY_MB
    output_bytes: int = DEFAULT_OUTPUT_BYTES
    file_mb: int = DEFAULT_FILE_MB
    processes: int = DEFAULT_PROCESSES
    open_files: int = DEFAULT_OPEN_FILES
    disk_quota_mb: int = DEFAULT_DISK_QUOTA_MB
    max_files: int = DEFAULT_MAX_FILES

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


def _pick(raw: dict[str, Any], key: str, default: int, ceiling: int, minimum: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        value = default
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, ceiling))


def resolve_limits(raw: dict[str, Any] | None, ceilings: Ceilings) -> RunLimits:
    """把请求里的限额规整到 [下限, 上限]；未知字段忽略，类型错误回落默认。"""

    data = raw if isinstance(raw, dict) else {}
    timeout = _pick(
        data,
        "timeout_seconds",
        DEFAULT_TIMEOUT_SECONDS,
        ceilings.timeout_seconds,
        1,
    )
    return RunLimits(
        timeout_seconds=timeout,
        # CPU 时间默认与墙钟时限一致；多线程程序会更早触发 SIGXCPU。
        cpu_seconds=_pick(data, "cpu_seconds", timeout, ceilings.cpu_seconds, 1),
        memory_mb=_pick(data, "memory_mb", DEFAULT_MEMORY_MB, ceilings.memory_mb, 64),
        output_bytes=_pick(
            data, "output_bytes", DEFAULT_OUTPUT_BYTES, ceilings.output_bytes, 1024
        ),
        file_mb=_pick(data, "file_mb", DEFAULT_FILE_MB, ceilings.file_mb, 1),
        processes=_pick(data, "processes", DEFAULT_PROCESSES, ceilings.processes, 1),
        open_files=_pick(
            data, "open_files", DEFAULT_OPEN_FILES, ceilings.open_files, 16
        ),
        disk_quota_mb=_pick(
            data,
            "disk_quota_mb",
            DEFAULT_DISK_QUOTA_MB,
            ceilings.disk_quota_mb,
            1,
        ),
        max_files=_pick(data, "max_files", DEFAULT_MAX_FILES, ceilings.max_files, 10),
    )


def apply_child_limits(limits: RunLimits, *, limit_processes: bool) -> None:
    """在子进程 exec 之前调用（preexec_fn）。只做 setrlimit，不分配锁。

    ``limit_processes`` 只在切换到独立 uid 时开启：RLIMIT_NPROC 按 uid 计数，
    开发环境同 uid 运行时设置它会误伤 runner 自己。
    """

    memory = limits.memory_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    # 软限先到发 SIGXCPU，留 2 秒硬限兜底 SIGKILL。
    resource.setrlimit(
        resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds + 2)
    )
    file_bytes = limits.file_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
    resource.setrlimit(
        resource.RLIMIT_NOFILE, (limits.open_files, limits.open_files)
    )
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if limit_processes:
        resource.setrlimit(
            resource.RLIMIT_NPROC, (limits.processes, limits.processes)
        )


__all__ = ["RunLimits", "apply_child_limits", "resolve_limits"]
