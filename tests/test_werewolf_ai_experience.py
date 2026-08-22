"""狼人杀 AI 玩家智能化改进的聚焦测试。

覆盖：结构化已知信息钩子沉淀、[你的已知信息] 上下文渲染、
发言提示场景化与阵营策略、投票决策提示增强。
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path
from typing import Any

import nonebot
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VICTIM_SEAT = 3
PLUGIN_ROOT = PROJECT_ROOT / "src" / "plugins" / "yawn_core"


@pytest.fixture(scope="module")
def ww_modules() -> tuple[Any, Any, Any, Any, Any]:
    """不加载整个 yawn_core，隔离导入狼人杀状态机。"""
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()
    if nonebot.get_plugin("nonebot_plugin_orm") is None:
        nonebot.load_plugin("nonebot_plugin_orm")
    package = types.ModuleType("yawn_core")
    package.__path__ = [str(PLUGIN_ROOT)]
    sys.modules.setdefault("yawn_core", package)
    ww_package = types.ModuleType("yawn_core.yawn_werewolf")
    ww_package.__path__ = [str(PLUGIN_ROOT / "yawn_werewolf")]
    sys.modules.setdefault("yawn_core.yawn_werewolf", ww_package)
    state = importlib.import_module("yawn_core.yawn_werewolf.state")
    engine = importlib.import_module("yawn_core.yawn_werewolf.engine")
    ai_player = importlib.import_module("yawn_core.yawn_werewolf.ai_player")
    roles = importlib.import_module("yawn_core.yawn_werewolf.roles")
    config = importlib.import_module("yawn_core.yawn_werewolf.config")
    return state, engine, ai_player, roles, config


@pytest.fixture
def driver_of(ww_modules: tuple[Any, Any, Any, Any, Any]) -> Any:
    """把 AIDriver 注册进 ai_player 注册表，测试结束自动摘除。"""
    _, _, ai_player, _, _ = ww_modules
    registered: list[Any] = []

    def _register(game: Any) -> Any:
        driver = ai_player.AIDriver(game)
        ai_player._drivers[game.group_id] = driver
        registered.append(game.group_id)
        return driver

    yield _register
    for group_id in registered:
        ai_player._drivers.pop(group_id, None)


async def _dm_true(*_args: object, **_kwargs: object) -> bool:
    return True


# ── 结构化已知信息钩子 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_seer_check_result_is_deposited_as_knowledge(
    ww_modules: tuple[Any, Any, Any, Any, Any],
    driver_of: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, engine, _ai_player, roles, config = ww_modules
    seer = state.PlayerState(1, 1, roles.Role.SEER, roles.Faction.GOOD, is_ai=True)
    wolf = state.PlayerState(2, 2, roles.Role.WEREWOLF, roles.Faction.WOLF)
    game = state.Game(group_id=301, host_user_id=1, players=[seer, wolf])
    game.action_queue.put_nowait(
        state.Action(state.ActionKind.CHECK, seer.user_id, 2),
    )
    driver = driver_of(game)
    monkeypatch.setattr(engine, "_dm", _dm_true)

    await engine._phase_seer(game, config.Config(ww_night_timeout=1))

    known = driver.knowledge[seer.seat]
    assert known.checks == [(game.round_no, 2, "狼人")]


@pytest.mark.asyncio
async def test_witch_potions_are_deposited_as_knowledge(
    ww_modules: tuple[Any, Any, Any, Any, Any],
    driver_of: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, engine, _ai_player, roles, config = ww_modules
    witch = state.PlayerState(1, 1, roles.Role.WITCH, roles.Faction.GOOD, is_ai=True)
    victim = state.PlayerState(3, 3, roles.Role.VILLAGER, roles.Faction.GOOD)
    game = state.Game(group_id=302, host_user_id=1, players=[witch, victim])
    game.action_queue.put_nowait(
        state.Action(state.ActionKind.SAVE, witch.user_id),
    )
    driver = driver_of(game)
    monkeypatch.setattr(engine, "_dm", _dm_true)

    saved, poison_seat = await engine._phase_witch(
        game,
        config.Config(ww_night_timeout=1),
        kill_seat=3,
    )

    assert saved and poison_seat is None
    known = driver.knowledge[witch.seat]
    assert known.save_used and known.saved_seat == VICTIM_SEAT
    assert not known.poison_used

    game.action_queue.put_nowait(
        state.Action(state.ActionKind.POISON, witch.user_id, 3),
    )
    _, poison_seat = await engine._phase_witch(
        game,
        config.Config(ww_night_timeout=1),
        kill_seat=None,
    )

    assert poison_seat == VICTIM_SEAT
    known = driver.knowledge[witch.seat]
    assert known.poison_used and known.poisoned_seat == VICTIM_SEAT


@pytest.mark.asyncio
async def test_wolf_mates_are_deposited_each_night(
    ww_modules: tuple[Any, Any, Any, Any, Any],
    driver_of: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, engine, _ai_player, roles, config = ww_modules
    wolves = [
        state.PlayerState(1, 1, roles.Role.WEREWOLF, roles.Faction.WOLF, is_ai=True),
        state.PlayerState(2, 2, roles.Role.WEREWOLF, roles.Faction.WOLF, is_ai=True),
    ]
    villager = state.PlayerState(3, 3, roles.Role.VILLAGER, roles.Faction.GOOD)
    game = state.Game(group_id=303, host_user_id=1, players=[*wolves, villager])
    driver = driver_of(game)
    monkeypatch.setattr(engine, "_dm", _dm_true)

    await engine._phase_wolves(game, config.Config(ww_wolf_timeout=0))

    assert driver.knowledge[wolves[0].seat].wolf_mates == [2]
    assert driver.knowledge[wolves[1].seat].wolf_mates == [1]
    # 非狼座位不应有狼队友记录
    assert villager.seat not in driver.knowledge


# ── [你的已知信息] 渲染 ────────────────────────────────────


def test_render_context_includes_knowledge_block(
    ww_modules: tuple[Any, Any, Any, Any, Any],
    driver_of: Any,
) -> None:
    state, _, ai_player, roles, _ = ww_modules
    seer = state.PlayerState(1, 1, roles.Role.SEER, roles.Faction.GOOD, is_ai=True)
    villager = state.PlayerState(3, 3, roles.Role.VILLAGER, roles.Faction.GOOD)
    wolf = state.PlayerState(5, 5, roles.Role.WEREWOLF, roles.Faction.WOLF, is_ai=True)
    game = state.Game(group_id=304, host_user_id=1, players=[seer, villager, wolf])
    game.round_no = 3
    driver = driver_of(game)
    ai_player.note_check(game, seer.seat, 1, 3, "好人")
    ai_player.note_check(game, seer.seat, 2, 5, "狼人")
    # 塞满远超 private_log 渲染上限的私聊，模拟长局文本截断
    for i in range(40):
        ai_player.on_dm(game, seer, f"噪声私聊 {i}")

    context = ai_player._render_context(driver, seer)

    assert "[你的已知信息]" in context
    assert "第1夜查验 3号=好人" in context
    assert "第2夜查验 5号=狼人" in context
    assert "噪声私聊 0" not in context  # 早于最近 15 条的文本被截掉

    villager_context = ai_player._render_context(driver, villager)
    assert "[你的已知信息]" not in villager_context
    assert "查验" not in villager_context


def test_render_context_includes_wolf_and_potion_knowledge(
    ww_modules: tuple[Any, Any, Any, Any, Any],
    driver_of: Any,
) -> None:
    state, _, ai_player, roles, _ = ww_modules
    wolf = state.PlayerState(2, 2, roles.Role.WEREWOLF, roles.Faction.WOLF, is_ai=True)
    witch = state.PlayerState(4, 4, roles.Role.WITCH, roles.Faction.GOOD, is_ai=True)
    game = state.Game(group_id=305, host_user_id=1, players=[wolf, witch])
    driver = driver_of(game)
    ai_player.note_wolf_mates(game, wolf.seat, [4, 6])
    ai_player.note_potion(game, witch.seat, save_seat=2)
    ai_player.note_potion(game, witch.seat, poison_seat=6)

    wolf_context = ai_player._render_context(driver, wolf)
    assert "狼队友座位：4、6（" in wolf_context
    witch_context = ai_player._render_context(driver, witch)
    assert "解药已用（救过 2号）" in witch_context
    assert "毒药已用（毒过 6号）" in witch_context


# ── 发言场景化与阵营策略 ───────────────────────────────────


@pytest.mark.asyncio
async def test_llm_speech_prompts_are_scene_and_faction_aware(
    ww_modules: tuple[Any, Any, Any, Any, Any],
    driver_of: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _, ai_player, roles, _ = ww_modules
    seer = state.PlayerState(1, 1, roles.Role.SEER, roles.Faction.GOOD, is_ai=True)
    wolf = state.PlayerState(2, 2, roles.Role.WEREWOLF, roles.Faction.WOLF, is_ai=True)
    game = state.Game(group_id=306, host_user_id=1, players=[seer, wolf])
    driver = driver_of(game)
    calls: list[dict[str, Any]] = []

    async def complete_spy(messages: Any, **kwargs: Any) -> str:
        calls.append({"messages": messages, **kwargs})
        return "发言"

    monkeypatch.setattr(ai_player, "complete", complete_spy)

    game.phase = state.Phase.LAST_WORDS
    await ai_player._llm_speech(driver, seer)
    game.phase = state.Phase.SHERIFF_FINAL_SPEECH
    await ai_player._llm_speech(driver, seer)
    game.phase = state.Phase.DAY_SPEECH
    await ai_player._llm_speech(driver, wolf)

    last_words_user = calls[0]["messages"][-1]["content"]
    assert "遗言" in last_words_user and "查验记录" in last_words_user
    final_user = calls[1]["messages"][-1]["content"]
    assert "终辩" in final_user
    day_system = calls[2]["messages"][0]["content"]
    assert "悍跳" in day_system
    day_user = calls[2]["messages"][-1]["content"]
    assert "白天发言" in day_user


# ── 决策提示增强 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_vote_instruction_lists_candidates_and_goal(
    ww_modules: tuple[Any, Any, Any, Any, Any],
    driver_of: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _, ai_player, roles, _ = ww_modules
    voter = state.PlayerState(1, 1, roles.Role.VILLAGER, roles.Faction.GOOD, is_ai=True)
    game = state.Game(
        group_id=307,
        host_user_id=1,
        players=[voter],
        phase=state.Phase.DAY_VOTE,
        vote_targets=[2, 3],
    )
    driver = driver_of(game)
    instructions: list[str] = []

    async def decide_spy(
        _driver: Any, _player: Any, instruction: str, **_kwargs: Any
    ) -> None:
        instructions.append(instruction)

    monkeypatch.setattr(ai_player, "_llm_decide", decide_spy)

    await ai_player._vote_decide(driver, voter)

    assert "本轮可投对象：2、3。" in instructions[0]
    assert "放逐" in instructions[0]
    action = game.action_queue.get_nowait()
    assert action.kind is state.ActionKind.ABSTAIN
    assert action.actor_user_id == voter.user_id
