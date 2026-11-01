"""执行器：在独立 uid、rlimit、墙钟时限和磁盘配额看门狗下运行一段 Python。

一次执行的生命周期：
1. 取全局并发槽（有界排队）+ 该工作区的互斥锁（同一工作区串行）；
2. 预检工作区配额；把代码写进 runner 私有的作业目录（tmpfs）；
3. ``Popen``：切换到工作区 uid、新会话、清空环境变量、setrlimit；
4. 读取 stdout/stderr（各自只保留上限内的头尾，其余丢弃但继续读，防止管道阻塞）；
5. 轮询：超时/取消/磁盘超额就整组 SIGKILL；
6. 回收：``wait4`` 拿 CPU 与峰值内存，杀掉该 uid 的残留进程，比较文件快照
   得到新建/修改的文件，删除作业目录。
"""

from __future__ import annotations

import contextlib
import os
import secrets
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

from .limits import RunLimits, apply_child_limits
from .workspace import (
    Workspace,
    WorkspaceStore,
    file_kind,
    file_mime,
    remove_tree,
)

if TYPE_CHECKING:
    from .config import RunnerConfig
    from .netpolicy import NetPolicy

MAX_CODE_CHARS = 64 * 1024
# python：经 bootstrap 运行（notebook 式回显、自动存图）；shell：bash 脚本，
# 用来 ls/cat/grep/sed/find/zip 这类终端操作。两者共用同一个工作区、uid 与限额。
LANGUAGES = ("python", "shell")
_BASH = "/bin/bash"
MAX_REPORTED_FILES = 50
_POLL_SECONDS = 0.05
_QUOTA_CHECK_SECONDS = 1.0
_BOOTSTRAP = Path(__file__).with_name("bootstrap.py")

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_TIMEOUT = "timeout"
STATUS_CPU_LIMIT = "cpu_limit"
STATUS_MEMORY_LIMIT = "memory_limit"
STATUS_DISK_LIMIT = "disk_limit"
STATUS_OUTPUT_LIMIT = "output_limit"
STATUS_KILLED = "killed"
STATUS_CANCELLED = "cancelled"
STATUS_REJECTED = "rejected"
STATUS_BUSY = "busy"
RUN_STATUSES = (
    STATUS_OK,
    STATUS_ERROR,
    STATUS_TIMEOUT,
    STATUS_CPU_LIMIT,
    STATUS_MEMORY_LIMIT,
    STATUS_DISK_LIMIT,
    STATUS_OUTPUT_LIMIT,
    STATUS_KILLED,
    STATUS_CANCELLED,
    STATUS_REJECTED,
    STATUS_BUSY,
)

# 子进程只拿到这些环境变量；runner 自己的环境（含可能的 token）一概不继承。
_BASE_CHILD_ENV = {
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUNBUFFERED": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "MPLBACKEND": "Agg",
    # 数值库默认按 CPU 数开线程池，会瞬间撞上 RLIMIT_NPROC/AS；固定单线程。
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    # 联网时 pip 只往工作区的 .pylib 装（根文件系统只读），不留缓存、不查新版本。
    "PIP_NO_CACHE_DIR": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
}

_MATPLOTLIBRC = """\
backend: Agg
font.family: sans-serif
font.sans-serif: WenQuanYi Micro Hei, Noto Sans CJK SC, DejaVu Sans
axes.unicode_minus: False
figure.dpi: 110
figure.figsize: 8, 5
savefig.dpi: 144
savefig.bbox: tight
"""


@dataclass(slots=True)
class RunRequest:
    workspace: str
    code: str
    limits: RunLimits
    meta: dict[str, Any] = field(default_factory=dict)
    run_id: str = field(default_factory=lambda: secrets.token_hex(8))
    language: str = "python"
    # 是否允许这次执行访问外网；None 表示按 runner 默认（SANDBOX_NETWORK_DEFAULT）。
    network: bool | None = None


