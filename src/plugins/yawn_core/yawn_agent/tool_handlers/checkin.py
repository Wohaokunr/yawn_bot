# ruff: noqa: TID252, E501
"""签到工具族：群里让 Agent 替调用者本人签到、查签到与好感现状。

签到逻辑只在 ``checkin_service``，这里只做调用者校验、功能开关和结果投影。
工具只作用于**调用者本人**、**当前群**，不提供 user_id / group_id 参数。
"""

from __future__ import annotations

from typing import Any

from ... import affinity
from ...checkin_service import (
    POINTS_MAX,
    POINTS_MIN,
    checkin_overview,
    group_checkin_board,
    perform_checkin,
)
from ...permission import check_feature_permission
from ..context import now_beijing
from ..log import dbg
from ..tool_args import ERROR_PERMISSION, ToolError
from ..tool_execution import ToolExecutionContext, ToolHandlerResult

FAMILY = "checkin"
NAMES = frozenset(["checkin_for_me", "get_my_checkin"])


def _actor(context: ToolExecutionContext) -> int:
    actor = int(context.actor_user_id or 0)
    if actor <= 0:
        raise ToolError(
            "签到只能替当前发言的成员本人做",
            code=ERROR_PERMISSION,
            hint="主动发言或定时任务里不能替人签到",
        )
    return actor


async def _ensure_feature(context: ToolExecutionContext, actor: int) -> None:
    if not await check_feature_permission(
        actor, context.group_id, "checkin", context.session
    ):
        raise ToolError(
            "本群的签到功能已关闭，或你的签到被管理员关掉了",
            code=ERROR_PERMISSION,
            hint="如实告诉成员签到功能没开",
        )


async def handle(
    name: str, args: dict[str, Any], context: ToolExecutionContext
) -> ToolHandlerResult:
    _ = args
    session = context.session
    if session is None:
        raise ToolError("签到需要数据库会话", code=ERROR_PERMISSION)
    actor = _actor(context)
    await _ensure_feature(context, actor)
    now = now_beijing()
    if name == "checkin_for_me":
        from random import randint

        result = await perform_checkin(
            session,
            group_id=context.group_id,
            user_id=actor,
            now=now,
            points_reward=randint(POINTS_MIN, POINTS_MAX),
        )
        dbg(
            f"群 {context.group_id} checkin_for_me: 成员 {actor} status={result.status} "
            f"streak={result.streak_days}"
        )
        return ToolHandlerResult(
            result.public_dict(),
            mutated_db=result.ok,
            needs_commit=result.ok,
            immediate_commit=result.ok,
        )
    statuses = await checkin_overview(
        session, actor, group_ids=[context.group_id], now=now
    )
    snapshot = await affinity.snapshot(session, actor, now=now)
    board = await group_checkin_board(session, context.group_id, now=now, limit=50)
    rank = next((place for user_id, place in board if user_id == actor), None)
    payload: dict[str, Any] = {
        "checkin": statuses[0].public_dict()
        if statuses
        else {"checked_today": False, "total_days": 0},
        "relationship_with_you": snapshot.public_dict(),
        "checked_in_today_count": len(board),
    }
    if rank is not None:
        payload["rank_today"] = rank
    return ToolHandlerResult(payload)
