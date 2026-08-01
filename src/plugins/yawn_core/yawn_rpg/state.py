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
    from .module_schema import NPC, ModuleDef


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
    WAIT = "wait"  # 原地等待（value=分钟数）
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
    # NPC 当前 HP（在场解析命中时幂等初始化，见 npcs_in_scene）
    npc_hp: dict[str, int] = field(default_factory=dict)
    dead_npcs: set[str] = field(default_factory=set)
    # 对调查员怀有敌意的 NPC（被攻击而未死）
    npc_hostile: set[str] = field(default_factory=set)
    # ── 游戏内时钟与事件标记（仅引擎写入）────────────────
    # 开局游戏内起始时刻（自 0:00 起的分钟数；引擎按 module.time.start 初始化）
    clock_start_minutes: int = 0
    # 自开局已流逝的游戏内分钟数（每个行动 tick 推进）
    elapsed_minutes: int = 0
    # 事件标记（名称 → 累计次数；供条件词条 flag: 与通用结局升级使用）
    flags: dict[str, int] = field(default_factory=dict)
    # 群聊记录（KP 上下文与播报存档）
    group_log: deque[str] = field(default_factory=lambda: deque(maxlen=200))
    # ── KP 智能体循环状态 ──
    # 单槽缓冲：SAY 合批时暂存第一个非 SAY 行动
    pending: Optional[Action] = None
    # 端点不支持 tool_call 时降级为纯叙述模式（本局内不再重试工具）
    tools_broken: bool = False
    # 最近一次 KP 调用的事件循环时刻（防刷屏限速）
    last_kp_at: float = 0.0
    # 工具 schema 缓存（schema 全静态，整局只构建一次，见 ai_kp.build_tools）
    tools_cache: Optional[Any] = None

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

    # ── 游戏内时钟 ────────────────────────────────────

    @property
    def clock_minutes(self) -> int:
        """自第 1 天 0:00 起的绝对分钟数（起始时刻 + 已流逝）。"""
        return self.clock_start_minutes + self.elapsed_minutes

    @property
    def time_of_day(self) -> int:
        """游戏内时钟（自当日 0:00 起的分钟数，0-1439）。"""
        return self.clock_minutes % 1440

    @property
    def day_number(self) -> int:
        """游戏内天数（1 起，跨午夜递增）。"""
        return 1 + self.clock_minutes // 1440

    def clock_text(self) -> str:
        """时钟文案，如「第 1 天 21:35」（命令层 / 引擎 / 提示词共用）。"""
        tod = self.time_of_day
        return f"第 {self.day_number} 天 {tod // 60:02d}:{tod % 60:02d}"

    def condition_context(self) -> ConditionContext:
        """组装条件求值所需的事实快照（供出口/结局判定）。"""
        return ConditionContext(
            clues=set(self.discovered_clues),
            dead_monsters=set(self.dead_monsters),
            current_scene=self.current_scene or "",
            all_incapped=self.all_incapped(),
            time_of_day=self.time_of_day,
            flags=dict(self.flags),
        )

    # ── NPC 在场解析（死亡过滤 + HP 幂等初始化的包装层）──────

    def npcs_in_scene(self, scene_id: str) -> list[tuple["NPC", str]]:
        """当前时刻存活于给定场景的 NPC（按模组声明序）。

        每个元素为 (NPC, activity)。模组层解析器不感知死亡，
        过滤在此处统一进行；命中者顺手初始化 npc_hp（幂等，
        仿 enter_scene 对怪物 HP 的处理）。
        """
        if self.module is None:
            return []
        found: list[tuple[NPC, str]] = []
        for npc, activity in self.module.npcs_in_scene(
            scene_id,
            self.time_of_day,
            self.condition_context(),
        ):
            if npc.id in self.dead_npcs:
                continue
            self.npc_hp.setdefault(npc.id, npc.hp)
            found.append((npc, activity))
        return found

    def npc_present(self, npc_id: str) -> Optional[tuple["NPC", str]]:
        """NPC 当前时刻是否存活于当前场景；是则返回 (NPC, activity)。"""
        if self.current_scene is None:
            return None
        for npc, activity in self.npcs_in_scene(self.current_scene):
            if npc.id == npc_id:
                return npc, activity
        return None

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
