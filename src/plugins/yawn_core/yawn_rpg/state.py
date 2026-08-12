"""跑团的内存状态管理模块。

注册表结构与身份守卫式清理仿照 yawn_werewolf/state.py：
引擎任务（engine.run_game）独占一局的状态变更，命令处理器
只负责校验并把 Action 投入 action_queue。KP 的每一次工具
调用同样由引擎验证后执行，状态写入不经过 AI。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

from .. import game_registry  # noqa: TID252
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
    JOIN_GAME = "join_game"
    LEAVE_GAME = "leave_game"
    TRANSFER_HOST = "transfer_host"
    SAY = "say"  # 群自由发言（aux=文本）
    CHECK = "check"  # 显式检定（aux="技能key"，可选 value 保留难度）
    TALK_NPC = "talk_npc"  # 与 NPC 交谈（aux="npc_id|发言内容"）
    SHARE_FACT = "share_fact"  # 公开个人 NPC 情报（aux="npc_id|fact_id"）
    ATTACK = "attack"  # 攻击怪物（aux=怪物 id）
    MOVE = "move"  # 前往出口（aux=关键词/场景名）
    WAIT = "wait"  # 原地等待（value=分钟数）
    ASSIST = "assist"  # 协助（aux="目标玩家|技能"）
    SHARE_CLUE = "share_clue"  # 公开本人持有线索（aux=线索名/id）
    PASS_TURN = "pass_turn"
    # ── 建卡期私聊行动 ──
    REROLL = "reroll"  # 整卡重掷
    ADD_SKILL = "add_skill"  # 加点（aux=技能 key，value=点数）
    SUB_SKILL = "sub_skill"  # 减点（aux=技能 key，value=点数）
    RESET_SKILLS = "reset_skills"  # 清空加点
    SHOW_CARD = "show_card"  # 重发角色卡
    CONFIRM_CARD = "confirm_card"  # 锁定角色卡


_RELATION_MIN = -100
_RELATION_MAX = 100
_RELATION_BANDS = (
    (-60, "敌对"),
    (-21, "警惕"),
    (20, "中立"),
    (60, "友善"),
)


@dataclass
class Action:
    """命令处理器提交给引擎的一次玩家行动。"""

    kind: ActionKind
    actor_user_id: int
    value: Optional[int] = None
    aux: Optional[str] = None
    action_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    submitted_at: float = field(default_factory=time.monotonic)
    expected_phase: Optional[Phase] = None
    expected_scene: Optional[str] = None
    result: Optional["asyncio.Future[str]"] = None
    # SAY 由引擎在消费前分类；这些字段不受玩家输入信任，
    # 只作为同一串行引擎内的路由快照。
    route: Optional[str] = None
    target_id: Optional[str] = None
    social_node_id: Optional[str] = None
    social_skill: Optional[str] = None
    emotion: Optional[str] = None
    emotion_confidence: float = 0.0
    # Privilege checked by the command layer and revalidated by the signup engine.
    authority: str = "player"
    # Set when an action leaves the queue; in-flight SAY must not be rewritten.
    in_flight: bool = False


class SubmitResult(str, Enum):
    ACCEPTED = "accepted"
    QUEUE_FULL = "queue_full"
    USER_LIMIT = "user_limit"
    DUPLICATE = "duplicate"
    STALE = "stale"


def relationship_band(value: int) -> str:
    """把内部关系值映射为不暴露裸数值的定性状态。"""
    value = max(_RELATION_MIN, min(_RELATION_MAX, value))
    for upper_bound, band in _RELATION_BANDS:
        if value <= upper_bound:
            return band
    return "信任"


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
    action_queue: asyncio.Queue[Action] = field(
        default_factory=lambda: asyncio.Queue(maxsize=100)
    )
    worker: Optional[asyncio.Task[None]] = None
    # 持久化 RPGGame 行主键
    game_row_id: Optional[int] = None
    # 携带 onebot v11 Bot 实例；用 Any 避免 nonebot 基类与适配器类型冲突
    bot: Any = None
    # ── 局内状态（仅引擎写入）──────────────────────────
    current_scene: Optional[str] = None
    discovered_clues: set[str] = field(default_factory=set)
    # 线索发现后归属发现者；未指定发现者的旧路径直接公开。
    clue_owners: dict[str, set[int]] = field(default_factory=dict)
    public_clues: set[str] = field(default_factory=set)
    # 已触发的 once 检定点 id
    fired_checks: set[str] = field(default_factory=set)
    # 检定成功的检定点 id（grant_clue 据此拒绝覆盖失败检定的线索）
    passed_checks: set[str] = field(default_factory=set)
    # 怪物当前 HP（出场时由引擎按模组初始化）
    monster_hp: dict[str, int] = field(default_factory=dict)
    dead_monsters: set[str] = field(default_factory=set)
    # NPC 当前 HP（在场解析命中时幂等初始化，见 npcs_in_scene）
    npc_hp: dict[str, int] = field(default_factory=dict)
    dead_npcs: set[str] = field(default_factory=set)
    # 对调查员怀有敌意的 NPC（被攻击而未死）
    npc_hostile: set[str] = field(default_factory=set)
    # NPC 社交状态（均由引擎单写；关系值钳制在 -100~100）
    npc_contexts: dict[str, deque[str]] = field(default_factory=dict)
    npc_rapport: dict[str, dict[int, int]] = field(default_factory=dict)
    npc_attitude: dict[str, int] = field(default_factory=dict)
    npc_social_attempts: dict[tuple[str, int, str], int] = field(default_factory=dict)
    # 已发放过的节点奖励；避免重复成功/失败尝试重复增加 flag 或线索。
    npc_social_rewards: set[tuple[str, int, str, bool]] = field(default_factory=set)
    npc_unlocked_facts: dict[tuple[str, int], set[str]] = field(default_factory=dict)
    npc_public_facts: dict[str, set[str]] = field(default_factory=dict)
    npc_focus: dict[int, str] = field(default_factory=dict)
    # 普通情绪微调的探索轮预算；节点结算不受此预算限制。
    npc_emotion_rapport_delta: dict[tuple[str, int], int] = field(default_factory=dict)
    npc_emotion_attitude_delta: dict[str, int] = field(default_factory=dict)
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
    # 本 KP 回合开始时的局面场景块（get_situation 据此去重；仅引擎写入）
    kp_situation_scene_block: str = ""
    # 剧本概览（ai_kp.build_module_overview 整局构建一次，跨回合复用）
    kp_overview: str = ""
    # 已发生的具名事件 id（引擎 check_events 写入；KP 提示词只列名称）
    occurred_events: set[str] = field(default_factory=set)
    # 回合中吸纳：KP 回合内到达的非 SAY 行动（_run_play 循环顶先于
    # pending 与 queue 消费；不复用单槽 pending——那是 SAY 合批窗口
    # 专用，且多条会乱序）
    mid_turn_buffer: deque[Action] = field(default_factory=deque)
    # 探索软轮次：每名可行动调查员每轮一次主要行动。
    explore_round: int = 0
    explore_acted: set[int] = field(default_factory=set)
    explore_deadline: float = 0.0
    # target_user_id, skill_key, scene_id, round_number, helper_user_id
    assists: list[tuple[int, str, str, int, int]] = field(default_factory=list)
    # 进入战斗后的稳定行动顺序；当前版本仅约束玩家行动，敌方仍由既有反击逻辑处理。
    combat_order: list[int] = field(default_factory=list)
    combat_index: int = 0
    combat_round: int = 0
    combat_deadline: float = 0.0
    # 输入层背压与去重账本；动作被引擎取走时释放。
    pending_actions: dict[str, Action] = field(default_factory=dict)
    pending_by_user: dict[int, int] = field(default_factory=dict)
    pending_say_by_user: dict[int, int] = field(default_factory=dict)
    # ── 监听规则缓存（命令层读写）──────────────────────
    # 特性开关判定缓存：(user_id, group_id|None) → (判定, 过期循环时刻)
    feature_ok_cache: dict[tuple[int, Optional[int]], tuple[bool, float]] = field(
        default_factory=dict
    )

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
            clock_start_minutes=self.clock_start_minutes,
            elapsed_minutes=self.elapsed_minutes,
            flags=dict(self.flags),
        )

    def start_explore_round(self, timeout: float) -> None:
        """开始新探索轮，并清除上一轮未消耗的协助。"""
        self.explore_round += 1
        self.explore_acted.clear()
        self.explore_deadline = asyncio.get_running_loop().time() + timeout
        self.assists.clear()
        self.npc_emotion_rapport_delta.clear()
        self.npc_emotion_attitude_delta.clear()

    def active_user_ids(self) -> set[int]:
        return {p.user_id for p in self.players if not p.incapped}

    # ── NPC 社交状态与独立上下文 ───────────────────────────

    def npc_context(self, npc_id: str) -> deque[str]:
        """取得单个 NPC 的公开对话上下文；不同 NPC 永不共用队列。"""
        context = self.npc_contexts.get(npc_id)
        if context is None:
            context = deque(maxlen=12)  # 最近 6 轮玩家/NPC 往返
            self.npc_contexts[npc_id] = context
        return context

    def append_npc_context(self, npc_id: str, line: str) -> None:
        """追加一条公开 NPC 上下文；私人情报不得从这里写入。"""
        text = line.strip()
        if text:
            self.npc_context(npc_id).append(text)

    def npc_rapport_value(self, npc_id: str, user_id: int) -> int:
        """取得个人好感；未初始化时使用模组 NPC 的初始值。"""
        current = self.npc_rapport.get(npc_id, {}).get(user_id)
        if current is not None:
            return max(-100, min(100, current))
        npc = self.module.npc(npc_id) if self.module is not None else None
        return npc.initial_rapport if npc is not None else 0

    def npc_attitude_value(self, npc_id: str) -> int:
        """取得全队公共态度；未初始化时使用模组 NPC 的初始值。"""
        current = self.npc_attitude.get(npc_id)
        if current is not None:
            return max(-100, min(100, current))
        npc = self.module.npc(npc_id) if self.module is not None else None
        return npc.initial_attitude if npc is not None else 0

    def npc_rapport_band(self, npc_id: str, user_id: int) -> str:
        """个人好感的玩家可见分段。"""
        return relationship_band(self.npc_rapport_value(npc_id, user_id))

    def npc_attitude_band(self, npc_id: str) -> str:
        """公共态度的玩家可见分段。"""
        return relationship_band(self.npc_attitude_value(npc_id))

    # ── NPC 在场解析（死亡过滤 + HP 幂等初始化的包装层）──────

    def npcs_in_scene(
        self,
        scene_id: str,
        ctx: Optional[ConditionContext] = None,
    ) -> list[tuple["NPC", str]]:
        """当前时刻存活于给定场景的 NPC（按模组声明序）。

        每个元素为 (NPC, activity)。模组层解析器不感知死亡，
        过滤在此处统一进行；命中者顺手初始化 npc_hp（幂等，
        仿 enter_scene 对怪物 HP 的处理）。调用方已持有条件
        快照时可经 ctx 传入复用，避免逐次重建。
        """
        if self.module is None:
            return []
        found: list[tuple[NPC, str]] = []
        for npc, activity in self.module.npcs_in_scene(
            scene_id,
            self.time_of_day,
            ctx if ctx is not None else self.condition_context(),
        ):
            if npc.id in self.dead_npcs:
                continue
            self.npc_hp.setdefault(npc.id, npc.hp)
            found.append((npc, activity))
        return found

    def npc_present(
        self,
        npc_id: str,
        ctx: Optional[ConditionContext] = None,
    ) -> Optional[tuple["NPC", str]]:
        """NPC 当前时刻是否存活于当前场景；是则返回 (NPC, activity)。"""
        if self.current_scene is None:
            return None
        for npc, activity in self.npcs_in_scene(self.current_scene, ctx):
            if npc.id == npc_id:
                return npc, activity
        return None

    def stow_actions(self) -> None:
        """非阻塞收存队列与 pending 到 mid_turn_buffer（场景切换用）。

        不再丢弃：一切确定性命令都会对当前场景状态做校验（过期的
        /攻击、/前往 会拿到中文回执），收存后由 _run_play 于旁白
        之后按原序执行；中途已被 run_kp_turn 吸收的 SAY 早已进入
        KP 对话与群聊记录，不受影响。pending 更早，先入。
        """
        if self.pending is not None:
            self.mid_turn_buffer.append(self.pending)
            self.pending = None
        while not self.action_queue.empty():
            try:
                action = self.action_queue.get_nowait()
                action.in_flight = True
                self.mid_turn_buffer.append(action)
                self.action_queue.task_done()
            except asyncio.QueueEmpty:  # noqa: PERF203
                break

    def release_unprocessed_actions(self) -> None:
        """终局清理仍在缓冲区/队列中的动作，避免玩家配额泄漏。"""
        actions: list[Action] = []
        if self.pending is not None:
            actions.append(self.pending)
            self.pending = None
        actions.extend(self.mid_turn_buffer)
        self.mid_turn_buffer.clear()
        while True:
            try:
                action = self.action_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self.action_queue.task_done()
            actions.append(action)
        actions.extend(self.pending_actions.values())
        seen: set[str] = set()
        for action in actions:
            if action.action_id in seen:
                continue
            seen.add(action.action_id)
            release_action(self, action, result="ended")


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
    if not game_registry.reserve_game("rpg", group_id, host_user_id):
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


def submit_action(
    game: Game,
    action: Action,
    *,
    queue_max: int,
    user_pending_max: int,
    user_say_pending_max: int,
) -> SubmitResult:
    """唯一入队入口：执行轻量背压/去重，状态裁决仍留给引擎。"""
    if action.expected_phase is not None and action.expected_phase is not game.phase:
        return SubmitResult.STALE
    if action.action_id in game.pending_actions:
        return SubmitResult.DUPLICATE
    is_say = action.kind is ActionKind.SAY
    per_user = game.pending_say_by_user if is_say else game.pending_by_user
    limit = user_say_pending_max if is_say else user_pending_max
    pending_says = (
        [
            candidate
            for candidate in game.pending_actions.values()
            if candidate.actor_user_id == action.actor_user_id
            and candidate.kind is ActionKind.SAY
            and not candidate.in_flight
        ]
        if is_say
        else []
    )
    pending_count = (
        len(pending_says) if is_say else per_user.get(action.actor_user_id, 0)
    )
    if pending_count >= limit and is_say and pending_says:
        # SAY 不丢弃：将新内容并入同一玩家尚未结算的最后一条发言。
        last = max(pending_says, key=lambda candidate: candidate.submitted_at)
        last.aux = "\n".join(part for part in (last.aux, action.aux) if part)
        return SubmitResult.ACCEPTED
    if pending_count >= limit:
        return SubmitResult.USER_LIMIT
    if game.action_queue.qsize() >= min(queue_max, game.action_queue.maxsize):
        return SubmitResult.QUEUE_FULL
    game.pending_actions[action.action_id] = action
    per_user[action.actor_user_id] = per_user.get(action.actor_user_id, 0) + 1
    game.action_queue.put_nowait(action)
    return SubmitResult.ACCEPTED


def release_action(game: Game, action: Action, result: str = "done") -> None:
    """引擎消费动作后释放背压账本，并完成可选结果 Future。"""
    if game.pending_actions.pop(action.action_id, None) is None:
        return
    is_say = action.kind is ActionKind.SAY
    per_user = game.pending_say_by_user if is_say else game.pending_by_user
    count = per_user.get(action.actor_user_id, 0) - 1
    if count > 0:
        per_user[action.actor_user_id] = count
    else:
        per_user.pop(action.actor_user_id, None)
    if action.result is not None and not action.result.done():
        action.result.set_result(result)


def join_signup(game: Game, user_id: int) -> bool:
    """报名；已在任意局中或已报名返回 False。"""
    if user_id in _user_index or user_id in game.signup_user_ids:
        return False
    if not game_registry.reserve_user("rpg", game.group_id, user_id):
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
    game_registry.release_user("rpg", game.group_id, user_id)
    return True


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
    game_registry.release_game("rpg", game.group_id)


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
    # A task cancelled before its first scheduling point never enters
    # run_game(), so its finally block cannot release the registries.
    if _games.get(game.group_id) is game:
        discard_game(game)
