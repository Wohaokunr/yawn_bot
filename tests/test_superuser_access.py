# ruff: noqa: ANN001, ANN201, ARG005, E501, PLR2004, F811, F401, FBT001, FBT003
"""超级管理员免群管身份用全部能力：统一判定、群聊工具、私聊代办/转告、网页、群命令。"""

from __future__ import annotations

import asyncio
import importlib
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from test_group_manage import BOT_ID, FakeBot, _manage_env
from test_webui_user_accounts import (
    API,
    G_MEMBER,
    G_OTHER,
    G_OWNED,
    GOOD_PASSWORD,
    MEMBER,
    OWNER,
    STRANGER,
    _isolated,
    _login,
    accounts,
    env,
)

_PKG = "src.plugins.yawn_core"
su = importlib.import_module(f"{_PKG}.superuser")
gm = importlib.import_module(f"{_PKG}.yawn_agent.group_manage")
capabilities = importlib.import_module(f"{_PKG}.yawn_agent.capabilities")
agent_bridge = importlib.import_module(f"{_PKG}.yawn_agent.agent_bridge")
private_context = importlib.import_module(f"{_PKG}.yawn_agent.private_context")
bridge_config = importlib.import_module(f"{_PKG}.private_chat_bridge")
permission = importlib.import_module(f"{_PKG}.permission")
command_access = importlib.import_module(f"{_PKG}.command_access")
manage_routes = importlib.import_module(f"{_PKG}.webui.group_manage_routes")
config_routes = importlib.import_module(f"{_PKG}.webui.agent_config_routes")
persona_routes = importlib.import_module(f"{_PKG}.webui.agent_persona_routes")
user_scope = importlib.import_module(f"{_PKG}.webui.user_scope")
route_helpers = importlib.import_module(f"{_PKG}.webui.agent_route_helpers")

GID = G_OWNED
SU = 10030  # 不在任何群里的超级管理员


@pytest.fixture(autouse=True)
def _superusers(monkeypatch):
    monkeypatch.setattr(su, "superuser_ids", lambda: frozenset({str(SU)}))
    cfg = bridge_config.private_agent_config
    for name in ("superuser_full_access", "superuser_skip_daily_limits", "superuser_bypass_group_policy"):
        monkeypatch.setattr(cfg, name, True)


def run(coro):
    return asyncio.run(coro)


def _flag(monkeypatch, name: str, value: bool) -> None:
    monkeypatch.setattr(bridge_config.private_agent_config, name, value)


# ── 统一判定 ────────────────────────────────────────────────────────────


def test_superuser_helpers(monkeypatch):
    assert su.is_superuser(SU) and su.is_superuser(str(SU)) and not su.is_superuser(OWNER)
    assert not su.is_superuser(None)
    assert su.superuser_full_access(SU)
    assert su.effective_role(None, SU) == "superuser"
    assert su.effective_role("member", SU) == "superuser"
    assert su.effective_role("owner", SU) == "owner"
    assert su.effective_role("admin", OWNER) == "admin"
    assert su.is_manager_role("superuser") and su.is_owner_level("superuser")
    assert not su.is_owner_level("admin")
    assert su.role_label("superuser") == "超级管理员"
    assert su.bot_role_hint("admin") is None
    assert "Bot 是群主" in (su.bot_role_hint("admin", need_owner=True) or "")
    assert "设为管理员" in (su.bot_role_hint("member") or "")
    _flag(monkeypatch, "superuser_full_access", False)
    assert su.is_superuser(SU) and not su.superuser_full_access(SU)
    assert su.effective_role("member", SU) == "member"
    assert not su.skips_daily_limits(SU) and not su.bypasses_group_policy(SU)


def test_superuser_sub_switches(monkeypatch):
    _flag(monkeypatch, "superuser_skip_daily_limits", False)
    assert su.superuser_full_access(SU) and not su.skips_daily_limits(SU)
    _flag(monkeypatch, "superuser_bypass_group_policy", False)
    assert not su.bypasses_group_policy(SU)


