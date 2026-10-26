# ruff: noqa: TID252, TRY003, E501
"""网页搜索工具族：用 DeepSeek 官方 Search / Fetch API 查资料、读网页。

这里只负责：策略与配额（``web_search_policy``）、缓存、调用客户端
（``web_search_client``）、把结果投影成模型需要的最小信息
（``web_search_output``）。本族工具都是只读的，不往群里发任何东西。
"""

from __future__ import annotations

import time
from typing import Any

from ..capabilities import user_can_manage_group
from ..context import now_beijing
from ..log import dbg
from ..tool_args import (
    ERROR_INVALID_ARGS,
    ERROR_NOT_FOUND,
    ERROR_PERMISSION,
    ERROR_QUOTA,
    ERROR_UPSTREAM,
    ToolError,
)
from ..tool_execution import ToolExecutionContext, ToolHandlerResult
from ..web_search_client import (
    DeepSeekWebClient,
    FetchedPage,
    SearchResponse,
    WebSearchError,
    client_stats,
    find_api_key,
)
from ..web_search_output import (
    TtlCache,
    build_query,
    fetch_cache_key,
    filter_hits,
    host_of,
    project_page,
    project_search,
    search_cache_key,
)
from ..web_search_policy import (
    WebSearchPolicyError,
    check_access,
    domain_matches,
    estimated_cost_yuan,
    load_settings,
    quota_status,
    record_cached,
    record_recent,
    reserve_call,
    usage_snapshot,
)

FAMILY = "web"
NAMES = frozenset(["web_search", "web_fetch", "web_search_info"])
# 成员当天剩余次数不多时才把余量告诉模型，免得每次都提。
_LOW_QUOTA_WARNING = 5
_search_cache: TtlCache[SearchResponse] = TtlCache(limit=256)
_fetch_cache: TtlCache[FetchedPage] = TtlCache(limit=64)

_POLICY_ERRORS: dict[str, tuple[str, str]] = {
    "web_closed": (ERROR_PERMISSION, "管理员没有在本群开放网页搜索；如实说明，可以凭已有知识回答但要说明没联网核实"),
    "fetch_closed": (ERROR_PERMISSION, "管理员关闭了网页正文抓取，只能根据 web_search 的摘要回答"),
    "web_no_key": (ERROR_UPSTREAM, "网页搜索没有配置密钥，告诉对方暂时不能联网查"),
    "web_paused": (ERROR_QUOTA, "管理员暂停了网页搜索，如实说明"),
    "actor_not_admin": (ERROR_PERMISSION, "本群网页搜索只允许群主或管理员使用，如实说明"),
    "no_actor": (ERROR_PERMISSION, "只有群成员明确请求时才能联网搜索"),
    "turn_limit": (ERROR_QUOTA, "这一轮搜索次数到上限了：根据已有结果回答，没查到的部分如实说"),
    "cooldown": (ERROR_QUOTA, "冷却还没结束：先用已有结果回答"),
    "member_quota": (ERROR_QUOTA, "这位成员今天的搜索次数用完了，如实说明，不要换工具绕过"),
    "group_quota": (ERROR_QUOTA, "本群今天的搜索次数用完了，如实说明"),
    "global_quota": (ERROR_QUOTA, "今天的搜索总预算用完了，如实说明"),
}
_CLIENT_ERRORS: dict[str, tuple[str, str]] = {
    "invalid_query": (ERROR_INVALID_ARGS, "query 写成几个关键词，不要空着"),
    "query_too_long": (ERROR_INVALID_ARGS, "把 query 缩短到几个核心关键词（加上运算符不超过 128 字），再搜一次"),
    "invalid_url": (ERROR_INVALID_ARGS, "web_fetch 只接受 web_search 返回的 ref（如 w1）或完整的 http(s) 网址"),
    "private_url": (ERROR_PERMISSION, "不能抓取内网/本机地址，如实说明"),
    "auth": (ERROR_UPSTREAM, "搜索服务密钥失效了，告诉对方暂时不能联网查，请管理员检查"),
    "balance": (ERROR_UPSTREAM, "搜索服务余额不足，告诉对方暂时不能联网查，请管理员充值"),
    "no_key": (ERROR_UPSTREAM, "网页搜索没有配置密钥，告诉对方暂时不能联网查"),
    "rate_limited": (ERROR_UPSTREAM, "搜索服务正忙，先用已有信息回答，或请对方稍后再问"),
    "timeout": (ERROR_UPSTREAM, "搜索超时了，可以换个更简单的关键词再试一次，仍然不行就如实说"),
    "network": (ERROR_UPSTREAM, "搜索服务连不上，告诉对方暂时不能联网查，不要编造结果"),
    "upstream": (ERROR_UPSTREAM, "搜索服务暂时出错，不要编造结果"),
    "bad_response": (ERROR_UPSTREAM, "搜索服务返回异常，不要编造结果"),
    "forbidden": (ERROR_PERMISSION, "这个网站不让抓取：改用 web_search 结果里的 summary，或打开另一条结果"),
    "not_found": (ERROR_NOT_FOUND, "这个页面没取到正文：换一条搜索结果打开"),
    "too_large": (ERROR_UPSTREAM, "页面太大取不下来：换一条搜索结果"),
    "bad_request": (ERROR_INVALID_ARGS, "检查参数后再试一次"),
}


