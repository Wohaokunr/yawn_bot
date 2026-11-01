# ruff: noqa: TID252
"""WebUI 路由共享的请求依赖与响应信封。

app.py 与 games.py 等兄弟路由模块都从这里导入，避免
路由模块之间互相引用造成循环导入。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status
from nonebot_plugin_orm import get_session

from .. import webui_accounts
from .auth import Session, require_csrf, require_session
from .config import config
from .guest_access import guest_group_is_allowed, guest_session_is_current
from .user_scope import ensure_group_manager


def ok(data: Any, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"data": data, "meta": meta or {}}


def user_session_is_current(session: Session) -> bool:
    """个人账号会话：功能开着、账号还在且没停用、密码没改过（版本号对得上）。"""

    if session.role != "user" or session.user_id is None:
        return False
    if not config.webui_user_login_enabled:
        return False
    current = webui_accounts.session_version(session.user_id)
    return current is not None and current == session.credential_version


async def authenticated_session(request: Request) -> Session:
    """任意角色的有效会话（运维 / 访客 / 个人账号），各自校验是否已被撤销。"""

    session = require_session(request)
    if session.role == "guest" and not await guest_session_is_current(
        session.credential_version
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "访客会话已失效")
    if session.role == "user" and not user_session_is_current(session):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已失效，请重新登录")
    return session


async def authenticated_write_session(request: Request) -> Session:
    session = await authenticated_session(request)
    require_csrf(request, session)
    return session


def admin_read_session(request: Request) -> Session:
    session = require_session(request)
    if session.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要管理员权限")
    return session


def admin_write_session(request: Request) -> Session:
    session = admin_read_session(request)
    require_csrf(request, session)
    return session


async def user_read_session(request: Request) -> Session:
    """只给个人账号会话；接口里的“本人”一律取会话里的 QQ，不信任请求参数。"""

    session = await authenticated_session(request)
    if session.role != "user" or session.user_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "仅个人账号可访问")
    return session


async def user_write_session(request: Request) -> Session:
    session = await user_read_session(request)
    require_csrf(request, session)
    return session


async def require_relation_view_access(group_id: int, request: Request) -> Session:
    """群关系只读接口：管理员任意群、访客被授权的群、个人账号自己管理的群。

    个人账号和访客一样只拿精简投影（见 ``agent_route_helpers.is_guest_view``）。
    """

    session = await authenticated_session(request)
    if session.role == "admin":
        return session
    if session.role == "guest":
        if await guest_group_is_allowed(group_id):
            return session
        raise HTTPException(status.HTTP_403_FORBIDDEN, "该群聊未向访客开放")
    if session.role == "user" and session.user_id is not None:
        async with get_session() as db:
            await ensure_group_manager(db, group_id, session.user_id, write=False)
        return session
    raise HTTPException(status.HTTP_403_FORBIDDEN, "无权查看这个群")


async def require_group_view_access(group_id: int, request: Request) -> Session:
    """Allow admins to view any group and guests only allowlisted groups."""
    session = await authenticated_session(request)
    if session.role == "admin":
        return session
    if session.role == "guest" and await guest_group_is_allowed(group_id):
        return session
    raise HTTPException(status.HTTP_403_FORBIDDEN, "该群聊未向访客开放")


AuthenticatedSession = Annotated[Session, Depends(authenticated_session)]
AuthenticatedWriteSession = Annotated[Session, Depends(authenticated_write_session)]
AdminReadSession = Annotated[Session, Depends(admin_read_session)]
AdminWriteSession = Annotated[Session, Depends(admin_write_session)]
GroupViewSession = Annotated[Session, Depends(require_group_view_access)]
RelationViewSession = Annotated[Session, Depends(require_relation_view_access)]
UserReadSession = Annotated[Session, Depends(user_read_session)]
UserWriteSession = Annotated[Session, Depends(user_write_session)]


def page_params(page: int, page_size: int) -> tuple[int, int]:
    return max(1, page), max(1, min(page_size, 100))
