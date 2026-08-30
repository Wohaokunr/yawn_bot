from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nonebot
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_COOLDOWN_EXAMPLE = 8
_PROBABILITY_EXAMPLE = 0.35


@pytest.fixture(scope="module")
def session_modules() -> SimpleNamespace:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()
    if nonebot.get_plugin("yawn_core") is None:
        nonebot.load_from_toml("pyproject.toml")
    return SimpleNamespace(
        helpers=importlib.import_module("src.plugins.yawn_core.session_interaction"),
        commands=importlib.import_module("src.plugins.yawn_core.yawn_agent.commands"),
    )


def test_session_helpers_are_small_and_strict(session_modules: SimpleNamespace) -> None:
    helpers = session_modules.helpers
    choices = (
        helpers.SessionChoice("cooldown", "主动冷却", ("冷却",)),
        helpers.SessionChoice("cache", "媒体缓存", ()),
    )

    assert helpers.resolve_choice("1", choices) == "cooldown"
    assert helpers.resolve_choice(" 冷却 ", choices) == "cooldown"
    assert helpers.resolve_choice("媒体缓存", choices) == "cache"
    assert helpers.resolve_choice("3", choices) is None
    assert helpers.is_cancel(" 取消 ") is True
    assert helpers.is_cancel("0") is True
    assert helpers.is_cancel("返回") is True
    assert helpers.is_back(" 返回 ") is True
    assert helpers.is_back("back") is True
    assert helpers.is_exit("返回") is False
    assert helpers.is_exit("退出") is True
    assert helpers.resolve_session_intent("菜单") is helpers.SessionIntent.MENU
    assert helpers.resolve_session_intent("返回") is helpers.SessionIntent.BACK
    assert helpers.resolve_session_intent("/help") is helpers.SessionIntent.NEW_COMMAND
    assert helpers.resolve_session_intent("普通输入") is helpers.SessionIntent.INPUT
    assert helpers.is_new_command(" /Agent状态") is True
    assert helpers.is_new_command("功能 1") is False
    assert helpers.parse_toggle("开启") is True
    assert helpers.parse_toggle("off") is False
    assert helpers.parse_toggle("随便") is None
    assert helpers.confirmation_matches("确认清理", "确认清理") is True
    assert helpers.confirmation_matches("确认", "确认清理") is False


@pytest.mark.asyncio
async def test_session_releases_block_for_new_command(
    session_modules: SimpleNamespace,
) -> None:
    helpers = session_modules.helpers

    class FinishedError(Exception):
        pass

    class FakeMatcher:
        block = True

        async def finish(self) -> None:
            raise FinishedError

    matcher = FakeMatcher()
    with pytest.raises(FinishedError):
        await helpers.pass_through_new_command(matcher, "/Agent状态")
    assert matcher.block is False


def test_agent_setting_parser_rejects_invalid_values(
    session_modules: SimpleNamespace,
) -> None:
    commands = session_modules.commands

    assert (
        commands._parse_agent_setting_value("cooldown_minutes", "8")
        == _COOLDOWN_EXAMPLE
    )
    assert commands._parse_agent_setting_value("media_cache_enabled", "开") is True
    assert (
        commands._parse_agent_setting_value("proactive_probability", "0.35")
        == _PROBABILITY_EXAMPLE
    )
    assert (
        commands._parse_agent_setting_value("participation_intensity", "平衡")
        == "平衡"
    )
    assert (
        commands._parse_agent_setting_value("participation_intensity", "high")
        == "活跃"
    )
    with pytest.raises(ValueError):
        commands._parse_agent_setting_value("cooldown_minutes", "-1")
    with pytest.raises(ValueError):
        commands._parse_agent_setting_value("proactive_probability", "1.5")
    with pytest.raises(ValueError):
        commands._parse_agent_setting_value("media_cache_enabled", "也许")


