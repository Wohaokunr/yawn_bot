"""跑团的内存状态管理模块。

注册表结构与身份守卫式清理仿照 yawn_werewolf/state.py：
引擎任务（engine.run_game）独占一局的状态变更，命令处理器
只负责校验并把 Action 投入 action_queue。KP 的每一次工具
调用同样由引擎验证后执行，状态写入不经过 AI。
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

from .module_schema import ConditionContext

if TYPE_CHECKING:
    from .charsheet import CharacterSheet
    from .module_schema import ModuleDef


class Phase(str, Enum):
    """游戏阶段。"""

    SIGNUP = "SIGNUP"  # 报名 + 选模组
    CHAR_CREATE = "CHAR_CREATE"  # 建卡（私聊调整角色卡）
    PLAY = "PLAY"  # 局内场景循环
    ENDED = "ENDED"


class ActionKind(str, Enum):
    """玩家行动类型。"""

    START_GAME = "start_game"  # 房主/管理员请求开局
    MODULE_SELECT = "module_select"  # 选定模组（aux=模组 id）
    SAY = "say"  # 群自由发言（aux=文本）
    CHECK = "check"  # 显式检定（aux="技能key"，可选 value 保留难度）
    TALK_NPC = "talk_npc"  # 与 NPC 交谈（aux="npc_id|发言内容"）
    ATTACK = "attack"  # 攻击怪物（aux=怪物 id）
    MOVE = "move"  # 前往出口（aux=关键词/场景名）
    # ── 建卡期私聊行动 ──
    REROLL = "reroll"  # 整卡重掷
    ADD_SKILL = "add_skill"  # 加点（aux=技能 key，value=点数）
    SUB_SKILL = "sub_skill"  # 减点（aux=技能 key，value=点数）
    RESET_SKILLS = "reset_skills"  # 清空加点
    SHOW_CARD = "show_card"  # 重发角色卡
    CONFIRM_CARD = "confirm_card"  # 锁定角色卡


@dataclass
class Action:
    """命令处理器提交给引擎的一次玩家行动。"""

    kind: ActionKind
    actor_user_id: int
    value: Optional[int] = None
    aux: Optional[str] = None


@dataclass
class PlayerState:
    """玩家在局内的内存状态。"""

    user_id: int
    # 玩家编号（1 起，群内称呼用）
    seat: int
    # 角色卡（建卡后填充）
    sheet: Optional["CharacterSheet"] = None
    # 当前 HP / SAN（开局由卡片上限初始化，引擎维护）
    hp: int = 0
    san: int = 0
    # 存活：HP 归零或 SAN 归零即失去行动能力
    incapped: bool = False
    # 建卡状态
    confirmed: bool = False
    rerolls_left: int = 0
    # 角色卡私聊是否投递成功
    dm_ok: bool = True
    # 终局结算：存活与否（对局未结束为 None）
    survived: Optional[bool] = None

    @property
    def alive(self) -> bool:
        """未失去行动能力即视为在场。"""
        return not self.incapped


@dataclass
class Game:
    """单局游戏的内存状态（一群同时仅一局）。"""

    group_id: int
    host_user_id: int
    phase: Phase = Phase.SIGNUP
    # 选定的模组（报名阶段填充；未选时引擎取列表第一个）
    module: Optional["ModuleDef"] = None
    # 开局后填充
    players: list[PlayerState] = field(default_factory=list)
    # 报名顺序（退报名移除）
    signup_user_ids: list[int] = field(default_factory=list)
    # 命令处理器投入、引擎串行消费的行动队列
    action_queue: asyncio.Queue[Action] = field(default_factory=asyncio.Queue)
    worker: Optional[asyncio.Task[None]] = None
    # 持久化 RPGGame 行主键
    game_row_id: Optional[int] = None
    # 携带 onebot v11 Bot 实例；用 Any 避免 nonebot 基类与适配器类型冲突
    bot: Any = None
    # ── 局内状态（仅引擎写入）──────────────────────────
    current_scene: Optional[str] = None
    discovered_clues: set[str] = field(default_factory=set)
    # 已触发的 once 检定点 id
    fired_checks: set[str] = field(default_factory=set)
    # 怪物当前 HP（出场时由引擎按模组初始化）
    monster_hp: dict[str, int] = field(default_factory=dict)
    dead_monsters: set[str] = field(default_factory=set)
    # 群聊记录（KP 上下文与播报存档）
    group_log: deque[str] = field(default_factory=lambda: deque(maxlen=200))
    # ── KP 智能体循环状态 ──
    # 单槽缓冲：SAY 合批时暂存第一个非 SAY 行动
    pending: Optional[Action] = None
    # 端点不支持 tool_call 时降级为纯叙述模式（本局内不再重试工具）
    tools_broken: bool = False
    # 最近一次 KP 调用的事件循环时刻（防刷屏限速）
    last_kp_at: float = 0.0

    # ── 玩家查询 ──────────────────────────────────────

    def player_by_user(self, user_id: int) -> Optional[PlayerState]:
        """按 QQ 号查玩家。"""
        for p in self.players:
            if p.user_id == user_id:
                return p
        return None

    def player_by_seat(self, seat: int) -> Optional[PlayerState]:
        """按编号查玩家。"""
        for p in self.players:
            if p.seat == seat:
                return p
        return None

    def active_players(self) -> list[PlayerState]:
        """未失去行动能力的玩家（按编号升序）。"""
        return sorted(
            (p for p in self.players if not p.incapped),
            key=lambda p: p.seat,
        )

    def all_incapped(self) -> bool:
        """是否全体失去行动能力（坏结局条件）。"""
        return bool(self.players) and all(p.incapped for p in self.players)

    def condition_context(self) -> ConditionContext:
        """组装条件求值所需的事实快照（供出口/结局判定）。"""
        return ConditionContext(
            clues=set(self.discovered_clues),
            dead_monsters=set(self.dead_monsters),
            current_scene=self.current_scene or "",
            all_incapped=self.all_incapped(),
        )

    def drain_actions(self) -> None:
        """非阻塞清空行动队列（场景切换时防上一场景指令泄漏）。"""
        while not self.action_queue.empty():
            try:
                self.action_queue.get_nowait()
                self.action_queue.task_done()
            except asyncio.QueueEmpty:  # noqa: PERF203
                break


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


def create_game(group_id: int, host_user_id: int) -> Optional[Game]:
    """创建对局并把房主登记为首位报名者。

    群内已有对局或房主已在其他局中时返回 None。
    """
    if group_id in _games or host_user_id in _user_index:
        return None
    game = Game(group_id=group_id, host_user_id=host_user_id)
    _games[group_id] = game
    _user_index[host_user_id] = group_id
    game.signup_user_ids.append(host_user_id)
    return game


def join_signup(game: Game, user_id: int) -> bool:
    """报名；已在任意局中或已报名返回 False。"""
    if user_id in _user_index or user_id in game.signup_user_ids:
        return False
    _user_index[user_id] = game.group_id
    game.signup_user_ids.append(user_id)
    return True


def leave_signup(game: Game, user_id: int) -> bool:
    """退出报名；未报名返回 False。"""
    if user_id not in game.signup_user_ids:
        return False
    game.signup_user_ids.remove(user_id)
    if _user_index.get(user_id) == game.group_id:
        _user_index.pop(user_id, None)
    return True


def discard_game(game: Game) -> None:
    """身份守卫清理：仅摘除仍属于本局的状态。

    由引擎任务的 finally 调用；热重载后残留的对局对象
    不会误删新对局的注册信息。
    """
    if _games.get(game.group_id) is game:
        _games.pop(game.group_id, None)
    for uid, gid in list(_user_index.items()):
        if gid == game.group_id:
            _user_index.pop(uid, None)


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
