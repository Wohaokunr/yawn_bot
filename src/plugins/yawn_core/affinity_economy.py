# ruff: noqa: C901, E501, PLR0912, PLR0913, PLR0915, PLR2004, RUF001, SIM105, TRY003
"""好感玩法的“经济”：签到积分当钱包，送礼、商店兑换、道具使用、签到加成。

签到积分原来只是一个越涨越大的数，没有任何用处。现在：

- **钱包** :func:`wallet`：一个人在各群攒的签到积分合计；花的时候不指定群就从积分最多的
  群开始扣（:func:`spend`），扣多少记在个人数据里。
- **送礼** :func:`send_gift`：花积分送 Bot 礼物涨好感；每天能送几次随等级提高，每天有
  一样“今日心仪”效果翻倍；礼物好感有每日上限（策略 ``gift_affinity_daily_cap``）。
- **商店** :func:`buy_item`：用积分换道具，部分道具要到一定等级才能买，等级越高折扣越多。
- **道具** :func:`use_item`：补签卡续上断掉的连签、好感护盾挡住衰减、签到暴击卡让当天下一次
  签到积分翻倍、任务刷新券换一批今天的任务。
- **签到加成** :func:`checkin_bonus`：签到积分按等级多给（``checkin_points_pct``），暴击卡再翻倍。

全部函数**不提交事务**，调用方（命令、Agent 工具、WebUI）决定提交点；积分不足、等级
不够等业务错误抛 :class:`EconomyError`，带可以直接展示给用户的中文说明。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select

from . import affinity
from . import affinity_store as store
from .affinity import level_for
from .affinity_perks import load_points, remember_points
from .affinity_policy import AffinityPolicy, Gift, load_policy

# 一次最多送/买几个
MAX_COUNT = 10
# 补签卡能救回多少天内断掉的连签
REPAIR_WINDOW_DAYS = 7
SHIELD_DAYS = 14


class EconomyError(Exception):
    """业务错误：``code`` 给程序判断，``str(error)`` 直接给用户看。"""

    def __init__(self, message: str, *, code: str = "invalid") -> None:
        super().__init__(message)
        self.code = code


def _today(now: datetime) -> str:
    return now.date().isoformat()


def _enabled(pol: AffinityPolicy) -> None:
    if not pol.enabled:
        raise EconomyError("好感玩法现在被管理员关掉了", code="disabled")


def _count(raw: Any) -> int:
    try:
        value = int(raw or 1)
    except (TypeError, ValueError):
        value = 1
    if value < 1 or value > MAX_COUNT:
        raise EconomyError(f"数量要在 1~{MAX_COUNT} 之间", code="invalid")
    return value


# ── 钱包 ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Wallet:
    total: int
    groups: tuple[tuple[int, str | None, int], ...]  # (群号, 群名, 积分)

    def public_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "groups": [{"group_id": gid, "group_name": name or f"群 {gid}", "points": pts} for gid, name, pts in self.groups],
        }


async def wallet(session: Any, user_id: int) -> Wallet:
    from .data_models.bot_group import BotGroup
    from .data_models.checkin_user import CheckinUser

    rows = (
        await session.execute(
            select(CheckinUser.group_id, BotGroup.group_name, CheckinUser.points)
            .outerjoin(BotGroup, BotGroup.group_id == CheckinUser.group_id)
            .where(CheckinUser.user_id == int(user_id))
        )
    ).all()
    groups = tuple(sorted(((int(g), n, max(int(p or 0), 0)) for g, n, p in rows), key=lambda r: (-r[2], r[0])))
    return Wallet(sum(row[2] for row in groups), groups)


async def spend(session: Any, user_id: int, amount: int, *, group_id: int | None = None) -> list[tuple[int, int]]:
    """扣积分，返回 [(群号, 扣了多少)]；不够时抛 ``EconomyError(code="points")``。"""

    from .data_models.checkin_user import CheckinUser

    amount = int(amount)
    if amount <= 0:
        return []
    statement = select(CheckinUser).where(CheckinUser.user_id == int(user_id))
    if group_id is not None:
        statement = statement.where(CheckinUser.group_id == int(group_id))
    rows = list((await session.execute(statement)).scalars().all())
    total = sum(max(int(row.points or 0), 0) for row in rows)
    if total < amount:
        where = "这个群的" if group_id is not None else ""
        raise EconomyError(f"{where}签到积分不够：需要 {amount}，现在只有 {total}", code="points")
    rows.sort(key=lambda row: (-int(row.points or 0), int(row.group_id)))
    left = amount
    taken: list[tuple[int, int]] = []
    for row in rows:
        if left <= 0:
            break
        have = max(int(row.points or 0), 0)
        take = min(have, left)
        if take:
            row.points = have - take
            left -= take
            taken.append((int(row.group_id), take))
    await session.flush()
    return taken


def price_after_discount(price: int, discount_pct: int) -> int:
    if price <= 0:
        return 0
    return max(round(price * (100 - max(0, min(discount_pct, 90))) / 100), 1)


# ── 礼物 ────────────────────────────────────────────────────────────


def favorite_gift(now: datetime, pol: AffinityPolicy | None = None) -> Gift | None:
    """今天 Bot 最喜欢的礼物（按日期轮换，所有人同一个）。"""

    pol = pol or load_policy()
    # 只从人人都能送的礼物里挑（贵重礼物要等级，当“心仪”大多数人送不了）
    gifts = [gift for gift in pol.gifts if gift.enabled and pol.level_index(gift.min_level) == 0] or [gift for gift in pol.gifts if gift.enabled]
    if not gifts:
        return None
    digest = hashlib.sha256(f"favorite:{_today(now)}".encode()).digest()
    return gifts[digest[0] % len(gifts)]


def gifts_used_today(record: dict[str, Any], now: datetime) -> int:
    today = record.get("gifts_today") or {}
    return int(today.get("count") or 0) if today.get("day") == _today(now) else 0


@dataclass(frozen=True, slots=True)
class GiftResult:
    gift: str
    emoji: str
    count: int
    cost: int
    favorite: bool
    affinity_gain: int
    capped: bool
    level: str
    leveled_up_to: str | None
    left_today: int
    reaction: str
    quests: dict[str, Any] = field(default_factory=dict)
    # 解锁了“回礼”时，Bot 每周回赠的一件道具名
    return_gift: str | None = None

    def public_dict(self) -> dict[str, Any]:
        """给模型：不含好感数值，只有涨没涨、是不是心仪、升没升级。"""

        payload: dict[str, Any] = {
            "gift": self.gift,
            "count": self.count,
            "points_spent": self.cost,
            "favorite_today": self.favorite,
            "affinity_up": self.affinity_gain > 0,
            "level": self.level,
            "gifts_left_today": self.left_today,
            "suggested_reaction": self.reaction,
        }
        if self.capped:
            payload["daily_gift_affinity_cap_reached"] = True
        if self.leveled_up_to:
            payload["leveled_up_to"] = self.leveled_up_to
        if self.return_gift:
            payload["you_gave_back"] = self.return_gift
        return payload


_REACTIONS: dict[str, tuple[str, ...]] = {
    "low": ("谢谢你的{gift}～", "诶，送我{gift}吗？谢谢啦"),
    "mid": ("哇，{gift}！好开心～", "收到{gift}啦，谢谢你惦记我"),
    "high": ("又给我带{gift}了！你最好了～", "{gift}！你怎么知道我想要这个，嘿嘿"),
}


def gift_reaction(gift: Gift, level_index: int, *, favorite: bool, capped: bool) -> str:
    tier = "low" if level_index <= 1 else "mid" if level_index <= 3 else "high"
    text = _REACTIONS[tier][len(gift.name) % 2].format(gift=f"{gift.emoji}{gift.name}")
    if favorite:
        text += " 这正是我今天最想要的！"
    if capped:
        text += "（今天收到的礼物已经够多啦，心意领了～）"
    return text


async def send_gift(
    session: Any,
    user_id: int,
    gift_name: str,
    count: Any = 1,
    *,
    group_id: int | None = None,
    pay_group_id: int | None = None,
    now: datetime,
) -> GiftResult:
    pol = load_policy()
    _enabled(pol)
    gift = pol.gift(gift_name)
    if gift is None:
        names = "、".join(f"{g.emoji}{g.name}" for g in pol.gifts if g.enabled)
        raise EconomyError(f"没有这种礼物；可以送：{names}", code="not_found")
    amount = _count(count)
    points = await load_points(session, user_id)
    perks = pol.perks(level_for(points).name)
    if level_for(points).index < pol.level_index(gift.min_level):
        raise EconomyError(f"{gift.emoji}{gift.name}太贵重啦，等我们熟到「{gift.min_level}」再送吧", code="level")
    record = store.load(user_id)
    used = gifts_used_today(record, now)
    if perks.gift_daily <= 0 or used + amount > perks.gift_daily:
        raise EconomyError(
            f"今天已经送了 {used} 次礼物，「{perks.level}」每天最多送 {perks.gift_daily} 次",
            code="quota",
        )
    cost = price_after_discount(gift.price * amount, perks.shop_discount_pct)
    await spend(session, user_id, cost, group_id=pay_group_id)
    favorite = (fav := favorite_gift(now, pol)) is not None and fav.key == gift.key
    gain = gift.affinity * amount * (pol.favorite_multiplier if favorite else 1)
    from .affinity_bond import FOOD_GIFTS, maybe_return_gift, mood_for

    mood = mood_for(record, now) if pol.bot_mood_enabled else None
    # Bot 今天“有点嘴馋”：吃的每份多 1 点（仍受礼物好感日上限）
    snack_bonus = mood is not None and mood.key == "snacky" and gift.key in FOOD_GIFTS
    if snack_bonus:
        gain += amount
    change = await affinity.grant(
        session, user_id, "gift", gain, group_id=group_id, note=f"送了{gift.name}×{amount}" + ("（今日心仪）" if favorite else ""), now=now
    )
    after_points = change.after if change.available and change.applied else points
    remember_points(user_id, after_points)
    today = _today(now)
    from .affinity_perks import has_unlock

    can_return = has_unlock(after_points, "return_gift", pol)
    returned: list[str] = []

    def write(rec: dict[str, Any]) -> None:
        from .affinity_bond import bond

        if favorite:
            stats = bond(rec)["stats"]
            stats["favorite"] = int(stats.get("favorite", 0) or 0) + 1
        if can_return:
            key = maybe_return_gift(rec, user_id, now, pol)
            if key:
                returned.append(pol.item_name(key))
        current = rec.get("gifts_today") or {}
        if current.get("day") != today:
            current = {"day": today, "count": 0, "names": []}
        current["count"] = int(current.get("count") or 0) + amount
        names = list(current.get("names") or [])
        names.append(gift.name if amount == 1 else f"{gift.name}×{amount}")
        current["names"] = names[-10:]
        rec["gifts_today"] = current
        totals = rec.setdefault("gift_totals", {})
        totals[gift.key] = int(totals.get(gift.key, 0)) + amount
        store.push_limited(
            rec,
            "gift_log",
            {"key": gift.key, "name": gift.name, "count": amount, "cost": cost, "favorite": favorite, "affinity": change.applied, "group_id": group_id, "at": now.isoformat(timespec="seconds")},
            store.GIFT_LOG_LIMIT,
        )

    store.update(user_id, write)
    level_after = level_for(after_points)
    return GiftResult(
        gift=gift.name,
        emoji=gift.emoji,
        count=amount,
        cost=cost,
        favorite=favorite,
        affinity_gain=change.applied,
        capped=change.capped,
        level=level_after.name,
        leveled_up_to=level_after.name if change.leveled_up else None,
        left_today=max(perks.gift_daily - used - amount, 0),
        reaction=gift_reaction(gift, level_after.index, favorite=favorite, capped=change.capped and change.applied < gain)
        + ("（今天有点馋，这个正好～）" if snack_bonus else "")
        + (f" 这是给你的回礼：{returned[0]}，收好哦～" if returned else ""),
        return_gift=returned[0] if returned else None,
    )


# ── 商店 ────────────────────────────────────────────────────────────


def shop_view(points: int | None, record: dict[str, Any], pol: AffinityPolicy | None = None) -> list[dict[str, Any]]:
    pol = pol or load_policy()
    level = level_for(points)
    perks = pol.perks(level.name)
    names = list(pol.levels)
    rows = []
    for item in pol.shop:
        if not item.enabled:
            continue
        need_index = next((i for i, row in enumerate(names) if row.level == item.min_level), 0)
        rows.append(
            {
                "key": item.key,
                "name": item.name,
                "description": item.description,
                "price": item.price,
                "final_price": price_after_discount(item.price, perks.shop_discount_pct),
                "min_level": item.min_level,
                "unlocked": level.index >= need_index,
                "have": int((record.get("inventory") or {}).get(item.key, 0)),
                "max_hold": item.max_hold,
            }
        )
    return rows


async def buy_item(session: Any, user_id: int, item_name: str, count: Any = 1, *, pay_group_id: int | None = None, now: datetime) -> dict[str, Any]:
    pol = load_policy()
    _enabled(pol)
    item = pol.item(item_name)
    if item is None or not item.enabled:
        names = "、".join(row.name for row in pol.shop if row.enabled)
        raise EconomyError(f"商店里没有这个；现在有：{names}", code="not_found")
    amount = _count(count)
    points = await load_points(session, user_id)
    level = level_for(points)
    need_index = next((i for i, row in enumerate(pol.levels) if row.level == item.min_level), 0)
    if level.index < need_index:
        raise EconomyError(f"{item.name}要到「{item.min_level}」才能买，我们现在是「{level.name}」", code="level")
    record = store.load(user_id)
    have = int(record["inventory"].get(item.key, 0))
    if have + amount > item.max_hold:
        raise EconomyError(f"{item.name}最多存 {item.max_hold} 张，你已经有 {have} 张了", code="quota")
    perks = pol.perks(level.name)
    cost = price_after_discount(item.price * amount, perks.shop_discount_pct)
    await spend(session, user_id, cost, group_id=pay_group_id)

    def write(rec: dict[str, Any]) -> int:
        store.add_items(rec, {item.key: amount}, cap={item.key: item.max_hold})
        store.push_limited(rec, "item_log", {"key": item.key, "action": "buy", "count": amount, "cost": cost, "at": now.isoformat(timespec="seconds")}, store.HISTORY_LIMIT)
        return int(rec["inventory"].get(item.key, 0))

    now_have = store.update(user_id, write)
    return {"item": item.name, "key": item.key, "count": amount, "points_spent": cost, "have": now_have, "discount_pct": perks.shop_discount_pct}


def _item_description(pol: AffinityPolicy, key: str) -> str:
    found = pol.item(key)
    return found.description if found is not None else ""


def inventory_view(record: dict[str, Any], pol: AffinityPolicy | None = None, *, today: date | None = None) -> dict[str, Any]:
    pol = pol or load_policy()
    items = [
        {"key": key, "name": pol.item_name(key), "count": count, "description": _item_description(pol, key)}
        for key, count in sorted((record.get("inventory") or {}).items())
        if count > 0
    ]
    shield = str(record.get("shield_until") or "")
    active_shield = bool(shield and (today is None or shield >= today.isoformat()))
    return {
        "items": items,
        "shield_until": shield if active_shield else "",
        "lucky_today": bool(today is not None and record.get("lucky_day") == today.isoformat()),
    }


# ── 道具 ────────────────────────────────────────────────────────────


async def _repair_streak(session: Any, user_id: int, group_id: int | None, now: datetime) -> dict[str, Any]:
    from .checkin_service import checkin_today
    from .data_models.bot_group import BotGroup
    from .data_models.checkin_user import CheckinUser

    today = checkin_today(now)
    yesterday = today - timedelta(days=1)
    statement = select(CheckinUser, BotGroup.group_name).outerjoin(BotGroup, BotGroup.group_id == CheckinUser.group_id).where(CheckinUser.user_id == int(user_id))
    if group_id is not None:
        statement = statement.where(CheckinUser.group_id == int(group_id))
    candidates = []
    for summary, name in (await session.execute(statement)).all():
        last = summary.last_checkin_date
        if last is None or last >= yesterday or int(summary.streak_days or 0) <= 0:
            continue
        if (yesterday - last).days > REPAIR_WINDOW_DAYS:
            continue
        candidates.append((summary, name))
    if not candidates:
        raise EconomyError(f"没有能续上的连签（只能救回 {REPAIR_WINDOW_DAYS} 天内断掉的）", code="not_applicable")
    candidates.sort(key=lambda row: -int(row[0].streak_days or 0))
    summary, name = candidates[0]
    missed = (yesterday - summary.last_checkin_date).days
    summary.streak_days = int(summary.streak_days or 0) + missed
    summary.last_checkin_date = yesterday
    await session.flush()
    return {"group_id": int(summary.group_id), "group_name": name or f"群 {summary.group_id}", "days_restored": missed, "streak_days": int(summary.streak_days)}


async def use_item(session: Any, user_id: int, item_name: str, *, group_id: int | None = None, now: datetime) -> dict[str, Any]:
    pol = load_policy()
    _enabled(pol)
    item = pol.item(item_name)
    key = item.key if item is not None else str(item_name or "").strip()
    record = store.load(user_id)
    if int(record["inventory"].get(key, 0)) <= 0:
        name = pol.item_name(key)
        raise EconomyError(f"背包里没有{name}，可以去好感商店兑换", code="empty")
    today = now.date()
    detail: dict[str, Any] = {}
    if key == "repair_card":
        detail = await _repair_streak(session, user_id, group_id, now)
    elif key == "shield":
        start = today
        current = str(record.get("shield_until") or "")
        if current:
            try:
                start = max(start, date.fromisoformat(current))
            except ValueError:
                pass
        detail = {"shield_until": (start + timedelta(days=SHIELD_DAYS)).isoformat()}
    elif key == "lucky_card":
        if record.get("lucky_day") == today.isoformat():
            raise EconomyError("今天已经用过暴击卡了，先去签到吧", code="not_applicable")
        detail = {"lucky_day": today.isoformat()}
    elif key == "quest_refresh":
        if not (pol.quests.enabled):
            raise EconomyError("每日任务没开", code="disabled")
    elif key == "mood_candy":
        from .affinity_bond import bond

        if bond(record).get("mood_candy_day") == today.isoformat():
            raise EconomyError("今天已经吃过你的心情糖啦，我现在超开心的", code="not_applicable")
        detail = {"mood": "被你哄开心了"}
    elif key == "mystery_box":
        detail = {}
    else:
        raise EconomyError("这个道具现在用不了", code="not_applicable")

    def write(rec: dict[str, Any]) -> dict[str, Any]:
        if not store.take_item(rec, key):
            raise EconomyError("背包里没有了", code="empty")
        extra: dict[str, Any] = {}
        if key == "shield":
            rec["shield_until"] = detail["shield_until"]
        elif key == "lucky_card":
            rec["lucky_day"] = detail["lucky_day"]
        elif key == "quest_refresh":
            from .affinity_perks import refresh_quests

            fresh = refresh_quests(rec, user_id, now, pol)
            if not fresh:
                raise EconomyError("今天的任务都做完了，不用换", code="not_applicable")
            extra["new_quests"] = fresh
        elif key == "mood_candy":
            from .affinity_bond import bond

            bond(rec)["mood_candy_day"] = today.isoformat()
        elif key == "mystery_box":
            from .affinity_bond import open_mystery_box

            extra["box_item"] = pol.item_name(open_mystery_box(rec, user_id, now, pol))
        store.push_limited(rec, "item_log", {"key": key, "action": "use", "at": now.isoformat(timespec="seconds"), "note": str(detail.get("group_name") or "")}, store.HISTORY_LIMIT)
        extra["left"] = int(rec["inventory"].get(key, 0))
        return extra

    extra = store.update(user_id, write)
    if key == "quest_refresh":
        from .affinity_perks import QUEST_BY_KEY

        extra["new_quests"] = [QUEST_BY_KEY[k].title for k in extra.get("new_quests", []) if k in QUEST_BY_KEY]
    return {"item": pol.item_name(key), "key": key, **detail, **extra}


# ── 签到加成 ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CheckinBonus:
    level_points: int = 0
    lucky_points: int = 0
    level: str = ""

    @property
    def total(self) -> int:
        return self.level_points + self.lucky_points


async def checkin_bonus(session: Any, user_id: int, reward: int, *, now: datetime) -> CheckinBonus:
    """签到积分加成：等级百分比 + 暴击卡翻倍（用掉暴击卡）。任何失败都按无加成。"""

    try:
        pol = load_policy()
        if not pol.enabled or reward <= 0:
            return CheckinBonus()
        points = await load_points(session, user_id)
        level = level_for(points)
        level_points = max(int(pol.perks(level.name).checkin_points_pct), 0)
        level_bonus = max(round(reward * level_points / 100), 1) if level_points else 0
        lucky = 0
        today = now.date().isoformat()
        if store.exists(user_id) and store.load(user_id).get("lucky_day") == today:

            def consume(rec: dict[str, Any]) -> bool:
                if rec.get("lucky_day") == today:
                    rec["lucky_day"] = ""
                    return True
                return False

            if store.update(user_id, consume):
                lucky = reward
        return CheckinBonus(level_bonus, lucky, level.name)
    except Exception:  # noqa: BLE001
        return CheckinBonus()


__all__ = [
    "MAX_COUNT",
    "REPAIR_WINDOW_DAYS",
    "SHIELD_DAYS",
    "CheckinBonus",
    "EconomyError",
    "GiftResult",
    "Wallet",
    "buy_item",
    "checkin_bonus",
    "favorite_gift",
    "gift_reaction",
    "gifts_used_today",
    "inventory_view",
    "price_after_discount",
    "send_gift",
    "shop_view",
    "spend",
    "use_item",
    "wallet",
]
