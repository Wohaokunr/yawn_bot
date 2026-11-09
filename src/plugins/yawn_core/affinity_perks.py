# ruff: noqa: ARG001, C901, E501, PLW2901, RUF001, SIM105, TRY300
"""好感等级的实际作用：权益计算、额度加成、每日任务、升级记录、衰减、提示词视图。

策略在 :mod:`affinity_policy`，个人数据在 :mod:`affinity_store`，好感余额的唯一写入口
仍是 :func:`affinity.grant`。这里负责“到了某一级到底多了什么”，并在各处真正读取：

- **额度加成** :func:`bonus_limit`：私聊轮数、私聊联网、私聊表情包、群里沙箱/网页搜索/
  收藏表情包、私聊人设自助修改、私聊记忆条数。基础额度为 0（被管理员关掉）时不加，
  策略总开关关掉时一律按基础额度。
- **同步读取**：沙箱/网页搜索这类检查点没有数据库会话，读进程内的余额缓存
  （:func:`remember_points`）；缓存在每次 ``grant``/``snapshot``/上下文加载时刷新，
  没缓存时按“陌生”算（只会少给，不会多给）。
- **事件钩子** :func:`after_grant`：由 ``affinity.grant`` 调用，记任务进度、升级/降级、
  升级礼包、最近一次私聊时间；只写个人数据 JSON，失败一律吞掉，不影响好感本身。
- **每日任务** 三个由结构事实推进（签到、私聊、群里找 Bot、送礼、互动），完成后由
  调用方 :func:`settle_quests` 发奖励，不在 ``grant`` 里套娃。
- **衰减** :func:`settle_decay` / :func:`run_decay`：每天凌晨跑一次，长时间没有任何
  正向来往才掉，默认只掉到当前等级下限，护盾期内不掉。
- **提示词视图** :func:`speaker_view`：专属称呼、这一级的分寸、刚升级、今天收到的礼物；
  只有词语，不给模型数值。
"""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from . import affinity_store as store
from .affinity import level_for
from .affinity_policy import (
    INTERACT_KEYS,
    UNLOCKS,
    AffinityPolicy,
    LevelPerks,
    load_policy,
)

# ── 余额缓存（给没有数据库会话的同步检查点） ─────────────────────────

_CACHE_TTL = 6 * 3600.0
_CACHE_MAX = 20000
_points: dict[int, tuple[int, float]] = {}


