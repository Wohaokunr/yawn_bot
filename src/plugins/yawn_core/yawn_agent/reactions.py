# ruff: noqa: TRY003,TC003
"""表情包库的旧接口（``reaction_id`` + ``search_reactions``）。

真正的存储在 :mod:`sticker_library`：运行时目录默认位于
``AGENT_FILE_ROOT/reactions``，索引文件为 ``index.json``。模型只接触
reaction_id/名字/tags，不直接接触或猜测本地文件路径。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .sticker_library import (
    StickerError,
    available_stickers,
    get_sticker,
    index_path,
    library_root,
    sticker_path,
)

_MAX_RESULTS = 8
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def reaction_root() -> Path:
    return library_root()


def reaction_index_path() -> Path:
    return index_path()


def search_reactions(
    query: str, *, limit: int = 5, group_id: int | None = None
) -> list[dict[str, Any]]:
    """模型主动查表情包：按它给的词在名字/标签/说明里找（只返回本群可用的）。"""

    needle = " ".join(str(query).casefold().split())
    if not needle:
        return []
    tokens = [item for item in re.split(r"[\s,，/|]+", needle) if item]
    scored: list[tuple[int, Any]] = []
    for entry in available_stickers(group_id):
        fields = [
            entry.sticker_id.casefold(),
            entry.name.casefold(),
            *(tag.casefold() for tag in entry.tags),
            entry.description.casefold(),
        ]
        joined = " ".join(fields)
        score = 0
        if needle in fields:
            score += 100
        if needle in joined:
            score += 40
        score += sum(12 for token in tokens if token in joined)
        score += sum(8 for token in tokens if token in fields)
        if score:
            scored.append((score, entry))
    scored.sort(key=lambda item: (-item[0], item[1].sticker_id))
    bounded = max(1, min(int(limit), _MAX_RESULTS))
    return [
        {
            "reaction_id": entry.sticker_id,
            "name": entry.name,
            "tags": list(entry.tags),
            **({"description": entry.description} if entry.description else {}),
        }
        for _score, entry in scored[:bounded]
    ]


def resolve_reaction(reaction_id: str, *, group_id: int | None = None) -> Path:
    target = str(reaction_id).strip()
    if not _ID_RE.fullmatch(target):
        raise ValueError("reaction_id 格式无效")
    entry = get_sticker(target)
    if entry is None or not entry.usable:
        raise ValueError("表情包不存在或已从索引移除")
    if group_id is not None and not entry.available_in(group_id):
        raise ValueError("这个表情包没有对本群开放")
    try:
        return sticker_path(entry)
    except StickerError as exc:
        raise ValueError("表情包不存在或已从索引移除") from exc


__all__ = [
    "reaction_index_path",
    "reaction_root",
    "resolve_reaction",
    "search_reactions",
]
