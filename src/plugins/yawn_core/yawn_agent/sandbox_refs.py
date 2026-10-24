# ruff: noqa: E501,TRY003
"""``sandbox:<路径>`` 引用：把沙箱工作区里的产物取回 Bot，交给原有发送通道。

模型在 ``run_python`` 的结果里拿到 ``sandbox:chart.png`` 这样的引用，然后照常
调用 ``send_message``（image/record/video 段）或 ``send_file``。引用只会解析到
**当前群**的工作区；取回的文件放在 Agent 文件目录下的隐藏目录
``.sandbox_exports/<群号>/<随机串>/``，发送结束即删除，残留由定期清理兜底。
"""

from __future__ import annotations

import contextlib
import re
import secrets
import shutil
import time
from pathlib import Path, PurePosixPath

from .sandbox_client import SandboxClient, SandboxError, workspace_for_group
from .tool_args import ERROR_NOT_FOUND, ERROR_UPSTREAM, ToolError
from .tool_support import agent_file_root

SANDBOX_REF_PREFIX = "sandbox:"
EXPORT_DIR_NAME = ".sandbox_exports"
_EXPORT_TTL_SECONDS = 3600
_SAFE_NAME = re.compile(r"[^\w.\-一-鿿]+")
_MEDIA_KINDS = {"image": "image", "record": "audio", "video": "video"}
_KIND_LABELS = {"image": "图片", "audio": "音频（WAV/MP3）", "video": "视频"}


def is_sandbox_ref(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower().startswith(SANDBOX_REF_PREFIX)


def parse_sandbox_ref(value: str) -> str:
    """``sandbox:out/chart.png`` → ``out/chart.png``；拒绝绝对路径和 ``..``。"""

    raw = value.strip()[len(SANDBOX_REF_PREFIX) :].strip().lstrip("/")
    raw = raw.replace("\\", "/")
    parts = [part for part in PurePosixPath(raw).parts if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts) or len(raw) > 512:  # noqa: PLR2004
        raise ValueError("sandbox 引用的路径不合法")
    return "/".join(parts)


def export_root() -> Path:
    return agent_file_root() / EXPORT_DIR_NAME


def is_export_path(path: Path) -> bool:
    root = export_root().resolve()
    with contextlib.suppress(OSError):
        resolved = path.resolve()
        return resolved == root or root in resolved.parents
    return False


def _safe_name(path: str) -> str:
    name = PurePosixPath(path).name
    cleaned = _SAFE_NAME.sub("_", name).strip("._") or "file"
    return cleaned[:120]


def prune_exports(*, older_than: float = _EXPORT_TTL_SECONDS) -> int:
    """删掉超过一小时的导出目录（发送异常中断时留下的）。"""

    root = export_root()
    if not root.is_dir():
        return 0
    cutoff = time.time() - older_than
    removed = 0
    for group_dir in root.iterdir():
        if not group_dir.is_dir():
            continue
        for item in group_dir.iterdir():
            with contextlib.suppress(OSError):
                if item.stat().st_mtime < cutoff:
                    shutil.rmtree(item) if item.is_dir() else item.unlink()
                    removed += 1
    return removed


async def materialize(
    ref: str,
    group_id: int,
    *,
    media_type: str | None = None,
    max_bytes: int,
    client: SandboxClient | None = None,
) -> Path:
    """取回当前群工作区里的文件，返回 Bot 本地临时路径（调用方负责发送后删除）。

    ``media_type`` 为 image/record/video 时校验文件类型，避免把 MIDI 当语音发。
    """

    path = parse_sandbox_ref(ref)
    with contextlib.suppress(OSError):
        prune_exports()
    try:
        data, _mime, kind = await (client or SandboxClient()).read_file(
            workspace_for_group(group_id), path, max_bytes=max_bytes
        )
    except SandboxError as exc:
        if exc.code == "not_found":
            raise ToolError(
                f"沙箱里没有 {path}",
                code=ERROR_NOT_FOUND,
                hint="先用 list_sandbox_files 确认文件名，不要猜路径",
            ) from exc
        if exc.code == "too_large":
            raise ValueError("沙箱文件超过发送大小限制") from exc
        raise ToolError(
            f"代码沙箱暂时取不到文件：{exc}",
            code=ERROR_UPSTREAM,
            hint="告诉对方文件暂时发不出去，不要假装已经发送",
        ) from exc
    if media_type is not None:
        expected = _MEDIA_KINDS.get(media_type)
        if expected is not None and kind != expected:
            label = _KIND_LABELS[expected]
            raise ValueError(f"{path} 不是{label}文件，不能作为 {media_type} 段发送")
    if len(data) > max_bytes:
        raise ValueError("沙箱文件超过发送大小限制")
    target_dir = export_root() / str(int(group_id)) / secrets.token_hex(8)
    target_dir.mkdir(parents=True, exist_ok=False)
    target = target_dir / _safe_name(path)
    target.write_bytes(data)
    return target


def discard(path: Path) -> None:
    """删掉导出的文件及其随机目录。"""

    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        path.parent.rmdir()


__all__ = [
    "EXPORT_DIR_NAME",
    "SANDBOX_REF_PREFIX",
    "discard",
    "export_root",
    "is_export_path",
    "is_sandbox_ref",
    "materialize",
    "parse_sandbox_ref",
    "prune_exports",
]
