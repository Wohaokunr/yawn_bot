from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import nonebot
import pytest

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if nonebot.get_plugin("yawn_core") is None:
    nonebot.load_from_toml("pyproject.toml")

WebUIHub = importlib.import_module("src.plugins.yawn_core.webui.hub").WebUIHub


@pytest.mark.asyncio
async def test_entity_change_separates_group_scope_and_entity_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = WebUIHub()
    payloads: list[dict[str, Any]] = []

    async def capture(payload: dict[str, Any]) -> None:
        payloads.append(payload)

    monkeypatch.setattr(hub, "broadcast", capture)
    await hub.notify_change("agent_relation", "77", group_id=12345)

    assert payloads == [
        {
            "type": "entity.changed",
            "data": {
                "resource": "agent_relation",
                "scope": {"groupId": "12345"},
                "entityId": "77",
            },
        }
    ]


@pytest.mark.asyncio
async def test_group_scoped_entity_change_requires_explicit_group_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = WebUIHub()
    payloads: list[dict[str, Any]] = []

    async def capture(payload: dict[str, Any]) -> None:
        payloads.append(payload)

    monkeypatch.setattr(hub, "broadcast", capture)
    with pytest.raises(ValueError, match="explicit group_id"):
        await hub.notify_change("agent_config", "67890")

    assert payloads == []


@pytest.mark.asyncio
async def test_entity_id_never_infers_group_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = WebUIHub()
    payloads: list[dict[str, Any]] = []

    async def capture(payload: dict[str, Any]) -> None:
        payloads.append(payload)

    monkeypatch.setattr(hub, "broadcast", capture)
    await hub.notify_change("global_resource", "12345:entity")

    assert payloads[0]["data"] == {
        "resource": "global_resource",
        "scope": None,
        "entityId": "12345:entity",
    }


def test_group_scoped_notify_calls_are_explicit() -> None:
    scoped_resources = {
        "agent_config",
        "agent_persona",
        "agent_memory",
        "agent_group_data",
        "agent_member_data",
        "agent_privacy",
        "agent_relation",
        "group_feature",
        "user_feature",
    }
    webui_root = PROJECT_ROOT / "src" / "plugins" / "yawn_core" / "webui"
    assert webui_root.is_dir(), "WebUI backend source root is missing"
    violations: list[str] = []

    for path in sorted(webui_root.glob("*.py")):
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if "hub.notify_change(" not in line:
                continue
            if not any(f'"{resource}"' in line for resource in scoped_resources):
                continue
            if "group_id=" not in line:
                violations.append(f"{path.name}:{line_number}: {line.strip()}")

    assert not violations, (
        "group-scoped entity changes need explicit group_id:\n" + "\n".join(violations)
    )
