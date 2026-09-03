# ruff: noqa: PERF203,TC002,TC003
"""单进程 WebSocket 状态广播。"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import WebSocket
from nonebot import logger

_GROUP_ID_ENTITY_RESOURCES = frozenset(
    {
        "agent_config",
        "agent_persona",
        "agent_memory",
        "agent_group_data",
    }
)
_GROUP_PREFIX_ENTITY_RESOURCES = frozenset(
    {
        "agent_member_data",
        "agent_privacy",
        "group_feature",
    }
)


def _legacy_group_scope(resource: str, entity_id: str | None) -> str | None:
    """兼容尚未迁移的调用点；对外协议始终使用独立 scope/entityId。"""

    if not entity_id:
        return None
    if resource in _GROUP_ID_ENTITY_RESOURCES:
        return entity_id
    if resource in _GROUP_PREFIX_ENTITY_RESOURCES:
        return entity_id.split(":", 1)[0]
    return None


class WebUIHub:
    def __init__(self) -> None:
        self._clients: dict[WebSocket, asyncio.Lock] = {}
        self._task: asyncio.Task[None] | None = None
        self._last_snapshot = ""

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients[websocket] = asyncio.Lock()

    def disconnect(self, websocket: WebSocket) -> None:
        self._clients.pop(websocket, None)

    async def send(self, websocket: WebSocket, payload: dict[str, Any]) -> None:
        lock = self._clients.get(websocket)
        if lock is None:
            return
        async with lock:
            await websocket.send_json(payload)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        stale: list[WebSocket] = []
        for websocket in tuple(self._clients):
            try:
                await self.send(websocket, payload)
            except Exception:  # noqa: BLE001
                logger.debug("WebUI 广播失败，将断开该客户端", exc_info=True)
                stale.append(websocket)
        for websocket in stale:
            self.disconnect(websocket)

    async def notify_change(
        self,
        resource: str,
        entity_id: str | None = None,
        *,
        group_id: int | str | None = None,
    ) -> None:
        """广播资源变化；scope 描述查询所属范围，entityId 描述具体实体。

        group_id 与 entity_id 分开，避免过去把群号、关系 ID、用户 ID 都塞进
        resourceId 后让前端无法判断一次变化是否属于当前页面。
        """

        resolved_group_id = (
            str(group_id)
            if group_id is not None
            else _legacy_group_scope(resource, entity_id)
        )
        scope = {"groupId": resolved_group_id} if resolved_group_id is not None else None
        await self.broadcast(
            {
                "type": "entity.changed",
                "data": {
                    "resource": resource,
                    "scope": scope,
                    "entityId": entity_id,
                },
            }
        )

    def start(self, snapshot: Callable[[], Awaitable[dict[str, Any]]]) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(snapshot))

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self, snapshot: Callable[[], Awaitable[dict[str, Any]]]) -> None:
        while True:
            try:
                current = await snapshot()
                canonical = json.dumps(current, sort_keys=True, default=str)
                if self._last_snapshot and canonical != self._last_snapshot:
                    await self.broadcast({"type": "overview.updated", "data": current})
                self._last_snapshot = canonical
            except Exception:  # noqa: BLE001
                logger.debug("WebUI 快照轮询失败，等待下一轮", exc_info=True)
            await asyncio.sleep(5)


hub = WebUIHub()