@dataclass(slots=True)
class RunResult:
    run_id: str
    workspace: str
    status: str
    exit_code: int | None = None
    signal: str | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    started_at: float = 0.0
    duration_ms: int = 0
    queue_ms: int = 0
    cpu_ms: int = 0
    max_rss_kb: int = 0
    files: list[dict[str, Any]] = field(default_factory=list)
    files_truncated: bool = False
    message: str = ""
    limits: dict[str, int] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)
    language: str = "python"
    network: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workspace": self.workspace,
            "status": self.status,
            "exit_code": self.exit_code,
            "signal": self.signal,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "started_at": round(self.started_at, 3),
            "duration_ms": self.duration_ms,
            "queue_ms": self.queue_ms,
            "cpu_ms": self.cpu_ms,
            "max_rss_kb": self.max_rss_kb,
            "files": self.files,
            "files_truncated": self.files_truncated,
            "message": self.message,
            "limits": self.limits,
            "meta": self.meta,
            "usage": self.usage,
            "language": self.language,
            "network": self.network,
        }


class _BoundedCapture:
    """读满一个管道；保留前 ``head`` 与后 ``tail`` 字节，中间丢弃。"""

    def __init__(self, stream: IO[bytes], limit: int) -> None:
        self.stream = stream
        self.head_limit = max(limit * 3 // 4, 1)
        self.tail_limit = max(limit - self.head_limit, 0)
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _pump(self) -> None:
        with contextlib.suppress(OSError, ValueError):
            while True:
                chunk = os.read(self.stream.fileno(), 65536)
                if not chunk:
                    break
                self.total += len(chunk)
                room = self.head_limit - len(self.head)
                if room > 0:
                    self.head.extend(chunk[:room])
                    chunk = chunk[room:]
                if chunk and self.tail_limit:
                    self.tail.extend(chunk)
                    if len(self.tail) > self.tail_limit:
                        del self.tail[: len(self.tail) - self.tail_limit]

    @property
    def truncated(self) -> bool:
        return self.total > len(self.head) + len(self.tail)

    def text(self) -> str:
        head = self.head.decode("utf-8", errors="replace")
        if not self.truncated:
            return head + self.tail.decode("utf-8", errors="replace")
        skipped = self.total - len(self.head) - len(self.tail)
        tail = self.tail.decode("utf-8", errors="replace")
        return f"{head}\n…（省略 {skipped} 字节输出）…\n{tail}"

    def join(self, timeout: float) -> None:
        self.thread.join(timeout)


@dataclass(slots=True)
class ActiveRun:
    run_id: str
    workspace: str
    started_at: float
    meta: dict[str, Any]
    limits: dict[str, int]
    cancel: threading.Event = field(default_factory=threading.Event)
    pid: int | None = None
    phase: str = "queued"

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workspace": self.workspace,
            "started_at": round(self.started_at, 3),
            "elapsed_ms": int((time.time() - self.started_at) * 1000),
            "meta": self.meta,
            "limits": self.limits,
            "phase": self.phase,
        }


def kill_uid_processes(uid: int) -> int:
    """杀掉某 uid 的全部进程（处理 setsid/double-fork 逃出进程组的残留）。"""

    killed = 0
    proc = Path("/proc")
    if not proc.is_dir():
        return 0
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in status.splitlines():
            if line.startswith("Uid:"):
                fields = line.split()
                # 真实/有效/保存 uid 任一匹配都算。
                if str(uid) in fields[1:4]:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.kill(int(entry.name), signal.SIGKILL)
                        killed += 1
                break
    return killed


def _signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"SIG{number}"


