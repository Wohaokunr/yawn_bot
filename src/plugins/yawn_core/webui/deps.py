"""WebUI 路由共享的请求依赖与响应信封。

app.py 与 games.py 等兄弟路由模块都从这里导入，避免
路由模块之间互相引用造成循环导入。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Request

from .auth import Session, require_csrf, require_session


def ok(data: Any, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"data": data, "meta": meta or {}}


def write_session(request: Request) -> Session:
    session = require_session(request)
    require_csrf(request, session)
    return session


ReadSession = Annotated[Session, Depends(require_session)]
WriteSession = Annotated[Session, Depends(write_session)]


def page_params(page: int, page_size: int) -> tuple[int, int]:
    return max(1, page), max(1, min(page_size, 100))
