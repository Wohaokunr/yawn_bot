# ruff: noqa: PLR2004
"""回归：工具包发现（群公告找不到）与审计结果分类（主动发言“必定失败”）。"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

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
audit_outcome = importlib.import_module(f"{_PKG}.audit_outcome")

NOTICE_CAPS = tools.BotGroupCapabilities(
    role="admin",
    can_manage=True,
    actions=frozenset({"_send_group_notice", "_get_group_notice", "_del_group_notice"}),
)


def _context(*, admin: bool, allowlist: frozenset[str]) -> Any:
    return tools.ToolExecutionContext(
        bot=object(),
        group_id=1,
        actor_user_id=2,
        session=None,
        capabilities=NOTICE_CAPS,
        actor_can_manage=admin,
        privileged_allowlist=allowlist,
    )


async def _discover(args: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    result = await tools.execute_tool_with_meta(
        "discover_tools", args, context=_context(**kwargs)
    )
    return result.payload


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["notice", "公告", "announcement", "Notices"])
async def test_family_only_discovery_finds_announcement_tool(family: str) -> None:
    payload = await _discover(
        {"family": family},
        admin=True,
        allowlist=frozenset({"create_group_announcement"}),
    )
    assert payload["ok"] is True
    names = {item["name"] for item in payload["result"]["tools"]}
    assert "create_group_announcement" in names
    assert payload["result"]["mode"] == "toolpack"


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["发群公告", "帮我发个通知", "announcement"])
async def test_query_discovery_ranks_announcement_tool(query: str) -> None:
    payload = await _discover(
        {"query": query},
        admin=True,
        allowlist=frozenset({"create_group_announcement"}),
    )
    names = [item["name"] for item in payload["result"]["tools"]]
    assert names and names[0] == "create_group_announcement"


@pytest.mark.asyncio
async def test_discovery_without_query_or_family_is_an_argument_error() -> None:
    payload = await _discover({}, admin=True, allowlist=frozenset())
    assert payload["ok"] is False
    assert payload["error_code"] == "invalid_args"
    assert payload["hint"]


@pytest.mark.asyncio
async def test_unknown_family_falls_back_to_search_instead_of_empty() -> None:
    payload = await _discover(
        {"family": "发布群公告"},
        admin=True,
        allowlist=frozenset({"create_group_announcement"}),
    )
    assert payload["result"]["mode"] == "search"
    assert "create_group_announcement" in {
        item["name"] for item in payload["result"]["tools"]
    }


@pytest.mark.asyncio
async def test_member_learns_why_announcement_is_unavailable() -> None:
    payload = await _discover(
        {"query": "发群公告"},
        admin=False,
        allowlist=frozenset({"create_group_announcement"}),
    )
    result = payload["result"]
    assert "create_group_announcement" not in {item["name"] for item in result["tools"]}
    unavailable = {item["name"]: item for item in result["unavailable"]}
    assert unavailable["create_group_announcement"]["reason"] == "需要群主或管理员本人发起"
    # 只说明存在与原因：不给参数，也不会被下一轮当作 schema 装载。
    assert "args" not in unavailable["create_group_announcement"]
    assert "message" in result


@pytest.mark.asyncio
async def test_admin_learns_announcement_is_not_allowlisted() -> None:
    payload = await _discover(
        {"family": "notice"}, admin=True, allowlist=frozenset()
    )
    reasons = {item["name"]: item["reason"] for item in payload["result"]["unavailable"]}
    assert "白名单" in reasons["create_group_announcement"]


@pytest.mark.asyncio
async def test_critical_tools_never_appear_even_as_unavailable() -> None:
    payload = await _discover({"query": "踢人"}, admin=False, allowlist=frozenset())
    names = {item["name"] for item in payload["result"].get("unavailable", [])}
    names |= {item["name"] for item in payload["result"]["tools"]}
    assert "kick_member" not in names


def test_family_alias_normalization() -> None:
    assert router.normalize_tool_family("群公告") == "notice"
    assert router.normalize_tool_family("group-notices") == "notice"
    assert router.normalize_tool_family("files") == "file"
    assert router.normalize_tool_family("不存在") is None


@pytest.mark.parametrize(
    ("result", "outcome"),
    [
        ("success", "success"),
        ("speak", "success"),
        ("degraded_to_text", "success"),
        ("wait", "skipped"),
        ("close", "skipped"),
        ("delivery_unknown", "unknown"),
        ("error", "failed"),
        ("send_failed", "failed"),
        ("validation_failed", "failed"),
        ("something_new", "failed"),
    ],
)
def test_audit_outcome_classification(result: str, outcome: str) -> None:
    assert audit_outcome.classify_audit_result(result) == outcome
