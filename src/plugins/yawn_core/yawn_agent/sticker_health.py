# ruff: noqa: E501, C901, PLR0912, PLR0915, PLR2004
"""表情包库体检：找出“能发但发得不好”和“占着地方没用”的问题，并给出能一键修的那几类。

全部是只看库、文件和统计的确定性检查，不调模型、不看聊天内容：

- 文件层：图片丢了、目录里没人引用的孤儿文件、索引坏过；
- 标注层：名字还是文件名、没有标签、没有“什么时候发”、AI 觉得不合适却在用、AI 没把握；
- 使用层：入库很久一次没发过、很久没再发、只有一张的标签（按情绪挑不到）、
  别名被别的名字挡住、同名但不在同一个群可用；
- 图片层：太大（发得慢）、太小、可能重复（尺寸/帧数相同、大小几乎一样——只是提示，要人看）。

``fix`` 只做不会丢图的修复：补尺寸、清掉悬空的合集引用、把丢了文件的记录放进回收站、
删孤儿文件（孤儿文件本来就没有任何记录指向它）。
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta
from typing import Any

from .context import now_beijing
from .sticker_library import (
    INDEX_NAME,
    STATUS_ACTIVE,
    Sticker,
    StickerError,
    _library,
    all_packs,
    all_stickers,
    backfill_meta,
    library_health,
    library_root,
    trash_sticker,
    trashed_stickers,
)
from .sticker_policy import stats_snapshot

NEVER_USED_DAYS = 30
STALE_DAYS = 60
HEAVY_BYTES = 1024 * 1024
TINY_SIDE = 48
LOW_CONFIDENCE = 0.5
MAX_IDS_PER_ISSUE = 60
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})

# kind → (严重程度, 标题, 怎么办)
ISSUES: dict[str, tuple[str, str, str]] = {
    "index_corrupt": ("error", "索引文件坏过", "这次是从备份读回来的；随便改一张保存一次，就会写出新的好索引"),
    "missing_file": ("error", "图片文件丢了", "发不出去；放进回收站，或重新上传同一张"),
    "orphan_file": ("warn", "没人引用的图片文件", "库里没有任何记录指向它们，可以删掉腾地方"),
    "unsafe_active": ("error", "AI 觉得不合适却在用", "看一眼，不合适就停用或删掉"),
    "placeholder_name": ("warn", "名字还是文件名", "模型看到这种名字不知道什么时候发；让 AI 重标或手动改名"),
    "no_tags": ("warn", "没有情绪标签", "按情绪挑不到它；让 AI 补标签"),
    "no_description": ("info", "没写什么时候发", "菜单里只剩一个名字；让 AI 补一句"),
    "low_confidence": ("info", "AI 起名没把握", "可以重标一次，或人工看一眼"),
    "never_used": ("info", f"入库超过 {NEVER_USED_DAYS} 天一次没发过", "可能名字起得不好，或者这种情绪用不上"),
    "stale": ("info", f"超过 {STALE_DAYS} 天没再发", "可以考虑停用，或者置顶让它重新被看到"),
    "heavy": ("info", "图片超过 1 MB", "发送慢、占流量；换一张压缩过的"),
    "tiny": ("info", "图片太小", "在群里会糊；换一张清楚的"),
    "possible_duplicate": ("info", "可能是同一张图", "尺寸、帧数相同且大小几乎一样；人工看一下，留一张"),
    "alias_shadowed": ("warn", "别名被别的名字挡住", "这个别名和另一张的名字一样，模型写它只会发那一张；改掉别名"),
    "dangling_pack": ("warn", "属于已经删掉的合集", "清掉这些引用"),
    "no_dimensions": ("info", "没有尺寸信息", "老图片入库时没记宽高，补一下"),
    "weight_zero_unnamed": ("info", "设了“不主动用”但名字是占位", "模型点不出它的名字，等于永远发不出去"),
}
FIXABLE = frozenset({"missing_file", "orphan_file", "dangling_pack", "no_dimensions"})
_PENALTY = {"error": 12, "warn": 4, "info": 1}


def _parse_time(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")  # noqa: DTZ007
    except (TypeError, ValueError):
        return None


def orphan_files() -> list[str]:
    """库目录里没有任何记录（含回收站）指向的图片文件名。"""

    root = library_root()
    referenced = {item.file_name for item in _library.all()}
    output: list[str] = []
    with contextlib.suppress(OSError):
        for path in sorted(root.iterdir()):
            if not path.is_file() or path.name == INDEX_NAME or path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            if path.name not in referenced:
                output.append(path.name)
    return output


def _fold(text: str) -> str:
    return "".join(str(text or "").split()).casefold()


def run_check(*, now: datetime | None = None) -> dict[str, Any]:
    """跑一遍体检。返回 ``{score, issues: [{kind, severity, title, advice, count, ids, fixable}], ...}``。"""

    moment = (now or now_beijing()).replace(tzinfo=None)
    stickers = all_stickers()
    stats = stats_snapshot()
    root = library_root()
    pack_ids = {pack.pack_id for pack in all_packs()}
    found: dict[str, list[str]] = {kind: [] for kind in ISSUES}
    names: dict[str, list[Sticker]] = {}
    for item in stickers:
        names.setdefault(_fold(item.name), []).append(item)
    shape_groups: dict[tuple[int, int, int], list[Sticker]] = {}
    for item in stickers:
        sid = item.sticker_id
        row = stats.get(sid)
        uses = int(row.get("uses", 0)) if isinstance(row, dict) else 0
        last_used = _parse_time(str(row.get("last_used") or "")) if isinstance(row, dict) else None
        created = _parse_time(item.created_at)
        if not (root / item.file_name).is_file():
            found["missing_file"].append(sid)
            continue
        ai = item.ai
        labeled = bool(ai) and not ai.get("undone")
        if labeled and ai.get("safe") is False and item.usable:
            found["unsafe_active"].append(sid)
        if item.placeholder_name:
            found["placeholder_name"].append(sid)
            if item.weight == 0:
                found["weight_zero_unnamed"].append(sid)
        if not item.tags:
            found["no_tags"].append(sid)
        if not item.description:
            found["no_description"].append(sid)
        confidence = ai.get("confidence")
        if labeled and isinstance(confidence, (int, float)) and confidence < LOW_CONFIDENCE:
            found["low_confidence"].append(sid)
        if item.usable and item.status == STATUS_ACTIVE:
            if uses == 0 and created is not None and moment - created > timedelta(days=NEVER_USED_DAYS):
                found["never_used"].append(sid)
            elif uses > 0 and last_used is not None and moment - last_used > timedelta(days=STALE_DAYS):
                found["stale"].append(sid)
        if item.size > HEAVY_BYTES:
            found["heavy"].append(sid)
        if item.width and item.height:
            if min(item.width, item.height) < TINY_SIDE:
                found["tiny"].append(sid)
            shape_groups.setdefault((item.width, item.height, item.frames), []).append(item)
        else:
            found["no_dimensions"].append(sid)
        if any(pack not in pack_ids for pack in item.packs):
            found["dangling_pack"].append(sid)
        for alias in item.aliases:
            if any(other.sticker_id != sid for other in names.get(_fold(alias), [])):
                found["alias_shadowed"].append(sid)
                break
    duplicate_groups: list[list[str]] = []
    for group in shape_groups.values():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda item: item.size)
        cluster = [ordered[0]]
        for item in ordered[1:]:
            if item.size - cluster[-1].size <= max(cluster[-1].size * 0.02, 64):
                cluster.append(item)
                continue
            if len(cluster) > 1:
                duplicate_groups.append([entry.sticker_id for entry in cluster])
            cluster = [item]
        if len(cluster) > 1:
            duplicate_groups.append([entry.sticker_id for entry in cluster])
    found["possible_duplicate"] = [sid for group in duplicate_groups for sid in group]
    orphans = orphan_files()
    health = library_health()
    issues: list[dict[str, Any]] = []
    penalty = 0
    total = max(len(stickers), 1)
    for kind, (severity, title, advice) in ISSUES.items():
        ids = found[kind]
        count = len(orphans) if kind == "orphan_file" else (1 if kind == "index_corrupt" and health["corrupt"] else len(ids))
        if not count:
            continue
        # 扣分看占比：两千张里有三张没写说明不该和“一半都没写”扣一样多。
        share = 1.0 if kind in {"orphan_file", "index_corrupt"} else min(count / total, 1.0)
        penalty += _PENALTY[severity] * (1 + 4 * share) if severity != "info" else _PENALTY[severity] * 6 * share
        issues.append(
            {
                "kind": kind,
                "severity": severity,
                "title": title,
                "advice": advice,
                "count": count,
                "ids": ids[:MAX_IDS_PER_ISSUE],
                "fixable": kind in FIXABLE,
                **({"files": orphans[:MAX_IDS_PER_ISSUE]} if kind == "orphan_file" else {}),
                **({"groups": duplicate_groups[:20]} if kind == "possible_duplicate" else {}),
            }
        )
    order = {"error": 0, "warn": 1, "info": 2}
    issues.sort(key=lambda row: (order[row["severity"]], -row["count"]))
    trashed = trashed_stickers()
    return {
        "score": max(0, round(100 - penalty)),
        "checkedAt": moment.strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(stickers),
        "issues": issues,
        "index": health,
        "trash": {"count": len(trashed), "bytes": sum(item.size for item in trashed)},
        "bytes": sum(item.size for item in stickers),
    }


def fix(kind: str) -> dict[str, Any]:
    """修一类问题；返回 ``{kind, fixed}``。不在 ``FIXABLE`` 里的抛 ``StickerError``。"""

    if kind not in FIXABLE:
        raise StickerError("not_fixable", "这类问题要人来处理，没法一键修")
    fixed = 0
    if kind == "no_dimensions":
        fixed = backfill_meta()
    elif kind == "orphan_file":
        root = library_root()
        for name in orphan_files():
            with contextlib.suppress(OSError):
                (root / name).unlink()
                fixed += 1
    elif kind == "missing_file":
        root = library_root()
        for item in all_stickers():
            if not (root / item.file_name).is_file():
                with contextlib.suppress(StickerError):
                    trash_sticker(item.sticker_id)
                    fixed += 1
    elif kind == "dangling_pack":
        from dataclasses import replace

        known = {pack.pack_id for pack in all_packs()}

        def change(entries: list[Sticker]) -> int:
            count = 0
            for index, item in enumerate(entries):
                kept = tuple(pack for pack in item.packs if pack in known)
                if len(kept) != len(item.packs):
                    entries[index] = replace(item, packs=kept)
                    count += 1
            return count

        fixed = _library.mutate(change)
    return {"kind": kind, "fixed": fixed}


__all__ = ["FIXABLE", "ISSUES", "fix", "orphan_files", "run_check"]
