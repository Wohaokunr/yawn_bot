"""短生命周期的本机番茄移动端 helper 桥接。

该模块只连接由本进程启动、绑定到 ``127.0.0.1`` 的 helper。helper 的数据目录、
导出目录和配置都位于临时目录，章节读取完成后会随进程一起清理。
"""

# helper 进程、HTTP 轮询和本地文件导出均需要较多错误分支。
# ruff: noqa: ASYNC240, C901, PLR0912, TRY003, TRY300

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from .config import Config

_LOOPBACK_HOST = "127.0.0.1"
_STARTUP_POLL_SECONDS = 0.1
_JOB_POLL_SECONDS = 0.25
_STOP_TIMEOUT_SECONDS = 5.0
_HTTP_ERROR_STATUS = 400


class MobileHelperError(RuntimeError):
    """本机 helper 不可用、协议不匹配或没有导出单章文本。"""


@dataclass(frozen=True, slots=True)
class MobileChapterText:
    """从 helper 的单章 TXT 导出的已清洗正文。"""

    title: str
    content: str


def mobile_helper_configured(settings: Config) -> bool:
    """是否明确配置了本机 helper 可执行文件。"""

    return bool(settings.fanqie_mobile_helper_path.strip())


def _find_loopback_port() -> int:
    """临时分配一个本地回环端口；helper 仅在该端口监听。"""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((_LOOPBACK_HOST, 0))
        return int(listener.getsockname()[1])


def _normalize_title(value: str) -> str:
    return "".join(value.split())


def _read_exported_chapter(
    output_dir: Path,
    expected_title: str,
    max_bytes: int,
) -> MobileChapterText:
    """读取 bulk TXT 中唯一的章节文件，并验证页面和导出标题一致。"""

    output_root = output_dir.resolve()
    candidates: list[Path] = []
    for candidate in output_dir.rglob("*.txt"):
        resolved = candidate.resolve()
        try:
            resolved.relative_to(output_root)
        except ValueError:
            continue
        if resolved.name == "0000_书籍信息.txt" or not resolved.is_file():
            continue
        candidates.append(resolved)
    if len(candidates) != 1:
        raise MobileHelperError("本机 helper 未导出唯一的单章 TXT")

    chapter_path = candidates[0]
    if chapter_path.stat().st_size > max_bytes:
        raise MobileHelperError("本机 helper 导出的单章超过文件大小限制")
    try:
        raw_text = chapter_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MobileHelperError("本机 helper 导出的单章不是 UTF-8 文本") from exc

    lines = raw_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].strip().startswith("分卷："):
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    if not lines:
        raise MobileHelperError("本机 helper 导出的单章缺少标题")

    title = lines.pop(0).strip()
    if expected_title and _normalize_title(title) != _normalize_title(expected_title):
        raise MobileHelperError("本机 helper 导出的章节标题与阅读页不一致")
    while lines and not lines[0].strip():
        lines.pop(0)
    content = "\n".join(lines).strip()
    if not content:
        raise MobileHelperError("本机 helper 导出的章节正文为空")
    return MobileChapterText(title=title, content=content)


async def _json_request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    **kwargs: Any,
) -> dict[str, Any]:
    try:
        response = await client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise MobileHelperError("无法连接本机移动端 helper") from exc
    if response.status_code >= _HTTP_ERROR_STATUS:
        raise MobileHelperError(
            f"本机移动端 helper 返回 HTTP {response.status_code}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise MobileHelperError("本机移动端 helper 返回了无效 JSON") from exc
    if not isinstance(payload, dict):
        raise MobileHelperError("本机移动端 helper 返回结构异常")
    return payload


async def _wait_until_ready(
    client: httpx.AsyncClient,
    process: asyncio.subprocess.Process,
    timeout: float,
) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if process.returncode is not None:
            raise MobileHelperError("本机移动端 helper 启动后立即退出")
        try:
            await _json_request(client, "GET", "/api/status")
            return
        except MobileHelperError:
            await asyncio.sleep(_STARTUP_POLL_SECONDS)
    raise MobileHelperError("等待本机移动端 helper 启动超时")


async def _wait_for_job(
    client: httpx.AsyncClient,
    process: asyncio.subprocess.Process,
    job_id: int,
    timeout: float,
) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if process.returncode is not None:
            raise MobileHelperError("本机移动端 helper 在下载时退出")
        jobs = await _json_request(client, "GET", "/api/jobs", params={"id": job_id})
        items = jobs.get("items")
        if (
            not isinstance(items, list)
            or len(items) != 1
            or not isinstance(items[0], dict)
        ):
            raise MobileHelperError("本机移动端 helper 未返回下载任务")
        job = items[0]
        state = str(job.get("state", ""))
        if state == "done":
            return
        if state in {"failed", "canceled"}:
            message = str(job.get("message", "")).strip()
            suffix = f"：{message[:160]}" if message else ""
            raise MobileHelperError(f"本机移动端 helper 下载失败{suffix}")
        if job.get("book_name_options") or job.get("format_options"):
            raise MobileHelperError("本机移动端 helper 要求交互选择，无法自动下载")
        await asyncio.sleep(_JOB_POLL_SECONDS)
    raise MobileHelperError("等待本机移动端 helper 下载超时")


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=_STOP_TIMEOUT_SECONDS)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            return
        await process.wait()


