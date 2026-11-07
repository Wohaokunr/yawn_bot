# ruff: noqa: E501, C901, TID252, RUF001, FBT001, PLR0913, PLR0917
"""私聊 Agent 的后台调度：到点提醒、早安问候、签到提醒、生日祝福、倒数日当天提醒。

每 20 秒扫一遍有个人数据的用户（``private_store``）。规则：

- 用户自己设的提醒照常送达（他明确要的），不受免打扰和 ``proactive`` 偏好影响；
  Bot 停机错过的提醒补发并注明晚了多久，晚超过 ``STALE_AFTER`` 的一次性提醒直接丢弃。
  规则与到点推进在 ``private_schedule``（和群定时任务同一个规则引擎）；发送失败按
  2/5/15… 分钟退避重试（策略 ``private_retry_limit``），不再直接丢掉。
- agent 模式的提醒（“到点替我去办”）唤醒一轮私聊回合（``private_schedule_turn``），
  没办成或他 /退出 暂停了就改发提醒原文。
- 早安/签到提醒/生日/倒数日属于主动消息：用户关了 ``proactive``、暂停了自动回复、
  或正在免打扰时都不发；每天最多各一次（标记写在个人数据 ``flags`` 里，先记后发，不会重复）。
- 全局开关 ``PRIVATE_AGENT_PROACTIVE`` 关掉时整个调度都不发。
- 发出的消息记进这位用户最近的私聊会话（assistant），下一轮 Agent 看得到自己说过什么。
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from nonebot import get_bots, get_driver, logger
from nonebot_plugin_apscheduler import scheduler
from sqlalchemy import select

from ..checkin_service import checkin_overview
from ..data_models.chat_message import ChatMessage
from ..data_models.chat_session import ChatSession
from ..private_chat_bridge import auto_paused, private_agent_config
from . import private_schedule as ps
from . import private_store as store
from .context import now_beijing
from .log import dbg, dbg_exc
from .private_organizer import in_quiet_hours

JOB_ID = "yawn_private_agent_tick"
TICK_SECONDS = 20
# 错过超过这么久的一次性提醒不再补发。
STALE_AFTER = ps.STALE_AFTER
# 早安/签到提醒在设定时刻之后多久内还算数（Bot 刚重启时补一次）。
GREETING_WINDOW = timedelta(hours=2)
# 生日/倒数日当天几点以后再发。
DAY_EVENT_HOUR = 9

Sender = Callable[[int, str], Awaitable[bool]]
CheckinProbe = Callable[[int, datetime], Awaitable[int]]
# agent 模式的私聊提醒：唤醒一轮私聊回合，返回 done/fallback/failed。
Waker = Callable[[int, "ps.Firing"], Awaitable[str]]

_MORNING = (
    "早安{name}～新的一天也要元气满满哦",
    "{name}早上好！记得吃早饭～",
    "早呀{name}，今天也一起加油吧",
    "{name}，起床啦～今天天气怎么样？",
)


def _pick(options: tuple[str, ...], user_id: int, day: str) -> str:
    digest = hashlib.sha256(f"{user_id}:{day}".encode()).digest()
    return options[digest[0] % len(options)]


def _name(prefs: dict[str, Any]) -> str:
    return str(prefs.get("call_me") or "")


def _clock_reached(clock: str | None, now: datetime) -> bool:
    if not clock:
        return False
    hour, minute = (int(x) for x in clock.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return target <= now < target + GREETING_WINDOW


def _voice(record: dict[str, Any], text: str) -> str:
    """主动问候带上他私聊人设的自称和口癖（提醒内容是他自己写的，不改）。"""

    try:
        from .private_persona import personalize_greeting

        return personalize_greeting(record, text)
    except Exception:  # noqa: BLE001
        return text


def plan_user(record: dict[str, Any], now: datetime, *, proactive_ok: bool) -> tuple[list[str], list[str]]:
    """原地更新 ``record``，返回 (要发的主动消息, 待定的主动消息种类)。

    用户自己设的提醒不在这里：由 :func:`private_schedule.collect_due` 推进、
    :func:`run_tick` 执行并写回结果（失败可以重试）。
    主动消息里签到提醒还要查库，所以只返回种类，由调用方决定内容；
    其余（早安、生日、倒数日）直接给出文本。标记在这里就写好，保证不重复。
    """

    messages: list[str] = []
    pending: list[str] = []
    prefs = record.get("prefs") or {}
    if not proactive_ok or prefs.get("proactive") is False or in_quiet_hours(prefs, now):
        return messages, pending
    flags = record.setdefault("flags", {})
    today = now.date().isoformat()
    name = _name(prefs)
    if _clock_reached(prefs.get("morning_greeting"), now) and flags.get("greet_day") != today:
        flags["greet_day"] = today
        messages.append(
            _voice(record, _pick(_MORNING, int(record.get("user_id") or 0), today).format(name=name))
        )
    if _clock_reached(prefs.get("checkin_nudge"), now) and flags.get("nudge_day") != today:
        flags["nudge_day"] = today
        pending.append("checkin_nudge")
    if now.hour >= DAY_EVENT_HOUR:
        if prefs.get("birthday") == f"{now:%m-%d}" and flags.get("birthday_year") != now.year:
            flags["birthday_year"] = now.year
            messages.append(
                _voice(record, f"🎂 {name or ''}生日快乐！今天是属于你的日子，愿新的一岁开开心心～".replace("  ", " "))
            )
        sent_days = flags.setdefault("countdown_sent", {})
        for item in record.get("countdowns", []):
            if not item.get("notify", True) or item.get("date") != today:
                continue
            key = str(item.get("id"))
            if sent_days.get(key) == today:
                continue
            sent_days[key] = today
            messages.append(_voice(record, f"📅 今天就是「{item['title']}」啦！"))
        # 只留今天的标记，避免越积越多
        flags["countdown_sent"] = {k: v for k, v in sent_days.items() if v == today}
    return messages, pending


async def _unchecked_groups(user_id: int, now: datetime) -> int:
    from nonebot_plugin_orm import get_session

    async with get_session() as session:
        statuses = await checkin_overview(session, user_id, now=now)
    return sum(1 for item in statuses if not item.checked_today)


async def record_outgoing(user_id: int, text: str, now: datetime) -> None:
    """把主动发出的消息记进最近的私聊会话；没有会话就不记。"""

    from nonebot_plugin_orm import get_session

    async with get_session() as session:
        session_id = await session.scalar(
            select(ChatSession.id)
            .where(
                ChatSession.user_id == user_id,
                ChatSession.group_id.is_(None),
                ChatSession.is_deleted.is_(False),
            )
            .order_by(ChatSession.updated_at.desc().nullslast())
            .limit(1)
        )
        if session_id is None:
            return
        session.add(ChatMessage(session_id=int(session_id), role="assistant", content=text, created_at=now))
        await session.commit()


async def run_tick(
    now: datetime,
    send: Sender,
    *,
    unchecked: CheckinProbe = _unchecked_groups,
    record: Callable[[int, str, datetime], Awaitable[None]] = record_outgoing,
    wake: Waker | None = None,
) -> int:
    """扫描一遍，返回发出的消息条数（提醒、替他办完的回合、主动消息）。"""

    if not private_agent_config.private_agent_enabled or not private_agent_config.private_agent_proactive:
        return 0
    sent = 0
    for user_id in store.known_user_ids():
        proactive_ok = not auto_paused(user_id)
        try:
            firings = store.update(user_id, lambda rec: ps.collect_due(rec, now))
        except Exception:  # noqa: BLE001
            dbg_exc(f"私聊调度读写用户 {user_id} 的提醒失败")
            firings = []
        for firing in firings:
            sent += await _run_firing(user_id, firing, now, send, record, wake, proactive_ok)

        def plan(rec: dict[str, Any], uid: int = user_id, ok: bool = proactive_ok) -> tuple[list[str], list[str]]:
            rec["user_id"] = uid
            try:
                return plan_user(rec, now, proactive_ok=ok)
            finally:
                rec.pop("user_id", None)

        try:
            messages, pending = store.update(user_id, plan)
        except Exception:  # noqa: BLE001
            dbg_exc(f"私聊调度读写用户 {user_id} 失败")
            continue
        if "checkin_nudge" in pending:
            try:
                missing = await unchecked(user_id, now)
            except Exception:  # noqa: BLE001
                dbg_exc(f"私聊签到提醒查询用户 {user_id} 失败")
                missing = 0
            if missing:
                messages.append(
                    _voice(store.load(user_id), f"今天还有 {missing} 个群没签到哦，回我一句“帮我签到”就好～")
                )
        for text in messages:
            if not await send(user_id, text):
                break
            sent += 1
            try:
                await record(user_id, text, now)
            except Exception:  # noqa: BLE001
                dbg_exc(f"私聊主动消息入库失败 用户 {user_id}")
    return sent


async def _run_firing(
    user_id: int,
    firing: ps.Firing,
    now: datetime,
    send: Sender,
    record: Callable[[int, str, datetime], Awaitable[None]],
    wake: Waker | None,
    proactive_ok: bool,
) -> int:
    """执行一条到点的私聊提醒并写回结果；返回送达条数。

    agent 模式交给私聊回合去办；他用 /退出 暂停了或回合没办成时退回发提醒原文，
    保证他至少收到。发不出去时按退避重试，不再像以前那样直接丢掉。
    """

    from .schedule_policy import load_settings, record_private_usage

    settings = load_settings()
    result = "failed"
    delivered = 0
    if firing.mode == "agent" and wake is not None and proactive_ok and settings.private_agent_mode:
        try:
            result = await wake(user_id, firing)
        except Exception:  # noqa: BLE001
            dbg_exc(f"私聊用户 {user_id} 定时代办回合失败，改发提醒")
            result = "fallback_needed"
        if result == "done":
            delivered = 1
    if result != "done":
        text = ps.notify_text(firing) if firing.mode == "notify" else f"⏰ 到点了{ps.late_note(firing.late)}：{firing.content}"
        if await send(user_id, text):
            delivered = 1
            result = "sent" if firing.mode == "notify" else "fallback"
            try:
                await record(user_id, text, now)
            except Exception:  # noqa: BLE001
                dbg_exc(f"私聊提醒入库失败 用户 {user_id}")
        else:
            result = "failed"
    try:
        final = store.update(
            user_id,
            lambda rec: ps.finish(rec, firing, result, now, retry_limit=int(settings.private_retry_limit)),
        )
    except Exception:  # noqa: BLE001
        dbg_exc(f"私聊提醒结果写回失败 用户 {user_id}")
        final = result
    record_private_usage(final)
    return delivered


async def _send_private(user_id: int, text: str) -> bool:
    bots = [bot for bot in get_bots().values() if bot is not None]
    if not bots:
        return False
    try:
        await bots[0].send_private_msg(user_id=user_id, message=text)
    except Exception:  # noqa: BLE001
        dbg_exc(f"私聊主动消息发送失败 用户 {user_id}")
        return False
    return True


async def _tick() -> None:
    bots = [bot for bot in get_bots().values() if bot is not None]
    if not bots:
        return
    from .private_schedule_turn import bot_waker

    try:
        await run_tick(now_beijing(), _send_private, wake=bot_waker(bots[0]))
    except Exception:  # noqa: BLE001
        logger.exception("私聊 Agent 调度 tick 失败")
    try:
        from .private_tasks import flush_held

        # 免打扰期间完成的小助手任务：免打扰结束后补发汇报（不受 proactive 开关影响，是他要的）。
        await flush_held(now_beijing())
    except Exception:  # noqa: BLE001
        logger.exception("私聊子任务补发汇报失败")


@get_driver().on_startup
async def _register_job() -> None:
    if scheduler.get_job(JOB_ID) is None:
        scheduler.add_job(
            _tick,
            "interval",
            seconds=TICK_SECONDS,
            id=JOB_ID,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=TICK_SECONDS,
        )
        dbg(f"已注册私聊调度 {JOB_ID}（每 {TICK_SECONDS} 秒）")


__all__ = ["JOB_ID", "plan_user", "record_outgoing", "run_tick"]
