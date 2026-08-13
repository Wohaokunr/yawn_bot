"""番茄任务文件投递层。

文件始终发送给请求者私聊；群内只发送状态文本。不同 OneBot 实现对文件
上传 API 的支持不一致，因此先尝试标准上传 API，再退回运行时可识别的
``file`` 消息段。
"""

# OneBot 文件 API 是同步文件路径接口，使用 pathlib 做越界校验是必要的。
# ruff: noqa: ASYNC240, TC003, TRY003, TRY300

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from nonebot.adapters.onebot.v11 import Message, MessageSegment

logger = logging.getLogger(__name__)


async def send_file_to_user(
    bot: Any,
    user_id: int,
    path: Path,
    filename: str,
) -> None:
    """向请求者私聊发送文件，两个通道均失败时抛出原始异常。"""

    resolved = path.resolve()
    if not resolved.is_file():
        logger.debug(
            "fanqie delivery file missing: user_id=%s path=%s",
            user_id,
            resolved,
        )
        raise FileNotFoundError(resolved)
    try:
        logger.debug(
            "fanqie delivery upload start: user_id=%s path=%s filename=%r",
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
        logger.debug("fanqie delivery upload complete: user_id=%s", user_id)
        return
    except Exception as upload_error:
        logger.debug(
            "fanqie delivery upload failed, fallback to file segment: user_id=%s "
            "error_type=%s",
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
                "文件上传 API 与 file 消息段均发送失败"
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
