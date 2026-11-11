# ruff: noqa: TID252, E501, PLR0913
"""私聊主动提议的调度与回合：每分钟看一遍谁到了该问的时候，替他找事、问他要不要我去做。

- 调度 ``run_offer_tick``：独立的 apscheduler 任务（每 60 秒），只看有个人数据的用户
  （``private_store.known_user_ids``）。先过不查库的门槛（总闸、策略、他的开关、时段），
  再查私聊记录拿“他/我上一次说话”的时间、好友、/退出、是不是正在和我说话，
  过了全部门槛才找候选；没有值得问的事就什么都不做（不记日志、不问“有什么要帮忙的吗”）。
  每次最多给 ``per_tick`` 个人跑回合，一个跑完再下一个。
- 回合 ``run_offer_turn``：和到点代办一样走完整私聊主回合（``extra["offer"]``，
  pipeline ``offer``，同一份开头与工具表，命中前缀缓存），user 通知
  ``private.offer_notice`` + 候选 JSON，system#3 加 ``private.offer_turn``。拿回合锁；
  这一轮只放行只读工具（不许真的开始做、不许设提醒或去群里）。模型可以回
  【提议:无】/【不回】选择这次不问；生成期间他先发了消息就不发（让给他那一轮）。
- 结果写进他的个人数据 ``offers.log``（问了/没问/他先开口/没发出去）和全站当日用量。

门槛与候选的规则在 ``private_offers``，策略在 ``private_offer_policy``。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from nonebot import get_bots, get_driver, logger
from nonebot_plugin_apscheduler import scheduler

from . import private_offers as po
from . import private_store as store
from .context import now_beijing
from .log import dbg, dbg_exc
from .private_offer_policy import OfferSettings, load_settings, record_usage

JOB_ID = "yawn_private_offer_tick"
TICK_SECONDS = 60
# 提议回合只放行这些只读工具：能查一查再问，但不能真的开始做（等他答应了再做）。
OFFER_ALLOWED_TOOLS = frozenset(
    {"recall", "my_status", "group_recent"}
)


def env_on() -> bool:
    from ..private_chat_bridge import private_agent_config

    return bool(getattr(private_agent_config, "private_agent_offers", True))


def agent_on() -> bool:
    from ..private_chat_bridge import private_agent_config

    return bool(private_agent_config.private_agent_enabled and private_agent_config.private_agent_proactive)


# ── 事实 ────────────────────────────────────────────────────────────────


async def chat_times(user_id: int) -> tuple[datetime | None, datetime | None]:
    """最近私聊会话里他最后一条、我最后一条消息的时间。"""

    from nonebot_plugin_orm import get_session
    from sqlalchemy import func, select

    from ..data_models.chat_message import ChatMessage
    from ..data_models.chat_session import ChatSession

    async with get_session() as session:
        session_id = await session.scalar(
            select(ChatSession.id)
            .where(
                ChatSession.user_id == int(user_id),
                ChatSession.group_id.is_(None),
                ChatSession.is_deleted.is_(False),
            )
            .order_by(ChatSession.updated_at.desc().nullslast())
            .limit(1)
        )
        if session_id is None:
            return None, None
        rows = (
            await session.execute(
                select(ChatMessage.role, func.max(ChatMessage.created_at))
                .where(ChatMessage.session_id == session_id, ChatMessage.is_deleted.is_(False))
                .group_by(ChatMessage.role)
            )
        ).all()
    found = {str(role): moment for role, moment in rows}
    return found.get("user"), found.get("assistant")


async def gather_facts(bot: Any, user_id: int, settings: OfferSettings) -> po.Facts:
    from ..private_chat_bridge import auto_paused, user_turn_lock

    last_user, last_bot = await chat_times(user_id)
    friend: bool | None = None
    if settings.only_friends and bot is not None:
        from .agent_bridge import is_friend

        try:
            friend = await is_friend(bot, user_id)
        except Exception:  # noqa: BLE001
            friend = None
    return po.Facts(
        last_user_at=last_user,
        last_bot_at=last_bot,
        friend=friend,
        paused=auto_paused(user_id),
        busy=user_turn_lock(user_id).locked(),
        affinity_locked=await _offers_locked(user_id),
        interval_factor=_interval_factor(user_id),
    )


def _interval_factor(user_id: int) -> float:
    """好感主动性决定的提议间隔倍数（读余额缓存，没缓存按 1）。"""

    try:
        from ..affinity_bond import offer_interval_factor
        from ..affinity_perks import cached_points

        points = cached_points(user_id)
        return 1.0 if points is None else offer_interval_factor(points)
    except Exception:  # noqa: BLE001
        return 1.0


async def _offers_locked(user_id: int) -> bool:
    """好感玩法开着、而他的等级还没解锁“主动提议”时为 True；查不到按没锁。"""

    try:
        from ..affinity_perks import cached_points, has_unlock, load_points, policy

        pol = policy()
        if not pol.enabled or pol.unlock_level("offers") is None:
            return False
        points = cached_points(user_id)
        if points is None:
            from nonebot_plugin_orm import get_session

            async with get_session() as session:
                points = await load_points(session, user_id)
        return not has_unlock(points, "offers", pol)
    except Exception:  # noqa: BLE001
        return False


async def gather_candidates(user_id: int, record: dict[str, Any], now: datetime, settings: OfferSettings) -> list[po.Candidate]:
    """候选：个人数据里的 + 要现查的（没签到的群、群里转来的事）。"""

    unchecked = 0
    if settings.kind_on("checkin") and now.hour >= po.CHECKIN_FROM_HOUR:
        from .private_scheduler import (
            _unchecked_groups,  # pyright: ignore[reportPrivateUsage]
        )

        try:
            unchecked = await _unchecked_groups(user_id, now)
        except Exception:  # noqa: BLE001
            dbg_exc(f"主动提议查用户 {user_id} 签到失败(忽略)")
    handoffs: list[dict[str, Any]] = []
    if settings.kind_on("handoff_open"):
        try:
            from .agent_bridge import active_handoffs

            handoffs = active_handoffs(user_id, now)
        except Exception:  # noqa: BLE001
            dbg_exc(f"主动提议读用户 {user_id} 群里转来的事失败(忽略)")
    return po.collect_candidates(record, now, unchecked_groups=unchecked, handoffs=handoffs)


# ── 预览（WebUI）──────────────────────────────────────────────────────────


@dataclass(slots=True)
class Plan:
    user_id: int
    gate: po.Gate
    facts: po.Facts
    candidates: list[po.Candidate] = field(default_factory=list)
    dropped: list[dict[str, Any]] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)

    def view(self) -> dict[str, Any]:
        facts = self.facts
        return {
            "user_id": self.user_id,
            "gate": self.gate.view(),
            "facts": {
                "last_user_at": po.fmt(facts.last_user_at) if facts.last_user_at else None,
                "last_bot_at": po.fmt(facts.last_bot_at) if facts.last_bot_at else None,
                "friend": facts.friend,
                "paused": facts.paused,
                "busy": facts.busy,
            },
            "candidates": [item.admin_view() for item in self.candidates],
            "dropped": self.dropped,
            "payload": self.payload,
        }


async def plan_for(bot: Any, user_id: int, now: datetime, *, force: bool = False, settings: OfferSettings | None = None) -> Plan:
    """算一遍：门槛、候选（含被筛掉的和原因）、要给模型的 JSON。不写任何东西。"""

    settings = settings or load_settings()
    record = store.load(user_id)
    facts = await gather_facts(bot, user_id, settings)
    gate = po.evaluate_gate(record, settings, facts, now, user_id, env_on=env_on(), agent_on=agent_on(), force=force)
    plan = Plan(user_id, gate, facts)
    found = await gather_candidates(user_id, record, now, settings)
    plan.candidates, plan.dropped = po.filter_candidates(record, found, settings, now)
    if plan.candidates:
        plan.payload = po.notice_payload(plan.candidates, settings, facts, now, record)
    elif gate.ok:
        gate.reason = "no_candidates"
    return plan


# ── 回合 ────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class OfferResult:
    status: str  # sent / skipped / user_spoke / failed
    text: str = ""
    picks: list[int] = field(default_factory=list)


async def run_offer_turn(bot: Any, user_id: int, payload: dict[str, Any]) -> OfferResult:
    """跑一轮提议；拿回合锁，和他自己发来的消息排队。"""

    from nonebot_plugin_orm import get_session

    from ..llm import background_priority
    from ..private_chat_bridge import PrivateTurnRequest, user_turn_lock
    from . import private_turn_taking as turn_taking
    from .private_agent import (  # pyright: ignore[reportPrivateUsage]
        _recent_history,
        handle_private_turn,
    )
    from .private_prompt import PRIVATE_OFFER_NOTICE
    from .private_scheduler import record_outgoing
    from .prompt_sections import apply_section

    notice = apply_section("private.offer_notice", PRIVATE_OFFER_NOTICE) + "\n" + json.dumps(payload, ensure_ascii=False)
    lock = user_turn_lock(user_id)
    if lock.locked():
        return OfferResult("user_spoke")
    async with lock:
        since = turn_taking.arrivals(user_id)
        out: dict[str, Any] = {}
        try:
            async with get_session() as session:
                history = await _recent_history(session, user_id)
                request = PrivateTurnRequest(
                    bot=bot,
                    event=None,
                    user_id=user_id,
                    user_input=notice,
                    raw_text="",
                    history=[*history, {"role": "user", "content": notice}],
                    session=session,
                    extra={"offer": payload, "offer_out": out},
                )
                # 是我主动找他，没人在等：模型调用走后台车道，不和实时聊天抢名额。
                with background_priority():
                    text = await handle_private_turn(request)
        except Exception:  # noqa: BLE001
            dbg_exc(f"私聊用户 {user_id} 主动提议回合出错")
            return OfferResult("failed")
        if out.get("user_spoke") or turn_taking.newer_message_arrived(user_id, since):
            return OfferResult("user_spoke")
        if text is None:
            return OfferResult("failed")
        if not text:
            return OfferResult("skipped")
        try:
            await record_outgoing(int(user_id), text, now_beijing())
        except Exception:  # noqa: BLE001
            dbg_exc(f"私聊用户 {user_id} 主动提议入库失败(忽略)")
        return OfferResult("sent", text, list(out.get("picks") or [1]))


Runner = Callable[[Any, int, dict[str, Any]], Awaitable[OfferResult]]


async def offer_once(
    bot: Any,
    user_id: int,
    now: datetime,
    *,
    force: bool = False,
    by: str = "auto",
    runner: Runner | None = None,
    plan: Plan | None = None,
) -> dict[str, Any]:
    """给一个人走一遍：门槛 → 候选 → 回合 → 记结果。返回给日志/WebUI 的摘要。"""

    settings = load_settings()
    plan = plan or await plan_for(bot, user_id, now, force=force, settings=settings)
    _note_check(user_id, plan.gate, now)
    if not plan.gate.ok or not plan.candidates:
        return {"status": "not_now", "reason": plan.gate.reason, "label": plan.gate.view()["label"]}
    runner = runner or run_offer_turn
    result: OfferResult = await runner(bot, user_id, plan.payload)
    candidates = plan.candidates

    def apply(record: dict[str, Any]) -> dict[str, Any]:
        return po.record_attempt(
            record,
            result.status,
            now,
            candidates=candidates,
            picks=result.picks,
            text=result.text,
            by=by,
            settings=settings,
        )

    try:
        row = store.update(user_id, apply)
    except Exception:  # noqa: BLE001
        dbg_exc(f"主动提议结果写回失败 用户 {user_id}")
        row = {}
    picked = [c.kind for i, c in enumerate(candidates, start=1) if i in result.picks] if result.status == "sent" else []
    record_usage(result.status, picked)
    _trace(user_id, result, candidates, by)
    dbg(f"私聊用户 {user_id} 主动提议：{result.status}（候选 {len(candidates)} 件，问了 {result.picks}）")
    return {"status": result.status, "id": row.get("id"), "text": result.text, "picks": result.picks}


def _note_check(user_id: int, gate: po.Gate, now: datetime) -> None:
    """门槛结果写进 ``offers.last_check``（原因或下一次时间变了才写，避免每分钟改文件）。"""

    view = gate.view()
    current = po.peek(store.load(user_id)).get("last_check") or {}
    if current.get("reason") == view["reason"] and current.get("next_at") == view.get("next_at"):
        return

    def apply(record: dict[str, Any]) -> None:
        po.state(record)["last_check"] = {**view, "at": po.fmt(now)}

    try:
        store.update(user_id, apply)
    except Exception:  # noqa: BLE001
        dbg_exc(f"主动提议记录检查结果失败 用户 {user_id}")


def _trace(user_id: int, result: OfferResult, candidates: list[po.Candidate], by: str) -> None:
    try:
        from .execution_trace import trace_event

        trace_event(
            "decision",
            "私聊主动提议",
            status="success" if result.status in {"sent", "skipped"} else "error" if result.status == "failed" else "success",
            output={
                "user_id": user_id,
                "status": result.status,
                "picks": result.picks,
                "kinds": [c.kind for c in candidates],
                "by": by,
            },
        )
    except Exception:  # noqa: BLE001
        dbg_exc("主动提议 Trace 记录失败(忽略)")


def _cheap_skip(record: dict[str, Any], settings: OfferSettings, now: datetime, user_id: int) -> bool:
    """不查库就能确定现在不问的：省掉每分钟对每个人的数据库查询。"""

    if not settings.enabled or settings.user_open(user_id) or not po.user_enabled(record, settings):
        return True
    if settings.in_quiet_hours(f"{now:%H:%M}"):
        return True
    data = po.peek(record)
    snooze = po.parse(data["prefs"].get("snooze_until"))
    if snooze is not None and snooze > now:
        return True
    retry = po.parse(data.get("retry_at"))
    if retry is not None and retry > now:
        return True
    if po.sent_today(data, now) >= int(settings.daily_max):
        return True
    last = po.last_attempt_at(data)
    return last is not None and now - last < timedelta(hours=po.interval_hours(record, settings))


async def run_offer_tick(now: datetime, bot: Any, *, runner: Runner | None = None) -> list[dict[str, Any]]:
    """扫一遍；返回这次真的跑了回合的结果。"""

    if not env_on() or not agent_on():
        return []
    settings = load_settings()
    if not settings.enabled:
        return []
    done: list[dict[str, Any]] = []
    for user_id in store.known_user_ids():
        if len(done) >= int(settings.per_tick):
            break
        try:
            record = store.load(user_id)
            if _cheap_skip(record, settings, now, user_id):
                continue
            plan = await plan_for(bot, user_id, now, settings=settings)
            if not plan.gate.ok:
                _note_check(user_id, plan.gate, now)
                continue
            result = await offer_once(bot, user_id, now, runner=runner, plan=plan)
        except Exception:  # noqa: BLE001
            dbg_exc(f"主动提议处理用户 {user_id} 失败")
            continue
        if result.get("status") != "not_now":
            done.append({"user_id": user_id, **result})
    return done


async def _tick() -> None:
    bots = [bot for bot in get_bots().values() if bot is not None]
    if not bots:
        return
    try:
        await run_offer_tick(now_beijing(), bots[0])
    except Exception:  # noqa: BLE001
        logger.exception("私聊主动提议 tick 失败")


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
        dbg(f"已注册私聊主动提议 {JOB_ID}（每 {TICK_SECONDS} 秒）")


__all__ = [
    "JOB_ID",
    "OFFER_ALLOWED_TOOLS",
    "OfferResult",
    "Plan",
    "agent_on",
    "chat_times",
    "env_on",
    "gather_candidates",
    "gather_facts",
    "offer_once",
    "plan_for",
    "run_offer_tick",
    "run_offer_turn",
]
