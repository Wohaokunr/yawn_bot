# ruff: noqa: TID252, E501
"""好感玩法的 WebUI 接口：管理台「好感度」页（策略、统计、单人道具）与个人「好感与签到」页的玩法。

管理台：
- ``GET/PUT /affinity/policy``、``POST /affinity/policy/reset``：每级权益、礼物、商店、任务、衰减；
- ``GET /affinity/stats``：等级分布、近 30 天来源、排行、送礼/道具统计；
- ``POST /affinity/decay/run``：立刻结算一次衰减；
- ``GET /users/{id}/affinity/perks``、``POST /users/{id}/affinity/items``、
  ``DELETE /users/{id}/affinity/nickname``：看某人的权益/背包/任务，发道具，清称呼。

个人（``/me``）：
- ``GET /me/affinity/perks``：我的权益、下一级解锁、钱包、背包、任务、礼物与商店；
- ``POST /me/affinity/gift|buy|use|interact|fortune|quests/claim``、``PUT /me/affinity/nickname``。
- 3.0 起 ``perks`` 里多一份 ``bond``（头衔、认识天数、连续来往、心情、缘分、周任务、徽章、心动瞬间、来信）。

业务逻辑全在 ``affinity_perks`` / ``affinity_economy`` / ``affinity_commands``；这里只做
校验、错误码映射和提交。``EconomyError``：积分不够 409、次数/持有上限 429、等级不够 403、
找不到 404，其余 422。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, status
from nonebot_plugin_orm import get_session
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel
from sqlalchemy import func, select

from .. import affinity
from .. import affinity_bond as bond
from .. import affinity_economy as economy
from .. import affinity_perks as perks
from .. import affinity_store as store
from ..affinity_policy import (
    ITEM_EFFECTS,
    UNLOCKS,
    AffinityPolicy,
    load_policy,
    reset_policy,
    save_policy,
)
from ..checkin_service import CHECKIN_TIMEZONE
from ..data_models.affinity_event import AffinityEvent
from ..data_models.bot_user import BotUser
from .config import API_PATH
from .deps import (
    AdminReadSession,
    AdminWriteSession,
    UserReadSession,
    UserWriteSession,
    ok,
)
from .hub import hub
from .route_helpers import require_user

router = APIRouter(prefix=API_PATH)
me_router = APIRouter(prefix=f"{API_PATH}/me")

_ERROR_STATUS = {
    "points": status.HTTP_409_CONFLICT,
    "quota": status.HTTP_429_TOO_MANY_REQUESTS,
    "level": status.HTTP_403_FORBIDDEN,
    "disabled": status.HTTP_403_FORBIDDEN,
    "not_found": status.HTTP_404_NOT_FOUND,
    "empty": status.HTTP_409_CONFLICT,
}


def _now() -> datetime:
    return datetime.now(CHECKIN_TIMEZONE).replace(tzinfo=None)


def _economy_error(error: economy.EconomyError) -> HTTPException:
    return HTTPException(_ERROR_STATUS.get(error.code, status.HTTP_422_UNPROCESSABLE_ENTITY), str(error))


class _Body(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


# ── 通用投影 ────────────────────────────────────────────────────────


def policy_meta() -> dict[str, Any]:
    """前端编辑器需要的目录：等级下限、解锁项、道具效果、加成字段。"""

    return {
        "levels": [{"floor": floor, "name": name} for floor, name in affinity.LEVELS],
        "unlocks": [{"key": key, "label": label, "description": desc} for key, (label, desc) in UNLOCKS.items()],
        "items": [{"key": key, "description": desc} for key, desc in ITEM_EFFECTS.items()],
        "bonusFields": [{"key": key, "label": label} for key, label in perks.BONUS_FIELDS.items()],
        "quests": [{"key": kind.key, "title": kind.title, "target": kind.target} for kind in perks.QUEST_KINDS],
        **bond.bond_meta(),
    }


async def perks_payload(db: Any, user_id: int, *, now: datetime, admin: bool = False) -> dict[str, Any]:
    """一个人的好感玩法全貌（个人页与管理台用户详情共用）。"""

    pol = load_policy()
    points = await perks.load_points(db, user_id)
    level = affinity.level_for(points)
    wallet = await economy.wallet(db, user_id)

    user = await db.get(BotUser, user_id)
    first_seen = getattr(user, "first_interaction_at", None) if user is not None else None

    def read(rec: dict[str, Any]) -> dict[str, Any]:
        bond.backfill_first_day(rec, first_seen)
        bond.ensure_weekly(rec, user_id, now, pol)
        return perks.quests_view(rec, user_id, now, pol)

    quests = store.update(user_id, read)
    record = store.load(user_id)
    fav = economy.favorite_gift(now, pol)
    current = pol.perks(level.name)
    payload: dict[str, Any] = {
        "enabled": pol.enabled,
        "level": level.name,
        "levelIndex": level.index,
        "current": perks.perks_public(current),
        "next": perks.next_unlocks(points, pol),
        "ladder": [
            {"floor": floor, "name": name, **perks.perks_public(pol.perks(name))}
            for floor, name in affinity.LEVELS
        ],
        "wallet": wallet.public_dict(),
        "bag": economy.inventory_view(record, pol, today=now.date()),
        "quests": quests,
        "gifts": {
            "favorite": fav.key if fav else None,
            "sentToday": economy.gifts_used_today(record, now),
            "dailyLimit": current.gift_daily,
            "dailyAffinityCap": pol.gift_affinity_daily_cap,
            "favoriteMultiplier": pol.favorite_multiplier,
            "menu": [
                {
                    "key": gift.key,
                    "name": gift.name,
                    "emoji": gift.emoji,
                    "price": gift.price,
                    "finalPrice": economy.price_after_discount(gift.price, current.shop_discount_pct),
                    "affinity": gift.affinity,
                    "minLevel": gift.min_level,
                    "unlocked": level.index >= pol.level_index(gift.min_level),
                }
                for gift in pol.gifts
                if gift.enabled
            ],
            "totals": dict(record.get("gift_totals") or {}),
            "log": list(reversed(record.get("gift_log") or []))[:20],
        },
        "shop": economy.shop_view(points, record, pol),
        "nickname": str(record.get("nickname") or ""),
        "nicknameActive": bool(perks.nickname_for(record, points, pol)),
        "nicknameUnlockLevel": pol.unlock_level("nickname"),
        "interactions": [
            {
                "word": word,
                "key": key,
                "unlocked": perks.has_unlock(points, key, pol),
                "needLevel": pol.unlock_level(key),
                "count": int((record.get("interact_totals") or {}).get(key, 0)),
            }
            for word, key in perks.INTERACT_COMMANDS.items()
        ],
        "levelHistory": list(reversed(record.get("levels") or []))[:20],
        "itemLog": list(reversed(record.get("item_log") or []))[:20],
        "bond": _camel_bond(bond.bond_payload(user_id, points, record, now, pol)),
    }
    if admin:
        payload["points"] = points
        payload["lastPrivate"] = record.get("last_private") or None
    return payload


def _camel_bond(info: dict[str, Any]) -> dict[str, Any]:
    """羁绊视图转成前端习惯的驼峰键（嵌套的周任务/徽章保持后端字段名，前端类型里照写）。"""

    return {
        "title": info["title"],
        "speechStyle": info["speech_style"],
        "initiative": info["initiative"],
        "initiativeText": info["initiative_text"],
        "daysTogether": info["days_together"],
        "firstDay": info["first_day"],
        "activeDays": info["active_days"],
        "streak": info["streak"],
        "bestStreak": info["best_streak"],
        "nextStreakMilestone": info["next_streak_milestone"],
        "trend": info["trend"],
        "mood": info["mood"],
        "fortune": info["fortune"],
        "weekly": info["weekly"],
        "badges": info["badges"],
        "moments": info["moments"],
        "letters": info["letters"],
        "daily": info["daily"],
    }


# ── 管理台：策略 ────────────────────────────────────────────────────


@router.get("/affinity/policy")
async def get_policy(_session: AdminReadSession) -> dict[str, Any]:
    return ok({"policy": load_policy().model_dump(), "meta": policy_meta(), "defaults": AffinityPolicy().model_dump()})


@router.put("/affinity/policy")
async def put_policy(body: dict[str, Any], _session: AdminWriteSession) -> dict[str, Any]:
    try:
        saved = save_policy(body.get("policy", body))
    except ValidationError as error:
        first = error.errors()[0] if error.errors() else {}
        where = ".".join(str(part) for part in first.get("loc", ()))
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"策略不合法：{where} {first.get('msg', '')}".strip()) from error
    await hub.notify_change("affinity_policy", "global")
    return ok({"policy": saved.model_dump(), "meta": policy_meta()})


@router.post("/affinity/policy/reset")
async def post_policy_reset(_session: AdminWriteSession) -> dict[str, Any]:
    policy = reset_policy()
    await hub.notify_change("affinity_policy", "global")
    return ok({"policy": policy.model_dump(), "meta": policy_meta()})


# ── 管理台：统计 ────────────────────────────────────────────────────

STATS_STORE_SCAN = 3000


@router.get("/affinity/stats")
async def get_stats(_session: AdminReadSession) -> dict[str, Any]:  # noqa: C901, PLR0912, PLR0915
    now = _now()
    since = now.date() - timedelta(days=29)
    async with get_session() as db:
        rows = (await db.execute(select(BotUser.user_id, BotUser.nickname, BotUser.affinity))).all()
        distribution = {name: 0 for _floor, name in affinity.LEVELS}
        for _uid, _nick, points in rows:
            distribution[affinity.level_for(points).name] += 1
        top = sorted(rows, key=lambda row: -int(row[2] or 0))[:10]
        sources: dict[str, int] = {}
        daily: list[dict[str, Any]] = []
        available = not affinity.ledger_known_missing()
        if available:
            try:
                sources = {
                    str(source): int(total or 0)
                    for source, total in (
                        await db.execute(
                            select(AffinityEvent.source, func.sum(AffinityEvent.delta))
                            .where(AffinityEvent.day >= since)
                            .group_by(AffinityEvent.source)
                        )
                    ).all()
                }
                per_day = (
                    await db.execute(
                        select(AffinityEvent.day, func.sum(AffinityEvent.delta), func.count(func.distinct(AffinityEvent.user_id)))
                        .where(AffinityEvent.day >= since, AffinityEvent.delta > 0)
                        .group_by(AffinityEvent.day)
                        .order_by(AffinityEvent.day)
                    )
                ).all()
                daily = [{"day": str(day), "gained": int(total or 0), "users": int(users or 0)} for day, total, users in per_day]
            except Exception:  # noqa: BLE001
                available = False
    gift_totals: dict[str, int] = {}
    item_counts: dict[str, int] = {}
    nicknames = 0
    quests_done_today = 0
    shields = 0
    today = now.date().isoformat()
    badge_counts: dict[str, int] = {}
    moments_up = moments_down = 0
    streaks: list[tuple[int, int]] = []
    fortunes_today = 0
    letters_sent = 0
    week = bond.week_key(now.date())
    weekly_done = 0
    for uid in store.known_user_ids()[:STATS_STORE_SCAN]:
        record = store.load(uid)
        for key in (record.get("badges") or {}):
            badge_counts[key] = badge_counts.get(key, 0) + 1
        for row in record.get("moments") or []:
            if str(row.get("at") or "")[:10] >= since.isoformat():
                if row.get("kind") == "up":
                    moments_up += 1
                else:
                    moments_down += 1
        live = bond.live_streak(record, now.date())
        if live >= 2:  # noqa: PLR2004
            streaks.append((uid, live))
        if (record.get("fortune") or {}).get("day") == today:
            fortunes_today += 1
        letters_sent += sum(1 for row in record.get("letters") or [] if row.get("sent"))
        weekly = record.get("weekly") or {}
        if weekly.get("week") == week:
            weekly_done += sum(1 for item in weekly.get("items") or [] if item.get("done"))
        for key, count in (record.get("gift_totals") or {}).items():
            gift_totals[key] = gift_totals.get(key, 0) + int(count or 0)
        for key, count in (record.get("inventory") or {}).items():
            item_counts[key] = item_counts.get(key, 0) + int(count or 0)
        nicknames += 1 if record.get("nickname") else 0
        quests = record.get("quests") or {}
        if quests.get("day") == today:
            quests_done_today += sum(1 for item in quests.get("items") or [] if item.get("done"))
        if str(record.get("shield_until") or "") >= today:
            shields += 1
    pol = load_policy()
    names = {int(uid): nick for uid, nick, _points in rows}
    return ok(
        {
            "available": available,
            "enabled": pol.enabled,
            "users": len(rows),
            "withAffinity": sum(1 for row in rows if int(row[2] or 0) > 0),
            "distribution": [{"level": name, "count": distribution[name]} for _floor, name in affinity.LEVELS],
            "sources30d": [
                {"source": key, "label": affinity.SOURCE_RULES[key].label if key in affinity.SOURCE_RULES else key, "total": total}
                for key, total in sorted(sources.items(), key=lambda kv: -abs(kv[1]))
            ],
            "daily": daily,
            "top": [
                {"userId": str(uid), "nickname": nick, "level": affinity.level_for(points).name, "points": int(points or 0)}
                for uid, nick, points in top
                if int(points or 0) > 0
            ],
            "gifts": [
                {"key": key, "name": _gift_name(pol, key), "count": count}
                for key, count in sorted(gift_totals.items(), key=lambda kv: -kv[1])
            ],
            "items": [{"key": key, "name": pol.item_name(key), "count": count} for key, count in sorted(item_counts.items())],
            "nicknames": nicknames,
            "questsDoneToday": quests_done_today,
            "activeShields": shields,
            "favoriteToday": (fav.name if (fav := economy.favorite_gift(now, pol)) else None),
            "moodToday": bond.bot_mood(now).label,
            "bond": {
                "badges": [
                    {"key": b.key, "name": b.name, "emoji": b.emoji, "count": badge_counts.get(b.key, 0)}
                    for b in bond.BADGES
                ],
                "moments30d": {"up": moments_up, "down": moments_down},
                "streakTop": [{"userId": str(uid), "nickname": names.get(uid), "days": days} for uid, days in sorted(streaks, key=lambda kv: -kv[1])[:10]],
                "fortunesToday": fortunes_today,
                "lettersSent": letters_sent,
                "weeklyDoneThisWeek": weekly_done,
            },
        }
    )


def _gift_name(pol: AffinityPolicy, key: str) -> str:
    found = pol.gift(key)
    return found.name if found is not None else key


@router.post("/affinity/decay/run")
async def post_decay_run(_session: AdminWriteSession) -> dict[str, Any]:
    async with get_session() as db:
        result = await perks.run_decay(db, now=_now())
        await db.commit()
    for uid in result:
        await hub.notify_change("user_affinity", str(uid))
    return ok({"affected": len(result), "total": sum(result.values())})


# ── 管理台：单人 ────────────────────────────────────────────────────


class GrantItemBody(_Body):
    item: str = Field(min_length=1, max_length=24)
    count: int = Field(default=1, ge=1, le=20)
    note: str = Field(default="", max_length=60)


@router.get("/users/{user_id}/affinity/perks")
async def get_user_perks(user_id: int, _session: AdminReadSession) -> dict[str, Any]:
    async with get_session() as db:
        await require_user(db, user_id)
        payload = await perks_payload(db, user_id, now=_now(), admin=True)
    return ok(payload)


@router.post("/users/{user_id}/affinity/items")
async def post_user_items(user_id: int, body: GrantItemBody, _session: AdminWriteSession) -> dict[str, Any]:
    pol = load_policy()
    item = pol.item(body.item)
    key = item.key if item is not None else body.item
    if key not in ITEM_EFFECTS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "没有这种道具")
    now = _now()
    async with get_session() as db:
        await require_user(db, user_id)

        def give(rec: dict[str, Any]) -> None:
            store.add_items(rec, {key: body.count})
            store.push_limited(rec, "item_log", {"key": key, "action": "admin", "count": body.count, "at": now.isoformat(timespec="seconds"), "note": body.note}, store.HISTORY_LIMIT)

        store.update(user_id, give)
        payload = await perks_payload(db, user_id, now=now, admin=True)
    await hub.notify_change("user_affinity", str(user_id))
    return ok(payload)


@router.delete("/users/{user_id}/affinity/nickname")
async def delete_user_nickname(user_id: int, _session: AdminWriteSession) -> dict[str, Any]:
    async with get_session() as db:
        await require_user(db, user_id)
        store.update(user_id, lambda rec: rec.__setitem__("nickname", ""))
        payload = await perks_payload(db, user_id, now=_now(), admin=True)
    await hub.notify_change("user_affinity", str(user_id))
    return ok(payload)


# ── 个人页 ──────────────────────────────────────────────────────────


class GiftBody(_Body):
    gift: str = Field(min_length=1, max_length=24)
    count: int = Field(default=1, ge=1, le=economy.MAX_COUNT)
    group_id: str | None = Field(default=None, max_length=20)


class BuyBody(_Body):
    item: str = Field(min_length=1, max_length=24)
    count: int = Field(default=1, ge=1, le=economy.MAX_COUNT)


class UseBody(_Body):
    item: str = Field(min_length=1, max_length=24)
    group_id: str | None = Field(default=None, max_length=20)


class NicknameBody(_Body):
    nickname: str = Field(default="", max_length=24)


class InteractBody(_Body):
    action: str = Field(min_length=1, max_length=8)


def _uid(session: Any) -> int:
    return int(session.user_id)


def _gid(raw: str | None) -> int | None:
    if raw in (None, ""):
        return None
    try:
        return int(str(raw))
    except ValueError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "群号不对") from error


async def _member_group(db: Any, user_id: int, group_id: int | None) -> int | None:
    """个人页只能指定自己在的群。"""

    if group_id is None:
        return None
    from ..data_models.user_group import UserGroup

    if await db.get(UserGroup, (group_id, user_id)) is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "你不在这个群里")
    return group_id


async def _me_write(user_id: int, run: Any) -> dict[str, Any]:
    now = _now()
    async with get_session() as db:
        try:
            result = await run(db, now)
            settled = await perks.settle_quests(db, user_id, now=now)
            await db.commit()
        except economy.EconomyError as error:
            await db.rollback()
            raise _economy_error(error) from error
        payload = await perks_payload(db, user_id, now=now)
    await hub.notify_change("user_affinity", str(user_id))
    return ok({"result": result, "quests": settled, "state": payload})


@me_router.get("/affinity/perks")
async def my_perks(session: UserReadSession) -> dict[str, Any]:
    async with get_session() as db:
        payload = await perks_payload(db, _uid(session), now=_now())
    return ok(payload)


@me_router.post("/affinity/gift")
async def my_gift(body: GiftBody, session: UserWriteSession) -> dict[str, Any]:
    user_id = _uid(session)

    async def run(db: Any, now: datetime) -> dict[str, Any]:
        group_id = await _member_group(db, user_id, _gid(body.group_id))
        result = await economy.send_gift(db, user_id, body.gift, body.count, group_id=group_id, now=now)
        return {**result.public_dict(), "reaction": result.reaction, "levelUp": result.leveled_up_to}

    return await _me_write(user_id, run)


@me_router.post("/affinity/buy")
async def my_buy(body: BuyBody, session: UserWriteSession) -> dict[str, Any]:
    user_id = _uid(session)

    async def run(db: Any, now: datetime) -> dict[str, Any]:
        return await economy.buy_item(db, user_id, body.item, body.count, now=now)

    return await _me_write(user_id, run)


@me_router.post("/affinity/use")
async def my_use(body: UseBody, session: UserWriteSession) -> dict[str, Any]:
    user_id = _uid(session)

    async def run(db: Any, now: datetime) -> dict[str, Any]:
        from ..affinity_commands import format_use_result

        group_id = await _member_group(db, user_id, _gid(body.group_id))
        result = await economy.use_item(db, user_id, body.item, group_id=group_id, now=now)
        return {**result, "message": format_use_result(result)}

    return await _me_write(user_id, run)


@me_router.post("/affinity/interact")
async def my_interact(body: InteractBody, session: UserWriteSession) -> dict[str, Any]:
    user_id = _uid(session)

    async def run(db: Any, now: datetime) -> dict[str, Any]:
        from ..affinity_commands import do_interact

        user = await db.get(BotUser, user_id)
        done, text, extra = await do_interact(db, user_id, body.action, group_id=None, name=str(user.nickname or "") if user else "", now=now)
        return {"done": done, "message": text, **extra}

    return await _me_write(user_id, run)


@me_router.put("/affinity/nickname")
async def my_nickname(body: NicknameBody, session: UserWriteSession) -> dict[str, Any]:
    user_id = _uid(session)

    async def run(db: Any, _now: datetime) -> dict[str, Any]:
        from ..affinity_commands import set_nickname

        done, text = await set_nickname(db, user_id, body.nickname)
        if not done:
            raise economy.EconomyError(text, code="level")
        return {"done": True, "message": text}

    return await _me_write(user_id, run)


@me_router.post("/affinity/fortune")
async def my_fortune(session: UserWriteSession) -> dict[str, Any]:
    """抽今日缘分（每天第一次抽涨一点好感，再抽返回同一个结果）。"""

    user_id = _uid(session)

    async def run(db: Any, now: datetime) -> dict[str, Any]:
        try:
            result = await bond.draw_fortune(db, user_id, now=now)
        except bond.BondError as error:
            raise economy.EconomyError(str(error), code="disabled") from error
        return {**result, "message": bond.format_fortune(result)}

    return await _me_write(user_id, run)


@me_router.post("/affinity/quests/claim")
async def my_claim(session: UserWriteSession) -> dict[str, Any]:
    user_id = _uid(session)

    async def run(_db: Any, _now: datetime) -> dict[str, Any]:
        return {"done": True}

    return await _me_write(user_id, run)


__all__ = ["me_router", "perks_payload", "policy_meta", "router"]