def test_agent_participation_intensity_updates_both_internal_probabilities(
    session_modules: SimpleNamespace,
) -> None:
    commands = session_modules.commands
    config = SimpleNamespace(
        proactive_probability=0.35,
        proactive_active_probability=0.25,
        explicit_wakeup_enabled=True,
        reply_trigger_enabled=True,
        short_conversation_enabled=True,
        proactive_enabled=True,
        proactive_active_enabled=True,
        idle_threshold_minutes=15,
        cooldown_minutes=8,
        daily_limit=30,
        media_cache_enabled=False,
        proactive_count=0,
        proactive_day=None,
    )

    assert (
        commands._get_agent_setting_value(config, "participation_intensity")
        == "平衡"
    )
    commands._set_agent_setting_value(config, "participation_intensity", "克制")
    assert config.proactive_probability == pytest.approx(0.18)
    assert config.proactive_active_probability == pytest.approx(0.10)
    assert (
        commands._get_agent_setting_value(config, "participation_intensity")
        == "克制"
    )

    menu = commands._build_agent_settings_menu(config)
    assert "参与强度" in menu
    assert "暖场基础概率" not in menu
    assert "加入话题概率" not in menu

    summary = commands._format_agent_runtime_summary(config, runtime_enabled=True)
    assert "参与强度：克制" in summary
    assert "暖场概率" not in summary
    assert "插话概率" not in summary


