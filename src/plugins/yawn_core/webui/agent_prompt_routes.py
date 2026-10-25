# ruff: noqa: FAST002, TC001, TID252
"""Agent 提示词分段：查看、覆盖（全局/本群）、历史、导入导出，以及 Trace 完整提示词。

全部仅管理员可用：提示词里有群消息、成员资料与内部策略，访客一律 403。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from nonebot_plugin_orm import get_session

from ..yawn_agent import prompt_capture, prompt_sections
from ..yawn_agent.execution_trace import execution_trace_object
from ..yawn_agent.prompt import PROMPT_VERSION
from .config import API_PATH
from .deps import AdminReadSession, AdminWriteSession, ok
from .hub import hub
from .route_helpers import require_group
from .route_models import PromptBulkBody, PromptOverrideBody

router = APIRouter(prefix=API_PATH)
_SECTION_ID_MAX = 96


async def _check_group(group_id: int | None) -> None:
    if group_id is None:
        return
    async with get_session() as db:
        await require_group(db, group_id)


def _scope_group(scope: str, group_id: int | None) -> int | None:
    if scope == "group":
        if group_id is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "本群范围需要 groupId"
            )
        return group_id
    return None


def _validation_error(exc: prompt_sections.PromptStoreValidationError) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


def _conflict(exc: prompt_sections.PromptStoreConflictError) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, str(exc))


def _require_section(section_id: str) -> prompt_sections.PromptSection:
    if len(section_id) > _SECTION_ID_MAX:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "分段 ID 无效")
    section = prompt_sections.get_section(section_id)
    if section is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "提示词分段不存在")
    return section


async def _notify(group_id: int | None) -> None:
    await hub.notify_change(
        "agent_prompts", str(group_id or "global"), group_id=group_id
    )


@router.get("/agent/prompts/status")
async def get_prompt_status(_session: AdminReadSession) -> dict[str, Any]:
    """覆盖文件状态与完整提示词抓取开关。"""

    snapshot = prompt_sections.store_snapshot()
    return ok(
        {
            "promptVersion": PROMPT_VERSION,
            "revision": snapshot["revision"],
            "updatedAt": snapshot["updatedAt"],
            "storeFile": snapshot["path"],
            "loadError": snapshot["loadError"],
            "scopes": {
                scope: len(entries) for scope, entries in snapshot["scopes"].items()
            },
            "capture": {
                "enabled": prompt_capture.capture_enabled(),
                "perTrace": prompt_capture.MAX_SNAPSHOTS_PER_TRACE,
                "snapshotChars": prompt_capture.MAX_SNAPSHOT_CHARS,
                "tracesWithPrompts": prompt_capture.MAX_TRACES_WITH_PROMPTS,
            },
        }
    )


@router.get("/agent/prompts/catalog")
async def get_prompt_catalog(
    _session: AdminReadSession,
    group_id: int | None = Query(default=None, alias="groupId", gt=0),
) -> dict[str, Any]:
    """全部分段：默认值、全局/本群覆盖、生效文本、最近样例与 Token 估算。"""

    await _check_group(group_id)
    return ok({"promptVersion": PROMPT_VERSION, **prompt_sections.catalog(group_id)})


@router.put("/agent/prompts/sections/{section_id}")
async def put_prompt_override(
    section_id: str, body: PromptOverrideBody, session: AdminWriteSession
) -> dict[str, Any]:
    _require_section(section_id)
    group_id = _scope_group(body.scope, body.group_id)
    await _check_group(group_id)
    try:
        result = prompt_sections.set_override(
            section_id,
            group_id=group_id,
            mode=body.mode,
            text=body.text,
            note=body.note,
            actor=session.actor_fingerprint,
            expected_revision=body.expected_revision,
            confirm_safety=body.confirm_safety,
        )
    except prompt_sections.PromptStoreValidationError as exc:
        raise _validation_error(exc) from exc
    except prompt_sections.PromptStoreConflictError as exc:
        raise _conflict(exc) from exc
    await _notify(group_id)
    return ok(result)


@router.delete("/agent/prompts/sections/{section_id}")
async def delete_prompt_override(
    section_id: str,
    _session: AdminWriteSession,
    scope: str = Query(default="global", pattern="^(global|group)$"),
    group_id: int | None = Query(default=None, alias="groupId", gt=0),
    expected_revision: int | None = Query(default=None, alias="expectedRevision", ge=0),
) -> dict[str, Any]:
    """去掉这一层覆盖：本群回到继承全局，全局回到代码默认。"""

    _require_section(section_id)
    target = _scope_group(scope, group_id)
    await _check_group(target)
    try:
        result = prompt_sections.clear_override(
            section_id,
            group_id=target,
            actor=_session.actor_fingerprint,
            expected_revision=expected_revision,
        )
    except prompt_sections.PromptStoreConflictError as exc:
        raise _conflict(exc) from exc
    await _notify(target)
    return ok(result)


@router.get("/agent/prompts/sections/{section_id}/history")
async def get_prompt_history(
    section_id: str,
    _session: AdminReadSession,
    scope: str = Query(default="global", pattern="^(global|group)$"),
    group_id: int | None = Query(default=None, alias="groupId", gt=0),
) -> dict[str, Any]:
    _require_section(section_id)
    target = _scope_group(scope, group_id)
    return ok(prompt_sections.section_history(section_id, target))


@router.post("/agent/prompts/bulk")
async def bulk_prompt_overrides(
    body: PromptBulkBody, session: AdminWriteSession
) -> dict[str, Any]:
    """批量改：一键删除安全段、整类恢复默认、导入覆盖。全部校验通过才写入。"""

    group_id = _scope_group(body.scope, body.group_id)
    await _check_group(group_id)
    try:
        result = prompt_sections.bulk_apply(
            [item.model_dump(by_alias=True) for item in body.changes],
            group_id=group_id,
            actor=session.actor_fingerprint,
            expected_revision=body.expected_revision,
            confirm_safety=body.confirm_safety,
            replace_scope=body.replace_scope,
        )
    except prompt_sections.PromptStoreValidationError as exc:
        raise _validation_error(exc) from exc
    except prompt_sections.PromptStoreConflictError as exc:
        raise _conflict(exc) from exc
    await _notify(group_id)
    return ok(result)


@router.get("/agent/prompts/export")
async def export_prompt_overrides(
    _session: AdminReadSession,
    scope: str = Query(default="global", pattern="^(global|group)$"),
    group_id: int | None = Query(default=None, alias="groupId", gt=0),
) -> dict[str, Any]:
    target = _scope_group(scope, group_id)
    await _check_group(target)
    return ok(prompt_sections.export_scope(target))


@router.get("/agent/prompts/background")
async def get_background_prompts(_session: AdminReadSession) -> dict[str, Any]:
    """不属于任何群 Trace 的 Agent 后台请求（记忆整理、周摘要、收图转述）。"""

    return ok(
        {
            "captureEnabled": prompt_capture.capture_enabled(),
            "prompts": prompt_capture.background_prompts(),
        }
    )


@router.delete("/agent/prompts/background")
async def clear_background_prompts(_session: AdminWriteSession) -> dict[str, Any]:
    return ok({"cleared": prompt_capture.clear_background_prompts()})


@router.get("/agent/groups/{group_id}/execution-traces/{trace_id}/prompts")
async def get_trace_prompts(
    group_id: int, trace_id: str, _session: AdminReadSession
) -> dict[str, Any]:
    """一条 Trace 里每次真实发给模型的完整请求（messages、tools、参数、回复）。"""

    await _check_group(group_id)
    trace = execution_trace_object(group_id, trace_id)
    if trace is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "执行 Trace 不存在")
    return ok(prompt_capture.trace_prompts(trace))
