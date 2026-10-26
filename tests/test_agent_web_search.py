# ruff: noqa: E501, PLR2004, ARG001, ARG005, RUF012, FBT001
"""网页搜索（DeepSeek Search / Fetch API）：客户端、策略配额、查询拼装与投影、handler、
发现门控、路由/预算/进度/声明核验，以及 WebUI 接口。全部用 httpx.MockTransport
或假客户端，不发真实请求。
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import nonebot
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()
if nonebot.get_plugin("yawn_core") is None:
    nonebot.load_from_toml("pyproject.toml")

_PKG = "src.plugins.yawn_core.yawn_agent"
tools = importlib.import_module(f"{_PKG}.tools")
router = importlib.import_module(f"{_PKG}.tool_router")
planner = importlib.import_module(f"{_PKG}.tool_planner")
budget = importlib.import_module(f"{_PKG}.turn_budget")
progress = importlib.import_module(f"{_PKG}.progress_notice")
claims = importlib.import_module(f"{_PKG}.speech_claims")
prompt = importlib.import_module(f"{_PKG}.prompt")
sections = importlib.import_module(f"{_PKG}.prompt_sections")
client_mod = importlib.import_module(f"{_PKG}.web_search_client")
policy = importlib.import_module(f"{_PKG}.web_search_policy")
output = importlib.import_module(f"{_PKG}.web_search_output")
handler = importlib.import_module(f"{_PKG}.tool_handlers.web")
registry = importlib.import_module(f"{_PKG}.tool_registry")
llm = importlib.import_module("src.plugins.yawn_core.llm")
webui_routes = importlib.import_module("src.plugins.yawn_core.webui.web_search")

GROUP = 123456
USER = 42
KEY = "test-key-0123456789abcdef"

SEARCH_PAYLOAD = {
    "results": [
        {
            "title": "DeepSeek 发布 Search API",
            "url": "https://news.example.com/a?utm_source=x",
            "snippet": "短摘要",
            "summary": "DeepSeek 在 2026 年 10 月发布了 Search 与 Fetch API。" * 3,
            "site_name": "示例新闻",
            "date": "2026-10-17T08:00:00",
            "rerank_score": 2.4,
            "authority": 3,
        },
        {"title": "重复", "url": "https://www.news.example.com/a/", "summary": "同一篇"},
        {"title": "屏蔽站", "url": "https://spam.bad.com/x", "summary": "广告"},
        {"title": "坏链接", "url": "ftp://x.com/file"},
    ]
}


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(policy, "STATE_DIR", tmp_path / "web")
    policy.reset_state_for_tests()
    client_mod.reset_stats_for_tests()
    handler.clear_caches()
    for name in ("AGENT_WEB_SEARCH_API_KEY", "DEEPSEEK_API_KEY", "AGENT_WEB_SEARCH_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_WEB_SEARCH_API_KEY", KEY)
    monkeypatch.setattr(client_mod.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(policy.asyncio, "sleep", _no_sleep)


async def _no_sleep(_seconds: float) -> None:
    return None


def _transport(handler_fn: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler_fn)


def _context(*, admin: bool = False, user: int | None = USER) -> Any:
    return tools.ToolExecutionContext(
        bot=object(),
        group_id=GROUP,
        actor_user_id=user,
        session=None,
        capabilities=tools.BotGroupCapabilities(role="member", can_manage=False, actions=frozenset()),
        actor_can_manage=admin,
        privileged_allowlist=frozenset(),
    )


def _save(**changes: Any) -> None:
    policy.save_settings(policy.WebSearchSettings(**changes))


# ---- 客户端 ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_sends_official_request_and_parses_results() -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=SEARCH_PAYLOAD)

    client = client_mod.DeepSeekWebClient(transport=_transport(respond))
    response = await client.search("  DeepSeek   search api ", count=99, need_content=True)
    request = seen[0]
    assert request.url == "https://api.deepseek.com/v1/tools/search"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert json.loads(request.content) == {"query": "DeepSeek search api", "count": 50, "need_content": True}
    assert response.raw_count == 4 and response.billed
    assert [hit.url for hit in response.hits][:1] == ["https://news.example.com/a?utm_source=x"]
    assert all(hit.url.startswith("http") for hit in response.hits)
    first = response.hits[0]
    assert first.site_name == "示例新闻" and first.score == 2.4 and first.authority == 3


@pytest.mark.asyncio
async def test_retries_rate_limit_then_succeeds() -> None:
    calls = {"n": 0}

    def respond(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"retry-after": "30"}, json={"error": {"message": "slow down"}})
        return httpx.Response(200, json={"results": []})

    response = await client_mod.DeepSeekWebClient(transport=_transport(respond)).search("x")
    assert calls["n"] == 3 and response.attempts == 3
    assert not response.billed  # 空结果不计费


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "auth"), (402, "balance"), (422, "bad_request"), (403, "forbidden")],
)
async def test_client_errors_fail_fast(status: int, code: str) -> None:
    calls = {"n": 0}

    def respond(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(status, json={"error": {"message": "nope"}})

    with pytest.raises(client_mod.WebSearchError) as excinfo:
        await client_mod.DeepSeekWebClient(transport=_transport(respond)).search("x")
    assert excinfo.value.code == code and calls["n"] == 1
    assert "nope" in str(excinfo.value)


@pytest.mark.asyncio
async def test_server_errors_exhaust_retries() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    with pytest.raises(client_mod.WebSearchError) as excinfo:
        await client_mod.DeepSeekWebClient(transport=_transport(respond)).search("x")
    assert excinfo.value.code == "upstream" and excinfo.value.status == 503
    assert client_mod.client_stats()["failures"] == 1


@pytest.mark.asyncio
async def test_timeout_is_retried_then_reported() -> None:
    calls = {"n": 0}

    def respond(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(client_mod.WebSearchError) as excinfo:
        await client_mod.DeepSeekWebClient(transport=_transport(respond)).search("x")
    assert excinfo.value.code == "timeout" and calls["n"] == client_mod.MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_query_length_checked_before_request() -> None:
    def respond(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不应被调用
        raise AssertionError("超长查询不应发出请求")

    with pytest.raises(client_mod.WebSearchError) as excinfo:
        await client_mod.DeepSeekWebClient(transport=_transport(respond)).search("字" * 129)
    assert excinfo.value.code == "query_too_long"


@pytest.mark.asyncio
async def test_fetch_annotates_policy_and_missing_content() -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "blocked"}})

    def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "none"}})

    with pytest.raises(client_mod.WebSearchError) as excinfo:
        await client_mod.DeepSeekWebClient(transport=_transport(forbidden)).fetch("https://example.com")
    assert excinfo.value.code == "forbidden" and "安全策略" in str(excinfo.value)
    with pytest.raises(client_mod.WebSearchError) as excinfo:
        await client_mod.DeepSeekWebClient(transport=_transport(missing)).fetch("https://example.com")
    assert excinfo.value.code == "not_found"


@pytest.mark.asyncio
async def test_fetch_returns_markdown_page() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"url": "https://example.com/post"}
        return httpx.Response(200, json={"title": "标题", "content": "# 标题\n\n正文"})

    page = await client_mod.DeepSeekWebClient(transport=_transport(respond)).fetch(" https://example.com/post ")
    assert page.title == "标题" and "正文" in page.content


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com",
        "https://user:pw@example.com",
        "http://localhost:8080",
        "http://127.0.0.1/x",
        "http://192.168.1.2/",
        "http://[::1]/",
        "http://nas/",
        "http://printer.local/",
        "https://",
    ],
)
def test_fetch_url_validation_rejects_unsafe_targets(url: str) -> None:
    with pytest.raises(client_mod.WebSearchError):
        client_mod.validate_fetch_url(url)


def test_api_key_resolution_order(monkeypatch: pytest.MonkeyPatch) -> None:
    assert client_mod.find_api_key().source == "AGENT_WEB_SEARCH_API_KEY"
    monkeypatch.delenv("AGENT_WEB_SEARCH_API_KEY")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-xxxx")
    assert client_mod.find_api_key().source == "DEEPSEEK_API_KEY"
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    monkeypatch.setattr(client_mod, "_env", lambda name: "")
    monkeypatch.setattr(llm.ai_config, "ai_base_url", "https://api.deepseek.com/v1")
    monkeypatch.setattr(llm.ai_config, "ai_api_key", "sk-provider-1234")
    info = client_mod.find_api_key()
    assert info is not None and info.source == "AI_API_KEY"
    assert info.masked == "sk-…1234"
    monkeypatch.setattr(llm.ai_config, "ai_base_url", "https://other.example.com/v1")
    assert client_mod.find_api_key() is None


# ---- 策略与配额 -------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_billed_calls_keep_quota() -> None:
    for outcome in ("empty", "failed", "billed"):
        reservation = await policy.reserve_call(GROUP, USER, kind="search", actor_is_admin=False, turn_calls=1)
        reservation.settle(outcome)
        reservation.settle(outcome)  # 重复结算不会多扣/多退
    usage = policy.usage_snapshot()
    assert usage["groups"][str(GROUP)] == 1
    assert usage["members"][f"{GROUP}:{USER}"] == 1
    assert usage["totals"] == {"search": 3, "fetch": 0, "billed": 1, "empty": 1, "failed": 1, "cached": 0}


@pytest.mark.asyncio
async def test_member_group_and_global_quotas() -> None:
    _save(member_daily_calls=1, group_daily_calls=2, global_daily_calls=3, member_cooldown_seconds=0)
    (await policy.reserve_call(GROUP, USER, kind="search", actor_is_admin=False)).settle("billed")
    with pytest.raises(policy.WebSearchPolicyError) as excinfo:
        await policy.reserve_call(GROUP, USER, kind="search", actor_is_admin=False)
    assert excinfo.value.reason == "member_quota"
    (await policy.reserve_call(GROUP, 7, kind="fetch", actor_is_admin=False)).settle("billed")
    with pytest.raises(policy.WebSearchPolicyError) as excinfo:
        await policy.reserve_call(GROUP, 8, kind="search", actor_is_admin=False)
    assert excinfo.value.reason == "group_quota"
    (await policy.reserve_call(999999, 9, kind="search", actor_is_admin=False)).settle("billed")
    with pytest.raises(policy.WebSearchPolicyError) as excinfo:
        await policy.reserve_call(888888, 10, kind="search", actor_is_admin=False)
    assert excinfo.value.reason == "global_quota"


@pytest.mark.asyncio
async def test_cooldown_waits_short_and_skips_within_turn() -> None:
    _save(member_cooldown_seconds=5)
    (await policy.reserve_call(GROUP, USER, kind="search", actor_is_admin=False)).settle("billed")
    # 同一回合第二次不吃冷却；跨回合短冷却会等一下（测试里 sleep 被替换）。
    second = await policy.reserve_call(GROUP, USER, kind="fetch", actor_is_admin=False, turn_calls=1)
    assert second.waited_seconds == 0
    third = await policy.reserve_call(GROUP, USER, kind="search", actor_is_admin=False)
    assert third.waited_seconds > 0
    _save(member_cooldown_seconds=60)
    with pytest.raises(policy.WebSearchPolicyError) as excinfo:
        await policy.reserve_call(GROUP, USER, kind="search", actor_is_admin=False)
    assert excinfo.value.reason == "cooldown"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("settings", "admin", "user", "turn_calls", "reason"),
    [
        ({"enabled": False}, False, USER, 0, "web_closed"),
        ({"group_mode": "allowlist", "allowed_groups": [1]}, False, USER, 0, "web_closed"),
        ({"blocked_groups": [GROUP]}, True, USER, 0, "web_closed"),
        ({"admin_only": True}, False, USER, 0, "actor_not_admin"),
        ({}, False, None, 0, "no_actor"),
        ({"max_calls_per_turn": 2}, False, USER, 2, "turn_limit"),
        ({"fetch_enabled": False}, False, USER, 0, "fetch_closed"),
    ],
)
async def test_access_rules(settings: dict[str, Any], admin: bool, user: int | None, turn_calls: int, reason: str) -> None:
    _save(**settings)
    with pytest.raises(policy.WebSearchPolicyError) as excinfo:
        await policy.reserve_call(GROUP, user, kind="fetch", actor_is_admin=admin, turn_calls=turn_calls)
    assert excinfo.value.reason == reason


def test_exposure_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    assert policy.exposure_reason(GROUP, actor_is_admin=False) is None
    _save(admin_only=True)
    assert policy.exposure_reason(GROUP, actor_is_admin=False) == "actor_not_admin"
    assert policy.exposure_reason(GROUP, actor_is_admin=True) is None
    _save(global_daily_calls=0)
    assert policy.exposure_reason(GROUP, actor_is_admin=True) == "web_paused"
    _save()
    monkeypatch.setattr(policy, "find_api_key", lambda: None)
    assert policy.exposure_reason(GROUP, actor_is_admin=True) == "web_no_key"


def test_settings_clean_domains_and_corrupt_file_falls_back(tmp_path: Path) -> None:
    settings = policy.WebSearchSettings(blocked_domains=["https://www.Bad.com/x", "bad.com", "", "not a domain", "*.ads.net"])
    assert settings.blocked_domains == ["bad.com", "ads.net"]
    path = policy.STATE_DIR / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{oops", encoding="utf-8")
    policy.reset_state_for_tests()
    assert policy.load_settings() == policy.WebSearchSettings()


def test_recent_calls_are_bounded_and_forgettable() -> None:
    for index in range(policy.RECENT_LIMIT + 5):
        policy.record_recent({"group_id": GROUP, "user_id": USER if index % 2 else 7, "tool": "web_search", "query": f"q{index}"})
    assert len(policy.recent_calls(500)) == policy.RECENT_LIMIT
    removed = policy.forget_member_recent(GROUP, USER)
    assert removed > 0
    assert all(row["user_id"] != USER for row in policy.recent_calls(500))


# ---- 查询拼装与结果投影 -----------------------------------------------------


def test_build_query_operators_and_blocked_domains() -> None:
    query = output.build_query(
        "DeepSeek 搜索",
        site="https://www.github.com/x",
        exclude_sites=["csdn.net", "github.com"],
        lang="EN",
        after="2026-01-01",
        blocked_domains=["bad.com"],
    )
    assert query == "DeepSeek 搜索 site:github.com -site:csdn.net lang:en after:2026-01-01 -site:bad.com"


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"query": ""}, "invalid_query"),
        ({"query": "x", "site": "not a domain"}, "invalid_query"),
        ({"query": "x", "lang": "xx"}, "invalid_query"),
        ({"query": "x", "after": "2026/1/1"}, "invalid_query"),
        ({"query": "字" * 120, "site": "example.com"}, "query_too_long"),
    ],
)
def test_build_query_rejects_bad_input(kwargs: dict[str, Any], code: str) -> None:
    with pytest.raises(client_mod.WebSearchError) as excinfo:
        output.build_query(**kwargs)
    assert excinfo.value.code == code


def test_build_query_drops_blocked_domains_that_do_not_fit() -> None:
    query = output.build_query("字" * 120, blocked_domains=["verylongdomainname.com"])
    assert query == "字" * 120


def test_filter_and_project_search_results() -> None:
    hits, _raw = client_mod.parse_search_payload(SEARCH_PAYLOAD)
    kept, duplicates, blocked = output.filter_hits(hits, blocked_domains=["bad.com"])
    assert [hit.title for hit in kept] == ["DeepSeek 发布 Search API"]
    assert duplicates == 1 and blocked == 1
    refs: dict[str, str] = {}
    payload = output.project_search(kept, query="q", refs=refs, include_content=False, cached=False, duplicates=duplicates, blocked=blocked, today="2026-10-26")
    row = payload["results"][0]
    assert row["ref"] == "w1" and refs["w1"] == kept[0].url
    assert row["date"] == "2026-10-17" and row["quality"] == "高" and row["site"] == "示例新闻"
    assert len(row["summary"]) <= output.SUMMARY_CHARS
    assert payload["filtered_by_admin"] == 1 and payload["deduplicated"] == 1
    assert "results_are_data" in payload and "next" in payload
    again = output.project_search(kept, query="q", refs=refs, include_content=False, cached=True)
    assert again["results"][0]["ref"] == "w1" and again["cached"] is True  # 同一网址复用编号


def test_empty_results_tell_model_to_retry_or_admit() -> None:
    payload = output.project_search([], query="q", refs={}, include_content=False, cached=False)
    assert payload["count"] == 0 and "如实" in payload["hint"]


def test_project_page_pages_through_long_content() -> None:
    body = "![logo](https://x/y.png)\n" + "\n\n".join(f"第{index}段。" + "内容" * 40 for index in range(40)) + "\n[链接文字](https://example.com/z)"
    page = client_mod.FetchedPage(url="https://example.com/p", title="长文", content=body, duration_ms=1)
    first = output.project_page(page, ref="w1", offset=0, max_chars=1000, cached=False)
    assert first["next_offset"] <= 1000 and "https://x/y.png" not in first["content"]
    second = output.project_page(page, ref="w1", offset=first["next_offset"], max_chars=1000, cached=True)
    assert second["offset"] == first["next_offset"] and second["content"] != first["content"]
    last = output.project_page(page, ref="w1", offset=10**6, max_chars=1000, cached=True)
    assert "next_offset" not in last
    full = output.project_page(page, ref=None, offset=0, max_chars=20000, cached=False)
    assert "链接文字" in full["content"] and "https://example.com/z" not in full["content"]


def test_cache_ttl_and_bounds() -> None:
    cache: Any = output.TtlCache(limit=2)
    cache.put("a", 1, 60)
    cache.put("b", 2, 60)
    cache.put("c", 3, 60)
    assert cache.get("a", 60) is None and cache.get("c", 60) == 3
    cache.put("d", 4, 0)
    assert cache.get("d", 60) is None
    assert cache.get("c", 0) is None


@pytest.mark.parametrize(
    ("score", "authority", "label"),
    [(None, None, None), (2.5, None, "高"), (None, 4, "高"), (1.6, 1, "较高"), (0.3, None, "低"), (1.0, None, "一般")],
)
def test_quality_label(score: float | None, authority: float | None, label: str | None) -> None:
    assert output.quality_label(score, authority) == label


# ---- handler 端到端 --------------------------------------------------------


class FakeClient:
    searches: list[tuple[str, int, bool]] = []
    fetches: list[str] = []
    search_error: client_mod.WebSearchError | None = None
    payload: dict[str, Any] = SEARCH_PAYLOAD

    def __init__(self, **_kwargs: Any) -> None:
        pass

    async def search(self, query: str, *, count: int = 5, need_content: bool = False) -> Any:
        FakeClient.searches.append((query, count, need_content))
        if FakeClient.search_error is not None:
            raise FakeClient.search_error
        hits, raw = client_mod.parse_search_payload(FakeClient.payload)
        return client_mod.SearchResponse(query=query, hits=tuple(hits), raw_count=raw, duration_ms=5)

    async def fetch(self, url: str) -> Any:
        FakeClient.fetches.append(url)
        return client_mod.FetchedPage(url=url, title="页面", content="正文" * 50, duration_ms=5)


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> type[FakeClient]:
    FakeClient.searches = []
    FakeClient.fetches = []
    FakeClient.search_error = None
    FakeClient.payload = SEARCH_PAYLOAD
    monkeypatch.setattr(handler, "DeepSeekWebClient", FakeClient)
    return FakeClient


@pytest.mark.asyncio
async def test_web_search_then_fetch_by_ref(fake_client: type[FakeClient]) -> None:
    _save(blocked_domains=["bad.com"])
    context = _context()
    result = await tools.execute_tool_with_meta("web_search", {"query": "DeepSeek 搜索", "count": 30, "include_content": True}, context=context)
    payload = result.payload
    assert payload["ok"] is True
    data = payload["result"]
    assert fake_client.searches == [("DeepSeek 搜索 -site:bad.com", 10, False)]  # 条数被夹到 max_count，正文未开放
    assert data["results"][0]["ref"] == "w1" and "note" in data and data["today"]
    fetched = await tools.execute_tool_with_meta("web_fetch", {"target": "W1"}, context=context)
    assert fake_client.fetches == ["https://news.example.com/a?utm_source=x"]
    assert fetched.payload["result"]["ref"] == "w1"
    assert context.ledger.web_calls == 2
    usage = policy.usage_snapshot()
    assert usage["totals"]["billed"] == 2 and usage["members"][f"{GROUP}:{USER}"] == 2
    recent = policy.recent_calls()
    assert {row["tool"] for row in recent} == {"web_search", "web_fetch"}


@pytest.mark.asyncio
async def test_repeated_search_hits_cache_without_billing(fake_client: type[FakeClient]) -> None:
    await tools.execute_tool_with_meta("web_search", {"query": "同一个问题"}, context=_context())
    second = await tools.execute_tool_with_meta("web_search", {"query": "同一个问题"}, context=_context())
    assert len(fake_client.searches) == 1
    assert second.payload["result"]["cached"] is True
    totals = policy.usage_snapshot()["totals"]
    assert totals["billed"] == 1 and totals["cached"] == 1


@pytest.mark.asyncio
async def test_fetch_offset_reads_from_cache_without_turn_cost(fake_client: type[FakeClient]) -> None:
    _save(max_calls_per_turn=1)
    context = _context()
    first = await tools.execute_tool_with_meta("web_fetch", {"target": "https://example.com/p", "max_chars": 500}, context=context)
    assert first.payload["ok"] is True
    more = await tools.execute_tool_with_meta("web_fetch", {"target": "https://example.com/p", "offset": 10}, context=context)
    assert more.payload["ok"] is True and more.payload["result"]["cached"] is True
    assert len(fake_client.fetches) == 1


@pytest.mark.asyncio
async def test_empty_and_failed_searches_are_refunded(fake_client: type[FakeClient]) -> None:
    fake_client.payload = {"results": []}
    empty = await tools.execute_tool_with_meta("web_search", {"query": "没有结果的词"}, context=_context())
    assert empty.payload["result"]["count"] == 0
    fake_client.search_error = client_mod.WebSearchError("balance", "余额不足", status=402)
    failed = await tools.execute_tool_with_meta("web_search", {"query": "另一个词"}, context=_context())
    assert failed.payload["ok"] is False
    assert failed.payload["error_code"] == "upstream" and "充值" in failed.payload["hint"]
    usage = policy.usage_snapshot()
    assert usage["groups"].get(str(GROUP), 0) == 0
    assert usage["totals"]["empty"] == 1 and usage["totals"]["failed"] == 1


@pytest.mark.asyncio
async def test_unknown_ref_and_blocked_fetch(fake_client: type[FakeClient]) -> None:
    missing = await tools.execute_tool_with_meta("web_fetch", {"target": "w3"}, context=_context())
    assert missing.payload["ok"] is False and missing.payload["error_code"] == "not_found"
    _save(blocked_domains=["example.com"])
    blocked = await tools.execute_tool_with_meta("web_fetch", {"target": "https://sub.example.com/x"}, context=_context())
    assert blocked.payload["ok"] is False and blocked.payload["error_code"] == "permission"
    assert fake_client.fetches == []


@pytest.mark.asyncio
async def test_policy_errors_reach_model_with_hint(fake_client: type[FakeClient]) -> None:
    _save(admin_only=True)
    result = await tools.execute_tool_with_meta("web_search", {"query": "x"}, context=_context(admin=False))
    assert result.payload["ok"] is False and result.payload["error_code"] == "permission"
    assert fake_client.searches == []
    admin = await tools.execute_tool_with_meta("web_search", {"query": "x"}, context=_context(admin=True))
    assert admin.payload["ok"] is True


@pytest.mark.asyncio
async def test_invalid_operator_rejected_without_request(fake_client: type[FakeClient]) -> None:
    result = await tools.execute_tool_with_meta("web_search", {"query": "x", "after": "昨天"}, context=_context())
    assert result.payload["ok"] is False and result.payload["error_code"] == "invalid_args"
    assert fake_client.searches == []


@pytest.mark.asyncio
async def test_info_tool_reports_quota() -> None:
    result = await tools.execute_tool_with_meta("web_search_info", {}, context=_context())
    data = result.payload["result"]
    assert data["available"] is True and data["quota"]["member_limit"] == 30
    assert "DeepSeek" in data["provider"]


# ---- 发现门控、路由、预算、进度、提示词、声明核验 ----------------------------


async def _discover(args: dict[str, Any], *, admin: bool = True) -> dict[str, Any]:
    result = await tools.execute_tool_with_meta("discover_tools", args, context=_context(admin=admin))
    return result.payload["result"]


@pytest.mark.asyncio
async def test_discovery_exposes_web_family_with_key() -> None:
    result = await _discover({"family": "联网"})
    names = {item["name"] for item in result["tools"]}
    assert names == {"web_search", "web_fetch", "web_search_info"}


@pytest.mark.asyncio
async def test_discovery_hides_web_family_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(policy, "find_api_key", lambda: None)
    result = await _discover({"family": "web"})
    assert not any(item["family"] == "web" for item in result["tools"])
    reasons = {item["reason"] for item in result.get("unavailable", [])}
    assert reasons == {"网页搜索还没有配置 DeepSeek API 密钥"}


@pytest.mark.parametrize("alias", ["web", "联网", "网页搜索", "search", "上网", "新闻"])
def test_web_family_aliases(alias: str) -> None:
    assert router.normalize_tool_family(alias) == "web"


def test_registry_and_planner_know_web_family() -> None:
    web = {item.name for item in registry.TOOL_DEFINITIONS if item.family == "web"}
    assert web == {"web_search", "web_fetch", "web_search_info"}
    assert all(registry.TOOL_BY_NAME[name].permission_level == "read" for name in web)
    assert "web" in planner.TOOLPACK_CATALOG
    assert "web" in planner._SYSTEM_PROMPT


def test_round_and_time_budget_for_web_tools() -> None:
    names = frozenset({"web_search", "web_fetch", "discover_tools"})
    assert router.dialogue_tool_round_limit(names) == router.WEB_TOOL_ROUNDS
    assert budget.turn_seconds(["web_search"]) == budget.WEB_TURN_SECONDS
    assert budget.turn_seconds(["run_python", "web_search"]) == budget.SANDBOX_TURN_SECONDS
    assert budget.discovery_round_limit(2, 2, ["web_search"]) == 5
    assert budget.discovery_deadline(10.0, 100.0, ["web_fetch"]) == 100.0 + budget.WEB_EXTENSION_SECONDS


def test_progress_phase_for_web_tools() -> None:
    state = progress.TurnProgress()
    state.enter_tool("web_search", family="web")
    assert state.phase == "web" and state.running_slow_tool
    state.leave_tool()
    assert state.phase == "thinking"


def test_prompt_guidance_includes_web_rules_only_when_loaded() -> None:
    def schema(name: str) -> dict[str, Any]:
        return {"type": "function", "function": {"name": name}}

    with_web = prompt.build_tool_guidance([schema("web_search")])
    assert "联网搜索" in with_web and "不执行" in with_web
    assert "联网搜索" not in prompt.build_tool_guidance([schema("search_group_memory")])
    assert any(section.id == "tools.web" for section in sections.SECTIONS)


@pytest.mark.parametrize(
    ("text", "changed"),
    [
        ("我搜了一下，DeepSeek 上周发布了新接口。", True),
        ("搜索结果显示今天会下雨。", True),
        ("我没搜到相关内容。", False),
        ("要不要我搜一下？", False),
        ("DeepSeek 上周发布了新接口。", False),
    ],
)
def test_unverified_search_claims_are_removed(text: str, changed: bool) -> None:
    result = claims.check_action_claims(text, frozenset())
    assert result.changed is changed
    assert claims.check_action_claims(text, frozenset({"web_search"})).changed is False


# ---- WebUI 接口 ------------------------------------------------------------


@pytest.mark.asyncio
async def test_webui_status_masks_key_and_reports_usage() -> None:
    reservation = await policy.reserve_call(GROUP, USER, kind="search", actor_is_admin=False)
    reservation.settle("billed")
    data = (await webui_routes.web_search_status(None))["data"]
    assert data["state"] == "ready" and data["configured"] is True
    assert data["keyMasked"] == "tes…cdef" and KEY not in json.dumps(data, ensure_ascii=False)
    assert data["usage"]["billed"] == 1 and data["usage"]["costYuan"] == pytest.approx(0.005)
    assert data["usage"]["groups"][0]["groupId"] == str(GROUP)
    assert data["settings"]["memberDailyCalls"] == 30


@pytest.mark.asyncio
async def test_webui_settings_validation_and_save() -> None:
    body = webui_routes.SettingsBody.model_validate(
        {
            **webui_routes._settings_payload(policy.WebSearchSettings()),
            "adminOnly": True,
            "allowedGroups": ["123456", "123456"],
            "blockedDomains": ["https://www.bad.com/x"],
        }
    )
    saved = (await webui_routes.web_search_update_settings(body, None))["data"]["settings"]
    assert saved["adminOnly"] is True and saved["allowedGroups"] == ["123456"]
    assert saved["blockedDomains"] == ["bad.com"]
    assert policy.load_settings().admin_only is True
    bad = webui_routes.SettingsBody.model_validate(
        {**webui_routes._settings_payload(policy.WebSearchSettings()), "defaultCount": 20, "maxCount": 5}
    )
    with pytest.raises(webui_routes.HTTPException) as excinfo:
        await webui_routes.web_search_update_settings(bad, None)
    assert excinfo.value.status_code == 422
    bad_group = webui_routes.SettingsBody.model_validate(
        {**webui_routes._settings_payload(policy.WebSearchSettings()), "blockedGroups": ["abc"]}
    )
    with pytest.raises(webui_routes.HTTPException):
        await webui_routes.web_search_update_settings(bad_group, None)


@pytest.mark.asyncio
async def test_webui_test_search_counts_globally_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webui_routes, "DeepSeekWebClient", FakeClient)
    FakeClient.payload = SEARCH_PAYLOAD
    FakeClient.search_error = None
    data = (await webui_routes.web_search_test(webui_routes.TestSearchBody(query="测试"), None))["data"]
    assert data["billed"] is True and data["results"][0]["site"] == "示例新闻"
    usage = policy.usage_snapshot()
    assert usage["totals"]["billed"] == 1 and usage["groups"] == {}
    FakeClient.search_error = client_mod.WebSearchError("auth", "密钥无效", status=401)
    with pytest.raises(webui_routes.HTTPException) as excinfo:
        await webui_routes.web_search_test(webui_routes.TestSearchBody(query="测试2"), None)
    assert excinfo.value.status_code == 503
    FakeClient.search_error = None


@pytest.mark.asyncio
async def test_webui_recent_and_cache_clear(fake_client: type[FakeClient]) -> None:
    await tools.execute_tool_with_meta("web_search", {"query": "最近"}, context=_context())
    recent = (await webui_routes.web_search_recent(None, limit=10, group_id=str(GROUP)))["data"]["items"]
    assert recent[0]["query"] == "最近" and recent[0]["groupId"] == str(GROUP)
    cleared = (await webui_routes.web_search_clear_cache(None))["data"]["cleared"]
    assert cleared == 1
