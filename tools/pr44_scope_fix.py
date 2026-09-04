from pathlib import Path


def replace(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        raise SystemExit(f"expected text not found in {path}: {old!r}")
    p.write_text(text.replace(old, new), encoding="utf-8")
    print(f"{path}: replaced {count} occurrence(s)")


hub = Path("src/plugins/yawn_core/webui/hub.py")
text = hub.read_text(encoding="utf-8")
start = text.index("_GROUP_ID_ENTITY_RESOURCES = frozenset(")
end = text.index("\n\nclass WebUIHub:", start)
strict = '''_GROUP_SCOPED_RESOURCES = frozenset(
    {
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
)
'''
text = text[:start] + strict + text[end:]
old = '''        resolved_group_id = (
            str(group_id)
            if group_id is not None
            else _legacy_group_scope(resource, entity_id)
        )
        scope = (
            {"groupId": resolved_group_id}
            if resolved_group_id is not None
            else None
        )
'''
new = '''        if resource in _GROUP_SCOPED_RESOURCES and group_id is None:
            raise ValueError(
                f"group-scoped entity change requires explicit group_id: {resource}"
            )
        scope = {"groupId": str(group_id)} if group_id is not None else None
'''
if old not in text:
    raise SystemExit("hub notify_change legacy resolution block drifted")
hub.write_text(text.replace(old, new), encoding="utf-8")

replacements = {
    "src/plugins/yawn_core/webui/agent_config_routes.py": [
        ('hub.notify_change("agent_config", str(group_id))', 'hub.notify_change("agent_config", str(group_id), group_id=group_id)'),
        ('hub.notify_change("group_feature", f"{group_id}:group_agent")', 'hub.notify_change("group_feature", f"{group_id}:group_agent", group_id=group_id)'),
    ],
    "src/plugins/yawn_core/webui/agent_persona_routes.py": [
        ('hub.notify_change("agent_persona", str(group_id))', 'hub.notify_change("agent_persona", str(group_id), group_id=group_id)'),
    ],
    "src/plugins/yawn_core/webui/agent_memory_routes.py": [
        ('hub.notify_change("agent_memory", str(group_id))', 'hub.notify_change("agent_memory", str(group_id), group_id=group_id)'),
        ('hub.notify_change("agent_memory", str(row.id))', 'hub.notify_change("agent_memory", str(row.id), group_id=group_id)'),
        ('hub.notify_change("agent_memory", str(memory_id))', 'hub.notify_change("agent_memory", str(memory_id), group_id=group_id)'),
        ('hub.notify_change("agent_member_data", f"{group_id}:{user_id}")', 'hub.notify_change("agent_member_data", f"{group_id}:{user_id}", group_id=group_id)'),
        ('hub.notify_change("agent_group_data", str(group_id))', 'hub.notify_change("agent_group_data", str(group_id), group_id=group_id)'),
    ],
    "src/plugins/yawn_core/webui/agent_privacy_routes.py": [
        ('hub.notify_change("agent_privacy", f"{group_id}:{user_id}")', 'hub.notify_change("agent_privacy", f"{group_id}:{user_id}", group_id=group_id)'),
    ],
    "src/plugins/yawn_core/webui/groups.py": [
        ('hub.notify_change("group_feature", f"{group_id}:{feature}")', 'hub.notify_change("group_feature", f"{group_id}:{feature}", group_id=group_id)'),
        ('hub.notify_change("agent_config", str(group_id))', 'hub.notify_change("agent_config", str(group_id), group_id=group_id)'),
        ('hub.notify_change("user_feature", f"{group_id}:{user_id}:{feature}")', 'hub.notify_change("user_feature", f"{group_id}:{user_id}:{feature}", group_id=group_id)'),
    ],
    "webui/src/agent-panels/AgentMessagesPanel.tsx": [
        ('disabled={query.transitioning}', 'disabled={query.stale}'),
    ],
    "webui/src/agent-panels/MemberProfilesPanel.tsx": [
        ('memberQuery.transitioning', 'memberQuery.stale'),
    ],
    "webui/src/agent-panels/MemoriesPanel.tsx": [
        ('const compact = async () => {\n    if (readOnly) return;', 'const compact = async () => {\n    if (readOnly || query.stale) return;'),
        ('const rebuild = async () => {\n    if (readOnly) return;', 'const rebuild = async () => {\n    if (readOnly || query.stale) return;'),
        ('<Button loading={status?.inFlight}>立即整理</Button>', '<Button loading={status?.inFlight} disabled={query.stale}>立即整理</Button>'),
        ('<Button>重建派生记忆</Button>', '<Button disabled={query.stale}>重建派生记忆</Button>'),
    ],
    "webui/src/agent-panels/RelationsPanel.tsx": [
        ('    if (readOnly) return;\n    setSaving(true);', '    if (readOnly || query.stale) return;\n    setSaving(true);'),
    ],
}
for path, pairs in replacements.items():
    for old_text, new_text in pairs:
        replace(path, old_text, new_text)

test = Path("tests/test_webui_hub.py")
test.write_text('''from __future__ import annotations

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
''', encoding="utf-8")

scoped = {
    "agent_config", "agent_persona", "agent_memory", "agent_group_data",
    "agent_member_data", "agent_privacy", "agent_relation", "group_feature", "user_feature",
}
for p in Path("src/plugins/yawn_core/webui").glob("*.py"):
    for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if "hub.notify_change(" not in line:
            continue
        if any(f'"{resource}"' in line for resource in scoped) and "group_id=" not in line:
            raise SystemExit(f"unscoped group entity change: {p}:{lineno}: {line.strip()}")
