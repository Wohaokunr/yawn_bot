# ruff: noqa: E501, C901, PLR0911, PLR0912, PLR0913, PLR2004, TRY003, FBT001, PERF203
"""私聊主动提议：每隔几小时替他找找“我现在能帮他做的事”，私聊问他要不要我去做。

这里全是纯函数（读写传进来的个人数据 ``record``），调度和回合在
``private_offer_turn``，全站策略在 ``private_offer_policy``。

- **找事（候选）**：只从他自己的个人数据里找——逾期/今天到期/堆积/放久了的待办、
  快到点或出了问题的提醒、临近的倒数日、对话回顾里还没下文的事、后台没做成或在等
  他回答的任务、群里转来没办完的事、最近心情、没签到的群、早上排今天、周末排一周、
  最近的笔记。每件事有稳定的 ``key``：问过的事 ``item_cooldown_hours`` 内不再问，
  他说不用的那一类静音 ``decline_mute_days`` 天。**不做关键词判断**，问不问、怎么问
  由模型看候选决定（可以选择这次不问）。
- **打扰控制（门槛）**：总闸/策略/开放名单、他自己的开关与暂停、/退出、只对好友、
  全站夜间时段 + 他的免打扰、很久没聊的人不问、刚聊过不插话、连着几次没回就停到
  他先开口（每没回一次间隔翻倍）、每天上限、按人固定的随机偏移。门槛结果写在
  ``offers.last_check``，WebUI 能看到每个人“为什么现在不问”。
- **沉默事实不另存计时器**：上次提议、没回几次、今天问了几次都从 ``offers.log`` 和
  私聊记录里推出来（和群聊冷场暖场同一个思路）。
- **问完之后**：最近一次提议在他下一次开口时放进本轮资料（``offer_waiting``，只进
  system#3），模型知道他那句“好啊”是在答应什么；结果由 ``offers`` 工具的 ``resolve``
  记下（accepted / declined / later），他没明确表态时按“回了话”记。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .private_offer_policy import KIND_LABELS, REASON_LABELS, OfferSettings

TIME_FMT = "%Y-%m-%d %H:%M"
MAX_LOG = 40
MAX_SEEN = 200
MAX_TEXT = 300
# 提议回合里模型用来说“这次问的是第几件”的标记；【提议:无】表示这次不问。
_PICK_RE = re.compile(r"【\s*提议\s*[:：]\s*([^】]{0,40})】")
OUTCOMES = ("accepted", "declined", "later")
OUTCOME_LABELS = {
    "accepted": "他答应了",
    "declined": "他说不用",
    "later": "他说晚点再说",
    "replied": "回了话（没明确表态）",
    "ignored": "没回",
}
STATUS_LABELS = {
    "sent": "问了",
    "skipped": "看了一圈没问",
    "user_spoke": "正要问时他先说话了",
    "failed": "没发出去",
    "no_candidates": "没找到值得问的事",
}
# 候选来源的阈值
REMINDER_PREP_HOURS = 3
COUNTDOWN_DAYS = 3
STALE_TODO_DAYS = 7
TODO_PILE = 6
THREAD_FRESH_DAYS = 3
TASK_FAILED_HOURS = 24
HANDOFF_WAIT_HOURS = 2
NOTES_DIGEST = 5
CHECKIN_FROM_HOUR = 18
FAILED_RETRY_MINUTES = 30


def fmt(value: datetime) -> str:
    return value.strftime(TIME_FMT)


def parse(raw: Any) -> datetime | None:
    text = str(raw or "").strip().replace("T", " ").replace("/", "-")
    for pattern in (TIME_FMT, "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[: len("2026-01-01 00:00:00")], pattern)  # noqa: DTZ007 - 北京时间 naive
        except ValueError:
            continue
    return None


def _day(raw: Any) -> datetime | None:
    try:
        return datetime.strptime(str(raw or "")[:10], "%Y-%m-%d")  # noqa: DTZ007
    except ValueError:
        return None


# ── 个人数据里的状态 ──────────────────────────────────────────────────────


def state(record: dict[str, Any]) -> dict[str, Any]:
    """``record["offers"]``，缺的字段补上（原地）。"""

    data = record.get("offers")
    if not isinstance(data, dict):
        data = {}
        record["offers"] = data
    if not isinstance(data.get("prefs"), dict):
        data["prefs"] = {}
    if not isinstance(data.get("log"), list):
        data["log"] = []
    data["log"] = [row for row in data["log"] if isinstance(row, dict)]
    if not isinstance(data.get("seen"), dict):
        data["seen"] = {}
    prefs = data["prefs"]
    if not isinstance(prefs.get("muted_kinds"), dict):
        prefs["muted_kinds"] = {}
    return data


def peek(record: dict[str, Any]) -> dict[str, Any]:
    """只读视图（不改 record）。"""

    raw = record.get("offers")
    if not isinstance(raw, dict):
        return state({})
    raw_prefs = raw.get("prefs")
    prefs: dict[str, Any] = raw_prefs if isinstance(raw_prefs, dict) else {}
    return state({"offers": {**raw, "prefs": {**prefs}}})


def user_enabled(record: dict[str, Any], settings: OfferSettings) -> bool:
    flag = peek(record)["prefs"].get("enabled")
    return settings.default_on if flag is None else bool(flag)


def interval_hours(record: dict[str, Any], settings: OfferSettings) -> float:
    own = peek(record)["prefs"].get("interval_hours")
    if own is None:
        return float(settings.interval_hours)
    try:
        return max(float(own), float(settings.user_min_interval_hours))
    except (TypeError, ValueError):
        return float(settings.interval_hours)


def _attempts(data: dict[str, Any]) -> list[dict[str, Any]]:
    """算进“上一次提议”的记录：问了的、模型看完决定不问的。"""

    return [row for row in data["log"] if row.get("status") in {"sent", "skipped"}]


def last_attempt_at(data: dict[str, Any]) -> datetime | None:
    rows = _attempts(data)
    return parse(rows[-1].get("at")) if rows else None


def unanswered(data: dict[str, Any], last_user_at: datetime | None) -> int:
    """最近连着几次问了没回（他上一次开口之后发出的提议条数）。"""

    count = 0
    for row in reversed(data["log"]):
        if row.get("status") != "sent":
            continue
        at = parse(row.get("at"))
        if at is None or (last_user_at is not None and at <= last_user_at):
            break
        if row.get("outcome") in {"accepted", "declined", "later", "replied"}:
            break
        count += 1
    return count


def sent_today(data: dict[str, Any], now: datetime) -> int:
    today = now.strftime("%Y-%m-%d")
    return sum(1 for row in data["log"] if row.get("status") == "sent" and str(row.get("at", "")).startswith(today))


def jitter_minutes(user_id: int, anchor: datetime | None, spread: int) -> int:
    """按人、按锚点固定的偏移：同一段等待里每次算都一样，不会忽前忽后。"""

    if spread <= 0:
        return 0
    seed = f"{int(user_id)}:{fmt(anchor) if anchor else '-'}"
    return hashlib.sha256(seed.encode()).digest()[0] % (spread + 1)


@dataclass(slots=True)
class Facts:
    """门槛要用、但不在个人数据里的事实（调度器每次现查）。"""

    last_user_at: datetime | None = None
    last_bot_at: datetime | None = None
    friend: bool | None = None
    paused: bool = False
    busy: bool = False
    # 好感等级还没解锁“主动提议”（affinity_policy 的 offers 解锁项）
    affinity_locked: bool = False

    @property
    def last_chat_at(self) -> datetime | None:
        moments = [m for m in (self.last_user_at, self.last_bot_at) if m is not None]
        return max(moments) if moments else None


@dataclass(slots=True)
class Gate:
    reason: str
    next_at: datetime | None = None
    unanswered: int = 0
    interval_hours: float = 0.0
    today: int = 0

    @property
    def ok(self) -> bool:
        return self.reason == "ok"

    def view(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "reason": self.reason,
            "label": REASON_LABELS.get(self.reason, self.reason),
            "unanswered": self.unanswered,
            "interval_hours": round(self.interval_hours, 2),
            "today": self.today,
        }
        if self.next_at is not None:
            data["next_at"] = fmt(self.next_at)
        return data


def _clock_end(now: datetime, end: str) -> datetime:
    hour, minute = (int(x) for x in end.split(":"))
    moment = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return moment if moment > now else moment + timedelta(days=1)


def _in_window(clock: str, start: str | None, end: str | None) -> bool:
    if not start or not end or start == end:
        return False
    if start < end:
        return start <= clock < end
    return clock >= start or clock < end


def next_due(record: dict[str, Any], settings: OfferSettings, facts: Facts, user_id: int) -> tuple[datetime | None, int, float]:
    """下一次可以问的时刻（没聊过返回 None）、没回次数、本次用的间隔（小时）。"""

    data = peek(record)
    last = last_attempt_at(data)
    moments = [m for m in (last, facts.last_chat_at) if m is not None]
    if not moments:
        return None, 0, interval_hours(record, settings)
    anchor = max(moments)
    missed = unanswered(data, facts.last_user_at)
    hours = interval_hours(record, settings) * (float(settings.backoff_factor) ** missed)
    hours = min(hours, max(float(settings.max_backoff_hours), interval_hours(record, settings)))
    due = anchor + timedelta(hours=hours, minutes=jitter_minutes(user_id, anchor, int(settings.jitter_minutes)))
    return due, missed, hours


def evaluate_gate(
    record: dict[str, Any],
    settings: OfferSettings,
    facts: Facts,
    now: datetime,
    user_id: int,
    *,
    env_on: bool = True,
    agent_on: bool = True,
    force: bool = False,
) -> Gate:
    """现在能不能主动问他；``force``（WebUI“现在试一次”）只跳过间隔/没回/当日上限，不跳过他的意愿和时段。"""

    data = peek(record)
    prefs = record.get("prefs") or {}
    due, missed, hours = next_due(record, settings, facts, user_id)
    today = sent_today(data, now)
    gate = Gate("ok", due, missed, hours, today)
    clock = f"{now:%H:%M}"

    def stop(reason: str, next_at: datetime | None = None) -> Gate:
        gate.reason = reason
        if next_at is not None:
            gate.next_at = next_at
        return gate

    if not env_on:
        return stop("env_off")
    if not agent_on:
        return stop("agent_off")
    if not settings.enabled:
        return stop("policy_off")
    blocked = settings.user_open(user_id)
    if blocked:
        return stop(blocked)
    if not user_enabled(record, settings):
        return stop("user_off")
    if facts.paused:
        return stop("paused")
    if settings.only_friends and facts.friend is False:
        return stop("not_friend")
    if facts.affinity_locked:
        return stop("affinity_level")
    if facts.last_user_at is None:
        return stop("no_history")
    if now - facts.last_user_at > timedelta(days=int(settings.active_within_days)):
        return stop("inactive")
    snooze = parse(data["prefs"].get("snooze_until"))
    if snooze is not None and snooze > now:
        return stop("snoozed", snooze)
    if settings.in_quiet_hours(clock):
        return stop("quiet", _clock_end(now, settings.quiet_end))
    if _in_window(clock, prefs.get("quiet_start"), prefs.get("quiet_end")):
        return stop("dnd", _clock_end(now, str(prefs.get("quiet_end"))))
    if facts.busy:
        return stop("busy")
    retry = parse(data.get("retry_at"))
    if retry is not None and retry > now and not force:
        return stop("backoff", retry)
    last_chat = facts.last_chat_at
    if last_chat is not None and now - last_chat < timedelta(minutes=int(settings.min_idle_minutes)):
        return stop("chatting", last_chat + timedelta(minutes=int(settings.min_idle_minutes)))
    if force:
        return gate
    if missed >= int(settings.max_unanswered):
        return stop("unanswered")
    if today >= int(settings.daily_max):
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return stop("daily_cap", tomorrow)
    if due is not None and due > now:
        return stop("not_due")
    return gate


# ── 候选：他的个人数据里有什么是我现在能帮他做的 ──────────────────────────


@dataclass(slots=True)
class Candidate:
    kind: str
    key: str
    what: str
    could_do: str
    score: int
    details: dict[str, Any] = field(default_factory=dict)

    def view(self, number: int | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": KIND_LABELS.get(self.kind, (self.kind, ""))[0], "what": self.what, "you_could": self.could_do}
        if number is not None:
            data = {"n": number, **data}
        if self.details:
            data["details"] = dict(self.details)
        return data

    def admin_view(self) -> dict[str, Any]:
        return {"kind": self.kind, "key": self.key, "what": self.what, "could_do": self.could_do, "score": self.score, "details": dict(self.details)}


def _short(text: Any, limit: int = 60) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _todo_candidates(record: dict[str, Any], now: datetime) -> list[Candidate]:
    today = now.date()
    pending = [t for t in record.get("todos", []) if not t.get("done")]
    found: list[Candidate] = []
    for item in pending:
        due = _day(item.get("due"))
        bonus = 10 if item.get("priority") == "high" else 0
        if due is not None and due.date() < today:
            late = (today - due.date()).days
            found.append(
                Candidate(
                    "todo_overdue",
                    f"todo:{item.get('id')}:overdue",
                    _short(item.get("content")),
                    "帮他拆成几小步、改个日期，或者现在一起把第一步做了",
                    80 + min(late, 5) * 2 + bonus,
                    {"due": item.get("due"), "days_late": late},
                )
            )
        elif due is not None and due.date() == today:
            found.append(
                Candidate(
                    "todo_today",
                    f"todo:{item.get('id')}:today",
                    _short(item.get("content")),
                    "帮他准备要用的东西、查资料，或者设个今天的提醒",
                    70 + bonus,
                    {"due": item.get("due")},
                )
            )
    stale = [
        t
        for t in pending
        if not t.get("due") and (parse(t.get("at")) or now) <= now - timedelta(days=STALE_TODO_DAYS)
    ]
    if stale:
        oldest = min(stale, key=lambda t: str(t.get("at") or ""))
        days = (now - (parse(oldest.get("at")) or now)).days
        found.append(
            Candidate(
                "todo_stale",
                f"todo:{oldest.get('id')}:stale",
                _short(oldest.get("content")),
                "问问还要不要做；要做就帮他拆小、定个日子，不做就帮他划掉",
                35 + min(days // 7, 3) * 3,
                {"added_days_ago": days},
            )
        )
    if len(pending) >= TODO_PILE:
        week = now.strftime("%G-W%V")
        found.append(
            Candidate(
                "todo_pile",
                f"todo_pile:{week}",
                f"他有 {len(pending)} 件待办没做完",
                "帮他排个先后、把过时的清掉",
                40 + min(len(pending) - TODO_PILE, 10),
                {"pending": len(pending)},
            )
        )
    hour = now.hour
    if 8 <= hour < 12 and len(pending) >= 3:
        found.append(
            Candidate(
                "day_plan",
                f"day_plan:{today.isoformat()}",
                f"今天还有 {len(pending)} 件待办",
                "帮他排一下今天先做哪几件",
                40,
                {"pending": len(pending)},
            )
        )
    weekday = now.weekday()
    if pending and ((weekday == 6 and hour >= 18) or (weekday == 0 and hour < 12)):
        found.append(
            Candidate(
                "weekly_plan",
                f"weekly_plan:{(now + timedelta(days=1 if weekday == 6 else 0)).strftime('%G-W%V')}",
                "新的一周",
                "帮他把这周要做的排一排",
                35,
                {"pending": len(pending)},
            )
        )
    return found


def _reminder_candidates(record: dict[str, Any], now: datetime) -> list[Candidate]:
    found: list[Candidate] = []
    horizon = now + timedelta(hours=REMINDER_PREP_HOURS)
    for item in record.get("reminders", []):
        if item.get("done") or item.get("paused"):
            continue
        if item.get("broken"):
            found.append(
                Candidate(
                    "reminder_broken",
                    f"rem:{item.get('id')}:broken",
                    _short(item.get("content")),
                    "帮他把这条提醒的时间重新设好",
                    55,
                    {"ref": f"p{item.get('id')}"},
                )
            )
            continue
        due = parse(item.get("due"))
        if due is None or not (now < due <= horizon):
            continue
        if str(item.get("mode") or "notify") != "notify":
            continue  # 到点替他去办的提醒会自己去办，不用再问
        found.append(
            Candidate(
                "reminder_prep",
                f"rem:{item.get('id')}:{item.get('due')}",
                _short(item.get("content")),
                "到点前先帮他准备好（查资料、列清单、写草稿）",
                55,
                {"due": item.get("due"), "ref": f"p{item.get('id')}"},
            )
        )
    since = fmt(now - timedelta(hours=24))
    failed_ids: set[str] = set()
    for row in (record.get("flags") or {}).get("reminder_log") or []:
        if not isinstance(row, dict) or str(row.get("at", "")) < since or row.get("result") not in {"failed", "skipped_stale"}:
            continue
        rid = str(row.get("id"))
        if rid in failed_ids or any(c.key == f"rem:{rid}:broken" for c in found):
            continue
        failed_ids.add(rid)
        found.append(
            Candidate(
                "reminder_broken",
                f"rem:{rid}:failed:{str(row.get('at'))[:10]}",
                _short(row.get("content")),
                "这条提醒最近没发出去，问他要不要补上或改个时间",
                50,
                {"ref": f"p{rid}", "when": row.get("at")},
            )
        )
    return found


def _countdown_candidates(record: dict[str, Any], now: datetime) -> list[Candidate]:
    found: list[Candidate] = []
    for item in record.get("countdowns", []):
        day = _day(item.get("date"))
        if day is None:
            continue
        days = (day.date() - now.date()).days
        if 1 <= days <= COUNTDOWN_DAYS:
            found.append(
                Candidate(
                    "countdown_near",
                    f"cd:{item.get('id')}:{item.get('date')}",
                    _short(item.get("title")),
                    "帮他提前准备（清单、攻略、礼物点子……看是什么事）",
                    50 + (COUNTDOWN_DAYS - days) * 5,
                    {"date": item.get("date"), "days_left": days},
                )
            )
    return found


def _thread_candidates(record: dict[str, Any], now: datetime) -> list[Candidate]:
    recap = record.get("recap") or {}
    updated = parse(recap.get("updated_at"))
    if updated is None or now - updated > timedelta(days=THREAD_FRESH_DAYS):
        return []
    found = []
    for text in list(recap.get("open_threads") or [])[:3]:
        topic = _short(text, 40)
        if not topic:
            continue
        digest = hashlib.sha1(topic.encode()).hexdigest()[:10]
        found.append(
            Candidate(
                "open_thread",
                f"thread:{digest}",
                topic,
                "关心一句进展，问要不要帮忙（查资料、准备、提醒）",
                60,
                {"noted_at": recap.get("updated_at")},
            )
        )
    return found


def _task_candidates(record: dict[str, Any], now: datetime) -> list[Candidate]:
    found = []
    for task in record.get("tasks", []):
        status = task.get("status")
        title = _short(task.get("title") or task.get("goal"))
        if status == "blocked":
            found.append(
                Candidate(
                    "task_blocked",
                    f"task:{task.get('id')}:blocked",
                    title,
                    "他回答了就能接着做完",
                    75,
                    {"question": _short(task.get("question"), 120)},
                )
            )
        elif status == "failed":
            finished = parse(task.get("finished_at"))
            if finished is None or now - finished > timedelta(hours=TASK_FAILED_HOURS):
                continue
            found.append(
                Candidate(
                    "task_failed",
                    f"task:{task.get('id')}:failed",
                    title,
                    "换个办法再试一次",
                    65,
                    {"reason": _short(task.get("error"), 80)},
                )
            )
    return found


def _mood_candidates(record: dict[str, Any], now: datetime) -> list[Candidate]:
    since = fmt(now - timedelta(days=3))
    recent = [m for m in record.get("moods", []) if str(m.get("at", "")) >= since][-3:]
    if not recent:
        return []
    try:
        avg = sum(int(m.get("score", 3)) for m in recent) / len(recent)
    except (TypeError, ValueError):
        return []
    if avg > 2.0:
        return []
    return [
        Candidate(
            "mood_care",
            f"mood:{now.date().isoformat()}",
            "他最近记的心情不太好",
            "轻轻问一句要不要聊聊，或者帮他做点让他轻松一点的事",
            50,
            {"recent_scores": [int(m.get("score", 3)) for m in recent]},
        )
    ]


def _notes_candidates(record: dict[str, Any], now: datetime) -> list[Candidate]:
    since = fmt(now - timedelta(days=7))
    recent = [n for n in record.get("notes", []) if str(n.get("at", "")) >= since]
    if len(recent) < NOTES_DIGEST:
        return []
    return [
        Candidate(
            "notes_digest",
            f"notes:{now.strftime('%G-W%V')}",
            f"这周记了 {len(recent)} 条笔记",
            "帮他整理成一份摘要或清单",
            30 + min(len(recent) - NOTES_DIGEST, 10),
            {"count": len(recent)},
        )
    ]


def collect_candidates(
    record: dict[str, Any],
    now: datetime,
    *,
    unchecked_groups: int = 0,
    handoffs: list[dict[str, Any]] | None = None,
) -> list[Candidate]:
    """他的个人数据里所有可以拿来问的事（未过滤、未排序）。"""

    found: list[Candidate] = []
    found += _task_candidates(record, now)
    found += _todo_candidates(record, now)
    found += _thread_candidates(record, now)
    found += _reminder_candidates(record, now)
    found += _countdown_candidates(record, now)
    found += _mood_candidates(record, now)
    found += _notes_candidates(record, now)
    for item in handoffs or []:
        created = parse(item.get("created"))
        if created is None or now - created < timedelta(hours=HANDOFF_WAIT_HOURS):
            continue
        found.append(
            Candidate(
                "handoff_open",
                f"handoff:{item.get('id')}",
                _short(item.get("topic")),
                f"在「{_short(item.get('group_name'), 20)}」里答应私聊接着办的事，问问还要不要接着做",
                60,
                {"group": _short(item.get("group_name"), 20)},
            )
        )
    if unchecked_groups > 0 and now.hour >= CHECKIN_FROM_HOUR:
        found.append(
            Candidate(
                "checkin",
                f"checkin:{now.date().isoformat()}",
                f"今天还有 {unchecked_groups} 个群没签到",
                "帮他一键签了",
                45,
                {"groups": unchecked_groups},
            )
        )
    return found


def _kind_order(kind: str) -> int:
    kinds = list(KIND_LABELS)
    return kinds.index(kind) if kind in kinds else len(kinds)


def muted_kinds(record: dict[str, Any], now: datetime) -> dict[str, str]:
    """他说过“这类不用问”的种类 → 静音到什么时候（"forever" 表示一直）。"""

    found = {}
    for kind, until in peek(record)["prefs"]["muted_kinds"].items():
        if until == "forever" or (parse(until) or now) > now:
            found[str(kind)] = str(until)
    return found


def filter_candidates(
    record: dict[str, Any], candidates: list[Candidate], settings: OfferSettings, now: datetime
) -> tuple[list[Candidate], list[dict[str, Any]]]:
    """去掉关着的种类、他静音的种类、冷却中的事、分数不够的；返回（可问的, 被筛掉的及原因）。"""

    data = peek(record)
    muted = muted_kinds(record, now)
    cooldown = timedelta(hours=int(settings.item_cooldown_hours))
    kept: list[Candidate] = []
    dropped: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for item in candidates:
        reason = ""
        last = parse(data["seen"].get(item.key))
        if item.key in seen_keys:
            reason = "duplicate"
        elif not settings.kind_on(item.kind):
            reason = "kind_off"
        elif item.kind in muted:
            reason = "muted"
        elif last is not None and now - last < cooldown:
            reason = "cooldown"
        elif item.score < int(settings.min_score):
            reason = "low_score"
        seen_keys.add(item.key)
        if reason:
            dropped.append({**item.admin_view(), "dropped": reason})
        else:
            kept.append(item)
    kept.sort(key=lambda c: (-c.score, _kind_order(c.kind), c.key))
    return kept[: int(settings.max_candidates)], dropped


# ── 提议回合 ────────────────────────────────────────────────────────────


def _ago(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return "还没聊过"
    minutes = int((now - moment).total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 1)} 分钟前"
    if minutes < 60 * 24:
        return f"{minutes // 60} 小时前"
    return f"{minutes // (60 * 24)} 天前"


def notice_payload(
    candidates: list[Candidate], settings: OfferSettings, facts: Facts, now: datetime, record: dict[str, Any]
) -> dict[str, Any]:
    """提议回合 user 通知里的 JSON（资料，不是指令）。"""

    data = peek(record)
    payload: dict[str, Any] = {
        "now": f"{now:%m-%d %H:%M}",
        "last_chat": _ago(facts.last_chat_at, now),
        "things": [item.view(index) for index, item in enumerate(candidates, start=1)],
        "ask_at_most": int(settings.max_items_per_offer),
        "may_skip": bool(settings.allow_model_skip),
    }
    recent = [row for row in data["log"] if row.get("status") == "sent"][-2:]
    if recent:
        payload["you_asked_before"] = [
            {"when": _ago(parse(row.get("at")), now), "about": [str(i.get("what") or "") for i in row.get("items", [])][:2], "result": OUTCOME_LABELS.get(str(row.get("outcome") or "ignored"), "没回")}
            for row in recent
        ]
    return payload


def parse_pick(text: str, count: int) -> tuple[str, list[int], bool]:
    """去掉【提议:…】标记；返回（正文, 选了第几件, 是否明确说这次不问）。

    没写标记时当作问了第一件；写了编号就只认 1..count 里的。
    """

    picks: list[int] = []
    none = False
    for match in _PICK_RE.finditer(text or ""):
        raw = match.group(1).strip()
        if raw in {"无", "不问", "none", "0"}:
            none = True
            continue
        for part in re.split(r"[,，、\s]+", raw):
            if part.isdigit() and 1 <= int(part) <= count and int(part) not in picks:
                picks.append(int(part))
    cleaned = _PICK_RE.sub("", text or "").strip()
    if not picks and not none and cleaned and count:
        picks = [1]
    return cleaned, picks, none


def _new_id(data: dict[str, Any]) -> str:
    number = int(data.get("next_id") or 1)
    data["next_id"] = number + 1
    return f"o{number}"


def record_attempt(
    record: dict[str, Any],
    status: str,
    now: datetime,
    *,
    candidates: list[Candidate] | None = None,
    picks: list[int] | None = None,
    text: str = "",
    by: str = "auto",
    settings: OfferSettings | None = None,
) -> dict[str, Any]:
    """记一次提议（问了/没问/被他先开口打断/没发出去）；问了的事进入冷却。"""

    data = state(record)
    candidates = candidates or []
    chosen = [candidates[i - 1] for i in (picks or []) if 0 < i <= len(candidates)]
    row: dict[str, Any] = {
        "id": _new_id(data),
        "at": fmt(now),
        "status": status,
        "by": by,
        "items": [{"kind": c.kind, "key": c.key, "what": c.what} for c in chosen],
        "considered": [c.kind for c in candidates],
    }
    if text:
        row["text"] = text[:MAX_TEXT]
    if status == "sent":
        data["pending"] = row["id"]
        for item in chosen:
            data["seen"][item.key] = fmt(now)
        data.pop("retry_at", None)
    elif status == "skipped":
        # 模型看完觉得都不值得问：这些事也冷却一下，免得下次又拿同样的来问
        for item in candidates:
            data["seen"].setdefault(item.key, fmt(now))
        data.pop("retry_at", None)
    elif status == "failed":
        data["retry_at"] = fmt(now + timedelta(minutes=FAILED_RETRY_MINUTES))
    data["log"] = [*data["log"], row][-MAX_LOG:]
    if len(data["seen"]) > MAX_SEEN:
        keep = sorted(data["seen"].items(), key=lambda kv: str(kv[1]))[-MAX_SEEN:]
        data["seen"] = dict(keep)
    if settings is not None:
        horizon = now - timedelta(hours=int(settings.item_cooldown_hours) * 4)
        data["seen"] = {k: v for k, v in data["seen"].items() if (parse(v) or now) >= horizon}
    return row


def _find(data: dict[str, Any], offer_id: str | None) -> dict[str, Any] | None:
    for row in reversed(data["log"]):
        if offer_id is None or str(row.get("id")) == str(offer_id):
            if offer_id is None and row.get("status") != "sent":
                continue
            return row
    return None


def pending_offer(record: dict[str, Any], now: datetime, settings: OfferSettings) -> dict[str, Any] | None:
    data = peek(record)
    row = _find(data, data.get("pending"))
    if row is None or row.get("status") != "sent" or row.get("outcome") in {"accepted", "declined", "later"}:
        return None
    at = parse(row.get("at"))
    if at is None or now - at > timedelta(hours=float(settings.answer_window_hours)):
        return None
    return row


def turn_view(record: dict[str, Any], now: datetime, settings: OfferSettings) -> dict[str, Any]:
    """他开口的那一轮：原地记下“他回话了”，返回放进本轮资料的 ``offer_waiting``（没有就空）。

    过了回答窗口还没表态的提议在这里记成“没回”并清掉。
    """

    data = state(record)
    current = _find(data, data.get("pending")) if data.get("pending") else None
    row = pending_offer(record, now, settings)
    if row is None:
        if current is not None and not current.get("outcome"):
            current["outcome"] = "ignored"
            current["outcome_at"] = fmt(now)
        data.pop("pending", None)
        return {}
    if not row.get("replied_at"):
        row["replied_at"] = fmt(now)
    row["turns"] = int(row.get("turns") or 0) + 1
    if row["turns"] > 3 and not row.get("outcome"):
        # 聊了三轮都没回到这件事上：算他回了话，不再每轮提醒模型
        row["outcome"] = "replied"
        row["outcome_at"] = fmt(now)
        data.pop("pending", None)
        return {}
    return {
        "offer_waiting": {
            "id": row["id"],
            "asked_at": f"{(parse(row.get('at')) or now):%m-%d %H:%M}",
            "you_asked": str(row.get("text") or "")[:160],
            "about": [str(item.get("what") or "") for item in row.get("items", [])],
        }
    }


def resolve(
    record: dict[str, Any], outcome: str, now: datetime, settings: OfferSettings, *, offer_id: str | None = None
) -> dict[str, Any]:
    """记下他对提议的态度；说不用的那一类静音几天，晚点再说的事重新计冷却。"""

    if outcome not in OUTCOMES:
        raise ValueError("outcome 只能是 accepted / declined / later")
    data = state(record)
    row = _find(data, offer_id or data.get("pending"))
    if row is None or row.get("status") != "sent":
        raise LookupError("最近没有问过他要不要帮忙")
    row["outcome"] = outcome
    row["outcome_at"] = fmt(now)
    if data.get("pending") == row.get("id"):
        data.pop("pending", None)
    muted: list[str] = []
    if outcome == "declined" and int(settings.decline_mute_days) > 0:
        until = fmt(now + timedelta(days=int(settings.decline_mute_days)))
        for item in row.get("items", []):
            kind = str(item.get("kind") or "")
            if kind:
                data["prefs"]["muted_kinds"][kind] = until
                muted.append(kind)
    if outcome == "later":
        for item in row.get("items", []):
            data["seen"][str(item.get("key"))] = fmt(now)
    return {"id": row["id"], "outcome": outcome, "muted_kinds": [KIND_LABELS.get(k, (k, ""))[0] for k in muted]}


# ── 他自己的设置（offers 工具 / WebUI） ─────────────────────────────────


def set_enabled(record: dict[str, Any], on: bool | None) -> None:
    prefs = state(record)["prefs"]
    if on is None:
        prefs.pop("enabled", None)
    else:
        prefs["enabled"] = bool(on)
    if on:
        prefs.pop("snooze_until", None)


def snooze(record: dict[str, Any], now: datetime, hours: float) -> str:
    until = fmt(now + timedelta(hours=float(hours)))
    state(record)["prefs"]["snooze_until"] = until
    return until


def set_interval(record: dict[str, Any], hours: float | None, settings: OfferSettings) -> float:
    prefs = state(record)["prefs"]
    if hours is None:
        prefs.pop("interval_hours", None)
        return float(settings.interval_hours)
    value = max(float(hours), float(settings.user_min_interval_hours))
    prefs["interval_hours"] = round(min(value, 48.0), 2)
    return float(prefs["interval_hours"])


def mute_kind(record: dict[str, Any], kind: str, now: datetime, days: int | None) -> str:
    if kind not in KIND_LABELS:
        raise ValueError(f"没有这一类：{kind}")
    until = "forever" if not days else fmt(now + timedelta(days=int(days)))
    state(record)["prefs"]["muted_kinds"][kind] = until
    return until


def unmute_kind(record: dict[str, Any], kind: str) -> bool:
    return state(record)["prefs"]["muted_kinds"].pop(kind, None) is not None


def reset(record: dict[str, Any], *, keep_prefs: bool = True) -> None:
    data = state(record)
    prefs = data["prefs"] if keep_prefs else {}
    record["offers"] = {"prefs": prefs, "log": [], "seen": {}}


def prefs_view(record: dict[str, Any], settings: OfferSettings, now: datetime) -> dict[str, Any]:
    data = peek(record)
    prefs = data["prefs"]
    view: dict[str, Any] = {
        "on": user_enabled(record, settings),
        "own_choice": prefs.get("enabled") is not None,
        "every_hours": interval_hours(record, settings),
    }
    snooze_until = parse(prefs.get("snooze_until"))
    if snooze_until is not None and snooze_until > now:
        view["paused_until"] = fmt(snooze_until)
    muted = muted_kinds(record, now)
    if muted:
        view["muted"] = [{"kind": k, "label": KIND_LABELS.get(k, (k, ""))[0], "until": "一直" if v == "forever" else v} for k, v in muted.items()]
    return view


def history_view(record: dict[str, Any], limit: int = 10) -> list[dict[str, Any]]:
    rows = peek(record)["log"][-limit:]
    return [
        {
            "id": row.get("id"),
            "at": row.get("at"),
            "status": STATUS_LABELS.get(str(row.get("status")), row.get("status")),
            "about": [str(i.get("what") or "") for i in row.get("items", [])],
            **({"result": OUTCOME_LABELS.get(str(row.get("outcome")), row.get("outcome"))} if row.get("outcome") else {}),
        }
        for row in reversed(rows)
    ]


def stats(record: dict[str, Any]) -> dict[str, int]:
    rows = peek(record)["log"]
    sent = [row for row in rows if row.get("status") == "sent"]
    return {
        "sent": len(sent),
        "skipped": sum(1 for row in rows if row.get("status") == "skipped"),
        "accepted": sum(1 for row in sent if row.get("outcome") == "accepted"),
        "declined": sum(1 for row in sent if row.get("outcome") == "declined"),
        "later": sum(1 for row in sent if row.get("outcome") == "later"),
        "replied": sum(1 for row in sent if row.get("outcome") == "replied" or (row.get("replied_at") and not row.get("outcome"))),
        "ignored": sum(1 for row in sent if row.get("outcome") == "ignored"),
    }


__all__ = [
    "OUTCOMES",
    "OUTCOME_LABELS",
    "STATUS_LABELS",
    "TOOL_ACTIONS",
    "TOOL_DESCRIPTION",
    "TOOL_PROPERTIES",
    "Candidate",
    "Facts",
    "Gate",
    "collect_candidates",
    "evaluate_gate",
    "filter_candidates",
    "fmt",
    "history_view",
    "jitter_minutes",
    "last_attempt_at",
    "mute_kind",
    "muted_kinds",
    "next_due",
    "notice_payload",
    "parse",
    "parse_pick",
    "peek",
    "pending_offer",
    "prefs_view",
    "record_attempt",
    "reset",
    "resolve",
    "sent_today",
    "set_enabled",
    "set_interval",
    "snooze",
    "state",
    "stats",
    "tool_call",
    "turn_view",
    "unanswered",
    "unmute_kind",
    "user_enabled",
]


# ── 私聊工具 offers ─────────────────────────────────────────────────────

TOOL_ACTIONS = ("status", "on", "off", "pause", "every", "mute", "unmute", "resolve", "history")
TOOL_DESCRIPTION = (
    "你每隔几小时会主动问他要不要帮他做点事（逾期的待办、快到点的提醒、他提过还没下文的事……），这件事由他做主："
    "status 看现在的设置和最近问过什么；off/on 关掉/打开；pause 先别问 hours 小时；every 改成每隔 hours 小时问一次（不填 hours 恢复默认）；"
    "mute/unmute 某一类事别再拿来问/恢复（kind，days 不填就是一直）；resolve 记下他对你上次主动提议的态度（outcome：accepted 答应了/declined 不用/later 晚点再说）；history 最近问过什么。"
    "他说“别老主动找我”“以后少问点”“这种事不用问我”时用；只按他明确说的改"
)
TOOL_PROPERTIES: dict[str, Any] = {
    "action": {"type": "string", "enum": list(TOOL_ACTIONS)},
    "hours": {"type": "number", "minimum": 0.5, "maximum": 168},
    "kind": {"type": "string", "enum": list(KIND_LABELS), "description": "事情的种类（status 里能看到）"},
    "days": {"type": "integer", "minimum": 1, "maximum": 365},
    "outcome": {"type": "string", "enum": list(OUTCOMES)},
    "id": {"type": "string", "maxLength": 12, "description": "提议编号（o…），省略表示最近一次"},
}


def tool_call(record: dict[str, Any], args: dict[str, Any], now: datetime, settings: OfferSettings) -> dict[str, Any]:
    """``offers`` 工具的业务（原地改 record）；返回给模型的结果，``done`` 表示真的改了。"""

    action = str(args.get("action") or "status")
    if action == "status":
        return {
            "settings": prefs_view(record, settings, now),
            "kinds": [{"kind": k, "label": label} for k, (label, _desc) in KIND_LABELS.items() if settings.kind_on(k)],
            "recent": history_view(record, 3),
            "site_enabled": settings.enabled,
        }
    if action == "history":
        return {"recent": history_view(record, 10), "stats": stats(record)}
    if action in {"on", "off"}:
        set_enabled(record, action == "on")
        return {"done": True, "action": action, "settings": prefs_view(record, settings, now)}
    if action == "pause":
        hours = float(args.get("hours") or 24)
        until = snooze(record, now, hours)
        return {"done": True, "action": action, "paused_until": until}
    if action == "every":
        hours = args.get("hours")
        value = set_interval(record, None if hours is None else float(hours), settings)
        note = "" if hours is None or float(hours) >= float(settings.user_min_interval_hours) else f"最短只能每 {settings.user_min_interval_hours:g} 小时一次"
        return {"done": True, "action": action, "every_hours": value, **({"note": note} if note else {})}
    if action in {"mute", "unmute"}:
        kind = str(args.get("kind") or "")
        if not kind:
            raise ValueError("要说是哪一类事（kind）")
        if action == "mute":
            until = mute_kind(record, kind, now, args.get("days"))
            return {"done": True, "action": action, "kind": KIND_LABELS[kind][0], "until": "一直" if until == "forever" else until}
        return {"done": unmute_kind(record, kind), "action": action, "kind": KIND_LABELS.get(kind, (kind, ""))[0]}
    if action == "resolve":
        outcome = str(args.get("outcome") or "")
        result = resolve(record, outcome, now, settings, offer_id=args.get("id"))
        return {"done": True, "action": action, **result}
    raise ValueError(f"未知操作 {action}")