def _policy_error(exc: WebSearchPolicyError) -> ToolError:
    code, hint = _POLICY_ERRORS.get(exc.reason, (ERROR_PERMISSION, None))
    return ToolError(str(exc), code=code, hint=hint)


def _client_error(exc: WebSearchError) -> ToolError:
    code, hint = _CLIENT_ERRORS.get(exc.code, (ERROR_UPSTREAM, "搜索失败了，不要编造结果"))
    return ToolError(str(exc), code=code, hint=hint)


async def _actor_is_admin(context: ToolExecutionContext) -> bool:
    if context.actor_can_manage is not None:
        return bool(context.actor_can_manage)
    if context.actor_user_id is None:
        return False
    return await user_can_manage_group(context.bot, context.group_id, context.actor_user_id)


def _today() -> str:
    moment = now_beijing()
    return f"{moment:%Y-%m-%d}（{'一二三四五六日'[moment.weekday()]}）"


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part for part in value.replace("，", ",").split(",") if part.strip()]
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []


def _recent(context: ToolExecutionContext, **fields: Any) -> None:
    record_recent({"group_id": context.group_id, "user_id": context.actor_user_id, **fields})


async def _web_search(args: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
    settings = load_settings()
    ledger = context.ledger
    try:
        query = build_query(
            str(args.get("query") or ""),
            site=str(args.get("site") or "").strip() or None,
            exclude_sites=_string_list(args.get("exclude_sites")),
            lang=str(args.get("lang") or "").strip() or None,
            after=str(args.get("after") or "").strip() or None,
            before=str(args.get("before") or "").strip() or None,
            blocked_domains=settings.blocked_domains,
        )
    except WebSearchError as exc:
        raise _client_error(exc) from exc
    count = settings.clamp_count(args.get("count"))
    include_content = bool(args.get("include_content")) and settings.allow_full_content
    actor_is_admin = await _actor_is_admin(context)
    key = search_cache_key(query, count, include_content)
    ttl = settings.cache_ttl_minutes * 60
    started = time.monotonic()
    response = _search_cache.get(key, ttl)
    cached = response is not None
    if response is not None:
        try:
            check_access(
                context.group_id,
                context.actor_user_id,
                kind="search",
                actor_is_admin=actor_is_admin,
                turn_calls=ledger.web_calls,
            )
        except WebSearchPolicyError as exc:
            raise _policy_error(exc) from exc
        ledger.web_calls += 1
        record_cached("search")
    else:
        try:
            reservation = await reserve_call(
                context.group_id,
                context.actor_user_id,
                kind="search",
                actor_is_admin=actor_is_admin,
                turn_calls=ledger.web_calls,
            )
        except WebSearchPolicyError as exc:
            raise _policy_error(exc) from exc
        ledger.web_calls += 1
        try:
            response = await DeepSeekWebClient().search(
                query, count=count, need_content=include_content
            )
        except WebSearchError as exc:
            reservation.settle("failed")
            _recent(context, tool="web_search", query=query, status="error", error=exc.code,
                    ms=int((time.monotonic() - started) * 1000))
            raise _client_error(exc) from exc
        reservation.settle("billed" if response.billed else "empty")
        if response.hits:
            _search_cache.put(key, response, ttl)
    hits, duplicates, blocked = filter_hits(response.hits, blocked_domains=settings.blocked_domains)
    payload = project_search(
        hits,
        query=response.query,
        refs=ledger.web_refs,
        include_content=include_content,
        cached=cached,
        duplicates=duplicates,
        blocked=blocked,
        today=_today(),
    )
    if args.get("include_content") and not settings.allow_full_content:
        payload["note"] = "管理员没开放整页正文，只给了摘要；需要细节用 web_fetch 打开单条"
    quota = quota_status(context.group_id, context.actor_user_id)
    if "member_left" in quota and quota["member_left"] <= _LOW_QUOTA_WARNING:
        payload["searches_left_today"] = quota["member_left"]
    duration = int((time.monotonic() - started) * 1000)
    _recent(
        context,
        tool="web_search",
        query=response.query,
        status="cached" if cached else ("ok" if hits else "empty"),
        results=len(hits),
        ms=duration,
    )
    dbg(f"群 {context.group_id} web_search: {len(hits)} 条 cached={cached} {duration}ms")
    return payload


def _resolve_target(args: dict[str, Any], context: ToolExecutionContext) -> tuple[str, str | None]:
    raw = str(args.get("target") or args.get("url") or args.get("ref") or "").strip()
    if not raw:
        raise ToolError("需要 target：web_search 返回的 ref（如 w1）或网址", code=ERROR_INVALID_ARGS)
    refs = context.ledger.web_refs
    folded = raw.casefold()
    if folded in refs:
        return refs[folded], folded
    if folded.startswith("w") and folded[1:].isdigit():
        raise ToolError(
            f"本轮没有编号 {raw} 的搜索结果",
            code=ERROR_NOT_FOUND,
            hint="ref 只在同一轮有效：先 web_search，再用它返回的 ref",
        )
    by_url = {url: ref for ref, url in refs.items()}
    return raw, by_url.get(raw)


async def _web_fetch(args: dict[str, Any], context: ToolExecutionContext) -> dict[str, Any]:
    settings = load_settings()
    ledger = context.ledger
    url, ref = _resolve_target(args, context)
    if settings.blocked_domains and domain_matches(host_of(url), settings.blocked_domains):
        raise ToolError(
            "管理员屏蔽了这个网站",
            code=ERROR_PERMISSION,
            hint="换一条其他网站的结果，或只根据摘要回答",
        )
    offset = max(int(args.get("offset") or 0), 0)
    max_chars = settings.max_fetch_chars
    if args.get("max_chars") is not None:
        max_chars = max(500, min(int(args["max_chars"]), settings.max_fetch_chars))
    actor_is_admin = await _actor_is_admin(context)
    key = fetch_cache_key(url)
    # 正文缓存至少保留 10 分钟：分段续读（offset）不该每段都重新计费。
    ttl = max(settings.cache_ttl_minutes * 60, 600)
    started = time.monotonic()
    page = _fetch_cache.get(key, ttl)
    cached = page is not None
    if page is not None:
        try:
            check_access(
                context.group_id,
                context.actor_user_id,
                kind="fetch",
                actor_is_admin=actor_is_admin,
                # 续读同一页不占本轮次数。
                turn_calls=0 if offset else ledger.web_calls,
            )
        except WebSearchPolicyError as exc:
            raise _policy_error(exc) from exc
        if not offset:
            ledger.web_calls += 1
        record_cached("fetch")
    else:
        try:
            reservation = await reserve_call(
                context.group_id,
                context.actor_user_id,
                kind="fetch",
                actor_is_admin=actor_is_admin,
                turn_calls=ledger.web_calls,
            )
        except WebSearchPolicyError as exc:
            raise _policy_error(exc) from exc
        ledger.web_calls += 1
        try:
            page = await DeepSeekWebClient().fetch(url)
        except WebSearchError as exc:
            reservation.settle("failed")
            _recent(context, tool="web_fetch", host=host_of(url), status="error", error=exc.code,
                    ms=int((time.monotonic() - started) * 1000))
            raise _client_error(exc) from exc
        reservation.settle("billed")
        _fetch_cache.put(key, page, ttl)
    payload = project_page(page, ref=ref, offset=offset, max_chars=max_chars, cached=cached)
    duration = int((time.monotonic() - started) * 1000)
    _recent(
        context,
        tool="web_fetch",
        host=host_of(page.url),
        status="cached" if cached else "ok",
        chars=payload.get("chars"),
        ms=duration,
    )
    return payload


def _info(context: ToolExecutionContext) -> dict[str, Any]:
    settings = load_settings()
    key = find_api_key()
    usage = usage_snapshot()
    return {
        "available": bool(settings.enabled and settings.group_open(context.group_id) and key),
        "provider": "DeepSeek 官方搜索（Search / Fetch API）",
        "fetch_enabled": settings.fetch_enabled,
        "full_content": settings.allow_full_content,
        "results_per_search": f"默认 {settings.default_count}，最多 {settings.max_count}",
        "per_turn_limit": settings.max_calls_per_turn,
        "quota": quota_status(context.group_id, context.actor_user_id),
        "operators": 'query 里可直接写 site:域名、-site:域名、"精确短语"；也可以用 site/exclude_sites/lang/after/before 参数',
        "today": _today(),
        "spent_today_yuan": estimated_cost_yuan(int(usage["totals"].get("billed", 0))),
    }


async def handle(name: str, args: dict[str, Any], context: ToolExecutionContext) -> ToolHandlerResult:
    if name == "web_search":
        return ToolHandlerResult(await _web_search(args, context))
    if name == "web_fetch":
        return ToolHandlerResult(await _web_fetch(args, context))
    if name == "web_search_info":
        return ToolHandlerResult(_info(context))
    raise ValueError(f"未知工具: {name}")


def cache_stats() -> dict[str, Any]:
    return {"search": _search_cache.stats(), "fetch": _fetch_cache.stats(), "client": client_stats()}


def clear_caches() -> int:
    return _search_cache.clear() + _fetch_cache.clear()


__all__ = ["FAMILY", "NAMES", "cache_stats", "clear_caches", "handle"]
