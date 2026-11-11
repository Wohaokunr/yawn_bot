# ruff: noqa: E501, C901, PLR0912, PLR0915
"""表情包库的导出 / 导入（zip）：搬家、备份、把一套表情包分享给另一个 Bot。

导出：一个 zip，里面是 ``manifest.json``（每张的名字、别名、标签、说明、权重、合集名……）
和 ``images/<哈希前 16 位><扩展名>``。默认**不带**“谁加的 / 从谁那偷的 / 哪个群”这些来源
信息，也不带备注——备份可能发给别人；管理员明确勾选才带。

导入：只读 zip 里 manifest 点名的成员，按字节数硬限制读进内存（不解压到磁盘，没有
路径穿越的机会），每张都走 ``sticker_library.add_sticker`` 同一套校验（文件头、大小、
去重、数量上限）。库里已有同一张图时默认跳过，``update_existing`` 时用包里的名字/标签覆盖。
合集按名字对：没有就建。
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any

from .context import now_beijing
from .sticker_library import (
    STATUS_ACTIVE,
    STATUS_PENDING,
    Sticker,
    StickerError,
    add_sticker,
    all_packs,
    all_stickers,
    create_pack,
    find_by_sha256,
    library_root,
    update_sticker,
)

FORMAT = "yawnbot-stickers"
FORMAT_VERSION = 1
MANIFEST = "manifest.json"
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_ENTRIES = 2000


def _entry(item: Sticker, pack_names: dict[str, str], *, include_private: bool) -> dict[str, Any]:
    suffix = "." + item.file_name.rsplit(".", 1)[-1] if "." in item.file_name else ""
    row: dict[str, Any] = {
        "file": f"images/{item.sha256[:16] or item.sticker_id}{suffix}",
        "name": item.name,
        "aliases": list(item.aliases),
        "tags": list(item.tags),
        "description": item.description,
        "enabled": item.enabled,
        "status": item.status,
        "pinned": item.pinned,
        "weight": item.weight,
        "packs": [pack_names[pack] for pack in item.packs if pack in pack_names],
        "sha256": item.sha256,
    }
    ai = item.ai
    if ai and not ai.get("undone"):
        # AI 认出来的图上文字、情绪大类跟着图走；模型名和时间是本机的事，不带。
        row["ai"] = {key: ai[key] for key in ("text", "mood", "kind", "safe", "confidence") if key in ai}
    if include_private:
        row.update(
            source=item.source,
            added_by=item.added_by,
            added_group=item.added_group,
            groups=list(item.groups),
            note=item.note,
            origin=item.origin,
            created_at=item.created_at,
        )
    return row


def export_archive(ids: list[str] | None = None, *, pack_id: str = "", include_private: bool = False) -> tuple[bytes, dict[str, Any]]:
    """打包；``ids`` / ``pack_id`` 都不给时导出整个库（不含回收站）。返回 ``(zip 字节, 概况)``。"""

    wanted = set(ids or [])
    stickers = [item for item in all_stickers() if (not wanted or item.sticker_id in wanted) and (not pack_id or pack_id in item.packs)]
    if not stickers:
        raise StickerError("empty", "没有可以导出的表情包")
    packs = all_packs()
    pack_names = {pack.pack_id: pack.name for pack in packs}
    root = library_root()
    buffer = io.BytesIO()
    entries: list[dict[str, Any]] = []
    skipped = 0
    total = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        written: set[str] = set()
        for item in stickers:
            path = root / item.file_name
            if not path.is_file():
                skipped += 1
                continue
            row = _entry(item, pack_names, include_private=include_private)
            if row["file"] not in written:
                data = path.read_bytes()
                total += len(data)
                if total > MAX_ARCHIVE_BYTES:
                    raise StickerError("too_large", "导出的包超过 200 MB，分几次选着导出")
                archive.writestr(row["file"], data)
                written.add(row["file"])
            entries.append(row)
        used_packs = {name for row in entries for name in row["packs"]}
        manifest = {
            "format": FORMAT,
            "version": FORMAT_VERSION,
            "exported_at": now_beijing().strftime("%Y-%m-%d %H:%M:%S"),
            "include_private": include_private,
            "packs": [{"name": pack.name, "description": pack.description} for pack in packs if pack.name in used_packs],
            "stickers": entries,
        }
        archive.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
    return buffer.getvalue(), {"count": len(entries), "skipped": skipped, "bytes": total}


def inspect_archive(data: bytes) -> dict[str, Any]:
    """只看不导：包里有几张、几张库里已经有、几个合集。校验失败抛 ``StickerError``。"""

    manifest, _archive = _open(data)
    rows = manifest["stickers"]
    existing = sum(1 for row in rows if find_by_sha256(str(row.get("sha256") or ""), include_trashed=True) is not None)
    return {
        "count": len(rows),
        "existing": existing,
        "new": len(rows) - existing,
        "packs": [str(pack.get("name") or "") for pack in manifest.get("packs") or [] if isinstance(pack, dict)],
        "exportedAt": str(manifest.get("exported_at") or "")[:19] or None,
        "includePrivate": bool(manifest.get("include_private")),
    }


def _open(data: bytes) -> tuple[dict[str, Any], zipfile.ZipFile]:
    if len(data) > MAX_ARCHIVE_BYTES:
        raise StickerError("too_large", "包超过 200 MB")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        info = archive.getinfo(MANIFEST)
    except (zipfile.BadZipFile, KeyError) as exc:
        raise StickerError("bad_archive", "这不是表情包导出包（没有 manifest.json）") from exc
    if info.file_size > MAX_MANIFEST_BYTES:
        raise StickerError("bad_archive", "manifest.json 太大了")
    try:
        manifest = json.loads(archive.read(MANIFEST).decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise StickerError("bad_archive", "manifest.json 读不出来") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or not isinstance(manifest.get("stickers"), list):
        raise StickerError("bad_archive", "这不是表情包导出包")
    if int(manifest.get("version") or 0) > FORMAT_VERSION:
        raise StickerError("bad_archive", "这个包是更新的版本导出的，这里读不了")
    manifest["stickers"] = [row for row in manifest["stickers"] if isinstance(row, dict)][:MAX_ENTRIES]
    return manifest, archive


def import_archive(
    data: bytes,
    *,
    max_bytes: int,
    update_existing: bool = False,
    as_pending: bool = False,
    keep_private: bool = False,
) -> dict[str, Any]:
    """导入一个包。返回 ``{added, updated, skipped, failed: [{name, reason}], packsCreated}``。"""

    manifest, archive = _open(data)
    members = {info.filename: info for info in archive.infolist()}
    pack_ids = {pack.name: pack.pack_id for pack in all_packs()}
    created_packs = 0
    for raw in manifest.get("packs") or []:
        name = str(raw.get("name") or "") if isinstance(raw, dict) else ""
        if name and name not in pack_ids:
            try:
                pack = create_pack(name, description=str(raw.get("description") or ""))
            except StickerError:
                continue
            pack_ids[pack.name] = pack.pack_id
            created_packs += 1
    added = updated = skipped = 0
    failed: list[dict[str, str]] = []
    private_ok = keep_private and bool(manifest.get("include_private"))
    for row in manifest["stickers"]:
        name = str(row.get("name") or "")[:40]
        info = members.get(str(row.get("file") or ""))
        if info is None or info.is_dir():
            failed.append({"name": name, "reason": "包里没有这张图"})
            continue
        if info.file_size > max_bytes:
            failed.append({"name": name, "reason": f"超过 {max_bytes // 1024} KB 上限"})
            continue
        with archive.open(info) as handle:
            blob = handle.read(max_bytes + 1)
        if len(blob) > max_bytes:
            failed.append({"name": name, "reason": f"超过 {max_bytes // 1024} KB 上限"})
            continue
        packs = [pack_ids[str(item)] for item in row.get("packs") or [] if str(item) in pack_ids]
        status = STATUS_PENDING if as_pending or row.get("status") == STATUS_PENDING else STATUS_ACTIVE
        ai = row.get("ai") if isinstance(row.get("ai"), dict) else None
        try:
            sticker, created = add_sticker(
                blob,
                name=name,
                tags=row.get("tags") or [],
                description=str(row.get("description") or ""),
                source=str(row.get("source") or "upload") if private_ok else "upload",
                status=status,
                groups=row.get("groups") or [] if private_ok else [],
                added_by=_int(row.get("added_by")) if private_ok else None,
                added_group=_int(row.get("added_group")) if private_ok else None,
                max_bytes=max_bytes,
                ai={**ai, "imported": True} if ai else None,
                origin=row.get("origin") if private_ok and isinstance(row.get("origin"), dict) else None,
                aliases=row.get("aliases") or [],
                packs=packs,
                restore_trashed=True,
                allow_blocked=True,
            )
        except StickerError as exc:
            failed.append({"name": name, "reason": str(exc)})
            if exc.reason == "library_full":
                break
            continue
        extras: dict[str, Any] = {}
        if created:
            added += 1
            extras = {
                "pinned": bool(row.get("pinned")),
                "weight": row.get("weight", 1),
                "enabled": row.get("enabled") is not False,
                **({"note": str(row.get("note") or "")} if private_ok else {}),
            }
        elif update_existing:
            updated += 1
            extras = {
                "name": name or None,
                "tags": row.get("tags") or [],
                "description": str(row.get("description") or ""),
                "aliases": row.get("aliases") or [],
                "packs": list(dict.fromkeys([*sticker.packs, *packs])),
            }
        else:
            skipped += 1
        if extras:
            try:
                update_sticker(sticker.sticker_id, **extras)
            except StickerError as exc:
                failed.append({"name": name, "reason": str(exc)})
    return {"added": added, "updated": updated, "skipped": skipped, "failed": failed[:50], "failedCount": len(failed), "packsCreated": created_packs}


def _int(value: object) -> int | None:
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


__all__ = ["FORMAT", "MAX_ARCHIVE_BYTES", "export_archive", "import_archive", "inspect_archive"]
