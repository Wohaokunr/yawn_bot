# ruff: noqa: E501, C901, PLR2004
"""表情包统计：把库、使用统计、每日归档、没对上的名字拼成一份给管理员看的报告。

只读、确定性、不调模型。数据都来自 ``sticker_library`` 和 ``sticker_policy`` 已经
记下的东西（计数、时间、模型自己写的名字），不含任何聊天内容。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .context import now_beijing
from .sticker_labeler import MOODS
from .sticker_library import SOURCE_LABELS, Sticker, all_stickers, is_available
from .sticker_policy import (
    PRIVATE_STAT_KEY,
    REASON_TEXT,
    history_snapshot,
    misses_snapshot,
    send_log,
    stats_snapshot,
    usage_snapshot,
)

TOP_LIMIT = 12
MISS_LIMIT = 40
RECENT_LIMIT = 40
WEEKS = 8


def _uses(stats: dict[str, Any], sticker_id: str) -> int:
    row = stats.get(sticker_id)
    return int(row.get("uses", 0)) if isinstance(row, dict) else 0


def _sum(rows: list[dict[str, Any]], key: str) -> int:
    return sum(int(row.get(key, 0)) for row in rows)


def trend(history: list[dict[str, Any]], key: str = "sent", *, window: int = 7) -> dict[str, Any]:
    """最近 ``window`` 天对比再往前 ``window`` 天。纯函数。"""

    recent = history[-window:]
    previous = history[-2 * window : -window]
    now_total, before_total = _sum(recent, key), _sum(previous, key)
    change = None if before_total == 0 else round((now_total - before_total) / before_total * 100)
    return {"recent": now_total, "previous": before_total, "changePercent": change}


def weekly_growth(stickers: tuple[Sticker, ...] | list[Sticker], *, now: datetime | None = None, weeks: int = WEEKS) -> list[dict[str, Any]]:
    """最近几周每周入库多少张（按来源分）。纯函数。"""

    moment = (now or now_beijing()).replace(tzinfo=None)
    monday = (moment - timedelta(days=moment.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    starts = [monday - timedelta(weeks=offset) for offset in range(weeks - 1, -1, -1)]
    rows = [{"week": start.strftime("%m-%d"), "total": 0, **dict.fromkeys(SOURCE_LABELS, 0)} for start in starts]
    for item in stickers:
        try:
            created = datetime.strptime(item.created_at, "%Y-%m-%d %H:%M:%S")  # noqa: DTZ007
        except ValueError:
            continue
        for index, start in enumerate(starts):
            if start <= created < start + timedelta(weeks=1):
                rows[index]["total"] += 1
                rows[index][item.source] = rows[index].get(item.source, 0) + 1
                break
    return rows


def suggest_for_miss(name: str, stickers: tuple[Sticker, ...] | list[Sticker], *, limit: int = 3) -> list[dict[str, str]]:
    """给一个没对上的名字找“可能是想发这张”的候选（只给管理员看，不用于发送）。

    发送层从不模糊匹配；这里只是帮管理员省得翻库：名字/别名/标签互相包含，或有共同的字。
    """

    folded = "".join(name.split()).casefold()
    if not folded:
        return []
    scored: list[tuple[int, str, str]] = []
    for item in stickers:
        best = 0
        for text, weight in [(item.name, 3), *((alias, 3) for alias in item.aliases), *((tag, 2) for tag in item.tags)]:
            value = "".join(text.split()).casefold()
            if not value:
                continue
            if value in folded or folded in value:
                best = max(best, weight * 10 + min(len(value), len(folded)))
            else:
                shared = len(set(value) & set(folded))
                if shared >= 2:
                    best = max(best, weight * 2 + shared)
        if best:
            scored.append((best, item.sticker_id, item.name))
    scored.sort(key=lambda row: (-row[0], row[2]))
    seen: set[str] = set()
    output: list[dict[str, str]] = []
    for _score, sticker_id, label in scored:
        if label in seen:
            continue
        seen.add(label)
        output.append({"id": sticker_id, "name": label})
        if len(output) >= limit:
            break
    return output


def build_insights(*, days: int = 30) -> dict[str, Any]:
    stickers = all_stickers()
    by_id = {item.sticker_id: item for item in stickers}
    stats = stats_snapshot()
    usage = usage_snapshot()
    history = history_snapshot(days)
    used = sorted(((_uses(stats, item.sticker_id), item) for item in stickers), key=lambda row: -row[0])
    top = [
        {
            "id": item.sticker_id,
            "name": item.name,
            "uses": uses,
            "lastUsed": (stats.get(item.sticker_id) or {}).get("last_used"),
            "sentToday": int(usage.get("stickers", {}).get(item.sticker_id, 0)),
        }
        for uses, item in used[:TOP_LIMIT]
        if uses > 0
    ]
    usable = [item for item in stickers if item.usable]
    never = [item for uses, item in used if uses == 0 and item.usable]
    total_uses = sum(uses for uses, _item in used)
    # 集中度：前 10% 的图占了多少次发送（越高说明老发那几张）。
    head = max(len(usable) // 10, 1)
    concentration = round(sum(uses for uses, _item in used[:head]) / total_uses * 100) if total_uses else 0
    group_totals: dict[str, int] = {}
    group_favorite: dict[str, tuple[int, str]] = {}
    for sticker_id, row in stats.items():
        groups = row.get("groups") if isinstance(row, dict) else None
        if not isinstance(groups, dict) or sticker_id not in by_id:
            continue
        for group, count in groups.items():
            number = int(count or 0)
            group_totals[group] = group_totals.get(group, 0) + number
            if number > group_favorite.get(group, (0, ""))[0]:
                group_favorite[group] = (number, by_id[sticker_id].name)
    today_groups = usage.get("groups", {})
    groups = [
        {
            "groupId": None if group == PRIVATE_STAT_KEY else group,
            "private": group == PRIVATE_STAT_KEY,
            "uses": count,
            "today": int(today_groups.get(group, 0)) if group != PRIVATE_STAT_KEY else int(usage.get("totals", {}).get("private_sent", 0)),
            "favorite": group_favorite.get(group, (0, ""))[1],
        }
        for group, count in sorted(group_totals.items(), key=lambda pair: -pair[1])[:TOP_LIMIT]
    ]
    moods: dict[str, int] = dict.fromkeys(MOODS, 0)
    unlabeled_mood = 0
    sources: dict[str, int] = dict.fromkeys(SOURCE_LABELS, 0)
    animated = 0
    total_bytes = 0
    tags: dict[str, list[int]] = {}
    for item in stickers:
        mood = str(item.ai.get("mood") or "")
        if mood in moods and not item.ai.get("undone"):
            moods[mood] += 1
        else:
            unlabeled_mood += 1
        sources[item.source] = sources.get(item.source, 0) + 1
        animated += 1 if item.animated else 0
        total_bytes += item.size
        for tag in item.tags:
            row = tags.setdefault(tag, [0, 0])
            row[0] += 1
            row[1] += _uses(stats, item.sticker_id)
    everywhere = [item for item in usable if is_available(item, None)]
    misses = misses_snapshot()
    drops = usage.get("drops", {})
    recent = []
    for row in reversed(send_log()[-RECENT_LIMIT:]):
        item = by_id.get(str(row.get("stickerId")))
        recent.append(
            {
                "at": row.get("at"),
                "groupId": str(row["groupId"]) if row.get("groupId") else None,
                "stickerId": row.get("stickerId"),
                "name": item.name if item is not None else "（已删除）",
            }
        )
    contributors: dict[str, int] = {}
    for item in stickers:
        if item.added_by and item.source == "member":
            contributors[str(item.added_by)] = contributors.get(str(item.added_by), 0) + 1
    return {
        "days": days,
        "history": history,
        "trend": {key: trend(history, key) for key in ("sent", "stolen", "collected", "dropped")},
        "totals": {
            "uses": total_uses,
            "usable": len(usable),
            "everywhere": len(everywhere),
            "neverUsed": len(never),
            "animated": animated,
            "bytes": total_bytes,
            "concentration": concentration,
        },
        "top": top,
        "neverUsed": [{"id": item.sticker_id, "name": item.name, "createdAt": item.created_at or None} for item in never[:TOP_LIMIT]],
        "groups": groups,
        "moods": [{"mood": mood, "count": count} for mood, count in moods.items()],
        "moodGaps": [mood for mood, count in moods.items() if count == 0 and mood != "其他"],
        "moodUnknown": unlabeled_mood,
        "sources": [{"source": key, "label": SOURCE_LABELS.get(key, key), "count": count} for key, count in sources.items()],
        "tags": [{"tag": tag, "count": row[0], "uses": row[1]} for tag, row in sorted(tags.items(), key=lambda pair: (-pair[1][1], -pair[1][0]))[:24]],
        "drops": [
            {"reason": reason, "reasonText": REASON_TEXT.get(reason, reason), "count": int(count)}
            for reason, count in sorted(drops.items(), key=lambda pair: -int(pair[1]))
        ],
        "misses": [
            {
                "name": row.get("name"),
                "count": int(row.get("count", 0)),
                "last": row.get("last"),
                "groups": [str(group) for group in row.get("groups") or []],
                "suggestions": suggest_for_miss(str(row.get("name") or ""), usable),
            }
            for row in misses[:MISS_LIMIT]
        ],
        "missCount": len(misses),
        "recent": recent,
        "growth": weekly_growth(stickers),
        "contributors": [{"userId": user, "count": count} for user, count in sorted(contributors.items(), key=lambda pair: -pair[1])[:8]],
    }


__all__ = ["build_insights", "suggest_for_miss", "trend", "weekly_growth"]