def _event(role: str, user_id: int):
    return SimpleNamespace(sender=SimpleNamespace(role=role), get_user_id=lambda: str(user_id))


def test_group_command_permission(monkeypatch):
    assert permission.can_manage_group(_event("member", SU))
    assert permission.can_manage_group(_event("admin", MEMBER))
    assert not permission.can_manage_group(_event("member", MEMBER))
    assert not permission.is_group_admin(_event("member", SU))  # 原函数只回答 QQ 身份
    _flag(monkeypatch, "superuser_full_access", False)
    assert not permission.can_manage_group(_event("member", SU))


def test_help_menu_matches_handler(monkeypatch):
    ctx = command_access.CommandAccessContext(user_id=SU, group_id=GID, is_superuser=True)
    assert ctx.allows(scope="group", feature=None, permission="group_admin")
    _flag(monkeypatch, "superuser_full_access", False)
    assert not ctx.allows(scope="group", feature=None, permission="group_admin")
    assert ctx.allows(scope="group", feature=None, permission="superuser")


# ── 群聊 Agent 工具的调用者权限 ─────────────────────────────────────────


def test_capabilities_actor_checks():
    bot = FakeBot()
    bot.roles[STRANGER] = "owner"
    assert run(capabilities.user_can_manage_group(bot, GID, SU))
    assert not any(params.get("user_id") == SU for _api, params in bot.calls)  # 不向 QQ 查
    assert run(capabilities.actor_is_group_owner(bot, GID, SU))
    # 超级管理员等同群主：能处置管理员，不能处置群主
    assert run(capabilities.actor_can_act_on_target(bot, GID, SU, OWNER))
    assert not run(capabilities.actor_can_act_on_target(bot, GID, SU, STRANGER))
    # 普通成员不受影响
    assert not run(capabilities.user_can_manage_group(bot, GID, MEMBER))


def test_capabilities_switch_off(monkeypatch):
    _flag(monkeypatch, "superuser_full_access", False)
    bot = FakeBot()
    assert not run(capabilities.user_can_manage_group(bot, GID, SU))
    assert not run(capabilities.actor_is_group_owner(bot, GID, SU))


# ── 群管代办 ────────────────────────────────────────────────────────────


def test_verify_superuser_not_in_group():
    bot = FakeBot()
    access = run(gm.verify(bot, GID, SU, source="private"))
    assert access.user_role == "superuser" and access.is_owner and access.is_superuser
    # 等同群主：踢人、改群名这些默认只给群主的也能用
    assert {"kick", "rename", "mute"} <= set(access.allowed_actions())
    assert access.as_dict()["your_role_label"] == "超级管理员"


def test_verify_superuser_who_is_owner_stays_owner():
    bot = FakeBot()
    bot.roles[SU] = "owner"
    access = run(gm.verify(bot, GID, SU, source="webui"))
    assert access.user_role == "owner" and not access.is_superuser


def test_verify_superuser_bypasses_group_policy(monkeypatch):
    bot = FakeBot()
    gm.set_policy(GID, {"enabled": False, "private_enabled": False, "disabled_actions": ["mute"]}, actor="t")
    access = run(gm.verify(bot, GID, SU, source="private"))
    assert "mute" in access.allowed_actions()
    _flag(monkeypatch, "superuser_bypass_group_policy", False)
    with pytest.raises(gm.ManageError, match="关掉了群管代办"):
        run(gm.verify(bot, GID, SU, source="private"))


def test_verify_superuser_still_needs_bot_admin():
    bot = FakeBot()
    bot.roles[BOT_ID] = "member"
    with pytest.raises(gm.ManageError) as exc:
        run(gm.verify(bot, GID, SU, source="private"))
    assert "Bot 在这个群不是管理员" in str(exc.value)
    assert "超级管理员身份替代不了" in (exc.value.hint or "")


