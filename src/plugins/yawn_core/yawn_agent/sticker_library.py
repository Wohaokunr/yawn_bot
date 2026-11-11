# ruff: noqa: E501, C901, PLR0913,PLR0912,PLW2901
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
SOURCES = frozenset({"manual", "upload", "member", "stolen"})
SOURCE_LABELS = {"manual": "手动放入", "upload": "WebUI 上传", "member": "群友收藏", "stolen": "Bot 偷的"}
MAX_STICKERS = 2000
MAX_TRASH = 300
INDEX_VERSION = 3
MAX_NAME_CHARS = 16
MAX_TAGS = 8
MAX_TAG_CHARS = 10
MAX_DESCRIPTION_CHARS = 160
MAX_GROUPS = 200
MAX_ALIASES = 4
MAX_PACKS = 60
MAX_PACKS_PER_STICKER = 6
MAX_NOTE_CHARS = 200
MAX_BLOCKED_HASHES = 2000
# 权重：0=不进菜单（只有模型点名才发）、1=普通、2=多用、3=最爱。
WEIGHT_MIN, WEIGHT_MAX, WEIGHT_DEFAULT = 0, 3, 1
WEIGHT_LABELS = {0: "不主动用", 1: "普通", 2: "多用", 3: "最爱"}
DEFAULT_MAX_BYTES = 3 * 1024 * 1024

_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_PACK_ID_RE = re.compile(r"^[a-z0-9]{1,16}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
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


_PLACEHOLDER_RE = re.compile(
    r"^(?:img|image|pic|photo|screenshot|mmexport|wx_camera|qq图片|微信图片|截图|屏幕截图|下载|未命名|新建|tmp|temp|dsc|s[0-9a-f]{6,})[\s_\-0-9a-f]*$",
    re.IGNORECASE,
)
_HEXISH_RE = re.compile(r"^[0-9a-f_\-]{8,}$", re.IGNORECASE)


def looks_like_placeholder(name: str, sticker_id: str = "") -> bool:
    """名字是不是文件名/编号这种“没起过名”的占位（AI 补全时可以覆盖）。纯函数。"""

    text = str(name or "").strip()
    if not text:
        return True
    if sticker_id and text.casefold() == sticker_id[:MAX_NAME_CHARS].casefold():
        return True
    folded = text.replace(" ", "")
    return bool(_PLACEHOLDER_RE.match(folded) or _HEXISH_RE.match(folded) or folded.isdigit())


def clean_aliases(values: object, *, name: str = "") -> tuple[str, ...]:
    """别名：模型写 ``[表情:别名]`` 也能对上这张；去重、去掉和名字相同的。"""

    items = values if isinstance(values, (list, tuple)) else re.split(r"[,，、/]+", str(values or ""))
    output: list[str] = []
    taken = {_fold(name)} if name else set()
    for raw in items:
        alias = clean_name(raw)
        key = _fold(alias)
        if alias and key not in taken:
            taken.add(key)
            output.append(alias)
        if len(output) >= MAX_ALIASES:
            break
    return tuple(output)


def clean_note(value: object) -> str:
    text = _SPACE_RE.sub(" ", str(value or "").replace("\r", " ").replace("\n", " ")).strip()
    return text[:MAX_NOTE_CHARS]


def clean_weight(value: object) -> int:
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return WEIGHT_DEFAULT
    return min(max(number, WEIGHT_MIN), WEIGHT_MAX)


def _clean_pack_ids(values: object) -> tuple[str, ...]:
    output: list[str] = []
    for raw in values if isinstance(values, (list, tuple)) else []:
        text = str(raw or "").strip()
        if _PACK_ID_RE.fullmatch(text) and text not in output:
            output.append(text)
        if len(output) >= MAX_PACKS_PER_STICKER:
            break
    return tuple(output)


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
    # ── 3.0 新字段（旧索引里没有，全部有默认值）──
    aliases: tuple[str, ...] = ()
    # 置顶：只要本群能用，就排在菜单最前面。
    pinned: bool = False
    weight: int = WEIGHT_DEFAULT
    # 所属合集（pack id）；合集可以整体停用或限定群。
    packs: tuple[str, ...] = ()
    # 管理员备注：只在 WebUI 看，不进提示词。
    note: str = ""
    width: int = 0
    height: int = 0
    frames: int = 0
    # 这一张每天最多发几次（全部群合计）；0 = 不单独限制。
    daily_cap: int = 0
    # 进回收站的时间；非空 = 已删除（还能恢复）。
    deleted_at: str = ""

    @property
    def deleted(self) -> bool:
        return bool(self.deleted_at)

    @property
    def animated(self) -> bool:
        return self.frames > 1

    @property
    def usable(self) -> bool:
        return self.enabled and self.status == STATUS_ACTIVE and not self.deleted_at

    def available_in(self, group_id: int | None) -> bool:
        if not self.usable:
            return False
        return not self.groups or (group_id is not None and int(group_id) in self.groups)

    @property
    def label(self) -> str:
        return self.name or (self.tags[0] if self.tags else self.sticker_id)

    @property
    def ai(self) -> dict[str, Any]:
        """AI 标注元数据（模型、把握、图上文字、是否安全、时间）；没标注过为空。"""

        value = self.extra.get("ai")
        return dict(value) if isinstance(value, dict) else {}

    @property
    def origin(self) -> dict[str, Any]:
        """偷图/收藏的来源（群、发图的人、消息编号、私聊）；只给 WebUI 看，不进提示词。"""

        value = self.extra.get("origin")
        return dict(value) if isinstance(value, dict) else {}

    @property
    def ocr_text(self) -> str:
        return str(self.ai.get("text") or "")[:120]

    @property
    def can_undo_label(self) -> bool:
        return isinstance(self.extra.get("label_undo"), dict)

    @property
    def placeholder_name(self) -> bool:
        return looks_like_placeholder(self.name, self.sticker_id)

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
        # 新字段只在不是默认值时写，旧索引保持原样、文件也不变大。
        optional: dict[str, Any] = {
            "aliases": list(self.aliases),
            "pinned": self.pinned,
            "packs": list(self.packs),
            "note": self.note,
            "width": self.width,
            "height": self.height,
            "frames": self.frames,
            "daily_cap": self.daily_cap,
            "deleted_at": self.deleted_at,
        }
        data.update({key: value for key, value in optional.items() if value})
        if self.weight != WEIGHT_DEFAULT:
            data["weight"] = self.weight
        for key, value in self.extra.items():
            data.setdefault(key, value)
        return data

    def public(self) -> dict[str, Any]:
        """给 WebUI 的字段：不含文件名以外的本机信息。"""

        data = asdict(self)
        data.pop("extra", None)
        data["tags"] = list(self.tags)
        data["groups"] = list(self.groups)
        data["aliases"] = list(self.aliases)
        data["packs"] = list(self.packs)
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
        "aliases",
        "pinned",
        "weight",
        "packs",
        "note",
        "width",
        "height",
        "frames",
        "daily_cap",
        "deleted_at",
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
        aliases=clean_aliases(raw.get("aliases") or [], name=name),
        pinned=raw.get("pinned") is True,
        weight=clean_weight(raw.get("weight", WEIGHT_DEFAULT)),
        packs=_clean_pack_ids(raw.get("packs")),
        note=clean_note(raw.get("note")),
        width=_as_int(raw.get("width")) or 0,
        height=_as_int(raw.get("height")) or 0,
        frames=_as_int(raw.get("frames")) or 0,
        daily_cap=min(_as_int(raw.get("daily_cap")) or 0, 1000),
        deleted_at=str(raw.get("deleted_at") or "")[:19],
    )


