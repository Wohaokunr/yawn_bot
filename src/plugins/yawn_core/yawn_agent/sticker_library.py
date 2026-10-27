# ruff: noqa: E501,C901,PLR0913
"""表情包库：图片文件 + ``index.json`` 元数据（不建表、不需要迁移）。

目录沿用旧的 reaction 库：``AGENT_FILE_ROOT/reactions/``，索引是同目录的
``index.json``。旧索引只有 ``id/file/tags/description`` 四个字段，读取时
照常可用；新字段（名字、启用、审核状态、适用群、来源……）都有默认值。

模型只接触“名字/情绪标签/编号”，永远看不到文件名和本机路径。文件名用内容
哈希，同一张图重复收藏只会得到同一条记录。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import threading
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from .context import now_beijing

INDEX_NAME = "index.json"
STATUS_ACTIVE = "active"
STATUS_PENDING = "pending"
STATUSES = frozenset({STATUS_ACTIVE, STATUS_PENDING})
SOURCES = frozenset({"manual", "upload", "member"})
MAX_STICKERS = 2000
MAX_NAME_CHARS = 16
MAX_TAGS = 8
MAX_TAG_CHARS = 10
MAX_DESCRIPTION_CHARS = 160
MAX_GROUPS = 200
DEFAULT_MAX_BYTES = 3 * 1024 * 1024

_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# 名字/标签里不能有会和 [表情:名字] 标记打架的符号，也不能有换行。
_NAME_STRIP_RE = re.compile(r"[\[\]【】<>:：|｜\r\n\t]+")
_SPACE_RE = re.compile(r"\s+")

# 只收常见的静态/动态图片；按文件头判断，不信扩展名。
_MAGIC: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", ".png", "image/png"),
    (b"\xff\xd8\xff", ".jpg", "image/jpeg"),
    (b"GIF87a", ".gif", "image/gif"),
    (b"GIF89a", ".gif", "image/gif"),
)


class StickerError(ValueError):
    """库操作失败；``reason`` 是给工具/WebUI 用的机器可读原因。"""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def sniff_image(data: bytes) -> tuple[str, str] | None:
    """按文件头返回 ``(扩展名, MIME)``；不是支持的图片返回 None。"""

    head = bytes(data[:16])
    for magic, ext, mime in _MAGIC:
        if head.startswith(magic):
            return ext, mime
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return None


def clean_name(value: object) -> str:
    text = _NAME_STRIP_RE.sub(" ", str(value or ""))
    text = _SPACE_RE.sub(" ", text).strip()
    return text[:MAX_NAME_CHARS].strip()


def clean_tags(values: object) -> tuple[str, ...]:
    items = values if isinstance(values, (list, tuple)) else re.split(r"[,，、/\s]+", str(values or ""))
    output: list[str] = []
    for raw in items:
        tag = clean_name(raw)[:MAX_TAG_CHARS].strip()
        if tag and tag.casefold() not in {item.casefold() for item in output}:
            output.append(tag)
        if len(output) >= MAX_TAGS:
            break
    return tuple(output)


def clean_description(value: object) -> str:
    text = _SPACE_RE.sub(" ", str(value or "").replace("\r", " ").replace("\n", " ")).strip()
    return text[:MAX_DESCRIPTION_CHARS]


def _clean_groups(values: object) -> tuple[int, ...]:
    output: list[int] = []
    for raw in values if isinstance(values, (list, tuple)) else []:
        with contextlib.suppress(TypeError, ValueError):
            number = int(str(raw))
            if number > 0 and number not in output:
                output.append(number)
        if len(output) >= MAX_GROUPS:
            break
    return tuple(output)


def _as_int(value: object) -> int | None:
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


@dataclass(frozen=True, slots=True)
class Sticker:
    sticker_id: str
    file_name: str
    name: str
    tags: tuple[str, ...] = ()
    description: str = ""
    enabled: bool = True
    status: str = STATUS_ACTIVE
    # 空 = 所有群可用；否则只在这些群里出现。
    groups: tuple[int, ...] = ()
    source: str = "manual"
    added_by: int | None = None
    added_group: int | None = None
    sha256: str = ""
    size: int = 0
    mime: str = ""
    created_at: str = ""
    updated_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.enabled and self.status == STATUS_ACTIVE

    def available_in(self, group_id: int | None) -> bool:
        if not self.usable:
            return False
        return not self.groups or (group_id is not None and int(group_id) in self.groups)

    @property
    def label(self) -> str:
        return self.name or (self.tags[0] if self.tags else self.sticker_id)

    def to_index(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.sticker_id,
            "file": self.file_name,
            "name": self.name,
            "tags": list(self.tags),
            "description": self.description,
            "enabled": self.enabled,
            "status": self.status,
            "groups": list(self.groups),
            "source": self.source,
            "added_by": self.added_by,
            "added_group": self.added_group,
            "sha256": self.sha256,
            "size": self.size,
            "mime": self.mime,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        for key, value in self.extra.items():
            data.setdefault(key, value)
        return data

    def public(self) -> dict[str, Any]:
        """给 WebUI 的字段：不含文件名以外的本机信息。"""

        data = asdict(self)
        data.pop("extra", None)
        data["tags"] = list(self.tags)
        data["groups"] = list(self.groups)
        return data


_KNOWN_KEYS = frozenset(
    {
        "id",
        "reaction_id",
        "file",
        "name",
        "tags",
        "description",
        "enabled",
        "status",
        "groups",
        "source",
        "added_by",
        "added_group",
        "sha256",
        "size",
        "mime",
        "created_at",
        "updated_at",
    }
)


def library_root() -> Path:
    root = Path(os.path.realpath(os.environ.get("AGENT_FILE_ROOT", "data/agent_files")))
    return Path(os.path.realpath(str(root / "reactions")))


def index_path() -> Path:
    return library_root() / INDEX_NAME


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _now_text() -> str:
    return now_beijing().strftime("%Y-%m-%d %H:%M:%S")


def _parse_entry(raw: dict[str, Any], root: Path) -> Sticker | None:
    sticker_id = str(raw.get("id") or raw.get("reaction_id") or "").strip()
    file_name = str(raw.get("file") or "").strip()
    if not _ID_RE.fullmatch(sticker_id) or not file_name:
        return None
    candidate = Path(os.path.realpath(str(root / file_name)))
    if not _inside(candidate, root) or candidate == root:
        return None
    tags = clean_tags(raw.get("tags") or [])
    description = clean_description(raw.get("description"))
    name = clean_name(raw.get("name")) or (tags[0] if tags else "") or clean_name(description) or sticker_id[:MAX_NAME_CHARS]
    status = str(raw.get("status") or STATUS_ACTIVE)
    source = str(raw.get("source") or "manual")
    size = _as_int(raw.get("size")) or 0
    return Sticker(
        sticker_id=sticker_id,
        file_name=file_name,
        name=name,
        tags=tags,
        description=description,
        enabled=raw.get("enabled") is not False,
        status=status if status in STATUSES else STATUS_ACTIVE,
        groups=_clean_groups(raw.get("groups")),
        source=source if source in SOURCES else "manual",
        added_by=_as_int(raw.get("added_by")),
        added_group=_as_int(raw.get("added_group")),
        sha256=str(raw.get("sha256") or "")[:64],
        size=size,
        mime=str(raw.get("mime") or "")[:32],
        created_at=str(raw.get("created_at") or "")[:19],
        updated_at=str(raw.get("updated_at") or "")[:19],
        extra={key: value for key, value in raw.items() if key not in _KNOWN_KEYS},
    )


class _Library:
    """进程内缓存 + 原子写；索引文件被外部改了（mtime 变化）就重新读。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cache: tuple[str, float | None, tuple[Sticker, ...]] | None = None

    def _read(self) -> tuple[Sticker, ...]:
        path = index_path()
        try:
            mtime: float | None = path.stat().st_mtime
        except OSError:
            mtime = None
        key = str(path)
        cached = self._cache
        if cached is not None and cached[0] == key and cached[1] == mtime:
            return cached[2]
        entries: list[Sticker] = []
        if mtime is not None:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                payload = []
            if isinstance(payload, dict):
                payload = payload.get("stickers", payload.get("reactions", []))
            root = library_root()
            seen: set[str] = set()
            for raw in payload if isinstance(payload, list) else []:
                if not isinstance(raw, dict):
                    continue
                entry = _parse_entry(raw, root)
                if entry is None or entry.sticker_id in seen:
                    continue
                seen.add(entry.sticker_id)
                entries.append(entry)
                if len(entries) >= MAX_STICKERS:
                    break
        result = tuple(entries)
        self._cache = (key, mtime, result)
        return result

    def all(self) -> tuple[Sticker, ...]:
        with self._lock:
            return self._read()

    def _write(self, entries: list[Sticker]) -> None:
        path = index_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 2, "reactions": [item.to_index() for item in entries]}
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        self._cache = None

    def mutate(self, change: Any) -> Any:
        with self._lock:
            entries = list(self._read())
            result = change(entries)
            self._write(entries)
            return result


