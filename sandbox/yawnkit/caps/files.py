"""files 包：工作区文件的查看、查找、比较、整理。"""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import re
import shutil
from pathlib import Path
from typing import Any

from . import CapError, cap, clip_list, clip_text, dest, human, read_text, ref, src

_HIDDEN = (".pylib",)


def _walk(root: Path) -> list[Path]:
    return sorted(
        item
        for item in root.rglob("*")
        if item.is_file()
        and not any(part.startswith(".") for part in item.relative_to(root).parts)
    )


@cap("files.tree")
def tree(path: str = ".", depth: int = 3) -> dict[str, Any]:
    root = src(path, kind="目录") if path not in {"", "."} else Path()
    depth = max(1, min(int(depth), 6))
    lines: list[str] = []

    def walk(folder: Path, level: int, prefix: str) -> None:
        if level > depth or len(lines) > 300:
            return
        entries = sorted(
            (e for e in folder.iterdir() if not e.name.startswith(".")),
            key=lambda e: (e.is_file(), e.name),
        )
        for entry in entries:
            size = f"  {human(entry.stat().st_size)}" if entry.is_file() else "/"
            lines.append(f"{prefix}{entry.name}{size}")
            if entry.is_dir():
                walk(entry, level + 1, prefix + "  ")

    walk(root, 1, "")
    return clip_text("\n".join(lines) or "（空目录）")


@cap("files.find")
def find(pattern: str = "*", path: str = ".", min_size_kb: float = 0) -> dict[str, Any]:
    root = src(path, kind="目录") if path not in {"", "."} else Path()
    rows = []
    for item in _walk(root):
        if fnmatch.fnmatch(item.name.lower(), str(pattern).lower()):
            size = item.stat().st_size
            if size >= float(min_size_kb) * 1024:
                rows.append({"path": item.as_posix(), "size": human(size)})
    return clip_list(rows)


@cap("files.grep")
def grep(
    pattern: str, path: str = ".", ignore_case: bool = True, max_matches: int = 50
) -> dict[str, Any]:
    target = src(path) if path not in {"", "."} else Path()
    flags = re.IGNORECASE if ignore_case else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        raise CapError(f"正则写错了：{exc}") from exc
    files = [target] if target.is_file() else _walk(target)
    rows: list[dict[str, Any]] = []
    for item in files:
        if item.stat().st_size > 20 * 1024 * 1024:
            continue
        try:
            text = read_text(item)
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                rows.append(
                    {
                        "file": item.as_posix(),
                        "line": number,
                        "text": line.strip()[:200],
                    }
                )
                if len(rows) >= int(max_matches):
                    return clip_list(rows)
    return clip_list(rows)


@cap("files.du")
def du(path: str = ".", top: int = 15) -> dict[str, Any]:
    root = src(path, kind="目录") if path not in {"", "."} else Path()
    files = _walk(root)
    total = sum(item.stat().st_size for item in files)
    biggest = sorted(files, key=lambda p: -p.stat().st_size)[: int(top)]
    return {
        "total": human(total),
        "files": len(files),
        "largest": [
            {"path": p.as_posix(), "size": human(p.stat().st_size)} for p in biggest
        ],
    }


def _digest(path: Path, algorithm: str) -> str:
    hasher = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


@cap("files.hash")
def file_hash(path: str, algorithm: str = "sha256") -> dict[str, Any]:
    if algorithm not in {"md5", "sha1", "sha256", "sha512"}:
        raise CapError("algorithm 只能是 md5/sha1/sha256/sha512")
    target = src(path)
    return {
        "path": target.as_posix(),
        "algorithm": algorithm,
        "hash": _digest(target, algorithm),
        "size": human(target.stat().st_size),
    }


@cap("files.verify")
def verify(path: str, expected: str) -> dict[str, Any]:
    target = src(path)
    expected = str(expected).strip().lower()
    algorithm = {32: "md5", 40: "sha1", 64: "sha256", 128: "sha512"}.get(len(expected))
    if algorithm is None:
        raise CapError("expected 要是 md5/sha1/sha256/sha512 的十六进制摘要")
    actual = _digest(target, algorithm)
    return {"algorithm": algorithm, "match": actual == expected, "actual": actual}


@cap("files.dedupe")
def dedupe(path: str = ".", delete: bool = False) -> dict[str, Any]:
    root = src(path, kind="目录") if path not in {"", "."} else Path()
    seen: dict[str, Path] = {}
    groups: dict[str, list[str]] = {}
    for item in _walk(root):
        digest = _digest(item, "sha1")
        if digest in seen:
            groups.setdefault(digest, [seen[digest].as_posix()]).append(item.as_posix())
            if delete:
                item.unlink()
        else:
            seen[digest] = item
    return {
        "duplicate_groups": list(groups.values())[:30],
        "deleted": bool(delete),
        "groups": len(groups),
    }


