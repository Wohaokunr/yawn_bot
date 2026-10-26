# ruff: noqa: TID252,FAST002,E501
"""「网页搜索」管理页的后端：DeepSeek 搜索状态、策略与限额、用量与费用、最近调用、试搜。

搜索本身走 ``yawn_agent.web_search_client``；这里只做 snake_case → camelCase、
群名标注和输入校验。全部接口仅管理员可用：最近调用里有群成员的搜索词。

试搜/试抓是真实请求，会计入全站当日计费次数（和账单一致），但不占任何群或
成员的配额。密钥只回显来源和掩码，永远不返回明文。
"""

from __future__ import annotations

import time
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from nonebot_plugin_orm import get_session
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel
from sqlalchemy import select

from ..data_models.bot_group import BotGroup
from ..yawn_agent.tool_handlers.web import cache_stats, clear_caches
from ..yawn_agent.web_search_client import (
    PRICE_PER_CALL_YUAN,
    DeepSeekWebClient,
    WebSearchError,
    base_url,
    find_api_key,
)
from ..yawn_agent.web_search_output import (
    build_query,
    clean_text,
    host_of,
    short_date,
    strip_markdown_noise,
)
from ..yawn_agent.web_search_policy import (
    WebSearchSettings,
    estimated_cost_yuan,
    load_settings,
    recent_calls,
    record_admin_call,
    save_settings,
    usage_snapshot,
)
from .config import API_PATH
from .deps import AdminReadSession, AdminWriteSession, ok
from .hub import hub

router = APIRouter(prefix=f"{API_PATH}/web-search")
_TEST_PREVIEW_CHARS = 4000


def _camel(key: str) -> str:
    head, *rest = key.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def _settings_payload(settings: WebSearchSettings) -> dict[str, Any]:
    data = {_camel(key): value for key, value in settings.model_dump().items()}
    data["allowedGroups"] = [str(item) for item in settings.allowed_groups]
    data["blockedGroups"] = [str(item) for item in settings.blocked_groups]
    return data


async def _group_names(group_ids: set[int]) -> dict[str, str]:
    if not group_ids:
        return {}
    try:
        async with get_session() as session:
            rows = await session.execute(
                select(BotGroup.group_id, BotGroup.group_name).where(
                    BotGroup.group_id.in_(group_ids)
                )
            )
            return {str(group_id): name for group_id, name in rows if name}
    except Exception:  # noqa: BLE001
        return {}


def _usage_payload(settings: WebSearchSettings) -> dict[str, Any]:
    usage = usage_snapshot()
    groups = sorted(
        (
            {"groupId": key, "calls": int(value)}
            for key, value in usage.get("groups", {}).items()
            if int(value) > 0
        ),
        key=lambda row: -row["calls"],
    )
    members = sorted(
        (
            {"groupId": key.split(":", 1)[0], "userId": key.split(":", 1)[-1], "calls": int(value)}
            for key, value in usage.get("members", {}).items()
            if int(value) > 0
        ),
        key=lambda row: -row["calls"],
    )
    totals = {key: int(value) for key, value in (usage.get("totals") or {}).items()}
    billed = totals.get("billed", 0)
    return {
        "day": usage.get("day"),
        "totals": totals,
        "billed": billed,
        "costYuan": estimated_cost_yuan(billed),
        "pricePerCallYuan": PRICE_PER_CALL_YUAN,
        "globalLimit": settings.global_daily_calls,
        "globalLeft": max(settings.global_daily_calls - billed, 0),
        "groups": groups[:50],
        "members": members[:50],
        "groupLimit": settings.group_daily_calls,
        "memberLimit": settings.member_daily_calls,
    }


async def _label(rows: list[dict[str, Any]]) -> None:
    ids = {int(row["groupId"]) for row in rows if str(row.get("groupId") or "").isdigit()}
    names = await _group_names(ids)
    for row in rows:
        row["groupName"] = names.get(str(row.get("groupId") or ""))


def _state(settings: WebSearchSettings, configured: bool) -> str:  # noqa: FBT001
    if not configured:
        return "no_key"
    if not settings.enabled:
        return "paused"
    if settings.global_daily_calls <= 0:
        return "paused"
    return "ready"


@router.get("/status")
async def web_search_status(_session: AdminReadSession) -> dict[str, Any]:
    """状态条：密钥来源（掩码）、策略、今日用量与费用、缓存和上游统计。"""

    settings = load_settings()
    key = find_api_key()
    usage = _usage_payload(settings)
    await _label(usage["groups"])
    await _label(usage["members"])
    stats = cache_stats()
    return ok(
        {
            "state": _state(settings, key is not None),
            "configured": key is not None,
            "keySource": key.source if key else None,
            "keyMasked": key.masked if key else None,
            "baseUrl": base_url(),
            "settings": _settings_payload(settings),
            "defaults": _settings_payload(WebSearchSettings()),
            "usage": usage,
            "cache": {"search": stats["search"], "fetch": stats["fetch"]},
            "upstream": {_camel(k): v for k, v in stats["client"].items()},
        }
    )


