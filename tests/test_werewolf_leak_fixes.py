"""狼人杀结算 / 信息泄漏 / 取消语义修复的聚焦回归测试。"""

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
IDLE_SECONDS = 0.05
SHOT_SEAT = 2
POSTPONED_ROUND = 2


@pytest.fixture(scope="module")
def ww() -> types.SimpleNamespace:
    """隔离导入狼人杀模块（不加载整个 yawn_core）。"""
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

    def load(name: str) -> Any:
        return importlib.import_module(f"yawn_core.yawn_werewolf.{name}")

    return types.SimpleNamespace(
        state=load("state"),
        engine=load("engine"),
        ai_player=load("ai_player"),
        roles=load("roles"),
        config=load("config"),
        commands=load("commands"),
    )


def _p(ww: Any, seat: int, role: Any, **kwargs: Any) -> Any:
    faction = ww.roles.ROLE_FACTION[role]
    return ww.state.PlayerState(seat, seat, role, faction, **kwargs)


async def _none(*_args: object, **_kwargs: object) -> None:
    return None


async def _true(*_args: object, **_kwargs: object) -> bool:
    return True


# ── 1. 刀 + 毒同一人：死因以毒杀为准 ─────────────────────


@pytest.mark.asyncio
async def test_knifed_and_poisoned_dies_once_as_poison(
    ww: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, roles = ww.engine, ww.roles
    hunter = _p(ww, 2, roles.Role.HUNTER)
    game = ww.state.Game(
        group_id=901,
        host_user_id=1,
        players=[_p(ww, 1, roles.Role.WEREWOLF), hunter],
    )

    async def wolves(*_args: object) -> int:
        return 2

    async def witch(*_args: object) -> tuple[bool, int]:
        return False, 2

    monkeypatch.setattr(engine, "_announce", _none)
    monkeypatch.setattr(engine, "_phase_halfblood", _none)
    monkeypatch.setattr(engine, "_phase_wolves", wolves)
    monkeypatch.setattr(engine, "_phase_witch", witch)
    monkeypatch.setattr(engine, "_phase_seer", _none)
    monkeypatch.setattr(engine, "_phase_elder", _none)

    deaths = await engine._run_night(game, ww.config.Config())

    assert deaths == [hunter]
    assert hunter.death_cause is roles.DeathCause.WITCH_POISON


# ── 7. 心跳收尾不得吞掉引擎任务自身的取消 ─────────────────


@pytest.mark.asyncio
async def test_run_night_propagates_own_cancellation_during_heartbeat_cleanup(
    ww: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = ww.engine
    game = ww.state.Game(group_id=902, host_user_id=1)
    cleanup_started = asyncio.Event()

    async def slow_heartbeat(*_args: object) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await asyncio.sleep(0.2)

    async def wolves(*_args: object) -> None:
        # 让出一次事件循环，确保心跳任务已开始运行
        await asyncio.sleep(0)

    async def witch(*_args: object) -> tuple[bool, None]:
        return False, None

    monkeypatch.setattr(engine, "_announce", _none)
    monkeypatch.setattr(engine, "_night_heartbeat", slow_heartbeat)
    monkeypatch.setattr(engine, "_phase_halfblood", _none)
    monkeypatch.setattr(engine, "_phase_wolves", wolves)
    monkeypatch.setattr(engine, "_phase_witch", witch)
    monkeypatch.setattr(engine, "_phase_seer", _none)
    monkeypatch.setattr(engine, "_phase_elder", _none)

    task = asyncio.create_task(engine._run_night(game, ww.config.Config()))
    await asyncio.wait_for(cleanup_started.wait(), 2)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# ── 2. 警上玩家不参与警长投票 ─────────────────────────────


class _ExpiredTimer:
    def __init__(self, _deadline: float) -> None:
        pass

    @staticmethod
    def remaining() -> float:
        return 0


@pytest.mark.asyncio
async def test_sheriff_vote_excludes_candidates_and_tied_players(
    ww: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, roles = ww.engine, ww.roles
    players = [_p(ww, seat, roles.Role.VILLAGER) for seat in range(1, 6)]
    for p in players[:3]:
        p.sheriff_candidate = True
    game = ww.state.Game(group_id=903, host_user_id=1, players=players)
    calls: list[tuple[Any, tuple[int, ...]]] = []

    async def votes(
        _game: Any,
        _cfg: Any,
        _targets: list[int],
        phase: Any,
        exclude_seats: tuple[int, ...] = (),
    ) -> dict[int, int]:
        calls.append((phase, exclude_seats))
        return {4: 1, 5: 2} if len(calls) == 1 else {4: 1}

    monkeypatch.setattr(engine, "_Timer", _ExpiredTimer)
    monkeypatch.setattr(engine, "_announce", _none)
    monkeypatch.setattr(engine, "_speech_rotation", _none)
    monkeypatch.setattr(engine, "_collect_votes", votes)

    await engine._sheriff_campaign(game, ww.config.Config())

    assert calls == [
        (ww.state.Phase.SHERIFF_VOTE, (1, 2, 3)),
        (ww.state.Phase.SHERIFF_REVOTE, (1, 2)),
    ]
    assert players[0].is_sheriff


@pytest.mark.asyncio
async def test_sheriff_vote_with_everyone_running_ends_without_stall(
    ww: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, roles = ww.engine, ww.roles
    players = [_p(ww, seat, roles.Role.VILLAGER) for seat in range(1, 4)]
    for p in players:
        p.sheriff_candidate = True
    game = ww.state.Game(group_id=904, host_user_id=1, players=players)
    announced: list[str] = []

    async def announce(_game: Any, text: object) -> None:
        announced.append(str(text))

    async def votes(*_args: object, **_kwargs: object) -> dict[int, int]:
        raise AssertionError("不应开启空投票窗口")

    monkeypatch.setattr(engine, "_Timer", _ExpiredTimer)
    monkeypatch.setattr(engine, "_announce", announce)
    monkeypatch.setattr(engine, "_speech_rotation", _none)
    monkeypatch.setattr(engine, "_collect_votes", votes)

    await engine._sheriff_campaign(game, ww.config.Config())

    assert not any(p.is_sheriff for p in players)
    assert "无人可以投票" in announced[-1]


def test_ai_does_not_vote_for_excluded_seats(
    ww: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ai_player, roles = ww.ai_player, ww.roles
    players = [_p(ww, seat, roles.Role.VILLAGER, is_ai=True) for seat in (1, 2, 3)]
    game = ww.state.Game(
        group_id=905,
        host_user_id=1,
        players=players,
        phase=ww.state.Phase.SHERIFF_VOTE,
        vote_targets=[1, 2],
        vote_exclude=(1, 2),
    )
    driver = ai_player.AIDriver(game)
    spawned: list[int] = []

    def spawn(_driver: Any, coro: Any) -> None:
        coro.close()
        spawned.append(len(spawned))

    monkeypatch.setattr(ai_player, "_spawn", spawn)
    asyncio.run(ai_player._process_phase(driver))

    assert len(spawned) == 1
    assert (0, ww.state.Phase.SHERIFF_VOTE, 3, "vote") in driver.handled


# ── 3. 夜间技能先查角色，回复与子阶段无关 ─────────────────


class _FinishedError(Exception):
    pass


class _DMEvent:
    def __init__(self, user_id: int, text: str = "") -> None:
        self.user_id = user_id
        self._text = text

    def get_user_id(self) -> str:
        return str(self.user_id)

    def get_plaintext(self) -> str:
        return self._text


@pytest.fixture
def registered_game(ww: Any) -> Any:
    state, roles = ww.state, ww.roles
    game = state.Game(
        group_id=906,
        host_user_id=1,
        players=[
            _p(ww, 1, roles.Role.VILLAGER),
            _p(ww, 2, roles.Role.SEER),
            _p(ww, 3, roles.Role.WEREWOLF),
            _p(ww, 4, roles.Role.WITCH),
        ],
    )
    state._games[game.group_id] = game
    for p in game.players:
        state._user_index[p.user_id] = game.group_id
    yield game
    state._games.pop(game.group_id, None)
    for p in game.players:
        state._user_index.pop(p.user_id, None)


def _capture_finish(monkeypatch: pytest.MonkeyPatch, matcher: Any) -> None:
    async def finish(message: object = None, **_kwargs: object) -> None:
        raise _FinishedError(message)

    monkeypatch.setattr(matcher, "finish", finish)


async def _reply(handler: Any, *args: Any, **kwargs: Any) -> object:
    try:
        await handler(*args, **kwargs)
    except _FinishedError as finished:
        return finished.args[0]
    return None


@pytest.mark.asyncio
async def test_night_slash_commands_give_uniform_reply_to_non_holders(
    ww: Any, registered_game: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands, phase = ww.commands, ww.state.Phase
    game = registered_game
    for matcher in (commands.check_cmd, commands.save_cmd, commands.kill_cmd):
        _capture_finish(monkeypatch, matcher)
    monkeypatch.setattr(commands, "_submit", lambda *_a, **_k: None)

    replies = set()
    for night_phase in (phase.NIGHT_WOLVES, phase.NIGHT_WITCH, phase.NIGHT_SEER):
        game.phase = night_phase
        replies.add(
            await _reply(commands.handle_check, _DMEvent(1), arg="3", _perm=None)
        )
        replies.add(await _reply(commands.handle_save, _DMEvent(3), _perm=None))
        replies.add(
            await _reply(commands.handle_kill, _DMEvent(4), arg="1", _perm=None)
        )
    assert replies == {commands._NO_SKILL}

    # 持有者：本角色子阶段受理，其它子阶段才是"还不是时候"
    game.phase = phase.NIGHT_SEER
    accepted = await _reply(commands.handle_check, _DMEvent(2), arg="3", _perm=None)
    game.phase = phase.NIGHT_WITCH
    rejected = await _reply(commands.handle_check, _DMEvent(2), arg="3", _perm=None)
    assert accepted is None
    assert "还不是时候" in str(rejected)


@pytest.mark.asyncio
async def test_night_free_text_gate_is_phase_independent_for_non_holders(
    ww: Any, registered_game: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands, phase = ww.commands, ww.state.Phase
    game = registered_game
    _capture_finish(monkeypatch, commands.private_listener)
    monkeypatch.setattr(commands, "_submit", lambda *_a, **_k: None)

    async def collect(user_id: int, text: str) -> list[object]:
        out: list[object] = []
        for night_phase in (
            phase.NIGHT_WOLVES,
            phase.NIGHT_WITCH,
            phase.NIGHT_SEER,
        ):
            game.phase = night_phase
            out.append(
                await _reply(commands.handle_in_game_dm, _DMEvent(user_id, text))
            )
        return out

    # 村民发 查验 / 过：三个子阶段回复完全一致
    assert set(await collect(1, "查验3")) == {commands._NO_SKILL}
    assert set(await collect(1, "过")) == {commands._NO_SKILL}
    # 狼人发 过：仅在狼人阶段受理，女巫/预言家阶段回复一致
    wolf = await collect(3, "过")
    assert wolf[0] is None
    assert wolf[1] == wolf[2]
    assert "还不是时候" in str(wolf[1])


# ── 4. 角色缺席时夜间子阶段仍拟真等待 ─────────────────────


@pytest.mark.asyncio
async def test_absent_night_roles_still_wait(
    ww: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, roles = ww.engine, ww.roles
    monkeypatch.setattr(engine, "_IDLE_PHASE_SECONDS", (IDLE_SECONDS, IDLE_SECONDS))
    monkeypatch.setattr(engine, "_dm", _true)
    cfg = ww.config.Config(ww_night_timeout=1)
    loop = asyncio.get_running_loop()

    dead_witch = _p(ww, 1, roles.Role.WITCH, alive=False)
    dry_witch = _p(ww, 1, roles.Role.WITCH, save_used=True, poison_used=True)
    dead_seer = _p(ww, 2, roles.Role.SEER, alive=False)
    for witch in (dead_witch, dry_witch):
        game = ww.state.Game(group_id=907, host_user_id=1, players=[witch])
        start = loop.time()
        assert await engine._phase_witch(game, cfg, 3) == (False, None)
        assert loop.time() - start >= IDLE_SECONDS * 0.9
    game = ww.state.Game(group_id=907, host_user_id=1, players=[dead_seer])
    start = loop.time()
    await engine._phase_seer(game, cfg)
    assert loop.time() - start >= IDLE_SECONDS * 0.9

    elder = _p(ww, 3, roles.Role.SILENT_ELDER, alive=False)
    game = ww.state.Game(
        group_id=907, host_user_id=1, players=[elder], board="禁言骑士"
    )
    start = loop.time()
    await engine._phase_elder(game, cfg)
    assert loop.time() - start >= IDLE_SECONDS * 0.9
    assert game.phase is ww.state.Phase.NIGHT_ELDER

    # 等待受阶段超时夹取（测试与短配置下不拖慢流程）
    monkeypatch.setattr(engine, "_IDLE_PHASE_SECONDS", (30.0, 30.0))
    game = ww.state.Game(group_id=907, host_user_id=1, players=[dead_seer])
    await asyncio.wait_for(
        engine._phase_seer(game, ww.config.Config(ww_night_timeout=0)), 1
    )


# ── 5. 夜间私聊失败不群内点名 ─────────────────────────────


@pytest.mark.asyncio
async def test_dm_failure_notice_only_in_public_context(
    ww: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, roles, phase = ww.engine, ww.roles, ww.state.Phase
    announced: list[str] = []

    async def announce(_game: Any, text: object) -> None:
        announced.append(str(text))

    async def fail(*_args: object) -> bool:
        return False

    monkeypatch.setattr(engine, "_announce", announce)
    monkeypatch.setattr(engine.api, "send_dm", fail)

    witch = _p(ww, 1, roles.Role.WITCH)
    game = ww.state.Game(
        group_id=908, host_user_id=1, players=[witch], phase=phase.NIGHT_WITCH
    )
    game.bot = object()
    assert not await engine._dm(game, witch, "女巫请睁眼")
    assert announced == []
    assert not witch.dm_ok

    villager = _p(ww, 2, roles.Role.VILLAGER)
    game.players.append(villager)
    game.phase = phase.DEALING
    assert not await engine._dm(game, villager, "身份卡")
    assert len(announced) == 1
    assert "夜间" not in announced[0]


# ── 6. 猎人开枪前不公开身份，等待对所有夜间死者一致 ───────


@pytest.mark.asyncio
async def test_hunter_prompt_does_not_reveal_hunter_until_shot(
    ww: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, roles, state = ww.engine, ww.roles, ww.state
    announced: list[str] = []
    dms: list[str] = []

    async def announce(_game: Any, text: object) -> None:
        announced.append(str(text))

    async def dm(_game: Any, _player: Any, text: str) -> bool:
        dms.append(text)
        return True

    monkeypatch.setattr(engine, "_announce", announce)
    monkeypatch.setattr(engine, "_dm", dm)
    hunter = _p(ww, 1, roles.Role.HUNTER, alive=False)
    target = _p(ww, 2, roles.Role.VILLAGER)
    game = state.Game(group_id=909, host_user_id=1, players=[hunter, target])
    game.action_queue.put_nowait(state.Action(state.ActionKind.SHOOT, 1, 9))
    game.action_queue.put_nowait(state.Action(state.ActionKind.SHOOT, 1, 2))

    shot = await engine._hunter_prompt(
        game, ww.config.Config(ww_hunter_timeout=1), hunter
    )

    assert shot == SHOT_SEAT
    assert announced == [engine._death_skill_wait_text(hunter)]
    assert all("猎人" not in text for text in announced)
    assert any("无效" in text for text in dms)


@pytest.mark.asyncio
async def test_hidden_role_deaths_all_get_the_same_skill_wait(
    ww: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, roles = ww.engine, ww.roles
    prompted: list[int] = []
    idled: list[int] = []

    async def prompt(_game: Any, _cfg: Any, hunter: Any) -> None:
        prompted.append(hunter.seat)

    async def idle(_game: Any, _cfg: Any, dead: Any) -> None:
        idled.append(dead.seat)

    monkeypatch.setattr(engine, "_hunter_prompt", prompt)
    monkeypatch.setattr(engine, "_death_skill_idle", idle)
    knifed_hunter = _p(ww, 1, roles.Role.HUNTER, alive=False)
    knifed_hunter.death_cause = roles.DeathCause.WOLF_KILL
    poisoned_hunter = _p(ww, 2, roles.Role.HUNTER, alive=False)
    poisoned_hunter.death_cause = roles.DeathCause.WITCH_POISON
    knifed_villager = _p(ww, 3, roles.Role.VILLAGER, alive=False)
    knifed_villager.death_cause = roles.DeathCause.WOLF_KILL
    voted_villager = _p(ww, 4, roles.Role.VILLAGER, alive=False)
    voted_villager.death_cause = roles.DeathCause.VOTED
    dead = [knifed_hunter, poisoned_hunter, knifed_villager, voted_villager]
    game = ww.state.Game(group_id=910, host_user_id=1, players=list(dead))

    await engine._resolve_pending_day_effects(game, ww.config.Config(), list(dead))

    assert prompted == [1]
    assert idled == [2, 3]


# ── 8. 首日竞选被自爆打断顺延一天 ─────────────────────────


@pytest.mark.asyncio
async def test_sheriff_campaign_postponed_once_after_detonation(
    ww: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, roles = ww.engine, ww.roles
    monkeypatch.setattr(engine, "_announce", _none)
    candidate = _p(ww, 1, roles.Role.VILLAGER, sheriff_candidate=True)
    game = ww.state.Game(group_id=911, host_user_id=1, players=[candidate])
    game.round_no = 1

    await engine._postpone_sheriff_campaign(game)
    assert game.sheriff_campaign_round == POSTPONED_ROUND
    assert not candidate.sheriff_candidate

    game.round_no = 2
    await engine._postpone_sheriff_campaign(game)
    assert game.sheriff_campaign_round is None


@pytest.mark.asyncio
async def test_run_day_holds_postponed_campaign(
    ww: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, roles = ww.engine, ww.roles
    campaigns: list[int] = []

    async def campaign(game: Any, _cfg: Any) -> None:
        campaigns.append(game.round_no)

    async def order(game: Any, _cfg: Any, *_args: object) -> list[Any]:
        return game.alive_players()

    async def no_exile(*_args: object) -> None:
        return None

    monkeypatch.setattr(engine, "_announce", _none)
    monkeypatch.setattr(engine, "_sheriff_campaign", campaign)
    monkeypatch.setattr(engine, "_decide_speech_order", order)
    monkeypatch.setattr(engine, "_speech_rotation", _none)
    monkeypatch.setattr(engine, "_vote_phase", no_exile)
    game = ww.state.Game(
        group_id=912,
        host_user_id=1,
        players=[
            _p(ww, 1, roles.Role.WEREWOLF),
            _p(ww, 2, roles.Role.SEER),
            _p(ww, 3, roles.Role.VILLAGER),
        ],
        sheriff_campaign_round=2,
    )
    game.round_no = 2

    assert await engine._run_day(game, ww.config.Config(), []) is None
    assert campaigns == [2]
    assert game.sheriff_campaign_round is None


# ── 9. 决斗打断后 AI 发言仍收尾 ───────────────────────────


@pytest.mark.asyncio
async def test_ai_speech_finishes_after_mid_window_token_bump(
    ww: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ai_player, state = ww.ai_player, ww.state
    player = state.PlayerState(1, 1, None, None, is_ai=True)
    game = state.Game(
        group_id=913,
        host_user_id=1,
        phase=state.Phase.DAY_SPEECH,
        phase_token=5,
        players=[player],
        current_speaker=1,
        bot=object(),
    )
    driver = ai_player.AIDriver(game)

    async def speech(*_args: object) -> str:
        # 决斗失败 → 警徽移交 → 回到本阶段：token 前进，发言权未变
        game.phase_token += 2
        return "发言"

    monkeypatch.setattr(ai_player, "_llm_speech", speech)
    monkeypatch.setattr(ai_player.api, "safe_group_msg", _true)
    monkeypatch.setattr(ai_player, "_SPEECH_LINGER", 0)
    token = ai_player._AI_PHASE_TOKEN.set(5)
    try:
        await ai_player._do_speech(driver, player)
    finally:
        ai_player._AI_PHASE_TOKEN.reset(token)

    action = game.action_queue.get_nowait()
    assert action.kind is state.ActionKind.SKIP
    assert action.phase_token == game.phase_token


@pytest.mark.asyncio
async def test_ai_speech_rehandled_after_window_temporarily_left(
    ww: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ai_player, state = ww.ai_player, ww.state
    player = state.PlayerState(1, 1, None, None, is_ai=True)
    game = state.Game(
        group_id=914,
        host_user_id=1,
        phase=state.Phase.DAY_SPEECH,
        players=[player],
        current_speaker=1,
        bot=object(),
    )
    driver = ai_player.AIDriver(game)

    async def speech(*_args: object) -> str:
        game.phase = state.Phase.BADGE_TRANSFER
        return "发言"

    monkeypatch.setattr(ai_player, "_llm_speech", speech)
    await ai_player._do_speech(driver, player)

    assert game.action_queue.empty()
    assert (0, state.Phase.DAY_SPEECH, 1, "speech") not in driver.handled
