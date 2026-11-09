# ruff: noqa: E501, TC003
"""好感玩法的个人数据：背包、专属称呼、每日任务、礼物、升级记录、护盾、问候标记。

每人一个 JSON 文件（``data/affinity/users/<QQ>.json``，目录可用 ``AFFINITY_DATA_DIR``
改），不建表、不需要迁移；读-改-写在进程内一把锁里完成，写入走临时文件再替换。
好感余额本身仍只在数据库 ``BotUser.affinity``，这里不存任何好感数值。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from .affinity_policy import data_root

T = TypeVar("T")

_LOCK = threading.RLock()
# 升级记录、礼物记录最多留多少条
HISTORY_LIMIT = 30
GIFT_LOG_LIMIT = 50


def users_dir() -> Path:
    return data_root() / "users"


def _path(user_id: int) -> Path:
    return users_dir() / f"{int(user_id)}.json"


def empty_record() -> dict[str, Any]:
    return {
        "version": 1,
        # 道具 {key: 数量}
        "inventory": {},
        # /叫我 设置的专属称呼
        "nickname": "",
        # 每日任务 {"day": "YYYY-MM-DD", "items": [...], "bonus_claimed": bool, "refreshed": int}
        "quests": {},
        # 今天送礼 {"day": ..., "count": n}
        "gifts_today": {},
        # 送过的礼物累计 {key: 次数}
        "gift_totals": {},
        # 最近送礼 [{key, count, at, group_id, affinity}]
        "gift_log": [],
        # 升级记录 [{level, at}]
        "levels": [],
        # 升级了还没在对话里提过的等级 {"level": ..., "at": ..., "private": bool, "group": bool}
        "pending_levelup": {},
        # 好感护盾到期日 YYYY-MM-DD
        "shield_until": "",
        # 签到暴击卡生效日（当天下一次签到积分翻倍）
        "lucky_day": "",
        # 衰减结算到哪天
        "decay_day": "",
        # 最近一次私聊来往的时间（久别问候用）与已问候的那一段
        "last_private": "",
        "care_for": "",
        # 互动 {"day": ..., "count": n, "by": {key: n}}
        "interact_today": {},
        "interact_totals": {},
        # 道具使用记录 [{key, at, note}]
        "item_log": [],
    }


def _normalize(raw: Any) -> dict[str, Any]:
    record = empty_record()
    if not isinstance(raw, dict):
        return record
    for key, default in record.items():
        value = raw.get(key, default)
        if isinstance(default, list) and isinstance(value, list):
            record[key] = [item for item in value if isinstance(item, dict)]
        elif (isinstance(default, dict) and isinstance(value, dict)) or (isinstance(default, str) and isinstance(value, str)) or (isinstance(default, int) and isinstance(value, int)):
            record[key] = value
    inventory = record["inventory"]
    record["inventory"] = {
        str(k): int(v) for k, v in inventory.items() if isinstance(v, int) and v > 0
    }
    return record


def load(user_id: int) -> dict[str, Any]:
    path = _path(user_id)
    with _LOCK:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return empty_record()
        except (OSError, ValueError):
            return empty_record()
    return _normalize(raw)


def save(user_id: int, record: dict[str, Any]) -> None:
    path = _path(user_id)
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)


def update(user_id: int, fn: Callable[[dict[str, Any]], T]) -> T:
    with _LOCK:
        record = load(user_id)
        result = fn(record)
        save(user_id, record)
        return result


def exists(user_id: int) -> bool:
    return _path(user_id).exists()


def known_user_ids() -> list[int]:
    try:
        names = [p.stem for p in users_dir().glob("*.json")]
    except OSError:
        return []
    return sorted(int(name) for name in names if name.isdigit())


def delete(user_id: int) -> bool:
    path = _path(user_id)
    with _LOCK:
        try:
            path.unlink()
        except FileNotFoundError:
            return False
    return True


def add_items(record: dict[str, Any], items: dict[str, int], *, cap: dict[str, int] | None = None) -> dict[str, int]:
    """往背包加道具（原地），返回实际加上的数量；``cap`` 是每种最多持有多少。"""

    inventory = record.setdefault("inventory", {})
    added: dict[str, int] = {}
    for key, count in items.items():
        if count <= 0:
            continue
        have = int(inventory.get(key, 0))
        limit = (cap or {}).get(key)
        target = have + int(count) if limit is None else min(have + int(count), limit)
        if target > have:
            inventory[key] = target
            added[key] = target - have
    return added


def take_item(record: dict[str, Any], key: str, count: int = 1) -> bool:
    inventory = record.setdefault("inventory", {})
    have = int(inventory.get(key, 0))
    if have < count:
        return False
    if have == count:
        inventory.pop(key, None)
    else:
        inventory[key] = have - count
    return True


def push_limited(record: dict[str, Any], key: str, entry: dict[str, Any], limit: int) -> None:
    rows = record.setdefault(key, [])
    rows.append(entry)
    if len(rows) > limit:
        del rows[: len(rows) - limit]


__all__ = [
    "GIFT_LOG_LIMIT",
    "HISTORY_LIMIT",
    "add_items",
    "delete",
    "empty_record",
    "exists",
    "known_user_ids",
    "load",
    "push_limited",
    "save",
    "take_item",
    "update",
    "users_dir",
]
