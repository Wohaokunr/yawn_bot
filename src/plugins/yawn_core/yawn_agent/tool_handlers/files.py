# ruff: noqa: TID252, TRY003, E501
"""群文件与机器人文件库工具。"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any

from ..log import dbg
from ..sandbox_refs import EXPORT_DIR_NAME, is_export_path, is_sandbox_ref
from ..sandbox_refs import discard as discard_sandbox_export
from ..sandbox_refs import materialize as materialize_sandbox_ref
from ..tool_args import ERROR_NOT_FOUND, ToolArgumentError, ToolError
from ..tool_execution import ToolExecutionContext, ToolHandlerResult
from ..tool_support import (
    BLOCKED_FILE_EXTENSIONS,
    MAX_FILE_BYTES,
    _check_downloaded_path,
    _compact_group_file,
    _compact_group_folder,
    _download_allowed_file,
    _payload_list,
    _tool_result_limit,
    agent_file_root,
    check_file_extension,
    readable_size,
    resolve_agent_file,
    sanitize_upload_name,
    sort_by_time_desc,
    validate_file_component,
)

FAMILY = "files"
NAMES = frozenset(
    [
        "list_group_files",
        "search_group_files",
        "get_group_file_space",
        "list_agent_files",
        "get_group_file_link",
        "send_file",
        "create_group_folder",
        "delete_group_file",
        "move_group_file",
        "rename_group_file",
        "delete_group_folder",
    ]
)
# NapCat/go-cqhttp 的群文件 busid 绝大多数是 102。
DEFAULT_FILE_BUSID = 102
_MAX_SEARCH_FOLDERS = 8
_MAX_AGENT_FILE_SCAN = 500


async def _root_listing(bot: Any, group_id: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw = await bot.call_api("get_group_root_files", group_id=group_id)
    files = [item for item in _payload_list(raw, "files", "items", "file_list") if isinstance(item, dict)]
    folders = [item for item in _payload_list(raw, "folders", "folder_list") if isinstance(item, dict)]
    return files, folders


def _list_agent_files(keyword: str, limit: int) -> dict[str, Any]:
    root = agent_file_root()
    items: list[dict[str, Any]] = []
    total = 0
    if root.is_dir():
        for index, path in enumerate(sorted(root.rglob("*"))):
            if index >= _MAX_AGENT_FILE_SCAN:
                break
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            if any(part.startswith(".") for part in relative.parts):
                continue
            # reactions/ 是表情包库，走 search_reactions，不作为群文件发送。
            if relative.parts[0] == "reactions":
                continue
            if path.suffix.lower() in BLOCKED_FILE_EXTENSIONS:
                continue
            name = relative.as_posix()
            if keyword and keyword not in name.casefold():
                continue
            total += 1
            if len(items) >= limit:
                continue
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            items.append(
                {
                    "file": name,
                    "size": readable_size(path.stat().st_size),
                    "type": mime.split("/", 1)[0],
                }
            )
    return {"items": items, "count": len(items), "total": total, "truncated": total > len(items)}


async def _require_folder(bot: Any, group_id: int, folder_id: str) -> dict[str, Any]:
    _files, folders = await _root_listing(bot, group_id)
    for folder in folders:
        compact = _compact_group_folder(folder)
        if str(compact.get("folder_id")) == folder_id:
            return compact
    raise ToolError(
        "群文件里没有这个文件夹",
        code=ERROR_NOT_FOUND,
        hint="先用 list_group_files 查看文件夹 folder_id",
    )


async def _send_file(args: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
    bot = context.bot
    group_id = context.group_id
    file_ref = str(args.get("file") or "").strip()
    folder_id = str(args.get("folder_id") or "").strip()
    folder = await _require_folder(bot, group_id, folder_id) if folder_id else None
    temporary: Path | None = None
    if is_sandbox_ref(file_ref):
        # 代码沙箱产物：只解析当前群工作区，取回后作为临时文件上传。
        temporary = await materialize_sandbox_ref(file_ref, group_id, max_bytes=MAX_FILE_BYTES)
        path = temporary
        source_name = path.name
    elif file_ref.startswith(("http://", "https://")):
        temporary, _content_type = await _download_allowed_file(file_ref)
        try:
            path = _check_downloaded_path(temporary)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        source_name = Path(file_ref.split("?", 1)[0]).name or path.name
    else:
        if EXPORT_DIR_NAME in Path(file_ref).parts or is_export_path(Path(file_ref)):
            raise ToolError(
                "沙箱导出目录不能直接引用",
                code=ERROR_NOT_FOUND,
                hint="沙箱里的文件请用 sandbox:文件名 发送",
            )
        try:
            path = resolve_agent_file(file_ref)
        except ValueError as exc:
            raise ToolError(
                "机器人文件库里没有这个文件",
                code=ERROR_NOT_FOUND,
                hint="先用 list_agent_files 查看可发送的文件名",
            ) from exc
        source_name = path.name
    try:
        upload_name = sanitize_upload_name(args.get("name"), source_name=source_name)
        check_file_extension(source_name, upload_name)
        params: dict[str, Any] = {
            "group_id": group_id,
            # 沙箱产物用 base64 交给 OneBot 实现端，不依赖它能读到 Bot 容器的文件。
            "file": (
                "base64://" + base64.b64encode(path.read_bytes()).decode("ascii")
                if is_export_path(path)
                else str(path)
            ),
            "name": upload_name,
        }
        if folder_id:
            params["folder"] = folder_id
        dbg(f"群 {group_id} send_file: name={upload_name!r} folder={folder_id or '/'}")
        await bot.call_api("upload_group_file", **params)
        size = path.stat().st_size
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
            if is_export_path(temporary):
                discard_sandbox_export(temporary)
    result: dict[str, Any] = {"uploaded": True, "name": upload_name, "size": readable_size(size)}
    if folder is not None:
        result["folder"] = folder.get("name") or folder_id
    return result


async def _search_files(args: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
    bot = context.bot
    group_id = context.group_id
    keyword = str(args.get("keyword") or "").strip().casefold()
    if not keyword:
        raise ToolArgumentError("keyword 不能为空")
    limit = _tool_result_limit(args, default=10, maximum=20)
    files, folders = await _root_listing(bot, group_id)
    matches: list[dict[str, Any]] = []
    for item in files:
        compact = _compact_group_file(item)
        if keyword in str(compact.get("name") or "").casefold():
            matches.append(compact)
    matched_folders: list[dict[str, Any]] = []
    searched = 0
    for folder in folders:
        compact_folder = _compact_group_folder(folder)
        folder_name = str(compact_folder.get("name") or "")
        if keyword in folder_name.casefold():
            matched_folders.append(compact_folder)
        folder_id = compact_folder.get("folder_id")
        if not folder_id or searched >= _MAX_SEARCH_FOLDERS:
            continue
        searched += 1
        raw = await bot.call_api(
            "get_group_files_by_folder", group_id=group_id, folder_id=str(folder_id)
        )
        for item in _payload_list(raw, "files", "items", "file_list"):
            if not isinstance(item, dict):
                continue
            compact = _compact_group_file(item)
            if keyword in str(compact.get("name") or "").casefold():
                compact["folder"] = folder_name
                matches.append(compact)
    ordered = sort_by_time_desc(matches)
    return {
        "files": ordered[:limit],
        "folders": matched_folders[:limit],
        "count": len(ordered),
        "searched_folders": searched,
        "truncated": len(folders) > searched,
    }


async def handle(  # noqa: C901, PLR0912, PLR0915
    name: str, args: dict[str, Any], context: ToolExecutionContext
) -> ToolHandlerResult:
    bot = context.bot
    group_id = context.group_id
    refund = False
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
        result: Any = {
            "files": sort_by_time_desc(
                [_compact_group_file(item) for item in files if isinstance(item, dict)]
            )[:limit],
            "folders": [
                _compact_group_folder(item)
                for item in folders[:limit]
                if isinstance(item, dict)
            ],
        }
    elif name == "search_group_files":
        result = await _search_files(args, context)
    elif name == "get_group_file_space":
        raw = await bot.call_api("get_group_file_system_info", group_id=group_id)
        if not isinstance(raw, dict):
            raise ValueError("群文件空间响应格式错误")
        used = raw.get("used_space")
        total = raw.get("total_space")
        result = {
            key: value
            for key, value in {
                "file_count": raw.get("file_count"),
                "file_limit": raw.get("limit_count"),
                "used": readable_size(used),
                "total": readable_size(total),
                "free": (
                    readable_size(int(total) - int(used))
                    if isinstance(total, int) and isinstance(used, int)
                    else None
                ),
            }.items()
            if value is not None
        }
    elif name == "list_agent_files":
        keyword = str(args.get("keyword") or "").strip().casefold()
        result = _list_agent_files(keyword, _tool_result_limit(args, default=20, maximum=50))
    elif name == "get_group_file_link":
        file_id = str(args.get("file_id") or "").strip()
        if not file_id:
            raise ToolArgumentError("file_id 不能为空")
        busid = args.get("busid")
        raw_link = await bot.call_api(
            "get_group_file_url",
            group_id=group_id,
            file_id=file_id,
            busid=int(busid) if busid is not None else DEFAULT_FILE_BUSID,
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
        result = await _send_file(args, context)
    elif name == "create_group_folder":
        folder_name = validate_file_component(args.get("name"), field_name="name")
        _files, folders = await _root_listing(bot, group_id)
        existing = next(
            (
                _compact_group_folder(item)
                for item in folders
                if str(_compact_group_folder(item).get("name") or "") == folder_name
            ),
            None,
        )
        if existing is not None:
            # 幂等：同名文件夹已存在时不重复创建，执行器据此退还预扣配额。
            result = {"created": False, "existed": True, **existing}
            refund = True
        else:
            await bot.call_api(
                "create_group_file_folder", group_id=group_id, name=folder_name
            )
            result = {"created": True, "name": folder_name}
    elif name == "delete_group_file":
        busid = args.get("busid")
        await bot.call_api(
            "delete_group_file",
            group_id=group_id,
            file_id=str(args["file_id"]),
            busid=int(busid) if busid is not None else DEFAULT_FILE_BUSID,
        )
        result = {"deleted": True, "file_id": str(args["file_id"])}
    elif name == "move_group_file":
        await bot.call_api(
            "move_group_file",
            group_id=group_id,
            file_id=str(args["file_id"]),
            target_dir=str(args["target_dir"]),
        )
        result = {"moved": True, "file_id": str(args["file_id"])}
    elif name == "rename_group_file":
        new_name = validate_file_component(args.get("new_name"), field_name="new_name", limit=160)
        check_file_extension(new_name)
        await bot.call_api(
            "rename_group_file",
            group_id=group_id,
            file_id=str(args["file_id"]),
            current_parent_directory=str(args["current_parent_directory"]),
            new_name=new_name,
        )
        result = {"renamed": True, "new_name": new_name}
    elif name == "delete_group_folder":
        await bot.call_api(
            "delete_group_folder",
            group_id=group_id,
            folder_id=str(args["folder_id"]),
        )
        result = {"deleted": True, "folder_id": str(args["folder_id"])}
    else:
        raise ValueError(f"{FAMILY} handler 不支持工具: {name}")
    return ToolHandlerResult(result, refund_quota=refund)
