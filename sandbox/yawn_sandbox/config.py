"""Runner 进程级配置：全部来自环境变量，带硬上限。

Bot 每次请求会带上希望的限额，runner 用这里的上限再夹一次；即使 Bot 被攻破
或配置写错，单次执行也不可能超过容器运维者设定的天花板。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_STATE_DIR = "/var/lib/yawnbot-sandbox"
DEFAULT_SOCKET = "/run/yawnbot-sandbox/runner.sock"
DEFAULT_JOBS_DIR = "/tmp/yawnbot-sandbox-jobs"
DEFAULT_PYTHON = "/opt/yawnbot-sandbox/venv/bin/python"
DEFAULT_LIB_DIR = "/opt/yawnbot-sandbox/lib"


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Ceilings:
    """单次执行允许的最大值；请求里的值只能更小。"""

    timeout_seconds: int = 120
    cpu_seconds: int = 120
    memory_mb: int = 2048
    output_bytes: int = 256 * 1024
    file_mb: int = 64
    processes: int = 64
    open_files: int = 256
    disk_quota_mb: int = 1024
    max_files: int = 5000


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    state_dir: Path
    jobs_dir: Path
    socket_path: Path | None
    socket_gid: int | None
    http_host: str | None
    http_port: int
    token: str
    python: str
    lib_dir: Path
    concurrency: int
    queue_limit: int
    queue_wait_seconds: int
    history_limit: int
    retention_days: int
    uid_base: int
    uid_span: int
    max_upload_mb: int
    ceilings: Ceilings
    require_isolation: bool

    @property
    def workspaces_dir(self) -> Path:
        return self.state_dir / "workspaces"

    @property
    def meta_dir(self) -> Path:
        return self.state_dir / "state"


def load_config() -> RunnerConfig:
    socket_raw = os.environ.get("SANDBOX_SOCKET", DEFAULT_SOCKET).strip()
    gid_raw = os.environ.get("SANDBOX_SOCKET_GID", "").strip()
    http_raw = os.environ.get("SANDBOX_HTTP", "").strip()
    http_host: str | None = None
    http_port = 8790
    if http_raw:
        host, _, port = http_raw.rpartition(":")
        http_host = host or "127.0.0.1"
        try:
            http_port = int(port)
        except ValueError:
            http_port = 8790
    ceilings = Ceilings(
        timeout_seconds=_env_int("SANDBOX_MAX_TIMEOUT", 120, minimum=5, maximum=600),
        cpu_seconds=_env_int("SANDBOX_MAX_CPU", 120, minimum=5, maximum=600),
        memory_mb=_env_int("SANDBOX_MAX_MEMORY_MB", 2048, minimum=128, maximum=16384),
        output_bytes=_env_int(
            "SANDBOX_MAX_OUTPUT_BYTES", 256 * 1024, minimum=4096, maximum=4 * 1024 * 1024
        ),
        file_mb=_env_int("SANDBOX_MAX_FILE_MB", 64, minimum=1, maximum=1024),
        processes=_env_int("SANDBOX_MAX_PROCESSES", 64, minimum=4, maximum=512),
        open_files=_env_int("SANDBOX_MAX_OPEN_FILES", 256, minimum=32, maximum=4096),
        disk_quota_mb=_env_int("SANDBOX_DISK_QUOTA_MB", 1024, minimum=8, maximum=65536),
        max_files=_env_int("SANDBOX_MAX_FILES", 5000, minimum=50, maximum=200000),
    )
    return RunnerConfig(
        state_dir=Path(os.environ.get("SANDBOX_STATE_DIR", DEFAULT_STATE_DIR)),
        jobs_dir=Path(os.environ.get("SANDBOX_JOBS_DIR", DEFAULT_JOBS_DIR)),
        socket_path=Path(socket_raw) if socket_raw and not http_raw else None,
        socket_gid=int(gid_raw) if gid_raw.isdigit() else None,
        http_host=http_host,
        http_port=http_port,
        token=os.environ.get("SANDBOX_TOKEN", "").strip(),
        python=os.environ.get("SANDBOX_PYTHON", DEFAULT_PYTHON),
        lib_dir=Path(os.environ.get("SANDBOX_LIB_DIR", DEFAULT_LIB_DIR)),
        concurrency=_env_int("SANDBOX_CONCURRENCY", 2, minimum=1, maximum=16),
        queue_limit=_env_int("SANDBOX_QUEUE_LIMIT", 8, minimum=0, maximum=128),
        queue_wait_seconds=_env_int("SANDBOX_QUEUE_WAIT", 30, minimum=1, maximum=300),
        history_limit=_env_int("SANDBOX_HISTORY_LIMIT", 2000, minimum=50, maximum=50000),
        retention_days=_env_int("SANDBOX_RETENTION_DAYS", 14, minimum=0, maximum=3650),
        uid_base=_env_int("SANDBOX_UID_BASE", 20000, minimum=1000, maximum=2_000_000_000),
        uid_span=_env_int("SANDBOX_UID_SPAN", 10000, minimum=16, maximum=1_000_000),
        max_upload_mb=_env_int("SANDBOX_MAX_UPLOAD_MB", 20, minimum=1, maximum=512),
        ceilings=ceilings,
        require_isolation=_env_bool("SANDBOX_REQUIRE_ISOLATION", default=False),
    )


__all__ = ["Ceilings", "RunnerConfig", "load_config"]
