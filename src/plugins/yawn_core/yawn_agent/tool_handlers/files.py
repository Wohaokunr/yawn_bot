# ruff: noqa: TID252, TRY003
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..context import now_beijing
from ..tool_execution import ToolExecutionContext, ToolHandlerResult
from ..tool_support import (
    _check_downloaded_path,
    _check_local_path,
    _compact_group_file,
    _compact_group_folder,
    _download_allowed_file,
    _payload_list,
    _tool_result_limit,
)

FAMILY = "files"
NAMES = frozenset(
    [
        "list_group_files",
        "get_group_file_link",
        "send_file",
        "create_group_folder",
        "delete_group_file",
        "move_group_file",
        "rename_group_file",
        "delete_group_folder",
    ]
)


async def handle(  # noqa: C901, PLR0912, PLR0915
    name: str, args: dict[str, Any], context: ToolExecutionContext
) -> ToolHandlerResult:
    bot = context.bot
    group_id = context.group_id
    now_beijing()
    if name == "list_group_files":
        limit = _tool_result_limit(args, default=20, maximum=30)
        folder_id = str(args.get("folder_id") or "").strip()
        if folder_id:
            raw_files = await bot.call_api(
                "get_group_files_by_folder",
                group_id=group_id,
                folder_id=folder_id,
            )
        else:
            raw_files = await bot.call_api("get_group_root_files", group_id=group_id)
        files = _payload_list(raw_files, "files", "items", "file_list")
        folders = _payload_list(raw_files, "folders", "folder_list")
        result = {
            "files": [
                _compact_group_file(item)
                for item in files[:limit]
                if isinstance(item, dict)
            ],
            "folders": [
                _compact_group_folder(item)
                for item in folders[:limit]
                if isinstance(item, dict)
            ],
        }
    elif name == "get_group_file_link":
        file_id = str(args.get("file_id") or "").strip()
        if not file_id:
            raise ValueError("file_id 不能为空")
        raw_link = await bot.call_api(
            "get_group_file_url",
            group_id=group_id,
            file_id=file_id,
            busid=int(args["busid"]),
        )
        if isinstance(raw_link, str):
            url = raw_link
        elif isinstance(raw_link, dict):
            url = str(raw_link.get("url") or raw_link.get("download_url") or "")
        else:
            url = ""
        if not url:
            raise ValueError("群文件链接响应格式错误")
        result = {"file_id": file_id, "url": url[:2048]}
    elif name == "send_file":
        file_ref = str(args.get("file", ""))
        temporary: Path | None = None
        if file_ref.startswith(("http://", "https://")):
            temporary, _content_type = await _download_allowed_file(file_ref)
            try:
                path = _check_downloaded_path(temporary)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
        else:
            path = _check_local_path(Path(file_ref))
        try:
            result = await bot.call_api(
                "upload_group_file",
                group_id=group_id,
                file=str(path),
                name=str(args["name"])[:128],
            )
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
    elif name == "create_group_folder":
        folder_name = str(args.get("name") or "").strip()[:120]
        if not folder_name:
            raise ValueError("name 不能为空")
        result = await bot.call_api(
            "create_group_file_folder",
            group_id=group_id,
            name=folder_name,
        )
    elif name == "delete_group_file":
        result = await bot.call_api(
            "delete_group_file",
            group_id=group_id,
            file_id=str(args["file_id"]),
            busid=int(args["busid"]),
        )
    elif name == "move_group_file":
        result = await bot.call_api(
            "move_group_file",
            group_id=group_id,
            file_id=str(args["file_id"]),
            target_dir=str(args["target_dir"]),
        )
    elif name == "rename_group_file":
        result = await bot.call_api(
            "rename_group_file",
            group_id=group_id,
            file_id=str(args["file_id"]),
            current_parent_directory=str(args["current_parent_directory"]),
            new_name=str(args["new_name"])[:160],
        )
    elif name == "delete_group_folder":
        result = await bot.call_api(
            "delete_group_folder",
            group_id=group_id,
            folder_id=str(args["folder_id"]),
        )
    else:
        raise ValueError(f"{FAMILY} handler 不支持工具: {name}")
    return ToolHandlerResult(result)
