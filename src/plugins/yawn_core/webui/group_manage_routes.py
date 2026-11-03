# ruff: noqa: TID252, FAST002, FBT001, FBT002, PLR0913, PLR0917
"""群管理员在网页上让 Bot 代办群管理：``/me/groups/{group_id}/manage``。

和私聊工具 ``group_manage`` 共用 ``yawn_agent.group_manage``：同一套双重实时校验
（他是群主/管理员、Bot 也是群主/管理员）、处置边界、每日额度和操作记录。网页没有
“下一条消息再确认”，改由前端弹窗确认，踢人和改群名还要求请求里带 ``confirm: true``。

运维管理员（role=admin）只能只读查看策略和操作记录（``/groups/{id}/manage/audit``），
不能以 Bot 身份代办——代办必须有一个真实的群主/管理员 QQ 作为发起人。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from nonebot import get_bots
from nonebot_plugin_orm import get_session
from pydantic import BaseModel, ConfigDict, Field

from ..data_models.bot_group import BotGroup
from ..yawn_agent import group_manage as gm
from .config import API_PATH
from .deps import AdminReadSession, UserReadSession, UserWriteSession, ok
from .hub import hub
from .user_scope import ensure_group_manager

router = APIRouter(prefix=API_PATH)

_HIGH_RISK = frozenset({"kick", "rename"})


class _Body(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class ManageActionBody(_Body):
    action: str = Field(min_length=1, max_length=20)
    member: str | None = Field(default=None, max_length=40)
    minutes: int | None = Field(default=None, ge=1, le=43200)
    enable: bool | None = None
    content: str | None = Field(default=None, max_length=gm.ANNOUNCE_MAX_CHARS + 50)
    text: str | None = Field(default=None, max_length=gm.SEND_MAX_CHARS + 50)
    pinned: bool = False
    confirm_required: bool = Field(default=False, alias="confirmRequired")
    reject: bool = False
    notice_id: str | None = Field(default=None, alias="noticeId", max_length=128)
    message_id: str | None = Field(default=None, alias="messageId", max_length=24)
    reason: str | None = Field(default=None, max_length=gm.REASON_MAX_CHARS)
    confirm: bool = False

    def args(self) -> dict[str, Any]:
        return {
            "member": self.member,
            "minutes": self.minutes,
            "enable": self.enable,
            "content": self.content,
            "text": self.text,
            "pinned": self.pinned,
            "confirm_required": self.confirm_required,
            "reject": self.reject,
            "notice_id": self.notice_id,
            "message_id": self.message_id,
            "reason": self.reason,
        }


class ManagePolicyBody(_Body):
    enabled: bool | None = None
    private_enabled: bool | None = Field(default=None, alias="privateEnabled")
    webui_enabled: bool | None = Field(default=None, alias="webuiEnabled")
    notify_group: bool | None = Field(default=None, alias="notifyGroup")
    allow_relay: bool | None = Field(default=None, alias="allowRelay")
    max_mute_minutes: int | None = Field(
        default=None, ge=1, le=43200, alias="maxMuteMinutes"
    )
    disabled_actions: list[str] | None = Field(
        default=None, alias="disabledActions", max_length=40
    )
    admin_actions: list[str] | None = Field(
        default=None, alias="adminActions", max_length=40
    )
    confirm_actions: list[str] | None = Field(
        default=None, alias="confirmActions", max_length=40
    )


def _bot() -> Any | None:
    try:
        bots = get_bots()
    except (RuntimeError, ValueError):
        return None
    return next(iter(bots.values()), None)


def _http(exc: gm.ManageError) -> HTTPException:
    message = str(exc)
    if exc.hint:
        message = f"{message}；{exc.hint}"
    return HTTPException(exc.status, message)


def _policy_payload(policy: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": policy["enabled"],
        "privateEnabled": policy["private_enabled"],
        "webuiEnabled": policy["webui_enabled"],
        "notifyGroup": policy["notify_group"],
        "allowRelay": policy.get("allow_relay", True),
        "maxMuteMinutes": policy["max_mute_minutes"],
        "maxMuteCeiling": gm.max_mute_minutes_ceiling(),
        "disabledActions": policy["disabled_actions"],
        "adminActions": policy["admin_actions"],
        "confirmActions": policy["confirm_actions"],
        "updatedAt": policy.get("updated_at"),
        "updatedBy": policy.get("updated_by"),
    }


def _audit_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "at": row.get("at"),
        "actorId": row.get("actor_id"),
        "actorRole": row.get("actor_role"),
        "source": row.get("source"),
        "action": row.get("action"),
        "label": row.get("label"),
        "result": row.get("result"),
        "targetId": row.get("target_id"),
        "targetName": row.get("target_name"),
        "detail": row.get("detail"),
        "error": row.get("error"),
        "reason": row.get("reason"),
    }


def _member_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "userId": row["user_id"],
        "name": row["name"],
        "nickname": row.get("nickname") or "",
        "card": row.get("card") or "",
        "role": row.get("role") or "member",
        "title": row.get("title"),
        "mutedUntil": row.get("muted_until"),
    }


def _catalog(access: gm.Access | None, policy: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for key in sorted(gm.WRITE_ACTIONS, key=list(gm.ACTIONS).index):
        spec = gm.ACTIONS[key]
        rows.append(
            {
                "key": key,
                "label": spec.label,
                "risk": spec.risk,
                "target": spec.target,
                "botOwner": spec.bot_owner,
                "confirm": key in policy["confirm_actions"],
                "adminAllowed": key in policy["admin_actions"],
                "disabled": key in policy["disabled_actions"],
                "blockedReason": gm.action_block_reason(access, key)
                if access is not None
                else "Bot 不在线或身份没确认",
            }
        )
    return rows


async def _verified(group_id: int, user_id: int) -> gm.Access:
    if not gm.webui_enabled():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "管理员没有开放网页群管理")
    async with get_session() as db:
        await ensure_group_manager(db, group_id, user_id, write=False)
    try:
        return await gm.verify(_bot(), group_id, user_id, source="webui")
    except gm.ManageError as exc:
        raise _http(exc) from exc


async def _group_name(group_id: int) -> str:
    async with get_session() as db:
        group = await db.get(BotGroup, group_id)
    return (
        str(group.group_name)
        if group is not None and group.group_name
        else f"群 {group_id}"
    )


# ── 状态与查询 ──────────────────────────────────────────────────────────


@router.get("/me/groups/{group_id}/manage")
async def manage_status(group_id: int, session: UserReadSession) -> dict[str, Any]:
    """本群代办状态：两边身份、能做哪些、策略、今天用了几次。Bot 不在线时只给策略。"""

    user_id = int(session.user_id)  # pyright: ignore[reportArgumentType]
    if not gm.webui_enabled():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "管理员没有开放网页群管理")
    async with get_session() as db:
        await ensure_group_manager(db, group_id, user_id, write=False)
    policy = gm.policy_for(group_id)
    access: gm.Access | None = None
    problem: str | None = None
    try:
        access = await gm.verify(_bot(), group_id, user_id, source="webui")
    except gm.ManageError as exc:
        problem = f"{exc}；{exc.hint}" if exc.hint else str(exc)
    return ok(
        {
            "available": access is not None,
            "problem": problem,
            "yourRole": access.user_role if access else None,
            "botRole": access.bot_role if access else None,
            "isOwner": bool(access and access.is_owner),
            "allowedActions": access.allowed_actions() if access else [],
            "actions": _catalog(access, policy),
            "policy": _policy_payload(policy),
            "usedToday": gm.used_today(user_id),
            "dailyLimit": gm.daily_limit(),
            "sent": [
                {
                    "messageId": item.get("message_id"),
                    "kind": item.get("kind"),
                    "at": item.get("at"),
                    "preview": item.get("preview"),
                }
                for item in gm.sent_messages(group_id)[:20]
            ],
        }
    )


@router.get("/me/groups/{group_id}/manage/members")
async def manage_members(
    group_id: int,
    session: UserReadSession,
    query: str | None = Query(default=None, max_length=40),
    limit: int = Query(default=60, ge=1, le=500),
    refresh: bool = False,
) -> dict[str, Any]:
    access = await _verified(group_id, int(session.user_id))  # pyright: ignore[reportArgumentType]
    try:
        members = await gm.member_list(access.bot, group_id, refresh=refresh)
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "读取群成员失败") from exc
    rows = gm.search_members(members, query, limit=limit)
    return ok(
        [_member_payload(row) for row in rows],
        {
            "total": len(members),
            "muted": sum(1 for row in members if row.get("muted_until")),
        },
    )


@router.get("/me/groups/{group_id}/manage/notices")
async def manage_notices(group_id: int, session: UserReadSession) -> dict[str, Any]:
    access = await _verified(group_id, int(session.user_id))  # pyright: ignore[reportArgumentType]
    try:
        notices = await gm.list_notices(access)
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "读取群公告失败") from exc
    return ok(
        [
            {
                "noticeId": str(item.get("notice_id") or ""),
                "senderId": str(item.get("sender_id") or ""),
                "publishTime": item.get("publish_time"),
                "content": item.get("content") or "",
            }
            for item in notices
        ]
    )


@router.get("/me/groups/{group_id}/manage/audit")
async def manage_audit(
    group_id: int,
    session: UserReadSession,
    limit: int = Query(default=50, ge=1, le=300),
    action: str | None = Query(default=None, max_length=20),
    result: Literal["success", "failed", "denied"] | None = None,
    mine: bool = False,
) -> dict[str, Any]:
    user_id = int(session.user_id)  # pyright: ignore[reportArgumentType]
    async with get_session() as db:
        await ensure_group_manager(db, group_id, user_id, write=False)
    rows = gm.audit_rows(
        group_id,
        limit=limit,
        actor_id=user_id if mine else None,
        action=action or None,
        result=result,
    )
    return ok([_audit_payload(row) for row in rows])


@router.get("/groups/{group_id}/manage/audit")
async def admin_manage_audit(
    group_id: int,
    _session: AdminReadSession,
    limit: int = Query(default=100, ge=1, le=300),
) -> dict[str, Any]:
    """运维管理员只读：这个群的群管代办策略与最近操作。"""

    return ok(
        {
            "policy": _policy_payload(gm.policy_for(group_id)),
            "rows": [
                _audit_payload(row) for row in gm.audit_rows(group_id, limit=limit)
            ],
        }
    )


# ── 执行 ────────────────────────────────────────────────────────────────


@router.post("/me/groups/{group_id}/manage/preview")
async def manage_preview(
    group_id: int, body: ManageActionBody, session: UserWriteSession
) -> dict[str, Any]:
    """只校验并说清要做什么（给确认弹窗用），不执行、不扣次数。"""

    if body.action not in gm.WRITE_ACTIONS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "没有这个操作")
    access = await _verified(group_id, int(session.user_id))  # pyright: ignore[reportArgumentType]
    try:
        summary, _normalized = await gm.describe(
            access, body.action, body.args(), await _group_name(group_id)
        )
    except gm.ManageError as exc:
        raise _http(exc) from exc
    spec = gm.ACTIONS[body.action]
    return ok({"summary": summary, "risk": spec.risk, "label": spec.label})


@router.post("/me/groups/{group_id}/manage/actions")
async def manage_action(
    group_id: int, body: ManageActionBody, session: UserWriteSession
) -> dict[str, Any]:
    user_id = int(session.user_id)  # pyright: ignore[reportArgumentType]
    if body.action not in gm.WRITE_ACTIONS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "没有这个操作")
    if body.action in _HIGH_RISK and not body.confirm:
        raise HTTPException(
            status.HTTP_428_PRECONDITION_REQUIRED, "这个操作不可撤回，需要再确认一次"
        )
    access = await _verified(group_id, user_id)
    try:
        summary, normalized = await gm.describe(
            access, body.action, body.args(), await _group_name(group_id)
        )
        result = await gm.execute(access, body.action, normalized, source="webui")
    except gm.ManageError as exc:
        if exc.code == "permission_denied":
            gm.deny(group_id, user_id, body.action, "webui", str(exc), access.user_role)
        raise _http(exc) from exc
    await hub.notify_change("group_manage", str(group_id), group_id=group_id)
    return ok({"summary": summary, "result": result})


@router.put("/me/groups/{group_id}/manage/policy")
async def manage_policy(
    group_id: int, body: ManagePolicyBody, session: UserWriteSession
) -> dict[str, Any]:
    """改本群代办策略：只有群主本人（实时确认）能改。"""

    user_id = int(session.user_id)  # pyright: ignore[reportArgumentType]
    if not gm.webui_enabled():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "管理员没有开放网页群管理")
    async with get_session() as db:
        await ensure_group_manager(db, group_id, user_id, write=True)
    bot = _bot()
    role = await gm.live_role(bot, group_id, user_id) if bot is not None else None
    if role != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只有群主能改群管代办策略")
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    for key in ("disabled_actions", "admin_actions", "confirm_actions"):
        unknown = [
            item for item in changes.get(key, []) if item not in gm.WRITE_ACTIONS
        ]
        if unknown:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"没有这些操作：{'、'.join(unknown)}",
            )
    if (
        "max_mute_minutes" in changes
        and changes["max_mute_minutes"] > gm.max_mute_minutes_ceiling()
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"禁言上限不能超过 {gm.format_minutes(gm.max_mute_minutes_ceiling())}",
        )
    if not changes:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "没有要改的内容")
    policy = gm.set_policy(group_id, changes, actor=str(user_id))
    gm.audit(
        group_id,
        actor_id=user_id,
        actor_role="owner",
        source="webui",
        action="manage_policy",
        result="success",
        detail="、".join(sorted(changes)),
    )
    await hub.notify_change("group_manage", str(group_id), group_id=group_id)
    return ok(_policy_payload(policy))


__all__ = ["router"]