async def fetch_mobile_chapter(
    settings: Config,
    *,
    book_id: str,
    chapter_order: int,
    expected_title: str,
) -> MobileChapterText:
    """通过用户配置的本机 helper 导出一章免费移动端正文。

    调用方必须先依据网页元数据确认该章节免费；本函数不接受远程地址，也不读取
    登录态、Cookie 或用户凭据。
    """

    configured_path = settings.fanqie_mobile_helper_path.strip()
    helper_path = Path(configured_path).expanduser()
    if not helper_path.is_file():
        raise MobileHelperError("未找到配置的本机移动端 helper 可执行文件")
    if chapter_order < 1:
        raise MobileHelperError("阅读页未提供有效章节序号")

    with tempfile.TemporaryDirectory(prefix="yawn-fanqie-mobile-") as temp_dir:
        temp_root = Path(temp_dir)
        output_dir = temp_root / "output"
        output_dir.mkdir()
        port = _find_loopback_port()
        environment = os.environ.copy()
        environment["TOMATO_WEB_ADDR"] = f"{_LOOPBACK_HOST}:{port}"
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            process = await asyncio.create_subprocess_exec(
                str(helper_path),
                "--server",
                "--data-dir",
                str(temp_root),
                cwd=str(temp_root),
                env=environment,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise MobileHelperError("无法启动本机移动端 helper") from exc

        base_url = f"http://{_LOOPBACK_HOST}:{port}"
        timeout = httpx.Timeout(min(settings.fanqie_mobile_helper_timeout, 15.0))
        try:
            async with httpx.AsyncClient(
                base_url=base_url,
                timeout=timeout,
                trust_env=False,
            ) as client:
                await _wait_until_ready(
                    client,
                    process,
                    settings.fanqie_mobile_helper_startup_timeout,
                )
                helper_config = await _json_request(client, "GET", "/api/config/full")
                helper_config.update(
                    {
                        "save_path": str(output_dir),
                        "novel_format": "txt",
                        "bulk_files": True,
                        "ask_format_after_download": False,
                        "use_official_api": True,
                        "enable_audiobook": False,
                        "enable_segment_comments": False,
                        "auto_open_downloaded_files": False,
                        "allow_overwrite_files": True,
                    }
                )
                await _json_request(
                    client,
                    "POST",
                    "/api/config/full",
                    json=helper_config,
                )
                created = await _json_request(
                    client,
                    "POST",
                    "/api/jobs",
                    json={
                        "book_id": book_id,
                        "range_start": chapter_order,
                        "range_end": chapter_order,
                    },
                )
                raw_job_id = created.get("id")
                if isinstance(raw_job_id, bool) or not isinstance(
                    raw_job_id,
                    (int, str),
                ):
                    raise MobileHelperError("本机移动端 helper 返回了无效任务编号")
                try:
                    job_id = int(raw_job_id)
                except (TypeError, ValueError) as exc:
                    raise MobileHelperError(
                        "本机移动端 helper 返回了无效任务编号"
                    ) from exc
                await _wait_for_job(
                    client,
                    process,
                    job_id,
                    settings.fanqie_mobile_helper_timeout,
                )
            return await asyncio.to_thread(
                _read_exported_chapter,
                output_dir,
                expected_title,
                settings.fanqie_max_file_bytes,
            )
        finally:
            await _stop_process(process)


__all__ = [
    "MobileChapterText",
    "MobileHelperError",
    "fetch_mobile_chapter",
    "mobile_helper_configured",
]
