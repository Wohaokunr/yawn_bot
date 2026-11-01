"""工作区：目录、独立 uid 分配，以及防符号链接/硬链接逃逸的文件访问。

runner 以 root 运行（只保留少量 capability），读写工作区文件时必须假设里面
的一切都可能是用户代码精心构造的：符号链接指向 runner 状态目录、硬链接到
别的文件、超长路径等。所以所有访问都经 ``_walk``：逐级 ``openat`` +
``O_NOFOLLOW``，最终文件还要校验属主是该工作区的 uid。
"""

from __future__ import annotations

import contextlib
import errno
import json
import mimetypes
import os
import re
import shutil
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

WORKSPACE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
MAX_PATH_DEPTH = 16
MAX_COMPONENT_LENGTH = 200
MAX_PATH_LENGTH = 512
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

_KIND_BY_SUFFIX: dict[str, str] = {
    **dict.fromkeys(
        (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".ico", ".heic"),
        "image",
    ),
    ".svg": "vector",
    # .amr/.silk 是 QQ 语音的原始编码，群聊附件入箱时可能原样存下来。
    **dict.fromkeys(
        (".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac", ".opus", ".amr", ".silk", ".wma"),
        "audio",
    ),
    **dict.fromkeys((".mid", ".midi"), "midi"),
    **dict.fromkeys((".mp4", ".webm", ".mov", ".mkv", ".avi", ".flv", ".m4v", ".3gp"), "video"),
    **dict.fromkeys((".csv", ".tsv", ".xlsx", ".xls", ".xlsm", ".ods", ".parquet"), "table"),
    **dict.fromkeys(
        (
            ".txt",
            ".md",
            ".json",
            ".py",
            ".log",
            ".yaml",
            ".yml",
            ".toml",
            ".ini",
            ".html",
            ".xml",
            ".tex",
            ".jsonl",
            ".srt",
            ".lrc",
            ".ass",
            ".vtt",
            ".sh",
            ".sql",
            ".js",
            ".ts",
            ".css",
            ".c",
            ".h",
            ".cpp",
            ".java",
            ".go",
            ".rs",
            ".ipynb",
            ".cfg",
            ".conf",
        ),
        "text",
    ),
    **dict.fromkeys((".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar"), "archive"),
    **dict.fromkeys((".pdf", ".docx", ".doc", ".pptx", ".ppt", ".odt", ".odp", ".epub", ".rtf"), "document"),
}
TEXT_KINDS = frozenset({"text", "table", "vector"})


