"""狼人杀的内存状态管理模块。

注册表结构与身份守卫式清理仿照 chat_state.py 的 worker 模式：
引擎任务（engine.run_game）独占一局的状态变更，命令处理器只
负责校验并把 Action 投入 action_queue。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

from .. import game_registry  # noqa: TID252
from ..metrics import record_queue_rejection  # noqa: TID252
from .roles import DEFAULT_BOARD_KEY

if TYPE_CHECKING:
    from .roles import DeathCause, Faction, Role


class Phase(str, Enum):
    """游戏阶段。"""

    SIGNUP = "SIGNUP"  # 报名
    DEALING = "DEALING"  # 发牌及身份下发（锁定报名状态）
    NIGHT_HALFBLOOD = "NIGHT_HALFBLOOD"  # 混血儿认主（仅首夜，率先睁眼）
    NIGHT_WOLVES = "NIGHT_WOLVES"  # 狼人行动
    NIGHT_WITCH = "NIGHT_WITCH"  # 女巫行动
    NIGHT_SEER = "NIGHT_SEER"  # 预言家行动
    NIGHT_ELDER = "NIGHT_ELDER"  # 禁言长老行动
    DAY_ANNOUNCE = "DAY_ANNOUNCE"  # 白天死讯播报
    LAST_WORDS = "LAST_WORDS"  # 遗言
    HUNTER_SHOT = "HUNTER_SHOT"  # 猎人开枪决策
    BADGE_TRANSFER = "BADGE_TRANSFER"  # 警徽移交决策
    SHERIFF_REGISTER = "SHERIFF_REGISTER"  # 警长竞选报名
    SHERIFF_SPEECH = "SHERIFF_SPEECH"  # 竞选发言
    SHERIFF_VOTE = "SHERIFF_VOTE"  # 警长投票
    SHERIFF_FINAL_SPEECH = "SHERIFF_FINAL_SPEECH"  # 警长平票终辩
    SHERIFF_REVOTE = "SHERIFF_REVOTE"  # 警长平票重投
    DAY_SPEECH = "DAY_SPEECH"  # 白天轮流发言
    DAY_VOTE = "DAY_VOTE"  # 放逐投票
    PK_SPEECH = "PK_SPEECH"  # 平票 PK 发言
    PK_VOTE = "PK_VOTE"  # 平票 PK 投票
    ENDED = "ENDED"


# 允许狼人 /自爆 中断的白天子阶段
SELF_DETONATE_PHASES: frozenset[Phase] = frozenset(
    {
        Phase.LAST_WORDS,
        Phase.SHERIFF_REGISTER,
        Phase.SHERIFF_SPEECH,
        Phase.SHERIFF_VOTE,
        Phase.SHERIFF_FINAL_SPEECH,
        Phase.SHERIFF_REVOTE,
        Phase.DAY_SPEECH,
        Phase.DAY_VOTE,
        Phase.PK_SPEECH,
        Phase.PK_VOTE,
    }
)

# 允许骑士 /决斗 翻牌的白天子阶段（放逐投票前的发言阶段）
DUEL_PHASES: frozenset[Phase] = frozenset(
    {
        Phase.DAY_SPEECH,
        Phase.PK_SPEECH,
    }
)


class ActionKind(str, Enum):
    """玩家行动类型。"""

    KILL = "kill"  # 狼人刀（value=目标座位）
    SAVE = "save"  # 女巫救人
    POISON = "poison"  # 女巫毒（value=目标座位）
    CHECK = "check"  # 预言家查验（value=目标座位）
    SHOOT = "shoot"  # 猎人开枪（value=目标座位）
    NO_SHOOT = "no_shoot"  # 猎人不开枪
    SKIP = "skip"  # 跳过（夜间行动 / 发言 / 遗言）
    VOTE = "vote"  # 投票（value=目标座位）
    ABSTAIN = "abstain"  # 弃票
    RUN = "run"  # 上警
    WITHDRAW = "withdraw"  # 退水
    ORDER = "order"  # 警长定发言序（value=起始座位，aux=cw|ccw）
    PASS_BADGE = "pass_badge"  # 移交警徽（value=目标座位）
    TEAR_BADGE = "tear_badge"  # 撕警徽
    SELF_DETONATE = "self_detonate"  # 狼人自爆
    SAY = "say"  # 狼人讨论发言（aux=文本，由引擎转发给其他狼人）
    START_GAME = "start_game"  # 房主/管理员手动开局（报名阶段）
    CHOOSE_OWNER = "choose_owner"  # 混血儿认主（value=主人座位）
    SILENCE = "silence"  # 禁言长老禁言（value=目标座位）
    DUEL = "duel"  # 骑士决斗（value=目标座位）


@dataclass
class Action:
    """命令处理器提交给引擎的一次玩家行动。"""

    kind: ActionKind
    actor_user_id: int
    value: Optional[int] = None
    aux: Optional[str] = None
    # AI 后台任务创建时的阶段代币；人类命令为 None。
    phase_token: Optional[int] = None


def _same_action(left: Action, right: Action) -> bool:
    """判断仍在队列中的重复行动。"""
    return (
        left.kind is right.kind
        and left.actor_user_id == right.actor_user_id
        and left.value == right.value
        and left.aux == right.aux
    )


@dataclass
class PlayerState:
    """玩家在局内的内存状态。"""

    user_id: int
    seat: int
    role: Role
    faction: Faction
    alive: bool = True
    death_round: Optional[int] = None
    death_cause: Optional[DeathCause] = None
    # AI 玩家：user_id 为负数合成 ID，不发私聊/禁言 API
    is_ai: bool = False
    # AI 伪装昵称（报名名单与代发发言显示用；人类玩家为 None）
    display_name: Optional[str] = None
    # 女巫药剂状态
    save_used: bool = False
    poison_used: bool = False
    # 白痴翻牌状态
    idiot_revealed: bool = False
    can_vote: bool = True
    # 警长状态
    sheriff_candidate: bool = False
    is_sheriff: bool = False
    was_sheriff: bool = False  # 曾经担任警长（战绩统计用）
    # 混血儿认主状态（主人座位；胜负随主人阵营）
    owner_seat: Optional[int] = None
    # 禁言长老上一晚的实际禁言目标（连续两晚不可同人；放弃后清空）
    elder_last_target: Optional[int] = None
    # 身份卡私聊是否投递成功
    dm_ok: bool = True


@dataclass
class Game:
    """单局游戏的内存状态（一群同时仅一局）。"""

    group_id: int
    host_user_id: int
    phase: Phase = Phase.SIGNUP
    phase_token: int = 0
    round_no: int = 0
    # 板子键名（roles.BOARDS 的键；报名阶段可由房主 /板子 切换）
    board: str = DEFAULT_BOARD_KEY
    # 禁言长老当夜禁言的座位（次日发言/投票限制用；每晚进入长老阶段时清空）
    silenced_seat: Optional[int] = None
    # 发牌后填充
    players: list[PlayerState] = field(default_factory=list)
    # 报名顺序（退报名移除）
    signup_user_ids: list[int] = field(default_factory=list)
    # 命令处理器投入、引擎串行消费的行动队列
    action_queue: asyncio.Queue[Action] = field(
        default_factory=lambda: asyncio.Queue(maxsize=100)
    )
    pending_actions: dict[int, Action] = field(default_factory=dict)
    pending_by_user: dict[int, int] = field(default_factory=dict)
    worker: Optional[asyncio.Task[None]] = None
    # 持久化 WerewolfGame 行主键
    game_row_id: Optional[int] = None
    # 携带 onebot v11 Bot 实例；用 Any 避免 nonebot 基类与适配器类型冲突
    bot: Any = None
    # ── 引擎 → AI 驱动的只读信号（仅引擎写入）──
    # 当前发言者座位（发言轮换 / 遗言期间），None=无
    current_speaker: Optional[int] = None
    # 当前投票阶段的合法目标座位 / 被排除座位（_collect_votes 写入）
    vote_targets: list[int] = field(default_factory=list)
    vote_exclude: tuple[int, ...] = ()
    # AI 玩家昵称分配表（AI user_id -> 伪装昵称）
    ai_names: dict[int, str] = field(default_factory=dict)
    # 人类报名者显示名（user_id -> 群名片/昵称；报名阶段由命令层记录，
    # 仅内存，不入 ORM）
    signup_names: dict[int, str] = field(default_factory=dict)
    # 报名阶段身份请求（user_id -> 期望角色；命令层记录，仅内存，
    # 发牌时由引擎消费，退报名即清理）
    role_requests: dict[int, Role] = field(default_factory=dict)
    # 事件日志使用独立的进程内稳定 id，不依赖 ORM 是否成功写入。
    event_log_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    # ── 玩家查询 ──────────────────────────────────────

    def player_by_user(self, user_id: int) -> Optional[PlayerState]:
        """按 QQ 号查玩家。"""
        for p in self.players:
            if p.user_id == user_id:
                return p
        return None

    def player_by_seat(self, seat: int) -> Optional[PlayerState]:
        """按座位号查玩家。"""
        for p in self.players:
            if p.seat == seat:
                return p
        return None

    def alive_players(self) -> list[PlayerState]:
        """存活玩家（按座位升序）。"""
        return sorted(
            (p for p in self.players if p.alive),
            key=lambda p: p.seat,
        )

    def alive_players_of_role(self, role: Role) -> list[PlayerState]:
        """存活的指定角色玩家。"""
        return [p for p in self.alive_players() if p.role is role]

    def sheriff(self) -> Optional[PlayerState]:
        """当前警长（存活与否均返回）。"""
        for p in self.players:
            if p.is_sheriff:
                return p
        return None

    def drain_actions(self) -> None:
        """非阻塞清空行动队列（阶段切换时防上一阶段指令泄漏）。"""
        while not self.action_queue.empty():
            try:
                action = self.action_queue.get_nowait()
                self.action_queue.task_done()
                release_action(self, action)
            except asyncio.QueueEmpty:  # noqa: PERF203
                break


# ── AI 玩家合成身份 ───────────────────────────────────────

# 负数 user_id 永不与真实 QQ 号冲突；_user_index 保证跨局唯一在局
AI_UID_BASE = -10000
_ai_uid_counter = 0

# AI 伪装昵称池（普通群昵称风格，不透露 AI 身份）
AI_NICKNAMES: tuple[str, ...] = (
    "阿宝",
    "小鱼",
    "团子",
    "栗子",
    "年糕",
    "布丁",
    "奶茶",
    "土豆",
    "花生",
    "芝麻",
    "汤圆",
    "橘子",
    "海苔",
    "麻薯",
    "柚子",
    "豆浆",
    "核桃",
    "樱桃",
    "麦兜",
    "胖虎",
)


def new_ai_uid() -> int:
    """分配一个新的 AI 合成 user_id（负数）。"""
    global _ai_uid_counter  # noqa: PLW0603
    _ai_uid_counter += 1
    return AI_UID_BASE - _ai_uid_counter


def is_ai_uid(user_id: int) -> bool:
    """判断 user_id 是否为 AI 合成 ID。"""
    return user_id < 0


def pick_ai_name(game: Game) -> str:
    """为本局新 AI 玩家取一个不重复的伪装昵称。"""
    used = set(game.ai_names.values())
    for name in AI_NICKNAMES:
        if name not in used:
            return name
    return f"玩家{len(game.ai_names) + 1}"


def add_ai_signup(game: Game) -> Optional[int]:
    """为当前对局报名一个 AI 玩家；返回其 user_id，失败返回 None。"""
    uid = new_ai_uid()
    if not join_signup(game, uid):
        return None
    game.ai_names[uid] = pick_ai_name(game)
    return uid


def remove_ai_signup(game: Game) -> bool:
    """移除最近加入的一个 AI 报名者；无 AI 返回 False。"""
    for uid in reversed(game.signup_user_ids):
        if not is_ai_uid(uid):
            continue
        if leave_signup(game, uid):
            game.ai_names.pop(uid, None)
            return True
    return False


def count_ai_signup(game: Game) -> int:
    """统计当前报名中的 AI 数量。"""
    return sum(1 for uid in game.signup_user_ids if is_ai_uid(uid))


# ── 注册表 ────────────────────────────────────────────────

_games: dict[int, Game] = {}  # group_id -> game
# user_id -> group_id：私聊路由，并保证用户跨群唯一在局
_user_index: dict[int, int] = {}


def get_game(group_id: int) -> Optional[Game]:
    """获取群内对局，不存在返回 None。"""
    return _games.get(group_id)


def game_of_user(user_id: int) -> Optional[Game]:
    """获取用户所在群的对局，不在局中返回 None。"""
    group_id = _user_index.get(user_id)
    if group_id is None:
        return None
    return _games.get(group_id)


def all_games() -> list[Game]:
    """当前进程内全部对局的快照（WebUI 对局监控用）。"""
    return list(_games.values())


def create_game(
    group_id: int,
    host_user_id: int,
    *,
    queue_max: int = 100,
) -> Optional[Game]:
    """创建对局并把房主登记为首位报名者。

    群内已有对局或房主已在其他局中时返回 None。
    """
    if group_id in _games or host_user_id in _user_index:
        return None
    if not game_registry.reserve_game("werewolf", group_id, host_user_id):
        return None
    game = Game(
        group_id=group_id,
        host_user_id=host_user_id,
        action_queue=asyncio.Queue(maxsize=max(1, queue_max)),
    )
    _games[group_id] = game
    _user_index[host_user_id] = group_id
    game.signup_user_ids.append(host_user_id)
    return game


def join_signup(game: Game, user_id: int) -> bool:
    """报名；已在任意局中或已报名返回 False。"""
    if user_id in _user_index or user_id in game.signup_user_ids:
        return False
    if not game_registry.reserve_user("werewolf", game.group_id, user_id):
        return False
    _user_index[user_id] = game.group_id
    game.signup_user_ids.append(user_id)
    return True


def leave_signup(game: Game, user_id: int) -> bool:
    """退出报名；未报名返回 False。"""
    if user_id not in game.signup_user_ids:
        return False
    game.signup_user_ids.remove(user_id)
    game.signup_names.pop(user_id, None)
    game.role_requests.pop(user_id, None)
    if _user_index.get(user_id) == game.group_id:
        _user_index.pop(user_id, None)
    game_registry.release_user("werewolf", game.group_id, user_id)
    return True


def submit_action(
    game: Game,
    action: Action,
    *,
    user_pending_max: int,
    allow_nonmember: bool = False,
) -> bool:
    """唯一行动入队入口，提供容量、单用户配额与重复行动去重。"""
    members = (
        game.signup_user_ids
        if game.phase in (Phase.SIGNUP, Phase.DEALING)
        else [player.user_id for player in game.players]
    )
    if action.actor_user_id not in members:
        allowed_admin_start = (
            allow_nonmember
            and game.phase is Phase.SIGNUP
            and action.kind is ActionKind.START_GAME
        )
        if not allowed_admin_start:
            record_queue_rejection("action_queue", "werewolf", "not_member")
            return False
    if any(
        _same_action(existing, action)
        for existing in game.pending_actions.values()
    ):
        record_queue_rejection("action_queue", "werewolf", "duplicate")
        return False
    if game.pending_by_user.get(action.actor_user_id, 0) >= max(user_pending_max, 1):
        record_queue_rejection("action_queue", "werewolf", "user_limit")
        return False
    try:
        game.action_queue.put_nowait(action)
    except asyncio.QueueFull:
        record_queue_rejection("action_queue", "werewolf", "queue_full")
        return False
    game.pending_actions[id(action)] = action
    game.pending_by_user[action.actor_user_id] = (
        game.pending_by_user.get(action.actor_user_id, 0) + 1
    )
    return True


def release_action(game: Game, action: Action) -> None:
    """引擎取走或清理行动后释放配额。"""
    if game.pending_actions.pop(id(action), None) is None:
        return
    count = game.pending_by_user.get(action.actor_user_id, 0) - 1
    if count > 0:
        game.pending_by_user[action.actor_user_id] = count
    else:
        game.pending_by_user.pop(action.actor_user_id, None)


def note_signup_name(game: Game, user_id: int, name: str) -> None:
    """记录人类报名者的显示名（报名阶段由命令层调用）。"""
    if name:
        game.signup_names[user_id] = name


def display_name_of(game: Game, user_id: int) -> str:
    """显示名：AI 伪装名 > 报名昵称 > QQ 号。"""
    return game.ai_names.get(user_id) or game.signup_names.get(user_id) or str(user_id)


def discard_game(game: Game) -> None:
    """身份守卫清理：仅摘除仍属于本局的状态。

    由引擎任务的 finally 调用；热重载后残留的对局对象
    不会误删新对局的注册信息。
    """
    if _games.get(game.group_id) is not game:
        return
    _games.pop(game.group_id, None)
    for uid, gid in list(_user_index.items()):
        if gid == game.group_id:
            _user_index.pop(uid, None)
    game_registry.release_game("werewolf", game.group_id)


async def stop_game(game: Game) -> None:
    """强制结束对局：先摘 worker 引用再取消，等待清理完成。"""
    game.phase = Phase.ENDED
    task = game.worker
    game.worker = None
    if task is not None and not task.done():
        task.cancel()
        # wait() 不抛出被取消任务的 CancelledError（引擎 finally 里的
        # 清理因此不会被打断），而外部对本协程的取消照常传播
        await asyncio.wait([task])
    # 任务若在首次调度前被取消，不会进入 run_game 的 finally。
    if _games.get(game.group_id) is game:
        discard_game(game)
