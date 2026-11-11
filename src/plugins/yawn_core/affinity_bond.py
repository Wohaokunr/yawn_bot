# ruff: noqa: ARG001, C901, E501, FBT003, PLR0912, PLR0913, PLR0915, PLR2004, RUF001, TRY003, TRY300
"""好感 3.0：“羁绊”——让关系有温度、有回忆、天天有事做，并把这些变成 Agent 说话的依据。

好感 2.0（:mod:`affinity_perks`）让每一级“有用”；这里让关系“有感觉”：

- **关系氛围**：认识第几天、连续几天来往、最近是升温还是降温、隔了几天才来、今天第一次来、
  快升级了、纪念日。全部由好感写入钩子里的结构事实（哪天有正向来往、涨了多少）推出来，
  不看聊天内容；
- **Bot 的心情**：每天一种（按日期固定，所有人一样），心情糖能让它对某个人“被哄开心”；
  心情会改语气提示，也有两处小玩法效果（嘴馋时送吃的多涨一点、心情超好时缘分更好）；
- **心动瞬间**：Agent 工具 ``affinity_moment`` / 私聊 ``affinity.moment`` 让模型自己记下
  “他让我很开心/有点受伤”的时刻，有界地涨跌好感（每天上下限、冷却、默认不掉级），
  原因写进回忆，之后 Bot 会自然提起；
- **玩法**：今日缘分（每天一签）、周任务、连续来往里程碑、相识纪念日、羁绊徽章、Bot 来信、
  回礼；
- **提示词视图** :func:`prompt_view`：分“稳定”（头衔、说话风格、主动性——只随等级变）和
  “本轮”（氛围、心情、缘分……每天都可能变）两份，私聊把后者放进本轮资料，不击穿前缀缓存。

个人数据仍在 :mod:`affinity_store` 的 JSON 里（新键 bond/moments/letters/weekly/fortune/badges），
不建表、不需要迁移；好感余额仍只经 :func:`affinity.grant` 写。所有对外函数**不提交事务**。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from . import affinity_store as store
from .affinity import level_for
from .affinity_policy import ITEM_EFFECTS, AffinityPolicy, load_policy

# 计入“来往”的来源：他主动做的事（奖励、衰减、管理员调整、心动瞬间都不算）。
ACTIVE_SOURCES = frozenset({"checkin", "private_chat", "group_chat", "gift", "interact", "fortune"})
# 每日涨幅记录保留多少天（够算两周的升温/降温）
DAILY_KEEP_DAYS = 35
MOMENT_LIMIT = 20
LETTER_LIMIT = 12
REASON_MAX = 40


class BondError(Exception):
    """业务错误：``code`` 给程序判断（disabled/quota/cooldown/invalid/level），文字直接给人看。"""

    def __init__(self, message: str, *, code: str = "invalid") -> None:
        super().__init__(message)
        self.code = code


def _day(now: datetime) -> str:
    return now.date().isoformat()


def week_key(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def _hash(text: str) -> bytes:
    return hashlib.sha256(text.encode()).digest()


def bond(record: dict[str, Any]) -> dict[str, Any]:
    """原地保证 ``record['bond']`` 有全部字段并返回它。"""

    data = record.get("bond")
    if not isinstance(data, dict):
        data = {}
        record["bond"] = data
    data.setdefault("first_day", "")
    data.setdefault("last_active", "")
    data.setdefault("active_days", 0)
    if not isinstance(data.get("daily"), dict):
        data["daily"] = {}
    streak = data.get("streak")
    if not isinstance(streak, dict):
        streak = {}
        data["streak"] = streak
    streak.setdefault("last", "")
    streak.setdefault("days", 0)
    streak.setdefault("best", 0)
    streak.setdefault("start", "")
    if not isinstance(streak.get("claimed"), list):
        streak["claimed"] = []
    if not isinstance(data.get("moment_today"), dict):
        data["moment_today"] = {}
    data.setdefault("mood_candy_day", "")
    data.setdefault("return_week", "")
    if not isinstance(data.get("anniversaries"), list):
        data["anniversaries"] = []
    if not isinstance(data.get("stats"), dict):
        data["stats"] = {}
    return data


def _stat(record: dict[str, Any], key: str, add: int = 1) -> int:
    stats = bond(record)["stats"]
    stats[key] = int(stats.get(key, 0) or 0) + add
    return stats[key]


# ── Bot 的心情 ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Mood:
    key: str
    label: str
    emoji: str
    hint: str


BOT_MOODS: tuple[Mood, ...] = (
    Mood("energetic", "元气满满", "☀️", "今天精神很好，说话可以更有活力"),
    Mood("sleepy", "有点犯困", "😪", "今天有点困，语气慵懒一点也行，但事情照样认真做"),
    Mood("happy", "心情超好", "😆", "今天心情很好，愿意多聊几句"),
    Mood("snacky", "有点嘴馋", "🍡", "今天有点馋，聊到吃的会来劲"),
    Mood("thoughtful", "若有所思", "🌙", "今天有点安静，适合认真聊聊天"),
    Mood("playful", "想恶作剧", "😜", "今天有点皮，可以多开点玩笑（照顾分寸）"),
    Mood("clingy", "想被陪", "🥺", "今天有点想要人陪，熟人来了会格外开心"),
)
COAXED = Mood("coaxed", "被你哄开心了", "🍬", "他今天给你吃了心情糖，你对他格外开心、格外耐心")
MOOD_BY_KEY = {mood.key: mood for mood in (*BOT_MOODS, COAXED)}
# “有点嘴馋”时这些礼物每份多 1 点好感
FOOD_GIFTS = frozenset({"candy", "milk_tea", "cake", "fish"})


def bot_mood(now: datetime) -> Mood:
    """今天 Bot 的心情：按日期固定，所有人看到的一样。"""

    return BOT_MOODS[_hash(f"mood:{_day(now)}")[0] % len(BOT_MOODS)]


def mood_for(record: dict[str, Any] | None, now: datetime) -> Mood:
    """对这个人的心情：今天吃过他的心情糖就是“被你哄开心了”。"""

    if record is not None and bond(record).get("mood_candy_day") == _day(now):
        return COAXED
    return bot_mood(now)


# ── 来往记录（grant 钩子） ───────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class WeeklyKind:
    key: str
    title: str
    target: int
    sources: frozenset[str]
    distinct_days: bool = False


WEEKLY_KINDS: tuple[WeeklyKind, ...] = (
    WeeklyKind("w_checkin", "本周签到 4 天", 4, frozenset({"checkin"}), True),
    WeeklyKind("w_active", "本周有 5 天来找我", 5, ACTIVE_SOURCES, True),
    WeeklyKind("w_private", "本周私聊聊 15 轮", 15, frozenset({"private_chat"})),
    WeeklyKind("w_group", "本周在群里找我 8 次", 8, frozenset({"group_chat"})),
    WeeklyKind("w_gift", "本周送我 3 次礼物", 3, frozenset({"gift"})),
    WeeklyKind("w_interact", "本周和我互动 6 次", 6, frozenset({"interact"})),
    WeeklyKind("w_fortune", "本周抽 3 次今日缘分", 3, frozenset({"fortune"}), True),
)
WEEKLY_BY_KEY = {kind.key: kind for kind in WEEKLY_KINDS}


def _pick_weekly(user_id: int, week: str, count: int) -> list[str]:
    digest = _hash(f"weekly:{user_id}:{week}")
    order = sorted(range(len(WEEKLY_KINDS)), key=lambda i: digest[i])
    picked: list[str] = []
    for index in order:
        key = WEEKLY_KINDS[index].key
        # “签到 4 天”和“来找我 5 天”太像，只放一个
        if key in {"w_checkin", "w_active"} and any(p in {"w_checkin", "w_active"} for p in picked):
            continue
        picked.append(key)
        if len(picked) >= count:
            break
    return picked


def ensure_weekly(record: dict[str, Any], user_id: int, now: datetime, pol: AffinityPolicy | None = None) -> dict[str, Any]:
    pol = pol or load_policy()
    week = week_key(now.date())
    weekly = record.get("weekly")
    if not isinstance(weekly, dict) or weekly.get("week") != week or not isinstance(weekly.get("items"), list):
        weekly = {
            "week": week,
            "items": [{"key": key, "progress": 0, "done": False, "claimed": False, "days": []} for key in _pick_weekly(user_id, week, pol.weekly.per_week)],
            "bonus_claimed": False,
        }
        record["weekly"] = weekly
    return weekly


def _advance_weekly(record: dict[str, Any], user_id: int, source: str, now: datetime, pol: AffinityPolicy) -> None:
    weekly = ensure_weekly(record, user_id, now, pol)
    today = _day(now)
    for item in weekly["items"]:
        kind = WEEKLY_BY_KEY.get(str(item.get("key")))
        if kind is None or item.get("done") or source not in kind.sources:
            continue
        if kind.distinct_days:
            days = [d for d in item.get("days") or [] if isinstance(d, str)]
            if today in days:
                continue
            days.append(today)
            item["days"] = days[-7:]
            item["progress"] = len(item["days"])
        else:
            item["progress"] = int(item.get("progress") or 0) + 1
        if item["progress"] >= kind.target:
            item["done"] = True


def weekly_view(record: dict[str, Any], user_id: int, now: datetime, pol: AffinityPolicy | None = None) -> dict[str, Any]:
    pol = pol or load_policy()
    weekly = ensure_weekly(record, user_id, now, pol)
    items = []
    for item in weekly["items"]:
        kind = WEEKLY_BY_KEY.get(str(item.get("key")))
        if kind is None:
            continue
        items.append({"key": kind.key, "title": kind.title, "progress": min(int(item.get("progress") or 0), kind.target), "target": kind.target, "done": bool(item.get("done")), "claimed": bool(item.get("claimed"))})
    monday = now.date() - timedelta(days=now.weekday())
    return {
        "enabled": pol.enabled and pol.weekly.enabled,
        "week": weekly["week"],
        "ends": (monday + timedelta(days=6)).isoformat(),
        "items": items,
        "reward_affinity": pol.weekly.reward_affinity,
        "all_done_item": pol.weekly.all_done_item,
        "all_done_item_name": pol.item_name(pol.weekly.all_done_item) if pol.weekly.all_done_item else "",
        "bonus_claimed": bool(weekly.get("bonus_claimed")),
        "all_done": bool(items) and all(row["done"] for row in items),
    }


def _touch_streak(data: dict[str, Any], today: date) -> None:
    streak = data["streak"]
    last = str(streak.get("last") or "")
    stamp = today.isoformat()
    if last == stamp:
        return
    if last == (today - timedelta(days=1)).isoformat():
        streak["days"] = int(streak.get("days") or 0) + 1
    else:
        streak["days"] = 1
        streak["start"] = stamp
        streak["claimed"] = []
    streak["last"] = stamp
    streak["best"] = max(int(streak.get("best") or 0), int(streak["days"]))


def on_grant(record: dict[str, Any], change: Any, *, user_id: int, source: str, now: datetime, pol: AffinityPolicy) -> None:
    """``affinity_perks.after_grant`` 在同一次 store.update 里调用：记涨幅、连续来往、周任务。"""

    data = bond(record)
    today = now.date()
    stamp = today.isoformat()
    if change.applied > 0 and source not in {"manual", "decay"}:
        daily = data["daily"]
        daily[stamp] = int(daily.get(stamp, 0) or 0) + int(change.applied)
        if len(daily) > DAILY_KEEP_DAYS:
            for key in sorted(daily)[: len(daily) - DAILY_KEEP_DAYS]:
                daily.pop(key, None)
    if source not in ACTIVE_SOURCES or change.requested <= 0:
        return
    if not data.get("first_day"):
        data["first_day"] = stamp
    if data.get("last_active") != stamp:
        data["active_days"] = int(data.get("active_days") or 0) + 1
        # 记下“这次之前上一次来往是哪天”，给“隔了几天才来”用
        data["prev_active"] = str(data.get("last_active") or "")
        data["last_active"] = stamp
    _touch_streak(data, today)
    if pol.weekly.enabled:
        _advance_weekly(record, user_id, source, now, pol)


def backfill_first_day(record: dict[str, Any], first: datetime | date | None) -> bool:
    """用 ``BotUser.first_interaction_at`` 补“认识的第一天”（只会往前补）。"""

    if first is None:
        return False
    day = first.date() if isinstance(first, datetime) else first
    data = bond(record)
    current = str(data.get("first_day") or "")
    if not current or day.isoformat() < current:
        data["first_day"] = day.isoformat()
        return True
    return False


# ── 氛围（纯函数） ─────────────────────────────────────────────────


def days_together(record: dict[str, Any], today: date) -> int | None:
    first = str(bond(record).get("first_day") or "")
    try:
        return (today - date.fromisoformat(first)).days + 1 if first else None
    except ValueError:
        return None


def live_streak(record: dict[str, Any], today: date) -> int:
    """还没断的连续来往天数（今天或昨天来过才算没断）。"""

    streak = bond(record)["streak"]
    last = str(streak.get("last") or "")
    if last in {today.isoformat(), (today - timedelta(days=1)).isoformat()}:
        return int(streak.get("days") or 0)
    return 0


def trend(record: dict[str, Any], today: date) -> str | None:
    """近 7 天比前 7 天：升温 / 平稳 / 降温；两周都没涨过返回 None。"""

    daily = bond(record)["daily"]

    def total(start: int, end: int) -> int:
        return sum(int(daily.get((today - timedelta(days=offset)).isoformat(), 0) or 0) for offset in range(start, end))

    recent, before = total(0, 7), total(7, 14)
    if recent <= 0 and before <= 0:
        return None
    if recent > before * 1.3 + 1:
        return "升温"
    if recent < before * 0.6:
        return "降温"
    return "平稳"


def gap_days(record: dict[str, Any], today: date) -> int | None:
    """这次来之前，上一次来往隔了几天（今天还没来过时按 last_active 算）。"""

    data = bond(record)
    last = str(data.get("last_active") or "")
    ref = str(data.get("prev_active") or "") if last == today.isoformat() else last
    if not ref:
        return None
    try:
        return (today - date.fromisoformat(ref)).days
    except ValueError:
        return None


def is_anniversary(record: dict[str, Any], today: date, pol: AffinityPolicy) -> int | None:
    together = days_together(record, today)
    if together is None:
        return None
    marks = {int(k) for k in pol.anniversary.items} | {100, 365}
    return together if together in marks else None


INITIATIVE_WORDS: dict[int, str] = {
    0: "别主动找他搭话，等他先开口",
    1: "可以接他的话，但别追着聊",
    2: "可以主动关心他的近况，分享你看到的有趣的事",
    3: "像老朋友一样，可以主动找他聊、惦记他的事",
}


def stable_view(points: int | None, pol: AffinityPolicy | None = None) -> dict[str, Any]:
    """只随等级变的部分：头衔、说话风格、主动性（私聊放长期资料，前缀缓存稳定）。"""

    pol = pol or load_policy()
    if not pol.enabled:
        return {}
    level = level_for(points)
    perks = pol.perks(level.name)
    out: dict[str, Any] = {}
    if perks.title:
        out["title"] = perks.title
    if perks.speech_style:
        out["speech_style"] = perks.speech_style
    out["initiative"] = INITIATIVE_WORDS.get(int(perks.initiative), INITIATIVE_WORDS[0])
    return out


def _fresh_moment(record: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    for row in reversed(record.get("moments") or []):
        try:
            at = datetime.fromisoformat(str(row.get("at")))
        except ValueError:
            continue
        if now - at <= timedelta(days=3):
            return row
        break
    return None


def volatile_view(user_id: int, points: int | None, record: dict[str, Any], now: datetime, pol: AffinityPolicy | None = None, *, limit: int | None = None) -> dict[str, Any]:
    """每天都可能变的部分（只有词语）：心情、氛围、缘分、纪念日、快升级、最近的心动瞬间。

    ``limit`` 给群聊用：发言人层有字符预算，按重要程度只留前几项。
    """

    pol = pol or load_policy()
    if not (pol.enabled and pol.prompt_bond_enabled):
        return {}
    today = now.date()
    level = level_for(points)
    rows: list[tuple[str, str]] = []
    anniversary = is_anniversary(record, today, pol)
    if anniversary:
        rows.append(("anniversary_today", f"今天是你们认识的第 {anniversary} 天，值得纪念"))
    gap = gap_days(record, today)
    data = bond(record)
    if gap is not None and gap >= 3 and data.get("last_active") != today.isoformat():
        rows.append(("missed_you", f"他隔了 {gap} 天才来找你，可以说想他了"))
    elif data.get("first_day") and data.get("last_active") != today.isoformat():
        rows.append(("first_time_today", "这是他今天第一次来找你"))
    streak = live_streak(record, today)
    if streak >= 2:
        rows.append(("streak_together", f"他已经连续 {streak} 天来找你"))
    fortune = record.get("fortune") or {}
    if fortune.get("day") == today.isoformat() and fortune.get("tier"):
        rows.append(("fortune_today", f"他今天抽到了「{fortune['tier']}」"))
    if level.next_name is not None:
        span = max(int(level.next_floor or 0) - level.floor, 1)
        left = level.to_next(int(points or 0)) or 0
        if 0 < left <= max(3, span // 10):
            rows.append(("almost_next_level", f"你们离「{level.next_name}」只差一点了"))
    moment = _fresh_moment(record, now)
    if moment and moment.get("reason"):
        word = "让你心动" if moment.get("kind") == "up" else "让你有点受伤"
        rows.append(("last_moment", f"最近他{word}的事：{moment['reason']}"))
    mood = mood_for(record, now) if pol.bot_mood_enabled else None
    if mood is not None:
        rows.append(("your_mood", f"{mood.label}（{mood.hint}）"))
    word = trend(record, today)
    if word in {"升温", "降温"}:
        rows.append(("trend", "你们最近越来越熟络" if word == "升温" else "你们最近来往变少了，别抱怨，自然地多关心他一点"))
    together = days_together(record, today)
    if together and together >= 2 and not anniversary:
        rows.append(("together", f"认识 {together} 天"))
    if limit is not None:
        rows = rows[:limit]
    return dict(rows)


# 群聊发言人层的字段：前缀 + 本轮最多几项（bot_bonds 有 900 字符硬预算）
GROUP_VOLATILE_LIMIT = 4
AFFINITY_PROMPT_KEYS = frozenset(
    {"title", "speech_style", "initiative", "anniversary_today", "missed_you", "first_time_today", "streak_together", "fortune_today", "almost_next_level", "last_moment", "your_mood", "trend", "together"}
)


def prompt_view(user_id: int, points: int | None, now: datetime, *, channel: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """(稳定, 本轮) 两份给 Agent 的字段；任何失败都返回空。"""

    try:
        pol = load_policy()
        stable = stable_view(points, pol)
        record = store.load(user_id) if store.exists(user_id) else store.empty_record()
        volatile = volatile_view(user_id, points, record, now, pol, limit=GROUP_VOLATILE_LIMIT if channel == "group" else None)
        return stable, volatile
    except Exception:  # noqa: BLE001
        return {}, {}


def offer_interval_factor(points: int | None, pol: AffinityPolicy | None = None) -> float:
    """私聊主动提议的间隔倍数：越熟、主动性越高，问得越勤（只在已解锁主动提议时有意义）。"""

    pol = pol or load_policy()
    if not pol.enabled:
        return 1.0
    initiative = int(pol.perks(level_for(points).name).initiative)
    return {0: 1.5, 1: 1.25, 2: 1.0, 3: 0.75}.get(initiative, 1.0)


# ── 心动瞬间 ───────────────────────────────────────────────────────

_BAD_REASON = re.compile(r"\[CQ:|@|https?://|www\.|\d{6,}|全体成员", re.IGNORECASE)


def clean_reason(raw: str) -> str | None:
    text = "".join(ch for ch in str(raw or "") if ch.isprintable()).strip()
    text = re.sub(r"\s+", " ", text)[:REASON_MAX]
    if len(text) < 2 or _BAD_REASON.search(text):
        return None
    return text


async def record_moment(
    session: Any,
    user_id: int,
    kind: str,
    reason: str,
    *,
    group_id: int | None = None,
    channel: str = "group",
    now: datetime,
) -> dict[str, Any]:
    """Bot 记一个心动瞬间（up）或小受伤（down）。业务错误抛 :class:`BondError`。"""

    from . import affinity
    from .affinity_perks import load_points

    pol = load_policy()
    rules = pol.moments
    if not (pol.enabled and rules.enabled):
        raise BondError("心动瞬间现在没开", code="disabled")
    if kind not in {"up", "down"}:
        raise BondError("kind 只能是 up 或 down", code="invalid")
    if kind == "down" and not rules.allow_down:
        raise BondError("管理员没开“小受伤”，不记掉好感的瞬间", code="disabled")
    text = clean_reason(reason)
    if text is None:
        raise BondError("原因要用一句普通的话写（不带 @、链接、号码），2~40 字", code="invalid")
    record = store.load(user_id)
    data = bond(record)
    today = _day(now)
    counter = data["moment_today"] if data["moment_today"].get("day") == today else {"day": today, "up": 0, "down": 0}
    last_at = str(data["moment_today"].get("last_at") or "")
    if last_at and rules.cooldown_minutes:
        try:
            if now - datetime.fromisoformat(last_at) < timedelta(minutes=rules.cooldown_minutes):
                raise BondError("刚记过一个瞬间，过一会儿再记", code="cooldown")
        except ValueError:
            pass
    points = await load_points(session, user_id)
    applied = 0
    if kind == "up":
        change = await affinity.grant(session, user_id, "moment", 1, group_id=group_id, note=f"心动：{text}", now=now)
        applied = change.applied
        level_after = change.level_after.name if change.available else level_for(points).name
    else:
        floor = level_for(points).floor if rules.keep_level else 0
        allowed = rules.daily_down - int(counter.get("down") or 0)
        if allowed > 0 and points - 1 >= floor:
            change = await affinity.grant(session, user_id, "moment", -1, group_id=group_id, note=f"受伤：{text}", now=now)
            applied = change.applied
        level_after = level_for(points + applied).name

    def write(rec: dict[str, Any]) -> None:
        d = bond(rec)
        current = d["moment_today"] if d["moment_today"].get("day") == today else {"day": today, "up": 0, "down": 0}
        current[kind] = int(current.get(kind) or 0) + (1 if applied else 0)
        current["last_at"] = now.isoformat(timespec="seconds")
        d["moment_today"] = current
        store.push_limited(rec, "moments", {"kind": kind, "reason": text, "at": now.isoformat(timespec="seconds"), "channel": channel, "changed": bool(applied)}, MOMENT_LIMIT)
        if kind == "up":
            _stat(rec, "moment_up")
        else:
            _stat(rec, "moment_down")

    store.update(user_id, write)
    return {"recorded": True, "kind": kind, "affinity_changed": applied != 0, "level": level_after}


# ── 今日缘分 ───────────────────────────────────────────────────────

FORTUNE_TIERS: tuple[str, ...] = ("大吉", "中吉", "小吉", "吉", "末吉")
_BASE_WEIGHTS = (8, 18, 26, 30, 18)
_YI = ("签到", "送一份小礼物", "和老朋友聊天", "早点睡", "出门走走", "听首新歌", "整理房间", "尝试新口味", "夸夸别人", "认真吃饭", "学点新东西", "晒太阳")
_JI = ("熬夜", "空腹喝咖啡", "和人抬杠", "拖延", "乱花钱", "一直刷手机", "忘带伞", "生闷气", "挑食", "说丧气话")
_ADVICE = {
    "大吉": ("今天做什么都顺，想做的事就去做吧！", "好运都在你这边，记得分一点给我～"),
    "中吉": ("运气不错，小小的愿望会实现哦。", "今天适合把搁置的事情推进一步。"),
    "小吉": ("平平稳稳也是一种幸运。", "留意身边的小惊喜，会有收获的。"),
    "吉": ("按自己的节奏来就好。", "今天适合照顾好自己。"),
    "末吉": ("稍微有点小波折，但我会陪着你的。", "慢一点没关系，傍晚会变好的。"),
}


def fortune_weights(level_index: int, *, happy: bool = False) -> list[int]:
    """越熟抽到好签的机会越大；Bot 心情超好时再好一点。"""

    weights: list[int] = list(_BASE_WEIGHTS)
    shift = max(0, int(level_index)) * 2 + (4 if happy else 0)
    weights[0] += shift
    weights[1] += shift
    weights[4] = max(weights[4] - shift, 4)
    weights[3] = max(weights[3] - shift, 8)
    return weights


def roll_fortune(user_id: int, now: datetime, level_index: int, pol: AffinityPolicy) -> dict[str, Any]:
    """纯函数：同一人同一天结果固定。"""

    happy = pol.bot_mood_enabled and bot_mood(now).key == "happy"
    weights = fortune_weights(level_index, happy=happy)
    digest = _hash(f"fortune:{user_id}:{_day(now)}")
    roll = int.from_bytes(digest[:4], "big") % sum(weights)
    tier = FORTUNE_TIERS[-1]
    for name, weight in zip(FORTUNE_TIERS, weights, strict=True):
        if roll < weight:
            tier = name
            break
        roll -= weight
    gifts = [g for g in pol.gifts if g.enabled]
    gift = gifts[digest[4] % len(gifts)] if gifts else None
    advice = _ADVICE[tier][digest[5] % len(_ADVICE[tier])]
    yi = _YI[digest[6] % len(_YI)]
    ji = _JI[digest[7] % len(_JI)]
    return {"day": _day(now), "tier": tier, "lucky_gift": f"{gift.emoji}{gift.name}" if gift else "", "yi": yi, "ji": ji, "advice": advice}


async def draw_fortune(session: Any, user_id: int, *, group_id: int | None = None, now: datetime) -> dict[str, Any]:
    """抽今日缘分：每天第一次抽给一点好感（大吉翻倍），再抽只是看结果。"""

    from . import affinity
    from .affinity_perks import load_points

    pol = load_policy()
    if not (pol.enabled and pol.fortune.enabled):
        raise BondError("今日缘分现在没开", code="disabled")
    record = store.load(user_id)
    current = record.get("fortune") or {}
    if current.get("day") == _day(now) and current.get("tier"):
        return {**current, "first_today": False, "affinity_up": False}
    points = await load_points(session, user_id)
    result = roll_fortune(user_id, now, level_for(points).index, pol)
    amount = pol.fortune.reward_affinity * (2 if result["tier"] == "大吉" else 1)
    changed = False
    if amount > 0:
        change = await affinity.grant(session, user_id, "fortune", amount, group_id=group_id, note=f"今日缘分：{result['tier']}", now=now)
        changed = change.applied > 0
    else:
        # 不给好感也算一次来往（连续来往、周任务）：直接走钩子
        from .affinity import AffinityChange
        from .affinity_perks import after_grant

        after_grant(AffinityChange("fortune", 1, 0, points, points), user_id=user_id, source="fortune", group_id=group_id, now=now)

    def write(rec: dict[str, Any]) -> None:
        rec["fortune"] = dict(result)
        _stat(rec, "fortune")
        if result["tier"] == "大吉":
            _stat(rec, "daikichi")

    store.update(user_id, write)
    return {**result, "first_today": True, "affinity_up": changed, "mood": mood_for(record, now).label}


def format_fortune(result: dict[str, Any], name: str = "") -> str:
    who = f"{name}的" if name else ""
    lines = [f"🔮 {who}今日缘分：{result['tier']}", result.get("advice", "")]
    lines.append(f"宜：{result.get('yi', '')}　忌：{result.get('ji', '')}")
    if result.get("lucky_gift"):
        lines.append(f"幸运礼物：{result['lucky_gift']}")
    if result.get("first_today") and result.get("affinity_up"):
        lines.append("（今天第一次抽，好感涨了一点）")
    elif not result.get("first_today"):
        lines.append("（今天已经抽过啦，明天再来）")
    return "\n".join(line for line in lines if line)


# ── 徽章 ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Badge:
    key: str
    name: str
    emoji: str
    description: str
    reward_item: str = ""


BADGES: tuple[Badge, ...] = (
    Badge("first_gift", "初次心意", "🎁", "第一次送 Bot 礼物"),
    Badge("gift_lover", "礼物达人", "🎀", "累计送出 30 份礼物", "mood_candy"),
    Badge("favorite_hit", "懂我心意", "💝", "送中今日心仪礼物 5 次", "lucky_card"),
    Badge("interact_20", "形影不离", "🤝", "互动 20 次"),
    Badge("interact_all", "五连互动", "🌈", "击掌、摸头、抱抱、贴贴、举高高都做过", "mood_candy"),
    Badge("streak_7", "一周不断", "🔥", "连续 7 天来找 Bot"),
    Badge("streak_30", "月度常客", "🌟", "连续 30 天来找 Bot", "shield"),
    Badge("streak_100", "百日相伴", "👑", "连续 100 天来找 Bot", "mystery_box"),
    Badge("fortune_7", "缘分常客", "🔮", "抽了 7 次今日缘分"),
    Badge("daikichi", "天选之人", "🍀", "抽到过大吉", "lucky_card"),
    Badge("moment_5", "心动收藏家", "💓", "被 Bot 记下 5 个心动瞬间"),
    Badge("weekly_1", "周常达人", "📅", "完成一整周的周任务"),
    Badge("nickname", "专属称呼", "🏷️", "让 Bot 用你定的称呼叫你"),
    Badge("letter", "收到来信", "💌", "收到 Bot 写的信"),
    Badge("friend", "成为朋友", "🫶", "和 Bot 的关系到了「朋友」"),
    Badge("best", "挚友", "💞", "和 Bot 的关系到了「挚友」", "mystery_box"),
    Badge("days_100", "相识百日", "🗓️", "认识 Bot 满 100 天"),
    Badge("active_50", "来往 50 天", "✨", "累计有 50 天来找过 Bot"),
)
BADGE_BY_KEY = {badge.key: badge for badge in BADGES}


def badge_progress(record: dict[str, Any], points: int | None, today: date) -> dict[str, tuple[int, int]]:
    """每个徽章的 (当前, 目标)；纯函数，WebUI 显示进度、颁发时判断是否达成。"""

    data = bond(record)
    stats = data["stats"]
    gifts = sum(int(v or 0) for v in (record.get("gift_totals") or {}).values())
    interacts = record.get("interact_totals") or {}
    best = int(data["streak"].get("best") or 0)
    level = level_for(points).index
    together = days_together(record, today) or 0
    return {
        "first_gift": (min(gifts, 1), 1),
        "gift_lover": (min(gifts, 30), 30),
        "favorite_hit": (min(int(stats.get("favorite", 0) or 0), 5), 5),
        "interact_20": (min(sum(int(v or 0) for v in interacts.values()), 20), 20),
        "interact_all": (sum(1 for k in ("interact.highfive", "interact.pat", "interact.hug", "interact.cuddle", "interact.lift") if int(interacts.get(k, 0) or 0) > 0), 5),
        "streak_7": (min(best, 7), 7),
        "streak_30": (min(best, 30), 30),
        "streak_100": (min(best, 100), 100),
        "fortune_7": (min(int(stats.get("fortune", 0) or 0), 7), 7),
        "daikichi": (min(int(stats.get("daikichi", 0) or 0), 1), 1),
        "moment_5": (min(int(stats.get("moment_up", 0) or 0), 5), 5),
        "weekly_1": (min(int(stats.get("weekly_done", 0) or 0), 1), 1),
        "nickname": (1 if str(record.get("nickname") or "").strip() else 0, 1),
        "letter": (min(int(stats.get("letters", 0) or 0), 1), 1),
        "friend": (min(level, 3), 3),
        "best": (min(level, 5), 5),
        "days_100": (min(together, 100), 100),
        "active_50": (min(int(data.get("active_days") or 0), 50), 50),
    }


def badges_view(record: dict[str, Any], points: int | None, today: date) -> list[dict[str, Any]]:
    earned = record.get("badges") or {}
    progress = badge_progress(record, points, today)
    rows = []
    for badge in BADGES:
        have, need = progress[badge.key]
        rows.append(
            {
                "key": badge.key,
                "name": badge.name,
                "emoji": badge.emoji,
                "description": badge.description,
                "earned": badge.key in earned,
                "earned_at": earned.get(badge.key) or None,
                "progress": have,
                "target": need,
                "reward_item": badge.reward_item,
            }
        )
    return rows


# ── Bot 来信 ───────────────────────────────────────────────────────

_LETTERS: dict[str, str] = {
    "朋友": "{name}：\n不知不觉我们已经是朋友了。谢谢你常来找我，{detail}以后也请多多指教呀。\n—— 你的 Bot",
    "好朋友": "{name}：\n写这封信的时候我有点不好意思……能和你变成好朋友真的很开心。{detail}有什么烦心事都可以跟我说。\n—— 一直在的 Bot",
    "挚友": "{name}：\n从认识到现在，{detail}你已经是我最重要的人之一了。往后的日子也一起走下去吧。\n—— 你的挚友 Bot",
}


def letter_text(level: str, name: str, together: int | None) -> str:
    template = _LETTERS.get(level) or "{name}：\n我们的关系升到了「" + level + "」，谢谢你一直以来的陪伴。{detail}\n—— Bot"
    detail = f"我们认识 {together} 天了，" if together and together > 1 else ""
    return template.format(name=name or "你", detail=detail)


def queue_letter(record: dict[str, Any], level: str, now: datetime) -> None:
    """升级时排一封信（由 :func:`affinity_perks.private_nudges` 在私聊发出）。"""

    letters = record.setdefault("letters", [])
    if any(row.get("level") == level for row in letters):
        return
    store.push_limited(record, "letters", {"level": level, "at": now.isoformat(timespec="seconds"), "sent": False, "text": ""}, LETTER_LIMIT)
    _stat(record, "letters")


def take_letters(record: dict[str, Any], *, name: str, now: datetime) -> list[str]:
    """取出还没寄出的信（原地标记已寄出）。"""

    texts = []
    together = days_together(record, now.date())
    for row in record.get("letters") or []:
        if row.get("sent"):
            continue
        text = letter_text(str(row.get("level") or ""), name, together)
        row["sent"] = True
        row["text"] = text
        row["sent_at"] = now.isoformat(timespec="seconds")
        texts.append(text)
    return texts


# ── 结算：连续来往、周任务、纪念日、徽章 ─────────────────────────────


async def settle_bond(session: Any, user_id: int, *, now: datetime) -> dict[str, Any]:
    """把达成的羁绊奖励发掉。由 :func:`affinity_perks.settle_quests` 在每个入口顺手调用。不提交事务。"""

    from . import affinity
    from .affinity_perks import load_points

    pol = load_policy()
    out: dict[str, Any] = {}
    if not pol.enabled:
        return out
    today = now.date()
    cap = {row.key: row.max_hold for row in pol.shop}
    grants: list[tuple[str, int, str]] = []
    items: dict[str, int] = {}
    points = await load_points(session, user_id)

    def take(rec: dict[str, Any]) -> None:
        data = bond(rec)
        # 连续来往里程碑
        if pol.streak.enabled:
            streak = data["streak"]
            days = live_streak(rec, today)
            claimed = [int(x) for x in streak.get("claimed") or [] if isinstance(x, int)]
            reached = [int(k) for k in pol.streak.milestones if int(k) <= days and int(k) not in claimed]
            if reached:
                top = max(reached)
                amount = sum(pol.streak.milestones[str(k)] for k in reached)
                streak["claimed"] = sorted({*claimed, *reached})
                if amount:
                    grants.append(("streak", amount, f"连续来往 {top} 天"))
                for k in reached:
                    item = pol.streak.milestone_items.get(str(k))
                    if item:
                        items[item] = items.get(item, 0) + 1
                out["streak"] = {"days": top, "affinity": amount}
        # 周任务
        if pol.weekly.enabled:
            weekly = ensure_weekly(rec, user_id, now, pol)
            done = []
            for item in weekly["items"]:
                if item.get("done") and not item.get("claimed"):
                    item["claimed"] = True
                    kind = WEEKLY_BY_KEY.get(str(item.get("key")))
                    if kind:
                        done.append(kind.title)
            if done:
                out["weekly"] = done
                if pol.weekly.reward_affinity:
                    grants.append(("weekly", pol.weekly.reward_affinity * len(done), f"周任务：{'、'.join(done)}"))
            if weekly["items"] and all(i.get("done") for i in weekly["items"]) and not weekly.get("bonus_claimed"):
                weekly["bonus_claimed"] = True
                _stat(rec, "weekly_done")
                if pol.weekly.all_done_item:
                    items[pol.weekly.all_done_item] = items.get(pol.weekly.all_done_item, 0) + 1
                    out["weekly_bonus_item"] = pol.item_name(pol.weekly.all_done_item)
        # 纪念日
        if pol.anniversary.enabled:
            together = days_together(rec, today)
            got = [int(x) for x in data.get("anniversaries") or [] if isinstance(x, int)]
            due = [int(k) for k in pol.anniversary.items if together is not None and int(k) <= together and int(k) not in got]
            if due:
                data["anniversaries"] = sorted({*got, *due})
                for k in due:
                    item = pol.anniversary.items[str(k)]
                    items[item] = items.get(item, 0) + 1
                out["anniversary"] = {"days": max(due), "item": pol.item_name(pol.anniversary.items[str(max(due))])}
        if items:
            added = store.add_items(rec, items, cap=cap)
            out["items"] = {pol.item_name(k): v for k, v in added.items()}

    store.update(user_id, take)
    for source, amount, note in grants:
        change = await affinity.grant(session, user_id, source, amount, note=note, now=now)
        out[f"{source}_affinity"] = out.get(f"{source}_affinity", 0) + change.applied
        if change.applied:
            points = change.after
    if pol.badges_enabled:
        new = award_badges(user_id, points, now, pol)
        if new:
            out["badges"] = new
    return out


def award_badges(user_id: int, points: int | None, now: datetime, pol: AffinityPolicy | None = None) -> list[str]:
    """颁发新达成的徽章（含奖励道具），返回新徽章名。"""

    pol = pol or load_policy()
    cap = {row.key: row.max_hold for row in pol.shop}
    names: list[str] = []

    def take(rec: dict[str, Any]) -> None:
        raw = rec.get("badges")
        earned: dict[str, str] = dict(raw) if isinstance(raw, dict) else {}
        progress = badge_progress(rec, points, now.date())
        for badge in BADGES:
            have, need = progress[badge.key]
            if badge.key in earned or have < need:
                continue
            earned[badge.key] = now.date().isoformat()
            names.append(f"{badge.emoji}{badge.name}")
            if badge.reward_item and badge.reward_item in ITEM_EFFECTS:
                store.add_items(rec, {badge.reward_item: 1}, cap=cap)
        rec["badges"] = earned

    store.update(user_id, take)
    return names


def settle_reply(settled: dict[str, Any]) -> str:
    """命令回复里追加的羁绊奖励说明；没东西时为空。"""

    parts: list[str] = []
    streak = settled.get("streak")
    if streak:
        parts.append(f"🔥 连续来往 {streak['days']} 天" + ("，好感涨了" if settled.get("streak_affinity") else ""))
    if settled.get("weekly"):
        parts.append(f"📅 完成周任务：{'、'.join(settled['weekly'])}")
    if settled.get("weekly_bonus_item"):
        parts.append(f"这周的任务全做完啦，送你一张{settled['weekly_bonus_item']}")
    anniversary = settled.get("anniversary")
    if anniversary:
        parts.append(f"🗓️ 我们认识满 {anniversary['days']} 天啦，送你{anniversary['item']}")
    if settled.get("badges"):
        parts.append(f"🏅 获得徽章：{'、'.join(settled['badges'])}")
    if settled.get("items") and not anniversary and not settled.get("weekly_bonus_item"):
        parts.append("获得道具：" + "、".join(f"{k}×{v}" for k, v in settled["items"].items()))
    return "；".join(parts)


# ── 回礼、心情糖、神秘礼盒（affinity_economy 调用） ───────────────────

RETURN_GIFT_POOL = ("lucky_card", "quest_refresh", "mood_candy", "repair_card")
MYSTERY_POOL = ("repair_card", "shield", "lucky_card", "quest_refresh", "mood_candy")


def maybe_return_gift(record: dict[str, Any], user_id: int, now: datetime, pol: AffinityPolicy) -> str | None:
    """解锁了“回礼”的人送礼时，每周回赠一件小道具（原地写入，返回道具 key）。"""

    data = bond(record)
    week = week_key(now.date())
    if data.get("return_week") == week:
        return None
    pool = [key for key in RETURN_GIFT_POOL if pol.item(key) is not None]
    if not pool:
        return None
    key = pool[_hash(f"return:{user_id}:{week}")[0] % len(pool)]
    cap = {row.key: row.max_hold for row in pol.shop}
    if not store.add_items(record, {key: 1}, cap=cap):
        return None
    data["return_week"] = week
    _stat(record, "return_gifts")
    return key


def open_mystery_box(record: dict[str, Any], user_id: int, now: datetime, pol: AffinityPolicy) -> str:
    opened = _stat(record, "boxes_opened")
    pool = [key for key in MYSTERY_POOL if pol.item(key) is not None] or ["lucky_card"]
    key = pool[_hash(f"box:{user_id}:{now:%Y%m%d%H%M%S}:{opened}")[0] % len(pool)]
    cap = {row.key: row.max_hold for row in pol.shop}
    if not store.add_items(record, {key: 1}, cap=cap):
        # 那种道具已经满了：换成第一种没满的
        for other in pool:
            if store.add_items(record, {other: 1}, cap=cap):
                return other
    return key


# ── 汇总视图（命令、工具、WebUI） ────────────────────────────────────


def bond_payload(user_id: int, points: int | None, record: dict[str, Any], now: datetime, pol: AffinityPolicy | None = None) -> dict[str, Any]:
    """一个人的羁绊全貌（不含好感数值）。"""

    pol = pol or load_policy()
    today = now.date()
    level = level_for(points)
    perks = pol.perks(level.name)
    data = bond(record)
    mood = mood_for(record, now)
    fortune = record.get("fortune") or {}
    return {
        "title": perks.title,
        "speech_style": perks.speech_style,
        "initiative": int(perks.initiative),
        "initiative_text": INITIATIVE_WORDS.get(int(perks.initiative), ""),
        "days_together": days_together(record, today),
        "first_day": data.get("first_day") or None,
        "active_days": int(data.get("active_days") or 0),
        "streak": live_streak(record, today),
        "best_streak": int(data["streak"].get("best") or 0),
        "next_streak_milestone": next((int(k) for k in pol.streak.milestones if int(k) > live_streak(record, today)), None) if pol.streak.enabled else None,
        "trend": trend(record, today),
        "mood": {"key": mood.key, "label": mood.label, "emoji": mood.emoji, "hint": mood.hint},
        "fortune": fortune if fortune.get("day") == today.isoformat() else None,
        "weekly": weekly_view(record, user_id, now, pol),
        "badges": badges_view(record, points, today) if pol.badges_enabled else [],
        "moments": list(reversed(record.get("moments") or []))[:10],
        "letters": [{"level": row.get("level"), "at": row.get("at"), "sent": bool(row.get("sent")), "text": row.get("text") or ""} for row in reversed(record.get("letters") or [])],
        "daily": [{"day": (today - timedelta(days=offset)).isoformat(), "gain": int(data["daily"].get((today - timedelta(days=offset)).isoformat(), 0) or 0)} for offset in range(13, -1, -1)],
    }


def bond_meta() -> dict[str, Any]:
    """WebUI 编辑器目录：徽章、周任务种类、心情、签级。"""

    return {
        "badges": [{"key": b.key, "name": b.name, "emoji": b.emoji, "description": b.description, "rewardItem": b.reward_item} for b in BADGES],
        "weekly": [{"key": k.key, "title": k.title, "target": k.target} for k in WEEKLY_KINDS],
        "moods": [{"key": m.key, "label": m.label, "emoji": m.emoji, "hint": m.hint} for m in BOT_MOODS],
        "fortuneTiers": list(FORTUNE_TIERS),
        "initiative": [{"value": k, "text": v} for k, v in INITIATIVE_WORDS.items()],
    }


__all__ = [
    "ACTIVE_SOURCES",
    "AFFINITY_PROMPT_KEYS",
    "BADGES",
    "BADGE_BY_KEY",
    "BOT_MOODS",
    "COAXED",
    "FOOD_GIFTS",
    "FORTUNE_TIERS",
    "GROUP_VOLATILE_LIMIT",
    "INITIATIVE_WORDS",
    "WEEKLY_KINDS",
    "BondError",
    "award_badges",
    "backfill_first_day",
    "badge_progress",
    "badges_view",
    "bond",
    "bond_meta",
    "bond_payload",
    "bot_mood",
    "clean_reason",
    "days_together",
    "draw_fortune",
    "ensure_weekly",
    "format_fortune",
    "fortune_weights",
    "gap_days",
    "is_anniversary",
    "letter_text",
    "live_streak",
    "maybe_return_gift",
    "mood_for",
    "offer_interval_factor",
    "on_grant",
    "open_mystery_box",
    "prompt_view",
    "queue_letter",
    "record_moment",
    "roll_fortune",
    "settle_bond",
    "settle_reply",
    "stable_view",
    "take_letters",
    "trend",
    "volatile_view",
    "week_key",
    "weekly_view",
]