def test_bot_owner_only_action_hint_for_superuser():
    bot = FakeBot()
    access = run(gm.verify(bot, GID, SU, source="webui"))
    assert "超级管理员身份替代不了" in (gm.action_block_reason(access, "title") or "")


def test_verify_superuser_switch_off(monkeypatch):
    _flag(monkeypatch, "superuser_full_access", False)
    with pytest.raises(gm.ManageError):
        run(gm.verify(FakeBot(), GID, SU, source="private"))


def test_superuser_can_mute_admin_when_bot_is_owner():
    bot = FakeBot()
    bot.roles[BOT_ID] = "owner"
    bot.roles[STRANGER] = "member"
    access = run(gm.verify(bot, GID, SU, source="webui"))
    summary, normalized = run(gm.describe(access, "mute", {"member": "副管", "minutes": 5}, "g"))
    assert "副管" in summary and normalized["target_id"] == 10010


def test_superuser_skips_daily_limit(monkeypatch):
    _flag(monkeypatch, "group_manage_daily", 1)
    gm._reserve(SU)
    gm._reserve(SU)  # 第二次不超额
    assert gm.used_today(SU) == 2
    gm._reserve(OWNER)
    with pytest.raises(gm.ManageError, match="次数用完"):
        gm._reserve(OWNER)


def test_superuser_candidate_groups_and_hint(env):
    async def go():
        async with env["factory"]() as session:
            return await gm.candidate_groups(session, SU)

    rows = run(go())
    assert {gid for gid, _name, _role in rows} == {G_OWNED, G_MEMBER, G_OTHER}
    assert {role for _gid, _name, role in rows} == {"superuser"}
    assert gm.manager_hint(SU)


# ── 私聊转告 ────────────────────────────────────────────────────────────


def test_bridge_policy_and_quota(monkeypatch):
    gm.set_policy(GID, {"allow_relay": False}, actor="t")
    assert not agent_bridge.relay_allowed_in_group(GID)
    assert not agent_bridge.relay_allowed_in_group(GID, OWNER)
    assert agent_bridge.relay_allowed_in_group(GID, SU)
    _flag(monkeypatch, "agent_bridge_relay_daily", 1)
    monkeypatch.setattr(agent_bridge, "_load_bridge", lambda _uid: {"relays": [{"created": "x"}] * 5, "handoffs": []})
    monkeypatch.setattr(agent_bridge, "_today_count", lambda items, now: len(items))
    agent_bridge.check_relay_quota(SU, GID)  # 不抛
    with pytest.raises(agent_bridge.BridgeError):
        agent_bridge.check_relay_quota(OWNER, GID)


def test_private_tool_groups_for_superuser(env):
    known = [private_context.SharedGroup(group_id=G_OWNED, name="猫猫群", agent_readable=True, nickname="超管")]

    async def go(user_id):
        async with env["factory"]() as session:
            return await private_context.superuser_tool_groups(session, user_id, known)

    groups = run(go(SU))
    assert groups[0].group_id == G_OWNED
    assert {group.group_id for group in groups} == {G_OWNED, G_MEMBER, G_OTHER}
    assert groups[0].nickname == "超管"  # 他本来就在的群保持原样
    assert [group.group_id for group in run(go(OWNER))] == [G_OWNED]


# ── 网页 ────────────────────────────────────────────────────────────────


@pytest.fixture
def su_web(env, monkeypatch):
    factory = env["factory"]

    @asynccontextmanager
    async def fake_session():
        async with factory() as session:
            yield session

    for module in (config_routes, persona_routes, manage_routes):
        monkeypatch.setattr(module, "get_session", fake_session)
    bot = FakeBot()
    monkeypatch.setattr(manage_routes, "_bot", lambda: bot)
    # 超级管理员不走实时确认：即使确认结果是 None（Bot 不在线）也能写
    env["live"]["value"] = None
    accounts.set_password(SU, GOOD_PASSWORD, source="test")
    assert _login(env["client"], SU).status_code == 200
    return SimpleNamespace(client=env["client"], bot=bot, env=env)