@cap("files.rename_batch")
def rename_batch(
    path: str = ".",
    find_text: str = "",
    replace_with: str = "",
    prefix: str = "",
    numbering: bool = False,
    pattern: str = "*",
) -> dict[str, Any]:
    root = src(path, kind="目录") if path not in {"", "."} else Path()
    items = [
        p
        for p in sorted(root.iterdir())
        if p.is_file()
        and fnmatch.fnmatch(p.name, pattern)
        and not p.name.startswith(".")
    ]
    renamed = []
    for index, item in enumerate(items, start=1):
        name = item.stem.replace(find_text, replace_with) if find_text else item.stem
        if numbering:
            name = f"{index:03d}_{name}"
        new = item.with_name(f"{prefix}{name}{item.suffix}")
        if new != item:
            if new.exists():
                raise CapError(f"{new.name} 已存在，停止改名")
            item.rename(new)
            renamed.append({"from": item.name, "to": new.name})
    return clip_list(renamed)


@cap("files.head")
def head(path: str, lines: int = 20) -> dict[str, Any]:
    text = read_text(src(path))
    return clip_text("\n".join(text.splitlines()[: max(1, min(int(lines), 500))]))


@cap("files.tail")
def tail(path: str, lines: int = 20) -> dict[str, Any]:
    text = read_text(src(path))
    return clip_text("\n".join(text.splitlines()[-max(1, min(int(lines), 500)) :]))


@cap("files.diff")
def diff(a: str, b: str, context: int = 2) -> dict[str, Any]:
    left = read_text(src(a)).splitlines()
    right = read_text(src(b)).splitlines()
    result = list(
        difflib.unified_diff(
            left, right, fromfile=a, tofile=b, n=int(context), lineterm=""
        )
    )
    ratio = (
        difflib.SequenceMatcher(None, "\n".join(left), "\n".join(right)).ratio()
        if len(left) + len(right) < 20000
        else None
    )
    payload = clip_text("\n".join(result) or "两个文件内容相同")
    payload["similarity"] = round(ratio, 3) if ratio is not None else None
    return payload


@cap("files.wc")
def wc(path: str) -> dict[str, Any]:
    text = read_text(src(path))
    return {
        "lines": text.count("\n") + (1 if text and not text.endswith("\n") else 0),
        "chars": len(text),
        "chinese_chars": len(re.findall(r"[一-鿿]", text)),
        "words": len(re.findall(r"[A-Za-z]+", text)),
    }


@cap("files.detect_encoding")
def detect_encoding(path: str) -> dict[str, Any]:
    raw = src(path).read_bytes()[: 2 * 1024 * 1024]
    if raw.startswith(b"\xef\xbb\xbf"):
        return {"encoding": "utf-8-sig"}
    for encoding in ("utf-8", "gb18030", "big5", "shift_jis", "utf-16"):
        try:
            raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        return {"encoding": encoding}
    return {"encoding": "unknown（可能是二进制文件）"}


@cap("files.convert_encoding")
def convert_encoding(path: str, to: str = "utf-8", output: str = "") -> dict[str, Any]:
    source = src(path)
    text = read_text(source)
    target = dest(output, source.name)
    try:
        target.write_text(text, encoding=to)
    except (LookupError, UnicodeEncodeError) as exc:
        raise CapError(f"转不成 {to}：{exc}") from exc
    return {"file": ref(target), "encoding": to}


@cap("files.split")
def split(path: str, part_mb: float = 20) -> dict[str, Any]:
    source = src(path)
    size = max(int(float(part_mb) * 1024 * 1024), 1024)
    parts = []
    with source.open("rb") as handle:
        index = 1
        while chunk := handle.read(size):
            target = dest("", f"{source.name}.part{index:03d}")
            target.write_bytes(chunk)
            parts.append(ref(target))
            index += 1
    return {"parts": parts, "count": len(parts), "join_with": "files.join"}


@cap("files.join")
def join(parts: list[str], output: str = "") -> dict[str, Any]:
    items = [src(p) for p in parts]
    if not items:
        raise CapError("parts 不能为空")
    name = re.sub(r"\.part\d+$", "", items[0].name)
    target = dest(output, name)
    with target.open("wb") as out:
        for item in items:
            with item.open("rb") as handle:
                shutil.copyfileobj(handle, out, 1024 * 1024)
    return {"file": ref(target), "size": human(target.stat().st_size)}


@cap("files.copy")
def copy(path: str, output: str) -> dict[str, Any]:
    source = src(path)
    target = dest(output, source.name)
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True)
    else:
        shutil.copy2(source, target)
    return {"file": ref(target)}


@cap("files.move")
def move(path: str, output: str) -> dict[str, Any]:
    source = src(path)
    target = dest(output, source.name)
    shutil.move(str(source), str(target))
    return {"file": ref(target)}


@cap("files.type")
def file_type(path: str) -> dict[str, Any]:
    import subprocess

    target = src(path)
    try:
        done = subprocess.run(
            ["file", "-b", str(target)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        description = done.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        description = ""
    with target.open("rb") as handle:
        magic = handle.read(16).hex(" ")
    return {
        "type": description or "未知",
        "magic": magic,
        "size": human(target.stat().st_size),
    }