class WorkspaceError(Exception):
    """可直接回报给调用方的工作区错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def file_kind(name: str) -> str:
    return _KIND_BY_SUFFIX.get(Path(name).suffix.lower(), "other")


def file_mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def validate_workspace_id(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not WORKSPACE_ID_PATTERN.fullmatch(text):
        raise WorkspaceError("invalid_workspace", "工作区标识不合法")
    return text


def split_relative_path(value: Any) -> tuple[str, ...]:
    """把用户给的相对路径拆成安全的组件；拒绝绝对路径、``..``、控制字符。"""

    text = str(value or "").replace("\\", "/").strip()
    if not text or len(text) > MAX_PATH_LENGTH:
        raise WorkspaceError("invalid_path", "路径为空或过长")
    if text.startswith("/"):
        raise WorkspaceError("invalid_path", "只能使用工作区内的相对路径")
    parts = tuple(part for part in text.split("/") if part not in {"", "."})
    if not parts or len(parts) > MAX_PATH_DEPTH:
        raise WorkspaceError("invalid_path", "路径层级不合法")
    for part in parts:
        if part == ".." or len(part) > MAX_COMPONENT_LENGTH:
            raise WorkspaceError("invalid_path", "路径不能包含 .. 或超长片段")
        if any(ord(char) < 32 or char == "\x7f" for char in part):
            raise WorkspaceError("invalid_path", "路径不能包含控制字符")
    return parts


@dataclass(frozen=True, slots=True)
class Workspace:
    id: str
    path: Path
    uid: int
    gid: int


@dataclass(frozen=True, slots=True)
class FileEntry:
    path: str
    size: int
    mtime: float
    kind: str
    mime: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size": self.size,
            "mtime": round(self.mtime, 3),
            "kind": self.kind,
            "mime": self.mime,
        }


class WorkspaceStore:
    """工作区目录与 uid 映射。uid 映射持久化，重启后同一个群仍是同一个 uid。"""

    def __init__(
        self,
        root: Path,
        meta_dir: Path,
        *,
        uid_base: int,
        uid_span: int,
        drop_privileges: bool | None = None,
    ) -> None:
        self.root = root
        self.meta_dir = meta_dir
        self.uid_base = uid_base
        self.uid_span = uid_span
        self.drop_privileges = (
            os.geteuid() == 0 if drop_privileges is None else drop_privileges
        )
        self._lock = threading.Lock()
        self._uids: dict[str, int] = {}
        self.root.mkdir(parents=True, exist_ok=True)
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.root, 0o711)
            os.chmod(self.meta_dir, 0o700)
        if self.drop_privileges:
            # 状态目录常是宿主 bind mount（可能是 0700 的部署用户目录）；降权后的
            # 工作区 uid 至少要能穿过它，但不能列出里面有哪些群。
            with contextlib.suppress(OSError):
                parent = self.root.parent
                if parent.stat().st_mode & 0o011 != 0o011:
                    os.chmod(parent, 0o711)
        self._load_uids()

    # ── uid 映射 ────────────────────────────────────────────────────────────

    @property
    def _uid_file(self) -> Path:
        return self.meta_dir / "uids.json"

    def _load_uids(self) -> None:
        try:
            raw = json.loads(self._uid_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                if (
                    isinstance(key, str)
                    and WORKSPACE_ID_PATTERN.fullmatch(key)
                    and isinstance(value, int)
                    and self.uid_base <= value < self.uid_base + self.uid_span
                ):
                    self._uids[key] = value

    def _save_uids(self) -> None:
        temporary = self._uid_file.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self._uids, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, self._uid_file)

    def _allocate_uid(self, workspace_id: str) -> int:
        existing = self._uids.get(workspace_id)
        if existing is not None:
            return existing
        used = set(self._uids.values())
        for offset in range(self.uid_span):
            candidate = self.uid_base + offset
            if candidate not in used:
                self._uids[workspace_id] = candidate
                self._save_uids()
                return candidate
        raise WorkspaceError("workspace_limit", "工作区数量已达上限")

    # ── 工作区 ──────────────────────────────────────────────────────────────

    def get(self, workspace_id: str) -> Workspace | None:
        workspace_id = validate_workspace_id(workspace_id)
        path = self.root / workspace_id
        uid = self._uids.get(workspace_id)
        if uid is None or not path.is_dir():
            return None
        return Workspace(workspace_id, path, uid, uid)

    def ensure(self, workspace_id: str) -> Workspace:
        workspace_id = validate_workspace_id(workspace_id)
        with self._lock:
            uid = self._allocate_uid(workspace_id)
            path = self.root / workspace_id
            if path.is_symlink():
                raise WorkspaceError("invalid_workspace", "工作区目录被替换为链接")
            path.mkdir(mode=0o700, exist_ok=True)
            if self.drop_privileges:
                os.chown(path, uid, uid, follow_symlinks=False)
                os.chmod(path, 0o700)
        return Workspace(workspace_id, path, uid, uid)

    def list_workspaces(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            entry.name
            for entry in os.scandir(self.root)
            if entry.is_dir(follow_symlinks=False)
            and WORKSPACE_ID_PATTERN.fullmatch(entry.name)
        )

    def uid_of(self, workspace_id: str) -> int | None:
        return self._uids.get(workspace_id)

    # ── 安全遍历 ────────────────────────────────────────────────────────────

    @contextlib.contextmanager
    def _walk(
        self, workspace: Workspace, parts: tuple[str, ...], *, create: bool = False
    ) -> Iterator[int]:
        """返回最后一级父目录的 fd；每一级都 ``O_NOFOLLOW``，不会跟随链接。"""

        fds: list[int] = []
        try:
            fds.append(os.open(workspace.path, _DIR_FLAGS))
            for part in parts[:-1]:
                try:
                    fds.append(os.open(part, _DIR_FLAGS, dir_fd=fds[-1]))
                except FileNotFoundError:  # noqa: PERF203
                    if not create:
                        raise
                    os.mkdir(part, 0o700, dir_fd=fds[-1])
                    fds.append(os.open(part, _DIR_FLAGS, dir_fd=fds[-1]))
                    if self.drop_privileges:
                        os.fchown(fds[-1], workspace.uid, workspace.gid)
            yield fds[-1]
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise WorkspaceError("invalid_path", "路径包含链接或不是目录") from exc
            if isinstance(exc, FileNotFoundError):
                raise WorkspaceError("not_found", "文件不存在") from exc
            raise
        finally:
            for fd in reversed(fds):
                with contextlib.suppress(OSError):
                    os.close(fd)

    def _check_owner(self, workspace: Workspace, info: os.stat_result) -> None:
        if not stat.S_ISREG(info.st_mode):
            raise WorkspaceError("invalid_path", "不是普通文件")
        if self.drop_privileges and info.st_uid != workspace.uid:
            # 硬链接/被替换的文件：属主不是工作区 uid 一律拒绝。
            raise WorkspaceError("invalid_path", "文件属主不属于该工作区")

    def read_file(self, workspace: Workspace, path: str, *, max_bytes: int) -> bytes:
        parts = split_relative_path(path)
        with self._walk(workspace, parts) as parent:
            try:
                fd = os.open(
                    parts[-1],
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                    dir_fd=parent,
                )
            except FileNotFoundError as exc:
                raise WorkspaceError("not_found", "文件不存在") from exc
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise WorkspaceError("invalid_path", "不能读取符号链接") from exc
                raise
            try:
                info = os.fstat(fd)
                self._check_owner(workspace, info)
                if info.st_size > max_bytes:
                    raise WorkspaceError("too_large", "文件超过可读取大小")
                chunks: list[bytes] = []
                remaining = max_bytes + 1
                while remaining > 0:
                    chunk = os.read(fd, min(remaining, 1024 * 1024))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                data = b"".join(chunks)
                if len(data) > max_bytes:
                    raise WorkspaceError("too_large", "文件超过可读取大小")
                return data
            finally:
                os.close(fd)

    def write_file(
        self, workspace: Workspace, path: str, data: bytes, *, overwrite: bool = True
    ) -> FileEntry:
        parts = split_relative_path(path)
        with self._walk(workspace, parts, create=True) as parent:
            with contextlib.suppress(FileNotFoundError):
                info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise WorkspaceError("invalid_path", "目标已存在且不是普通文件")
                if not overwrite:
                    raise WorkspaceError("exists", "同名文件已存在")
                os.unlink(parts[-1], dir_fd=parent)
            fd = os.open(
                parts[-1],
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=parent,
            )
            try:
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                if self.drop_privileges:
                    os.fchown(fd, workspace.uid, workspace.gid)
                info = os.fstat(fd)
            finally:
                os.close(fd)
        name = "/".join(parts)
        return FileEntry(name, info.st_size, info.st_mtime, file_kind(name), file_mime(name))

    def write_stream(
        self,
        workspace: Workspace,
        path: str,
        chunks: Iterable[bytes],
        *,
        overwrite: bool = True,
        max_bytes: int | None = None,
    ) -> FileEntry:
        """边收边写大文件：先写同目录下的隐藏临时文件，收完再改名，半截上传不留垃圾。

        ``max_bytes`` 是写入上限（声明长度不可信，实际写到超出就中止）。
        """

        parts = split_relative_path(path)
        temporary = f".upload-{os.getpid()}-{threading.get_ident()}-{time.monotonic_ns()}"
        with self._walk(workspace, parts, create=True) as parent:
            with contextlib.suppress(FileNotFoundError):
                info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise WorkspaceError("invalid_path", "目标已存在且不是普通文件")
                if not overwrite:
                    raise WorkspaceError("exists", "同名文件已存在")
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=parent,
            )
            done = False
            try:
                total = 0
                for chunk in chunks:
                    total += len(chunk)
                    if max_bytes is not None and total > max_bytes:
                        raise WorkspaceError("too_large", "上传文件过大")
                    view = memoryview(chunk)
                    while view:
                        written = os.write(fd, view)
                        view = view[written:]
                if self.drop_privileges:
                    os.fchown(fd, workspace.uid, workspace.gid)
                info = os.fstat(fd)
                os.close(fd)
                fd = -1
                if not overwrite:
                    try:
                        os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise WorkspaceError("exists", "同名文件已存在")
                os.rename(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
                done = True
            finally:
                if fd >= 0:
                    os.close(fd)
                if not done:
                    with contextlib.suppress(OSError):
                        os.unlink(temporary, dir_fd=parent)
        name = "/".join(parts)
        return FileEntry(name, info.st_size, info.st_mtime, file_kind(name), file_mime(name))

    def delete_path(self, workspace: Workspace, path: str) -> int:
        """删除文件或目录（目录递归，不跟随链接）；返回删掉的文件数。"""

        parts = split_relative_path(path)
        with self._walk(workspace, parts) as parent:
            try:
                info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise WorkspaceError("not_found", "文件不存在") from exc
            if stat.S_ISDIR(info.st_mode):
                return _rmtree_at(parent, parts[-1])
            os.unlink(parts[-1], dir_fd=parent)
            return 1

    def rename(
        self, workspace: Workspace, source: str, target: str, *, overwrite: bool = False
    ) -> FileEntry:
        """在工作区内移动/改名一个文件；两端都逐级 ``O_NOFOLLOW``，不跨工作区。"""

        source_parts = split_relative_path(source)
        target_parts = split_relative_path(target)
        if source_parts == target_parts:
            raise WorkspaceError("invalid_path", "新旧路径相同")
        with self._walk(workspace, source_parts) as source_parent:
            try:
                info = os.stat(source_parts[-1], dir_fd=source_parent, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise WorkspaceError("not_found", "文件不存在") from exc
            self._check_owner(workspace, info)
            with self._walk(workspace, target_parts, create=True) as target_parent:
                try:
                    existing = os.stat(
                        target_parts[-1], dir_fd=target_parent, follow_symlinks=False
                    )
                except FileNotFoundError:
                    existing = None
                if existing is not None:
                    if not overwrite:
                        raise WorkspaceError("exists", "目标位置已有同名文件")
                    if not stat.S_ISREG(existing.st_mode):
                        raise WorkspaceError("invalid_path", "目标已存在且不是普通文件")
                os.rename(
                    source_parts[-1],
                    target_parts[-1],
                    src_dir_fd=source_parent,
                    dst_dir_fd=target_parent,
                )
        name = "/".join(target_parts)
        return FileEntry(name, info.st_size, info.st_mtime, file_kind(name), file_mime(name))

    def make_dir(self, workspace: Workspace, path: str) -> str:
        parts = split_relative_path(path)
        with self._walk(workspace, (*parts, "."), create=True):
            pass
        return "/".join(parts)

    def reset(self, workspace: Workspace) -> int:
        removed = 0
        fd = os.open(workspace.path, _DIR_FLAGS)
        try:
            for name in os.listdir(fd):
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    removed += _rmtree_at(fd, name)
                else:
                    os.unlink(name, dir_fd=fd)
                    removed += 1
        finally:
            os.close(fd)
        return removed

    def iter_files(
        self, workspace: Workspace, *, limit: int
    ) -> Iterator[tuple[str, os.stat_result]]:
        """不跟随链接地遍历普通文件；最多 ``limit`` 项，防止百万小文件拖垮 runner。

        目录一律经 fd + ``O_NOFOLLOW`` 打开，执行中把子目录换成符号链接也
        不会让 runner 列出工作区之外的内容。
        """

        count = 0
        try:
            root_fd = os.open(workspace.path, _DIR_FLAGS)
        except OSError:
            return
        stack: list[tuple[int, str]] = [(root_fd, "")]
        try:
            while stack and count < limit:
                directory_fd, prefix = stack.pop()
                try:
                    names = sorted(os.listdir(directory_fd))
                    for name in names:
                        if count >= limit:
                            break
                        relative = f"{prefix}{name}"
                        try:
                            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                        except OSError:
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            try:
                                child = os.open(name, _DIR_FLAGS, dir_fd=directory_fd)
                            except OSError:
                                continue
                            stack.append((child, f"{relative}/"))
                            continue
                        if not stat.S_ISREG(info.st_mode):
                            continue
                        count += 1
                        yield relative, info
                except OSError:
                    pass
                finally:
                    with contextlib.suppress(OSError):
                        os.close(directory_fd)
        finally:
            for directory_fd, _prefix in stack:
                with contextlib.suppress(OSError):
                    os.close(directory_fd)

    def list_files(
        self,
        workspace: Workspace,
        *,
        prefix: str = "",
        limit: int = 200,
        scan_limit: int = 20000,
    ) -> tuple[list[FileEntry], int]:
        normalized = prefix.strip().strip("/")
        items: list[FileEntry] = []
        total = 0
        for relative, info in self.iter_files(workspace, limit=scan_limit):
            if normalized and not relative.startswith(normalized):
                continue
            total += 1
            items.append(
                FileEntry(
                    relative,
                    info.st_size,
                    info.st_mtime,
                    file_kind(relative),
                    file_mime(relative),
                )
            )
        items.sort(key=lambda item: (-item.mtime, item.path))
        return items[:limit], total

    def dir_summary(
        self, workspace: Workspace, *, scan_limit: int = 20000, limit: int = 300
    ) -> list[dict[str, Any]]:
        """每个目录（含所有上级）下的文件数与字节数，供管理页画目录树。"""

        dirs: dict[str, list[int]] = {}
        for relative, info in self.iter_files(workspace, limit=scan_limit):
            parts = relative.split("/")[:-1]
            for depth in range(1, len(parts) + 1):
                key = "/".join(parts[:depth])
                if key not in dirs and len(dirs) >= limit:
                    continue
                row = dirs.setdefault(key, [0, 0])
                row[0] += 1
                row[1] += info.st_size
        return [
            {"path": key, "files": value[0], "bytes": value[1]}
            for key, value in sorted(dirs.items())
        ]

    def usage(self, workspace: Workspace, *, scan_limit: int = 200000) -> dict[str, int]:
        size = 0
        files = 0
        for _relative, info in self.iter_files(workspace, limit=scan_limit):
            files += 1
            # 稀疏文件按真实占用计算，避免 truncate 出来的大洞绕过配额判断。
            size += info.st_blocks * 512 if info.st_blocks else info.st_size
        return {"bytes": size, "files": files}

    def snapshot(
        self, workspace: Workspace, *, limit: int
    ) -> dict[str, tuple[int, int]]:
        return {
            relative: (info.st_size, info.st_mtime_ns)
            for relative, info in self.iter_files(workspace, limit=limit)
        }

    def prune(self, *, older_than_days: int) -> int:
        """删除超过保留期的文件；0 表示不清理。返回删除数量。"""

        if older_than_days <= 0:
            return 0
        cutoff = time.time() - older_than_days * 86400
        removed = 0
        for workspace_id in self.list_workspaces():
            workspace = self.get(workspace_id)
            if workspace is None:
                continue
            stale = [
                relative
                for relative, info in self.iter_files(workspace, limit=200000)
                if info.st_mtime < cutoff
            ]
            for relative in stale:
                with contextlib.suppress(WorkspaceError, OSError):
                    removed += self.delete_path(workspace, relative)
        return removed


def _rmtree_at(parent_fd: int, name: str) -> int:
    """在 ``parent_fd`` 下递归删除目录 ``name``，全程不跟随链接。"""

    removed = 0
    fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    try:
        for child in os.listdir(fd):
            info = os.stat(child, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                removed += _rmtree_at(fd, child)
            else:
                os.unlink(child, dir_fd=fd)
                removed += 1
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)
    return removed


def remove_tree(path: Path) -> None:
    """删除 runner 自己的临时目录（作业目录），容忍并发消失。"""

    with contextlib.suppress(FileNotFoundError):
        shutil.rmtree(path)


__all__ = [
    "TEXT_KINDS",
    "FileEntry",
    "Workspace",
    "WorkspaceError",
    "WorkspaceStore",
    "file_kind",
    "file_mime",
    "remove_tree",
    "split_relative_path",
    "validate_workspace_id",
]
