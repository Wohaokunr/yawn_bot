# ruff: noqa: E501, TRY003, RUF001, PLR0913, TID252
"""Agent 定时任务的数据库读写：创建校验、查询、改状态、领取到期任务、记录运行。

工具 handler、调度器和 WebUI 共用这里，不各自拼 SQL。表还没迁移时所有入口抛
:class:`ScheduleUnavailableError`，调用方据此降级（工具如实说“还没启用”，调度器
直接跳过），绝不影响普通对话。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import OperationalError, ProgrammingError

from ..data_models.agent_scheduled_task import AgentScheduledTask
from .persona import text_overrides_policy
from .schedule_spec import (
    ScheduleError,
    ScheduleSpec,
    describe,
    format_datetime,
    min_gap_minutes,
    next_fire,
    normalize_spec,
    spec_from_dict,
    upcoming,
)

if TYPE_CHECKING:
    from .schedule_policy import ScheduleSettings

STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"
STATUSES = (STATUS_ACTIVE, STATUS_PAUSED, STATUS_COMPLETED, STATUS_CANCELLED, STATUS_FAILED)
OPEN_STATUSES = frozenset({STATUS_ACTIVE, STATUS_PAUSED})
MODES = ("agent", "notify")
MAX_TITLE_CHARS = 40
MAX_INSTRUCTION_CHARS = 600
MAX_PREVIEW_CHARS = 300
MAX_ERROR_CHARS = 240
LEASE_SECONDS = 15 * 60
_WHITESPACE_RE = re.compile(r"[ \t\r\f\v]+")
_CQ_RE = re.compile(r"\[CQ:", re.IGNORECASE)


class ScheduleUnavailableError(RuntimeError):
    """定时任务表不存在（迁移还没执行）。"""


class ScheduleRuleError(ValueError):
    """创建/修改被规则或限额拒绝；``code``/``hint`` 原样回给模型或 WebUI。"""

    def __init__(self, message: str, *, code: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint


def is_missing_table(exc: BaseException) -> bool:
    text = str(exc).lower()
    return isinstance(exc, (OperationalError, ProgrammingError)) and (
        "no such table" in text or "does not exist" in text or "undefined table" in text
    )


def _clean_text(value: Any, *, limit: int) -> str:
    lines = [_WHITESPACE_RE.sub(" ", line).strip() for line in str(value or "").splitlines()]
    return "\n".join(line for line in lines if line)[:limit]


def clean_title(value: Any, instruction: str) -> str:
    title = _clean_text(value, limit=MAX_TITLE_CHARS).replace("\n", " ")
    if title:
        return title
    first = instruction.split("\n", 1)[0]
    return (first[: MAX_TITLE_CHARS - 1] + "…") if len(first) > MAX_TITLE_CHARS else first


def validate_instruction(value: Any) -> str:
    instruction = _clean_text(value, limit=MAX_INSTRUCTION_CHARS + 1)
    if not instruction:
        raise ScheduleRuleError("任务内容不能为空", code="missing_instruction", hint="instruction 写清楚到点要做什么")
    if len(instruction) > MAX_INSTRUCTION_CHARS:
        raise ScheduleRuleError(
            f"任务内容最多 {MAX_INSTRUCTION_CHARS} 字",
            code="instruction_too_long",
            hint="只写要做的事和要点，资料可以到点再查",
        )
    if text_overrides_policy(instruction) or _CQ_RE.search(instruction) or "@全体" in instruction:
        raise ScheduleRuleError(
            "任务内容里有试图覆盖系统规则、伪造消息或 @全体成员 的内容",
            code="unsafe_instruction",
            hint="定时任务只能是普通的群聊任务",
        )
    return instruction


@dataclass(slots=True)
class TaskDraft:
    """创建/修改任务的输入（已经过工具参数或 WebUI 模型的基本校验）。"""

    instruction: str
    schedule: dict[str, Any]
    title: str = ""
    mode: str = "agent"
    max_runs: int | None = None
    mention_creator: bool = True


@dataclass(slots=True)
class ValidatedDraft:
    instruction: str
    title: str
    mode: str
    spec: ScheduleSpec
    max_runs: int | None
    mention_creator: bool
    first_run: datetime
    upcoming: list[datetime] = field(default_factory=list)
    description: str = ""


def validate_draft(
    draft: TaskDraft,
    *,
    settings: ScheduleSettings,
    now: datetime,
    anchor: datetime | None = None,
    run_count: int = 0,
) -> ValidatedDraft:
    """把草稿规范化并按策略校验；不碰数据库。"""

    instruction = validate_instruction(draft.instruction)
    mode = str(draft.mode or "agent").strip().lower()
    if mode not in MODES:
        raise ScheduleRuleError(f"不认识的模式：{mode}", code="bad_mode", hint="mode 取 agent（到点由你执行任务）或 notify（到点直接发这句提醒）")
    if mode == "agent" and not settings.allow_agent_mode:
        raise ScheduleRuleError(
            "管理员只开放了“到点直接发提醒”，到点再执行任务的模式没开",
            code="agent_mode_closed",
            hint="改用 mode=notify，instruction 写成要发的提醒原话",
        )
    try:
        spec = normalize_spec(draft.schedule, now=now)
    except ScheduleError as exc:
        raise ScheduleRuleError(str(exc), code=exc.code, hint=exc.hint) from exc
    max_runs = int(draft.max_runs) if draft.max_runs else None
    if max_runs is not None and not 1 <= max_runs <= 10000:  # noqa: PLR2004
        raise ScheduleRuleError("max_runs 取值 1–10000", code="bad_number")
    if spec.kind == "once":
        max_runs = 1
    anchor = anchor or now
    remaining = None if max_runs is None else max(max_runs - run_count, 0)
    fires = upcoming(spec, now, count=5, anchor=anchor, remaining_runs=remaining)
    if not fires:
        raise ScheduleRuleError(
            "按这个规则以后不会再触发（时间已经过去、超出截止时间或日期不存在）",
            code="never_fires",
            hint="检查 at/start_at/end_at 是否是将来的北京时间",
        )
    horizon = now + timedelta(days=settings.max_horizon_days)
    if fires[0] > horizon:
        raise ScheduleRuleError(
            f"第一次触发在 {format_datetime(fires[0])}，超过了最远 {settings.max_horizon_days} 天的限制",
            code="too_far",
            hint="改近一点的时间",
        )
    gap = min_gap_minutes(spec, now, anchor=anchor)
    if spec.recurring and gap is not None and gap < settings.min_interval_minutes:
        raise ScheduleRuleError(
            f"触发太频繁：两次之间最短只有 {gap:g} 分钟，本群要求至少 {settings.min_interval_minutes} 分钟",
            code="too_frequent",
            hint=f"把间隔调到 {settings.min_interval_minutes} 分钟以上",
        )
    return ValidatedDraft(
        instruction=instruction,
        title=clean_title(draft.title, instruction),
        mode=mode,
        spec=spec,
        max_runs=max_runs,
        mention_creator=bool(draft.mention_creator),
        first_run=fires[0],
        upcoming=fires,
        description=describe(spec, max_runs=max_runs if spec.recurring else None)[:200],
    )


async def count_open(session: Any, group_id: int, *, creator_user_id: int | None = None) -> int:
    stmt = select(func.count()).select_from(AgentScheduledTask).where(
        AgentScheduledTask.group_id == group_id,
        AgentScheduledTask.status.in_(tuple(OPEN_STATUSES)),
    )
    if creator_user_id is not None:
        stmt = stmt.where(AgentScheduledTask.creator_user_id == creator_user_id)
    try:
        return int(await session.scalar(stmt) or 0)
    except (OperationalError, ProgrammingError) as exc:
        if is_missing_table(exc):
            raise ScheduleUnavailableError("定时任务表还没有迁移") from exc
        raise


async def find_duplicate(
    session: Any, group_id: int, creator_user_id: int, validated: ValidatedDraft
) -> AgentScheduledTask | None:
    """同一个人在同一群里重复让 Bot 设一模一样的任务时，复用已有的那条。"""

    rows = (
        await session.execute(
            select(AgentScheduledTask).where(
                AgentScheduledTask.group_id == group_id,
                AgentScheduledTask.creator_user_id == creator_user_id,
                AgentScheduledTask.status == STATUS_ACTIVE,
                AgentScheduledTask.instruction == validated.instruction,
            )
        )
    ).scalars().all()
    target = validated.spec.as_dict()
    for row in rows:
        if dict(row.schedule or {}) == target and row.mode == validated.mode:
            return row
    return None


async def create_task(
    session: Any,
    *,
    group_id: int,
    bot_id: int | None,
    creator_user_id: int,
    creator_name: str,
    draft: TaskDraft,
    settings: ScheduleSettings,
    now: datetime,
    source_kind: str = "agent",
    enforce_quota: bool = True,
) -> tuple[AgentScheduledTask, bool, ValidatedDraft]:
    """校验并新建任务；返回 (任务, 是否复用了已有任务, 校验结果)。只 flush 不 commit。"""

    validated = validate_draft(draft, settings=settings, now=now)
    if enforce_quota:
        group_open = await count_open(session, group_id)
        if group_open >= settings.max_active_per_group:
            raise ScheduleRuleError(
                f"本群已经有 {group_open} 个定时任务，达到上限 {settings.max_active_per_group}",
                code="group_limit",
                hint="先取消不需要的任务（list_scheduled_tasks 查看）",
            )
    duplicate = await find_duplicate(session, group_id, creator_user_id, validated)
    if duplicate is not None:
        return duplicate, True, validated
    if enforce_quota:
        mine = await count_open(session, group_id, creator_user_id=creator_user_id)
        if mine >= settings.max_active_per_member:
            raise ScheduleRuleError(
                f"你在本群已经有 {mine} 个定时任务，每人最多 {settings.max_active_per_member} 个",
                code="member_limit",
                hint="先取消自己不需要的任务",
            )
    task = AgentScheduledTask(
        group_id=group_id,
        bot_id=bot_id,
        creator_user_id=creator_user_id,
        creator_name=_clean_text(creator_name, limit=64).replace("\n", " "),
        title=validated.title,
        instruction=validated.instruction,
        mode=validated.mode,
        schedule=validated.spec.as_dict(),
        schedule_kind=validated.spec.kind,
        description=validated.description,
        status=STATUS_ACTIVE,
        status_reason="",
        mention_creator=validated.mention_creator,
        max_runs=validated.max_runs,
        run_count=0,
        failure_count=0,
        skip_count=0,
        next_run_at=validated.first_run,
        last_result="",
        last_error="",
        last_output="",
        history=[],
        source_kind=source_kind,
        revision=0,
        created_at=now,
        updated_at=now,
    )
    session.add(task)
    await session.flush()
    return task, False, validated


def task_spec(task: AgentScheduledTask) -> ScheduleSpec | None:
    return spec_from_dict(dict(task.schedule or {}))


def remaining_runs(task: AgentScheduledTask) -> int | None:
    if task.max_runs is None:
        return None
    return max(int(task.max_runs) - int(task.run_count or 0), 0)


def following_run(task: AgentScheduledTask, after: datetime) -> datetime | None:
    """``after`` 之后的下一次（考虑剩余次数）；规则坏了或已经跑完返回 None。"""

    spec = task_spec(task)
    if spec is None:
        return None
    left = remaining_runs(task)
    if left is not None and left <= 0:
        return None
    return next_fire(spec, after, anchor=task.created_at)


def preview_runs(task: AgentScheduledTask, *, count: int = 5) -> list[datetime]:
    spec = task_spec(task)
    if spec is None or task.status != STATUS_ACTIVE:
        return []
    left = remaining_runs(task)
    start = task.next_run_at
    if start is None:
        return []
    result = [start]
    if left is not None:
        left -= 1
    fires = upcoming(spec, start, count=count - 1, anchor=task.created_at, remaining_runs=left)
    return [*result, *fires][:count]


async def get_task(session: Any, task_id: int, *, group_id: int | None = None) -> AgentScheduledTask | None:
    try:
        task = await session.get(AgentScheduledTask, int(task_id))
    except (OperationalError, ProgrammingError) as exc:
        if is_missing_table(exc):
            raise ScheduleUnavailableError("定时任务表还没有迁移") from exc
        raise
    if task is None or (group_id is not None and int(task.group_id) != int(group_id)):
        return None
    return task


async def list_tasks(
    session: Any,
    *,
    group_id: int | None = None,
    statuses: tuple[str, ...] | None = None,
    creator_user_id: int | None = None,
    query: str = "",
    mode: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[AgentScheduledTask], int]:
    conditions: list[Any] = []
    if group_id is not None:
        conditions.append(AgentScheduledTask.group_id == group_id)
    if statuses:
        conditions.append(AgentScheduledTask.status.in_(statuses))
    if creator_user_id is not None:
        conditions.append(AgentScheduledTask.creator_user_id == creator_user_id)
    if mode:
        conditions.append(AgentScheduledTask.mode == mode)
    text = str(query or "").strip()
    if text:
        pattern = f"%{text[:60]}%"
        conditions.append(
            or_(
                AgentScheduledTask.title.like(pattern),
                AgentScheduledTask.instruction.like(pattern),
                AgentScheduledTask.creator_name.like(pattern),
            )
        )
    try:
        total = int(
            await session.scalar(select(func.count()).select_from(AgentScheduledTask).where(*conditions)) or 0
        )
        rows = (
            await session.execute(
                select(AgentScheduledTask)
                .where(*conditions)
                .order_by(
                    # 有效任务在前，其余按最近变化。
                    (AgentScheduledTask.status != STATUS_ACTIVE).asc(),
                    AgentScheduledTask.next_run_at.is_(None).asc(),
                    AgentScheduledTask.next_run_at.asc(),
                    AgentScheduledTask.updated_at.desc(),
                    AgentScheduledTask.id.desc(),
                )
                .limit(max(1, min(int(limit), 200)))
                .offset(max(0, int(offset)))
            )
        ).scalars().all()
    except (OperationalError, ProgrammingError) as exc:
        if is_missing_table(exc):
            raise ScheduleUnavailableError("定时任务表还没有迁移") from exc
        raise
    return list(rows), total


def _touch(task: AgentScheduledTask, now: datetime) -> None:
    task.updated_at = now
    task.revision = int(task.revision or 0) + 1


def pause_task(task: AgentScheduledTask, *, now: datetime, reason: str = "") -> None:
    task.status = STATUS_PAUSED
    task.status_reason = reason[:120]
    task.lease_until = None
    _touch(task, now)


def resume_task(task: AgentScheduledTask, *, now: datetime) -> datetime | None:
    """恢复暂停的任务：从现在起重新算下一次；已经没有下一次时标记完成。"""

    task.failure_count = 0
    upcoming_run = following_run(task, now)
    if upcoming_run is None:
        task.status = STATUS_COMPLETED
        task.status_reason = "恢复时规则已经没有下一次"
        task.next_run_at = None
    else:
        task.status = STATUS_ACTIVE
        task.status_reason = ""
        task.next_run_at = upcoming_run
    task.lease_until = None
    _touch(task, now)
    return upcoming_run


def cancel_task(task: AgentScheduledTask, *, now: datetime, by_user_id: int | None, reason: str = "") -> None:
    task.status = STATUS_CANCELLED
    task.status_reason = reason[:120]
    task.next_run_at = None
    task.lease_until = None
    task.cancelled_at = now
    task.cancelled_by = by_user_id
    _touch(task, now)


def apply_draft(task: AgentScheduledTask, validated: ValidatedDraft, *, now: datetime) -> None:
    """把修改后的规则/内容写回任务；运行次数保留，下一次从现在重新算。"""

    task.instruction = validated.instruction
    task.title = validated.title
    task.mode = validated.mode
    task.schedule = validated.spec.as_dict()
    task.schedule_kind = validated.spec.kind
    task.description = validated.description
    task.mention_creator = validated.mention_creator
    task.max_runs = validated.max_runs if validated.spec.recurring else int(task.run_count or 0) + 1
    if task.status in {STATUS_COMPLETED, STATUS_FAILED}:
        task.status = STATUS_ACTIVE
        task.status_reason = ""
    if task.status == STATUS_ACTIVE:
        task.next_run_at = validated.first_run
    task.failure_count = 0
    task.lease_until = None
    _touch(task, now)


async def claim_due(session: Any, *, now: datetime, limit: int = 10) -> list[int]:
    """领取到期任务：写执行租约并提交；返回领到的任务 id（按到期先后）。

    条件更新带上 revision，多实例/并发 tick 同时看到同一行时只有一方领到。
    """

    try:
        rows = (
            await session.execute(
                select(AgentScheduledTask.id, AgentScheduledTask.revision)
                .where(
                    AgentScheduledTask.status == STATUS_ACTIVE,
                    AgentScheduledTask.next_run_at.is_not(None),
                    AgentScheduledTask.next_run_at <= now,
                    or_(AgentScheduledTask.lease_until.is_(None), AgentScheduledTask.lease_until < now),
                )
                .order_by(AgentScheduledTask.next_run_at.asc(), AgentScheduledTask.id.asc())
                .limit(max(1, limit))
            )
        ).all()
    except (OperationalError, ProgrammingError) as exc:
        if is_missing_table(exc):
            raise ScheduleUnavailableError("定时任务表还没有迁移") from exc
        raise
    claimed: list[int] = []
    lease = now + timedelta(seconds=LEASE_SECONDS)
    for task_id, revision in rows:
        result = await session.execute(
            update(AgentScheduledTask)
            .where(AgentScheduledTask.id == task_id, AgentScheduledTask.revision == revision)
            .values(lease_until=lease, revision=int(revision or 0) + 1)
        )
        if int(getattr(result, "rowcount", 0) or 0) == 1:
            claimed.append(int(task_id))
    await session.commit()
    return claimed


def push_history(task: AgentScheduledTask, entry: dict[str, Any], *, limit: int) -> None:
    history = [item for item in list(task.history or []) if isinstance(item, dict)]
    task.history = [entry, *history][: max(1, limit)]


def clip(text: Any, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def task_public_dict(task: AgentScheduledTask, *, now: datetime | None = None, preview_count: int = 0) -> dict[str, Any]:
    """给工具结果用的紧凑视图（模型看）；不含历史详情和内部字段。"""

    data: dict[str, Any] = {
        "task_id": int(task.id),
        "title": task.title,
        "mode": task.mode,
        "schedule": task.description,
        "status": task.status,
        "creator": task.creator_name or str(task.creator_user_id),
        "creator_user_id": int(task.creator_user_id),
        "runs": int(task.run_count or 0),
    }
    if task.max_runs and task.schedule_kind != "once":
        data["max_runs"] = int(task.max_runs)
    if task.next_run_at is not None and task.status == STATUS_ACTIVE:
        data["next_run"] = format_datetime(task.next_run_at)
    if task.last_run_at is not None:
        data["last_run"] = format_datetime(task.last_run_at)
        data["last_result"] = task.last_result
    if task.status_reason:
        data["status_reason"] = task.status_reason
    data["instruction"] = clip(task.instruction, 160)
    if preview_count and now is not None:
        data["upcoming"] = [format_datetime(item) for item in preview_runs(task, count=preview_count)]
    return data


__all__ = [
    "LEASE_SECONDS",
    "MAX_INSTRUCTION_CHARS",
    "MODES",
    "OPEN_STATUSES",
    "STATUSES",
    "STATUS_ACTIVE",
    "STATUS_CANCELLED",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_PAUSED",
    "ScheduleRuleError",
    "ScheduleUnavailableError",
    "TaskDraft",
    "ValidatedDraft",
    "apply_draft",
    "cancel_task",
    "claim_due",
    "clip",
    "count_open",
    "create_task",
    "find_duplicate",
    "following_run",
    "get_task",
    "is_missing_table",
    "list_tasks",
    "pause_task",
    "preview_runs",
    "push_history",
    "remaining_runs",
    "resume_task",
    "task_public_dict",
    "task_spec",
    "validate_draft",
    "validate_instruction",
]
