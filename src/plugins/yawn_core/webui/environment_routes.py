# ruff: noqa: TC001,TID252
"""Environment editing and LLM connection-test endpoints."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from typing import Any

from fastapi import APIRouter, HTTPException, status
from nonebot import logger
from pydantic import ValidationError

from ..llm import LLMProviderConfig, test_llm_connection
from .config import API_PATH
from .deps import AdminReadSession, AdminWriteSession, ok
from .environment import (
    PROCESS_STARTED_AT,
    EnvironmentConflictError,
    EnvironmentValidationError,
    load_environment,
    pending_restart,
    resolve_llm_provider,
    restart_supported,
    update_environment,
)
from .route_models import EnvironmentPatch, LLMConnectionTestBody

router = APIRouter(prefix=API_PATH)
_LLM_TEST_CONCURRENCY = asyncio.Semaphore(2)
# 给响应和审计留出写出的时间再退出
_RESTART_DELAY_SECONDS = 1.0
_restart_scheduled = False


def _system_status() -> dict[str, Any]:
    return {
        "restartSupported": restart_supported(),
        "restartScheduled": _restart_scheduled,
        "pendingRestart": pending_restart(),
        "startedAt": int(PROCESS_STARTED_AT),
        "uptimeSeconds": int(time.time() - PROCESS_STARTED_AT),
    }


def _terminate_for_restart() -> None:
    logger.warning("WebUI 请求重启：进程退出，由容器 restart 策略拉起")
    os.kill(os.getpid(), signal.SIGTERM)


@router.get("/environment")
async def get_environment(_session: AdminReadSession) -> dict[str, Any]:
    return ok(await asyncio.to_thread(load_environment))


@router.get("/system/status")
async def get_system_status(_session: AdminReadSession) -> dict[str, Any]:
    return ok(await asyncio.to_thread(_system_status))


@router.post("/system/restart")
async def restart_system(_session: AdminWriteSession) -> dict[str, Any]:
    """只在容器托管（YAWNBOT_SUPERVISED=1）时允许：退出后由 restart 策略拉起。"""

    global _restart_scheduled  # noqa: PLW0603
    if not restart_supported():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "当前运行方式不支持从 WebUI 重启，请在服务器上手动重启 YawnBot",
        )
    if not _restart_scheduled:
        _restart_scheduled = True
        asyncio.get_running_loop().call_later(
            _RESTART_DELAY_SECONDS, _terminate_for_restart
        )
    return ok({"scheduled": True, "startedAt": int(PROCESS_STARTED_AT)})


@router.patch("/environment")
async def patch_environment(
    body: EnvironmentPatch, _session: AdminWriteSession
) -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(
            update_environment,
            body.version,
            [(item.key, item.value) for item in body.changes],
            (
                [
                    item.model_dump(by_alias=True, exclude_unset=True)
                    for item in body.providers
                ]
                if body.providers is not None
                else None
            ),
        )
    except EnvironmentConflictError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "环境配置已被其他操作修改，请刷新后重试"
        ) from exc
    except EnvironmentValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return ok(result)


@router.post("/llm/test")
async def test_llm(
    body: LLMConnectionTestBody, _session: AdminWriteSession
) -> dict[str, Any]:
    stored_base_url = ""
    stored_api_key: str | None = None
    try:
        stored_base_url, stored_api_key = await asyncio.to_thread(
            resolve_llm_provider, body.provider_id
        )
    except EnvironmentValidationError:
        if body.base_url is None or "api_key" not in body.model_fields_set:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "新提供商测试必须填写 Base URL 和 API Key",
            ) from None

    base_url = (body.base_url or stored_base_url).strip().rstrip("/")
    api_key = (
        body.api_key
        if "api_key" in body.model_fields_set
        else stored_api_key
    )
    if not api_key:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "该提供商尚未配置 API Key"
        )
    try:
        validated = LLMProviderConfig(id=body.provider_id, base_url=base_url)
        async with _LLM_TEST_CONCURRENCY:
            elapsed_ms = await test_llm_connection(
                base_url=validated.base_url,
                api_key=api_key,
                model=body.model.strip(),
                timeout=10.0,
            )
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Base URL 格式不正确"
        ) from exc
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT, "连接测试超时（10 秒）"
        ) from exc
    except Exception as exc:
        message = str(exc).replace(api_key, "[REDACTED]")[:500]
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"连接测试失败：{message or type(exc).__name__}",
        ) from exc
    return ok({"success": True, "latencyMs": round(elapsed_ms, 1)})