def test_superuser_me_lists_all_groups(su_web):
    client = su_web.client
    me = client.get(f"{API}/me").json()["data"]
    assert me["isSuperuser"] and me["managedGroupCount"] == 3
    groups = client.get(f"{API}/me/groups").json()["data"]
    assert {row["groupId"] for row in groups} == {str(G_OWNED), str(G_MEMBER), str(G_OTHER)}
    assert {row["role"] for row in groups} == {"superuser"}
    detail = client.get(f"{API}/me/groups/{G_OTHER}").json()["data"]
    assert detail["role"] == "superuser" and detail["verified"] and detail["superuser"]


def test_superuser_edits_any_group_agent_without_live_bot(su_web):
    client = su_web.client
    got = client.get(f"{API}/agent/groups/{G_OTHER}/config")
    assert got.status_code == 200
    patched = client.patch(
        f"{API}/agent/groups/{G_OTHER}/config",
        json={"cooldownMinutes": 9, "version": got.json()["data"].get("version")},
    )
    assert patched.status_code == 200, patched.text
    row = gm.audit_rows(G_OTHER)[0]
    assert row["action"] == "agent_config" and row["actor_role"] == "superuser"
    persona = client.get(f"{API}/agent/groups/{G_MEMBER}/persona")
    assert persona.status_code == 200
    # 不存在的群仍然 403
    assert client.get(f"{API}/agent/groups/123/config").status_code == 403


def test_superuser_manage_status_and_policy(su_web):
    client = su_web.client
    data = client.get(f"{API}/me/groups/{G_OWNED}/manage").json()["data"]
    assert data["available"] and data["yourRole"] == "superuser" and data["isOwner"] and data["isSuperuser"]
    assert data["yourRoleLabel"] == "超级管理员"
    saved = client.put(f"{API}/me/groups/{G_OWNED}/manage/policy", json={"notify_group": True})
    assert saved.status_code == 200, saved.text
    assert gm.audit_rows(G_OWNED)[0]["actor_role"] == "superuser"


def test_superuser_switch_off_restores_scope(su_web, monkeypatch):
    _flag(monkeypatch, "superuser_full_access", False)
    client = su_web.client
    assert client.get(f"{API}/me/groups").json()["data"] == []
    assert client.get(f"{API}/agent/groups/{G_OTHER}/config").status_code == 403


def test_superuser_sees_full_projection(monkeypatch):
    assert not route_helpers.is_guest_view(SimpleNamespace(role="user", user_id=SU))
    assert route_helpers.is_guest_view(SimpleNamespace(role="user", user_id=OWNER))
    assert route_helpers.is_guest_view(SimpleNamespace(role="guest", user_id=None))
    _flag(monkeypatch, "superuser_full_access", False)
    assert route_helpers.is_guest_view(SimpleNamespace(role="user", user_id=SU))


def test_single_group_lookup_falls_back_for_superuser():
    ptools = importlib.import_module(f"{_PKG}.yawn_agent.private_tools")
    mine = private_context.SharedGroup(group_id=G_OWNED, name="猫猫群", agent_readable=True)
    other = private_context.SharedGroup(group_id=G_OTHER, name="别人的群", agent_readable=True)
    ctx = ptools.PrivateToolContext(
        session=None, user_id=SU, bot_id=None, groups=[mine], now=None, superuser_groups=[other]
    )
    assert ptools._single_group(ctx, "别人的群").group_id == G_OTHER
    assert ptools._single_group(ctx, "猫猫").group_id == G_OWNED
    # 不点名时（批量）只看他本人所在的群
    assert [group.group_id for group in ptools._match_groups(ctx, None)] == [G_OWNED]
    plain = ptools.PrivateToolContext(session=None, user_id=OWNER, bot_id=None, groups=[mine], now=None)
    with pytest.raises(ptools.ToolError):
        ptools._single_group(plain, "别人的群")