class SettingsBody(BaseModel):
    """前端提交 camelCase；群号用字符串避免 JS 精度问题。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    enabled: bool
    group_mode: Literal["all", "allowlist"]
    allowed_groups: list[str] = Field(default_factory=list, max_length=500)
    blocked_groups: list[str] = Field(default_factory=list, max_length=500)
    admin_only: bool
    member_daily_calls: int
    group_daily_calls: int
    global_daily_calls: int
    member_cooldown_seconds: int
    max_calls_per_turn: int
    default_count: int
    max_count: int
    allow_full_content: bool
    fetch_enabled: bool
    max_fetch_chars: int
    cache_ttl_minutes: int
    blocked_domains: list[str] = Field(default_factory=list, max_length=200)


def _group_list(values: list[str], field: str) -> list[int]:
    result: list[int] = []
    for raw in values:
        text = str(raw).strip()
        if not text:
            continue
        if not text.isdigit() or not 4 <= len(text) <= 12:  # noqa: PLR2004
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, f"{field} 里有不合法的群号：{text[:20]}"
            )
        value = int(text)
        if value not in result:
            result.append(value)
    return result


@router.get("/settings")
async def web_search_settings(_session: AdminReadSession) -> dict[str, Any]:
    return ok(
        {
            "settings": _settings_payload(load_settings()),
            "defaults": _settings_payload(WebSearchSettings()),
        }
    )


@router.put("/settings")
async def web_search_update_settings(
    body: SettingsBody, _session: AdminWriteSession
) -> dict[str, Any]:
    if body.default_count > body.max_count:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "默认条数不能大于最多条数"
        )
    try:
        settings = WebSearchSettings.model_validate(
            {
                **body.model_dump(),
                "allowed_groups": _group_list(body.allowed_groups, "允许的群"),
                "blocked_groups": _group_list(body.blocked_groups, "禁用的群"),
            }
        )
    except ValidationError as exc:
        fields = {_camel(str(item["loc"][0])): item["msg"] for item in exc.errors() if item["loc"]}
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "；".join(f"{key}: {msg}" for key, msg in fields.items()) or "设置不合法",
        ) from exc
    saved = save_settings(settings)
    await hub.notify_change("web_search")
    return ok({"settings": _settings_payload(saved)})


@router.get("/recent")
async def web_search_recent(
    _session: AdminReadSession,
    limit: int = Query(default=50, ge=1, le=200),
    group_id: str | None = Query(default=None, alias="groupId", pattern=r"^\d{4,12}$"),
) -> dict[str, Any]:
    rows = recent_calls(limit, group_id=int(group_id) if group_id else None)
    items = [
        {
            "at": row.get("at"),
            "groupId": str(row.get("group_id")) if row.get("group_id") is not None else None,
            "userId": str(row.get("user_id")) if row.get("user_id") is not None else None,
            "tool": row.get("tool"),
            "query": row.get("query"),
            "host": row.get("host"),
            "status": row.get("status"),
            "error": row.get("error"),
            "results": row.get("results"),
            "chars": row.get("chars"),
            "ms": row.get("ms"),
        }
        for row in rows
    ]
    await _label(items)
    return ok({"items": items})


class TestSearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=128)
    count: int = Field(default=5, ge=1, le=20)
    site: str | None = Field(default=None, max_length=80)
    lang: str | None = Field(default=None, max_length=8)
    after: str | None = Field(default=None, max_length=10)


def _http_error(exc: WebSearchError) -> HTTPException:
    if exc.code in {"no_key", "auth", "balance"}:
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    if exc.code in {"invalid_query", "query_too_long", "invalid_url", "private_url", "bad_request"}:
        return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    if exc.code in {"not_found", "forbidden"}:
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))


@router.post("/test")
async def web_search_test(body: TestSearchBody, _session: AdminWriteSession) -> dict[str, Any]:
    settings = load_settings()
    try:
        query = build_query(
            body.query,
            site=body.site or None,
            lang=body.lang or None,
            after=body.after or None,
            blocked_domains=settings.blocked_domains,
        )
        started = time.monotonic()
        response = await DeepSeekWebClient().search(query, count=body.count)
    except WebSearchError as exc:
        record_admin_call("search", "failed")
        raise _http_error(exc) from exc
    record_admin_call("search", "billed" if response.billed else "empty")
    return ok(
        {
            "query": response.query,
            "durationMs": int((time.monotonic() - started) * 1000),
            "attempts": response.attempts,
            "billed": response.billed,
            "results": [
                {
                    "title": clean_text(hit.title, 160),
                    "url": hit.url,
                    "site": hit.site_name or host_of(hit.url),
                    "date": short_date(hit.date) or None,
                    "summary": clean_text(hit.summary or hit.snippet, 800),
                    "score": hit.score,
                    "authority": hit.authority,
                }
                for hit in response.hits
            ],
        }
    )


class TestFetchBody(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


@router.post("/fetch-test")
async def web_search_fetch_test(body: TestFetchBody, _session: AdminWriteSession) -> dict[str, Any]:
    started = time.monotonic()
    try:
        page = await DeepSeekWebClient().fetch(body.url)
    except WebSearchError as exc:
        record_admin_call("fetch", "failed")
        raise _http_error(exc) from exc
    record_admin_call("fetch", "billed")
    text = strip_markdown_noise(page.content)
    return ok(
        {
            "url": page.url,
            "title": page.title or None,
            "chars": len(text),
            "durationMs": int((time.monotonic() - started) * 1000),
            "preview": text[:_TEST_PREVIEW_CHARS],
            "truncated": len(text) > _TEST_PREVIEW_CHARS,
        }
    )


@router.post("/cache/clear")
async def web_search_clear_cache(_session: AdminWriteSession) -> dict[str, Any]:
    return ok({"cleared": clear_caches()})


__all__ = ["router"]
