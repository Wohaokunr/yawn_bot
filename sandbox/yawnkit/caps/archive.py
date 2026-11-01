"""archive 包：zip / tar / gz / xz 的打包、解包与检查（解包防路径穿越和压缩炸弹）。"""

from __future__ import annotations

import gzip
import lzma
import shutil
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from . import CapError, as_list, cap, clip_list, dest, human, ref, src

# 解包总大小上限，防压缩炸弹（文件本身还受工作区空间约束）。
MAX_EXTRACT_BYTES = 4 * 1024 * 1024 * 1024
MAX_MEMBERS = 20000


def _safe_member(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and not name.startswith(("/", "\\"))
    )


def _collect(paths: list[Any]) -> list[Path]:
    items: list[Path] = []
    for raw in paths:
        item = src(raw)
        if item.is_dir():
            items.extend(p for p in sorted(item.rglob("*")) if p.is_file())
        else:
            items.append(item)
    if not items:
        raise CapError("没有要打包的文件")
    return items


def _folder(output: Any, archive: Path) -> Path:
    text = str(output or "").strip().removeprefix("sandbox:")
    stem = archive.name.split(".")[0] or "解压"
    folder = Path(text) if text else Path("out") / stem
    if folder.is_absolute() or ".." in folder.parts:
        raise CapError("输出路径要用工作区里的相对路径")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@cap("archive.zip")
def make_zip(paths: list[str], output: str = "") -> dict[str, Any]:
    items = _collect(as_list(paths))
    target = dest(output, "打包.zip")
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in items:
            archive.write(item, item.as_posix())
    return {
        "file": ref(target),
        "files": len(items),
        "size": human(target.stat().st_size),
    }


@cap("archive.list")
def list_archive(path: str) -> dict[str, Any]:
    target = src(path)
    rows = []
    if zipfile.is_zipfile(target):
        with zipfile.ZipFile(target) as archive:
            for info in archive.infolist():
                rows.append(
                    {
                        "name": info.filename,
                        "size": human(info.file_size),
                        "packed": human(info.compress_size),
                    }
                )
    elif tarfile.is_tarfile(target):
        with tarfile.open(target) as archive:
            for member in archive.getmembers():
                rows.append(
                    {
                        "name": member.name,
                        "size": human(member.size),
                        "type": "dir" if member.isdir() else "file",
                    }
                )
    else:
        raise CapError("不是 zip/tar 压缩包（7z/rar 不支持）")
    return clip_list(rows, 100)


@cap("archive.unzip")
def unzip(path: str, output: str = "", password: str = "") -> dict[str, Any]:
    target = src(path)
    folder = _folder(output, target)
    extracted = 0
    total = 0
    if zipfile.is_zipfile(target):
        with zipfile.ZipFile(target) as archive:
            members = archive.infolist()
            if len(members) > MAX_MEMBERS:
                raise CapError("压缩包里文件太多")
            if sum(m.file_size for m in members) > MAX_EXTRACT_BYTES:
                raise CapError("解压后太大（超过 4GB）")
            for member in members:
                if not _safe_member(member.filename):
                    continue
                archive.extract(
                    member, folder, pwd=password.encode() if password else None
                )
                extracted += 1
                total += member.file_size
    elif tarfile.is_tarfile(target):
        with tarfile.open(target) as archive:
            members = [
                m
                for m in archive.getmembers()
                if (m.isfile() or m.isdir()) and _safe_member(m.name)
            ]
            if sum(m.size for m in members) > MAX_EXTRACT_BYTES:
                raise CapError("解压后太大（超过 4GB）")
            archive.extractall(folder, members=members)
            extracted = len(members)
            total = sum(m.size for m in members)
    else:
        raise CapError("不是 zip/tar 压缩包（7z/rar 不支持）")
    return {"folder": ref(folder), "files": extracted, "size": human(total)}


@cap("archive.tar")
def make_tar(
    paths: list[str], output: str = "", compression: str = "gz"
) -> dict[str, Any]:
    if compression not in {"gz", "xz", "bz2", "none"}:
        raise CapError("compression 只能是 gz/xz/bz2/none")
    items = _collect(as_list(paths))
    suffix = "" if compression == "none" else f".{compression}"
    target = dest(output, f"打包.tar{suffix}")
    mode = "w" if compression == "none" else f"w:{compression}"
    with tarfile.open(target, mode) as archive:  # type: ignore[call-overload]
        for item in items:
            archive.add(item, item.as_posix())
    return {
        "file": ref(target),
        "files": len(items),
        "size": human(target.stat().st_size),
    }


@cap("archive.gzip")
def gzip_file(path: str, output: str = "", method: str = "gz") -> dict[str, Any]:
    source = src(path)
    opener = {"gz": gzip.open, "xz": lzma.open}.get(method)
    if opener is None:
        raise CapError("method 只能是 gz/xz")
    target = dest(output, f"{source.name}.{method}")
    with source.open("rb") as handle, opener(target, "wb") as out:
        shutil.copyfileobj(handle, out, 1024 * 1024)
    return {
        "file": ref(target),
        "before": human(source.stat().st_size),
        "after": human(target.stat().st_size),
    }


@cap("archive.gunzip")
def gunzip_file(path: str, output: str = "") -> dict[str, Any]:
    source = src(path)
    name = source.name
    if name.endswith(".gz"):
        opener, plain = gzip.open, name[:-3]
    elif name.endswith(".xz"):
        opener, plain = lzma.open, name[:-3]
    else:
        raise CapError("只认 .gz / .xz 文件")
    target = dest(output, plain or "解压结果")
    written = 0
    with opener(source, "rb") as handle, target.open("wb") as out:
        while chunk := handle.read(1024 * 1024):
            written += len(chunk)
            if written > MAX_EXTRACT_BYTES:
                raise CapError("解压后太大（超过 4GB）")
            out.write(chunk)
    return {"file": ref(target), "size": human(written)}


@cap("archive.test")
def test_archive(path: str) -> dict[str, Any]:
    target = src(path)
    if zipfile.is_zipfile(target):
        with zipfile.ZipFile(target) as archive:
            bad = archive.testzip()
            return {
                "format": "zip",
                "ok": bad is None,
                "first_bad_file": bad,
                "files": len(archive.infolist()),
            }
    if tarfile.is_tarfile(target):
        with tarfile.open(target) as archive:
            count = len(archive.getmembers())
        return {"format": "tar", "ok": True, "files": count}
    raise CapError("不是 zip/tar 压缩包")


@cap("archive.add")
def add_to_zip(path: str, files: list[str]) -> dict[str, Any]:
    target = src(path)
    if not zipfile.is_zipfile(target):
        raise CapError("只能往 zip 里加文件")
    items = _collect(as_list(files))
    with zipfile.ZipFile(target, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in items:
            archive.write(item, item.as_posix())
    return {"file": ref(target), "added": len(items)}
