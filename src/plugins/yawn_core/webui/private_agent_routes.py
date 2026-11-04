# ruff: noqa: FAST002, TC001, TID252
"""「私聊 Agent」页：私聊提示词注册表（全局 / 本人）、每用户人设、最近请求。

全部仅管理员可用：提示词里有用户资料和内部策略，人设与抓取的请求属于个人数据。
写操作由 ``app.py`` 的中间件统一记入操作审计（WebAdminAudit）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from nonebot_plugin_orm import get_session
from sqlalchemy.exc import SQLAlchemyError

from ..data_models.group_agent_config import GroupAgentConfig
from ..yawn_agent import (
    agent_bridge,
    memory_link,
    private_cache,
    private_persona,
    private_prompt_registry,
    private_rhythm,
    prompt_capture,
    prompt_sections,
)
from ..yawn_agent.persona import (
    PERSONA_PROMPT_TOKEN_LIMIT,
    PersonaCard,
    compile_persona_card,
    effective_persona_card,
    persona_card_summary,
    persona_override_reason,
    persona_preset_payloads,
    persona_prompt_tokens,
    prompt_persona,
)
from ..yawn_agent.persona_library import (
    PersonaLibraryError,
    create_template,
    refresh_private_library,
    serialize_template,
)
from .config import API_PATH
from .deps import AdminReadSession, AdminWriteSession, ok
from .hub import hub
from .private_agent_models import (
    PrivateCaptureSettingsBody,
    PrivatePersonaBatchBody,
    PrivatePersonaCardBody,
    PrivatePersonaCopyGroupBody,
    PrivatePersonaImportBody,
    PrivatePersonaLockBody,
    PrivatePersonaPolicyBody,
    PrivatePersonaPresetBody,
    PrivatePersonaPreviewBody,
    PrivatePersonaRevisionBody,
    PrivatePersonaSaveTemplateBody,
    PrivatePersonaTemplateBody,
    PrivatePromptBulkBody,
    PrivatePromptOverrideBody,
    PrivatePromptPreviewBody,
    PrivatePromptPromoteBody,
)
from .route_helpers import optimistic_write_lock, require_group, require_user

router = APIRouter(prefix=f"{API_PATH}/private-agent")
_SECTION_ID_MAX = 96
_SCOPE_PATTERN = "^(global|user)$"


# ── 通用 ────────────────────────────────────────────────────────────────


def _scope_user(scope: str, user_id: int | None) -> int | None:
    if scope == "user":
        if user_id is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "本人范围需要 userId"
            )
        return user_id
    return None


async def _check_user(user_id: int | None) -> None:
    if user_id is None:
        return
    async with get_session() as db:
        await require_user(db, user_id)


def _require_private_section(section_id: str) -> prompt_sections.PromptSection:
    if len(section_id) > _SECTION_ID_MAX:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "分段 ID 无效")
    section = prompt_sections.get_section(section_id)
    if section is None or not prompt_sections.is_private_section(section_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "私聊提示词分段不存在")
    return section


def _validation_error(exc: Exception) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


def _conflict(exc: Exception) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, str(exc))


def _edit_error(exc: private_persona.PersonaEditError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


def _require_safe_card(card: PersonaCard) -> None:
    reason = persona_override_reason(card)
    if reason is not None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{reason}；人设只描述角色，系统规则不能被人设覆盖",
        )


async def _prompts_changed(user_id: int | None) -> None:
    await hub.notify_change(
        "private_agent_prompts", str(user_id) if user_id else "global"
    )


async def _persona_changed(user_id: int | None) -> None:
    await hub.notify_change(
        "private_agent_persona", str(user_id) if user_id else "policy"
    )
    if user_id:
        # 用户抽屉里的私聊 Agent 卡片带着人设摘要，一起刷新。
        await hub.notify_change("user_private_agent", str(user_id))


# ── 提示词 ──────────────────────────────────────────────────────────────


@router.get("/prompts/catalog")
async def get_private_prompt_catalog(
    _session: AdminReadSession,
    user_id: int | None = Query(default=None, alias="userId", gt=0),
) -> dict[str, Any]:
    """私聊分段：默认值、全局覆盖、本人覆盖、生效文本、链路与体检。"""

    await _check_user(user_id)
    return ok(private_prompt_registry.catalog(user_id))


@router.put("/prompts/sections/{section_id}")
async def put_private_prompt_override(
    section_id: str, body: PrivatePromptOverrideBody, session: AdminWriteSession
) -> dict[str, Any]:
    _require_private_section(section_id)
    user_id = _scope_user(body.scope, body.user_id)
    await _check_user(user_id)
    try:
        result = prompt_sections.set_override(
            section_id,
            group_id=None,
            user_id=user_id,
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
    await _prompts_changed(user_id)
    return ok(result)


@router.delete("/prompts/sections/{section_id}")
async def delete_private_prompt_override(
    section_id: str,
    session: AdminWriteSession,
    scope: str = Query(default="global", pattern=_SCOPE_PATTERN),
    user_id: int | None = Query(default=None, alias="userId", gt=0),
    expected_revision: int | None = Query(default=None, alias="expectedRevision", ge=0),
) -> dict[str, Any]:
    """去掉这一层覆盖：本人回到继承全局，全局回到代码默认。"""

    _require_private_section(section_id)
    target = _scope_user(scope, user_id)
    try:
        result = prompt_sections.clear_override(
            section_id,
            group_id=None,
            user_id=target,
            actor=session.actor_fingerprint,
            expected_revision=expected_revision,
        )
    except prompt_sections.PromptStoreValidationError as exc:
        raise _validation_error(exc) from exc
    except prompt_sections.PromptStoreConflictError as exc:
        raise _conflict(exc) from exc
    await _prompts_changed(target)
    return ok(result)


@router.get("/prompts/sections/{section_id}/history")
async def get_private_prompt_history(
    section_id: str,
    _session: AdminReadSession,
    scope: str = Query(default="global", pattern=_SCOPE_PATTERN),
    user_id: int | None = Query(default=None, alias="userId", gt=0),
) -> dict[str, Any]:
    _require_private_section(section_id)
    target = _scope_user(scope, user_id)
    return ok(prompt_sections.section_history(section_id, None, user_id=target))


@router.post("/prompts/bulk")
async def bulk_private_prompt_overrides(
    body: PrivatePromptBulkBody, session: AdminWriteSession
) -> dict[str, Any]:
    """批量改（整组恢复默认、导入）；只收私聊分段，全部校验通过才写入。"""

    user_id = _scope_user(body.scope, body.user_id)
    await _check_user(user_id)
    changes = [item.model_dump(by_alias=True) for item in body.changes]
    for item in changes:
        if not prompt_sections.is_private_section(str(item.get("sectionId") or "")):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "这里只能改私聊分段"
            )
    if body.replace_scope and user_id is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "全局作用域与群聊共用，不能整体替换"
        )
    try:
        result = prompt_sections.bulk_apply(
            changes,
            group_id=None,
            user_id=user_id,
            actor=session.actor_fingerprint,
            expected_revision=body.expected_revision,
            confirm_safety=body.confirm_safety,
            replace_scope=body.replace_scope,
        )
    except prompt_sections.PromptStoreValidationError as exc:
        raise _validation_error(exc) from exc
    except prompt_sections.PromptStoreConflictError as exc:
        raise _conflict(exc) from exc
    await _prompts_changed(user_id)
    return ok(result)


@router.get("/prompts/export")
async def export_private_prompt_overrides(
    _session: AdminReadSession,
    scope: str = Query(default="global", pattern=_SCOPE_PATTERN),
    user_id: int | None = Query(default=None, alias="userId", gt=0),
) -> dict[str, Any]:
    target = _scope_user(scope, user_id)
    payload = prompt_sections.export_scope(None, user_id=target)
    # 全局作用域和群聊共用：导出时只留私聊分段，导入到这页时不会误改群聊。
    payload["overrides"] = {
        key: value
        for key, value in payload["overrides"].items()
        if prompt_sections.is_private_section(key)
    }
    payload["privateOnly"] = True
    return ok(payload)


@router.post("/prompts/preview")
async def preview_private_prompt(
    body: PrivatePromptPreviewBody, _session: AdminReadSession
) -> dict[str, Any]:
    """用这位用户的真实资料与近期历史拼出主回合请求（叠加未保存的草稿），不发送。"""

    if body.persona_card is not None:
        _require_safe_card(body.persona_card)
    for drafts in (body.global_drafts, body.user_drafts):
        for key in drafts:
            if not prompt_sections.is_private_section(key):
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT, "草稿里只能有私聊分段"
                )
    async with get_session() as db:
        await require_user(db, body.user_id)
        snapshot = await private_prompt_registry.preview(
            db,
            body.user_id,
            message=body.message,
            global_drafts=body.global_drafts,
            user_drafts=body.user_drafts,
            persona_card=body.persona_card,
            computer=body.computer,
            tasks=body.tasks,
            allow_silence=body.allow_silence,
            include_history=body.include_history,
        )
    return ok(snapshot)


@router.get("/prompts/users")
async def list_private_prompt_users(_session: AdminReadSession) -> dict[str, Any]:
    """有本人提示词覆盖的用户。"""

    return ok(private_prompt_registry.users_with_overrides())


@router.post("/prompts/users/{user_id}/promote")
async def promote_private_prompt_user(
    user_id: int, body: PrivatePromptPromoteBody, session: AdminWriteSession
) -> dict[str, Any]:
    """把这位用户的覆盖提升为全局（默认同时从本人层删掉）。"""

    try:
        result = private_prompt_registry.promote_user(
            user_id,
            body.section_ids,
            actor=session.actor_fingerprint,
            expected_revision=body.expected_revision,
            confirm_safety=body.confirm_safety,
            clear_user=not body.keep_user,
        )
    except prompt_sections.PromptStoreValidationError as exc:
        raise _validation_error(exc) from exc
    except prompt_sections.PromptStoreConflictError as exc:
        raise _conflict(exc) from exc
    await _prompts_changed(None)
    await _prompts_changed(user_id)
    return ok(result)


@router.delete("/prompts/users/{user_id}")
async def reset_private_prompt_user(
    user_id: int,
    session: AdminWriteSession,
    expected_revision: int | None = Query(default=None, alias="expectedRevision", ge=0),
) -> dict[str, Any]:
    """清空这位用户的全部本人覆盖。"""

    try:
        result = private_prompt_registry.reset_user(
            user_id,
            actor=session.actor_fingerprint,
            expected_revision=expected_revision,
        )
    except prompt_sections.PromptStoreConflictError as exc:
        raise _conflict(exc) from exc
    await _prompts_changed(user_id)
    return ok(result)


# ── 最近请求 ────────────────────────────────────────────────────────────


def _capture_status() -> dict[str, Any]:
    return {
        "captureEnabled": prompt_capture.capture_enabled(),
        "paused": prompt_capture.private_capture_paused(),
        "perUser": prompt_capture.MAX_PRIVATE_SNAPSHOTS_PER_USER,
        "maxUsers": prompt_capture.MAX_PRIVATE_USERS,
    }


@router.get("/captures")
async def list_private_captures(_session: AdminReadSession) -> dict[str, Any]:
    return ok({**_capture_status(), "users": prompt_capture.private_prompt_users()})


@router.get("/captures/{user_id}")
async def get_private_captures(
    user_id: int, _session: AdminReadSession
) -> dict[str, Any]:
    return ok(
        {
            **_capture_status(),
            "userId": str(user_id),
            "prompts": prompt_capture.private_prompts(user_id),
        }
    )


@router.delete("/captures")
async def clear_private_captures(
    _session: AdminWriteSession,
    user_id: int | None = Query(default=None, alias="userId", gt=0),
) -> dict[str, Any]:
    return ok({"cleared": prompt_capture.clear_private_prompts(user_id)})


@router.get("/cache")
async def get_private_cache_stats(_session: AdminReadSession) -> dict[str, Any]:
    """前缀缓存命中：各链路累计命中/未命中 token 与最近几次明细（进程内，重启清零）。"""

    snapshot = private_cache.usage_snapshot()
    try:
        snapshot["disclosure"] = _disclosure_summary()
    except Exception:  # noqa: BLE001 - 只是展示，算不出来不影响命中统计
        snapshot["disclosure"] = None
    return ok(snapshot)


def _disclosure_summary() -> dict[str, Any]:
    """工具披露方式和一位普通好友这一轮的工具表大小（渐进式 vs 全量），看省了多少。"""

    import json

    from ..yawn_agent.private_agent import tool_table
    from ..yawn_agent.private_computer import computer_ready
    from ..yawn_agent.private_tasks import tasks_enabled
    from ..yawn_agent.private_tool_catalog import direct_names, disclosure_mode
    from ..yawn_agent.private_tools import tool_schemas

    flags = {
        "is_superuser": False,
        "computer": computer_ready(),
        "tasks": tasks_enabled(),
        "manage": False,
    }
    table, available = tool_table(**flags)
    full = tool_schemas(**flags)
    return {
        "mode": disclosure_mode(),
        "direct": list(direct_names()),
        "available": len(available),
        "tableChars": len(json.dumps(table, ensure_ascii=False)),
        "fullChars": len(json.dumps(full, ensure_ascii=False)),
    }


@router.get("/rhythm")
async def get_private_rhythm_stats(
    _session: AdminReadSession,
    user_id: int | None = Query(default=None, alias="userId", gt=0),
) -> dict[str, Any]:
    """分条发送：全站实际发了几条的分布、各节奏次数、被打断次数（进程内，重启清零）；
    给了 userId 时附他最近几次的条数和是否套了模板。"""

    return ok(private_rhythm.snapshot(user_id))


@router.get("/bridge")
async def get_agent_bridge(
    _session: AdminReadSession,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """群聊 ↔ 私聊协作：最近从群里转到私聊的事、私聊替人在群里转告/提问的记录与限额。

    只含话题和发到群里的原文（本来就是群里公开的），不含私聊对话内容。
    """

    return ok(agent_bridge.overview(limit))


@router.get("/bridge/users/{user_id}")
async def get_agent_bridge_user(
    user_id: int, _session: AdminReadSession
) -> dict[str, Any]:
    """一位用户的协作记录：转来的事、转告/提问（含群友回复）、等他确认的转告。"""

    if user_id <= 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "userId 不对")
    return ok(agent_bridge.user_detail(user_id))


# ── 记忆打通 ────────────────────────────────────────────────────────────


@router.get("/memory-link")
async def get_memory_link(
    _session: AdminReadSession,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """群聊记忆 ↔ 私聊记忆打通：谁开了共享、静音了群、让 Bot 忘掉过，最近的流动记录。"""

    return ok(memory_link.overview(limit))


async def _memory_link_detail(user_id: int) -> dict[str, Any]:
    # 注意：路由参数里的 session 是 WebUI 登录会话，不是数据库会话；
    # 之前直接传进 user_detail 会在 session.execute 处 500，页面就打不开某位用户。
    async with get_session() as db:
        return await memory_link.user_detail(db, user_id)


@router.get("/memory-link/users/{user_id}")
async def get_memory_link_user(
    user_id: int, _session: AdminReadSession
) -> dict[str, Any]:
    """一位用户的记忆打通：设置、忘掉的记录、流动记录、私聊事实、各群画像、每个群看得到的共享事实。"""

    if user_id <= 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "userId 不对")
    return ok(await _memory_link_detail(user_id))


@router.post("/memory-link/users/{user_id}/unshare")
async def post_memory_link_unshare(
    user_id: int, _session: AdminWriteSession
) -> dict[str, Any]:
    """撤掉他全部“群里也知道”的共享（管理员只能收紧，不能替他打开）。"""

    from ..yawn_agent.context import now_beijing

    state = memory_link.load(user_id)
    if state.share_keys:
        memory_link.set_share(
            user_id, list(state.share_keys), on=False, now=now_beijing()
        )
    return ok(await _memory_link_detail(user_id))


@router.post("/memory-link/users/{user_id}/reset")
async def post_memory_link_reset(
    user_id: int, _session: AdminWriteSession
) -> dict[str, Any]:
    """恢复默认（不共享、不静音、私聊里可以提群里的事）；“忘掉”的记录保留。"""

    from ..yawn_agent.context import now_beijing

    memory_link.admin_reset(user_id, now=now_beijing())
    return ok(await _memory_link_detail(user_id))


@router.delete("/memory-link/users/{user_id}/tombstones/{index}")
async def delete_memory_link_tombstone(
    user_id: int, index: int, _session: AdminWriteSession
) -> dict[str, Any]:
    """删掉一条“忘掉”记录（之后自动整理可能重新记起这件事）。"""

    from ..yawn_agent.context import now_beijing

    if not memory_link.drop_tombstone(user_id, index, now=now_beijing()):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "没有这条记录")
    return ok(await _memory_link_detail(user_id))


@router.put("/captures/settings")
async def put_private_capture_settings(
    body: PrivateCaptureSettingsBody, _session: AdminWriteSession
) -> dict[str, Any]:
    prompt_capture.set_private_capture_paused(body.paused)
    return ok(_capture_status())


# ── 人设 ────────────────────────────────────────────────────────────────


def _persona_options() -> dict[str, Any]:
    return {
        "presets": persona_preset_payloads(),
        "library": private_persona.library_choices(),
        "fields": dict(private_persona.FIELD_LABELS),
        "promptTokenLimit": PERSONA_PROMPT_TOKEN_LIMIT,
    }


@router.get("/persona/policy")
async def get_private_persona_policy(_session: AdminReadSession) -> dict[str, Any]:
    return ok(
        {
            "policy": private_persona.load_policy().as_dict(),
            "options": _persona_options(),
        }
    )


@router.put("/persona/policy")
async def put_private_persona_policy(
    body: PrivatePersonaPolicyBody, session: AdminWriteSession
) -> dict[str, Any]:
    patch = body.model_dump(
        by_alias=True, exclude={"expected_revision"}, exclude_unset=True
    )
    try:
        policy = private_persona.save_policy(
            patch,
            actor=session.actor_fingerprint,
            expected_revision=body.expected_revision,
        )
    except private_persona.PersonaPolicyError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    await _persona_changed(None)
    return ok({"policy": policy.as_dict()})


@router.get("/persona/users")
async def list_private_persona_users(
    _session: AdminReadSession,
    q: str = Query(default="", max_length=40),
    source: str = Query(default="", max_length=20),
) -> dict[str, Any]:
    """有私聊人设记录的用户（最近修改在前）+ 统计 + 人设库使用数。"""

    rows = private_persona.user_rows()
    totals = private_persona.stats(rows)
    needle = q.strip().casefold()
    if needle:
        rows = [
            row
            for row in rows
            if needle in row["userId"]
            or needle in str(row.get("name") or "").casefold()
            or needle in str(row.get("templateName") or "").casefold()
        ]
    if source:
        rows = [row for row in rows if row["mode"] == source or row["source"] == source]
    return ok(
        {
            "users": rows,
            "stats": totals,
            "templateUsage": private_persona.template_usage(),
            "policy": private_persona.load_policy().as_dict(),
            "options": _persona_options(),
        }
    )


@router.get("/persona/users/{user_id}")
async def get_private_persona_user(
    user_id: int, _session: AdminReadSession
) -> dict[str, Any]:
    await _check_user(user_id)
    return ok(private_persona.state_payload(user_id))


async def _after_write(user_id: int, result: dict[str, Any]) -> dict[str, Any]:
    await _persona_changed(user_id)
    return ok({**result, "state": private_persona.state_payload(user_id)})


@router.put("/persona/users/{user_id}")
async def put_private_persona_user(
    user_id: int, body: PrivatePersonaCardBody, session: AdminWriteSession
) -> dict[str, Any]:
    """给这位用户一张专属人设卡。"""

    await _check_user(user_id)
    _require_safe_card(body.card)
    try:
        result = private_persona.set_custom(
            user_id,
            body.card,
            actor=session.actor_fingerprint,
            expected_revision=body.expected_revision,
        )
    except private_persona.PersonaEditError as exc:
        raise _edit_error(exc) from exc
    return await _after_write(user_id, result)


@router.post("/persona/users/{user_id}/template")
async def use_private_persona_template(
    user_id: int, body: PrivatePersonaTemplateBody, session: AdminWriteSession
) -> dict[str, Any]:
    """让这位用户跟随人设库里的一张模板（模板改了他也跟着变）。"""

    await _check_user(user_id)
    try:
        result = private_persona.use_template(
            user_id,
            body.template_id,
            actor=session.actor_fingerprint,
            expected_revision=body.expected_revision,
        )
    except private_persona.PersonaEditError as exc:
        raise _edit_error(exc) from exc
    return await _after_write(user_id, result)


@router.post("/persona/users/{user_id}/preset")
async def use_private_persona_preset(
    user_id: int, body: PrivatePersonaPresetBody, session: AdminWriteSession
) -> dict[str, Any]:
    await _check_user(user_id)
    try:
        result = private_persona.use_preset(
            user_id,
            body.preset_id,
            actor=session.actor_fingerprint,
            expected_revision=body.expected_revision,
        )
    except private_persona.PersonaEditError as exc:
        raise _edit_error(exc) from exc
    return await _after_write(user_id, result)


@router.post("/persona/users/{user_id}/reset")
async def reset_private_persona_user(
    user_id: int, body: PrivatePersonaRevisionBody, session: AdminWriteSession
) -> dict[str, Any]:
    """回到跟随私聊默认（卡片保留在历史里，可撤销）。"""

    try:
        result = private_persona.reset(
            user_id,
            actor=session.actor_fingerprint,
            expected_revision=body.expected_revision,
        )
    except private_persona.PersonaEditError as exc:
        raise _edit_error(exc) from exc
    return await _after_write(user_id, result)


@router.post("/persona/users/{user_id}/undo")
async def undo_private_persona_user(
    user_id: int, session: AdminWriteSession
) -> dict[str, Any]:
    try:
        result = private_persona.undo(user_id, actor=session.actor_fingerprint)
    except private_persona.PersonaEditError as exc:
        raise _edit_error(exc) from exc
    return await _after_write(user_id, result)


@router.put("/persona/users/{user_id}/lock")
async def lock_private_persona_user(
    user_id: int, body: PrivatePersonaLockBody, session: AdminWriteSession
) -> dict[str, Any]:
    """锁定后本人不能再用 my_persona 改；管理员仍可改。"""

    await _check_user(user_id)
    result = private_persona.set_locked(
        user_id, body.locked, actor=session.actor_fingerprint
    )
    return await _after_write(user_id, result)


@router.delete("/persona/users/{user_id}")
async def clear_private_persona_user(
    user_id: int, _session: AdminWriteSession
) -> dict[str, Any]:
    """彻底删掉这位用户的私聊人设（含历史，不可撤销）。"""

    deleted = private_persona.clear(user_id)
    await _persona_changed(user_id)
    return ok({"deleted": deleted, "state": private_persona.state_payload(user_id)})


@router.get("/persona/users/{user_id}/export")
async def export_private_persona_user(
    user_id: int, _session: AdminReadSession
) -> dict[str, Any]:
    return ok(private_persona.export_state(user_id))


@router.post("/persona/users/{user_id}/import")
async def import_private_persona_user(
    user_id: int, body: PrivatePersonaImportBody, session: AdminWriteSession
) -> dict[str, Any]:
    await _check_user(user_id)
    _require_safe_card(body.card)
    try:
        result = private_persona.set_custom(
            user_id,
            body.card,
            actor=session.actor_fingerprint,
            expected_revision=body.expected_revision,
            action="import",
        )
    except private_persona.PersonaEditError as exc:
        raise _edit_error(exc) from exc
    return await _after_write(user_id, result)


@router.post("/persona/users/{user_id}/save-as-template")
async def save_private_persona_as_template(
    user_id: int, body: PrivatePersonaSaveTemplateBody, session: AdminWriteSession
) -> dict[str, Any]:
    """把这位用户当前生效的人设存进人设库（可选让他改为跟随这张模板）。"""

    card = private_persona.resolve_user_persona(user_id).card
    _require_safe_card(card)
    async with optimistic_write_lock, get_session() as db:
        try:
            row = await create_template(db, name=body.name, card=card, is_global=False)
        except PersonaLibraryError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        except SQLAlchemyError as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "保存失败：人设库里可能已有同名人设"
            ) from exc
        await db.commit()
        await db.refresh(row)
        template = serialize_template(row)
        await refresh_private_library(db)
    await hub.notify_change("agent_persona_library", None)
    result: dict[str, Any] = {"template": template}
    if body.switch_user:
        try:
            result.update(
                private_persona.use_template(
                    user_id, int(row.id), actor=session.actor_fingerprint
                )
            )
        except private_persona.PersonaEditError as exc:
            raise _edit_error(exc) from exc
    return await _after_write(user_id, result)


@router.post("/persona/users/{user_id}/copy-group")
async def copy_group_persona_to_user(
    user_id: int, body: PrivatePersonaCopyGroupBody, session: AdminWriteSession
) -> dict[str, Any]:
    """把某个群当前生效的群聊人设复制成这位用户的专属私聊人设。"""

    await _check_user(user_id)
    async with get_session() as db:
        await require_group(db, body.group_id)
        config = await db.get(GroupAgentConfig, body.group_id)
        card = effective_persona_card(config)
    try:
        result = private_persona.set_custom(
            user_id,
            card,
            actor=session.actor_fingerprint,
            expected_revision=body.expected_revision,
            action="copy_group",
        )
    except private_persona.PersonaEditError as exc:
        raise _edit_error(exc) from exc
    return await _after_write(user_id, {**result, "fromGroup": str(body.group_id)})


@router.post("/persona/batch")
async def batch_private_persona(
    body: PrivatePersonaBatchBody, session: AdminWriteSession
) -> dict[str, Any]:
    """批量：恢复跟随默认 / 锁定 / 解锁 / 删除。单个失败不影响其他人。"""

    done: list[str] = []
    failed: list[dict[str, str]] = []
    for user_id in dict.fromkeys(body.user_ids):
        try:
            if body.action == "reset":
                private_persona.reset(user_id, actor=session.actor_fingerprint)
            elif body.action in {"lock", "unlock"}:
                private_persona.set_locked(
                    user_id, body.action == "lock", actor=session.actor_fingerprint
                )
            else:
                private_persona.clear(user_id)
            done.append(str(user_id))
        except (private_persona.PersonaEditError, OSError) as exc:  # noqa: PERF203
            failed.append({"userId": str(user_id), "error": str(exc)})
    await hub.notify_change("private_agent_persona", "batch")
    return ok({"done": done, "failed": failed})


@router.post("/persona/preview")
async def preview_private_persona(
    body: PrivatePersonaPreviewBody, _session: AdminReadSession
) -> dict[str, Any]:
    """人设草稿编译成提示词里的样子，以及 Token 用量。"""

    reason = persona_override_reason(body.card)
    compiled = compile_persona_card(body.card, source="draft")
    return ok(
        {
            "compiled": prompt_persona(compiled),
            "promptTokens": persona_prompt_tokens(compiled),
            "promptTokenLimit": PERSONA_PROMPT_TOKEN_LIMIT,
            "trimmed": bool(compiled.get("trimmed")),
            "summary": persona_card_summary(body.card),
            "unsafe": reason,
        }
    )
