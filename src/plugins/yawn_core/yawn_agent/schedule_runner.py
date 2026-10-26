# ruff: noqa: E501, C901, TID252, PLR0913, PLR0917, PERF203
"""Agent 定时任务调度器：到点领取任务、唤醒 Agent 或直接发提醒、记录结果。

每 ``TICK_SECONDS`` 秒扫一次 ``next_run_at`` 到期的有效任务：

1. ``schedule_store.claim_due`` 写执行租约并提交（并发 tick / 多实例只有一方领到）；
2. :func:`plan_due` 决定这一次是按时执行、迟到补跑、错过跳过，还是落在免打扰时段
   被推迟/跳过，并算好下一次时间——**执行前**就把 ``next_run_at`` 推进并提交，
   进程中途重启不会重复触发；
3. agent 模式构造一个“设任务的人发来任务说明”的合成回合，在
   :func:`schedule_wake.scheduled_wake` 标记下交给 ``dialogue.process_group_message``，
   与普通对话共用群锁、上下文、工具、发言与追踪；notify 模式不调模型，经
   ``outbound`` 直接发提醒原文；
4. 结果写回任务（运行次数、最近结果、发言预览、Trace id、历史），连续失败达到
   策略上限自动暂停；单次任务失败按 2/5/15 分钟退避重试。

同一个群的多个到期任务串行执行（共用群锁），不同群之间有界并发。
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

from nonebot import get_bots, get_driver, logger
from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_orm import get_session
from sqlalchemy import select

from ..data_models.group_agent_config import GroupAgentConfig
from ..data_models.group_agent_message import GroupAgentMessage
from .config_store import agent_runtime_enabled
from .context import now_beijing
from .dialogue_support import persist_bot_reply
from .execution_trace import recent_execution_trace_summaries
from .log import dbg, dbg_exc
from .message_parser import NormalizedMessage, SegmentNode
from .outbound import send_outbound_message
from .schedule_policy import (
    ScheduleSettings,
    group_runs_today,
    load_settings,
    record_run_usage,
)
from .schedule_spec import format_datetime
from .schedule_store import (
    STATUS_ACTIVE,
    STATUS_COMPLETED,
    ScheduleUnavailableError,
    claim_due,
    clip,
    following_run,
    get_task,
    pause_task,
    push_history,
    remaining_runs,
)
from .schedule_wake import WAKE_TRIGGER_SOURCE, ScheduledWake, scheduled_wake

JOB_ID = "yawn_core_agent:schedule"
TICK_SECONDS = 20
CLAIM_LIMIT = 12
# 单次任务失败后的重试间隔（分钟）；循环任务等下一次自然触发。
RETRY_BACKOFF_MINUTES = (2, 5, 15)

RESULT_SPOKE = "spoke"
RESULT_SENT = "sent"
RESULT_SILENT = "silent"
RESULT_FAILED = "failed"
RESULT_UNKNOWN = "delivery_unknown"
RESULT_SKIPPED_MISSED = "skipped_missed"
RESULT_SKIPPED_QUIET = "skipped_quiet"
RESULT_SKIPPED_QUOTA = "skipped_quota"
RESULT_SKIPPED_DISABLED = "skipped_disabled"
RESULT_SKIPPED_CLOSED = "skipped_closed"
RESULT_SKIPPED_OFFLINE = "skipped_offline"
RESULT_CREATOR_LEFT = "creator_left"
RESULT_DEFERRED = "deferred_quiet"
# 计入“成功执行一次”的结果：消耗运行次数、清零失败计数。
RAN_RESULTS = frozenset({RESULT_SPOKE, RESULT_SENT, RESULT_SILENT, RESULT_UNKNOWN})
SKIPPED_RESULTS = frozenset(
    {
        RESULT_SKIPPED_MISSED,
        RESULT_SKIPPED_QUIET,
        RESULT_SKIPPED_QUOTA,
        RESULT_SKIPPED_DISABLED,
        RESULT_SKIPPED_CLOSED,
        RESULT_SKIPPED_OFFLINE,
    }
)
RESULT_LABELS = {
    RESULT_SPOKE: "已执行并发言",
    RESULT_SENT: "已发送提醒",
    RESULT_SILENT: "已执行（没有发言）",
    RESULT_FAILED: "执行失败",
    RESULT_UNKNOWN: "发送结果未知",
    RESULT_SKIPPED_MISSED: "错过时间已跳过",
    RESULT_SKIPPED_QUIET: "免打扰时段跳过",
    RESULT_SKIPPED_QUOTA: "今日次数用完跳过",
    RESULT_SKIPPED_DISABLED: "群 Agent 关闭跳过",
    RESULT_SKIPPED_CLOSED: "定时任务未开放跳过",
    RESULT_SKIPPED_OFFLINE: "机器人不在线跳过",
    RESULT_CREATOR_LEFT: "设任务的人已不在群里",
    RESULT_DEFERRED: "免打扰时段推迟",
}


@dataclass(frozen=True, slots=True)
class DuePlan:
    """一次到期的处理方案（纯计算结果）。"""

    action: str  # run / skip / defer
    planned_at: datetime
    next_run_at: datetime | None
    late_minutes: int = 0
    result: str = ""


def _quiet_end(settings: ScheduleSettings, now: datetime) -> datetime:
    hour, minute = (int(part) for part in settings.quiet_end.split(":"))
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def plan_due(task: Any, settings: ScheduleSettings, now: datetime) -> DuePlan:
    """决定一条到期任务这次怎么处理，并算出下一次时间。

    - 迟到不超过宽限：按时执行，下一次从计划时间往后算（不漂移）；
    - 迟到超过宽限：``run_once`` 且在补跑窗口内就补跑一次，否则跳过；两种情况
      下一次都从“现在”往后算，错过的中间几次不会被连环补发；
    - 落在免打扰时段：推迟到时段结束（同一次只推迟一回）或跳过。
    """

    planned = task.next_run_at or now
    late = max((now - planned).total_seconds() / 60, 0.0)
    late_minutes = int(late)
    on_time = late <= settings.misfire_grace_minutes
    after = planned if on_time else now
    if not on_time:
        catch_up = settings.missed_policy == "run_once" and late <= settings.catch_up_max_hours * 60
        if not catch_up:
            return DuePlan("skip", planned, following_run(task, now), late_minutes, RESULT_SKIPPED_MISSED)
    if settings.in_quiet_hours(now.strftime("%H:%M")):
        if settings.quiet_policy == "defer":
            deferred = _quiet_end(settings, now)
            upcoming_run = following_run(task, now)
            # 推迟到时段结束；如果下一次本来就更早（高频任务），直接跳过这次。
            if upcoming_run is None or deferred < upcoming_run:
                return DuePlan("defer", planned, deferred, late_minutes, RESULT_DEFERRED)
        return DuePlan("skip", planned, following_run(task, now), late_minutes, RESULT_SKIPPED_QUIET)
    left = remaining_runs(task)
    if left is not None and left <= 1:
        next_run = None
    else:
        next_run = following_run(task, after)
        if next_run is not None and next_run <= now:
            next_run = following_run(task, now)
    return DuePlan("run", planned, next_run, late_minutes if not on_time else 0)


@dataclass(slots=True)
class _RunSnapshot:
    """提交前从 ORM 行里取出的执行所需字段（避免 commit 后读过期属性）。"""

    task_id: int
    group_id: int
    bot_id: int | None
    creator_user_id: int
    creator_name: str
    title: str
    instruction: str
    mode: str
    mention_creator: bool
    run_number: int
    max_runs: int | None
    description: str
    created_at: datetime | None
    previous_output: str
    planned_at: datetime
    late_minutes: int
    once: bool


@dataclass(slots=True)
class RunOutcome:
    result: str
    preview: str = ""
    error: str = ""
    trace_id: str | None = None
    duration_ms: float = 0.0


def _bot_id(bot: Any) -> int:
    return int(str(getattr(bot, "self_id", "") or 0))


async def _latest_group_bot_id(session: Any, group_id: int) -> int | None:
    value = await session.scalar(
        select(GroupAgentMessage.bot_id)
        .where(GroupAgentMessage.group_id == group_id)
        .order_by(GroupAgentMessage.id.desc())
        .limit(1)
    )
    return int(value) if value is not None else None


def pick_bot(bots: list[Any], preferred: tuple[int | None, ...]) -> Any | None:
    """按偏好顺序选在线的机器人：任务创建时的那个 → 群里最近活跃的 → 任意一个。"""

    for wanted in preferred:
        if wanted is None:
            continue
        for bot in bots:
            if _bot_id(bot) == wanted:
                return bot
    return bots[0] if bots else None


async def _member_info(bot: Any, group_id: int, user_id: int) -> dict[str, Any] | None:
    """设任务的人现在还在不在群里；查询本身失败（协议不支持）时按“在”处理。"""

    try:
        info = await bot.call_api("get_group_member_info", group_id=group_id, user_id=user_id, no_cache=True)
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        if any(marker in text for marker in ("不存在", "not found", "NotFound", "not a member", "retcode=100")):
            return None
        dbg(f"群 {group_id} 定时任务查询设任务成员 {user_id} 失败(按在群处理): {text[:120]}")
        return {}
    return info if isinstance(info, dict) else {}


def build_wake_turn(snapshot: _RunSnapshot, member: dict[str, Any]) -> tuple[Any, NormalizedMessage]:
    """合成“设任务的人把任务说明发给 Bot”的回合输入（不落库、没有 message_id）。"""

    text = snapshot.instruction
    normalized = NormalizedMessage(
        plain_text=text,
        segments=[SegmentNode(type="text", data={"text": text}, text=text)],
        # 需要提醒设任务的人时把他放进 mentions：本轮会开放 at 消息段。
        mentions=[snapshot.creator_user_id] if snapshot.mention_creator else [],
    )
    normalized.trigger_source = WAKE_TRIGGER_SOURCE
    normalized.trigger_signals = {"scheduled_task": True}
    sender = SimpleNamespace(
        card=str(member.get("card") or "") or snapshot.creator_name or None,
        nickname=str(member.get("nickname") or "") or snapshot.creator_name or str(snapshot.creator_user_id),
        role=str(member.get("role") or "member"),
        title=str(member.get("title") or "") or None,
    )
    creator = snapshot.creator_user_id
    event = SimpleNamespace(
        group_id=snapshot.group_id,
        message_id=None,
        user_id=creator,
        sender=sender,
        get_user_id=lambda: str(creator),
        to_me=True,
    )
    return event, normalized


async def _bot_messages_since(group_id: int, bot_id: int, since: datetime) -> list[str]:
    async with get_session() as session:
        rows = (
            await session.execute(
                select(GroupAgentMessage.normalized_text)
                .where(
                    GroupAgentMessage.group_id == group_id,
                    GroupAgentMessage.bot_id == bot_id,
                    GroupAgentMessage.user_id == bot_id,
                    GroupAgentMessage.received_at >= since,
                )
                .order_by(GroupAgentMessage.id.asc())
                .limit(8)
            )
        ).scalars().all()
    return [str(item or "") for item in rows if str(item or "").strip()]


def _wake_trace_id(group_id: int, actor_user_id: int, since: datetime) -> str | None:
    for summary in recent_execution_trace_summaries(
        group_id, trigger_source=WAKE_TRIGGER_SOURCE, actor_user_id=actor_user_id
    ):
        try:
            started = datetime.fromisoformat(str(summary.get("startedAt")))
        except ValueError:
            continue
        if started.replace(tzinfo=None) >= since - timedelta(seconds=1):
            return str(summary.get("traceId") or "") or None
    return None


async def _run_agent(bot: Any, snapshot: _RunSnapshot, member: dict[str, Any], settings: ScheduleSettings) -> RunOutcome:
    from .dialogue import process_group_message

    event, normalized = build_wake_turn(snapshot, member)
    wake = ScheduledWake(
        group_id=snapshot.group_id,
        task_id=snapshot.task_id,
        title=snapshot.title,
        creator_user_id=snapshot.creator_user_id,
        creator_name=snapshot.creator_name,
        planned_at=snapshot.planned_at,
        run_number=snapshot.run_number,
        max_runs=snapshot.max_runs,
        schedule=snapshot.description,
        created_at=snapshot.created_at,
        late_minutes=snapshot.late_minutes,
        previous_output=clip(snapshot.previous_output, 160),
        mention_creator=snapshot.mention_creator,
    )
    started_at = now_beijing().replace(microsecond=0)
    bot_id = _bot_id(bot)
    with scheduled_wake(wake):
        await asyncio.wait_for(
            process_group_message(cast("Any", bot), cast("Any", event), normalized),
            timeout=settings.run_timeout_seconds,
        )
    said = await _bot_messages_since(snapshot.group_id, bot_id, started_at)
    trace_id = _wake_trace_id(snapshot.group_id, snapshot.creator_user_id, started_at)
    if said:
        return RunOutcome(RESULT_SPOKE, preview=clip(" / ".join(said), 300), trace_id=trace_id)
    return RunOutcome(RESULT_SILENT, trace_id=trace_id)


async def _run_notify(bot: Any, snapshot: _RunSnapshot, settings: ScheduleSettings) -> RunOutcome:
    text = f"{settings.notify_prefix}{snapshot.instruction}".strip()
    segments: list[dict[str, Any]] = []
    if snapshot.mention_creator:
        segments.append({"type": "at", "user_id": snapshot.creator_user_id})
        text = f" {text}"
    segments.append({"type": "text", "text": text})
    async with get_session() as session:
        result = await send_outbound_message(
            bot,
            snapshot.group_id,
            segments,
            session=session,
            actor_user_id=snapshot.creator_user_id,
            source="scheduled_task",
        )
        if result.sent:
            config = await session.get(GroupAgentConfig, snapshot.group_id)
            await persist_bot_reply(
                session,
                _bot_id(bot),
                snapshot.group_id,
                result.message_id,
                result.normalized_text,
                int(getattr(config, "raw_retention_days", 30) or 30),
                segments=result.segments,
            )
        await session.commit()
    if result.sent:
        return RunOutcome(RESULT_SENT, preview=clip(result.normalized_text or text, 300))
    if result.may_have_delivered:
        return RunOutcome(RESULT_UNKNOWN, preview=clip(text, 300), error="QQ 没有返回发送回执")
    return RunOutcome(RESULT_FAILED, error=f"发送失败（{result.outcome}）")


async def _prepare(task_id: int, settings: ScheduleSettings, now: datetime) -> tuple[_RunSnapshot | None, list[Any]]:
    """锁定一次执行：推进 next_run_at 并提交；返回执行快照和候选机器人。"""

    async with get_session() as session:
        task = await get_task(session, task_id)
        if task is None or task.status != STATUS_ACTIVE:
            if task is not None:
                task.lease_until = None
                await session.commit()
            return None, []
        plan = plan_due(task, settings, now)
        group_id = int(task.group_id)
        skip_result: str | None = None
        if plan.action == "skip":
            skip_result = plan.result
        elif not settings.enabled or not settings.group_open(group_id):
            skip_result = RESULT_SKIPPED_CLOSED
        elif plan.action == "run" and group_runs_today(group_id) >= settings.group_daily_runs:
            skip_result = RESULT_SKIPPED_QUOTA
        elif plan.action == "run":
            config = await session.get(GroupAgentConfig, group_id)
            if config is None or not await agent_runtime_enabled(session, group_id, config=config):
                skip_result = RESULT_SKIPPED_DISABLED
        if plan.action == "defer":
            task.next_run_at = plan.next_run_at
            task.lease_until = None
            task.status_reason = f"免打扰时段，推迟到 {format_datetime(plan.next_run_at or now)}"
            push_history(
                task,
                {"at": format_datetime(now), "planned": format_datetime(plan.planned_at), "result": RESULT_DEFERRED},
                limit=settings.history_limit,
            )
            await session.commit()
            record_run_usage(group_id, RESULT_DEFERRED, counts=False)
            return None, []
        if skip_result is not None:
            next_run = plan.next_run_at if plan.action == "skip" else following_run(task, now)
            _finish_skip(task, skip_result, plan, next_run, now, settings)
            await session.commit()
            record_run_usage(group_id, skip_result, counts=False)
            dbg(f"群 {group_id} 定时任务 {task_id} 跳过: {skip_result}")
            return None, []
        bots = [bot for bot in get_bots().values() if bot is not None]
        preferred = (
            int(task.bot_id) if task.bot_id else None,
            await _latest_group_bot_id(session, group_id),
        )
        bot = pick_bot(bots, preferred)
        if bot is None:
            _finish_skip(task, RESULT_SKIPPED_OFFLINE, plan, following_run(task, now), now, settings)
            await session.commit()
            record_run_usage(group_id, RESULT_SKIPPED_OFFLINE, counts=False)
            return None, []
        snapshot = _RunSnapshot(
            task_id=int(task.id),
            group_id=group_id,
            bot_id=int(task.bot_id) if task.bot_id else None,
            creator_user_id=int(task.creator_user_id),
            creator_name=str(task.creator_name or ""),
            title=str(task.title or ""),
            instruction=str(task.instruction or ""),
            mode=str(task.mode or "agent"),
            mention_creator=bool(task.mention_creator),
            run_number=int(task.run_count or 0) + 1,
            max_runs=int(task.max_runs) if task.max_runs and task.schedule_kind != "once" else None,
            description=str(task.description or ""),
            created_at=task.created_at,
            previous_output=str(task.last_output or ""),
            planned_at=plan.planned_at,
            late_minutes=plan.late_minutes,
            once=task.schedule_kind == "once",
        )
        # 执行前推进下一次并提交：执行中途进程退出也不会重复触发同一次。
        task.next_run_at = plan.next_run_at
        await session.commit()
        return snapshot, [bot]


def _finish_skip(task: Any, result: str, plan: DuePlan, next_run: datetime | None, now: datetime, settings: ScheduleSettings) -> None:
    task.skip_count = int(task.skip_count or 0) + 1
    task.last_result = result
    task.lease_until = None
    task.next_run_at = next_run
    task.status_reason = RESULT_LABELS.get(result, result)
    if next_run is None:
        task.status = STATUS_COMPLETED
        task.status_reason = f"{RESULT_LABELS.get(result, result)}，之后没有下一次了"
    push_history(
        task,
        {
            "at": format_datetime(now),
            "planned": format_datetime(plan.planned_at),
            "result": result,
            **({"late": plan.late_minutes} if plan.late_minutes else {}),
        },
        limit=settings.history_limit,
    )
    task.updated_at = now
    task.revision = int(task.revision or 0) + 1


async def _record(snapshot: _RunSnapshot, outcome: RunOutcome, settings: ScheduleSettings) -> None:
    now = now_beijing()
    async with get_session() as session:
        task = await get_task(session, snapshot.task_id)
        if task is None:
            return
        ran = outcome.result in RAN_RESULTS
        task.last_run_at = now
        task.last_result = outcome.result
        task.last_error = outcome.error[:240]
        task.lease_until = None
        if outcome.preview:
            task.last_output = outcome.preview[:300]
        if outcome.trace_id:
            task.last_trace_id = outcome.trace_id
        if ran:
            task.run_count = int(task.run_count or 0) + 1
            task.failure_count = 0
            if task.status == STATUS_ACTIVE:
                task.status_reason = ""
        elif outcome.result == RESULT_CREATOR_LEFT:
            pause_task(task, now=now, reason="设任务的人已经不在群里，已自动暂停")
        else:
            task.failure_count = int(task.failure_count or 0) + 1
            if task.failure_count >= settings.max_consecutive_failures:
                pause_task(task, now=now, reason=f"连续失败 {task.failure_count} 次，已自动暂停")
            elif task.status == STATUS_ACTIVE:
                backoff = RETRY_BACKOFF_MINUTES[min(task.failure_count, len(RETRY_BACKOFF_MINUTES)) - 1]
                retry_at = now + timedelta(minutes=backoff)
                # 下一次本来就很近（循环任务）就不额外重试，免得挤在一起。
                if task.next_run_at is None or task.next_run_at > retry_at + timedelta(minutes=backoff):
                    task.next_run_at = retry_at
                    task.status_reason = f"执行失败，{backoff} 分钟后重试"
        if task.status == STATUS_ACTIVE and task.next_run_at is None:
            task.status = STATUS_COMPLETED
            task.status_reason = "已按规则执行完毕" if ran else "执行失败且没有下一次了"
        push_history(
            task,
            {
                "at": format_datetime(now),
                "planned": format_datetime(snapshot.planned_at),
                "result": outcome.result,
                "ms": round(outcome.duration_ms),
                **({"late": snapshot.late_minutes} if snapshot.late_minutes else {}),
                **({"preview": clip(outcome.preview, 160)} if outcome.preview else {}),
                **({"error": clip(outcome.error, 160)} if outcome.error else {}),
                **({"trace": outcome.trace_id} if outcome.trace_id else {}),
            },
            limit=settings.history_limit,
        )
        task.updated_at = now
        task.revision = int(task.revision or 0) + 1
        await session.commit()
    record_run_usage(snapshot.group_id, outcome.result, counts=outcome.result in RAN_RESULTS or outcome.result == RESULT_FAILED)


async def execute_task(task_id: int, *, now: datetime | None = None, force: bool = False) -> RunOutcome | None:
    """执行一条已领取（或 WebUI 手动触发）的任务；返回结果，没执行时返回 None。

    ``force`` 用于 WebUI“立即执行一次”：不看到期时间、不推进规则。
    """

    settings = load_settings()
    current = now or now_beijing()
    if force:
        snapshot, bots = await _prepare_forced(task_id, settings, current)
    else:
        snapshot, bots = await _prepare(task_id, settings, current)
    if snapshot is None or not bots:
        return None
    bot = bots[0]
    started = time.monotonic()
    outcome: RunOutcome
    try:
        member = await _member_info(bot, snapshot.group_id, snapshot.creator_user_id)
        if member is None:
            outcome = RunOutcome(RESULT_CREATOR_LEFT, error="设任务的人已经不在群里")
        elif snapshot.mode == "notify":
            outcome = await _run_notify(bot, snapshot, settings)
        else:
            outcome = await _run_agent(bot, snapshot, member, settings)
    except asyncio.TimeoutError:
        outcome = RunOutcome(RESULT_FAILED, error=f"执行超过 {settings.run_timeout_seconds} 秒")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Agent 定时任务执行失败: %s", task_id)
        outcome = RunOutcome(RESULT_FAILED, error=f"{type(exc).__name__}: {str(exc)[:160]}")
    outcome.duration_ms = (time.monotonic() - started) * 1000
    try:
        await _record(snapshot, outcome, settings)
    except Exception:  # noqa: BLE001
        dbg_exc(f"定时任务 {task_id} 结果写回失败")
    dbg(
        f"群 {snapshot.group_id} 定时任务 {task_id} 第 {snapshot.run_number} 次: "
        f"{outcome.result} {outcome.duration_ms:.0f}ms {outcome.error[:80]}"
    )
    return outcome


async def _prepare_forced(task_id: int, settings: ScheduleSettings, now: datetime) -> tuple[_RunSnapshot | None, list[Any]]:
    del now
    async with get_session() as session:
        task = await get_task(session, task_id)
        if task is None:
            return None, []
        bots = [bot for bot in get_bots().values() if bot is not None]
        bot = pick_bot(bots, (int(task.bot_id) if task.bot_id else None, await _latest_group_bot_id(session, int(task.group_id))))
        if bot is None:
            return None, []
        snapshot = _RunSnapshot(
            task_id=int(task.id),
            group_id=int(task.group_id),
            bot_id=int(task.bot_id) if task.bot_id else None,
            creator_user_id=int(task.creator_user_id),
            creator_name=str(task.creator_name or ""),
            title=str(task.title or ""),
            instruction=str(task.instruction or ""),
            mode=str(task.mode or "agent"),
            mention_creator=bool(task.mention_creator),
            run_number=int(task.run_count or 0) + 1,
            max_runs=int(task.max_runs) if task.max_runs and task.schedule_kind != "once" else None,
            description=str(task.description or ""),
            created_at=task.created_at,
            previous_output=str(task.last_output or ""),
            planned_at=now_beijing().replace(second=0, microsecond=0),
            late_minutes=0,
            once=task.schedule_kind == "once",
        )
        # 手动执行不改规则：单次任务的 next_run_at 保持原样（还会按时再跑）。
    del settings
    return snapshot, [bot]


async def run_due_tasks(*, now: datetime | None = None) -> int:
    """调度入口：领取并执行到期任务，返回执行（含跳过）的任务数。"""

    settings = load_settings()
    current = now or now_beijing()
    try:
        async with get_session() as session:
            claimed = await claim_due(session, now=current, limit=CLAIM_LIMIT)
    except ScheduleUnavailableError:
        return 0
    if not claimed:
        return 0
    by_group: dict[int, list[int]] = defaultdict(list)
    async with get_session() as session:
        for task_id in claimed:
            task = await get_task(session, task_id)
            if task is not None:
                by_group[int(task.group_id)].append(task_id)
    limiter = asyncio.Semaphore(settings.concurrency)

    async def _run_group(task_ids: list[int]) -> None:
        async with limiter:
            for task_id in task_ids:
                try:
                    await execute_task(task_id, now=current)
                except Exception:  # noqa: BLE001
                    logger.exception("Agent 定时任务调度失败: %s", task_id)

    await asyncio.gather(*(_run_group(ids) for ids in by_group.values()))
    return len(claimed)


async def _tick() -> None:
    try:
        await run_due_tasks()
    except Exception:  # noqa: BLE001
        logger.exception("Agent 定时任务 tick 失败")


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
        dbg(f"已注册定时任务 {JOB_ID}(每 {TICK_SECONDS} 秒扫描 Agent 定时任务)")


__all__ = [
    "JOB_ID",
    "RAN_RESULTS",
    "RESULT_LABELS",
    "SKIPPED_RESULTS",
    "DuePlan",
    "RunOutcome",
    "build_wake_turn",
    "execute_task",
    "pick_bot",
    "plan_due",
    "run_due_tasks",
]
