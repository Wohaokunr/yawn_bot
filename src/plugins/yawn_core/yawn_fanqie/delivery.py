"""番茄任务文件投递层。

文件始终发送给请求者私聊；群内只发送状态文本。生产环境中 YawnBot 与
NapCat 可能运行在不同容器，不能把 YawnBot 容器内的本地路径直接交给
NapCat。因此优先通过 NapCat Stream API 将文件内容传到协议端，再调用
私聊文件上传接口；旧版协议端才退回传统本地路径与 ``file`` 消息段。
"""

# OneBot 文件 API 是同步文件路径接口，使用 pathlib 做越界校验是必要的。
# ruff: noqa: ASYNC240, TC003, TRY003, TRY300

from __future__ import annotations

import base64
import hashlib
import logging
import uuid
from pathlib import Path
from typing import Any

from nonebot.adapters.onebot.v11 import Message, MessageSegment

logger = logging.getLogger(__name__)

_STREAM_CHUNK_BYTES = 512 * 1024
_STREAM_FILE_RETENTION_MS = 10 * 60 * 1000


def _stream_payload(result: Any) -> dict[str, Any]:
    """兼容 NoneBot 与不同 NapCat 版本的 Stream API 返回形态。"""

    if not isinstance(result, dict):
        return {}
    nested = result.get("data")
    if isinstance(nested, dict):
        return nested
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_STREAM_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


async def _upload_file_stream(bot: Any, path: Path, filename: str) -> str:
    """将本地文件跨容器分片传入 NapCat，并返回 NapCat 可访问的临时路径。"""

    file_size = path.stat().st_size
    if file_size <= 0:
        raise ValueError("不能发送空文件")

    stream_id = uuid.uuid4().hex
    total_chunks = (file_size + _STREAM_CHUNK_BYTES - 1) // _STREAM_CHUNK_BYTES
    expected_sha256 = _sha256(path)

    logger.debug(
        "fanqie delivery stream upload start: stream_id=%s path=%s "
        "bytes=%s chunks=%s filename=%r",
        stream_id,
        path,
        file_size,
        total_chunks,
        filename,
    )

    with path.open("rb") as stream:
        for chunk_index in range(total_chunks):
            chunk = stream.read(_STREAM_CHUNK_BYTES)
            if not chunk:
                raise RuntimeError("读取番茄小说成品文件时意外提前结束")
            await bot.call_api(
                "upload_file_stream",
                stream_id=stream_id,
                chunk_data=base64.b64encode(chunk).decode("ascii"),
                chunk_index=chunk_index,
                total_chunks=total_chunks,
                file_size=file_size,
                expected_sha256=expected_sha256,
                filename=filename,
                file_retention=_STREAM_FILE_RETENTION_MS,
            )

    result = await bot.call_api(
        "upload_file_stream",
        stream_id=stream_id,
        is_complete=True,
        file_retention=_STREAM_FILE_RETENTION_MS,
    )
    payload = _stream_payload(result)
    remote_path = payload.get("file_path")
    if not isinstance(remote_path, str) or not remote_path.strip():
        raise RuntimeError("NapCat Stream API 未返回可用的 file_path")

    logger.debug(
        "fanqie delivery stream upload complete: stream_id=%s remote_path=%s",
        stream_id,
        remote_path,
    )
    return remote_path


async def send_file_to_user(
    bot: Any,
    user_id: int,
    path: Path,
    filename: str,
) -> None:
    """向请求者私聊发送文件，并兼容跨容器与旧版 OneBot 实现。"""

    resolved = path.resolve()
    if not resolved.is_file():
        logger.debug(
            "fanqie delivery file missing: user_id=%s path=%s",
            user_id,
            resolved,
        )
        raise FileNotFoundError(resolved)

    try:
        remote_path = await _upload_file_stream(bot, resolved, filename)
        logger.debug(
            "fanqie delivery streamed private upload start: user_id=%s "
            "remote_path=%s filename=%r",
            user_id,
            remote_path,
            filename,
        )
        await bot.call_api(
            "upload_private_file",
            user_id=user_id,
            file=remote_path,
            name=filename,
            upload_file=True,
        )
        logger.debug(
            "fanqie delivery streamed private upload complete: user_id=%s",
            user_id,
        )
        return
    except Exception as stream_error:
        logger.debug(
            "fanqie delivery stream path failed, fallback to legacy upload: "
            "user_id=%s error_type=%s",
            user_id,
            type(stream_error).__name__,
            exc_info=True,
        )

    try:
        logger.debug(
            "fanqie delivery legacy upload start: user_id=%s path=%s filename=%r",
            user_id,
            resolved,
            filename,
        )
        await bot.call_api(
            "upload_private_file",
            user_id=user_id,
            file=str(resolved),
            name=filename,
        )
        logger.debug("fanqie delivery legacy upload complete: user_id=%s", user_id)
        return
    except Exception as upload_error:
        logger.debug(
            "fanqie delivery legacy upload failed, fallback to file segment: "
            "user_id=%s error_type=%s",
            user_id,
            type(upload_error).__name__,
            exc_info=True,
        )
        try:
            await bot.send_private_msg(
                user_id=user_id,
                message=Message(
                    MessageSegment("file", {"file": str(resolved), "name": filename})
                ),
            )
            logger.debug(
                "fanqie delivery file segment complete: user_id=%s", user_id
            )
            return
        except Exception as segment_error:
            logger.debug(
                "fanqie delivery file segment failed: user_id=%s error_type=%s",
                user_id,
                type(segment_error).__name__,
                exc_info=True,
            )
            raise RuntimeError(
                "NapCat 文件流、传统文件上传 API 与 file 消息段均发送失败"
            ) from segment_error


async def notify_group(bot: Any, group_id: int | None, text: str) -> None:
    """群聊任务状态通知；通知失败不影响任务数据库状态。"""

    if group_id is None:
        logger.debug("fanqie group notification skipped: reason=private_chat")
        return
    try:
        logger.debug("fanqie group notification start: group_id=%s", group_id)
        await bot.send_group_msg(group_id=group_id, message=text)
        logger.debug("fanqie group notification complete: group_id=%s", group_id)
    except Exception:
        logger.debug(
            "fanqie group notification failed: group_id=%s",
            group_id,
            exc_info=True,
        )
        return
