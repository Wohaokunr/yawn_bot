# ruff: noqa: FAST002, TC001, TID252, PLR0913, PLR0917, PLR0912, C901
"""Bot 视角关系（Bot 与每位群成员的关系）WebUI 接口。

成员之间的关系边在 ``agent_relation_routes``；这里是 ``AgentBotBond`` 的
列表、摘要、详情、人工修改与重置。访客只能看阶段、标签与熟悉程度，
看不到备注、边界、称呼和证据；隐私退出成员在所有接口里都不出现。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from nonebot_plugin_orm import get_session
from sqlalchemy import func, or_, select
from sqlalchemy.exc import OperationalError, ProgrammingError

from ..data_models.agent_bot_bond import AgentBotBond
from ..data_models.agent_memory import AgentMemory, AgentPrivacy
from ..data_models.bot_user import BotUser
from ..data_models.group_agent_message import GroupAgentMessage
from ..data_models.user_group import UserGroup
from ..yawn_agent.bot_bond import (
    BOND_LIST_FIELDS,
    BOND_LOCKABLE_FIELDS,
    BOND_MEMORY_TYPE,
    BOND_STAGE_LABELS,
    BOND_STAGES,
    BOND_TEXT_FIELDS,
    bond_summary,
    clean_bond_text,
    forget_member_bond,
    recompute,
    store_moment,
)
from ..yawn_agent.context import now_beijing
from .agent_route_helpers import is_guest_view as _is_guest_view
from .config import API_PATH
from .deps import AdminWriteSession, GroupViewSession, ok, page_params
from .hub import hub
from .route_helpers import require_group
from .route_models import BondMomentBody, BondPatchBody
from .service import iso, page_meta

router = APIRouter(prefix=API_PATH)

# 列表排序白名单；id 兜底保证翻页稳定。
_BOND_SORTS: dict[str, Any] = {
    "familiarity": AgentBotBond.familiarity,
    "affinity": AgentBotBond.affinity,
    "trust": AgentBotBond.trust,
    "recent": AgentBotBond.last_interaction_at,
    "since": AgentBotBond.first_interaction_at,
    "days": AgentBotBond.active_days,
}
_EVIDENCE_PREVIEW = 8
_EVIDENCE_CHARS = 120
_MOMENT_LIMIT = 30
_TABLE_MISSING_HINT = "Bot 关系表尚未迁移，请先执行 nb orm upgrade"


def _table_missing(error: Exception) -> bool:
    text = str(error).lower()
    return "no such table" in text or "does not exist" in text


async def _opted_out(db: Any, group_id: int) -> set[int]:
    return set(
        (
            await db.execute(
                select(AgentPrivacy.user_id).where(
                    AgentPrivacy.group_id == group_id,
                    AgentPrivacy.opted_out.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )


async def _bond_clauses(db: Any, group_id: int) -> list[Any]:
    clauses: list[Any] = [AgentBotBond.group_id == group_id]
    hidden = await _opted_out(db, group_id)
    if hidden:
        clauses.append(AgentBotBond.user_id.not_in(hidden))
    return clauses


async def _member_names(db: Any, group_id: int, user_ids: set[int]) -> dict[int, str]:
    if not user_ids:
        return {}
    rows = (
        await db.execute(
            select(UserGroup.user_id, UserGroup.group_nickname, BotUser.nickname)
            .outerjoin(BotUser, BotUser.user_id == UserGroup.user_id)
            .where(UserGroup.group_id == group_id, UserGroup.user_id.in_(user_ids))
        )
    ).all()
    return {
        int(user_id): str(group_nickname or nickname or user_id)
        for user_id, group_nickname, nickname in rows
    }


async def _name_matches(db: Any, group_id: int, keyword: str) -> list[int]:
    pattern = f"%{keyword}%"
    rows = (
        await db.execute(
            select(UserGroup.user_id)
            .outerjoin(BotUser, BotUser.user_id == UserGroup.user_id)
            .where(
                UserGroup.group_id == group_id,
                or_(
                    BotUser.nickname.ilike(pattern),
                    UserGroup.group_nickname.ilike(pattern),
                ),
            )
            .limit(200)
        )
    ).scalars()
    return [int(user_id) for user_id in rows]


async def _moment_counts(db: Any, group_id: int, user_ids: set[int]) -> dict[int, int]:
    if not user_ids:
        return {}
    now = now_beijing()
    rows = (
        await db.execute(
            select(AgentMemory.subject_user_id, func.count(AgentMemory.id))
            .where(
                AgentMemory.group_id == group_id,
                AgentMemory.memory_type == BOND_MEMORY_TYPE,
                AgentMemory.subject_user_id.in_(user_ids),
                (AgentMemory.expires_at.is_(None) | (AgentMemory.expires_at >= now)),
            )
            .group_by(AgentMemory.subject_user_id)
        )
    ).all()
    return {int(user_id): int(count) for user_id, count in rows}


def serialize_bond(row: AgentBotBond, *, name: str, moments: int = 0) -> dict[str, Any]:
    """管理员投影：统一口径的摘要 + 来源、锁定、计数与时间。"""

    now = now_beijing()
    summary = bond_summary(row, now)
    return {
        "id": str(row.id),
        "userId": str(row.user_id),
        "botId": str(row.bot_id),
        "name": name,
        "stage": summary["stage"],
        "stageLabel": summary["stage_label"],
        "familiarity": summary["familiarity"],
        "rawFamiliarity": round(float(row.familiarity or 0.0), 4),
        "affinity": summary["affinity"],
        "trust": summary["trust"],
        "label": summary["label"],
        "memberCallsBot": summary["member_calls_bot"],
        "botCallsMember": summary["bot_calls_member"],
        "tone": summary["tone"],
        "note": summary["note"],
        "insideJokes": summary["inside_jokes"],
        "boundaries": summary["boundaries"],
        "lockedFields": list(row.locked_fields or []),
        "sourceKind": row.source_kind,
        "memberToBotCount": int(row.member_to_bot_count or 0),
        "botToMemberCount": int(row.bot_to_member_count or 0),
        "exchangeCount": int(row.exchange_count or 0),
        "positiveCount": int(row.positive_count or 0),
        "negativeCount": int(row.negative_count or 0),
        "activeDays": int(row.active_days or 0),
        "knownDays": summary["known_days"],
        "idleDays": summary["idle_days"],
        "evidenceCount": len(row.evidence_message_ids or []),
        "momentCount": moments,
        "firstInteractionAt": iso(row.first_interaction_at),
        "lastInteractionAt": iso(row.last_interaction_at),
        "lastModelUpdateAt": iso(row.last_model_update_at),
        "updatedAt": iso(row.updated_at),
    }


def serialize_guest_bond(row: AgentBotBond, *, name: str) -> dict[str, Any]:
    """访客投影：只给阶段、标签与熟悉程度，不暴露称呼、边界、备注和证据。"""

    summary = bond_summary(row, now_beijing())
    return {
        "id": str(row.id),
        "userId": str(row.user_id),
        "name": name,
        "stage": summary["stage"],
        "stageLabel": summary["stage_label"],
        "familiarity": summary["familiarity"],
        "label": summary["label"],
        "activeDays": int(row.active_days or 0),
        "lastInteractionAt": iso(row.last_interaction_at),
    }


def _stage_clause(stage: str) -> Any:
    # 阶段落库值在互动后才刷新，读侧还会因久未互动而降级；筛选按落库值，
    # 每日衰减任务会把它们追平。
    return AgentBotBond.stage == stage


@router.get("/agent/groups/{group_id}/bonds")
async def list_bonds(
    group_id: int,
    _session: GroupViewSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=100),
    search: str = Query(default="", max_length=24),
    stage: str = Query(default="", max_length=16),
    source: str = Query(default="", max_length=16),
    sort: str = Query(default="familiarity", max_length=16),
    order: str = Query(default="desc", max_length=4),
) -> dict[str, Any]:
    page, page_size = page_params(page, page_size)
    guest = _is_guest_view(_session)
    async with get_session() as db:
        await require_group(db, group_id)
        clauses = await _bond_clauses(db, group_id)
        if stage in BOND_STAGES:
            clauses.append(_stage_clause(stage))
        if source and not guest:
            clauses.append(AgentBotBond.source_kind == source)
        keyword = search.strip()
        if keyword.isdigit():
            clauses.append(AgentBotBond.user_id == int(keyword))
        elif keyword:
            matched = await _name_matches(db, group_id, keyword)
            label_hit = AgentBotBond.label.ilike(f"%{keyword}%")
            clauses.append(
                or_(AgentBotBond.user_id.in_(matched), label_hit)
                if matched
                else label_hit
            )
        column = _BOND_SORTS.get(sort, AgentBotBond.familiarity)
        ordering = (
            [column.asc(), AgentBotBond.id.asc()]
            if order == "asc"
            else [column.desc(), AgentBotBond.id.desc()]
        )
        try:
            total = int(
                await db.scalar(
                    select(func.count()).select_from(AgentBotBond).where(*clauses)
                )
                or 0
            )
        except (OperationalError, ProgrammingError) as error:
            if not _table_missing(error):
                raise
            return ok([], {**page_meta(page, page_size, 0), "tableReady": False})
        rows = list(
            (
                await db.execute(
                    select(AgentBotBond)
                    .where(*clauses)
                    .order_by(*ordering)
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        user_ids = {int(row.user_id) for row in rows}
        names = await _member_names(db, group_id, user_ids)
        moments = {} if guest else await _moment_counts(db, group_id, user_ids)
    data = [
        serialize_guest_bond(row, name=names.get(int(row.user_id), str(row.user_id)))
        if guest
        else serialize_bond(
            row,
            name=names.get(int(row.user_id), str(row.user_id)),
            moments=moments.get(int(row.user_id), 0),
        )
        for row in rows
    ]
    return ok(data, {**page_meta(page, page_size, total), "tableReady": True})


@router.get("/agent/groups/{group_id}/bonds/summary")
async def bond_overview(group_id: int, _session: GroupViewSession) -> dict[str, Any]:
    """阶段分布、平均熟悉/好感、近 7 天互动人数与最亲近的几位。"""

    now = now_beijing()
    async with get_session() as db:
        await require_group(db, group_id)
        clauses = await _bond_clauses(db, group_id)
        try:
            rows = list(
                (await db.execute(select(AgentBotBond).where(*clauses))).scalars().all()
            )
        except (OperationalError, ProgrammingError) as error:
            if not _table_missing(error):
                raise
            return ok({"tableReady": False, "total": 0, "stageCounts": []})
        summaries = [(row, bond_summary(row, now)) for row in rows]
        top = sorted(
            summaries,
            key=lambda item: (-item[1]["familiarity"], -item[1]["affinity"]),
        )[:5]
        names = await _member_names(db, group_id, {int(row.user_id) for row, _ in top})
    stage_counts = dict.fromkeys(BOND_STAGES, 0)
    for _row, summary in summaries:
        stage_counts[summary["stage"]] = stage_counts.get(summary["stage"], 0) + 1
    total = len(summaries)
    week_ago = now - timedelta(days=7)
    result: dict[str, Any] = {
        "tableReady": True,
        "total": total,
        "stageCounts": [
            {
                "stage": stage,
                "label": BOND_STAGE_LABELS[stage],
                "count": stage_counts[stage],
            }
            for stage in BOND_STAGES
        ],
        "avgFamiliarity": (
            round(sum(item["familiarity"] for _row, item in summaries) / total, 4)
            if total
            else None
        ),
        "avgAffinity": (
            round(sum(item["affinity"] for _row, item in summaries) / total, 4)
            if total
            else None
        ),
        "activeWeekCount": sum(
            1
            for row, _summary in summaries
            if row.last_interaction_at is not None
            and row.last_interaction_at >= week_ago
        ),
        "top": [
            {
                "userId": str(row.user_id),
                "name": names.get(int(row.user_id), str(row.user_id)),
                "stage": summary["stage"],
                "stageLabel": summary["stage_label"],
                "familiarity": summary["familiarity"],
                "label": summary["label"],
            }
            for row, summary in top
        ],
    }
    if not _is_guest_view(_session):
        result["strainedCount"] = stage_counts.get("strained", 0)
        result["lockedCount"] = sum(1 for row in rows if row.locked_fields)
        result["sourceCounts"] = [
            {"source": source, "count": count}
            for source, count in sorted(
                {
                    source: sum(1 for row in rows if row.source_kind == source)
                    for source in {str(row.source_kind) for row in rows}
                }.items()
            )
        ]
    return ok(result)


async def _load_bond(
    db: Any, group_id: int, user_id: int, bot_id: int | None
) -> AgentBotBond:
    if user_id in await _opted_out(db, group_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "该成员已退出 Agent 记忆")
    statement = select(AgentBotBond).where(
        AgentBotBond.group_id == group_id, AgentBotBond.user_id == user_id
    )
    if bot_id is not None:
        statement = statement.where(AgentBotBond.bot_id == bot_id)
    try:
        rows = list(
            (await db.execute(statement.order_by(AgentBotBond.id))).scalars().all()
        )
    except (OperationalError, ProgrammingError) as error:
        if not _table_missing(error):
            raise
        raise HTTPException(status.HTTP_409_CONFLICT, _TABLE_MISSING_HINT) from None
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bot 与该成员还没有关系记录")
    # 多 Bot 同群时不指定 botId 就取互动最多的那条。
    return max(rows, key=lambda row: float(row.familiarity or 0.0))


@router.get("/agent/groups/{group_id}/bonds/{user_id}")
async def get_bond(
    group_id: int,
    user_id: int,
    _session: GroupViewSession,
    bot_id: int | None = Query(default=None, alias="botId", gt=0),
) -> dict[str, Any]:
    guest = _is_guest_view(_session)
    async with get_session() as db:
        await require_group(db, group_id)
        row = await _load_bond(db, group_id, user_id, bot_id)
        names = await _member_names(db, group_id, {user_id})
        name = names.get(user_id, str(user_id))
        if guest:
            return ok(serialize_guest_bond(row, name=name))
        now = now_beijing()
        moments = list(
            (
                await db.execute(
                    select(AgentMemory)
                    .where(
                        AgentMemory.group_id == group_id,
                        AgentMemory.subject_user_id == user_id,
                        AgentMemory.memory_type == BOND_MEMORY_TYPE,
                        (
                            AgentMemory.expires_at.is_(None)
                            | (AgentMemory.expires_at >= now)
                        ),
                    )
                    .order_by(AgentMemory.updated_at.desc(), AgentMemory.id.desc())
                    .limit(_MOMENT_LIMIT)
                )
            )
            .scalars()
            .all()
        )
        evidence_ids = list(row.evidence_message_ids or [])[-_EVIDENCE_PREVIEW:]
        evidence_rows = (
            list(
                (
                    await db.execute(
                        select(GroupAgentMessage)
                        .where(
                            GroupAgentMessage.group_id == group_id,
                            GroupAgentMessage.id.in_(evidence_ids),
                        )
                        .order_by(GroupAgentMessage.id.desc())
                    )
                )
                .scalars()
                .all()
            )
            if evidence_ids
            else []
        )
        payload = serialize_bond(row, name=name, moments=len(moments))
        payload["moments"] = [
            {
                "id": str(item.id),
                "content": str(item.content or ""),
                "sourceKind": item.source_kind,
                "accessCount": int(item.access_count or 0),
                "createdAt": iso(item.created_at),
                "updatedAt": iso(item.updated_at),
                "expiresAt": iso(item.expires_at),
            }
            for item in moments
        ]
        # 证据原文可能已过保留期被清理，找不到的 id 只计数不报错。
        payload["evidence"] = [
            {
                "id": str(item.id),
                "text": str(item.normalized_text or "")[:_EVIDENCE_CHARS],
                "receivedAt": iso(item.received_at),
            }
            for item in evidence_rows
        ]
        payload["evidenceMissing"] = len(evidence_ids) - len(evidence_rows)
    return ok(payload)


@router.patch("/agent/groups/{group_id}/bonds/{user_id}")
async def patch_bond(
    group_id: int,
    user_id: int,
    body: BondPatchBody,
    _session: AdminWriteSession,
    bot_id: int | None = Query(default=None, alias="botId", gt=0),
) -> dict[str, Any]:
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "没有可更新的字段")
    unlock = {str(item) for item in updates.pop("unlock", None) or []}
    unknown = unlock - BOND_LOCKABLE_FIELDS
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"不能解锁的字段：{sorted(unknown)}"
        )
    async with get_session() as db:
        await require_group(db, group_id)
        row = await _load_bond(db, group_id, user_id, bot_id)
        locked = [str(item) for item in row.locked_fields or [] if item not in unlock]
        for name in BOND_TEXT_FIELDS:
            if name not in updates:
                continue
            raw = str(updates[name] or "").strip()
            if raw:
                cleaned = clean_bond_text(raw, name)
                if cleaned is None:
                    raise HTTPException(
                        status.HTTP_422_UNPROCESSABLE_ENTITY,
                        f"「{name}」含联系方式、敏感信息或像指令的内容，不能保存",
                    )
                setattr(row, name, cleaned)
            else:
                setattr(row, name, "")
            locked.append(name)
        for name in BOND_LIST_FIELDS:
            if name not in updates:
                continue
            values: list[str] = []
            for item in updates[name] or []:
                if not str(item).strip():
                    continue
                cleaned = clean_bond_text(item, name)
                if cleaned is None:
                    raise HTTPException(
                        status.HTTP_422_UNPROCESSABLE_ENTITY,
                        f"「{item}」含联系方式、敏感信息或像指令的内容，不能保存",
                    )
                if cleaned not in values:
                    values.append(cleaned)
            setattr(row, name, values)
            locked.append(name)
        for name in ("affinity", "trust"):
            if updates.get(name) is not None:
                setattr(row, name, round(float(updates[name]), 4))
                locked.append(name)
        row.locked_fields = [
            name for name in dict.fromkeys(locked) if name in BOND_LOCKABLE_FIELDS
        ]
        row.source_kind = "manual"
        recompute(row, now=now_beijing())
        await db.commit()
        await db.refresh(row)
        names = await _member_names(db, group_id, {user_id})
        moments = await _moment_counts(db, group_id, {user_id})
        result = serialize_bond(
            row, name=names.get(user_id, str(user_id)), moments=moments.get(user_id, 0)
        )
    await hub.notify_change("agent_bond", str(user_id), group_id=group_id)
    return ok(result)


@router.delete("/agent/groups/{group_id}/bonds/{user_id}")
async def reset_bond(
    group_id: int, user_id: int, _session: AdminWriteSession
) -> dict[str, Any]:
    """重置 Bot 与该成员的关系：删除关系行与非手工的共同经历，之后重新积累。"""

    async with get_session() as db:
        await require_group(db, group_id)
        try:
            exists = await db.scalar(
                select(func.count(AgentBotBond.id)).where(
                    AgentBotBond.group_id == group_id, AgentBotBond.user_id == user_id
                )
            )
        except (OperationalError, ProgrammingError) as error:
            if not _table_missing(error):
                raise
            raise HTTPException(status.HTTP_409_CONFLICT, _TABLE_MISSING_HINT) from None
        if not exists:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Bot 与该成员还没有关系记录")
        # 管理员主动重置连手工内容一起清掉：先把 manual 改回 auto 再走统一删除。
        rows = list(
            (
                await db.execute(
                    select(AgentBotBond).where(
                        AgentBotBond.group_id == group_id,
                        AgentBotBond.user_id == user_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.source_kind = "auto"
        await db.flush()
        result = await forget_member_bond(db, group_id=group_id, user_id=user_id)
        await db.commit()
    await hub.notify_change("agent_bond", str(user_id), group_id=group_id)
    return ok(result)


@router.post("/agent/groups/{group_id}/bonds/{user_id}/moments")
async def add_bond_moment(
    group_id: int,
    user_id: int,
    body: BondMomentBody,
    _session: AdminWriteSession,
    bot_id: int | None = Query(default=None, alias="botId", gt=0),
) -> dict[str, Any]:
    async with get_session() as db:
        await require_group(db, group_id)
        row = await _load_bond(db, group_id, user_id, bot_id)
        moment = await store_moment(
            db,
            group_id=group_id,
            bot_id=int(row.bot_id),
            user_id=user_id,
            content=body.content,
            evidence=[],
            now=now_beijing(),
            source_kind="manual",
            salience=0.8,
        )
        if moment is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "这段经历含联系方式、敏感信息或像指令的内容，不能保存",
            )
        await db.commit()
        await db.refresh(moment)
        result = {"id": str(moment.id), "content": str(moment.content or "")}
    await hub.notify_change("agent_bond", str(user_id), group_id=group_id)
    return ok(result)


__all__ = ["router", "serialize_bond", "serialize_guest_bond"]
