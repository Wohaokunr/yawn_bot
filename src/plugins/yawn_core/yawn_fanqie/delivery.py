"""番茄任务文件投递层。

文件始终发送给请求者私聊；群内只发送状态文本。不同 OneBot 实现对文件
上传 API 的支持不一致，因此先尝试标准上传 API，再退回运行时可识别的
``file`` 消息段。
"""

# OneBot 文件 API 是同步文件路径接口，使用 pathlib 做越界校验是必要的。
# ruff: noqa: ASYNC240, TC003, TRY003, TRY300, BLE001

from __future__ import annotations

from pathlib import Path
from typing import Any

from nonebot.adapters.onebot.v11 import Message, MessageSegment


async def send_file_to_user(
    bot: Any,
    user_id: int,
    path: Path,
    filename: str,
) -> None:
    """向请求者私聊发送文件，两个通道均失败时抛出原始异常。"""

    resolved = path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    try:
        await bot.call_api(
            "upload_private_file",
            user_id=user_id,
            file=str(resolved),
            name=filename,
        )
        return
    except Exception:
        try:
            await bot.send_private_msg(
                user_id=user_id,
                message=Message(
                    MessageSegment("file", {"file": str(resolved), "name": filename})
                ),
            )
            return
        except Exception as segment_error:
            raise RuntimeError(
                "文件上传 API 与 file 消息段均发送失败"
            ) from segment_error


async def notify_group(bot: Any, group_id: int | None, text: str) -> None:
    """群聊任务状态通知；通知失败不影响任务数据库状态。"""

    if group_id is None:
        return
    try:
        await bot.send_group_msg(group_id=group_id, message=text)
    except Exception:
        return
