# ruff: noqa: TC001, TID252
"""个人账号（QQ 用户）的自助接口，前缀 ``/me``。

权限边界全部在服务端：

- “本人”只认会话里的 QQ（``Session.user_id``），请求里没有任何可以指定别人的参数。
- 群相关接口先过 ``user_scope.ensure_group_manager``：只有本人当群主/管理员的群；
  写接口还要求 Bot 在线并实时确认身份。
- 群关系洞悉直接复用 ``/agent/groups/{id}/relations*``、``/bonds*`` 只读接口
  （``RelationViewSession`` 放行本人管理的群，投影同访客）。
- 私聊人设只走 ``private_persona`` 的“本人”（``BY_SELF``）路径：管理员锁定、策略
  不开放的字段、每日次数、提示词注入检查都和私聊里让 Agent 改人设一致。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Response, status
from nonebot_plugin_orm import get_session
from sqlalchemy import func, select

from .. import webui_accounts
from ..data_models.bot_group import BotGroup
from ..data_models.bot_user import BotUser
from ..data_models.user_group import UserGroup
from ..permission import FEATURE_REGISTRY
from ..yawn_agent import private_persona
from ..yawn_agent.config_store import get_or_create_config
from ..yawn_agent.conversation import close_group_conversations
from ..yawn_agent.persona import PersonaCard, persona_preset_payloads
from .auth import Session, create_session, login_allowed, record_login_failure
from .config import API_PATH, BASE_PATH, COOKIE_NAME, config
from .deps import UserReadSession, UserWriteSession, ok
from .hub import hub
from .service import group_feature_rows, iso, set_group_feature
from .user_account_models import (
    ChangePasswordBody,
    GroupFeatureBody,
    PersonaFieldsBody,
    PersonaPickBody,
)
from .user_scope import ensure_group_manager, managed_group_rows

router = APIRouter(prefix=f"{API_PATH}/me")

# 群管理员在网页上关闭这些功能前，前端会再确认一次。
_GROUP_FEATURE_HINTS: dict[str, str] = {
    "group_agent": "关闭后 Bot 在本群不再以 Agent 身份聊天，进行中的对话会结束。",
}


def _uid(session: Any) -> int:
    # user_read_session 已保证 role == "user" 且 user_id 不为空
    return int(session.user_id)


# ── 本人 ────────────────────────────────────────────────────────────────


@router.get("")
async def get_me(session: UserReadSession) -> dict[str, Any]:
    user_id = _uid(session)
    async with get_session() as db:
        user = await db.get(BotUser, user_id)
        groups = await managed_group_rows(db, user_id)
        group_count = int(
            await db.scalar(
                select(func.count())
                .select_from(UserGroup)
                .where(UserGroup.user_id == user_id)
            )
            or 0
        )
    account = webui_accounts.get_account(user_id)
    persona = _persona_brief(user_id)
    return ok(
        {
            "userId": str(user_id),
            "nickname": user.nickname if user is not None else None,
            "account": account.as_dict() if account is not None else None,
            "managedGroupCount": len(groups),
            "groupCount": group_count,
            "persona": persona,
            "sessionExpiresAt": session.expires_at,
        }
    )


def _persona_brief(user_id: int) -> dict[str, Any] | None:
    try:
        state = private_persona.state_payload(user_id)
    except Exception:  # noqa: BLE001
        return None
    return {
        "sourceLabel": state["sourceLabel"],
        "summary": state["summary"],
        "name": state["effectiveCard"].get("name"),
        "locked": state["locked"],
    }


@router.post("/password")
async def change_password(
    body: ChangePasswordBody, response: Response, session: UserWriteSession
) -> dict[str, Any]:
    """改密码：要先输对旧密码；改完别的设备全部下线，当前页面换新会话继续用。"""

    user_id = _uid(session)
    limit_key = f"change-qq:{user_id}"
    if not login_allowed(limit_key, max_failures=5, window=900):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "尝试次数过多，请稍后再试"
        )
    encoded = webui_accounts.password_hash_for(user_id)
    valid = encoded is not None and await asyncio.to_thread(
        webui_accounts.verify_password, body.current_password, encoded
    )
    if not valid:
        record_login_failure(limit_key, window=900)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "当前密码不正确")
    if body.current_password == body.new_password:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "新密码和当前密码一样")
    try:
        account = await asyncio.to_thread(
            webui_accounts.set_password, user_id, body.new_password, source="webui"
        )
    except webui_accounts.AccountError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    new_session = _reissue_cookie(response, user_id, account.credential_version)
    return ok(
        {
            "account": account.as_dict(),
            "csrfToken": new_session.csrf_token,
            "expiresAt": new_session.expires_at,
        }
    )


@router.post("/logout-all")
async def logout_everywhere(
    response: Response, session: UserWriteSession
) -> dict[str, Any]:
    """退出所有设备（包括这一台）。"""

    webui_accounts.revoke_sessions(_uid(session))
    response.delete_cookie(COOKIE_NAME, path=BASE_PATH)
    return ok({"authenticated": False})


def _reissue_cookie(response: Response, user_id: int, version: int) -> Session:
    cookie, new_session = create_session(
        role="user", credential_version=version, user_id=user_id
    )
    response.set_cookie(
        COOKIE_NAME,
        cookie,
        max_age=config.webui_session_ttl_hours * 3600,
        httponly=True,
        secure=config.webui_cookie_secure,
        samesite="strict",
        path=BASE_PATH,
    )
    return new_session


# ── 管理的群 ────────────────────────────────────────────────────────────


@router.get("/groups")
async def list_my_groups(session: UserReadSession) -> dict[str, Any]:
    """本人在数据库里是群主/管理员的群（打开某个群时再实时确认）。"""

    user_id = _uid(session)
    async with get_session() as db:
        rows = await managed_group_rows(db, user_id)
        group_ids = [int(group.group_id) for _membership, group in rows]
        counts: dict[int, int] = {}
        if group_ids:
            counts = {
                int(group_id): int(count)
                for group_id, count in (
                    await db.execute(
                        select(UserGroup.group_id, func.count())
                        .where(UserGroup.group_id.in_(group_ids))
                        .group_by(UserGroup.group_id)
                    )
                ).all()
            }
        result = []
        for membership, group in rows:
            features = await group_feature_rows(db, int(group.group_id))
            result.append(
                {
                    "groupId": str(group.group_id),
                    "groupName": group.group_name,
                    "role": membership.role,
                    "memberCount": counts.get(int(group.group_id), 0),
                    "lastActiveAt": iso(group.last_active_at),
                    "enabledFeatures": sum(1 for row in features if row["effective"]),
                    "featureCount": len(features),
                    "agentEnabled": next(
                        (
                            row["effective"]
                            for row in features
                            if row["key"] == "group_agent"
                        ),
                        True,
                    ),
                }
            )
    return ok(result)


@router.get("/groups/{group_id}")
async def get_my_group(group_id: int, session: UserReadSession) -> dict[str, Any]:
    user_id = _uid(session)
    async with get_session() as db:
        access = await ensure_group_manager(db, group_id, user_id, write=False)
        group = await db.get(BotGroup, group_id)
        member_count = int(
            await db.scalar(
                select(func.count())
                .select_from(UserGroup)
                .where(UserGroup.group_id == group_id)
            )
            or 0
        )
        features = await group_feature_rows(db, group_id)
    return ok(
        {
            "groupId": str(group_id),
            "groupName": group.group_name if group is not None else None,
            "memberCount": member_count,
            "lastActiveAt": iso(group.last_active_at) if group is not None else None,
            "role": access["role"],
            "verified": access["verified"],
            "features": features,
            "featureHints": _GROUP_FEATURE_HINTS,
        }
    )


@router.patch("/groups/{group_id}/features/{feature}")
async def patch_my_group_feature(
    group_id: int, feature: str, body: GroupFeatureBody, session: UserWriteSession
) -> dict[str, Any]:
    """群管理员在网页上开关本群功能；和管理台、群里 /面板 写的是同一份群功能设置。"""

    if feature not in FEATURE_REGISTRY:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "功能不存在")
    user_id = _uid(session)
    async with get_session() as db:
        await ensure_group_manager(db, group_id, user_id, write=True)
        await set_group_feature(db, group_id, feature, body.override)
        if feature == "group_agent":
            agent_config = await get_or_create_config(db, group_id)
            if agent_config is None:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE, "Agent 配置暂时不可用"
                )
            agent_config.enabled = body.override is not False
        await db.commit()
        rows = await group_feature_rows(db, group_id)
    if feature == "group_agent" and body.override is False:
        close_group_conversations(group_id, reason="群管理员在 WebUI 关闭了 Agent")
    await hub.notify_change("group_feature", f"{group_id}:{feature}", group_id=group_id)
    if feature == "group_agent":
        await hub.notify_change("agent_config", str(group_id), group_id=group_id)
    return ok(next(row for row in rows if row["key"] == feature))


# ── 私聊人设 ────────────────────────────────────────────────────────────


def _self_options() -> dict[str, Any]:
    policy = private_persona.load_policy()
    return {
        "presets": persona_preset_payloads() if policy.allow_presets else [],
        "library": private_persona.library_choices() if policy.allow_library else [],
        "fields": {
            key: private_persona.FIELD_LABELS[key]
            for key in policy.self_fields
            if key in private_persona.FIELD_LABELS
        },
    }


def _persona_state(user_id: int) -> dict[str, Any]:
    state = private_persona.state_payload(user_id)
    policy = private_persona.load_policy()
    # 历史里的 actor 是管理端会话指纹，本人用不上，去掉。
    state["history"] = [
        {key: value for key, value in item.items() if key != "actor"}
        for item in state.get("history") or []
    ]
    state.pop("updatedBy", None)
    can_edit = policy.enabled and policy.self_edit and not state["locked"]
    state["canEdit"] = can_edit
    state["blockedReason"] = (
        None
        if can_edit
        else "管理员锁定了你的私聊人设"
        if state["locked"]
        else "管理员没有开放自己改私聊人设"
    )
    state["editsLeftToday"] = (
        max(0, policy.self_edit_daily - int(state["selfEditsToday"])) if can_edit else 0
    )
    state["options"] = _self_options()
    return state


def _persona_guard() -> None:
    policy = private_persona.load_policy()
    if not policy.enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "管理员关闭了私聊人设自定义")
    if not policy.self_edit:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "管理员没有开放自己改私聊人设")


async def _persona_written(user_id: int, result: dict[str, Any]) -> dict[str, Any]:
    await hub.notify_change("private_agent_persona", str(user_id))
    await hub.notify_change("user_private_agent", str(user_id))
    return ok({**result, "state": _persona_state(user_id)})


def _edit_error(exc: private_persona.PersonaEditError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


_FIELD_ALIASES: dict[str, str] = {
    **{name: name for name in PersonaCard.model_fields},
    **{
        str(field.serialization_alias): name
        for name, field in PersonaCard.model_fields.items()
        if field.serialization_alias
    },
}


@router.get("/persona")
async def get_my_persona(session: UserReadSession) -> dict[str, Any]:
    return ok(_persona_state(_uid(session)))


@router.patch("/persona")
async def patch_my_persona(
    body: PersonaFieldsBody, session: UserWriteSession
) -> dict[str, Any]:
    """只改给出的字段（字段名可用 camelCase 或 snake_case）。"""

    _persona_guard()
    user_id = _uid(session)
    fields: dict[str, Any] = {}
    for key, value in body.fields.items():
        name = _FIELD_ALIASES.get(key)
        if name is None or name not in private_persona.FIELD_LABELS:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"没有这个人设字段：{key}"
            )
        fields[name] = value
    try:
        result = private_persona.patch_fields(
            user_id, fields, by=private_persona.BY_SELF, actor="webui"
        )
    except private_persona.PersonaEditError as exc:
        raise _edit_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "人设内容格式不对"
        ) from exc
    return await _persona_written(user_id, result)


@router.post("/persona/preset")
async def use_my_persona_preset(
    body: PersonaPickBody, session: UserWriteSession
) -> dict[str, Any]:
    _persona_guard()
    user_id = _uid(session)
    try:
        result = private_persona.use_preset(
            user_id, body.id, by=private_persona.BY_SELF, actor="webui"
        )
    except private_persona.PersonaEditError as exc:
        raise _edit_error(exc) from exc
    return await _persona_written(user_id, result)


@router.post("/persona/template")
async def use_my_persona_template(
    body: PersonaPickBody, session: UserWriteSession
) -> dict[str, Any]:
    _persona_guard()
    if not body.id.isdigit():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "人设库里没有这个人设")
    user_id = _uid(session)
    try:
        result = private_persona.use_template(
            user_id, int(body.id), by=private_persona.BY_SELF, actor="webui"
        )
    except private_persona.PersonaEditError as exc:
        raise _edit_error(exc) from exc
    return await _persona_written(user_id, result)


@router.post("/persona/reset")
async def reset_my_persona(session: UserWriteSession) -> dict[str, Any]:
    _persona_guard()
    user_id = _uid(session)
    try:
        result = private_persona.reset(
            user_id, by=private_persona.BY_SELF, actor="webui"
        )
    except private_persona.PersonaEditError as exc:
        raise _edit_error(exc) from exc
    return await _persona_written(user_id, result)


@router.post("/persona/undo")
async def undo_my_persona(session: UserWriteSession) -> dict[str, Any]:
    _persona_guard()
    user_id = _uid(session)
    try:
        result = private_persona.undo(
            user_id, by=private_persona.BY_SELF, actor="webui"
        )
    except private_persona.PersonaEditError as exc:
        raise _edit_error(exc) from exc
    return await _persona_written(user_id, result)


__all__ = ["router"]
