# ruff: noqa: E501,F401,I001,TID252,PLR0911,PLR0912,PLR0913,PLR0915,PLR0917,PLR2004,C901,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100,PLW0603,FAST002
"""狼人杀 / 跑团子插件的对局管理端点。

实时对局读取子插件进程内注册表（一群一局、单进程部署前提下的
权威状态），只读快照、不改局内数据；强制结束复用子插件自身的
``stop_game()`` 状态机入口（与群内 /结束游戏 同一路径），由后台
任务执行并立即返回。战绩查询读取各子插件的赛后总结表。子插件
未加载时所有端点优雅降级，不影响 WebUI 其余功能。
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from nonebot import get_plugin_config, logger
from nonebot_plugin_orm import get_session
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import String, func, or_, select

from ..data_models.bot_group import BotGroup
from .config import API_PATH
from .deps import AdminReadSession, AdminWriteSession, ok, page_params
from .hub import hub
from .service import iso, page_meta
from ..replay import load_replay  # noqa: TID252

router = APIRouter(prefix=API_PATH)

_ww_state_module: Any = None
_ww_state_resolved = False
_ww_game_log_module: Any = None
_ww_game_log_resolved = False
_rpg_state_module: Any = None
_rpg_state_resolved = False
_rpg_engine_module: Any = None
_rpg_engine_resolved = False
_rpg_config_instance: Any = None
_rpg_config_resolved = False


def _werewolf_game_log() -> Any | None:
    """延迟解析狼人杀可视化事件日志模块；子插件缺失时返回 None。

    仅在解析成功后缓存；失败不落缓存，子插件随后加载成功时下次
    调用即可自动恢复（与前端提示语义一致）。
    """
    global _ww_game_log_module, _ww_game_log_resolved
    if not _ww_game_log_resolved:
        try:
            from ..yawn_werewolf import game_log as module  # pyright: ignore[reportMissingImports]
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"狼人杀子插件不可用，事件日志降级：{exc}")
            return None
        _ww_game_log_module = module
        _ww_game_log_resolved = True
    return _ww_game_log_module


def _werewolf_state() -> Any | None:
    """延迟解析狼人杀状态模块；子插件缺失或损坏时返回 None。

    仅在解析成功后缓存，失败下次重试（同 ``_werewolf_game_log``）。
    """
    global _ww_state_module, _ww_state_resolved
    if not _ww_state_resolved:
        try:
            from ..yawn_werewolf import state as module  # pyright: ignore[reportMissingImports]
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"狼人杀子插件不可用，对局监控降级：{exc}")
            return None
        _ww_state_module = module
        _ww_state_resolved = True
    return _ww_state_module


def _rpg_state() -> Any | None:
    """延迟解析跑团状态模块；子插件缺失或损坏时返回 None。

    仅在解析成功后缓存，失败下次重试（同 ``_werewolf_state``）。
    """
    global _rpg_state_module, _rpg_state_resolved
    if not _rpg_state_resolved:
        try:
            from ..yawn_rpg import state as module  # pyright: ignore[reportMissingImports]
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"跑团子插件不可用，对局监控降级：{exc}")
            return None
        _rpg_state_module = module
        _rpg_state_resolved = True
    return _rpg_state_module


def _rpg_engine() -> Any | None:
    """延迟解析跑团引擎投影/行动模块，失败时保持 WebUI 可用。"""
    global _rpg_engine_module, _rpg_engine_resolved
    if not _rpg_engine_resolved:
        try:
            from ..yawn_rpg import engine as module  # pyright: ignore[reportMissingImports]
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"跑团引擎不可用，对局详情降级：{exc}")
            return None
        _rpg_engine_module = module
        _rpg_engine_resolved = True
    return _rpg_engine_module


def _rpg_config() -> Any | None:
    """延迟读取跑团配置；子插件不可用时返回 None。"""
    global _rpg_config_instance, _rpg_config_resolved
    if not _rpg_config_resolved:
        try:
            from ..yawn_rpg.config import Config  # pyright: ignore[reportMissingImports]

            _rpg_config_instance = get_plugin_config(Config)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"跑团子插件不可用，行动配置降级：{exc}")
            return None
        _rpg_config_resolved = True
    return _rpg_config_instance


class RpgActionSubmit(BaseModel):
    """管理台投递到 RPG 单写引擎的最小行动载荷。"""

    model_config = ConfigDict(populate_by_name=True)

    user_id: int = Field(alias="userId", gt=0)
    kind: str = Field(min_length=1, max_length=32)
    text: str | None = Field(default=None, max_length=2_000)
    minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    # 允许测试与未来客户端携带幂等 id；未提供时由 Action 自行生成。
    action_id: str | None = Field(default=None, alias="actionId", max_length=64)


# 与 yawn_werewolf/commands.py 的 _PHASE_CN 保持一致；不直接 import
# commands 是为了避免其 nonebot matcher 注册副作用。
_WW_PHASE_LABELS: dict[str, str] = {
    "SIGNUP": "报名中",
    "DEALING": "发牌中",
    "NIGHT_HALFBLOOD": "夜晚",
    "NIGHT_WOLVES": "夜晚",
    "NIGHT_WITCH": "夜晚",
    "NIGHT_SEER": "夜晚",
    "NIGHT_ELDER": "夜晚",
    "DAY_ANNOUNCE": "天亮结算",
    "LAST_WORDS": "遗言环节",
    "HUNTER_SHOT": "猎人开枪决策",
    "BADGE_TRANSFER": "警徽移交",
    "SHERIFF_REGISTER": "警长竞选报名",
    "SHERIFF_SPEECH": "竞选发言",
    "SHERIFF_VOTE": "警长投票",
    "SHERIFF_FINAL_SPEECH": "警长平票终辩",
    "SHERIFF_REVOTE": "警长平票重投",
    "DAY_SPEECH": "白天发言",
    "DAY_VOTE": "放逐投票",
    "PK_SPEECH": "PK 发言",
    "PK_VOTE": "PK 投票",
    "ENDED": "已结束",
}

_RPG_PHASE_LABELS: dict[str, str] = {
    "SIGNUP": "报名选本",
    "CHAR_CREATE": "建卡中",
    "PLAY": "进行中",
    "ENDED": "已结束",
}

# 发后即忘的后台任务登记：无引用的任务可能在完成前被事件循环
# 的弱引用回收，登记持有强引用直至完成（与 commands.py 同款）。
_bg_tasks: set[asyncio.Task[None]] = set()


def _spawn_background(coro: Any) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def _enum_value(value: Any) -> str | None:
    """安全取枚举值；非枚举时退化为字符串。"""
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _worker_alive(game: Any) -> bool:
    task = game.worker
    return task is not None and not task.done()


# ── 实时对局快照（只读内存） ───────────────────────────────


def _ww_live_game(state: Any, game: Any) -> dict[str, Any]:
    """狼人杀单局快照：完整信息（含身份），前端负责默认遮蔽。"""
    players = [
        {
            "seat": player.seat,
            "userId": player.user_id,
            "name": state.display_name_of(game, player.user_id),
            "isAi": player.is_ai,
            "alive": player.alive,
            "isSheriff": player.is_sheriff,
            "role": _enum_value(player.role),
            "faction": _enum_value(player.faction),
            "deathRound": player.death_round,
            "deathCause": _enum_value(player.death_cause),
        }
        for player in sorted(game.players, key=lambda p: p.seat)
    ]
    signup = [
        {
            "userId": user_id,
            "name": state.display_name_of(game, user_id),
            "isAi": user_id < 0,
        }
        for user_id in game.signup_user_ids
    ]
    return {
        "groupId": game.group_id,
        "hostUserId": game.host_user_id,
        "board": game.board,
        "phase": _enum_value(game.phase),
        "phaseLabel": _WW_PHASE_LABELS.get(_enum_value(game.phase) or "", ""),
        "roundNo": game.round_no,
        "currentSpeaker": game.current_speaker,
        "signupCount": len(game.signup_user_ids),
        "playerCount": len(game.players),
        "aiCount": sum(1 for player in game.players if player.is_ai),
        "aliveCount": sum(1 for player in game.players if player.alive),
        "queueDepth": game.action_queue.qsize(),
        "pendingCount": len(game.pending_actions),
        "workerAlive": _worker_alive(game),
        "players": players,
        "signup": signup,
    }


def _rpg_live_game(state: Any, game: Any) -> dict[str, Any]:
    """跑团单局快照：只暴露 /局面 的公共信息口径，不含 HP/SAN
    与个人线索等私密内容。"""
    module = game.module
    current_actor = None
    if game.combat_order and game.combat_index < len(game.combat_order):
        current_actor = game.combat_order[game.combat_index]
    players = [
        {
            "seat": player.seat,
            "userId": player.user_id,
            "charName": getattr(player.sheet, "name", None)
            if player.sheet is not None
            else None,
            "confirmed": player.confirmed,
            "incapped": player.incapped,
        }
        for player in sorted(game.players, key=lambda p: p.seat)
    ]
    return {
        "groupId": game.group_id,
        "hostUserId": game.host_user_id,
        "moduleId": getattr(module, "id", None) if module is not None else None,
        "moduleName": getattr(module, "name", None) if module is not None else None,
        "phase": _enum_value(game.phase),
        "phaseLabel": _RPG_PHASE_LABELS.get(_enum_value(game.phase) or "", ""),
        "sceneId": game.current_scene,
        "clockText": game.clock_text(),
        "exploreRound": game.explore_round,
        "combatRound": game.combat_round if game.combat_order else None,
        "currentActorUserId": current_actor,
        "signupCount": len(game.signup_user_ids),
        "playerCount": len(game.players),
        "queueDepth": game.action_queue.qsize() + len(game.mid_turn_buffer),
        "pendingCount": len(game.pending_actions),
        "toolsBroken": game.tools_broken,
        "workerAlive": _worker_alive(game),
        "players": players,
    }


def _rpg_action_snapshot(  # noqa: PLR0913
    kind: Any,
    user_id: int,
    *,
    game: Any,
    value: int | None = None,
    aux: str | None = None,
    authority: str = "player",
    action_id: str | None = None,
) -> Any:
    """构造与 ``yawn_rpg.commands._action`` 等价的逻辑快照。

    WebUI 不能 import commands（会注册 NoneBot matcher），因此这里保留一份
    模块级适配器。两处若新增阶段/轮次快照字段，必须同步维护。
    """
    state = _rpg_state()
    if state is None:
        return None
    action_kind = kind
    if isinstance(kind, str):
        action_kind = state.ActionKind(kind.lower())
    phase = state.Phase
    scene = game.current_scene if game.phase is phase.PLAY else None
    combat_active = bool(game.combat_order)
    combat_actor = (
        game.combat_order[game.combat_index]
        if combat_active and 0 <= game.combat_index < len(game.combat_order)
        else None
    )
    action = state.Action(
        action_kind,
        user_id,
        value=value,
        aux=aux,
        expected_phase=game.phase,
        expected_scene=scene,
        expected_explore_round=game.explore_round if game.phase is phase.PLAY else None,
        expected_combat_round=game.combat_round if combat_active else None,
        expected_combat_actor=combat_actor,
        authority=authority,
    )
    if action_id:
        action.action_id = action_id
    return action


def _rpg_pending_deduction(game: Any) -> dict[str, Any] | None:
    pending = game.pending_deduction
    if pending is None:
        return None
    return {
        "proposerUserId": pending.proposer_user_id,
        "clueIds": list(pending.clue_ids),
        "conclusion": pending.conclusion,
        "confirmations": sorted(pending.confirmations),
    }


def _rpg_detail(state: Any, engine: Any, game: Any) -> dict[str, Any]:
    """管理员完整视角；公共/私密文本仍明确分栏，避免前端误播。"""
    players = [
        {
            "seat": player.seat,
            "userId": player.user_id,
            "charName": getattr(player.sheet, "name", None)
            if player.sheet is not None
            else None,
            "confirmed": player.confirmed,
            "incapped": player.incapped,
            "hp": player.hp,
            "san": player.san,
            "rerollsLeft": player.rerolls_left,
            "dmOk": player.dm_ok,
        }
        for player in sorted(game.players, key=lambda item: item.seat)
    ]
    return {
        "game": _rpg_live_game(state, game),
        "players": players,
        "situationText": engine.public_situation_text(game),
        "clueBoardText": engine.clue_board_text(game),
        "groupLog": list(game.group_log)[-200:],
        "signupUserIds": list(game.signup_user_ids),
        "pendingDeduction": _rpg_pending_deduction(game),
        "completedDeductions": sorted(game.completed_deductions),
    }


async def _group_names(group_ids: set[int]) -> dict[int, str | None]:
    """批量取群名；群不在 presence 表中时映射为 None。"""
    if not group_ids:
        return {}
    async with get_session() as db:
        rows = (
            await db.execute(
                select(BotGroup.group_id, BotGroup.group_name).where(
                    BotGroup.group_id.in_(group_ids)
                )
            )
        ).all()
    # 字典推导式而非 dict(rows)：pyright 无法从 Row 序列推断 dict 重载。
    return {group_id: name for group_id, name in rows}  # noqa: C416


@router.get("/games/live")
async def get_live_games(_session: AdminReadSession) -> dict[str, Any]:
    ww = _werewolf_state()
    rpg = _rpg_state()
    ww_games = (
        [_ww_live_game(ww, game) for game in ww.all_games()] if ww is not None else []
    )
    rpg_games = (
        [_rpg_live_game(rpg, game) for game in rpg.all_games()]
        if rpg is not None
        else []
    )
    names = await _group_names(
        {game["groupId"] for game in ww_games} | {game["groupId"] for game in rpg_games}
    )
    for game in (*ww_games, *rpg_games):
        game["groupName"] = names.get(game["groupId"])
    return ok(
        {
            "werewolf": {"available": ww is not None, "games": ww_games},
            "rpg": {"available": rpg is not None, "games": rpg_games},
        }
    )


def _require_live_game(state: Any, kind: str, group_id: int) -> Any:
    if state is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"{kind}子插件未加载")
    game = state.get_game(group_id)
    if game is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"该群当前没有进行中的{kind}对局"
        )
    return game


@router.post("/games/werewolf/{group_id}/stop")
async def stop_werewolf_game(group_id: int, _session: AdminWriteSession) -> dict[str, Any]:
    state = _werewolf_state()
    if state is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "狼人杀子插件未加载")
    game = _require_live_game(state, "狼人杀", group_id)
    _spawn_background(state.stop_game(game))
    await hub.notify_change("werewolf_game", str(group_id))
    return ok({"stopping": True})


@router.get("/games/werewolf/{group_id}/events")
async def get_werewolf_game_events(
    group_id: int,
    _session: AdminReadSession,
    after_seq: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """可视化对局详情：实时快照 + 内存事件日志（支持 afterSeq 增量）。"""
    state = _werewolf_state()
    if state is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "狼人杀子插件未加载")
    game = _require_live_game(state, "狼人杀", group_id)
    game_log = _werewolf_game_log()
    events = (
        [event.as_dict() for event in game_log.events(group_id, after_seq)]
        if game_log is not None
        else []
    )
    snapshot = _ww_live_game(state, game)
    return ok({"game": snapshot, "events": events})


@router.post("/games/rpg/{group_id}/stop")
async def stop_rpg_game(group_id: int, _session: AdminWriteSession) -> dict[str, Any]:
    state = _rpg_state()
    if state is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "跑团子插件未加载")
    game = _require_live_game(state, "跑团", group_id)
    _spawn_background(state.stop_game(game))
    await hub.notify_change("rpg_game", str(group_id))
    return ok({"stopping": True})


@router.get("/games/rpg/{group_id}/detail")
async def get_rpg_detail(group_id: int, _session: AdminReadSession) -> dict[str, Any]:
    state = _rpg_state()
    engine = _rpg_engine()
    if state is None or engine is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "跑团子插件未加载")
    game = _require_live_game(state, "跑团", group_id)
    return ok(_rpg_detail(state, engine, game))


@router.get("/games/rpg/{group_id}/players/{user_id}/private")
async def get_rpg_player_private(
    group_id: int,
    user_id: int,
    _session: AdminReadSession,
) -> dict[str, Any]:
    state = _rpg_state()
    engine = _rpg_engine()
    if state is None or engine is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "跑团子插件未加载")
    game = _require_live_game(state, "跑团", group_id)
    player = game.player_by_user(user_id)
    if player is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "该用户不在本局中")
    return ok(
        {
            "userId": user_id,
            "situationText": engine.private_situation_text(game, player),
            "journalText": engine.private_journal_text(game, player),
        }
    )


@router.post("/games/rpg/{group_id}/actions")
async def submit_rpg_action(
    group_id: int,
    body: RpgActionSubmit,
    _session: AdminWriteSession,
) -> dict[str, Any]:
    state = _rpg_state()
    config = _rpg_config()
    if state is None or config is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "跑团子插件未加载")
    game = _require_live_game(state, "跑团", group_id)
    kind_name = body.kind.strip().upper()
    phase = _enum_value(game.phase)
    play_kinds = {"SAY", "WAIT", "PASS_TURN"}
    signup_kinds = {"MODULE_SELECT", "START_GAME"}
    if phase == "PLAY":
        if kind_name not in play_kinds:
            raise HTTPException(status.HTTP_409_CONFLICT, "进行中阶段不支持该行动")
        actor_user_id = body.user_id
        player = game.player_by_user(actor_user_id)
        if player is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "该用户不在本局中")
        if player.incapped:
            raise HTTPException(status.HTTP_409_CONFLICT, "该玩家当前无法行动")
        value = None
        aux = None
        if kind_name == "SAY":
            aux = (body.text or "").strip()
            if not aux:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "SAY 行动需要文本")
        elif kind_name == "WAIT":
            value = max(
                1,
                min(
                    body.minutes if body.minutes is not None else config.rpg_wait_default,
                    config.rpg_wait_max,
                ),
            )
        action = _rpg_action_snapshot(
            kind_name,
            actor_user_id,
            game=game,
            value=value,
            aux=aux,
            action_id=body.action_id,
        )
    elif phase == "SIGNUP":
        if kind_name not in signup_kinds:
            raise HTTPException(status.HTTP_409_CONFLICT, "报名阶段不支持该行动")
        actor_user_id = game.host_user_id
        aux = (body.text or "").strip() if kind_name == "MODULE_SELECT" else None
        if kind_name == "MODULE_SELECT" and not aux:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "选择模组需要模组编号或 id")
        action = _rpg_action_snapshot(
            kind_name,
            actor_user_id,
            game=game,
            aux=aux,
            authority="admin",
            action_id=body.action_id,
        )
    else:
        raise HTTPException(status.HTTP_409_CONFLICT, "当前阶段不支持管理台行动")

    if action is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "跑团子插件未加载")
    result = state.submit_action(
        game,
        action,
        queue_max=config.rpg_action_queue_max,
        user_pending_max=config.rpg_user_pending_max,
        user_say_pending_max=config.rpg_user_say_pending_max,
    )
    result_value = str(getattr(result, "value", result))
    if result_value == "accepted":
        await hub.notify_change("rpg_game", str(group_id))
        return ok({"accepted": True, "actionId": action.action_id})
    if result_value == "stale":
        raise HTTPException(status.HTTP_409_CONFLICT, "局面已经变化，请重新操作~")
    if result_value == "duplicate":
        raise HTTPException(status.HTTP_409_CONFLICT, "这条行动已经提交过了~")
    if result_value in {"queue_full", "user_limit"}:
        message = (
            "当前行动过多，请稍后再试~"
            if result_value == "queue_full"
            else "你的待处理行动过多，请等待系统结算~"
        )
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, message)
    raise HTTPException(status.HTTP_409_CONFLICT, "行动未被接受")


# ── 战绩查询（赛后总结表） ─────────────────────────────────


def _status_condition(model: Any, status_filter: str) -> Any | None:
    if status_filter == "running":
        return model.ended_at.is_(None)
    if status_filter == "finished":
        return model.ended_at.is_not(None)
    return None


async def _paged_history(
    model: Any,
    *,
    page: int,
    page_size: int,
    search: str,
    status_filter: str,
) -> tuple[list[Any], int]:
    """两种战绩表结构一致，共用分页 + 筛选查询。"""
    conditions = []
    if search:
        pattern = f"%{search}%"
        conditions.append(
            or_(
                model.group_id.cast(String).like(pattern),
                model.host_user_id.cast(String).like(pattern),
            )
        )
    status_condition = _status_condition(model, status_filter)
    if status_condition is not None:
        conditions.append(status_condition)
    count_stmt = select(func.count()).select_from(model)
    stmt = select(model).order_by(model.started_at.desc(), model.id.desc())
    if conditions:
        count_stmt = count_stmt.where(*conditions)
        stmt = stmt.where(*conditions)
    async with get_session() as db:
        total = int(await db.scalar(count_stmt) or 0)
        rows = list(
            (await db.execute(stmt.offset((page - 1) * page_size).limit(page_size)))
            .scalars()
            .all()
        )
    return rows, total


@router.get("/games/werewolf/history")
async def werewolf_history(
    _session: AdminReadSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=100),
    search: str = Query(default="", max_length=120),
    status_filter: Literal["all", "running", "finished"] = Query(
        default="all", alias="status"
    ),
) -> dict[str, Any]:
    if _werewolf_state() is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "狼人杀子插件未加载")
    from ..yawn_werewolf.models import (  # pyright: ignore[reportMissingImports]
        WerewolfGame,
        WerewolfPlayer,
    )

    page, page_size = page_params(page, page_size)
    rows, total = await _paged_history(
        WerewolfGame,
        page=page,
        page_size=page_size,
        search=search.strip(),
        status_filter=status_filter,
    )
    game_ids = [row.id for row in rows]
    players: dict[int, list[Any]] = {}
    if game_ids:
        async with get_session() as db:
            player_rows = (
                (
                    await db.execute(
                        select(WerewolfPlayer)
                        .where(WerewolfPlayer.game_id.in_(game_ids))
                        .order_by(WerewolfPlayer.game_id, WerewolfPlayer.seat)
                    )
                )
                .scalars()
                .all()
            )
        for player in player_rows:
            players.setdefault(player.game_id, []).append(player)
    names = await _group_names({row.group_id for row in rows})
    data = [
        {
            "id": row.id,
            "groupId": row.group_id,
            "groupName": names.get(row.group_id),
            "hostUserId": row.host_user_id,
            "board": row.board,
            "playerCount": row.player_count,
            "startedAt": iso(row.started_at),
            "endedAt": iso(row.ended_at),
            "winnerFaction": row.winner_faction,
            "endRound": row.end_round,
            "status": "finished" if row.ended_at is not None else "running",
            "players": [
                {
                    "seat": player.seat,
                    "userId": player.user_id,
                    "isAi": player.is_ai,
                    "role": player.role,
                    "faction": player.faction,
                    "isWinner": player.is_winner,
                    "isSheriff": player.is_sheriff,
                    "deathRound": player.death_round,
                    "deathCause": player.death_cause,
                }
                for player in players.get(row.id, [])
            ],
        }
        for row in rows
    ]
    return ok(data, page_meta(page, page_size, total))


@router.get("/games/rpg/history")
async def rpg_history(
    _session: AdminReadSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=100),
    search: str = Query(default="", max_length=120),
    status_filter: Literal["all", "running", "finished"] = Query(
        default="all", alias="status"
    ),
) -> dict[str, Any]:
    if _rpg_state() is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "跑团子插件未加载")
    from ..yawn_rpg.models import (  # pyright: ignore[reportMissingImports]
        RPGGame,
        RPGPlayer,
    )

    page, page_size = page_params(page, page_size)
    rows, total = await _paged_history(
        RPGGame,
        page=page,
        page_size=page_size,
        search=search.strip(),
        status_filter=status_filter,
    )
    game_ids = [row.id for row in rows]
    players: dict[int, list[Any]] = {}
    if game_ids:
        async with get_session() as db:
            player_rows = (
                (
                    await db.execute(
                        select(RPGPlayer)
                        .where(RPGPlayer.game_id.in_(game_ids))
                        .order_by(RPGPlayer.game_id, RPGPlayer.id)
                    )
                )
                .scalars()
                .all()
            )
            for player in player_rows:
                players.setdefault(player.game_id, []).append(player)
    names = await _group_names({row.group_id for row in rows})
    data = [
        {
            "id": row.id,
            "groupId": row.group_id,
            "groupName": names.get(row.group_id),
            "hostUserId": row.host_user_id,
            "moduleId": row.module_id,
            "moduleName": row.module_name,
            "eventLogId": getattr(row, "event_log_id", None),
            "playerCount": row.player_count,
            "startedAt": iso(row.started_at),
            "endedAt": iso(row.ended_at),
            "endingId": row.ending_id,
            "outcome": row.outcome,
            "terminationReason": row.termination_reason,
            "status": "finished" if row.ended_at is not None else "running",
            "players": [
                {
                    "userId": player.user_id,
                    "charName": player.char_name,
                    "startHp": player.start_hp,
                    "startSan": player.start_san,
                    "finalHp": player.final_hp,
                    "finalSan": player.final_san,
                    "isIncapped": player.is_incapped,
                    "survived": player.survived,
                }
                for player in players.get(row.id, [])
            ],
        }
        for row in rows
    ]
    return ok(data, page_meta(page, page_size, total))


@router.get("/games/rpg/history/{row_id}/replay")
async def get_rpg_history_replay(
    row_id: int,
    _session: AdminReadSession,
) -> dict[str, Any]:
    """按 ORM 对局行定位事件日志，并返回公开回放投影。"""
    if _rpg_state() is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "跑团子插件未加载")
    from ..yawn_rpg.models import RPGGame  # pyright: ignore[reportMissingImports]

    async with get_session() as db:
        row = await db.get(RPGGame, row_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "没有找到这局跑团记录")
    event_log_id = getattr(row, "event_log_id", None)
    if not event_log_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "这局没有可用的事件日志回放")
    return ok(load_replay(event_log_id, game_kind="rpg", view="public").as_dict())