def remember_points(user_id: int | None, points: int | None) -> None:
    if not user_id:
        return
    if len(_points) >= _CACHE_MAX:
        # 粗暴但够用：满了就丢掉最旧的一半
        for key, _value in sorted(_points.items(), key=lambda kv: kv[1][1])[: _CACHE_MAX // 2]:
            _points.pop(key, None)
    _points[int(user_id)] = (max(int(points or 0), 0), time.monotonic())


def cached_points(user_id: int | None) -> int | None:
    if not user_id:
        return None
    hit = _points.get(int(user_id))
    if hit is None or time.monotonic() - hit[1] > _CACHE_TTL:
        return None
    return hit[0]


def clear_points_cache() -> None:
    _points.clear()


async def load_points(session: Any, user_id: int) -> int:
    """从库里读余额（并刷新缓存）。"""

    from .data_models.bot_user import BotUser

    user = await session.get(BotUser, int(user_id))
    points = max(int(user.affinity or 0), 0) if user is not None else 0
    remember_points(user_id, points)
    return points


# ── 权益 ────────────────────────────────────────────────────────────


def policy() -> AffinityPolicy:
    return load_policy()


def perks_for(points: int | None, current: AffinityPolicy | None = None) -> LevelPerks:
    pol = current or load_policy()
    return pol.perks(level_for(points).name)


def has_unlock(points: int | None, unlock: str, current: AffinityPolicy | None = None) -> bool:
    pol = current or load_policy()
    if not pol.enabled:
        return False
    return unlock in perks_for(points, pol).unlocks


_PCT_FIELDS = frozenset({"private_turns_pct", "checkin_points_pct"})
BONUS_FIELDS: dict[str, str] = {
    "private_turns_pct": "私聊每天能聊的轮数",
    "private_web_bonus": "私聊每天联网搜索次数",
    "private_sticker_bonus": "私聊每天表情包条数",
    "web_search_bonus": "群里每天网页搜索次数",
    "sandbox_bonus": "群里每天代码沙箱次数",
    "sticker_collect_bonus": "群里每天收藏表情包张数",
    "persona_edit_bonus": "每天自己改私聊人设次数",
    "memory_bonus": "爱好/喜好这类记忆每类多记几样",
    "checkin_points_pct": "签到积分",
}


def apply_bonus(base: int, perks: LevelPerks, field: str) -> int:
    """在基础额度上加这一级的加成；基础额度 ≤ 0（关掉/不限）原样返回。"""

    base = int(base)
    if base <= 0:
        return base
    extra = int(getattr(perks, field, 0) or 0)
    if extra <= 0:
        return base
    if field in _PCT_FIELDS:
        return base + max(math.ceil(base * extra / 100), 1)
    return base + extra


def bonus_limit(base: int, user_id: int | None, field: str, *, points: int | None = None) -> int:
    """某人在某项额度上的实际上限。``points`` 不给时读缓存（没缓存按陌生）。"""

    pol = load_policy()
    if not pol.enabled or not user_id:
        return int(base)
    value = points if points is not None else cached_points(user_id)
    return apply_bonus(base, pol.perks(level_for(value).name), field)


def bonus_note(base: int, actual: int, user_id: int | None, *, points: int | None = None) -> str:
    """额度提示里的一句“（好感「朋友」+8）”；没加成时为空。"""

    if actual <= base:
        return ""
    value = points if points is not None else cached_points(user_id)
    return f"（好感「{level_for(value).name}」多给了 {actual - base}）"


def perks_public(perks: LevelPerks) -> dict[str, Any]:
    """给工具/WebUI 的权益投影（只有说明，不含好感余额）。"""

    bonuses = []
    for field, label in BONUS_FIELDS.items():
        value = int(getattr(perks, field, 0) or 0)
        if value > 0:
            bonuses.append({"key": field, "label": label, "value": value, "unit": "%" if field in _PCT_FIELDS else "次"})
    return {
        "level": perks.level,
        "tone": perks.tone,
        "behavior": perks.behavior,
        "bonuses": bonuses,
        "gift_daily": perks.gift_daily,
        "shop_discount_pct": perks.shop_discount_pct,
        "unlocks": [{"key": key, "label": UNLOCKS[key][0], "description": UNLOCKS[key][1]} for key in perks.unlocks],
        "levelup_items": dict(perks.levelup_items),
    }


def next_unlocks(points: int | None, current: AffinityPolicy | None = None) -> dict[str, Any] | None:
    """下一级比这一级多了什么（解锁项 + 加成差值）；已是最高级返回 None。"""

    pol = current or load_policy()
    level = level_for(points)
    if level.next_name is None:
        return None
    now_perks, next_perks = pol.perks(level.name), pol.perks(level.next_name)
    new_unlocks = [key for key in next_perks.unlocks if key not in now_perks.unlocks]
    better = []
    for field, label in BONUS_FIELDS.items():
        delta = int(getattr(next_perks, field, 0) or 0) - int(getattr(now_perks, field, 0) or 0)
        if delta > 0:
            better.append({"key": field, "label": label, "delta": delta, "unit": "%" if field in _PCT_FIELDS else "次"})
    return {
        "level": level.next_name,
        "to_next": level.to_next(int(points or 0)),
        "unlocks": [{"key": key, "label": UNLOCKS[key][0], "description": UNLOCKS[key][1]} for key in new_unlocks],
        "better": better,
        "gift_daily": next_perks.gift_daily,
        "shop_discount_pct": next_perks.shop_discount_pct,
        "levelup_items": dict(next_perks.levelup_items),
    }


def unlock_summary(perks: LevelPerks) -> str:
    names = [UNLOCKS[key][0] for key in perks.unlocks if key in UNLOCKS]
    return "、".join(names)


# ── 动态日上限（礼物、互动的上限由策略决定） ─────────────────────────


def dynamic_daily_cap(source: str) -> int | bool | None:
    """``grant`` 查来源日上限时先问这里；返回 False 表示用 SOURCE_RULES 的静态值。"""

    pol = load_policy()
    if source == "gift":
        return int(pol.gift_affinity_daily_cap)
    if source == "interact":
        return int(pol.interact_affinity_daily_cap)
    if source == "quest":
        return int(pol.quests.reward_affinity) * int(pol.quests.per_day)
    return False


# ── 每日任务 ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class QuestKind:
    key: str
    title: str
    target: int
    sources: tuple[str, ...]


QUEST_KINDS: tuple[QuestKind, ...] = (
    QuestKind("checkin", "签到一次", 1, ("checkin",)),
    QuestKind("checkin_multi", "在 2 个群签到", 2, ("checkin",)),
    QuestKind("private_chat", "私聊和我聊 3 轮", 3, ("private_chat",)),
    QuestKind("group_chat", "在群里找我聊 2 次", 2, ("group_chat",)),
    QuestKind("gift", "送我一份礼物", 1, ("gift",)),
    QuestKind("interact", "和我互动一次（/击掌 等）", 1, ("interact",)),
)
QUEST_BY_KEY = {kind.key: kind for kind in QUEST_KINDS}


def _day(now: datetime) -> str:
    return now.date().isoformat()


def _pick_quests(user_id: int, day: str, count: int, *, salt: int = 0) -> list[str]:
    keys = [kind.key for kind in QUEST_KINDS]
    digest = hashlib.sha256(f"quest:{user_id}:{day}:{salt}".encode()).digest()
    order = sorted(range(len(keys)), key=lambda i: digest[i])
    picked: list[str] = []
    for index in order:
        key = keys[index]
        # 签到类只放一个，不然一天全是签到
        if key.startswith("checkin") and any(p.startswith("checkin") for p in picked):
            continue
        picked.append(key)
        if len(picked) >= count:
            break
    return picked


def ensure_quests(record: dict[str, Any], user_id: int, now: datetime, pol: AffinityPolicy | None = None) -> dict[str, Any]:
    """原地保证 ``record['quests']`` 是今天的任务；返回它。"""

    pol = pol or load_policy()
    day = _day(now)
    quests = record.get("quests") or {}
    if quests.get("day") != day or not isinstance(quests.get("items"), list):
        quests = {
            "day": day,
            "items": [
                {"key": key, "progress": 0, "done": False, "claimed": False}
                for key in _pick_quests(user_id, day, pol.quests.per_day)
            ],
            "bonus_claimed": False,
            "refreshed": 0,
        }
        record["quests"] = quests
    return quests


def quests_view(record: dict[str, Any], user_id: int, now: datetime, pol: AffinityPolicy | None = None) -> dict[str, Any]:
    pol = pol or load_policy()
    quests = ensure_quests(record, user_id, now, pol)
    items = []
    for item in quests["items"]:
        kind = QUEST_BY_KEY.get(str(item.get("key")))
        if kind is None:
            continue
        items.append(
            {
                "key": kind.key,
                "title": kind.title,
                "progress": min(int(item.get("progress") or 0), kind.target),
                "target": kind.target,
                "done": bool(item.get("done")),
                "claimed": bool(item.get("claimed")),
            }
        )
    return {
        "enabled": pol.enabled and pol.quests.enabled,
        "day": quests["day"],
        "items": items,
        "reward_affinity": pol.quests.reward_affinity,
        "all_done_item": pol.quests.all_done_item,
        "all_done_item_name": pol.item_name(pol.quests.all_done_item) if pol.quests.all_done_item else "",
        "bonus_claimed": bool(quests.get("bonus_claimed")),
        "all_done": bool(items) and all(row["done"] for row in items),
    }


def _advance_quests(record: dict[str, Any], user_id: int, source: str, now: datetime, pol: AffinityPolicy) -> list[str]:
    quests = ensure_quests(record, user_id, now, pol)
    finished: list[str] = []
    for item in quests["items"]:
        kind = QUEST_BY_KEY.get(str(item.get("key")))
        if kind is None or item.get("done") or source not in kind.sources:
            continue
        item["progress"] = int(item.get("progress") or 0) + 1
        if item["progress"] >= kind.target:
            item["done"] = True
            finished.append(kind.key)
    return finished


def refresh_quests(record: dict[str, Any], user_id: int, now: datetime, pol: AffinityPolicy | None = None) -> list[str]:
    """用任务刷新券：没完成的换一批（已完成的保留）。返回新换上的任务 key。"""

    pol = pol or load_policy()
    quests = ensure_quests(record, user_id, now, pol)
    kept = [item for item in quests["items"] if item.get("done")]
    kept_keys = {str(item["key"]) for item in kept}
    salt = int(quests.get("refreshed") or 0) + 1
    fresh: list[dict[str, Any]] = []
    for key in _pick_quests(user_id, quests["day"], len(QUEST_KINDS), salt=salt):
        if key in kept_keys or len(kept) + len(fresh) >= pol.quests.per_day:
            continue
        if key.startswith("checkin") and any(k.startswith("checkin") for k in kept_keys):
            continue
        fresh.append({"key": key, "progress": 0, "done": False, "claimed": False})
    quests["items"] = kept + fresh
    quests["refreshed"] = salt
    return [str(item["key"]) for item in fresh]


async def settle_quests(session: Any, user_id: int, *, now: datetime) -> dict[str, Any]:
    """把已完成没领的任务奖励发掉（好感 + 全做完的道具）。不提交事务。"""

    from . import affinity

    pol = load_policy()
    result: dict[str, Any] = {"claimed": [], "affinity": 0, "bonus_item": None}
    if not (pol.enabled and pol.quests.enabled):
        return result
    claim: list[str] = []
    bonus: str | None = None

    def take(record: dict[str, Any]) -> None:
        nonlocal bonus
        quests = ensure_quests(record, user_id, now, pol)
        for item in quests["items"]:
            if item.get("done") and not item.get("claimed"):
                item["claimed"] = True
                claim.append(str(item["key"]))
        if quests["items"] and all(item.get("done") for item in quests["items"]) and not quests.get("bonus_claimed"):
            quests["bonus_claimed"] = True
            if pol.quests.all_done_item:
                bonus = pol.quests.all_done_item
                cap = {row.key: row.max_hold for row in pol.shop}
                store.add_items(record, {bonus: 1}, cap=cap)

    store.update(user_id, take)
    for key in claim:
        if pol.quests.reward_affinity > 0:
            change = await affinity.grant(
                session, user_id, "quest", pol.quests.reward_affinity, note=f"完成任务：{QUEST_BY_KEY[key].title}", now=now
            )
            result["affinity"] += change.applied
        result["claimed"].append(QUEST_BY_KEY[key].title)
    if bonus:
        result["bonus_item"] = pol.item_name(bonus)
    return result


def quest_reply(settled: dict[str, Any]) -> str:
    """命令回复里追加的一句；没东西领时为空。"""

    if not settled.get("claimed"):
        return ""
    text = f"✅ 完成任务：{'、'.join(settled['claimed'])}"
    if settled.get("affinity"):
        text += f"，好感 +{settled['affinity']}"
    if settled.get("bonus_item"):
        text += f"；今天的任务全做完啦，送你一张{settled['bonus_item']}"
    return text


# ── grant 钩子 ──────────────────────────────────────────────────────


def after_grant(change: Any, *, user_id: int, source: str, group_id: int | None, now: datetime) -> None:
    """``affinity.grant`` 每次调用后执行：刷新缓存、任务进度、升级记录。失败吞掉。"""

    try:
        if change.available and (change.applied or change.after):
            remember_points(user_id, change.after)
        pol = load_policy()
        if not pol.enabled:
            return
        quest_source = source in {"checkin", "private_chat", "group_chat", "gift", "interact"} and change.requested > 0
        level_changed = change.applied and change.level_after.index != change.level_before.index
        private_seen = source == "private_chat" and change.requested > 0
        if not (quest_source and pol.quests.enabled) and not level_changed and not private_seen:
            return

        def apply(record: dict[str, Any]) -> None:
            if quest_source and pol.quests.enabled:
                _advance_quests(record, user_id, source, now, pol)
            if private_seen:
                record["last_private"] = now.isoformat(timespec="seconds")
                record["care_for"] = ""
            if level_changed:
                _record_level_change(record, change, now, pol)

        store.update(user_id, apply)
    except Exception:  # noqa: BLE001
        from nonebot import logger

        logger.opt(exception=True).debug(f"好感钩子失败(忽略) user={user_id} source={source}")


def _record_level_change(record: dict[str, Any], change: Any, now: datetime, pol: AffinityPolicy) -> None:
    before, after = change.level_before, change.level_after
    stamp = now.isoformat(timespec="seconds")
    store.push_limited(record, "levels", {"level": after.name, "at": stamp, "up": after.index > before.index}, store.HISTORY_LIMIT)
    if after.index <= before.index:
        record["pending_levelup"] = {}
        return
    # 升级礼包：跨了几级就给几级的礼包；只给第一次到达（降级再升回来不重复送）
    reached = {str(row.get("level")) for row in record.get("levels", [])[:-1] if row.get("up")}
    cap = {row.key: row.max_hold for row in pol.shop}
    gifts: dict[str, int] = {}
    for index in range(before.index + 1, after.index + 1):
        perks = pol.levels[index]
        if perks.level in reached or "levelup_gift" not in perks.unlocks:
            continue
        for key, count in perks.levelup_items.items():
            gifts[key] = gifts.get(key, 0) + count
    added = store.add_items(record, gifts, cap=cap) if gifts else {}
    record["pending_levelup"] = {
        "level": after.name,
        "at": stamp,
        "items": {pol.item_name(k): v for k, v in added.items()},
        "private": False,
        "group": False,
    }


# ── 提示词视图 ──────────────────────────────────────────────────────

# 升级多久内还算“刚升级”
LEVELUP_FRESH = timedelta(days=3)


def _fresh_levelup(record: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    pending = record.get("pending_levelup") or {}
    if not pending.get("level"):
        return None
    try:
        at = datetime.fromisoformat(str(pending.get("at")))
    except ValueError:
        return None
    if now - at > LEVELUP_FRESH:
        return None
    return pending


def nickname_for(record: dict[str, Any], points: int | None, pol: AffinityPolicy | None = None) -> str:
    """生效中的专属称呼：设过且当前等级解锁了 nickname 才算。"""

    name = str(record.get("nickname") or "").strip()
    if not name:
        return ""
    return name if has_unlock(points, "nickname", pol) else ""


def speaker_view(user_id: int, points: int | None, now: datetime, *, channel: str) -> dict[str, Any]:
    """给 Agent 发言人层的字段（只有词语）：称呼、分寸、刚升级、今天的礼物。

    ``channel`` 为 ``group`` / ``private``：刚升级的提示每个渠道各出现一次就标记掉。
    """

    pol = load_policy()
    if not pol.enabled:
        return {}
    remember_points(user_id, points)
    view: dict[str, Any] = {}
    perks = pol.perks(level_for(points).name)
    if perks.behavior and level_for(points).index > 0:
        view["what_you_can_do"] = perks.behavior
    try:
        record = store.load(user_id)
    except Exception:  # noqa: BLE001
        return view
    name = nickname_for(record, points, pol)
    if name:
        view["call_them"] = name
    pending = _fresh_levelup(record, now) if pol.announce_levelup else None
    if pending and not pending.get(channel):
        text = f"你们的关系刚升到「{pending['level']}」"
        unlocked = unlock_summary(pol.perks(str(pending["level"])))
        if unlocked:
            text += f"（他现在能用：{unlocked}）"
        view["just_leveled_up"] = text + "；可以自然地提一句，别报数值"

        def mark(rec: dict[str, Any]) -> None:
            if isinstance(rec.get("pending_levelup"), dict) and rec["pending_levelup"].get("level") == pending["level"]:
                rec["pending_levelup"][channel] = True

        try:
            store.update(user_id, mark)
        except Exception:  # noqa: BLE001
            pass
    gifts = record.get("gifts_today") or {}
    if gifts.get("day") == _day(now) and gifts.get("names"):
        view["gifts_today"] = f"他今天送了你：{'、'.join(list(gifts['names'])[-3:])}"
    return view


# ── 衰减 ────────────────────────────────────────────────────────────


async def last_active_days(session: Any, user_ids: list[int] | None = None) -> dict[int, date]:
    """每人最近一次正向好感来往（不含衰减/管理员调整）的日期。"""

    from sqlalchemy import func, select

    from .data_models.affinity_event import AffinityEvent

    statement = (
        select(AffinityEvent.user_id, func.max(AffinityEvent.day))
        .where(AffinityEvent.delta > 0, AffinityEvent.source.notin_(("decay", "manual")))
        .group_by(AffinityEvent.user_id)
    )
    if user_ids is not None:
        statement = statement.where(AffinityEvent.user_id.in_(user_ids))
    rows = (await session.execute(statement)).all()
    out: dict[int, date] = {}
    for uid, day in rows:
        if isinstance(day, str):
            day = date.fromisoformat(day)
        if day is not None:
            out[int(uid)] = day
    return out


def decay_amount(points: int, last_active: date | None, today: date, record: dict[str, Any], pol: AffinityPolicy) -> int:
    """这次要掉多少（正数）；纯函数，便于测试。"""

    decay = pol.decay
    if not (pol.enabled and decay.enabled) or points <= 0 or last_active is None:
        return 0
    shield = str(record.get("shield_until") or "")
    if shield and shield >= today.isoformat():
        return 0
    start = last_active + timedelta(days=decay.idle_days)
    settled = str(record.get("decay_day") or "")
    if settled:
        try:
            start = max(start, date.fromisoformat(settled) + timedelta(days=1))
        except ValueError:
            pass
    days = (today - start).days + 1
    if days <= 0:
        return 0
    amount = days * decay.per_day
    floor = level_for(points).floor if decay.keep_level else 0
    return max(min(amount, points - floor), 0)


async def run_decay(session: Any, *, now: datetime) -> dict[int, int]:
    """所有人结算一次衰减。不提交事务。返回 {user_id: 掉了多少}。"""

    from sqlalchemy import select

    from . import affinity
    from .data_models.bot_user import BotUser

    pol = load_policy()
    if not (pol.enabled and pol.decay.enabled) or affinity.ledger_known_missing():
        return {}
    today = now.date()
    rows = (await session.execute(select(BotUser.user_id, BotUser.affinity).where(BotUser.affinity > 0))).all()
    if not rows:
        return {}
    active = await last_active_days(session, [int(uid) for uid, _ in rows])
    result: dict[int, int] = {}
    for uid, points in rows:
        uid, points = int(uid), int(points or 0)
        record = store.load(uid) if store.exists(uid) else store.empty_record()
        amount = decay_amount(points, active.get(uid), today, record, pol)
        if amount <= 0:
            continue
        change = await affinity.grant(session, uid, "decay", -amount, note=f"{pol.decay.idle_days} 天以上没有来往", now=now)
        if change.applied:
            result[uid] = -change.applied

            def mark(rec: dict[str, Any]) -> None:
                rec["decay_day"] = today.isoformat()

            store.update(uid, mark)
    return result


# ── 久别问候（私聊调度器调用） ───────────────────────────────────────

_CARE = (
    "{name}好几天没来找我聊天啦，最近还好吗？",
    "{name}，最近忙什么呢？有空来聊聊呀～",
    "突然想起{name}了，这几天过得怎么样？",
    "{name}～好久没聊了，有什么新鲜事要分享吗？",
)
CARE_HOURS = range(10, 22)


def care_message(user_id: int, record: dict[str, Any], points: int | None, now: datetime, *, name: str = "") -> str | None:
    """该不该发一句久别问候；发的话原地标记并返回文本。纯逻辑（调用方负责免打扰/开关）。"""

    pol = load_policy()
    if now.hour not in CARE_HOURS or not has_unlock(points, "care", pol):
        return None
    last = str(record.get("last_private") or "")
    if not last or record.get("care_for") == last:
        return None
    try:
        last_at = datetime.fromisoformat(last)
    except ValueError:
        return None
    idle = (now - last_at).days
    if idle < pol.care.idle_days or idle > pol.care.give_up_days:
        return None
    record["care_for"] = last
    call = nickname_for(record, points, pol) or name
    digest = hashlib.sha256(f"care:{user_id}:{last}".encode()).digest()
    return _CARE[digest[0] % len(_CARE)].format(name=call)


async def private_nudges(user_id: int, now: datetime, *, name: str = "") -> list[str]:
    """私聊调度器每轮问一次：要不要发久别问候。任何失败返回空。"""

    try:
        if not store.exists(user_id):
            return []
        points = cached_points(user_id)
        if points is None:
            from nonebot_plugin_orm import get_session

            async with get_session() as session:
                points = await load_points(session, user_id)
        texts: list[str] = []

        def plan(record: dict[str, Any]) -> None:
            text = care_message(user_id, record, points, now, name=name)
            if text:
                texts.append(text)

        store.update(user_id, plan)
        return texts
    except Exception:  # noqa: BLE001
        return []


# ── 互动动作 ────────────────────────────────────────────────────────

INTERACT_COMMANDS: dict[str, str] = {
    "击掌": "interact.highfive",
    "摸头": "interact.pat",
    "抱抱": "interact.hug",
    "贴贴": "interact.cuddle",
    "举高高": "interact.lift",
}

_INTERACT_REPLIES: dict[str, tuple[str, ...]] = {
    "interact.highfive": ("啪！耶～", "击掌成功✋ 今天也要加油！", "（伸手）啪！默契满分～"),
    "interact.pat": ("嘿嘿，被摸头了～", "（蹭蹭）再摸一下也可以哦", "头发要被揉乱啦～不过没关系"),
    "interact.hug": ("抱住！暖暖的～", "（张开手）来，抱一个！", "抱抱～今天辛苦啦"),
    "interact.cuddle": ("贴贴～最喜欢{name}了", "（贴过来）嘿嘿嘿", "贴贴！今天也是好朋友～"),
    "interact.lift": ("哇——好高！放我下来啦～", "举高高！视野好好～", "飞起来啦！{name}力气好大"),
}
# 互动一次给的好感（受策略的 interact_affinity_daily_cap 约束）
INTERACT_AFFINITY = 1


def interact_reply(unlock: str, user_id: int, now: datetime, *, name: str) -> str:
    options = _INTERACT_REPLIES.get(unlock, ("嘿嘿～",))
    digest = hashlib.sha256(f"{unlock}:{user_id}:{now:%Y%m%d%H%M}".encode()).digest()
    return options[digest[0] % len(options)].format(name=name or "你")


def interact_refusal(unlock: str, points: int | None, pol: AffinityPolicy | None = None) -> str:
    pol = pol or load_policy()
    need = pol.unlock_level(unlock)
    label = UNLOCKS.get(unlock, (unlock, ""))[0]
    if need is None:
        return f"「{label}」现在没有开放哦"
    return f"我们现在还是「{level_for(points).name}」，等熟到「{need}」再{label}吧～"


__all__ = [
    "BONUS_FIELDS",
    "INTERACT_AFFINITY",
    "INTERACT_COMMANDS",
    "INTERACT_KEYS",
    "QUEST_BY_KEY",
    "QUEST_KINDS",
    "after_grant",
    "apply_bonus",
    "bonus_limit",
    "bonus_note",
    "cached_points",
    "care_message",
    "clear_points_cache",
    "decay_amount",
    "dynamic_daily_cap",
    "ensure_quests",
    "has_unlock",
    "interact_refusal",
    "interact_reply",
    "last_active_days",
    "load_points",
    "next_unlocks",
    "nickname_for",
    "perks_for",
    "perks_public",
    "policy",
    "private_nudges",
    "quest_reply",
    "quests_view",
    "refresh_quests",
    "remember_points",
    "run_decay",
    "settle_quests",
    "speaker_view",
    "unlock_summary",
]