_library = _Library()


def all_stickers() -> tuple[Sticker, ...]:
    return _library.all()


def get_sticker(sticker_id: str) -> Sticker | None:
    target = str(sticker_id or "").strip()
    return next((item for item in all_stickers() if item.sticker_id == target), None)


def available_stickers(group_id: int | None) -> list[Sticker]:
    """当前群真正能发的表情（启用、已审核、适用本群、文件还在）。"""

    root = library_root()
    output: list[Sticker] = []
    for item in all_stickers():
        if not item.available_in(group_id):
            continue
        if not (root / item.file_name).is_file():
            continue
        output.append(item)
    return output


def sticker_path(sticker: Sticker) -> Path:
    root = library_root()
    candidate = Path(os.path.realpath(str(root / sticker.file_name)))
    if not _inside(candidate, root) or candidate == root or not candidate.is_file():
        raise StickerError("missing_file", "表情包文件不存在或已被移走")
    return candidate


def _fold(text: str) -> str:
    return _SPACE_RE.sub("", str(text or "")).casefold()


def match_stickers(query: str, group_id: int | None) -> tuple[str, list[Sticker]]:
    """把模型写的 ``[表情:xxx]`` 解析成候选。

    这里只做“模型从菜单里选了哪一项”的对照（编号、名字、情绪标签的精确匹配，
    名字可以省略空格/大小写），不做模糊搜索，更不去猜用户意图。返回
    ``(匹配方式, 候选)``：``id``/``name`` 只会有一张，``tag`` 可能有多张。
    """

    folded = _fold(query)
    if not folded:
        return "none", []
    stickers = available_stickers(group_id)
    for item in stickers:
        if item.sticker_id.casefold() == folded:
            return "id", [item]
    named = [item for item in stickers if _fold(item.name) == folded]
    if named:
        return "name", named[:1]
    tagged = [item for item in stickers if any(_fold(tag) == folded for tag in item.tags)]
    if tagged:
        return "tag", tagged
    return "none", []


