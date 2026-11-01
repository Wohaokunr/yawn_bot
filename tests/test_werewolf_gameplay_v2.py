# ruff: noqa: PLR2004, ARG001
"""狼人杀玩法 2.0：新角色、房规、交互与复盘的回归测试。"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path
from typing import Any

import nonebot
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROJECT_ROOT / "src" / "plugins" / "yawn_core"


@pytest.fixture(scope="module")
def ww() -> types.SimpleNamespace:
    """隔离导入狼人杀子插件（不加载整个 yawn_core）。"""
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
    names = ("state", "engine", "roles", "config", "dsl", "house_rules")
    return types.SimpleNamespace(
        **{
            name: importlib.import_module(f"yawn_core.yawn_werewolf.{name}")
            for name in names
        }
    )


def _player(ww: Any, seat: int, role: Any, **kwargs: Any) -> Any:
    return ww.state.PlayerState(seat, seat, role, ww.roles.ROLE_FACTION[role], **kwargs)


def _game(ww: Any, roles: list[Any], **kwargs: Any) -> Any:
    players = [_player(ww, i + 1, role) for i, role in enumerate(roles)]
    return ww.state.Game(group_id=900, host_user_id=1, players=players, **kwargs)


@pytest.fixture
def capture(ww: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """截获群播报与私聊，跳过拟真等待。"""
    sent: dict[str, list[Any]] = {"announce": [], "dm": []}

    async def announce(_game: Any, text: Any, **_kwargs: Any) -> None:
        sent["announce"].append(str(text))

    async def dm(_game: Any, player: Any, text: str) -> bool:
        sent["dm"].append((player.seat, text))
        return True

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(ww.engine, "_announce", announce)
    monkeypatch.setattr(ww.engine, "_dm", dm)
    monkeypatch.setattr(ww.engine, "_ban", noop)
    monkeypatch.setattr(ww.engine, "_unban", noop)
    monkeypatch.setattr(ww.engine, "_whole_ban", noop)
    monkeypatch.setattr(ww.engine, "_IDLE_PHASE_SECONDS", (0.0, 0.0))
    return sent


def _queue(ww: Any, game: Any, kind: Any, actor: int, value: Any = None) -> None:
    game.action_queue.put_nowait(ww.state.Action(kind, actor, value))


# ── 角色与板子 ────────────────────────────────────────────


def test_boards_are_consistent(ww: Any) -> None:
    roles = ww.roles
    for key, spec in roles.BOARDS.items():
        assert spec.key == key
        assert spec.description
        for count, composition in spec.counts.items():
            assert sum(composition.values()) == count
            assert any(r in roles.WOLF_ROLES for r in composition)
            assert any(r in roles.GOD_ROLES for r in composition)
    assert "新手局" in roles.suggest_boards(6)
    assert roles.resolve_board_key("新手") == "新手局"
    assert roles.resolve_board_key("骑士") is None  # 有歧义


def test_every_role_has_card_and_guide(ww: Any) -> None:
    roles = ww.roles
    for role in roles.Role:
        card = roles.build_role_card(1, role, 12, rule_notes=["警长：关"])
        assert card.startswith("═══ 狼人杀 · 身份卡 ═══")
        assert "本局房规" in card
        assert role.value in roles.role_guide(role)
    assert roles.parse_role("守") is roles.Role.GUARD
    assert roles.parse_role("白狼") is roles.Role.WHITE_WOLF_KING


def test_hidden_wolf_checks_as_good(ww: Any) -> None:
    roles = ww.roles
    assert roles.seer_sees_wolf(roles.Role.WOLF_KING)
    assert not roles.seer_sees_wolf(roles.Role.HIDDEN_WOLF)
    assert not roles.seer_sees_wolf(roles.Role.HALFBLOOD)


# ── 房规 ──────────────────────────────────────────────────


def test_house_rules_set_and_describe(ww: Any) -> None:
    hr = ww.house_rules
    rules, msg = hr.set_rule(hr.DEFAULT_RULES, "警长", "关")
    assert rules.sheriff is False and "关" in msg
    rules, msg = hr.set_rule(rules, "胜负", "屠城")
    assert rules.win_mode == "city"
    rules, msg = hr.set_rule(rules, "发言时长", "500")
    assert msg.startswith("×") and rules.speech_seconds is None
    rules, _ = hr.set_rule(rules, "发言时长", "90秒")
    assert rules.speech_seconds == 90
    assert "警长：关" in hr.diff_from_default(rules)
    assert hr.preset_name(hr.PRESETS["快速"]) == "快速"
    _, msg = hr.set_rule(rules, "不存在", "开")
    assert msg.startswith("×")


# ── DSL ───────────────────────────────────────────────────


def test_dsl_new_forms(ww: Any) -> None:
    dsl, kinds = ww.dsl, ww.state.ActionKind
    guard = dsl.parse_dm_action("守护3", 1)
    assert guard is not None and guard.kind is kinds.GUARD and guard.value == 3
    boom = dsl.parse_dm_action("自爆5", 1)
    assert boom is not None and boom.kind is kinds.SELF_DETONATE and boom.value == 5
    cn = dsl.parse_dm_action("刀十二号", 1)
    assert cn is not None and cn.kind is kinds.KILL and cn.value == 12
    full = dsl.parse_dm_action("查验３。", 1)
    assert full is not None and full.kind is kinds.CHECK and full.value == 3
    skip = dsl.parse_dm_action("空刀", 1)
    assert skip is not None and skip.kind is kinds.SKIP
    say = dsl.parse_dm_action("说 刀三号！", 1)
    assert say is not None and say.kind is kinds.SAY and say.aux == "刀三号！"


# ── 夜晚结算 ──────────────────────────────────────────────


def _patch_night(
    ww: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    guard: Any,
    kill: Any,
    witch: tuple[bool, Any],
) -> None:
    async def fixed(value: Any) -> Any:
        return value

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(ww.engine, "_phase_guard", lambda *_a: fixed(guard))
    monkeypatch.setattr(ww.engine, "_phase_wolves", lambda *_a: fixed(kill))
    monkeypatch.setattr(ww.engine, "_phase_witch", lambda *_a: fixed(witch))
    for name in (
        "_phase_halfblood",
        "_phase_seer",
        "_phase_elder",
        "_phase_gravekeeper",
    ):
        monkeypatch.setattr(ww.engine, name, noop)


@pytest.mark.asyncio
async def test_guard_blocks_knife(
    ww: Any, capture: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = ww.roles.Role
    game = _game(ww, [r.WEREWOLF, r.GUARD, r.VILLAGER, r.WITCH])
    _patch_night(ww, monkeypatch, guard=3, kill=3, witch=(False, None))
    deaths = await ww.engine._run_night(game, ww.config.Config())
    assert deaths == []


@pytest.mark.asyncio
async def test_guard_and_save_conflict_kills(
    ww: Any, capture: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = ww.roles.Role
    game = _game(ww, [r.WEREWOLF, r.GUARD, r.VILLAGER, r.WITCH])
    _patch_night(ww, monkeypatch, guard=3, kill=3, witch=(True, None))
    deaths = await ww.engine._run_night(game, ww.config.Config())
    assert [p.seat for p in deaths] == [3]
    game2 = _game(ww, [r.WEREWOLF, r.GUARD, r.VILLAGER, r.WITCH])
    game2.rules = ww.house_rules.HouseRules(guard_save_conflict=False)
    deaths = await ww.engine._run_night(game2, ww.config.Config())
    assert deaths == []


@pytest.mark.asyncio
async def test_guard_phase_rejects_repeat_target(ww: Any, capture: Any) -> None:
    r = ww.roles.Role
    kinds = ww.state.ActionKind
    game = _game(ww, [r.WEREWOLF, r.GUARD, r.VILLAGER], board="预女猎守")
    guard = game.player_by_seat(2)
    guard.guard_last_target = 3
    _queue(ww, game, kinds.GUARD, 2, 3)
    _queue(ww, game, kinds.GUARD, 2, 2)
    seat = await ww.engine._phase_guard(game, ww.config.Config())
    assert seat == 2
    assert guard.guard_last_target == 2
    assert any("目标无效" in text for _, text in capture["dm"])


@pytest.mark.asyncio
async def test_witch_first_night_self_save_rule(ww: Any, capture: Any) -> None:
    r = ww.roles.Role
    kinds = ww.state.ActionKind
    game = _game(ww, [r.WEREWOLF, r.WITCH, r.VILLAGER], round_no=1)
    game.rules = ww.house_rules.HouseRules(witch_self_save_first_night=True)
    _queue(ww, game, kinds.SAVE, 2)
    saved, poison = await ww.engine._phase_witch(game, ww.config.Config(), 2)
    assert saved is True and poison is None


@pytest.mark.asyncio
async def test_witch_double_potion(ww: Any, capture: Any) -> None:
    r = ww.roles.Role
    kinds = ww.state.ActionKind
    game = _game(ww, [r.WEREWOLF, r.WITCH, r.VILLAGER], round_no=2)
    game.rules = ww.house_rules.HouseRules(witch_double_potion=True)
    _queue(ww, game, kinds.SAVE, 2)
    _queue(ww, game, kinds.POISON, 2, 1)
    saved, poison = await ww.engine._phase_witch(game, ww.config.Config(), 3)
    assert saved is True and poison == 1


@pytest.mark.asyncio
async def test_seer_sees_hidden_wolf_as_good(ww: Any, capture: Any) -> None:
    r = ww.roles.Role
    kinds = ww.state.ActionKind
    game = _game(ww, [r.HIDDEN_WOLF, r.SEER, r.VILLAGER], round_no=1)
    _queue(ww, game, kinds.CHECK, 2, 1)
    await ww.engine._phase_seer(game, ww.config.Config())
    assert game.player_by_seat(2).seer_checks == [(1, 1, "好人")]


@pytest.mark.asyncio
async def test_gravekeeper_learns_exiled_faction(ww: Any, capture: Any) -> None:
    r = ww.roles.Role
    game = _game(
        ww, [r.WEREWOLF, r.GRAVEKEEPER, r.VILLAGER], board="隐狼守墓", round_no=2
    )
    game.last_exiled_seat = 1
    await ww.engine._phase_gravekeeper(game, ww.config.Config())
    assert any(seat == 2 and "【狼人】" in text for seat, text in capture["dm"])


# ── 白天与胜负 ────────────────────────────────────────────


def test_city_win_mode(ww: Any) -> None:
    r = ww.roles.Role
    game = _game(ww, [r.WEREWOLF, r.SEER, r.VILLAGER])
    game.player_by_seat(2).alive = False
    assert ww.engine._check_winner(game) is ww.roles.Faction.WOLF
    game.rules = ww.house_rules.HouseRules(win_mode="city")
    assert ww.engine._check_winner(game) is None


def test_speech_order_starts_right_of_death_and_sheriff_last(ww: Any) -> None:
    r = ww.roles.Role
    game = _game(ww, [r.VILLAGER] * 6)
    dead = game.player_by_seat(3)
    dead.alive = False
    start, text = ww.engine._default_start(game, game.alive_players(), [dead])
    assert start.seat == 4 and "3号" in text
    order = ww.engine._clockwise_order(game.alive_players(), 4, clockwise=True)
    sheriff = game.player_by_seat(5)
    assert ww.engine._sheriff_last(order, sheriff)[-1] is sheriff


@pytest.mark.asyncio
async def test_white_wolf_king_takes_target(
    ww: Any, capture: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = ww.roles.Role
    game = _game(ww, [r.WHITE_WOLF_KING, r.SEER, r.VILLAGER, r.VILLAGER])

    async def no_effects(*_args: Any) -> None:
        return None

    monkeypatch.setattr(ww.engine, "_resolve_pending_day_effects", no_effects)
    await ww.engine._handle_detonation(
        game, ww.config.Config(), game.player_by_seat(1), 2
    )
    seer = game.player_by_seat(2)
    assert not seer.alive
    assert seer.death_cause is ww.roles.DeathCause.WHITE_WOLF_KING_BOOM
    assert "白狼王" in capture["announce"][-1]


@pytest.mark.asyncio
async def test_wolf_king_shot_uses_own_cause(ww: Any, capture: Any) -> None:
    r = ww.roles.Role
    game = _game(ww, [r.WOLF_KING, r.SEER, r.VILLAGER])
    await ww.engine._resolve_hunter_shot(game, ww.config.Config(), 2, 1)
    assert game.player_by_seat(2).death_cause is ww.roles.DeathCause.WOLF_KING_SHOT
    assert "狼王 1号" in capture["announce"][-1]


def test_wolf_king_cannot_shoot_after_poison_or_detonation(ww: Any) -> None:
    r = ww.roles.Role
    king = _player(ww, 1, r.WOLF_KING)
    king.death_cause = ww.roles.DeathCause.VOTED
    assert ww.engine._can_shoot(king)
    king.death_cause = ww.roles.DeathCause.SELF_DETONATION
    assert not ww.engine._can_shoot(king)
    king.death_cause = ww.roles.DeathCause.WITCH_POISON
    assert not ww.engine._can_shoot(king)


def test_hidden_role_reveal_rule(ww: Any) -> None:
    r = ww.roles.Role
    game = _game(ww, [r.WEREWOLF, r.SEER])
    assert "预言家" in ww.engine._role_reveal(game, game.player_by_seat(2))
    game.rules = ww.house_rules.HouseRules(reveal_on_death=False)
    assert "预言家" not in ww.engine._role_reveal(game, game.player_by_seat(2))


@pytest.mark.asyncio
async def test_change_vote_takes_last_ballot(
    ww: Any, capture: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = ww.roles.Role
    kinds = ww.state.ActionKind
    game = _game(ww, [r.WEREWOLF, r.VILLAGER, r.VILLAGER])
    game.rules = ww.house_rules.HouseRules(allow_change_vote=True, vote_seconds=20)
    monkeypatch.setattr(ww.engine, "_VOTE_WARN_SECONDS", 0)
    real_timer = ww.engine._timer
    monkeypatch.setattr(ww.engine, "_timer", lambda g, _s: real_timer(g, 0.3))
    _queue(ww, game, kinds.VOTE, 2, 1)
    _queue(ww, game, kinds.VOTE, 2, 3)
    votes = await ww.engine._collect_votes(
        game, ww.config.Config(), [1, 2, 3], ww.state.Phase.DAY_VOTE
    )
    assert votes[2] == 3
    assert any("改投" in text for text in capture["announce"])


@pytest.mark.asyncio
async def test_tie_mode_peace_skips_pk(
    ww: Any, capture: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = ww.roles.Role
    game = _game(ww, [r.WEREWOLF, r.VILLAGER, r.VILLAGER])
    game.rules = ww.house_rules.HouseRules(tie_mode="peace")

    async def tied_votes(*_args: Any, **_kwargs: Any) -> dict[int, Any]:
        return {1: 2, 2: 1}

    monkeypatch.setattr(ww.engine, "_collect_votes", tied_votes)
    assert await ww.engine._vote_phase(game, ww.config.Config()) is None
    assert any("平安日" in text for text in capture["announce"])


# ── 终局与跨局记忆 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_finish_builds_review_and_highlights(
    ww: Any, capture: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = ww.roles.Role
    game = _game(ww, [r.WEREWOLF, r.SEER, r.VILLAGER], round_no=2)
    seer = game.player_by_seat(2)
    seer.seer_checks = [(1, 1, "狼人"), (2, 1, "狼人")]
    game.note("预言家", "预言家 2号 查验 1号 → 狼人")
    wolf = game.player_by_seat(1)
    ww.engine._kill(game, wolf, ww.roles.DeathCause.VOTED)

    async def no_persist(*_args: Any) -> None:
        return None

    monkeypatch.setattr(ww.engine, "_persist_end", no_persist)
    await ww.engine._finish(game, ww.roles.Faction.GOOD)
    final = capture["announce"][-1]
    assert "本局高光" in final and "火眼金睛" in final
    review = ww.state.last_review(900)
    assert review is not None and "复盘" in review and "第 2 轮" in review
    humans, ai_count, _names = ww.state.last_roster(900)
    assert sorted(humans) == [1, 2, 3] and ai_count == 0


def test_phase_remaining_outside_loop(ww: Any) -> None:
    game = _game(ww, [ww.roles.Role.VILLAGER])
    assert ww.state.phase_remaining(game) is None


@pytest.mark.asyncio
async def test_phase_remaining_tracks_timer(ww: Any) -> None:
    game = _game(ww, [ww.roles.Role.VILLAGER])
    ww.engine._timer(game, 30)
    remaining = ww.state.phase_remaining(game)
    assert remaining is not None and 28 <= remaining <= 30
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_speech_rotation_tracks_afk_and_skip(ww: Any, capture: Any) -> None:
    r = ww.roles.Role
    kinds = ww.state.ActionKind
    game = _game(ww, [r.VILLAGER, r.VILLAGER, r.WEREWOLF])
    order = game.alive_players()
    # 轮换开始时会清空队列，/过 需在第一位发言窗口打开后到达
    asyncio.get_running_loop().call_later(0.05, _queue, ww, game, kinds.SKIP, 1)
    await ww.engine._speech_rotation(
        game, ww.config.Config(), order, ww.state.Phase.DAY_SPEECH, speech_timeout=1
    )
    assert game.player_by_seat(1).afk_strikes == 0
    assert game.player_by_seat(2).afk_strikes == 1
    assert any("下一位 2号" in text for text in capture["announce"])
    assert any("2号 超时未发言" in text for text in capture["announce"])