class Executor:
    def __init__(
        self, config: RunnerConfig, store: WorkspaceStore, netpolicy: NetPolicy | None = None
    ) -> None:
        self.config = config
        self.store = store
        self.netpolicy = netpolicy
        self.drop_privileges = store.drop_privileges
        self._slots = threading.BoundedSemaphore(config.concurrency)
        self._state_lock = threading.Lock()
        self._workspace_locks: dict[str, threading.Lock] = {}
        self._waiting = 0
        self.active: dict[str, ActiveRun] = {}
        self.config.jobs_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.config.jobs_dir, 0o711)

    # ── 排队与取消 ──────────────────────────────────────────────────────────

    @property
    def waiting(self) -> int:
        return self._waiting

    def _workspace_lock(self, workspace: str) -> threading.Lock:
        with self._state_lock:
            return self._workspace_locks.setdefault(workspace, threading.Lock())

    def cancel(self, run_id: str) -> bool:
        with self._state_lock:
            active = self.active.get(run_id)
        if active is None:
            return False
        active.cancel.set()
        return True

    def cancel_workspace(self, workspace: str) -> int:
        with self._state_lock:
            targets = [item for item in self.active.values() if item.workspace == workspace]
        for item in targets:
            item.cancel.set()
        return len(targets)

    def active_runs(self) -> list[dict[str, Any]]:
        with self._state_lock:
            return [item.to_dict() for item in self.active.values()]

    # ── 执行 ────────────────────────────────────────────────────────────────

    def run(self, request: RunRequest) -> RunResult:
        submitted = time.time()
        result = RunResult(
            run_id=request.run_id,
            workspace=request.workspace,
            status=STATUS_BUSY,
            started_at=submitted,
            limits=request.limits.to_dict(),
            meta=request.meta,
            language=request.language,
        )
        if not request.code.strip():
            result.status = STATUS_REJECTED
            result.message = "代码为空"
            return result
        if len(request.code) > MAX_CODE_CHARS:
            result.status = STATUS_REJECTED
            result.message = f"代码超过 {MAX_CODE_CHARS} 字符上限"
            return result
        if "\x00" in request.code:
            result.status = STATUS_REJECTED
            result.message = "代码包含 NUL 字符"
            return result
        if request.language not in LANGUAGES:
            result.status = STATUS_REJECTED
            result.message = f"不支持的语言：{request.language}"
            return result

        with self._state_lock:
            if self._waiting >= self.config.queue_limit + self.config.concurrency:
                result.message = "沙箱排队已满，请稍后再试"
                return result
            self._waiting += 1
            active = ActiveRun(
                run_id=request.run_id,
                workspace=request.workspace,
                started_at=submitted,
                meta=request.meta,
                limits=request.limits.to_dict(),
            )
            self.active[request.run_id] = active
        try:
            deadline = time.monotonic() + self.config.queue_wait_seconds
            if not self._slots.acquire(timeout=self.config.queue_wait_seconds):
                result.message = "沙箱繁忙（排队超时），请稍后再试"
                return result
            try:
                lock = self._workspace_lock(request.workspace)
                remaining = max(deadline - time.monotonic(), 0.1)
                if not lock.acquire(timeout=remaining):
                    result.message = "这个工作区还有代码在运行，请稍后再试"
                    return result
                try:
                    if active.cancel.is_set():
                        result.status = STATUS_CANCELLED
                        result.message = "排队期间已取消"
                        return result
                    result.queue_ms = int((time.time() - submitted) * 1000)
                    active.phase = "running"
                    return self._run_locked(request, result, active)
                finally:
                    lock.release()
            finally:
                self._slots.release()
        finally:
            with self._state_lock:
                self._waiting -= 1
                self.active.pop(request.run_id, None)

    def _child_env(self, workspace: Workspace, job_dir: Path) -> dict[str, str]:
        env = dict(_BASE_CHILD_ENV)
        # 终端里敲 python/pip list 时用的是沙箱 venv，而不是系统 Python。
        venv_bin = str(Path(self.config.python).parent)
        env["PATH"] = f"{venv_bin}:{env['PATH']}"
        env.update(
            {
                "HOME": str(workspace.path),
                "TMPDIR": str(job_dir / "tmp"),
                "MPLCONFIGDIR": str(job_dir / "mpl"),
                "YAWN_SANDBOX_LIB": str(self.config.lib_dir),
                "YAWN_WORKSPACE": workspace.id,
            }
        )
        return env

    def _prepare_job_dir(self, workspace: Workspace, request: RunRequest) -> Path:
        job_dir = self.config.jobs_dir / request.run_id
        job_dir.mkdir(mode=0o700)
        (job_dir / "tmp").mkdir(mode=0o700)
        (job_dir / "mpl").mkdir(mode=0o700)
        script = "main.sh" if request.language == "shell" else "main.py"
        (job_dir / script).write_text(request.code, encoding="utf-8")
        (job_dir / "mpl" / "matplotlibrc").write_text(_MATPLOTLIBRC, encoding="utf-8")
        cache = self.config.lib_dir.parent / "mplcache"
        if cache.is_dir():
            # 镜像构建时预生成的字体缓存；复制过来能省掉每次 1~3 秒的字体扫描。
            for item in cache.glob("fontlist-*.json"):
                with contextlib.suppress(OSError):
                    (job_dir / "mpl" / item.name).write_bytes(item.read_bytes())
        if self.drop_privileges:
            for path in (job_dir, *job_dir.rglob("*")):
                os.chown(path, workspace.uid, workspace.gid, follow_symlinks=False)
        return job_dir

    def _run_locked(  # noqa: PLR0912, PLR0915
        self, request: RunRequest, result: RunResult, active: ActiveRun
    ) -> RunResult:
        limits = request.limits
        workspace = self.store.ensure(request.workspace)
        usage = self.store.usage(workspace)
        quota_bytes = limits.disk_quota_mb * 1024 * 1024
        if usage["bytes"] >= quota_bytes or usage["files"] >= limits.max_files:
            result.status = STATUS_DISK_LIMIT
            result.message = "工作区空间或文件数已满，请先删除不需要的文件"
            result.usage = usage
            return result
        if self.netpolicy is not None:
            allowed = (
                self.netpolicy.default_allowed if request.network is None else bool(request.network)
            )
            enforced = self.netpolicy.apply(
                workspace.uid if self.drop_privileges else None, request.network
            )
            result.network = {"allowed": allowed, "enforced": enforced}
        before = self.store.snapshot(workspace, limit=limits.max_files + 100)
        job_dir = self._prepare_job_dir(workspace, request)
        if request.language == "shell":
            # 不读 profile/rc，环境只有 _child_env 给的那些；pipefail 让管道里
            # 前面的命令失败也能如实报错。
            command = [
                _BASH,
                "--noprofile",
                "--norc",
                "-o",
                "pipefail",
                str(job_dir / "main.sh"),
            ]
        else:
            command = [
                self.config.python,
                "-I",
                "-X",
                "utf8",
                str(_BOOTSTRAP),
                str(job_dir / "main.py"),
                request.run_id[:8],
            ]
        popen_kwargs: dict[str, Any] = {}
        if self.drop_privileges:
            popen_kwargs.update(
                user=workspace.uid,
                group=workspace.gid,
                extra_groups=[],
            )

        def _preexec() -> None:
            apply_child_limits(limits, limit_processes=self.drop_privileges)

        started = time.monotonic()
        result.started_at = time.time()
        reason: str | None = None
        try:
            process = subprocess.Popen(
                command,
                cwd=workspace.path,
                env=self._child_env(workspace, job_dir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                close_fds=True,
                umask=0o077,
                preexec_fn=_preexec,  # noqa: PLW1509 - rlimit 只能在 fork 后设置
                **popen_kwargs,
            )
        except OSError as exc:
            remove_tree(job_dir)
            result.status = STATUS_ERROR
            result.message = f"无法启动 Python：{exc.strerror or exc}"
            return result

        active.pid = process.pid
        assert process.stdout is not None and process.stderr is not None
        stdout = _BoundedCapture(process.stdout, limits.output_bytes)
        stderr = _BoundedCapture(process.stderr, max(limits.output_bytes // 2, 1024))
        status_code: int | None = None
        rusage: Any = None
        next_quota_check = started + _QUOTA_CHECK_SECONDS
        output_cap = limits.output_bytes * 64
        try:
            while True:
                pid, wait_status, usage_info = os.wait4(process.pid, os.WNOHANG)
                if pid == process.pid:
                    status_code = wait_status
                    rusage = usage_info
                    break
                now = time.monotonic()
                if active.cancel.is_set():
                    reason = STATUS_CANCELLED
                elif now - started > limits.timeout_seconds:
                    reason = STATUS_TIMEOUT
                elif stdout.total + stderr.total > output_cap:
                    # 疯狂刷屏会把 CPU 全耗在管道上；超过保留量的 64 倍直接结束。
                    reason = STATUS_OUTPUT_LIMIT
                elif now >= next_quota_check:
                    next_quota_check = now + _QUOTA_CHECK_SECONDS
                    current = self.store.usage(workspace)
                    if current["bytes"] > quota_bytes or current["files"] > limits.max_files:
                        reason = STATUS_DISK_LIMIT
                if reason is not None:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(process.pid, signal.SIGKILL)
                    _pid, wait_status, usage_info = os.wait4(process.pid, 0)
                    status_code = wait_status
                    rusage = usage_info
                    break
                time.sleep(_POLL_SECONDS)
        finally:
            # 已经用 wait4 回收；告诉 Popen 不要再等待这个 pid。
            process.returncode = (
                os.waitstatus_to_exitcode(status_code) if status_code is not None else -9
            )
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            if self.drop_privileges:
                kill_uid_processes(workspace.uid)
            stdout.join(2)
            stderr.join(2)
            for stream in (process.stdout, process.stderr):
                with contextlib.suppress(OSError):
                    stream.close()
            remove_tree(job_dir)

        result.duration_ms = int((time.monotonic() - started) * 1000)
        if rusage is not None:
            result.cpu_ms = int((rusage.ru_utime + rusage.ru_stime) * 1000)
            result.max_rss_kb = int(rusage.ru_maxrss)
        result.stdout = stdout.text()
        result.stderr = stderr.text()
        result.stdout_truncated = stdout.truncated
        result.stderr_truncated = stderr.truncated
        exit_code = os.waitstatus_to_exitcode(status_code) if status_code is not None else None
        if exit_code is not None and exit_code < 0:
            result.signal = _signal_name(-exit_code)
            result.exit_code = None
        else:
            result.exit_code = exit_code
        result.status = self._classify(reason, result, limits)
        result.message = _STATUS_MESSAGES.get(result.status, "")
        after = self.store.snapshot(workspace, limit=limits.max_files + 100)
        changed = [
            (path, info)
            for path, info in after.items()
            if before.get(path) != info
        ]
        changed.sort(key=lambda item: (-item[1][1], item[0]))
        result.files_truncated = len(changed) > MAX_REPORTED_FILES
        result.files = [
            {
                "path": path,
                "size": size,
                "kind": file_kind(path),
                "mime": file_mime(path),
                "created": path not in before,
            }
            for path, (size, _mtime) in changed[:MAX_REPORTED_FILES]
        ]
        result.usage = self.store.usage(workspace)
        if result.status == STATUS_OK and (
            result.usage["bytes"] > quota_bytes or result.usage["files"] > limits.max_files
        ):
            # 两次配额巡检之间写爆的情况：文件保留，方便查看和删除，但如实标成超限。
            result.status = STATUS_DISK_LIMIT
            result.message = "运行结束时工作区已超出空间配额；删除不需要的文件前不能再运行"
        return result

    @staticmethod
    def _classify(reason: str | None, result: RunResult, limits: RunLimits) -> str:
        if reason is not None:
            return reason
        stderr_tail = result.stderr[-2000:]
        if result.signal == "SIGXCPU" or (
            result.signal == "SIGKILL" and result.cpu_ms >= limits.cpu_seconds * 1000
        ):
            return STATUS_CPU_LIMIT
        if "MemoryError" in stderr_tail or "Cannot allocate memory" in stderr_tail:
            return STATUS_MEMORY_LIMIT
        if result.signal == "SIGXFSZ" or "File too large" in stderr_tail:
            return STATUS_DISK_LIMIT
        if result.signal is not None:
            return STATUS_KILLED
        if result.exit_code == 0:
            return STATUS_OK
        return STATUS_ERROR


_STATUS_MESSAGES = {
    STATUS_OK: "",
    STATUS_ERROR: "代码抛出异常或以非零状态退出",
    STATUS_TIMEOUT: "超过运行时限，已强制结束",
    STATUS_CPU_LIMIT: "超过 CPU 时间上限，已强制结束",
    STATUS_MEMORY_LIMIT: "超过内存上限",
    STATUS_DISK_LIMIT: "超过工作区空间/单文件大小上限，已强制结束",
    STATUS_OUTPUT_LIMIT: "输出过多，已强制结束",
    STATUS_KILLED: "进程被信号结束",
    STATUS_CANCELLED: "已被管理员取消",
}


__all__ = [
    "LANGUAGES",
    "MAX_CODE_CHARS",
    "RUN_STATUSES",
    "ActiveRun",
    "Executor",
    "RunRequest",
    "RunResult",
    "kill_uid_processes",
]
