"""RPG engine boundaries that require the loaded NoneBot plugin."""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from typing import Any

import nonebot
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def rpg_modules() -> tuple[Any, Any]:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()
    if nonebot.get_plugin("yawn_core") is None:
        nonebot.load_from_toml("pyproject.toml")
    state = importlib.import_module("src.plugins.yawn_core.yawn_rpg.state")
    engine = importlib.import_module("src.plugins.yawn_core.yawn_rpg.engine")
    return state, engine


@pytest.mark.asyncio
@pytest.mark.parametrize("speaker_id", [1, 2])
async def test_combat_say_never_enters_kp_or_npc_tool_chain(
    rpg_modules: tuple[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    speaker_id: int,
) -> None:
    state, engine = rpg_modules
    game = state.Game(group_id=1, host_user_id=1, combat_order=[1])
    player = state.PlayerState(user_id=speaker_id, seat=speaker_id)
    action = state.Action(state.ActionKind.SAY, speaker_id, aux="去下一个场景")
    called = False

    async def handle_say(*_args: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(engine, "_handle_say", handle_say)

    await engine._process_action(game, object(), action, player)

    assert not called


def _experience_module(_engine: Any) -> Any:
    schema = importlib.import_module("src.plugins.yawn_core.yawn_rpg.module_schema")
    npc = schema.NPC(
        id="caretaker",
        name="守门人",
        public_desc="看守旧门的老人。",
        persona="谨慎。",
        facts=[
            schema.NPCFact(
                id="key_fact",
                name="钥匙下落",
                text="钥匙藏在井边。",
            )
        ],
    )
    return schema.ModuleDef(
        id="experience_test",
        name="体验测试模组",
        min_players=2,
        max_players=2,
        opening="开场。",
        start_scene="room",
        scenes=[
            schema.Scene(
                id="room",
                name="客厅",
                narration="一间客厅。",
                npcs=["caretaker"],
            ),
            schema.Scene(id="hall", name="走廊", narration="一条走廊。"),
        ],
        npcs=[npc],
        clues=[
            schema.Clue(
                id="public_note",
                name="公开纸条",
                text="纸条写着集合地点。",
            ),
            schema.Clue(
                id="private_note",
                name="私密纸条",
                text="纸条背面写着暗号正文。",
            ),
        ],
    )


def _experience_player(engine: Any, user_id: int, seat: int, name: str) -> Any:
    sheet = engine.CharacterSheet(
        name=name,
        attributes={
            "str": 50,
            "con": 50,
            "siz": 50,
            "dex": 50 + seat * 5,
            "app": 50,
            "int": 50,
            "pow": 50,
            "edu": 50,
            "luck": 50,
        },
    )
    return engine.PlayerState(
        user_id=user_id,
        seat=seat,
        sheet=sheet,
        hp=10,
        san=50,
    )


def _experience_game(state: Any, engine: Any) -> Any:
    return state.Game(
        group_id=1,
        host_user_id=10,
        phase=state.Phase.PLAY,
        module=_experience_module(engine),
        current_scene="room",
        players=[
            _experience_player(engine, 10, 1, "阿明"),
            _experience_player(engine, 11, 2, "小周"),
        ],
        explore_round=1,
    )


def test_signup_preflight_explains_shortage_and_rejects_overfull_module(
    rpg_modules: tuple[Any, Any],
) -> None:
    state, engine = rpg_modules
    game = state.Game(group_id=1, host_user_id=10, module=_experience_module(engine))
    game.signup_user_ids[:] = [10]
    cfg = engine.Config(rpg_min_players=2)

    shortage = engine.signup_start_error(game, cfg)
    assert shortage is not None
    assert "还差 1 位" in shortage
    assert "报名" in shortage

    game.signup_user_ids[:] = [10, 11, 12]
    selected = engine.module_selection_error(game, game.module, cfg)
    assert selected is not None
    assert "最多允许 2 人" in selected
    assert game.module.max_players == 2  # noqa: PLR2004


def test_public_and_private_situation_keep_private_text_out_of_group(
    rpg_modules: tuple[Any, Any],
) -> None:
    state, engine = rpg_modules
    game = _experience_game(state, engine)
    game.discovered_clues.update({"public_note", "private_note"})
    game.public_clues.add("public_note")
    game.clue_owners["private_note"] = {10}
    game.npc_unlocked_facts[("caretaker", 10)] = {"key_fact"}
    player = game.player_by_user(10)
    assert player is not None

    public = engine.public_situation_text(game)
    private = engine.private_situation_text(game, player)
    journal = engine.private_journal_text(game, player)

    assert "公开纸条" in public
    assert "私密纸条" not in public
    assert "暗号正文" not in public
    assert "HP 10/10" in private
    assert "暗号正文" in private
    assert "钥匙藏在井边" in private
    assert "集合地点" in journal
    assert "暗号正文" in journal


def test_action_snapshot_ignores_waiting_seconds_but_tracks_round(
    rpg_modules: tuple[Any, Any],
) -> None:
    state, engine = rpg_modules
    game = _experience_game(state, engine)
    action = state.Action(
        state.ActionKind.CHECK,
        10,
        expected_phase=state.Phase.PLAY,
        expected_scene="room",
        expected_explore_round=1,
        submitted_at=0.0,
    )

    assert not engine._action_stale(game, engine.Config(), action)
    game.explore_round = 2
    assert engine._action_stale(game, engine.Config(), action)


@pytest.mark.asyncio
async def test_say_batch_classifies_concurrently_but_preserves_npc_boundary(
    rpg_modules: tuple[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, engine = rpg_modules
    game = _experience_game(state, engine)
    first = state.Action(state.ActionKind.SAY, 10, aux="先说")
    second = state.Action(state.ActionKind.SAY, 11, aux="再问守门人")
    game.action_queue.put_nowait(second)
    calls: list[str] = []

    async def classify(_game: Any, _cfg: Any, action: Any) -> None:
        calls.append(action.aux or "")
        await asyncio.sleep(0.001 if action is first else 0)
        action.route = "npc_talk" if action is second else "kp_say"
        action.target_id = "caretaker" if action is second else None

    monkeypatch.setattr(engine, "_classify_say", classify)
    batch = await engine._collect_say_batch(
        game,
        engine.Config(rpg_say_settle_window=0.01),
        first,
    )

    assert [item.aux for item in batch] == ["先说"]
    assert list(game.mid_turn_buffer) == [second]
    assert set(calls) == {"先说", "再问守门人"}


@pytest.mark.asyncio
async def test_ai_wait_notice_is_one_ephemeral_message(
    rpg_modules: tuple[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, engine = rpg_modules
    game = _experience_game(state, engine)
    messages: list[str] = []

    async def capture(_game: Any, text: str) -> None:
        messages.append(text)

    monkeypatch.setattr(engine, "_announce_ephemeral", capture)
    await engine._delayed_ai_wait_notice(
        game,
        engine.Config(rpg_ai_wait_notice_delay=0.001),
    )

    assert messages == ["KP 正在整理局面，请稍候……"]
    messages.clear()
    await engine._delayed_ai_wait_notice(
        game,
        engine.Config(rpg_ai_enabled=False, rpg_ai_wait_notice_delay=0),
    )
    assert messages == []


@pytest.mark.asyncio
async def test_invalid_move_does_not_report_as_executed(
    rpg_modules: tuple[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, engine = rpg_modules
    game = _experience_game(state, engine)
    player = game.player_by_user(10)
    assert player is not None

    async def quiet_announce(*_args: object) -> None:
        return None

    monkeypatch.setattr(engine, "_announce", quiet_announce)
    assert not await engine._do_move(game, player, "不存在的地方")
    assert not game.explore_acted


@pytest.mark.asyncio
async def test_scene_entry_starts_fresh_round_and_combat_announces_order(
    rpg_modules: tuple[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, engine = rpg_modules
    game = _experience_game(state, engine)
    game.explore_acted.add(10)
    messages: list[str] = []

    async def capture(_game: Any, text: str) -> None:
        messages.append(str(text))

    monkeypatch.setattr(engine, "_announce", capture)
    assert await engine.enter_scene(game, "hall")
    assert game.explore_round == 2  # noqa: PLR2004
    assert not game.explore_acted

    game.current_scene = "room"
    game.combat_order.clear()
    await engine._start_combat(game, engine.Config())
    assert "行动顺序" in messages[-1]
    assert messages[-1].index("小周") < messages[-1].index("阿明")


def test_ending_recap_has_public_statistics_only(
    rpg_modules: tuple[Any, Any],
) -> None:
    state, engine = rpg_modules
    game = _experience_game(state, engine)
    game.elapsed_minutes = 65
    game.public_clues.add("public_note")
    game.flags["hidden_flag"] = 3
    recap = engine.ending_recap_text(game)

    assert "体验测试模组" in recap
    assert "1 小时 5 分钟" in recap
    assert "公开线索：1 条" in recap
    assert "hidden_flag" not in recap
    assert "私密纸条" not in recap


# ── 回归：骰子 / 违规扫描 / 行动排序 / 过期 / 空闲收尾 / 战斗 / 私聊失败 ──


def test_fumble_rule_matches_coc7e(rpg_modules: tuple[Any, Any]) -> None:
    _state, engine = rpg_modules
    dice = importlib.import_module("src.plugins.yawn_core.yawn_rpg.dice")
    tier = dice.CheckTier
    # 技能 < 50：96-100 大失败
    assert dice.classify_roll(96, 40) is tier.FUMBLE
    assert dice.classify_roll(100, 40) is tier.FUMBLE
    assert dice.classify_roll(95, 40) is tier.FAILURE
    # 技能 ≥ 50：仅 100 大失败
    assert dice.classify_roll(96, 60) is tier.FAILURE
    assert dice.classify_roll(99, 99) is tier.REGULAR
    assert dice.classify_roll(100, 60) is tier.FUMBLE
    assert engine is not None


@pytest.mark.parametrize(
    "text",
    ["我去点火柴看看", "点火把照明", "我发烧了", "拿起汽油灯", "点燃这里的蜡烛"],
)
def test_benign_fire_words_are_not_arson(
    rpg_modules: tuple[Any, Any], text: str
) -> None:
    _state, engine = rpg_modules
    assert engine._scan_offense(text) != "arson"


@pytest.mark.parametrize(
    "text",
    ["我要放火", "纵火！", "把房子烧了", "烧掉这栋房子", "点燃宅子", "往门上泼汽油"],
)
def test_clear_arson_intent_is_detected(
    rpg_modules: tuple[Any, Any], text: str
) -> None:
    _state, engine = rpg_modules
    assert engine._scan_offense(text) == "arson"


def test_victim_statement_is_not_threat(rpg_modules: tuple[Any, Any]) -> None:
    _state, engine = rpg_modules
    assert engine._scan_offense("他被杀了") is None
    assert engine._scan_offense("我杀了你") == "threat"


@pytest.mark.asyncio
async def test_say_batch_never_overwrites_pending_action(
    rpg_modules: tuple[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, engine = rpg_modules
    game = _experience_game(state, engine)
    earlier = state.Action(state.ActionKind.CHECK, 10, aux="侦查")
    later = state.Action(state.ActionKind.CHECK, 11, aux="聆听")
    game.pending = earlier
    game.action_queue.put_nowait(later)

    async def classify(_game: Any, _cfg: Any, action: Any) -> None:
        action.route = "kp_say"

    monkeypatch.setattr(engine, "_classify_say", classify)
    await engine._collect_say_batch(
        game,
        engine.Config(rpg_say_settle_window=0.05),
        state.Action(state.ActionKind.SAY, 10, aux="说话"),
    )

    assert engine._pop_ready_action(game) is earlier
    assert engine._pop_ready_action(game) is later


def test_say_is_not_stale_after_round_or_scene_change(
    rpg_modules: tuple[Any, Any],
) -> None:
    state, engine = rpg_modules
    game = _experience_game(state, engine)
    say = state.Action(
        state.ActionKind.SAY,
        10,
        aux="大家小心",
        expected_phase=state.Phase.PLAY,
        expected_scene="room",
        expected_explore_round=1,
    )
    game.explore_round = 2
    game.current_scene = "hall"
    assert not engine._action_stale(game, engine.Config(), say)
    game.phase = state.Phase.ENDED
    assert engine._action_stale(game, engine.Config(), say)


@pytest.mark.asyncio
async def test_idle_timeout_ends_game_without_spinning(
    rpg_modules: tuple[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, engine = rpg_modules
    game = _experience_game(state, engine)
    messages: list[str] = []

    async def capture(_game: Any, text: Any) -> None:
        messages.append(str(text))

    async def noop(*_args: object) -> None:
        return None

    monkeypatch.setattr(engine, "_announce", capture)
    monkeypatch.setattr(engine, "_tutorial_step", noop)
    monkeypatch.setattr(engine, "_persist_end", noop)
    cfg = engine.Config(
        rpg_idle_timeout=0,
        rpg_idle_warn_remain=0,
        rpg_explore_round_timeout=3600,
    )
    await asyncio.wait_for(engine._run_play(game, cfg), timeout=5)

    assert game.phase is state.Phase.ENDED
    assert any("不了了之" in message for message in messages)


def _combat_game(state: Any, engine: Any) -> Any:
    schema = importlib.import_module("src.plugins.yawn_core.yawn_rpg.module_schema")
    game = _experience_game(state, engine)
    module = game.module
    module.monsters.append(
        schema.Monster(id="ghoul", name="食尸鬼", hp=99, attack_skill=50, damage="1d3")
    )
    room = module.scene("room")
    room.monsters.append("ghoul")
    room.exits.append(schema.Exit(to_scene="hall"))
    game.combat_order[:] = [11, 10]
    game.combat_index = 0
    game.combat_round = 1
    return game


@pytest.mark.asyncio
async def test_monsters_attack_after_all_players_act(
    rpg_modules: tuple[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, engine = rpg_modules
    game = _combat_game(state, engine)
    attacks: list[str] = []

    async def quiet(*_args: object) -> None:
        return None

    async def monster_attack(_game: Any, monster: Any, _target: Any) -> str:
        attacks.append(monster.id)
        return ""

    monkeypatch.setattr(engine, "_announce", quiet)
    monkeypatch.setattr(engine, "do_monster_attack", monster_attack)
    cfg = engine.Config()
    await engine._advance_combat(game, cfg)
    assert attacks == []
    assert game.combat_index == 1
    await engine._advance_combat(game, cfg)
    assert attacks == ["ghoul"]
    assert game.combat_index == 0
    assert game.combat_round == 2  # noqa: PLR2004


@pytest.mark.asyncio
@pytest.mark.parametrize("escaped", [True, False])
async def test_move_during_combat_is_flee_attempt(
    rpg_modules: tuple[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    escaped: bool,
) -> None:
    state, engine = rpg_modules
    game = _combat_game(state, engine)
    player = game.player_by_user(11)
    attacks: list[str] = []

    async def quiet(*_args: object) -> None:
        return None

    async def monster_attack(_game: Any, monster: Any, _target: Any) -> str:
        attacks.append(monster.id)
        return ""

    def fixed_check(skill_value: int, *_args: object, **_kwargs: object) -> Any:
        dice = importlib.import_module("src.plugins.yawn_core.yawn_rpg.dice")
        return dice.CheckResult(
            roll=1 if escaped else 99,
            skill_value=skill_value,
            difficulty=dice.CheckDifficulty.REGULAR,
            tier=dice.CheckTier.CRITICAL if escaped else dice.CheckTier.FAILURE,
        )

    monkeypatch.setattr(engine, "_announce", quiet)
    monkeypatch.setattr(engine, "do_monster_attack", monster_attack)
    monkeypatch.setattr(engine, "skill_check", fixed_check)
    action = state.Action(state.ActionKind.MOVE, 11, aux="走廊")
    assert await engine._process_action(game, engine.Config(), action, player)
    if escaped:
        assert game.current_scene == "hall"
        assert not game.combat_order
    else:
        assert game.current_scene == "room"
        assert game.combat_order
        assert game.combat_index == 1


@pytest.mark.asyncio
async def test_failed_npc_fact_dm_is_reported_with_real_bot(
    rpg_modules: tuple[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, engine = rpg_modules
    schema = importlib.import_module("src.plugins.yawn_core.yawn_rpg.module_schema")
    game = _experience_game(state, engine)
    game.bot = object()
    player = game.player_by_user(10)
    npc = game.module.npc("caretaker")
    node = schema.SocialNode.model_construct(id="ask", unlock_facts=["key_fact"])
    messages: list[str] = []

    async def capture(_game: Any, text: Any) -> None:
        messages.append(str(text))

    async def failed_dm(*_args: object) -> bool:
        return False

    monkeypatch.setattr(engine, "_announce", capture)
    monkeypatch.setattr(engine.api, "send_dm", failed_dm)
    await engine._deliver_social_rewards(game, player, npc, node, success=True)

    assert any("私聊发送失败" in message and "/线索" in message for message in messages)
    assert all("钥匙藏在井边" not in message for message in messages)
