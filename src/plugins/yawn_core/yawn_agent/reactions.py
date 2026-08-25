# ruff: noqa: TRY003
"""轻量表情包索引。

运行时目录默认位于 ``AGENT_FILE_ROOT/reactions``，索引文件为 ``index.json``。
模型只接触 reaction_id/tags，不直接接触或猜测本地文件路径。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_MAX_RESULTS = 8
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@dataclass(frozen=True, slots=True)
class ReactionEntry:
    reaction_id: str
    file_name: str
    tags: tuple[str, ...]
    description: str = ""


def reaction_root() -> Path:
    root = Path(os.path.realpath(os.environ.get("AGENT_FILE_ROOT", "data/agent_files")))
    return Path(os.path.realpath(str(root / "reactions")))


def reaction_index_path() -> Path:
    return reaction_root() / "index.json"


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _load_entries() -> list[ReactionEntry]:
    index = reaction_index_path()
    if not index.is_file():
        return []
    try:
        payload = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    if isinstance(payload, dict):
        payload = payload.get("reactions", [])
    if not isinstance(payload, list):
        return []

    root = reaction_root()
    entries: list[ReactionEntry] = []
    seen: set[str] = set()
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        reaction_id = str(raw.get("id") or raw.get("reaction_id") or "").strip()
        file_name = str(raw.get("file") or "").strip()
        if not _ID_RE.fullmatch(reaction_id) or reaction_id in seen or not file_name:
            continue
        candidate = Path(os.path.realpath(str(root / file_name)))
        if not _inside(candidate, root) or not candidate.is_file():
            continue
        raw_tags = raw.get("tags", [])
        tags = tuple(
            dict.fromkeys(
                str(item).strip()
                for item in (raw_tags if isinstance(raw_tags, list) else [])
                if str(item).strip()
            )
        )
        description = str(raw.get("description") or "").strip()[:160]
        entries.append(
            ReactionEntry(
                reaction_id=reaction_id,
                file_name=file_name,
                tags=tags,
                description=description,
            )
        )
        seen.add(reaction_id)
    return entries


def search_reactions(query: str, *, limit: int = 5) -> list[dict[str, Any]]:
    needle = " ".join(str(query).casefold().split())
    if not needle:
        return []
    tokens = [item for item in re.split(r"[\s,，/|]+", needle) if item]
    scored: list[tuple[int, ReactionEntry]] = []
    for entry in _load_entries():
        fields = [
            entry.reaction_id.casefold(),
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
    scored.sort(key=lambda item: (-item[0], item[1].reaction_id))
    bounded = max(1, min(int(limit), _MAX_RESULTS))
    return [
        {
            "reaction_id": entry.reaction_id,
            "tags": list(entry.tags),
            **({"description": entry.description} if entry.description else {}),
        }
        for _score, entry in scored[:bounded]
    ]


def resolve_reaction(reaction_id: str) -> Path:
    target = str(reaction_id).strip()
    if not _ID_RE.fullmatch(target):
        raise ValueError("reaction_id 格式无效")
    for entry in _load_entries():
        if entry.reaction_id != target:
            continue
        root = reaction_root()
        candidate = Path(os.path.realpath(str(root / entry.file_name)))
        if not _inside(candidate, root) or not candidate.is_file():
            break
        return candidate
    raise ValueError("表情包不存在或已从索引移除")


__all__ = [
    "ReactionEntry",
    "reaction_index_path",
    "reaction_root",
    "resolve_reaction",
    "search_reactions",
]