@dataclass(frozen=True, slots=True)
class StickerPack:
    """合集：一组表情包，可以整体停用，或限定只在某些群出现。"""

    pack_id: str
    name: str
    description: str = ""
    enabled: bool = True
    groups: tuple[int, ...] = ()
    created_at: str = ""

    def open_to(self, group_id: int | None) -> bool:
        if not self.enabled:
            return False
        return not self.groups or (group_id is not None and int(group_id) in self.groups)

    def to_index(self) -> dict[str, Any]:
        return {
            "id": self.pack_id,
            "name": self.name,
            "description": self.description,
            "enabled": self.enabled,
            "groups": list(self.groups),
            "created_at": self.created_at,
        }


def _parse_pack(raw: object) -> StickerPack | None:
    if not isinstance(raw, dict):
        return None
    pack_id = str(raw.get("id") or "").strip()
    name = clean_name(raw.get("name"))
    if not _PACK_ID_RE.fullmatch(pack_id) or not name:
        return None
    return StickerPack(
        pack_id=pack_id,
        name=name,
        description=clean_description(raw.get("description"))[:60],
        enabled=raw.get("enabled") is not False,
        groups=_clean_groups(raw.get("groups")),
        created_at=str(raw.get("created_at") or "")[:19],
    )


@dataclass(slots=True)
class _State:
    """索引文件的全部内容：表情包（含回收站里的）、合集、拉黑的图片哈希。"""

    stickers: list[Sticker] = field(default_factory=list)
    packs: list[StickerPack] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    # 索引读不出来（坏 JSON）：下一次写入前先把坏文件挪到一边，不直接覆盖。
    corrupt: bool = False
    recovered_from_backup: bool = False


def backup_path() -> Path:
    return index_path().with_suffix(".bak")


def _load_payload(path: Path) -> tuple[Any, bool]:
    """返回 ``(payload, ok)``；文件不存在算 ok 的空库。"""

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], True
    except (OSError, UnicodeError):
        return [], False
    try:
        return json.loads(text), True
    except json.JSONDecodeError:
        return [], False


def _parse_state(payload: Any) -> _State:
    state = _State()
    packs_raw: Any = []
    blocked_raw: Any = []
    if isinstance(payload, dict):
        packs_raw = payload.get("packs") or []
        blocked_raw = payload.get("blocked") or []
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
        state.stickers.append(entry)
        if len(state.stickers) >= MAX_STICKERS + MAX_TRASH:
            break
    pack_ids: set[str] = set()
    for raw in packs_raw if isinstance(packs_raw, list) else []:
        pack = _parse_pack(raw)
        if pack is None or pack.pack_id in pack_ids:
            continue
        pack_ids.add(pack.pack_id)
        state.packs.append(pack)
        if len(state.packs) >= MAX_PACKS:
            break
    for raw in blocked_raw if isinstance(blocked_raw, list) else []:
        digest = str(raw or "").strip().casefold()
        if _SHA_RE.fullmatch(digest) and digest not in state.blocked:
            state.blocked.append(digest)
    state.blocked = state.blocked[-MAX_BLOCKED_HASHES:]
    return state