@pytest.mark.asyncio
async def test_agent_settings_session_rejects_bad_value_before_save(
    session_modules: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = session_modules.commands
    event = SimpleNamespace(group_id=10001, get_user_id=lambda: "20002")
    matcher = SimpleNamespace(
        state={
            "agent_settings_step": "value",
            "agent_settings_key": "cooldown_minutes",
        }
    )
    config = SimpleNamespace(cooldown_minutes=8, updated_by=None)
    prompts: list[tuple[Any, ...]] = []
    commits = 0

    async def fake_get_config(*_args: object, **_kwargs: object) -> object:
        return config

    async def fake_reject(*args: object, **_kwargs: object) -> None:
        prompts.append(args)

    async def fake_commit(_session: object) -> bool:
        nonlocal commits
        commits += 1
        return True

    monkeypatch.setattr(commands, "GroupMessageEvent", SimpleNamespace)
    monkeypatch.setattr(commands, "is_group_admin", lambda _event: True)
    monkeypatch.setattr(commands, "get_or_create_config", fake_get_config)
    monkeypatch.setattr(commands.agent_settings, "reject_arg", fake_reject)
    monkeypatch.setattr(commands, "_commit", fake_commit)

    await commands.handle_agent_settings_input(
        event,
        matcher,
        object(),
        "不是数字",
    )

    assert config.cooldown_minutes == _COOLDOWN_EXAMPLE
    assert commits == 0
    assert prompts and "请输入整数" in str(prompts[-1][1])


@pytest.mark.asyncio
async def test_agent_persona_session_previews_before_commit(
    session_modules: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = session_modules.commands
    event = SimpleNamespace(group_id=10001, get_user_id=lambda: "20002")
    matcher = SimpleNamespace(state={"agent_persona_step": "select"})
    config = SimpleNamespace(
        group_id=10001,
        persona="友好、自然、简洁的群友",
        persona_override={},
        persona_enabled=True,
        persona_version=1,
        updated_by=None,
    )
    prompts: list[tuple[Any, ...]] = []
    finishes: list[tuple[Any, ...]] = []
    commits = 0

    async def fake_get_config(*_args: object, **_kwargs: object) -> object:
        return config

    async def fake_reject(*args: object, **_kwargs: object) -> None:
        prompts.append(args)

    async def fake_finish(*args: object, **_kwargs: object) -> None:
        finishes.append(args)

    async def fake_commit(_session: object) -> bool:
        nonlocal commits
        commits += 1
        return True

    monkeypatch.setattr(commands, "GroupMessageEvent", SimpleNamespace)
    monkeypatch.setattr(commands, "is_group_admin", lambda _event: True)
    monkeypatch.setattr(commands, "get_or_create_config", fake_get_config)
    monkeypatch.setattr(commands.agent_persona, "reject_arg", fake_reject)
    monkeypatch.setattr(commands.agent_persona, "finish", fake_finish)
    monkeypatch.setattr(commands, "_commit", fake_commit)

    await commands.handle_agent_persona_input(event, matcher, object(), "语气")
    assert matcher.state["agent_persona_step"] == "value"

    await commands.handle_agent_persona_input(event, matcher, object(), "温和但简洁")
    assert matcher.state["agent_persona_step"] == "confirm"
    assert config.persona_override == {}
    assert commits == 0
    assert "修改前" in str(prompts[-1][1])
    assert "修改后：温和但简洁" in str(prompts[-1][1])

    await commands.handle_agent_persona_input(event, matcher, object(), "确认保存")
    assert config.persona_override["tone"] == "温和但简洁"
    assert commits == 1
    assert finishes and "已保存" in str(finishes[-1][0])


@pytest.mark.asyncio
async def test_agent_clear_never_deletes_from_same_command_message(
    session_modules: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = session_modules.commands
    event = SimpleNamespace(group_id=10001, get_user_id=lambda: "20002")
    matcher = SimpleNamespace(state={})
    sent: list[object] = []
    deletes = 0

    async def fake_send(message: object, **_kwargs: object) -> None:
        sent.append(message)

    async def fake_delete(*_args: object, **_kwargs: object) -> int:
        nonlocal deletes
        deletes += 1
        return 10

    monkeypatch.setattr(commands, "GroupMessageEvent", SimpleNamespace)
    monkeypatch.setattr(commands, "is_group_admin", lambda _event: True)
    monkeypatch.setattr(commands.agent_clear, "send", fake_send)
    monkeypatch.setattr(commands, "delete_group_memories", fake_delete)

    await commands.handle_agent_clear(
        event,
        matcher,
        object(),
        commands.Message("确认清理"),
        None,
    )

    assert deletes == 0
    assert sent and "单独回复「确认清理」" in str(sent[-1])


@pytest.mark.asyncio
async def test_agent_privacy_exit_requires_second_turn_confirmation(
    session_modules: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = session_modules.commands
    event = SimpleNamespace(group_id=10001, get_user_id=lambda: "20002")
    matcher = SimpleNamespace(state={})
    privacy = SimpleNamespace(opted_out=False)
    sent: list[object] = []
    finishes: list[tuple[Any, ...]] = []
    deletes = 0

    class FakeSession:
        async def get(self, *_args: object, **_kwargs: object) -> object:
            return privacy

    async def fake_send(message: object, **_kwargs: object) -> None:
        sent.append(message)

    async def fake_finish(*args: object, **_kwargs: object) -> None:
        finishes.append(args)

    async def fake_delete(*_args: object, **_kwargs: object) -> int:
        nonlocal deletes
        deletes += 1
        return 7

    monkeypatch.setattr(commands, "GroupMessageEvent", SimpleNamespace)
    monkeypatch.setattr(commands.agent_privacy, "send", fake_send)
    monkeypatch.setattr(commands.agent_privacy, "finish", fake_finish)
    monkeypatch.setattr(commands, "delete_member_memories", fake_delete)

    session = FakeSession()
    await commands.handle_agent_privacy(
        event,
        matcher,
        session,
        commands.Message("退出"),
        None,
    )

    assert deletes == 0
    assert matcher.state["agent_privacy_step"] == "confirm_exit"
    assert sent and "确认退出并删除" in str(sent[-1])

    await commands.handle_agent_privacy_input(
        event,
        matcher,
        session,
        "确认退出并删除",
    )
    assert deletes == 1
    assert privacy.opted_out is True
    assert finishes and "已删除 7 条" in str(finishes[-1][0])
