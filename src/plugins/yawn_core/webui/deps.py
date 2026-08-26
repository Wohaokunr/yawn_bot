"""WebUI 路由共享的请求依赖与响应信封。

app.py 与 games.py 等兄弟路由模块都从这里导入，避免
路由模块之间互相引用造成循环导入。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status

from .auth import Session, require_csrf, require_session
from .guest_access import guest_group_is_allowed, guest_session_is_current


def ok(data: Any, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"data": data, "meta": meta or {}}


async def authenticated_session(request: Request) -> Session:
    session = require_session(request)
    if session.role == "guest" and not await guest_session_is_current(
        session.credential_version
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "访客会话已失效")
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


def page_params(page: int, page_size: int) -> tuple[int, int]:
    return max(1, page), max(1, min(page_size, 100))
