from __future__ import annotations

from typing import Any

import pytest

from src.plugins.yawn_core.webui.hub import WebUIHub


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
async def test_entity_change_keeps_legacy_agent_group_calls_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = WebUIHub()
    payloads: list[dict[str, Any]] = []

    async def capture(payload: dict[str, Any]) -> None:
        payloads.append(payload)

    monkeypatch.setattr(hub, "broadcast", capture)
    await hub.notify_change("agent_config", "67890")

    assert payloads[0]["data"]["scope"] == {"groupId": "67890"}
    assert payloads[0]["data"]["entityId"] == "67890"
