# ruff: noqa: E501,TID252,C901,PLR0912,PLR0915,N806,FAST002,PERF403
"""狼人杀管理台 2.0 的只读端点：规则元数据、战绩统计、单局详情与公开回放。

全部端点只读：规则表来自 ``yawn_werewolf.roles.BOARDS``，配置来自子插件
``Config``，统计聚合 ``WerewolfGame`` / ``WerewolfPlayer`` 两张赛后总结表；
回放复用 ``replay.load_replay`` 的公开视角投影（与 RPG 回放同一口径）。
子插件未加载时统一 503，与 ``games.py`` 的降级语义一致。

实时对局快照、强停与战绩分页仍在 ``games.py``；本模块只新增端点，
不 import ``commands``（避免注册 nonebot matcher）。
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from nonebot import get_plugin_config, logger
from nonebot_plugin_orm import get_session
from sqlalchemy import select

from ..data_models.bot_group import BotGroup
from ..data_models.bot_user import BotUser
from ..replay import load_replay
from .config import API_PATH
from .deps import AdminReadSession, ok
from .service import iso

router = APIRouter(prefix=API_PATH)

# 统计最多扫描的对局数：赛后总结表很小，但仍给聚合一个硬上限，
# 避免多年数据一次性拉进内存。
STATS_MAX_GAMES = 5000
LEADERBOARD_LIMIT = 50
_REPLAY_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _ww_modules() -> tuple[Any, Any]:
    """返回 (roles, config 模块)；子插件不可用时 503。"""
    try:
        from ..yawn_werewolf import (
            config as config_module,  # pyright: ignore[reportMissingImports]
        )
        from ..yawn_werewolf import roles  # pyright: ignore[reportMissingImports]
    except Exception as exc:
        logger.debug(f"狼人杀子插件不可用，管理台元数据降级：{exc}")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "狼人杀子插件未加载") from exc
    return roles, config_module


def _ww_models() -> tuple[Any, Any]:
    try:
        from ..yawn_werewolf.models import (  # pyright: ignore[reportMissingImports]
            WerewolfGame,
            WerewolfPlayer,
        )
    except Exception as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "狼人杀子插件未加载") from exc
    return WerewolfGame, WerewolfPlayer


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


# ── 规则与配置 ────────────────────────────────────────────


def phase_timeouts(cfg: Any, final_speech_seconds: int) -> dict[str, int]:
    """各阶段的引擎窗口（秒），供前端估算倒计时；未知阶段不给。"""

    def num(name: str, default: float) -> int:
        return int(getattr(cfg, name, default))

    night = num("ww_night_timeout", 60)
    speech = num("ww_speech_timeout", 120)
    vote = num("ww_vote_timeout", 90)
    badge = num("ww_badge_timeout", 45)
    return {
        "SIGNUP": num("ww_signup_timeout", 180),
        "NIGHT_HALFBLOOD": night,
        "NIGHT_WOLVES": num("ww_wolf_timeout", 180),
        "NIGHT_WITCH": night,
        "NIGHT_SEER": night,
        "NIGHT_ELDER": night,
        "LAST_WORDS": num("ww_last_words_timeout", 60),
        "HUNTER_SHOT": num("ww_hunter_timeout", 60),
        "BADGE_TRANSFER": badge,
        "SHERIFF_REGISTER": num("ww_sheriff_register_timeout", 45),
        "SHERIFF_SPEECH": speech,
        "SHERIFF_VOTE": vote,
        "SHERIFF_FINAL_SPEECH": final_speech_seconds,
        "SHERIFF_REVOTE": vote,
        "DAY_SPEECH": speech,
        "DAY_VOTE": vote,
        "PK_SPEECH": speech,
        "PK_VOTE": vote,
    }


def build_meta(roles: Any, config_module: Any, cfg: Any) -> dict[str, Any]:
    """板子、角色、配置的只读快照（纯函数，便于测试）。"""
    god_roles = {_enum_value(role) for role in getattr(roles, "GOD_ROLES", ())}
    role_faction = getattr(roles, "ROLE_FACTION", {})
    skill_text = getattr(roles, "ROLE_SKILL_TEXT", {})
    boards = []
    for key, spec in roles.BOARDS.items():
        counts = []
        for player_count in sorted(spec.counts):
            composition = spec.counts[player_count]
            role_rows = [
                {
                    "role": _enum_value(role),
                    "count": int(count),
                    "faction": _enum_value(role_faction.get(role, "good")),
                    "isGod": _enum_value(role) in god_roles,
                }
                for role, count in composition.items()
            ]
            wolves = sum(row["count"] for row in role_rows if row["faction"] == "wolf")
            gods = sum(row["count"] for row in role_rows if row["isGod"])
            counts.append(
                {
                    "players": player_count,
                    "wolves": wolves,
                    "gods": gods,
                    "villagers": player_count - wolves - gods,
                    "roles": role_rows,
                }
            )
        boards.append(
            {
                "key": key,
                "silenceMode": getattr(spec, "silence_mode", None),
                "isDefault": key == getattr(roles, "DEFAULT_BOARD_KEY", None),
                "description": getattr(spec, "description", None) or None,
                "counts": counts,
            }
        )
    role_rows = []
    for role in roles.Role:
        text = skill_text.get(role, "")
        if role is getattr(roles.Role, "SILENT_ELDER", None):
            vote_text = getattr(roles, "SILENT_ELDER_VOTE_SKILL_TEXT", "")
        else:
            vote_text = ""
        role_rows.append(
            {
                "role": _enum_value(role),
                "faction": _enum_value(role_faction.get(role, "good")),
                "isGod": _enum_value(role) in god_roles,
                "skill": text,
                "voteVariantSkill": vote_text or None,
            }
        )
    config_values: dict[str, Any] = {}
    dump = cfg.model_dump() if hasattr(cfg, "model_dump") else dict(vars(cfg))
    for name, value in dump.items():
        if isinstance(value, (bool, int, float, str)) or value is None:
            config_values[name] = value
    final_speech = int(getattr(config_module, "SHERIFF_FINAL_SPEECH_SECONDS", 60))
    return {
        "defaultBoard": getattr(roles, "DEFAULT_BOARD_KEY", None),
        "boards": boards,
        "roles": role_rows,
        "config": config_values,
        "phaseTimeouts": phase_timeouts(cfg, final_speech),
        "sheriffFinalSpeechSeconds": final_speech,
    }


@router.get("/games/werewolf/meta")
async def get_werewolf_meta(_session: AdminReadSession) -> dict[str, Any]:
    roles, config_module = _ww_modules()
    try:
        cfg = get_plugin_config(config_module.Config)
    except Exception:  # noqa: BLE001
        cfg = config_module.Config()
    data = build_meta(roles, config_module, cfg)
    data["commands"] = _commands()
    return ok(data)


def _commands() -> list[dict[str, Any]]:
    """群内/私聊命令速查（命令声明表与 matcher 同源，不 import commands）。"""
    try:
        from ..yawn_werewolf.command_definitions import (  # pyright: ignore[reportMissingImports]
            COMMAND_DEFINITIONS,
        )
    except Exception:  # noqa: BLE001
        return []
    return [
        {
            "name": getattr(item, "name", ""),
            "aliases": list(getattr(item, "aliases", ()) or ()),
            "description": getattr(item, "description", ""),
            "scope": getattr(item, "scope", None),
            "permission": getattr(item, "permission", None),
            "level": getattr(item, "display_level", None),
        }
        for item in COMMAND_DEFINITIONS
    ]


# ── 统计 ──────────────────────────────────────────────────


def _player_won(player: Any) -> bool | None:
    return getattr(player, "is_winner", None)


def compute_stats(
    games: list[Any],
    players: list[Any],
    *,
    names: dict[int, str | None] | None = None,
    group_names: dict[int, str | None] | None = None,
) -> dict[str, Any]:
    """把赛后总结行聚合成管理台统计（纯函数）。

    ``games`` / ``players`` 只需有 ORM 同名属性；未结束（流局/强停）的
    对局计入总数与中断数，但不参与胜率、轮次与角色统计。
    """
    names = names or {}
    group_names = group_names or {}
    by_game: dict[int, list[Any]] = defaultdict(list)
    for player in players:
        by_game[player.game_id].append(player)
    finished = [game for game in games if game.ended_at is not None and game.winner_faction]
    wolf_wins = sum(1 for game in finished if game.winner_faction == "wolf")
    good_wins = sum(1 for game in finished if game.winner_faction == "good")
    rounds = [int(game.end_round) for game in finished if game.end_round is not None]
    seats = [player for game in games for player in by_game.get(game.id, [])]
    ai_seats = sum(1 for player in seats if player.is_ai)

    boards: dict[str, dict[str, Any]] = {}
    for game in games:
        key = game.board or "未知板子"
        row = boards.setdefault(
            key,
            {"board": key, "games": 0, "finished": 0, "wolfWins": 0, "goodWins": 0, "roundSum": 0},
        )
        row["games"] += 1
        if game in finished:
            row["finished"] += 1
            row["wolfWins" if game.winner_faction == "wolf" else "goodWins"] += 1
            row["roundSum"] += int(game.end_round or 0)
    board_rows = []
    for row in sorted(boards.values(), key=lambda item: -item["games"]):
        finished_count = row.pop("finished")
        round_sum = row.pop("roundSum")
        row["finished"] = finished_count
        row["avgRounds"] = round(round_sum / finished_count, 2) if finished_count else None
        board_rows.append(row)

    finished_ids = {game.id for game in finished}
    role_stats: dict[str, dict[str, Any]] = {}
    causes: Counter[str] = Counter()
    death_rounds: Counter[int] = Counter()
    ai_record = {"seats": 0, "wins": 0}
    human_record = {"seats": 0, "wins": 0}
    people: dict[int, dict[str, Any]] = {}
    for game_id in finished_ids:
        for player in by_game.get(game_id, []):
            won = bool(_player_won(player))
            stat = role_stats.setdefault(
                player.role,
                {"role": player.role, "faction": player.faction, "seats": 0, "wins": 0, "survived": 0, "sheriff": 0},
            )
            stat["seats"] += 1
            stat["wins"] += int(won)
            stat["survived"] += int(player.death_round is None)
            stat["sheriff"] += int(bool(player.is_sheriff))
            record = ai_record if player.is_ai else human_record
            record["seats"] += 1
            record["wins"] += int(won)
            if player.death_cause:
                causes[str(player.death_cause)] += 1
            if player.death_round is not None:
                death_rounds[int(player.death_round)] += 1
            if not player.is_ai:
                person = people.setdefault(
                    player.user_id,
                    {"userId": player.user_id, "games": 0, "wins": 0, "survived": 0, "sheriff": 0, "wolfGames": 0, "roles": Counter()},
                )
                person["games"] += 1
                person["wins"] += int(won)
                person["survived"] += int(player.death_round is None)
                person["sheriff"] += int(bool(player.is_sheriff))
                person["wolfGames"] += int(player.faction == "wolf")
                person["roles"][player.role] += 1
    role_rows = []
    for stat in sorted(role_stats.values(), key=lambda item: -item["seats"]):
        seats_count = stat["seats"] or 1
        stat["winRate"] = round(stat["wins"] / seats_count, 4)
        stat["survivalRate"] = round(stat["survived"] / seats_count, 4)
        role_rows.append(stat)

    leaderboard = []
    for person in people.values():
        roles_counter: Counter[str] = person.pop("roles")
        person["name"] = names.get(person["userId"])
        person["winRate"] = round(person["wins"] / person["games"], 4) if person["games"] else 0
        person["favoriteRole"] = roles_counter.most_common(1)[0][0] if roles_counter else None
        leaderboard.append(person)
    leaderboard.sort(key=lambda item: (-item["games"], -item["winRate"], item["userId"]))

    daily: dict[str, dict[str, Any]] = {}
    for game in games:
        if game.started_at is None:
            continue
        day = game.started_at.date().isoformat()
        row = daily.setdefault(day, {"date": day, "games": 0, "wolfWins": 0, "goodWins": 0, "unfinished": 0})
        row["games"] += 1
        if game in finished:
            row["wolfWins" if game.winner_faction == "wolf" else "goodWins"] += 1
        else:
            row["unfinished"] += 1

    group_counter: Counter[int] = Counter(game.group_id for game in games)
    groups = [
        {"groupId": group_id, "groupName": group_names.get(group_id), "games": count}
        for group_id, count in group_counter.most_common(10)
    ]
    hours: Counter[int] = Counter(game.started_at.hour for game in games if game.started_at is not None)

    def rate(record: dict[str, int]) -> float | None:
        return round(record["wins"] / record["seats"], 4) if record["seats"] else None

    return {
        "totals": {
            "games": len(games),
            "finished": len(finished),
            "unfinished": len(games) - len(finished),
            "wolfWins": wolf_wins,
            "goodWins": good_wins,
            "wolfWinRate": round(wolf_wins / len(finished), 4) if finished else None,
            "avgRounds": round(sum(rounds) / len(rounds), 2) if rounds else None,
            "avgPlayers": round(sum(game.player_count for game in games) / len(games), 2) if games else None,
            "aiSeatShare": round(ai_seats / len(seats), 4) if seats else None,
            "humanPlayers": len({p.user_id for p in seats if not p.is_ai}),
            "groups": len(group_counter),
        },
        "boards": board_rows,
        "roles": role_rows,
        "deathCauses": [{"cause": cause, "count": count} for cause, count in causes.most_common()],
        "deathRounds": [{"round": rnd, "count": death_rounds[rnd]} for rnd in sorted(death_rounds)],
        "rounds": [{"round": rnd, "count": count} for rnd, count in sorted(Counter(rounds).items())],
        "aiVsHuman": {
            "ai": {**ai_record, "winRate": rate(ai_record)},
            "human": {**human_record, "winRate": rate(human_record)},
        },
        "daily": [daily[key] for key in sorted(daily)],
        "hours": [{"hour": hour, "count": hours.get(hour, 0)} for hour in range(24)],
        "groups": groups,
        "leaderboard": leaderboard[:LEADERBOARD_LIMIT],
    }


async def _user_names(user_ids: set[int]) -> dict[int, str | None]:
    ids = {uid for uid in user_ids if uid > 0}
    if not ids:
        return {}
    async with get_session() as db:
        rows = (await db.execute(select(BotUser.user_id, BotUser.nickname).where(BotUser.user_id.in_(ids)))).all()
    return {user_id: name for user_id, name in rows}  # noqa: C416


async def _group_names(group_ids: set[int]) -> dict[int, str | None]:
    if not group_ids:
        return {}
    async with get_session() as db:
        rows = (await db.execute(select(BotGroup.group_id, BotGroup.group_name).where(BotGroup.group_id.in_(group_ids)))).all()
    return {group_id: name for group_id, name in rows}  # noqa: C416


def _now_bj() -> datetime:
    return datetime.utcnow() + timedelta(hours=8)  # noqa: DTZ003


@router.get("/games/werewolf/stats")
async def get_werewolf_stats(
    _session: AdminReadSession,
    days: int = Query(default=0, ge=0, le=3650),
    board: str = Query(default="", max_length=32),
    group_id: int | None = Query(default=None, alias="groupId"),
) -> dict[str, Any]:
    """战绩统计；days=0 表示全部时间。"""
    _ww_modules()
    WerewolfGame, WerewolfPlayer = _ww_models()
    stmt = select(WerewolfGame).order_by(WerewolfGame.started_at.desc(), WerewolfGame.id.desc())
    if days:
        stmt = stmt.where(WerewolfGame.started_at >= _now_bj() - timedelta(days=days))
    if board:
        stmt = stmt.where(WerewolfGame.board == board)
    if group_id is not None:
        stmt = stmt.where(WerewolfGame.group_id == group_id)
    async with get_session() as db:
        games = list((await db.execute(stmt.limit(STATS_MAX_GAMES + 1))).scalars().all())
        truncated = len(games) > STATS_MAX_GAMES
        games = games[:STATS_MAX_GAMES]
        game_ids = [game.id for game in games]
        players = (
            list((await db.execute(select(WerewolfPlayer).where(WerewolfPlayer.game_id.in_(game_ids)))).scalars().all())
            if game_ids
            else []
        )
    names = await _user_names({player.user_id for player in players})
    group_names = await _group_names({game.group_id for game in games})
    data = compute_stats(games, players, names=names, group_names=group_names)
    data["truncated"] = truncated
    data["window"] = {"days": days, "board": board or None, "groupId": group_id}
    return ok(data)


# ── 单局详情与回放 ────────────────────────────────────────


@router.get("/games/werewolf/history/{row_id}")
async def get_werewolf_history_game(row_id: int, _session: AdminReadSession) -> dict[str, Any]:
    """单局赛后详情：带玩家昵称与按轮出局顺序。"""
    _ww_modules()
    WerewolfGame, WerewolfPlayer = _ww_models()
    async with get_session() as db:
        row = await db.get(WerewolfGame, row_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "没有找到这局狼人杀记录")
        game = {
            "id": row.id,
            "groupId": row.group_id,
            "hostUserId": row.host_user_id,
            "board": row.board,
            "playerCount": row.player_count,
            "startedAt": iso(row.started_at),
            "endedAt": iso(row.ended_at),
            "winnerFaction": row.winner_faction,
            "endRound": row.end_round,
            "status": "finished" if row.ended_at is not None else "running",
        }
        player_rows = list(
            (await db.execute(select(WerewolfPlayer).where(WerewolfPlayer.game_id == row_id).order_by(WerewolfPlayer.seat))).scalars().all()
        )
        players = [
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
            for player in player_rows
        ]
    names = await _user_names({player["userId"] for player in players} | {game["hostUserId"]})
    group_names = await _group_names({game["groupId"]})
    for player in players:
        player["name"] = names.get(player["userId"])
    game["groupName"] = group_names.get(game["groupId"])
    game["hostName"] = names.get(game["hostUserId"])
    game["players"] = players
    return ok(game)


@router.get("/games/werewolf/replay/{replay_id}")
async def get_werewolf_replay(replay_id: str, _session: AdminReadSession) -> dict[str, Any]:
    """按群内终局播报里的「回放编号」读取公开视角回放。"""
    replay_id = replay_id.strip().lower()
    if _REPLAY_ID_RE.fullmatch(replay_id) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "回放编号应为 32 位十六进制字符串")
    projection = load_replay(replay_id, game_kind="werewolf", view="public")
    return ok(projection.as_dict())