def new_sticker_id(sha256: str) -> str:
    return f"s{sha256[:10]}"


def add_sticker(
    data: bytes,
    *,
    name: str = "",
    tags: object = (),
    description: str = "",
    source: str = "upload",
    status: str = STATUS_ACTIVE,
    groups: object = (),
    added_by: int | None = None,
    added_group: int | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> tuple[Sticker, bool]:
    """存一张图进库；同一张图（内容哈希相同）已在库里时返回旧记录和 False。"""

    if not data:
        raise StickerError("empty", "图片是空的")
    if len(data) > max_bytes:
        raise StickerError("too_large", f"图片超过 {max_bytes // 1024} KB 上限")
    kind = sniff_image(data)
    if kind is None:
        raise StickerError("not_image", "只支持 PNG/JPG/GIF/WebP 图片")
    ext, mime = kind
    digest = hashlib.sha256(data).hexdigest()
    clean_tag_list = clean_tags(tags)
    label = clean_name(name) or (clean_tag_list[0] if clean_tag_list else "")
    if not label:
        raise StickerError("no_name", "给表情包起个名字（几个字说明它表达什么）")

    def change(entries: list[Sticker]) -> tuple[Sticker, bool]:
        for item in entries:
            if item.sha256 == digest:
                return item, False
        if len(entries) >= MAX_STICKERS:
            raise StickerError("library_full", f"表情包库最多 {MAX_STICKERS} 张")
        root = library_root()
        root.mkdir(parents=True, exist_ok=True)
        file_name = f"{digest[:16]}{ext}"
        target = root / file_name
        if not target.is_file():
            temporary = target.with_suffix(".part")
            temporary.write_bytes(data)
            temporary.replace(target)
        sticker_id = new_sticker_id(digest)
        existing_ids = {item.sticker_id for item in entries}
        suffix = 1
        while sticker_id in existing_ids:
            suffix += 1
            sticker_id = f"{new_sticker_id(digest)}-{suffix}"
        stamp = _now_text()
        sticker = Sticker(
            sticker_id=sticker_id,
            file_name=file_name,
            name=label,
            tags=clean_tag_list,
            description=clean_description(description),
            enabled=True,
            status=status if status in STATUSES else STATUS_PENDING,
            groups=_clean_groups(groups),
            source=source if source in SOURCES else "upload",
            added_by=added_by,
            added_group=added_group,
            sha256=digest,
            size=len(data),
            mime=mime,
            created_at=stamp,
            updated_at=stamp,
        )
        entries.append(sticker)
        return sticker, True

    return _library.mutate(change)


def update_sticker(sticker_id: str, **fields: Any) -> Sticker:
    """改名字/标签/说明/启用/审核状态/适用群；未知字段忽略。"""

    def change(entries: list[Sticker]) -> Sticker:
        for index, item in enumerate(entries):
            if item.sticker_id != sticker_id:
                continue
            updates: dict[str, Any] = {}
            if "name" in fields and fields["name"] is not None:
                name = clean_name(fields["name"])
                if not name:
                    raise StickerError("no_name", "名字不能为空")
                updates["name"] = name
            if "tags" in fields and fields["tags"] is not None:
                updates["tags"] = clean_tags(fields["tags"])
            if "description" in fields and fields["description"] is not None:
                updates["description"] = clean_description(fields["description"])
            if "enabled" in fields and fields["enabled"] is not None:
                updates["enabled"] = bool(fields["enabled"])
            if "status" in fields and fields["status"] is not None:
                status = str(fields["status"])
                if status not in STATUSES:
                    raise StickerError("bad_status", "status 只能是 active 或 pending")
                updates["status"] = status
            if "groups" in fields and fields["groups"] is not None:
                updates["groups"] = _clean_groups(fields["groups"])
            updated = replace(item, **updates, updated_at=_now_text())
            entries[index] = updated
            return updated
        raise StickerError("not_found", "表情包不存在")

    return _library.mutate(change)


def delete_sticker(sticker_id: str) -> Sticker:
    """删记录；没有别的记录引用同一个文件时把图片也删掉。"""

    def change(entries: list[Sticker]) -> Sticker:
        for index, item in enumerate(entries):
            if item.sticker_id == sticker_id:
                removed = entries.pop(index)
                if not any(other.file_name == removed.file_name for other in entries):
                    root = library_root()
                    candidate = Path(os.path.realpath(str(root / removed.file_name)))
                    if _inside(candidate, root) and candidate != root:
                        candidate.unlink(missing_ok=True)
                return removed
        raise StickerError("not_found", "表情包不存在")

    return _library.mutate(change)


def forget_member(group_id: int, user_id: int) -> int:
    """成员要求忘掉自己时：抹掉他在本群收藏记录上的“谁加的”，图本身留给管理员处理。"""

    target = int(user_id)
    group = int(group_id)

    def mine(item: Sticker) -> bool:
        return item.added_by == target and item.added_group == group

    def change(entries: list[Sticker]) -> int:
        count = 0
        for index, item in enumerate(entries):
            if mine(item):
                entries[index] = replace(item, added_by=None)
                count += 1
        return count

    if not any(mine(item) for item in all_stickers()):
        return 0
    return _library.mutate(change)


def tag_counts(group_id: int | None) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    labels: dict[str, str] = {}
    for item in available_stickers(group_id):
        for tag in item.tags:
            key = _fold(tag)
            counts[key] = counts.get(key, 0) + 1
            labels.setdefault(key, tag)
    return sorted(((labels[key], value) for key, value in counts.items()), key=lambda row: (-row[1], row[0]))


def reset_cache_for_tests() -> None:
    _library._cache = None


__all__ = [
    "DEFAULT_MAX_BYTES",
    "STATUS_ACTIVE",
    "STATUS_PENDING",
    "Sticker",
    "StickerError",
    "add_sticker",
    "all_stickers",
    "available_stickers",
    "clean_description",
    "clean_name",
    "clean_tags",
    "delete_sticker",
    "forget_member",
    "get_sticker",
    "index_path",
    "library_root",
    "match_stickers",
    "sniff_image",
    "sticker_path",
    "tag_counts",
    "update_sticker",
]