class _Library:
    """进程内缓存 + 原子写；索引文件被外部改了（mtime 变化）就重新读。

    写之前把上一版留成 ``index.bak``；索引坏了先读备份，备份也没有时把坏文件改名
    留着（``index.corrupt-时间.json``），绝不拿一个空库把它覆盖掉。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cache: tuple[str, float | None, _State] | None = None

    def _read(self) -> _State:
        path = index_path()
        try:
            mtime: float | None = path.stat().st_mtime
        except OSError:
            mtime = None
        key = str(path)
        cached = self._cache
        if cached is not None and cached[0] == key and cached[1] == mtime:
            return cached[2]
        state = _State()
        if mtime is not None:
            payload, ok = _load_payload(path)
            if ok:
                state = _parse_state(payload)
            else:
                backup, backup_ok = _load_payload(backup_path())
                if backup_ok and backup:
                    state = _parse_state(backup)
                    state.recovered_from_backup = True
                state.corrupt = True
        self._cache = (key, mtime, state)
        return state

    def state(self) -> _State:
        with self._lock:
            return self._read()

    def all(self) -> tuple[Sticker, ...]:
        with self._lock:
            return tuple(self._read().stickers)

    def _write(self, state: _State) -> None:
        path = index_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {"version": INDEX_VERSION, "reactions": [item.to_index() for item in state.stickers]}
        if state.packs:
            payload["packs"] = [pack.to_index() for pack in state.packs]
        if state.blocked:
            payload["blocked"] = list(state.blocked[-MAX_BLOCKED_HASHES:])
        if path.is_file():
            if state.corrupt:
                stamp = now_beijing().strftime("%Y%m%d-%H%M%S")
                with contextlib.suppress(OSError):
                    path.replace(path.with_name(f"index.corrupt-{stamp}.json"))
            else:
                with contextlib.suppress(OSError):
                    backup_path().write_bytes(path.read_bytes())
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        self._cache = None

    def mutate(self, change: Any) -> Any:
        """``change(entries)`` 改表情包列表（兼容旧写法）。"""

        return self.mutate_state(lambda state: change(state.stickers))

    def mutate_state(self, change: Any) -> Any:
        with self._lock:
            current = self._read()
            state = _State(list(current.stickers), list(current.packs), list(current.blocked), current.corrupt)
            result = change(state)
            self._write(state)
            return result


_library = _Library()


def library_health() -> dict[str, bool]:
    """索引文件本身的状态：坏了没有、是不是从备份读回来的。"""

    state = _library.state()
    return {"corrupt": state.corrupt, "recoveredFromBackup": state.recovered_from_backup, "hasBackup": backup_path().is_file()}


def all_stickers() -> tuple[Sticker, ...]:
    """库里的表情包（不含回收站）。"""

    return tuple(item for item in _library.all() if not item.deleted_at)


def trashed_stickers() -> tuple[Sticker, ...]:
    return tuple(item for item in _library.all() if item.deleted_at)


def all_packs() -> tuple[StickerPack, ...]:
    return tuple(_library.state().packs)


def blocked_hashes() -> tuple[str, ...]:
    return tuple(_library.state().blocked)


def get_sticker(sticker_id: str, *, include_trashed: bool = False) -> Sticker | None:
    target = str(sticker_id or "").strip()
    item = next((item for item in _library.all() if item.sticker_id == target), None)
    if item is not None and item.deleted_at and not include_trashed:
        return None
    return item


def pack_allows(item: Sticker, group_id: int | None, packs: dict[str, StickerPack]) -> bool:
    """合集这一层：不在任何（还存在的）合集里 → 放行；否则至少有一个开着且对本群开放的合集。"""

    owned = [packs[pack_id] for pack_id in item.packs if pack_id in packs]
    if not owned:
        return True
    return any(pack.open_to(group_id) for pack in owned)


def is_available(item: Sticker, group_id: int | None) -> bool:
    """这张图本群现在能不能发（启用、已审核、适用群、合集没停）；不查文件在不在。"""

    if not item.available_in(group_id):
        return False
    return pack_allows(item, group_id, {pack.pack_id: pack for pack in all_packs()})


def available_stickers(group_id: int | None) -> list[Sticker]:
    """当前群真正能发的表情（启用、已审核、适用本群、合集开着、文件还在）。"""

    root = library_root()
    state = _library.state()
    packs = {pack.pack_id: pack for pack in state.packs}
    output: list[Sticker] = []
    for item in state.stickers:
        if not item.available_in(group_id) or not pack_allows(item, group_id, packs):
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


def match_stickers(
    query: str,
    group_id: int | None,
    *,
    stickers: tuple[Sticker, ...] | list[Sticker] | None = None,
) -> tuple[str, list[Sticker]]:
    """把模型写的 ``[表情:xxx]`` 解析成候选。

    这里只做“模型从菜单里选了哪一项”的对照（编号、名字、别名、情绪标签的精确匹配，
    名字可以省略空格/大小写），不做模糊搜索，更不去猜用户意图。返回
    ``(匹配方式, 候选)``：``id`` 只会有一张，``name``/``alias``/``tag`` 可能有多张。
    ``stickers`` 是调用方已算好的本群可用列表，省得每个标记都重新扫一遍库。
    """

    folded = _fold(query)
    if not folded:
        return "none", []
    if stickers is None:
        stickers = available_stickers(group_id)
    for item in stickers:
        if item.sticker_id.casefold() == folded:
            return "id", [item]
    named = [item for item in stickers if _fold(item.name) == folded]
    if named:
        # 同名的好几张（AI 标注常把同一种情绪起成同一个名字）都算候选，由 pick_sticker 轮换。
        return "name", named
    aliased = [item for item in stickers if any(_fold(alias) == folded for alias in item.aliases)]
    if aliased:
        return "alias", aliased
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
    ai: dict[str, Any] | None = None,
    origin: dict[str, Any] | None = None,
    aliases: object = (),
    packs: object = (),
    restore_trashed: bool = False,
    allow_blocked: bool = False,
) -> tuple[Sticker, bool]:
    """存一张图进库；同一张图（内容哈希相同）已在库里时返回旧记录和 False。

    - 这张图在回收站里：``restore_trashed``（管理员上传）时捞回来；否则报 ``trashed``，
      管理员删掉的图不会被收藏/偷图悄悄加回来；
    - 这张图被拉黑了：除非 ``allow_blocked``（管理员上传，同时解除拉黑），否则报 ``blocked``。
    """

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
    from .sticker_meta import image_info

    info = image_info(data)

    def change(state: _State) -> tuple[Sticker, bool]:
        entries = state.stickers
        for index, item in enumerate(entries):
            if item.sha256 != digest:
                continue
            if not item.deleted_at:
                return item, False
            if not restore_trashed:
                raise StickerError("trashed", "这张图被管理员删掉了（还在回收站里），不再收")
            restored = replace(item, deleted_at="", updated_at=_now_text())
            entries[index] = restored
            return restored, False
        if digest in state.blocked:
            if not allow_blocked:
                raise StickerError("blocked", "这张图被管理员拉黑了，不再收")
            state.blocked.remove(digest)
        if sum(1 for item in entries if not item.deleted_at) >= MAX_STICKERS:
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
        known_packs = {pack.pack_id for pack in state.packs}
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
            extra={
                **({"ai": _clean_meta(ai)} if ai else {}),
                **({"origin": _clean_meta(origin)} if origin else {}),
            },
            aliases=clean_aliases(aliases, name=label),
            packs=tuple(pack for pack in _clean_pack_ids(list(packs) if isinstance(packs, (list, tuple)) else []) if pack in known_packs),
            width=info.width,
            height=info.height,
            frames=info.frames,
        )
        entries.append(sticker)
        return sticker, True

    return _library.mutate_state(change)


def _clean_meta(value: dict[str, Any] | None) -> dict[str, Any]:
    """元数据只留短的标量（字符串截到 120 字），防止索引被塞进大块内容。"""

    output: dict[str, Any] = {}
    for key, item in (value or {}).items():
        if not isinstance(key, str) or len(output) >= 16:  # noqa: PLR2004
            continue
        if isinstance(item, bool) or item is None or isinstance(item, (int, float)):
            output[key[:32]] = item
        else:
            output[key[:32]] = str(item)[:120]
    return output


def find_by_sha256(digest: str, *, include_trashed: bool = False) -> Sticker | None:
    target = str(digest or "").strip().casefold()
    pool = _library.all() if include_trashed else all_stickers()
    return next((item for item in pool if target and item.sha256 == target), None)


def is_blocked(digest: str) -> bool:
    return str(digest or "").strip().casefold() in _library.state().blocked


def digest_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def known_names(limit: int = 60) -> list[str]:
    """库里已有的名字（去重，按用得多少无关，按入库顺序的后 limit 个），给 AI 起名时避免撞名。"""

    names = list(dict.fromkeys(item.name for item in all_stickers() if item.name))
    return names[-limit:]


def apply_label(
    sticker_id: str,
    changes: dict[str, Any],
    meta: dict[str, Any],
    *,
    keep_undo: bool = True,
) -> Sticker:
    """写入 AI 标注：改名字/标签/说明/别名（已清洗过的 ``changes``），记下 ``ai`` 元数据，
    并把改之前的名字/标签/说明/别名存进 ``label_undo``，WebUI 能一键撤销。"""

    def change(entries: list[Sticker]) -> Sticker:
        for index, item in enumerate(entries):
            if item.sticker_id != sticker_id:
                continue
            updates: dict[str, Any] = {}
            if changes.get("name"):
                name = clean_name(changes["name"])
                if name:
                    updates["name"] = name
            if changes.get("tags") is not None:
                updates["tags"] = clean_tags(changes["tags"])
            if changes.get("description") is not None:
                updates["description"] = clean_description(changes["description"])
            if changes.get("aliases") is not None:
                updates["aliases"] = clean_aliases(changes["aliases"], name=str(updates.get("name") or item.name))
            elif "name" in updates:
                # 改了名字：和新名字撞了的别名去掉。
                updates["aliases"] = clean_aliases(list(item.aliases), name=updates["name"])
            extra = dict(item.extra)
            extra["ai"] = _clean_meta(meta)
            if keep_undo and updates:
                extra["label_undo"] = {
                    "name": item.name,
                    "tags": list(item.tags),
                    "description": item.description,
                    "aliases": list(item.aliases),
                    "at": _now_text(),
                }
            updated = replace(item, **updates, extra=extra, updated_at=_now_text())
            entries[index] = updated
            return updated
        raise StickerError("not_found", "表情包不存在")

    return _library.mutate(change)


def undo_label(sticker_id: str) -> Sticker:
    """撤销最近一次 AI 标注：名字/标签/说明/别名恢复成标注前的样子。"""

    def change(entries: list[Sticker]) -> Sticker:
        for index, item in enumerate(entries):
            if item.sticker_id != sticker_id:
                continue
            previous = item.extra.get("label_undo")
            if not isinstance(previous, dict):
                raise StickerError("no_undo", "这张没有可以撤销的 AI 标注")
            extra = {key: value for key, value in item.extra.items() if key != "label_undo"}
            if isinstance(extra.get("ai"), dict):
                extra["ai"] = {**extra["ai"], "undone": True}
            name = clean_name(previous.get("name")) or item.name
            updated = replace(
                item,
                name=name,
                tags=clean_tags(previous.get("tags") or []),
                description=clean_description(previous.get("description")),
                aliases=clean_aliases(previous["aliases"], name=name) if "aliases" in previous else item.aliases,
                extra=extra,
                updated_at=_now_text(),
            )
            entries[index] = updated
            return updated
        raise StickerError("not_found", "表情包不存在")

    return _library.mutate(change)


def search_text(item: Sticker) -> str:
    """WebUI 搜索用：名字、别名、标签、说明、图上的字。"""

    return " ".join([item.name, *item.aliases, *item.tags, item.description, item.ocr_text])


def _field_updates(item: Sticker, fields: dict[str, Any], known_packs: set[str]) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if fields.get("name") is not None:
        name = clean_name(fields["name"])
        if not name:
            raise StickerError("no_name", "名字不能为空")
        updates["name"] = name
    if fields.get("tags") is not None:
        updates["tags"] = clean_tags(fields["tags"])
    if fields.get("description") is not None:
        updates["description"] = clean_description(fields["description"])
    if fields.get("enabled") is not None:
        updates["enabled"] = bool(fields["enabled"])
    if fields.get("status") is not None:
        status = str(fields["status"])
        if status not in STATUSES:
            raise StickerError("bad_status", "status 只能是 active 或 pending")
        updates["status"] = status
    if fields.get("groups") is not None:
        updates["groups"] = _clean_groups(fields["groups"])
    final_name = str(updates.get("name") or item.name)
    if fields.get("aliases") is not None:
        updates["aliases"] = clean_aliases(fields["aliases"], name=final_name)
    elif "name" in updates and item.aliases:
        updates["aliases"] = clean_aliases(list(item.aliases), name=final_name)
    if fields.get("pinned") is not None:
        updates["pinned"] = bool(fields["pinned"])
    if fields.get("weight") is not None:
        updates["weight"] = clean_weight(fields["weight"])
    if fields.get("packs") is not None:
        updates["packs"] = tuple(pack for pack in _clean_pack_ids(list(fields["packs"])) if pack in known_packs)
    if fields.get("note") is not None:
        updates["note"] = clean_note(fields["note"])
    if fields.get("daily_cap") is not None:
        updates["daily_cap"] = min(max(int(fields["daily_cap"]), 0), 1000)
    return updates


def update_sticker(sticker_id: str, **fields: Any) -> Sticker:
    """改名字/标签/说明/启用/审核状态/适用群/别名/置顶/权重/合集/备注/单张日上限；未知字段忽略。"""

    def change(state: _State) -> Sticker:
        known_packs = {pack.pack_id for pack in state.packs}
        for index, item in enumerate(state.stickers):
            if item.sticker_id != sticker_id or item.deleted_at:
                continue
            updated = replace(item, **_field_updates(item, fields, known_packs), updated_at=_now_text())
            state.stickers[index] = updated
            return updated
        raise StickerError("not_found", "表情包不存在")

    return _library.mutate_state(change)


BULK_OPS = frozenset(
    {"add_tags", "remove_tags", "set_groups", "add_pack", "remove_pack", "pin", "unpin", "set_weight", "enable", "disable", "approve", "hold"}
)


def bulk_update(ids: list[str], op: str, value: Any = None) -> tuple[list[str], list[str]]:
    """一次写盘改一批：加/减标签、设适用群、进/出合集、置顶、权重、启停、审核。

    返回 ``(改了的, 没找到的)``。旧的批量操作每张写一次索引，几百张时又慢又容易半途失败。
    """

    if op not in BULK_OPS:
        raise StickerError("bad_op", "不支持的批量操作")
    wanted = list(dict.fromkeys(str(item) for item in ids))
    tags = clean_tags(value) if op in {"add_tags", "remove_tags"} else ()
    if op in {"add_tags", "remove_tags"} and not tags:
        raise StickerError("bad_value", "要写至少一个标签")

    def change(state: _State) -> tuple[list[str], list[str]]:
        known_packs = {pack.pack_id for pack in state.packs}
        if op in {"add_pack", "remove_pack"} and str(value) not in known_packs:
            raise StickerError("pack_not_found", "合集不存在")
        positions = {item.sticker_id: index for index, item in enumerate(state.stickers) if not item.deleted_at}
        done: list[str] = []
        missing: list[str] = []
        for sticker_id in wanted:
            index = positions.get(sticker_id)
            if index is None:
                missing.append(sticker_id)
                continue
            item = state.stickers[index]
            updates: dict[str, Any] = {}
            if op == "add_tags":
                updates["tags"] = clean_tags([*item.tags, *tags])
            elif op == "remove_tags":
                drop = {_fold(tag) for tag in tags}
                updates["tags"] = tuple(tag for tag in item.tags if _fold(tag) not in drop)
            elif op == "set_groups":
                updates["groups"] = _clean_groups(value or [])
            elif op == "add_pack":
                updates["packs"] = _clean_pack_ids([*item.packs, str(value)])
            elif op == "remove_pack":
                updates["packs"] = tuple(pack for pack in item.packs if pack != str(value))
            elif op in {"pin", "unpin"}:
                updates["pinned"] = op == "pin"
            elif op == "set_weight":
                updates["weight"] = clean_weight(value)
            elif op in {"enable", "disable"}:
                updates["enabled"] = op == "enable"
            elif op == "approve":
                updates.update(status=STATUS_ACTIVE, enabled=True)
            elif op == "hold":
                updates["status"] = STATUS_PENDING
            if any(getattr(item, key) != new for key, new in updates.items()):
                state.stickers[index] = replace(item, **updates, updated_at=_now_text())
            done.append(sticker_id)
        return done, missing

    return _library.mutate_state(change)


def _unlink_if_unreferenced(removed: Sticker, entries: list[Sticker]) -> None:
    if any(other.file_name == removed.file_name for other in entries):
        return
    root = library_root()
    candidate = Path(os.path.realpath(str(root / removed.file_name)))
    if _inside(candidate, root) and candidate != root:
        candidate.unlink(missing_ok=True)


def delete_sticker(sticker_id: str, *, block: bool = False) -> Sticker:
    """彻底删记录（回收站里的也能删）；没有别的记录引用同一个文件时把图片也删掉。

    ``block`` 为真时把这张图的哈希记进黑名单：以后收藏/偷图都不再收它。
    """

    def change(state: _State) -> Sticker:
        for index, item in enumerate(state.stickers):
            if item.sticker_id == sticker_id:
                removed = state.stickers.pop(index)
                _unlink_if_unreferenced(removed, state.stickers)
                if block and removed.sha256 and removed.sha256 not in state.blocked:
                    state.blocked.append(removed.sha256)
                return removed
        raise StickerError("not_found", "表情包不存在")

    return _library.mutate_state(change)


def trash_sticker(sticker_id: str) -> Sticker:
    """放进回收站：马上发不出去、菜单里也没有，但图和标注都留着，可以恢复。"""

    def change(entries: list[Sticker]) -> Sticker:
        trashed = sum(1 for item in entries if item.deleted_at)
        for index, item in enumerate(entries):
            if item.sticker_id != sticker_id or item.deleted_at:
                continue
            if trashed >= MAX_TRASH:
                # 回收站满了：把最早进去的那张彻底删掉腾位置。
                oldest = min((other for other in entries if other.deleted_at), key=lambda other: other.deleted_at)
                entries.remove(oldest)
                _unlink_if_unreferenced(oldest, entries)
                index = next(position for position, other in enumerate(entries) if other.sticker_id == sticker_id)
            updated = replace(item, deleted_at=_now_text(), pinned=False)
            entries[index] = updated
            return updated
        raise StickerError("not_found", "表情包不存在")

    return _library.mutate(change)


def restore_sticker(sticker_id: str) -> Sticker:
    def change(entries: list[Sticker]) -> Sticker:
        if sum(1 for item in entries if not item.deleted_at) >= MAX_STICKERS:
            raise StickerError("library_full", f"表情包库最多 {MAX_STICKERS} 张")
        for index, item in enumerate(entries):
            if item.sticker_id == sticker_id and item.deleted_at:
                updated = replace(item, deleted_at="", updated_at=_now_text())
                entries[index] = updated
                return updated
        raise StickerError("not_found", "回收站里没有这张")

    return _library.mutate(change)


def purge_trash(*, older_than_days: int | None = None) -> list[Sticker]:
    """清空回收站；给了 ``older_than_days`` 时只清进去超过这么多天的。"""

    from datetime import timedelta

    cutoff = ""
    if older_than_days is not None:
        cutoff = (now_beijing() - timedelta(days=max(older_than_days, 0))).strftime("%Y-%m-%d %H:%M:%S")

    def expired(item: Sticker) -> bool:
        return bool(item.deleted_at) and (not cutoff or item.deleted_at <= cutoff)

    if not any(expired(item) for item in _library.all()):
        return []

    def change(entries: list[Sticker]) -> list[Sticker]:
        removed = [item for item in entries if expired(item)]
        entries[:] = [item for item in entries if not expired(item)]
        for item in removed:
            _unlink_if_unreferenced(item, entries)
        return removed

    return _library.mutate(change)


def unblock_hash(digest: str) -> bool:
    target = str(digest or "").strip().casefold()
    if target not in _library.state().blocked:
        return False

    def change(state: _State) -> bool:
        state.blocked.remove(target)
        return True

    return _library.mutate_state(change)


def replace_image(sticker_id: str, data: bytes, *, max_bytes: int = DEFAULT_MAX_BYTES) -> Sticker:
    """换一张图（比如换成更清楚的版本），名字、标签、统计都留着。"""

    if not data:
        raise StickerError("empty", "图片是空的")
    if len(data) > max_bytes:
        raise StickerError("too_large", f"图片超过 {max_bytes // 1024} KB 上限")
    kind = sniff_image(data)
    if kind is None:
        raise StickerError("not_image", "只支持 PNG/JPG/GIF/WebP 图片")
    digest = hashlib.sha256(data).hexdigest()
    from .sticker_meta import image_info

    info = image_info(data)

    def change(entries: list[Sticker]) -> Sticker:
        if any(item.sha256 == digest and item.sticker_id != sticker_id for item in entries):
            raise StickerError("duplicate", "库里（或回收站里）已经有这张图了")
        for index, item in enumerate(entries):
            if item.sticker_id != sticker_id or item.deleted_at:
                continue
            if item.sha256 == digest:
                return item
            root = library_root()
            file_name = f"{digest[:16]}{kind[0]}"
            target = root / file_name
            if not target.is_file():
                temporary = target.with_suffix(".part")
                temporary.write_bytes(data)
                temporary.replace(target)
            updated = replace(
                item,
                file_name=file_name,
                sha256=digest,
                size=len(data),
                mime=kind[1],
                width=info.width,
                height=info.height,
                frames=info.frames,
                updated_at=_now_text(),
            )
            entries[index] = updated
            _unlink_if_unreferenced(item, entries)
            return updated
        raise StickerError("not_found", "表情包不存在")

    return _library.mutate(change)


def backfill_meta() -> int:
    """给老的记录补上宽高/帧数（只读文件头）；返回补了几张。"""

    from .sticker_meta import image_info

    root = library_root()
    pending = [item for item in _library.all() if not item.width and (root / item.file_name).is_file()]
    if not pending:
        return 0
    found: dict[str, tuple[int, int, int]] = {}
    for item in pending:
        with contextlib.suppress(OSError):
            info = image_info((root / item.file_name).read_bytes())
            if info.width:
                found[item.sticker_id] = (info.width, info.height, info.frames)
    if not found:
        return 0

    def change(entries: list[Sticker]) -> int:
        for index, item in enumerate(entries):
            meta = found.get(item.sticker_id)
            if meta is not None:
                entries[index] = replace(item, width=meta[0], height=meta[1], frames=meta[2])
        return len(found)

    return _library.mutate(change)


# ── 标签整理 ─────────────────────────────────────────────────────────────


def rename_tag(old: str, new: str) -> int:
    """把全库的标签 ``old`` 改成 ``new``（``new`` 已存在就是合并）；返回改了几张。"""

    source = _fold(old)
    target = clean_name(new)[:MAX_TAG_CHARS].strip()
    if not source or not target:
        raise StickerError("bad_value", "标签不能为空")

    def change(entries: list[Sticker]) -> int:
        count = 0
        for index, item in enumerate(entries):
            if not any(_fold(tag) == source for tag in item.tags):
                continue
            entries[index] = replace(item, tags=clean_tags([target if _fold(tag) == source else tag for tag in item.tags]), updated_at=_now_text())
            count += 1
        return count

    return _library.mutate(change)


def delete_tag(tag: str) -> int:
    source = _fold(tag)
    if not source:
        raise StickerError("bad_value", "标签不能为空")

    def change(entries: list[Sticker]) -> int:
        count = 0
        for index, item in enumerate(entries):
            kept = tuple(value for value in item.tags if _fold(value) != source)
            if len(kept) != len(item.tags):
                entries[index] = replace(item, tags=kept, updated_at=_now_text())
                count += 1
        return count

    return _library.mutate(change)


# ── 合集 ─────────────────────────────────────────────────────────────────


def create_pack(name: str, *, description: str = "", groups: object = (), enabled: bool = True) -> StickerPack:
    label = clean_name(name)
    if not label:
        raise StickerError("no_name", "合集要有名字")

    def change(state: _State) -> StickerPack:
        if len(state.packs) >= MAX_PACKS:
            raise StickerError("too_many", f"合集最多 {MAX_PACKS} 个")
        if any(_fold(pack.name) == _fold(label) for pack in state.packs):
            raise StickerError("duplicate", "已经有同名的合集了")
        taken = {pack.pack_id for pack in state.packs}
        number = len(state.packs) + 1
        while f"p{number}" in taken:
            number += 1
        pack = StickerPack(f"p{number}", label, clean_description(description)[:60], bool(enabled), _clean_groups(groups), _now_text())
        state.packs.append(pack)
        return pack

    return _library.mutate_state(change)


def update_pack(pack_id: str, **fields: Any) -> StickerPack:
    def change(state: _State) -> StickerPack:
        for index, pack in enumerate(state.packs):
            if pack.pack_id != pack_id:
                continue
            updates: dict[str, Any] = {}
            if fields.get("name") is not None:
                label = clean_name(fields["name"])
                if not label:
                    raise StickerError("no_name", "合集要有名字")
                if any(_fold(other.name) == _fold(label) and other.pack_id != pack_id for other in state.packs):
                    raise StickerError("duplicate", "已经有同名的合集了")
                updates["name"] = label
            if fields.get("description") is not None:
                updates["description"] = clean_description(fields["description"])[:60]
            if fields.get("enabled") is not None:
                updates["enabled"] = bool(fields["enabled"])
            if fields.get("groups") is not None:
                updates["groups"] = _clean_groups(fields["groups"])
            updated = replace(pack, **updates)
            state.packs[index] = updated
            return updated
        raise StickerError("pack_not_found", "合集不存在")

    return _library.mutate_state(change)


def delete_pack(pack_id: str) -> StickerPack:
    """删合集：里面的表情包留在库里，只是不再属于它。"""

    def change(state: _State) -> StickerPack:
        for index, pack in enumerate(state.packs):
            if pack.pack_id != pack_id:
                continue
            removed = state.packs.pop(index)
            for position, item in enumerate(state.stickers):
                if pack_id in item.packs:
                    state.stickers[position] = replace(item, packs=tuple(value for value in item.packs if value != pack_id))
            return removed
        raise StickerError("pack_not_found", "合集不存在")

    return _library.mutate_state(change)


def forget_member(group_id: int, user_id: int) -> int:
    """成员要求忘掉自己时：抹掉他在本群收藏记录上的“谁加的”，图本身留给管理员处理。"""

    target = int(user_id)
    group = int(group_id)

    def sent_by(item: Sticker) -> bool:
        origin = item.origin
        return str(origin.get("sender_id") or "") == str(target) and str(origin.get("group_id") or "") == str(group)

    def mine(item: Sticker) -> bool:
        return (item.added_by == target and item.added_group == group) or sent_by(item)

    def change(entries: list[Sticker]) -> int:
        count = 0
        for index, item in enumerate(entries):
            if mine(item):
                extra = dict(item.extra)
                if sent_by(item):
                    # 偷图记录里“谁发的”也抹掉（名字、号码），图和群号留给管理员处理。
                    extra["origin"] = {key: value for key, value in item.origin.items() if key not in {"sender_id", "sender_name"}}
                entries[index] = replace(
                    item,
                    added_by=None if item.added_by == target and item.added_group == group else item.added_by,
                    extra=extra,
                )
                count += 1
        return count

    # 回收站里的记录同样要抹。
    if not any(mine(item) for item in _library.all()):
        return 0
    return _library.mutate(change)


def tag_counts(
    group_id: int | None,
    *,
    stickers: tuple[Sticker, ...] | list[Sticker] | None = None,
) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    labels: dict[str, str] = {}
    for item in stickers if stickers is not None else available_stickers(group_id):
        for tag in item.tags:
            key = _fold(tag)
            counts[key] = counts.get(key, 0) + 1
            labels.setdefault(key, tag)
    return sorted(((labels[key], value) for key, value in counts.items()), key=lambda row: (-row[1], row[0]))


def reset_cache_for_tests() -> None:
    _library._cache = None


__all__ = [
    "BULK_OPS",
    "DEFAULT_MAX_BYTES",
    "SOURCE_LABELS",
    "STATUS_ACTIVE",
    "STATUS_PENDING",
    "WEIGHT_LABELS",
    "Sticker",
    "StickerError",
    "StickerPack",
    "add_sticker",
    "all_packs",
    "all_stickers",
    "apply_label",
    "available_stickers",
    "backfill_meta",
    "blocked_hashes",
    "bulk_update",
    "clean_aliases",
    "clean_description",
    "clean_name",
    "clean_tags",
    "create_pack",
    "delete_pack",
    "delete_sticker",
    "delete_tag",
    "digest_of",
    "find_by_sha256",
    "forget_member",
    "get_sticker",
    "index_path",
    "is_available",
    "is_blocked",
    "known_names",
    "library_health",
    "library_root",
    "looks_like_placeholder",
    "match_stickers",
    "pack_allows",
    "purge_trash",
    "rename_tag",
    "replace_image",
    "restore_sticker",
    "search_text",
    "sniff_image",
    "sticker_path",
    "tag_counts",
    "trash_sticker",
    "trashed_stickers",
    "unblock_hash",
    "undo_label",
    "update_pack",
    "update_sticker",
]
