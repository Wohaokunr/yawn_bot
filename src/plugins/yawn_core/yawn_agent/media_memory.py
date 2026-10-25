# ruff: noqa: TID252, E501
"""看过的图片的短期转述：让历史里的 ``[图片]`` 变成“[图片: 一只橘猫趴在键盘上]”。

只在进程内保留、数量和时长都有上限，按 (群, 图片内容哈希) 存，不记 URL/路径，
不写成新的长期聊天副本；群配置里“缓存图片理解结果”打开时转述另外会进数据库
缓存（见 ``media.store_caption``），这里只是给 Prompt 历史用的热缓存。
``AGENT_IMAGE_AUTO_CAPTION=off`` 时历史里不显示转述。
"""

from __future__ import annotations

import time
from collections import OrderedDict

from ..llm import ai_config

CAPTION_LIMIT = 512
CAPTION_TTL_SECONDS = 6 * 3600.0
HISTORY_CAPTION_CHARS = 120

_captions: OrderedDict[tuple[int, str], tuple[float, str]] = OrderedDict()


def auto_caption_mode() -> str:
    mode = str(getattr(ai_config, "agent_image_auto_caption", "triggered") or "triggered")
    return mode if mode in {"off", "triggered", "all"} else "triggered"


def remember(group_id: int, content_hash: str, caption: str) -> None:
    text = " ".join(str(caption or "").split())
    if not content_hash or not text or auto_caption_mode() == "off":
        return
    key = (int(group_id), str(content_hash))
    _captions[key] = (time.monotonic(), text[:2000])
    _captions.move_to_end(key)
    while len(_captions) > CAPTION_LIMIT:
        _captions.popitem(last=False)


def lookup(group_id: int, content_hash: str | None) -> str | None:
    if not content_hash or auto_caption_mode() == "off":
        return None
    key = (int(group_id), str(content_hash))
    entry = _captions.get(key)
    if entry is None:
        return None
    if time.monotonic() - entry[0] > CAPTION_TTL_SECONDS:
        _captions.pop(key, None)
        return None
    return entry[1]


def history_note(group_id: int, content_hash: str | None) -> str | None:
    """历史消息里用的短转述；超长截断并标注这是转述而不是原图。"""

    caption = lookup(group_id, content_hash)
    if not caption:
        return None
    if len(caption) > HISTORY_CAPTION_CHARS:
        caption = caption[:HISTORY_CAPTION_CHARS] + "…"
    return caption


def forget_group(group_id: int) -> int:
    keys = [key for key in _captions if key[0] == int(group_id)]
    for key in keys:
        _captions.pop(key, None)
    return len(keys)


def size() -> int:
    return len(_captions)


def reset_for_tests() -> None:
    _captions.clear()


__all__ = [
    "auto_caption_mode",
    "forget_group",
    "history_note",
    "lookup",
    "remember",
    "reset_for_tests",
    "size",
]
