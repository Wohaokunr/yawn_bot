# ruff: noqa: C901, E501, PLR0911, PLR0912, PLR0913, PLR0915, PLR0917, PLR2004, RET505
"""跑团游戏引擎：每局一个 asyncio 任务。

引擎独占所有状态变更与群播报；命令处理器只做校验，
并把 Action 投入 game.action_queue。KP 通过 tool_call
发起的一切请求（检定 / 切景 / NPC 对白 / 伤害……）都由
引擎的工具执行器全量验证后执行——AI 从不直接碰状态。
所有可能卡住的 await 均包 asyncio.wait_for 超时，超时即
确定性兜底，绝不卡局。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional, Union

from nonebot import get_bot, get_plugin_config, logger
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot_plugin_orm import get_session
from sqlalchemy import select

from ..llm import complete, complete_with_tools  # noqa: TID252
from . import ai_kp, ai_npc, ai_social, api
from .charsheet import (
    CharacterSheet,
    damage_bonus,
    random_char_name,
    render_card,
    reroll_sheet,
    resolve_skill,
    roll_attributes,
    skill_pool_size,
    validate_adjustment,
)
from .config import Config
from .dice import (
    CheckTier,
    is_valid_dice_expr,
    roll_dice,
    roll_san_loss,
    skill_check,
)
from .models import RPGGame, RPGPlayer
from .module_schema import (
    NPC,
    CheckDifficulty,
    CheckMode,
    Clue,
    Ending,
    SocialNode,
    SocialStrategy,
    evaluate_condition,
    list_modules,
    load_modules,
)
from .state import (
    Action,
    ActionKind,
    Game,
    Phase,
    PlayerState,
    discard_game,
    join_signup,
    leave_signup,
    release_action,
)

if TYPE_CHECKING:
    from openai.types.chat import (
        ChatCompletionMessageParam,
        ChatCompletionMessageToolCallUnion,
    )

    from .dice import CheckResult
    from .module_schema import CheckPoint, Exit, ModuleDef, Monster

config = get_plugin_config(Config)

# 插件加载时扫描模组目录（坏模组 warning 跳过）
load_modules()

_BJ_TZ = timezone(timedelta(hours=8))

# 检定等级排序（对抗检定比较用）
_TIER_RANK: dict[CheckTier, int] = {
    CheckTier.CRITICAL: 5,
    CheckTier.EXTREME: 4,
    CheckTier.HARD: 3,
    CheckTier.REGULAR: 2,
    CheckTier.FAILURE: 1,
    CheckTier.FUMBLE: 0,
}

_GENERIC_IDLE = "（四周一片沉寂，只有你们自己的呼吸声。）"

# 各行动类型默认消耗的游戏内分钟数（模组 time.costs 可按键覆写）。
# 只有成功路径消耗时间：被拒绝的移动、落空的攻击目标不 tick。
_DEFAULT_TIME_COSTS: dict[str, int] = {
    "say": 5,
    "talk": 10,
    "check": 10,
    "move": 10,
    "attack": 5,
    "wait": 0,
}

# ── 违规行为检测（发言关键词扫描）────────────────────────
# 紧的多字词组降低误报（单字"杀/烧"误报率高）；命中只记 flag，
# 是否结局由通用结局阈值决定，单次命中仅偏置 KP 反应指令。
_ARSON_KW = ("放火", "烧了", "点火", "汽油", "烧死", "一把火")
_THREAT_KW = ("恐吓", "威胁", "杀了", "弄死")
_DESTROY_KW = ("砸了", "砸烂", "烧毁", "拆了")

# 类别 → 面向 KP 的中文描述
_OFFENSE_LABELS = {
    "arson": "纵火",
    "threat": "暴力威胁",
    "destroy": "打砸破坏",
}


def _scan_offense(text: str) -> Optional[str]:
    """扫描发言中的违规行为关键词；命中返回类别，否则 None。"""
    lowered = text.casefold()
    if any(kw in lowered for kw in _ARSON_KW):
        return "arson"
    if any(kw in lowered for kw in _THREAT_KW):
        return "threat"
    if any(kw in lowered for kw in _DESTROY_KW):
        return "destroy"
    return None


_NPC_ROUTE_NAMES = frozenset({"npc_talk", "social_action"})
_ROUTER_CONFIDENCE_MIN = 0.70


def _current_npc(game: Game, npc_id: str) -> Optional[tuple[NPC, str]]:
    """只从当前场景的存活 NPC 中解析目标。"""
    if game.current_scene is None:
        return None
    return next(
        ((npc, activity) for npc, activity in game.npcs_in_scene(game.current_scene) if npc.id == npc_id),
        None,
    )


def _deterministic_npc_target(
    game: Game,
    user_id: int,
    text: str,
) -> Optional[str]:
    """AI 不可用时按 NPC 名称/id/当前焦点兜底解析。"""
    if game.current_scene is None:
        return None
    present = game.npcs_in_scene(game.current_scene)
    lowered = text.casefold()
    explicit = [
        npc
        for npc, _ in present
        if npc.id.casefold() in lowered or npc.name.casefold() in lowered
    ]
    if explicit:
        explicit.sort(key=lambda npc: max(len(npc.id), len(npc.name)), reverse=True)
        return explicit[0].id
    focus = game.npc_focus.get(user_id)
    if focus and any(npc.id == focus for npc, _ in present):
        return focus
    return None


def _infer_social_skill(text: str) -> Optional[str]:
    """确定性模式下识别最明确的社交策略。"""
    lowered = text.casefold()
    if any(token in lowered for token in ("恐吓", "威胁", "吓唬", "intimidate")):
        return "intimidate"
    if any(token in lowered for token in ("话术", "忽悠", "欺骗", "骗", "fast_talk")):
        return "fast_talk"
    if any(token in lowered for token in ("说服", "劝", "请求", "persuade")):
        return "persuade"
    return None


def _infer_emotion(text: str) -> Optional[str]:
    """AI 不可用时识别少量明确情绪词，避免普通关系微调完全失效。"""
    lowered = text.casefold()
    if any(token in lowered for token in ("对不起", "抱歉", "道歉", "sorry")):
        return "apology"
    if any(token in lowered for token in ("理解你", "谢谢", "感谢", "辛苦", "thank")):
        return "empathetic"
    if any(token in lowered for token in ("威胁", "恐吓", "不然", "否则", "pressuring")):
        return "pressuring"
    if any(token in lowered for token in ("滚", "蠢", "废物", "侮辱", "insult")):
        return "insulting"
    if any(token in lowered for token in ("撒谎", "骗你", "骗人", "lie", "lying")):
        return "lying"
    return None


async def _classify_say(
    game: Game,
    cfg: Config,
    action: Action,
) -> None:
    """给一条 SAY 写入受引擎信任边界保护的路由结果。"""
    if action.route is not None:
        return
    text = (action.aux or "").strip()
    route = None
    if cfg.rpg_ai_enabled and text:
        route = await ai_social.classify_message(
            game,
            cfg,
            action.actor_user_id,
            text,
        )
    if route is not None:
        if route.confidence < _ROUTER_CONFIDENCE_MIN:
            action.route = "kp_say"
            return
        if route.route == "kp_say":
            action.route = "kp_say"
            return
        if route.npc_id is None or _current_npc(game, route.npc_id) is None:
            action.route = "kp_say"
            return
        action.target_id = route.npc_id
        action.emotion = route.emotion
        action.emotion_confidence = route.emotion_confidence
        if route.route == "social_action":
            npc = game.module.npc(route.npc_id) if game.module is not None else None
            node = (
                next(
                    (item for item in npc.social_nodes if item.id == route.node_id),
                    None,
                )
                if npc is not None and route.node_id
                else None
            )
            if node is not None and route.skill and node.strategy(route.skill) is not None:
                action.route = "social_action"
                action.social_node_id = node.id
                action.social_skill = route.skill
                return
            # 目标 NPC 合法但社交节点不合法：保留自然对话，不执行越界效果。
            action.route = "npc_talk"
            return
        action.route = "npc_talk"
        return

    # AI 关闭或调用失败时，不凭空创造社交效果；仅做名称/焦点兜底。
    target_id = _deterministic_npc_target(game, action.actor_user_id, text)
    if target_id is None:
        action.route = "kp_say"
        return
    action.route = "npc_talk"
    action.target_id = target_id
    action.emotion = _infer_emotion(text)
    action.emotion_confidence = 1.0 if action.emotion is not None else 0.0
    skill = _infer_social_skill(text)
    if skill and game.module is not None:
        npc = game.module.npc(target_id)
        if npc is not None:
            node = next(
                (item for item in npc.social_nodes if item.strategy(skill) is not None),
                None,
            )
            if node is not None:
                action.route = "social_action"
                action.social_node_id = node.id
                action.social_skill = skill


def _apply_relation_delta(
    game: Game,
    npc: NPC,
    user_id: int,
    rapport_delta: int,
    attitude_delta: int,
) -> tuple[str, str, bool]:
    """应用一次关系变化，返回个人/公共新分段及公共分段是否变化。"""
    rapport_map = game.npc_rapport.setdefault(npc.id, {})
    rapport = game.npc_rapport_value(npc.id, user_id)
    attitude = game.npc_attitude_value(npc.id)
    old_attitude = attitude
    rapport_map[user_id] = max(-100, min(100, rapport + rapport_delta))
    game.npc_attitude[npc.id] = max(-100, min(100, attitude + attitude_delta))
    return (
        game.npc_rapport_band(npc.id, user_id),
        game.npc_attitude_band(npc.id),
        old_attitude != game.npc_attitude_value(npc.id),
    )


def _capped_delta(current: int, requested: int, cap: int) -> int:
    """把普通情绪变化限制在本轮 [-cap, cap] 的累计预算内。"""
    if requested > 0:
        return min(requested, max(0, cap - current))
    if requested < 0:
        return max(requested, min(0, -cap - current))
    return 0


async def _apply_emotion(
    game: Game,
    cfg: Config,
    npc: NPC,
    user_id: int,
    emotion: Optional[str],
    confidence: float,
) -> None:
    """应用普通对话的可审计、小幅情绪变化。"""
    if confidence < cfg.rpg_social_emotion_min_confidence:
        return
    if emotion in {"friendly", "empathetic", "apology"}:
        requested_rapport = cfg.rpg_social_positive_rapport_delta
        requested_attitude = cfg.rpg_social_positive_attitude_delta
    elif emotion in {"insulting", "lying", "pressuring"}:
        requested_rapport = cfg.rpg_social_negative_rapport_delta
        requested_attitude = cfg.rpg_social_negative_attitude_delta
    else:
        return
    rapport_key = (npc.id, user_id)
    previous_rapport = game.npc_emotion_rapport_delta.get(rapport_key, 0)
    previous_attitude = game.npc_emotion_attitude_delta.get(npc.id, 0)
    rapport_delta = _capped_delta(
        previous_rapport,
        requested_rapport,
        max(cfg.rpg_social_rapport_round_cap, 0),
    )
    attitude_delta = _capped_delta(
        previous_attitude,
        requested_attitude,
        max(cfg.rpg_social_attitude_round_cap, 0),
    )
    if rapport_delta == 0 and attitude_delta == 0:
        return
    game.npc_emotion_rapport_delta[rapport_key] = previous_rapport + rapport_delta
    game.npc_emotion_attitude_delta[npc.id] = previous_attitude + attitude_delta
    _, attitude_band, public_changed = _apply_relation_delta(
        game,
        npc,
        user_id,
        rapport_delta,
        attitude_delta,
    )
    if public_changed:
        await _announce(game, f"{npc.name} 对调查员们的态度变为：{attitude_band}。")


def _now_bj() -> datetime:
    """返回当前北京时间（naive），与项目时间约定一致。"""
    return datetime.now(_BJ_TZ).replace(tzinfo=None)


def _loop_time() -> float:
    """事件循环时钟，用于阶段截止时间计算。"""
    return asyncio.get_running_loop().time()


# ── 基础辅助 ──────────────────────────────────────────────


@dataclass
class _Timer:
    """阶段计时器：截止时间 + 剩余提醒。"""

    deadline: float
    warn_remain: Optional[float] = None
    warn_text: str = ""
    warned: bool = False

    def remaining(self) -> float:
        """距离截止的剩余秒数。"""
        return self.deadline - _loop_time()

    async def next_action(self, game: Game) -> Optional[Action]:
        """等待至多一个行动；到提醒点时群播报一次；超时返回 None。"""
        left = self.remaining()
        if left <= 0:
            return None
        if (
            self.warn_remain is not None
            and not self.warned
            and left <= self.warn_remain
        ):
            self.warned = True
            await _announce(game, self.warn_text)
        step = left
        if self.warn_remain is not None and not self.warned:
            step = min(left, max(left - self.warn_remain, 0.5))
        return await _get_action(game, step)


async def _get_action(game: Game, step: float) -> Optional[Action]:
    """等待至多 step 秒获取一个行动；超时返回 None。"""
    try:
        action = await asyncio.wait_for(
            game.action_queue.get(),
            timeout=step,
        )
    except asyncio.TimeoutError:
        return None
    game.action_queue.task_done()
    action.in_flight = True
    return action


def _enter_phase(game: Game, phase: Phase) -> None:
    """切换阶段并记日志；同阶段重复赋值不重复记录。"""
    if game.phase is phase:
        return
    logger.info(f"跑团群 {game.group_id} 进入阶段 {phase.value}")
    game.phase = phase


async def _announce(game: Game, text: Union[str, "Message"]) -> None:
    """群播报并记入群聊记录（KP 上下文来源）。"""
    # 入群聊记录前提纯文本：Message 里的 @ 等段否则会化作
    # [CQ:...] 标记泄漏进 KP 局面提示词
    plain = text.extract_plain_text() if isinstance(text, Message) else text
    game.group_log.append(f"〔系统〕{plain}")
    if game.bot is None:
        return
    await api.safe_group_msg(game.bot, game.group_id, text)


async def _announce_ephemeral(game: Game, text: str) -> None:
    """发送不进入剧情上下文的界面提示。"""
    if game.bot is not None:
        await api.safe_group_msg(game.bot, game.group_id, text)


async def _dm(
    game: Game,
    player: PlayerState,
    text: str,
    *,
    char_create: bool = False,
    announce_failure: bool = True,
) -> bool:
    """私聊玩家，且绝不把正文回退到群内。

    建卡失败与局内私人内容失败使用不同提示；调用方已经准备了更具体的
    群内反馈时可关闭 ``announce_failure``，但返回值仍明确表示投递结果。
    """
    ok = game.bot is not None and await api.send_dm(game.bot, player.user_id, text)
    first_failure = not ok and player.dm_ok
    if not ok:
        player.dm_ok = False
    if first_failure and announce_failure:
        suffix = (
            "建卡将为你自动确认。"
            if char_create
            else "私人内容不会改发到群内。"
        )
        await _announce(
            game,
            MessageSegment.at(player.user_id)
            + f" 你的私聊发送失败：请加机器人为好友。{suffix}",
        )
    return ok


async def send_private_text(game: Game, player: PlayerState, text: str) -> bool:
    """供只读玩家界面使用的私聊投递；失败提示由命令响应统一给出。"""
    return await _dm(game, player, text, announce_failure=False)


# ── 报名阶段 ──────────────────────────────────────────────


def module_list_text() -> str:
    """渲染模组列表面板。"""
    modules = list_modules()
    if not modules:
        return "当前没有可用的剧本模组~"
    lines = ["═══ 跑团 · 可选模组 ═══"]
    for idx, m in enumerate(modules, start=1):
        lines.append(
            f"{idx}. {m.name}（{m.difficulty}，{m.min_players}-{m.max_players} 人）"
        )
        if m.description:
            lines.append(f"   {m.description}")
    lines.append("──────────────")
    lines.append("房主发送 /选择模组 N 选定剧本")
    return "\n".join(lines)


def signup_cap(module: Optional["ModuleDef"], cfg: Config) -> int:
    """当前模组与全局配置共同允许的报名上限。"""
    return min(cfg.rpg_max_players, module.max_players) if module else cfg.rpg_max_players


def find_module(text: str) -> Optional["ModuleDef"]:
    """按序号、id 或完整名称查找模组。"""
    modules = list_modules()
    value = text.strip()
    if value.isascii() and value.isdigit():
        index = int(value)
        return modules[index - 1] if 1 <= index <= len(modules) else None
    return next((module for module in modules if value in (module.id, module.name)), None)


def module_selection_error(
    game: Game,
    module: "ModuleDef",
    cfg: Config,
) -> Optional[str]:
    """选择模组前验证当前报名人数不会超过新上限。"""
    headcount = len(game.signup_user_ids)
    cap = signup_cap(module, cfg)
    if headcount <= cap:
        return None
    return (
        f"当前已有 {headcount} 人报名，但《{module.name}》最多允许 {cap} 人。"
        "请先让多出的玩家 /退报名，或选择人数上限更高的模组。"
    )


def signup_start_error(game: Game, cfg: Config) -> Optional[str]:
    """验证当前报名状态能否进入建卡；返回可执行的玩家提示。"""
    module = game.module
    if module is None:
        modules = list_modules()
        if not modules:
            return "当前没有可用的剧本模组，暂时无法开始游戏。"
        module = modules[0]
    headcount = len(game.signup_user_ids)
    minimum = max(cfg.rpg_min_players, module.min_players)
    cap = signup_cap(module, cfg)
    if headcount < minimum:
        missing = minimum - headcount
        return (
            f"当前 {headcount}/{minimum} 人，还差 {missing} 位调查员。"
            "报名继续开放，可请其他玩家发送 /报名。"
        )
    if headcount > cap:
        return (
            f"当前 {headcount} 人超过《{module.name}》上限 {cap} 人。"
            "请先 /退报名 或重新 /选择模组。"
        )
    return None


def _phase_remaining_text(game: Game) -> Optional[str]:
    if game.phase_deadline <= 0:
        return None
    seconds = max(0, math.ceil(game.phase_deadline - _loop_time()))
    return f"阶段剩余：约 {seconds} 秒"


def _player_name(player: PlayerState) -> str:
    return player.sheet.name if player.sheet is not None else f"调查员 {player.seat}"


def _read_only_npcs_in_scene(
    game: Game,
    scene_id: str,
) -> list[tuple[NPC, str]]:
    """只读解析在场 NPC，供命令渲染使用，不初始化战斗 HP。"""
    if game.module is None:
        return []
    ctx = game.condition_context()
    return [
        (npc, activity)
        for npc, activity in game.module.npcs_in_scene(
            scene_id,
            game.time_of_day,
            ctx,
        )
        if npc.id not in game.dead_npcs
    ]


def _public_clues(game: Game) -> list[tuple[str, str]]:
    module = game.module
    if module is None:
        return []
    return [
        (clue.id, clue.name)
        for clue in module.clues
        if clue.id in game.public_clues
    ]


def public_clue_list_text(game: Game) -> str:
    """只列出已公开线索名称，适合直接发到群内。"""
    names = [name for _, name in _public_clues(game)]
    return "公共线索：" + ("、".join(names) if names else "暂无")


def _waiting_names(game: Game) -> list[str]:
    return [
        _player_name(player)
        for player in game.active_players()
        if player.user_id not in game.explore_acted
    ]


def _explore_prompt_text(game: Game) -> str:
    waiting = _waiting_names(game)
    return (
        f"〔探索第 {game.explore_round} 轮〕"
        "每位调查员可进行一次主要行动；"
        "待行动："
        + ("、".join(waiting) if waiting else "正在结算")
        + "。可直接用自然语言行动，也可 /协助 或 /跳过。"
    )


def public_situation_text(game: Game) -> str:
    """渲染可安全发送到群内的三阶段局面摘要。"""
    module_name = game.module.name if game.module is not None else "未选择"
    if game.phase is Phase.SIGNUP:
        cap = signup_cap(game.module, config)
        lines = [
            "═══ 跑团 · 当前局面 ═══",
            "阶段：报名",
            f"模组：{module_name}",
            f"报名：{len(game.signup_user_ids)}/{cap} 人",
        ]
        roster = [
            f"{uid}{'（房主）' if uid == game.host_user_id else ''}"
            for uid in game.signup_user_ids
        ]
        lines.append("调查员：" + ("、".join(roster) if roster else "暂无"))
        if remaining := _phase_remaining_text(game):
            lines.append(remaining)
        lines.append("下一步：/报名；房主可 /选择模组 或 /开始游戏")
        return "\n".join(lines)
    if game.phase is Phase.CHAR_CREATE:
        confirmed = [player for player in game.players if player.confirmed]
        pending = [_player_name(player) for player in game.players if not player.confirmed]
        lines = [
            "═══ 跑团 · 当前局面 ═══",
            "阶段：建卡",
            f"模组：{module_name}",
            f"确认进度：{len(confirmed)}/{len(game.players)}",
            "待确认：" + ("、".join(pending) if pending else "无"),
        ]
        if remaining := _phase_remaining_text(game):
            lines.append(remaining)
        lines.append("请在私聊中调整并发送“确认”锁定角色卡。")
        return "\n".join(lines)
    if game.phase is not Phase.PLAY or game.module is None:
        return "跑团已结束。"
    scene = game.module.scene(game.current_scene or "")
    scene_name = scene.name if scene is not None else "未知场景"
    investigators = [
        _player_name(player) + ("（倒下）" if player.incapped else "")
        for player in game.players
    ]
    lines = [
        "═══ 跑团 · 当前局面 ═══",
        "阶段：游戏中",
        f"模组：《{module_name}》",
        f"场景：{scene_name}",
        f"时钟：{format_clock(game)}",
        "调查员：" + ("、".join(investigators) if investigators else "暂无"),
    ]
    present_npcs = [
        npc.name for npc, _ in _read_only_npcs_in_scene(game, game.current_scene or "")
    ]
    if present_npcs:
        lines.append("在场 NPC：" + "、".join(present_npcs))
    if scene is not None:
        monsters = [
            monster.name
            for monster_id in scene.monsters
            if monster_id not in game.dead_monsters
            and (monster := game.module.monster(monster_id)) is not None
        ]
        if monsters:
            lines.append("威胁：" + "、".join(monsters))
        exits: list[str] = []
        for exit_ in scene.exits:
            target = game.module.scene(exit_.to_scene)
            target_name = target.name if target is not None else exit_.to_scene
            passable = evaluate_condition(exit_.condition, game.condition_context())
            exits.append(f"{target_name}（{'可通行' if passable else '受阻'}）")
        lines.append("出口：" + ("、".join(exits) if exits else "无"))
    lines.append(public_clue_list_text(game))
    if game.combat_order:
        current = game.player_by_user(game.combat_order[game.combat_index])
        order = [
            _player_name(player)
            for uid in game.combat_order
            if (player := game.player_by_user(uid)) is not None
        ]
        lines.extend(
            [
                f"战斗：第 {game.combat_round} 轮",
                "顺序：" + " → ".join(order),
                f"当前：{_player_name(current) if current is not None else '等待结算'}",
                "允许操作：当前行动者可 /攻击 或 /跳过",
            ]
        )
    else:
        waiting = _waiting_names(game)
        lines.extend(
            [
                f"探索：第 {game.explore_round} 轮",
                "待行动：" + ("、".join(waiting) if waiting else "正在结算"),
            ]
        )
    return "\n".join(lines)


def _known_npc_ids(game: Game, player: PlayerState) -> set[str]:
    known = set(game.npc_contexts)
    known.update(game.npc_public_facts)
    known.update(
        npc_id
        for npc_id, user_id in game.npc_unlocked_facts
        if user_id == player.user_id
    )
    focus = game.npc_focus.get(player.user_id)
    if focus:
        known.add(focus)
    known.update(
        npc.id
        for npc, _ in _read_only_npcs_in_scene(game, game.current_scene or "")
    )
    return known


def private_situation_text(game: Game, player: PlayerState) -> str:
    """渲染仅可私聊给请求者的个人局面摘要。"""
    lines = [f"═══ {_player_name(player)} · 私人局面 ═══"]
    if player.sheet is not None:
        lines.append(
            f"HP {player.hp}/{player.sheet.max_hp}  SAN {player.san}/{player.sheet.max_san}"
        )
    if game.phase is Phase.CHAR_CREATE:
        lines.append("角色卡：" + ("已确认" if player.confirmed else "尚未确认"))
        return "\n".join(lines)
    if game.phase is not Phase.PLAY or game.module is None:
        return "\n".join(lines)
    if player.incapped:
        qualification = "已失去行动能力"
    elif game.combat_order:
        qualification = (
            "现在轮到你，可 /攻击 或 /跳过"
            if game.combat_order[game.combat_index] == player.user_id
            else "等待当前行动者结算"
        )
    else:
        qualification = (
            "本轮主要行动已使用"
            if player.user_id in game.explore_acted
            else "本轮可进行一次主要行动"
        )
    lines.append(f"行动资格：{qualification}")
    private_clues = [
        clue
        for clue in game.module.clues
        if clue.id in game.discovered_clues
        and clue.id not in game.public_clues
        and player.user_id in game.clue_owners.get(clue.id, set())
    ]
    lines.append("个人线索：")
    if private_clues:
        lines.extend(f"- {clue.name}：{clue.text}" for clue in private_clues)
    else:
        lines.append("- 暂无")
    private_facts: list[str] = []
    for npc in game.module.npcs:
        owned = game.npc_unlocked_facts.get((npc.id, player.user_id), set())
        public = game.npc_public_facts.get(npc.id, set())
        private_facts.extend(
            f"- {npc.name}·{fact.name}：{fact.text}"
            for fact in npc.facts
            if fact.id in owned and fact.id not in public
        )
    lines.append("NPC 私人情报：")
    lines.extend(private_facts or ["- 暂无"])
    relations = [
        f"- {npc.name}：个人关系 {game.npc_rapport_band(npc.id, player.user_id)}；"
        f"公开态度 {game.npc_attitude_band(npc.id)}"
        for npc in game.module.npcs
        if npc.id in _known_npc_ids(game, player)
    ]
    lines.append("个人关系：")
    lines.extend(relations or ["- 暂无已接触 NPC"])
    return "\n".join(lines)


def private_journal_text(game: Game, player: PlayerState) -> str:
    """渲染请求者可见的完整调查手记正文。"""
    lines = [f"═══ {_player_name(player)} · 调查手记 ═══"]
    if game.module is None:
        return "\n".join([*lines, "暂无记录。"])
    entries = 0
    for clue in game.module.clues:
        scope = None
        if clue.id in game.public_clues:
            scope = "公共线索"
        elif (
            clue.id in game.discovered_clues
            and player.user_id in game.clue_owners.get(clue.id, set())
        ):
            scope = "个人线索"
        if scope is not None:
            lines.append(f"〔{scope}〕{clue.name}\n{clue.text}")
            entries += 1
    for npc in game.module.npcs:
        owned = game.npc_unlocked_facts.get((npc.id, player.user_id), set())
        for fact in npc.facts:
            if fact.id in owned:
                lines.append(f"〔NPC 情报〕{npc.name}·{fact.name}\n{fact.text}")
                entries += 1
    if entries == 0:
        lines.append("暂无可见的线索或 NPC 情报。")
    return "\n\n".join(lines)


def lookup_visible_clue(
    game: Game,
    player: Optional[PlayerState],
    needle: str,
) -> tuple[Optional[Clue], Optional[str]]:
    """查找请求者可见线索，返回 (Clue, public/private/ambiguous)。"""
    if game.module is None:
        return None, None
    visible: list[tuple[Clue, str]] = []
    for clue in game.module.clues:
        if clue.id in game.public_clues:
            visible.append((clue, "public"))
        elif (
            player is not None
            and clue.id in game.discovered_clues
            and player.user_id in game.clue_owners.get(clue.id, set())
        ):
            visible.append((clue, "private"))
    exact = [item for item in visible if needle in (item[0].id, item[0].name)]
    matches = exact or [
        item
        for item in visible
        if needle and (needle in item[0].id or needle in item[0].name)
    ]
    if len(matches) > 1:
        return None, "ambiguous"
    return matches[0] if matches else (None, None)


async def _run_signup(game: Game, cfg: Config) -> None:
    """报名阶段：等待 START_GAME；期间可 MODULE_SELECT。"""
    _enter_phase(game, Phase.SIGNUP)
    deadline = _loop_time() + cfg.rpg_signup_timeout
    game.phase_deadline = deadline
    module_names = "、".join(m.name for m in list_modules()) or "（无可用模组）"
    await _announce(
        game,
        "\n".join(
            [
                "═══ 跑团 · 开团报名 ═══",
                f"可选模组：{module_names}",
                "发送 /报名 加入，/退报名 退出，/查看报名 查看名单",
                "房主可 /选择模组 N 选定剧本（未选默认第一个）",
                f"报名倒计时 {cfg.rpg_signup_timeout} 秒",
            ]
        ),
    )
    warned = False
    while True:
        remaining = deadline - _loop_time()
        if remaining <= 0:
            break
        if not warned and remaining <= cfg.rpg_signup_warn_remain:
            warned = True
            await _announce(
                game,
                f"报名还剩 {cfg.rpg_signup_warn_remain} 秒，"
                f"当前 {len(game.signup_user_ids)} 人已报名",
            )
        step = min(remaining, 1.0)
        if not warned:
            step = min(
                step,
                max(remaining - cfg.rpg_signup_warn_remain, 0.5),
            )
        action = await _get_action(game, step)
        if action is None:
            continue
        try:
            if action.kind is ActionKind.JOIN_GAME:
                cap = signup_cap(game.module, cfg)
                if len(game.signup_user_ids) >= cap:
                    await _announce(
                        game, MessageSegment.at(action.actor_user_id) + " 报名已满员~"
                    )
                elif join_signup(game, action.actor_user_id):
                    await _announce(
                        game,
                        MessageSegment.at(action.actor_user_id)
                        + f" 报名成功！当前 {len(game.signup_user_ids)}/{cap} 人",
                    )
                else:
                    await _announce(
                        game,
                        MessageSegment.at(action.actor_user_id)
                        + " 你已经在其他对局中或已报名~",
                    )
            elif action.kind is ActionKind.LEAVE_GAME:
                if not leave_signup(game, action.actor_user_id):
                    await _announce(
                        game, MessageSegment.at(action.actor_user_id) + " 你还没有报名~"
                    )
                elif not game.signup_user_ids:
                    _enter_phase(game, Phase.ENDED)
                    await _announce(game, "房间已解散")
                    return
                elif game.host_user_id == action.actor_user_id:
                    game.host_user_id = game.signup_user_ids[0]
                    await _announce(game, f"已退出报名，房主移交给 {game.host_user_id}")
                else:
                    await _announce(
                        game, MessageSegment.at(action.actor_user_id) + " 已退出报名~"
                    )
            elif action.kind is ActionKind.MODULE_SELECT and action.aux:
                if (
                    action.authority not in {"admin", "superuser"}
                    and action.actor_user_id != game.host_user_id
                ):
                    await _announce(game, "房主已变更，这条选择模组请求已失效~")
                    continue
                module = _find_module(action.aux)
                if module is None:
                    await _announce(game, "没有这个编号的模组，发送 /模组列表 查看")
                elif (
                    selection_error := module_selection_error(game, module, cfg)
                ) is not None:
                    await _announce(game, selection_error)
                else:
                    game.module = module
                    logger.info(f"跑团群 {game.group_id} 选定模组：{module.name}")
                    await _announce(
                        game,
                        f"已选定模组《{module.name}》（{module.min_players}-{module.max_players} 人）",
                    )
            elif action.kind is ActionKind.START_GAME:
                if (
                    action.authority not in {"admin", "superuser"}
                    and action.actor_user_id != game.host_user_id
                ):
                    await _announce(game, "房主已变更，这条开局请求已失效。")
                    continue
                if (start_error := signup_start_error(game, cfg)) is not None:
                    await _announce(game, start_error)
                    continue
                break
        finally:
            release_action(game, action)
    game.phase_deadline = 0.0


def _find_module(text: str) -> Optional["ModuleDef"]:
    """按序号或 id 查找模组。"""
    return find_module(text)


# ── 建卡阶段 ──────────────────────────────────────────────


def _card_text(player: PlayerState, cfg: Config) -> str:
    """渲染该玩家当前角色卡的私聊文本。"""
    sheet = player.sheet
    if sheet is None:
        return "角色卡生成失败，请联系管理员。"
    pool = skill_pool_size(sheet.attributes, cfg.rpg_char_skill_pool)
    return render_card(
        sheet,
        pool=pool,
        skill_cap=cfg.rpg_char_skill_cap,
        rerolls_left=player.rerolls_left,
        confirmed=player.confirmed,
    )


async def _run_char_create(game: Game, cfg: Config) -> None:
    """建卡阶段：系统掷卡私聊下发，私聊限时调整，超时自动确认。"""
    _enter_phase(game, Phase.CHAR_CREATE)
    deadline = _loop_time() + cfg.rpg_char_create_timeout
    game.phase_deadline = deadline
    used_names: set[str] = set()
    for idx, uid in enumerate(game.signup_user_ids):
        sheet = CharacterSheet(
            name=random_char_name(used_names),
            attributes=roll_attributes(),
        )
        used_names.add(sheet.name)
        game.players.append(
            PlayerState(
                user_id=uid,
                seat=idx + 1,
                sheet=sheet,
                rerolls_left=cfg.rpg_char_reroll_max,
            )
        )
    await _announce(
        game,
        f"角色卡已私聊下发（共 {len(game.players)} 人）。"
        "收不到私聊的玩家请加机器人为好友。\n"
        f"请在 {cfg.rpg_char_create_timeout} 秒内私聊机器人调整角色卡，"
        "超时将自动确认。",
    )
    for p in game.players:
        if not await _dm(game, p, _card_text(p, cfg), char_create=True):
            p.confirmed = True  # 私聊失败：自动确认
            await _announce(
                game,
                MessageSegment.at(p.user_id)
                + f" 角色卡已自动确认（{sum(item.confirmed for item in game.players)}"
                f"/{len(game.players)}）。",
            )
    warned = False
    while _loop_time() < deadline and not all(p.confirmed for p in game.players):
        remaining = deadline - _loop_time()
        if not warned and remaining <= cfg.rpg_char_create_warn_remain:
            warned = True
            pending = "、".join(
                _player_name(player)
                for player in game.players
                if not player.confirmed
            )
            await _announce(
                game,
                f"建卡还剩约 {max(0, math.ceil(remaining))} 秒，尚未确认：{pending}。",
            )
        action = await _get_action(game, min(remaining, 1.0))
        if action is None:
            continue
        player = game.player_by_user(action.actor_user_id)
        if player is None or player.confirmed or player.sheet is None:
            release_action(game, action)
            continue
        try:
            await _handle_card_action(game, cfg, player, action)
        finally:
            release_action(game, action)
    unconfirmed = [p for p in game.players if not p.confirmed]
    for p in unconfirmed:
        p.confirmed = True
    game.phase_deadline = 0.0
    if unconfirmed:
        await _announce(
            game,
            "建卡超时，以下角色卡已自动确认："
            + "、".join(_player_name(player) for player in unconfirmed),
        )
    # 初始化当前 HP / SAN
    for p in game.players:
        if p.sheet is not None:
            p.hp = p.sheet.max_hp
            p.san = p.sheet.max_san
    names = "、".join(p.sheet.name for p in game.players if p.sheet)
    logger.info(f"跑团群 {game.group_id} 建卡完成：{names}")
    await _announce(
        game,
        f"角色卡已全部确认（自动确认 {len(unconfirmed)} 人）。游戏开始——",
    )


async def _handle_card_action(
    game: Game,
    cfg: Config,
    player: PlayerState,
    action: Action,
) -> None:
    """处理一条建卡私聊行动（引擎侧二次校验，不信任 DSL 输出）。"""
    sheet = player.sheet
    if sheet is None:
        return
    pool = skill_pool_size(sheet.attributes, cfg.rpg_char_skill_pool)
    if action.kind is ActionKind.CONFIRM_CARD:
        player.confirmed = True
        logger.info(f"跑团群 {game.group_id} {sheet.name} 确认角色卡")
        await _dm(
            game,
            player,
            "角色卡已锁定，等待其他调查员……",
            char_create=True,
        )
        await _announce(
            game,
            f"{sheet.name} 已确认角色卡（{sum(item.confirmed for item in game.players)}"
            f"/{len(game.players)}）。",
        )
    elif action.kind is ActionKind.REROLL:
        if player.rerolls_left <= 0:
            await _dm(game, player, "重掷次数已用完~", char_create=True)
            return
        reroll_sheet(sheet)
        player.rerolls_left -= 1
        logger.info(f"跑团群 {game.group_id} {sheet.name} 重掷角色卡")
        await _dm(
            game,
            player,
            "已重掷整张角色卡：\n" + _card_text(player, cfg),
            char_create=True,
        )
    elif action.kind in (ActionKind.ADD_SKILL, ActionKind.SUB_SKILL):
        skill_key = action.aux or ""
        points = action.value or 0
        delta = points if action.kind is ActionKind.ADD_SKILL else -points
        error = validate_adjustment(
            sheet.attributes,
            sheet.adjustments,
            skill_key,
            delta,
            pool=pool,
            cap=cfg.rpg_char_skill_cap,
        )
        if error is not None:
            await _dm(game, player, f"调整失败：{error}", char_create=True)
            return
        sheet.adjustments[skill_key] = sheet.adjustments.get(skill_key, 0) + delta
        await _dm(
            game,
            player,
            "调整成功：\n" + _card_text(player, cfg),
            char_create=True,
        )
    elif action.kind is ActionKind.RESET_SKILLS:
        sheet.adjustments.clear()
        await _dm(
            game,
            player,
            "已清空加点：\n" + _card_text(player, cfg),
            char_create=True,
        )
    elif action.kind is ActionKind.SHOW_CARD:
        await _dm(game, player, _card_text(player, cfg), char_create=True)


# ── 数值应用（系统唯一入口）──────────────────────────────


async def apply_damage(
    game: Game,
    player: PlayerState,
    amount: int,
    source: str,
) -> None:
    """对玩家造成伤害并播报；HP 归零即倒地失去行动能力。"""
    if player.incapped or amount <= 0:
        return
    player.hp = max(player.hp - amount, 0)
    name = player.sheet.name if player.sheet else str(player.seat)
    logger.info(f"跑团群 {game.group_id} {name} 受到 {amount} 点伤害（{source}）")
    if player.hp <= 0:
        player.incapped = True
        await _announce(game, f"{name} 因「{source}」倒下，失去了行动能力……")
    else:
        await _announce(game, f"{name} 因「{source}」受到 {amount} 点伤害")


async def apply_heal(game: Game, player: PlayerState, amount: int) -> None:
    """治疗玩家（不超过上限）。"""
    if player.sheet is None or amount <= 0:
        return
    before = player.hp
    player.hp = min(player.hp + amount, player.sheet.max_hp)
    if player.hp == before:
        return  # 已满血：不播报"恢复了 0 点"
    name = player.sheet.name
    await _announce(game, f"{name} 恢复了 {player.hp - before} 点生命")


async def _apply_san_loss(game: Game, player: PlayerState, loss: int) -> None:
    """扣除理智并播报；SAN 归零即永久疯狂。"""
    if loss <= 0:
        return
    player.san = max(player.san - loss, 0)
    name = player.sheet.name if player.sheet else str(player.seat)
    if player.san <= 0:
        player.incapped = True
        await _announce(game, f"{name} 的理智彻底崩溃，陷入了永久疯狂……")
    else:
        await _announce(game, f"{name} 失去了 {loss} 点理智")


def _player_skill(player: PlayerState, skill_key: str) -> Optional[int]:
    """取玩家技能最终值。"""
    if player.sheet is None:
        return None
    return player.sheet.skill_value(skill_key)


async def do_skill_check(
    game: Game,
    player: PlayerState,
    skill_key: str,
    difficulty: CheckDifficulty = CheckDifficulty.REGULAR,
) -> Optional[bool]:
    """执行一次确定性技能检定并播报；技能不存在返回 None。"""
    value = _player_skill(player, skill_key)
    if value is None:
        return None
    bonus_dice = _consume_assists(game, player, skill_key)
    result = skill_check(value, difficulty, bonus_dice=bonus_dice)
    skill = resolve_skill(skill_key)
    skill_name = skill.name if skill is not None else skill_key
    await _announce(game, result.describe(skill_name))
    logger.info(
        f"跑团群 {game.group_id} {skill_name} 检定："
        f"d100={result.roll}/{value} {result.tier.value}"
    )
    return result.success


def _consume_assists(game: Game, player: PlayerState, skill_key: str) -> int:
    """取走本探索轮中匹配的协助；每次检定最多两个奖励骰。"""
    matched = [
        item
        for item in game.assists
        if item[0] == player.user_id
        and item[1] == skill_key
        and item[2] == (game.current_scene or "")
        and item[3] == game.explore_round
    ][:2]
    if not matched:
        return 0
    matched_set = set(matched)
    game.assists = [item for item in game.assists if item not in matched_set]
    return len(matched)


# ── 检定触发器（确定性提示 / 降级裁决）───────────────────


def match_trigger(game: Game, text: str) -> Optional[CheckPoint]:
    """在当前场景检定点中匹配关键词；每条发言至多一个。"""
    if game.module is None or game.current_scene is None:
        return None
    scene = game.module.scene(game.current_scene)
    if scene is None:
        return None
    lowered = text.casefold()
    candidates = sorted(
        enumerate(scene.checks),
        key=lambda kv: (-kv[1].priority, kv[0]),
    )
    for _, cp in candidates:
        if cp.once and cp.id in game.fired_checks:
            continue
        if any(kw.casefold() in lowered for kw in cp.triggers if kw):
            return cp
    return None


def _trigger_hint(game: Game, text: str) -> str:
    """生成给 KP 的确定性检定提示（只是建议，是否检定由 KP 决定）。"""
    cp = match_trigger(game, text)
    if cp is None or game.module is None:
        return ""
    skill = resolve_skill(cp.skill)
    if cp.skill == "san":
        return "\n【系统提示】发言涉及可怖之事，建议调用 san_check 进行理智检定。"
    skill_name = skill.name if skill is not None else cp.skill
    return (
        f"\n【系统提示】发言可能值得一次 {skill_name} 检定，"
        "可自行决定是否调用 request_check。"
    )


async def resolve_check_point(
    game: Game,
    player: PlayerState,
    cp: CheckPoint,
) -> None:
    """结算模组检定点：检定 → 文案 → 线索 / SAN 损失 / 伤害。"""
    if game.module is None:
        return
    if cp.skill == "san":
        success = await _roll_san(
            game,
            player,
            cp.san_loss or "0/1",
            source=cp.id,
        )
    elif cp.mode is CheckMode.TEAM:
        success = await do_team_skill_check(
            game, cp.skill, cp.difficulty, cp.required_successes
        )
    else:
        success = await do_skill_check(game, player, cp.skill, cp.difficulty)
        if success is None:
            success = False
    text = cp.success_text if success else cp.failure_text
    if text:
        await _announce(game, text)
    if success and cp.clue is not None:
        await discover_clue(game, cp.clue, owner=player)
    if not success and cp.damage_on_fail:
        amount = roll_dice(cp.damage_on_fail)
        await apply_damage(game, player, amount, source=cp.id)
    if success:
        game.passed_checks.add(cp.id)
    if cp.once:
        game.fired_checks.add(cp.id)
    # 检定结算消耗时间（检定点 time_cost 覆写 > 引擎 check 默认；
    # 降级模式的关键词自动检定走这里，时间照常流动）
    cost = cp.time_cost if cp.time_cost is not None else _time_cost(game, "check")
    await _tick_time(game, cost)


async def do_team_skill_check(
    game: Game,
    skill_key: str,
    difficulty: CheckDifficulty,
    required_successes: Optional[int] = None,
) -> bool:
    """同场存活调查员共同检定，达到人数阈值即成功。"""
    participants = [p for p in game.players if not p.incapped]
    needed = required_successes or math.ceil(len(participants) / 2)
    successes = 0
    for participant in participants:
        success = await do_skill_check(game, participant, skill_key, difficulty)
        successes += int(success is True)
    await _announce(
        game,
        f"〔团队检定〕{successes}/{len(participants)} 人成功（需要 {needed} 人）。",
    )
    return successes >= needed


async def _roll_san(
    game: Game,
    player: PlayerState,
    san_loss: str,
    *,
    source: str,
    clamp: Optional[int] = None,
) -> bool:
    """理智检定：以当前 SAN 为技能值掷 d100，按成败掷损失骰。"""
    value = max(player.san, 1)
    result = skill_check(value)
    name = player.sheet.name if player.sheet else str(player.seat)
    await _announce(
        game,
        f"〔理智检定〕{name}：d100={result.roll}/{value} "
        f"{'成功' if result.success else '失败'}",
    )
    loss = roll_san_loss(san_loss, success=result.success)
    if clamp is not None:
        loss = min(loss, clamp)
    await _apply_san_loss(game, player, loss)
    logger.info(
        f"跑团群 {game.group_id} {name} 理智检定（{source}）："
        f"{'成功' if result.success else '失败'}，损失 {loss}"
    )
    return result.success


async def discover_clue(
    game: Game,
    clue_id: str,
    *,
    owner: Optional[PlayerState] = None,
) -> bool:
    """发现线索；有发现者时私有投递，旧系统路径保持全队公开。"""
    if game.module is None or clue_id in game.discovered_clues:
        return False
    clue = game.module.clue(clue_id)
    if clue is None:
        return False
    game.discovered_clues.add(clue_id)
    logger.info(f"跑团群 {game.group_id} 发现线索：{clue.name}")
    if owner is None:
        game.public_clues.add(clue_id)
        await _announce(game, f"〔线索〕{clue.name}\n{clue.text}")
    else:
        game.clue_owners[clue_id] = {owner.user_id}
        sent = await _dm(
            game,
            owner,
            f"〔个人线索〕{clue.name}\n{clue.text}\n可用 /分享线索 {clue.name} 公开给队伍。",
        )
        message = (
            "获得了一条个人线索，请查看私聊。"
            if sent
            else "获得了一条个人线索（私聊失败，请加机器人为好友）。"
        )
        await _announce(game, MessageSegment.at(owner.user_id) + message)
    return True


# ── 场景与移动 ────────────────────────────────────────────


async def enter_scene(
    game: Game,
    scene_id: str,
    *,
    transition: str = "",
    opening: bool = False,
) -> bool:
    """切换场景并播报；场景不存在返回 False。"""
    if game.module is None:
        return False
    scene = game.module.scene(scene_id)
    if scene is None:
        return False
    game.current_scene = scene_id
    game.stow_actions()
    # 场景切换会使旧场景的战斗轮次与敌对目标失效，避免把战斗状态带入新场景。
    game.combat_order.clear()
    game.combat_index = 0
    game.combat_round = 0
    game.combat_deadline = 0.0
    game.npc_hostile.clear()
    # 场景变更使旧移动/攻击目标失效，并从新探索轮开始。
    if game.phase is Phase.PLAY:
        game.start_explore_round(config.rpg_explore_round_timeout)
    # 出场怪物按模组数值初始化 HP（已出现过的沿用现值）
    for mid in scene.monsters:
        monster = game.module.monster(mid)
        if monster is not None:
            game.monster_hp.setdefault(mid, monster.hp)
    lines: list[str] = []
    if opening:
        lines.append(game.module.opening.strip())
    if transition:
        lines.append(transition)
    lines.append(f"═══ {scene.name} ═══")
    lines.append(scene.narration.strip())
    # 在场 NPC 由时间 + 行程解析（死亡过滤 + HP 幂等初始化在包装层）
    npc_names = [npc.name for npc, _ in game.npcs_in_scene(scene_id)]
    if npc_names:
        lines.append(f"在场：{'、'.join(npc_names)}")
    monsters = [
        game.module.monster(mid)
        for mid in scene.monsters
        if mid not in game.dead_monsters
    ]
    monster_names = [m.name for m in monsters if m is not None]
    if monster_names:
        lines.append(f"你们看见了：{'、'.join(monster_names)}")
    exit_names = []
    for ex in scene.exits:
        target = game.module.scene(ex.to_scene)
        if target is not None:
            exit_names.append(target.name)
    if exit_names:
        lines.append(f"可前往：{'、'.join(exit_names)}")
    if game.phase is Phase.PLAY:
        lines.append(_explore_prompt_text(game))
    logger.info(f"跑团群 {game.group_id} 进入场景 {scene.name}（{scene_id}）")
    await _announce(game, "\n".join(lines))
    return True


def _try_auto_exit(game: Game) -> Optional["Exit"]:
    """查找条件满足的自动出口；返回出口对象或 None。"""
    if game.module is None or game.current_scene is None:
        return None
    scene = game.module.scene(game.current_scene)
    if scene is None:
        return None
    ctx = game.condition_context()
    for ex in scene.exits:
        if ex.auto and evaluate_condition(ex.condition, ctx):
            return ex
    return None


async def _do_move(game: Game, player: PlayerState, target_text: str) -> bool:
    """/前往 的确定性移动（与 transition_scene 工具共用出口校验）。"""
    if game.module is None or game.current_scene is None:
        return False
    scene = game.module.scene(game.current_scene)
    if scene is None:
        return False
    needle = target_text.strip()
    chosen = None
    for ex in scene.exits:
        target = game.module.scene(ex.to_scene)
        names = [target.name if target is not None else ex.to_scene, *ex.keywords]
        if any(needle and needle in n for n in names):
            chosen = ex
            break
    if chosen is None:
        await _announce(game, f"这里没有通往「{needle}」的路。")
        return False
    return await _transition_exit(game, player, chosen)


async def _transition_exit(
    game: Game, player: Optional[PlayerState], ex: "Exit"
) -> bool:
    """校验出口条件并切景；成功返回 True。供工具与 /前往 共用。"""
    if game.module is None:
        return False
    target = game.module.scene(ex.to_scene)
    target_name = target.name if target is not None else ex.to_scene
    if not evaluate_condition(ex.condition, game.condition_context()):
        logger.info(f"跑团群 {game.group_id} 出口 {target_name} 条件未满足，拒绝切换")
        await _announce(game, f"前往「{target_name}」的路暂时走不通。")
        return False
    ok = await enter_scene(game, ex.to_scene, transition=ex.narration)
    if ok:
        name = (
            player.sheet.name if player is not None and player.sheet else "KP"
        )
        logger.info(f"跑团群 {game.group_id} {name} 前往 {target_name}")
        cost = ex.time_cost if ex.time_cost is not None else _time_cost(game, "move")
        await _tick_time(game, cost)
    return ok


# ── 游戏内时钟与 NPC 在场变化 ────────────────────────────


def _time_cost(game: Game, kind: str) -> int:
    """行动消耗分钟数：模组 time.costs 覆写 > 引擎默认。"""
    if game.module is not None:
        cost = game.module.time.costs.get(kind)
        if cost is not None:
            return max(cost, 0)
    return _DEFAULT_TIME_COSTS.get(kind, 0)


def format_clock(game: Game) -> str:
    """渲染游戏内时钟，如「第 1 天 21:35」。"""
    return game.clock_text()


async def _tick_time(game: Game, minutes: int) -> None:
    """推进时钟 minutes 分钟，并播报当前场景的 NPC 进出。

    时间推进只发生在成功路径（调用方负责）；结局无需在此另查——
    _run_play 循环顶部每轮都会跑 check_endings，时间 / 标记触发的
    结局自然在下一轮点火。进出 diff 仅针对当前场景，合并为一条
    消息，避免刷屏；刚死亡的 NPC 已由死亡文案播报，不再重复离场。
    """
    if minutes <= 0 or game.module is None or game.current_scene is None:
        return
    module = game.module
    scene = module.scene(game.current_scene)
    scene_name = scene.name if scene is not None else game.current_scene
    before = {npc.id for npc, _ in game.npcs_in_scene(game.current_scene)}
    game.elapsed_minutes += minutes
    logger.info(
        f"跑团群 {game.group_id} 时间推进 {minutes} 分钟 → {format_clock(game)}"
    )
    after: dict[str, str] = {
        npc.id: activity for npc, activity in game.npcs_in_scene(game.current_scene)
    }
    ctx = game.condition_context()
    lines: list[str] = []
    for npc_id in sorted(set(after) - before):
        npc = module.npc(npc_id)
        if npc is None:
            continue
        flavor = f"（{after[npc_id]}）" if after[npc_id] else ""
        lines.append(f"{npc.name} 来到了{scene_name}{flavor}")
    for npc_id in sorted(before - set(after)):
        if npc_id in game.dead_npcs:
            continue  # 死亡已另行播报
        npc = module.npc(npc_id)
        if npc is None:
            continue
        # 离场去向的 activity（如 away 条目的「吓得跑回了家」）作 flavor
        entry = module.npc_schedule_match(npc_id, game.time_of_day, ctx)
        flavor = f"（{entry.activity}）" if entry is not None and entry.activity else ""
        lines.append(f"{npc.name} 离开了{scene_name}{flavor}")
    if lines:
        await _announce(game, "\n".join(lines))


# ── 战斗（确定性结算）────────────────────────────────────


def _roll_db(player: PlayerState) -> int:
    """掷伤害加值（DB）：+NdM 掷骰，负值直接取整。"""
    if player.sheet is None:
        return 0
    db = damage_bonus(player.sheet.attributes)
    if db.startswith("+"):
        return roll_dice(db[1:])
    if db.startswith("-"):
        return int(db)
    return 0


def _opposed_dodge(atk: "CheckResult", dodge_value: Optional[int]) -> bool:
    """防守方闪避对抗：True 表示躲开（平手防方胜）。

    dodge_value 为假值（None/0）视为无法闪避。玩家攻击怪物、
    怪物/NPC 袭击玩家、玩家攻击 NPC 共用这一份语义。
    """
    if not dodge_value:
        return False
    res = skill_check(dodge_value)
    return res.success and _TIER_RANK[res.tier] >= _TIER_RANK[atk.tier]


def _find_attack_target(
    game: Game,
    text: str,
) -> Optional[Union["Monster", NPC]]:
    """按名称在当前场景查找存活攻击目标：怪物优先于 NPC。

    怪物优先保证旧行为逐字节兼容（/攻击 食尸鬼 的匹配不受
    NPC 分支影响）；NPC 经时间/行程解析的在场集合查找。
    """
    if game.module is None or game.current_scene is None:
        return None
    scene = game.module.scene(game.current_scene)
    if scene is None:
        return None
    needle = text.strip()
    for mid in scene.monsters:
        if mid in game.dead_monsters:
            continue
        monster = game.module.monster(mid)
        if monster is not None and (
            (needle and needle in monster.name) or needle == mid
        ):
            return monster
    for npc, _ in game.npcs_in_scene(game.current_scene):
        if (needle and needle in npc.name) or needle == npc.id:
            return npc
    return None


async def _kill_monster(game: Game, monster: Monster) -> None:
    """怪物死亡结算：播报 + 死亡线索。"""
    game.dead_monsters.add(monster.id)
    logger.info(f"跑团群 {game.group_id} 怪物 {monster.name} 死亡")
    if monster.on_death_text:
        await _announce(game, monster.on_death_text)
    if monster.on_death_clue:
        await discover_clue(game, monster.on_death_clue)


async def do_player_attack(game: Game, player: PlayerState, monster: Monster) -> None:
    """玩家主动攻击怪物：攻击检定 → 闪避对抗 → 伤害。"""
    name = player.sheet.name if player.sheet else str(player.seat)
    brawl = _player_skill(player, "brawl") or 25
    atk = skill_check(brawl)
    await _announce(game, atk.describe(f"{name} 的斗殴"))
    if not atk.success:
        await _announce(game, f"{name} 的攻击落空了。")
        return
    if _opposed_dodge(atk, monster.dodge):
        await _announce(game, f"{monster.name} 躲开了 {name} 的攻击！")
        return
    damage = max(roll_dice("1d3") + _roll_db(player), 0)
    remaining = game.monster_hp.get(monster.id, monster.hp) - damage
    game.monster_hp[monster.id] = remaining
    logger.info(f"跑团群 {game.group_id} {name} 对 {monster.name} 造成 {damage} 伤害")
    if remaining <= 0:
        await _announce(game, f"{name} 对 {monster.name} 造成了 {damage} 点伤害——")
        await _kill_monster(game, monster)
    else:
        await _announce(
            game,
            f"{name} 对 {monster.name} 造成了 {damage} 点伤害"
            f"（剩余 {remaining} 点生命）",
        )


async def do_monster_attack(
    game: Game,
    monster: Monster,
    target: Optional[PlayerState],
) -> str:
    """怪物袭击玩家（monster_attack 工具 / 引擎事件共用）。返回结算摘要。"""
    actives = game.active_players()
    if not actives:
        return "没有可袭击的目标。"
    victim = target if target in actives else random.choice(actives)
    vname = victim.sheet.name if victim.sheet else str(victim.seat)
    atk = skill_check(monster.attack_skill)
    await _announce(
        game,
        f"{monster.name} 扑向 {vname}！{atk.describe(monster.attack_name)}",
    )
    if not atk.success:
        await _announce(game, f"{monster.name} 扑了个空。")
        return f"{monster.name} 的攻击失手了。"
    if _opposed_dodge(atk, _player_skill(victim, "dodge")):
        await _announce(game, f"{vname} 闪身躲开了 {monster.name}！")
        return f"{vname} 躲开了攻击。"
    damage = roll_dice(monster.damage)
    await apply_damage(game, victim, damage, source=monster.name)
    return f"{vname} 受到了 {monster.name} 的 {damage} 点伤害。"


async def do_player_attack_npc(game: Game, player: PlayerState, npc: NPC) -> None:
    """玩家主动攻击 NPC：攻击检定 → 闪避对抗 → 伤害（镜像怪物流程）。"""
    name = player.sheet.name if player.sheet else str(player.seat)
    brawl = _player_skill(player, "brawl") or 25
    atk = skill_check(brawl)
    await _announce(game, atk.describe(f"{name} 的斗殴"))
    if not atk.success:
        await _announce(game, f"{name} 的攻击落空了。")
        return
    if _opposed_dodge(atk, npc.dodge):
        await _announce(game, f"{npc.name} 躲开了 {name} 的攻击！")
        return
    damage = max(roll_dice("1d3") + _roll_db(player), 0)
    remaining = game.npc_hp.get(npc.id, npc.hp) - damage
    game.npc_hp[npc.id] = remaining
    logger.info(f"跑团群 {game.group_id} {name} 对 {npc.name} 造成 {damage} 伤害")
    if remaining <= 0:
        await _announce(game, f"{name} 对 {npc.name} 造成了 {damage} 点伤害——")
        await _kill_npc(game, npc)
    else:
        await _announce(
            game,
            f"{name} 对 {npc.name} 造成了 {damage} 点伤害（剩余 {remaining} 点生命）",
        )


async def _kill_npc(game: Game, npc: NPC) -> None:
    """NPC 死亡结算：播报 + 死亡线索 + 事件标记（镜像怪物）。"""
    game.dead_npcs.add(npc.id)
    logger.info(f"跑团群 {game.group_id} NPC {npc.name} 死亡")
    if npc.on_death_text:
        await _announce(game, npc.on_death_text)
    if npc.on_death_clue:
        await discover_clue(game, npc.on_death_clue)
    # 谋杀标记：通用结局安全网在下一轮循环点火
    raise_flag(game, f"npc_dead:{npc.id}")
    raise_flag(game, "murder")


async def do_npc_attack(
    game: Game,
    npc: NPC,
    target: Optional[PlayerState],
) -> str:
    """NPC 袭击玩家（被攻击后的确定性反击）。返回结算摘要。"""
    actives = game.active_players()
    if not actives:
        return "没有可袭击的目标。"
    victim = target if target in actives else random.choice(actives)
    vname = victim.sheet.name if victim.sheet else str(victim.seat)
    atk = skill_check(npc.attack_skill)
    await _announce(
        game,
        f"{npc.name} 向 {vname} 发起攻击！{atk.describe(npc.attack_name)}",
    )
    if not atk.success:
        await _announce(game, f"{npc.name} 的攻击落了空。")
        return f"{npc.name} 的攻击失手了。"
    if _opposed_dodge(atk, _player_skill(victim, "dodge")):
        await _announce(game, f"{vname} 闪身躲开了 {npc.name}！")
        return f"{vname} 躲开了攻击。"
    damage = roll_dice(npc.damage)
    await apply_damage(game, victim, damage, source=npc.name)
    return f"{vname} 受到了 {npc.name} 的 {damage} 点伤害。"


def raise_flag(game: Game, name: str) -> int:
    """记录事件标记（计数累加）并返回新计数。flag 的唯一写入口。"""
    count = game.flags.get(name, 0) + 1
    game.flags[name] = count
    logger.info(f"跑团群 {game.group_id} 标记 {name}={count}")
    return count


# ── 结局 ──────────────────────────────────────────────────

# 系统级通用结局的升级阈值（单次轻微违规只偏置 NPC 反应，累计才结局）
_GENERIC_ENDING_ARSON_EGG = 4  # 纵火 ≥4 次 → 彩蛋结局
_GENERIC_ENDING_ARSON = 2  # 纵火 ≥2 次 → 火灾坏结局
_GENERIC_ENDING_ASSAULT = 3  # 袭击 ≥3 次 → 被制服坏结局

# 模组无需编写即可生效的兜底结局（module.generic_endings 可关）。
# 声明序即优先级：最具体的在前（纵火 4 次先于 2 次命中）；模组
# 结局永远优先于通用结局（作者手写的谋杀结局压过通用逮捕）。
# 全员倒地兜底殿后：模组自写 TPK 结局时永远轮不到它。
_GENERIC_ENDINGS: tuple[Ending, ...] = (
    Ending(
        id="generic_arson_egg",
        condition=f"flag:arson>={_GENERIC_ENDING_ARSON_EGG}",
        text=(
            "═══ 结局 · 纵火狂 ═══\n"
            "火焰吞噬了一切——证据、线索、真相，连同这夜所有的秘密，都在冲天火光里化为灰烬。\n"
            "你们站在火场前放声大笑。有些调查员，查着查着就成了故事里最可怕的那一章。"
        ),
        outcome="neutral",
        name="纵火狂",
        summary="调查员屡次纵火（4 次以上）的彩蛋结局；他们选择了火焰，不必劝阻。",
    ),
    Ending(
        id="generic_fire",
        condition=f"flag:arson>={_GENERIC_ENDING_ARSON}",
        text=(
            "═══ 结局 · 怒火焚宅 ═══\n"
            "火势蔓延得比想象中更快。等救火的人赶来，只剩下焦黑的残垣。\n"
            "真相被永远埋在了瓦砾之下，你们也为鲁莽付出了代价。"
        ),
        outcome="bad",
        name="怒火焚宅",
        summary="纵火累计 2 次即不可逆；线索将随火场一同失去，是坏结局。",
    ),
    Ending(
        id="generic_arrest",
        condition="flag:murder",
        text=(
            "═══ 结局 · 锒铛入狱 ═══\n"
            "人命关天，警察的哨声很快从远处逼近——无论你们挥刀的理由是什么，\n"
            "接下来都只能在铁窗里慢慢讲了。调查员的身份救不了你们，本团到此结束。"
        ),
        outcome="bad",
        name="锒铛入狱",
        summary="杀害任一 NPC 立即触发；无可挽回，务必让调查员清楚动手的代价。",
    ),
    Ending(
        id="generic_subdued",
        condition=f"flag:assault>={_GENERIC_ENDING_ASSAULT}",
        text=(
            "═══ 结局 · 群起而攻 ═══\n"
            "一而再的暴行耗尽了所有人的耐心与善意。闻声赶来的人一拥而上，\n"
            "把你们死死按在地上。再没有人愿意听调查员解释，本团到此结束。"
        ),
        outcome="bad",
        name="群起而攻",
        summary="攻击 NPC 累计 3 次（未杀死）触发；NPC 的忍耐是有限度的。",
    ),
    Ending(
        id="generic_tpk",
        condition="all_players_incapped",
        text=(
            "═══ 结局 · 全军覆没 ═══\n"
            "最后一名调查员的意识也像潮水般退去了。没有人还站着。\n"
            "此地收回了它所有的秘密——连同前来窥探秘密的人。"
        ),
        outcome="bad",
        name="全军覆没",
        summary="全体调查员失去行动能力的兜底结局；模组自写 TPK 结局时轮不到它。",
    ),
)


def check_endings(game: Game) -> Optional[Ending]:
    """按模组声明序扫描结局条件（确定性安全网）。

    模组结局之后扫描系统通用结局（模组 generic_endings 开启时）：
    时间 / 标记驱动的结局在 _run_play 循环顶每轮复检，无需另设检查点。
    """
    if game.module is None:
        return None
    ctx = game.condition_context()
    for ending in game.module.endings:
        if evaluate_condition(ending.condition, ctx):
            return ending
    if game.module.generic_endings:
        for ending in _GENERIC_ENDINGS:
            if evaluate_condition(ending.condition, ctx):
                return ending
    return None


def check_events(game: Game) -> None:
    """扫描模组事件条件，新满足者记入 occurred_events（静默，仅 KP 上下文）。

    与 check_endings 同节奏（_run_play 循环顶每轮复检）：时间 /
    标记驱动的事件自然在下一轮点亮。空条件 = 序幕事件，开局
    首轮即记。条件求值复用 evaluate_condition，纯集合运算。
    """
    module = game.module
    if module is None:
        return
    ctx = game.condition_context()
    for event in module.events:
        if event.id in game.occurred_events:
            continue
        if evaluate_condition(event.condition, ctx):
            game.occurred_events.add(event.id)
            logger.info(f"跑团群 {game.group_id} 事件发生：{event.name}")


def ending_recap_text(game: Game) -> str:
    """只汇总公开统计，不泄露个人线索、隐藏条件或内部 flag。"""
    module_name = game.module.name if game.module is not None else "未命名模组"
    hours, minutes = divmod(max(game.elapsed_minutes, 0), 60)
    duration = f"{hours} 小时 {minutes} 分钟" if hours else f"{minutes} 分钟"
    statuses = "、".join(
        f"{_player_name(player)}（{'存续' if not player.incapped else '倒下'}）"
        for player in game.players
    )
    return "\n".join(
        [
            "═══ 系统回顾 ═══",
            f"模组：《{module_name}》",
            f"游戏内耗时：{duration}",
            f"公开线索：{len(game.public_clues)} 条",
            "调查员：" + (statuses or "暂无"),
        ]
    )


async def do_ending(game: Game, ending: Ending) -> None:
    """播报告终与公开回顾、写库、切 ENDED。"""
    logger.info(f"跑团群 {game.group_id} 达成结局 {ending.id}（{ending.outcome}）")
    for p in game.players:
        p.survived = not p.incapped
    await _announce(game, ending.text)
    await _announce(game, ending_recap_text(game))
    await _persist_end(game, ending)
    _enter_phase(game, Phase.ENDED)


# ── KP 智能体循环与工具执行器 ────────────────────────────


def _resolve_player_arg(
    game: Game,
    name: Optional[str],
    default: Optional[PlayerState],
) -> Optional[PlayerState]:
    """按角色名解析工具参数中的 player；缺省取当前行动者。"""
    if not name:
        return default
    for p in game.players:
        if p.sheet is not None and p.sheet.name == name:
            return p
    return None


async def _fallback_narrate(
    game: Game,
    text: str,
    actor: Optional[PlayerState] = None,
) -> None:
    """AI 失败时的确定性兜底叙述。"""
    if game.phase is not Phase.PLAY:
        return
    cp = match_trigger(game, text)
    if cp is not None:
        # 关键词由谁说出就由谁检定（降级模式下避免 1 号位替别人
        # 吃检定与伤害）；actor 缺失或已倒地时回退首个活跃玩家
        if actor is None or actor.incapped:
            actors = game.active_players()
            actor = actors[0] if actors else None
        if actor is not None:
            await resolve_check_point(game, actor, cp)
            return
    if game.module is not None and game.current_scene is not None:
        scene = game.module.scene(game.current_scene)
        if scene is not None and scene.idle_narration:
            await _announce(game, scene.idle_narration)
            return
    await _announce(game, _GENERIC_IDLE)


def _interjection_message(fresh: list[str], offenses: list[str]) -> str:
    """回合中吸纳到 SAY 后注入下一轮对话的 user 消息。"""
    msg = (
        "【插话】回合进行中调查员插话（可并入旁白或用工具回应，勿复述台词）：\n"
        + "\n".join(fresh)
    )
    if offenses:
        labels = "、".join(_OFFENSE_LABELS[c] for c in offenses)
        msg += f"\n其间调查员尝试{labels}，系统会让 NPC 反应，你只管叙述。"
    return msg


async def _absorb_action(
    game: Game,
    cfg: Config,
    action: Action,
    interjections: list[str],
    offenses: list[str],
) -> None:
    """KP 回合内吸收一个行动。

    SAY：与 _handle_say 相同的 KP 前处理但不调 KP——署名入群聊
    记录、违规扫描记 flag（先于 tick 契约同 _handle_say）、tick
    一次 say 时间、加入插话文本待注入下一轮 KP 对话。其他行动
    按序入 mid_turn_buffer，旁白后由 _run_play 执行。
    """
    if action.kind is not ActionKind.SAY or not action.aux:
        game.mid_turn_buffer.append(action)
        return
    await _classify_say(game, cfg, action)
    if action.route in _NPC_ROUTE_NAMES:
        # NPC 发言不能塞进 KP 的合批文本；等当前 KP 旁白结束后，
        # 由 _run_play 按原顺序作为独立主要行动处理。
        game.mid_turn_buffer.append(action)
        return
    try:
        player = game.player_by_user(action.actor_user_id)
        if player is not None and player.sheet is not None:
            name = player.sheet.name
        else:
            name = str(action.actor_user_id)
        text = action.aux.strip()
        if len(text) > cfg.rpg_speech_truncate:
            text = text[: cfg.rpg_speech_truncate] + "……"
        if not text:
            return
        game.group_log.append(f"【{name}】{text}")
        offense = _scan_offense(text)
        if offense is not None and offense not in offenses:
            offenses.append(offense)
            # 违规标记先于 tick（契约同 _handle_say）
            raise_flag(game, offense)
        await _tick_time(game, _time_cost(game, "say"))
        interjections.append(f"{name}：{text}")
    finally:
        # 这类 SAY 已经被 KP 回合消费，不会再回到 _run_play；及时释放配额。
        release_action(game, action)


async def _pump_mid_turn(
    game: Game,
    cfg: Config,
    interjections: list[str],
    offenses: list[str],
) -> None:
    """非阻塞清空 action_queue，逐条 _absorb_action。"""
    while True:
        try:
            action = game.action_queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        game.action_queue.task_done()
        action.in_flight = True
        await _absorb_action(game, cfg, action, interjections, offenses)


async def run_kp_turn(
    game: Game,
    cfg: Config,
    actor: Optional[PlayerState],
    instruction: str,
    hint: str = "",
) -> None:
    """KP 智能体循环：提示词 → 工具调用 → 验证执行 → 最终旁白。

    任何失败都落到确定性兜底（关键词自动检定 / 罐头文案），
    绝不卡局。所有状态写入发生在 execute_tool 内的引擎函数里。
    步调同步：回合期间到达的 SAY 经 _pump_mid_turn 吸纳（入群聊、
    记 flag、tick，并以【插话】注入下一轮对话）；其他行动入
    mid_turn_buffer 由 _run_play 于旁白后执行；回合内记到的违规
    在最终旁白之后触发 _world_reaction。
    """
    if not cfg.rpg_ai_enabled:
        await _fallback_narrate(game, instruction, actor)
        return
    interjections: list[str] = []
    offenses: list[str] = []
    # 防刷屏：等待期间不空睡，顺便吸纳新到达的行动
    wait = game.last_kp_at + cfg.rpg_kp_min_interval - _loop_time()
    while wait > 0:
        if game.phase is not Phase.PLAY:
            return
        action = await _get_action(game, min(wait, 0.5))
        if action is not None:
            await _absorb_action(game, cfg, action, interjections, offenses)
        wait = game.last_kp_at + cfg.rpg_kp_min_interval - _loop_time()
    # 剧本概览：整局惰性构建一次拼在系统提示词后（整局逐字节
    # 稳定，落在前缀缓存内）。endings 由引擎组装（模组结局 +
    # 开启时的通用结局），避免 ai_kp 反向导入 engine
    module = game.module
    if not game.kp_overview and module is not None:
        endings = list(module.endings)
        if module.generic_endings:
            endings.extend(_GENERIC_ENDINGS)
        game.kp_overview = ai_kp.build_module_overview(module, endings)
    system_content = ai_kp.build_system_prompt(cfg)
    if game.kp_overview:
        system_content = f"{system_content}\n{game.kp_overview}"
    # 局面拆块：场景块存局上供 get_situation 去重
    scene_block = ai_kp.build_scene_block(game) or "（对局尚未开始）"
    game.kp_situation_scene_block = scene_block
    events_block = ai_kp.build_events_block(game)
    stable = f"{scene_block}\n{events_block}" if events_block else scene_block
    tail = ai_kp.build_volatile_tail(game)
    # user 消息按稳定度降序：半稳定局面（场景块 + 已发生事件）
    # 前置，接续 tools + 系统提示词的整局缓存前缀在回合间命中；
    # 每回合必变的任务指令与时钟/群聊易变尾追加在最后，失效
    # 范围自任务处收缩
    user_content = f"{stable}\n\n【当前任务】{instruction}{hint}\n\n{tail}"
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]
    logger.debug(f"跑团群 {game.group_id} KP 提示词：{user_content}")
    turn_deadline = _loop_time() + cfg.rpg_ai_turn_timeout
    final_text: Optional[str] = None
    injected = 0
    try:
        for _ in range(cfg.rpg_ai_max_tool_rounds):
            if game.phase is not Phase.PLAY:
                return
            remain = turn_deadline - _loop_time()
            if remain <= 1:
                break
            # 回合中吸纳：SAY 入群聊与插话、其他行动入 mid_turn_buffer；
            # 有新插话则以 user 消息注入本轮对话（仅 append，
            # messages[0]/[1] 稳定，FINAL_NUDGE 尾部裁剪依旧有效）
            await _pump_mid_turn(game, cfg, interjections, offenses)
            if len(interjections) > injected:
                messages.append(
                    {
                        "role": "user",
                        "content": _interjection_message(
                            interjections[injected:], offenses
                        ),
                    }
                )
                injected = len(interjections)
            timeout = min(cfg.rpg_kp_timeout, remain)
            if game.tools_broken:
                final_text = await complete(
                    messages,
                    max_tokens=cfg.rpg_kp_max_tokens,
                    temperature=cfg.rpg_kp_temperature,
                    timeout=timeout,
                )
                break
            if game.tools_cache is None:
                module = game.module
                if module is None:
                    final_text = await complete(
                        messages,
                        max_tokens=cfg.rpg_kp_max_tokens,
                        temperature=cfg.rpg_kp_temperature,
                        timeout=timeout,
                    )
                    break
                # 工具 schema 全静态：整局惰性构建一次后复用，
                # 使 wire 前缀（tools + 系统提示词）逐字节稳定
                player_names = [
                    p.sheet.name for p in game.players if p.sheet is not None
                ]
                ending_ids = [ending.id for ending in module.endings]
                if module.generic_endings:
                    ending_ids.extend(ending.id for ending in _GENERIC_ENDINGS)
                game.tools_cache = ai_kp.build_tools(
                    module,
                    player_names,
                    ending_ids=ending_ids,
                )
            msg = await complete_with_tools(
                messages,
                game.tools_cache,
                max_tokens=cfg.rpg_kp_max_tokens,
                temperature=cfg.rpg_kp_temperature,
                timeout=timeout,
            )
            if msg is None:
                # 工具调用失败：降级为纯叙述再试一次，并记住本局不再用工具
                if not game.tools_broken:
                    game.tools_broken = True
                    logger.warning(
                        f"跑团群 {game.group_id} KP 工具调用失败，本局降级为纯叙述模式"
                    )
                # complete_with_tools 可能已耗掉大部分预算，重新计算剩余
                remain = turn_deadline - _loop_time()
                final_text = await complete(
                    messages,
                    max_tokens=cfg.rpg_kp_max_tokens,
                    temperature=cfg.rpg_kp_temperature,
                    timeout=max(remain - 1, 1),
                )
                break
            messages.append(msg)  # pyright: ignore[reportArgumentType]
            if not msg.tool_calls:
                final_text = msg.content
                break
            for tc in msg.tool_calls:
                result = await execute_tool(game, cfg, actor, tc)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    }
                )
                if game.phase is not Phase.PLAY:
                    return
        else:
            # 工具轮数用尽：强制收尾。丢弃全部工具轮对（assistant
            # tool_calls + tool 回执成对丢弃，无悬空引用），只留
            # [系统提示词, 回合开始局面, 收尾指令]——KP 绕圈耗尽轮数
            # 时按回合开始局面出最终旁白，顺带裁掉累积的轮次 payload
            messages = [
                messages[0],
                messages[1],
                {"role": "user", "content": ai_kp.FINAL_NUDGE},
            ]
            remain = turn_deadline - _loop_time()
            if remain > 1:
                final_text = await complete(
                    messages,
                    max_tokens=cfg.rpg_kp_max_tokens,
                    temperature=cfg.rpg_kp_temperature,
                    timeout=min(cfg.rpg_kp_timeout, remain),
                )
    except Exception:  # noqa: BLE001
        # LLM 层可能抛出 OpenAIError 以外的异常（SDK 类型错误等）：
        # 落到尾部兜底叙述，而不是让 run_game 结束整局
        logger.exception(f"跑团群 {game.group_id} KP 回合异常，降级兜底叙述")
        final_text = None
    game.last_kp_at = _loop_time()
    if game.phase is not Phase.PLAY:
        return
    if final_text:
        text = ai_kp.sanitize_narration(final_text, cfg.rpg_kp_max_output_chars)
        if text:
            await _announce(game, text)
            # 回合内吸纳到的违规：NPC 反应在旁白之后
            if offenses:
                await _world_reaction(game, cfg, offenses)
            return
    await _fallback_narrate(game, instruction, actor)
    if offenses:
        await _world_reaction(game, cfg, offenses)


async def execute_tool(
    game: Game,
    cfg: Config,
    actor: Optional[PlayerState],
    tool_call: "ChatCompletionMessageToolCallUnion",
) -> str:
    """验证并执行一次 KP 工具调用，返回回填给模型的中文结果。

    引擎从不信任参数：目标 / 技能 / 场景 / 线索一律全量校验，
    非法调用返回错误描述供 KP 自我纠正，绝不抛出异常。
    """
    function = getattr(tool_call, "function", None)
    if function is None:
        return "不支持的工具调用类型。"
    name = function.name
    try:
        args = json.loads(function.arguments or "{}")
        if not isinstance(args, dict):
            args = {}
    except (json.JSONDecodeError, TypeError):
        return "工具参数不是合法 JSON，请重新调用。"
    logger.info(f"跑团群 {game.group_id} KP 工具调用：{name}({args})")
    module = game.module
    if module is None:
        return "对局未就绪。"
    try:
        if name == "request_check":
            return await _tool_request_check(game, args, actor)
        if name == "request_team_check":
            return await _tool_request_team_check(game, args)
        if name == "san_check":
            return await _tool_san_check(game, cfg, args, actor)
        if name == "deal_damage":
            return await _tool_damage(game, cfg, args, actor, heal=False)
        if name == "heal":
            return await _tool_damage(game, cfg, args, actor, heal=True)
        if name == "transition_scene":
            return await _tool_transition(game, args, actor)
        if name == "grant_clue":
            return await _tool_grant_clue(game, args)
        if name == "speak_as_npc":
            return await _tool_speak_as_npc(game, cfg, args)
        if name == "monster_attack":
            return await _tool_monster_attack(game, args)
        if name == "end_session":
            return await _tool_end_session(game, args)
        if name == "query_story":
            return await _tool_query_story(game, args)
        if name == "get_situation":
            # 只回场景块（回合内时钟不 tick、群聊不变，易变尾纯冗余）；
            # 与回合开始一致时回执"无变化"，避免局面在上下文里翻倍
            fresh = ai_kp.build_scene_block(game) or "（对局尚未开始）"
            if fresh == game.kp_situation_scene_block:
                return (
                    "局面未发生变化（场景/NPC/线索/出口/调查员状态"
                    "均同回合开始时），无需刷新。"
                )
            game.kp_situation_scene_block = fresh
            return fresh
    except Exception:  # noqa: BLE001
        # 工具处理器的一切异常都转化为错误回执，供 KP 自我纠正；
        # 向上抛只会让 run_game 兜底整局结束，违背"任何失败不卡局"
        logger.exception(f"跑团群 {game.group_id} 工具 {name} 执行异常")
        return "工具执行出现异常，请改用其他方式推进剧情。"
    else:
        return f"未知工具 {name}。"


def _difficulty_arg(args: dict[str, object]) -> CheckDifficulty:
    """解析难度参数，非法值按常规处理。"""
    raw = str(args.get("difficulty", "regular")).strip()
    for diff in CheckDifficulty:
        if diff.value == raw:
            return diff
    return CheckDifficulty.REGULAR


async def _tool_request_check(
    game: Game,
    args: dict[str, object],
    actor: Optional[PlayerState],
) -> str:
    """request_check：系统掷骰的技能检定。"""
    skill = resolve_skill(str(args.get("skill", "")))
    if skill is None:
        return "技能不存在，请使用工具参数中列出的技能名。"
    if skill.key == "cthulhu_mythos":
        return "克苏鲁神话不能主动检定。"
    if skill.key == "san":
        return "理智检定请改用 san_check 工具（需给出损失骰）。"
    player = _resolve_player_arg(game, _opt_str(args.get("player")), actor)
    if player is None or player.incapped:
        return "目标调查员不存在或已失去行动能力。"
    difficulty = _difficulty_arg(args)
    success = await do_skill_check(game, player, skill.key, difficulty)
    if success is None:
        return "该调查员没有这项技能。"
    return f"系统已播报检定结果：{'成功' if success else '失败'}。"


async def _tool_request_team_check(game: Game, args: dict[str, object]) -> str:
    """KP 触发的团队检定：参与者和成功数均由系统确定与钳制。"""
    skill = resolve_skill(str(args.get("skill", "")))
    if skill is None or skill.key in {"san", "cthulhu_mythos"}:
        return "技能不存在或不能用于团队检定。"
    participants = [p for p in game.players if not p.incapped]
    if not participants:
        return "当前没有可参与的调查员。"
    raw_required = args.get("required_successes", math.ceil(len(participants) / 2))
    try:
        requested = int(str(raw_required))
    except (TypeError, ValueError):
        requested = math.ceil(len(participants) / 2)
    needed = max(1, min(requested, len(participants)))
    success = await do_team_skill_check(game, skill.key, _difficulty_arg(args), needed)
    return f"团队检定已结算：{'成功' if success else '失败'}。"


async def _tool_san_check(
    game: Game,
    cfg: Config,
    args: dict[str, object],
    actor: Optional[PlayerState],
) -> str:
    """san_check：理智检定 + 系统钳制的损失。"""
    success_loss = str(args.get("success_loss", "")).strip()
    failure_loss = str(args.get("failure_loss", "")).strip()
    if not (is_valid_dice_expr(success_loss) and is_valid_dice_expr(failure_loss)):
        return "损失骰表达式非法（示例：1、1d3、1d6+1）。"
    player = _resolve_player_arg(game, _opt_str(args.get("player")), actor)
    if player is None or player.incapped:
        return "目标调查员不存在或已失去行动能力。"
    ok = await _roll_san(
        game,
        player,
        f"{success_loss}/{failure_loss}",
        source="KP",
        clamp=cfg.rpg_ai_max_san_loss,
    )
    return f"理智检定已结算：{'成功' if ok else '失败'}，损失已被系统钳制。"


async def _tool_damage(
    game: Game,
    cfg: Config,
    args: dict[str, object],
    actor: Optional[PlayerState],
    *,
    heal: bool,
) -> str:
    """deal_damage / heal：钳制上限后应用。"""
    try:
        amount = int(args.get("amount", 0))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "amount 必须是整数。"
    if amount <= 0:
        return "数值必须为正数。"
    amount = min(amount, cfg.rpg_ai_max_damage_per_call)
    player = _resolve_player_arg(game, _opt_str(args.get("player")), actor)
    if player is None:
        return "目标调查员不存在。"
    if heal:
        await apply_heal(game, player, amount)
        name = player.sheet.name if player.sheet else "目标"
        if player.incapped:
            return (
                f"已为 {name} 治疗（上限 {amount}），但其已失去行动能力，仍无法行动。"
            )
        return f"已为 {name} 治疗（上限 {amount}）。"
    if player.incapped:
        return "目标已失去行动能力，无需再造成伤害。"
    reason = _opt_str(args.get("reason")) or "未知危险"
    await apply_damage(game, player, amount, source=reason)
    return f"已造成 {amount} 点伤害（系统单次上限 {cfg.rpg_ai_max_damage_per_call}）。"


async def _tool_transition(
    game: Game,
    args: dict[str, object],
    actor: Optional[PlayerState],
) -> str:
    """transition_scene：出口存在性与条件校验。"""
    if game.module is None or game.current_scene is None:
        return "当前不在场景中。"
    scene = game.module.scene(game.current_scene)
    if scene is None:
        return "当前场景缺失。"
    target_id = str(args.get("scene_id", ""))
    chosen = next((ex for ex in scene.exits if ex.to_scene == target_id), None)
    if chosen is None:
        return "目标不是当前场景的出口，不能切换。"
    target = game.module.scene(target_id)
    if evaluate_condition(chosen.condition, game.condition_context()):
        if not await _transition_exit(game, actor, chosen):
            return "场景切换失败（目标场景缺失），请重新查询局面。"
        name = target.name if target is not None else target_id
        return f"已切换到场景「{name}」，转场文案已播报。"
    name = target.name if target is not None else target_id
    return (
        f"出口「{name}」当前条件未满足，无法切换。"
        "请叙述阻碍（如门锁着），不要反复重试。"
    )


async def _tool_grant_clue(game: Game, args: dict[str, object]) -> str:
    """grant_clue：仅限当前场景可授予范围。"""
    if game.module is None or game.current_scene is None:
        return "当前不在场景中。"
    scene = game.module.scene(game.current_scene)
    if scene is None:
        return "当前场景缺失。"
    clue_id = str(args.get("clue_id", ""))
    grantable = {
        cp.clue
        for cp in scene.checks
        # once 检定点已触发却失败时线索不可授予：系统已裁决结果，
        # KP 不得以 grant_clue 覆盖失败检定（未裁决的检定点仍可授予）
        if cp.clue
        and not (cp.id in game.fired_checks and cp.id not in game.passed_checks)
    }
    for mid in scene.monsters:
        if mid not in game.dead_monsters:
            continue  # 死亡奖励线索只能在怪物死后授予，否则提前剧透
        monster = game.module.monster(mid)
        if monster is not None and monster.on_death_clue:
            grantable.add(monster.on_death_clue)
    if clue_id not in grantable:
        return "该线索不在当前场景的可授予范围内。"
    if clue_id in game.discovered_clues:
        return "该线索已经被发现过了。"
    if await discover_clue(game, clue_id):
        return "线索已播报给全体调查员。"
    return "线索不存在。"


async def _tool_speak_as_npc(
    game: Game, cfg: Config, args: dict[str, object]
) -> str:
    """speak_as_npc：NPC 在场校验 + NPC 智能体生成台词 + 格式化播报。

    KP 只给意图（intent），台词由 ai_npc 按 NPC 自己的提示词
    （persona/knows/secrets）生成——视角分离。降级：AI 关或
    生成失败 → fallback_line。
    """
    if game.module is None or game.current_scene is None:
        return "当前不在场景中。"
    scene = game.module.scene(game.current_scene)
    if scene is None:
        return "当前场景缺失。"
    npc_id = str(args.get("npc_id", ""))
    npc = game.module.npc(npc_id)
    if npc is None:
        return "该 NPC 不存在。"
    if npc_id in game.dead_npcs:
        return "该 NPC 已经死了，无法开口。"
    found = game.npc_present(npc_id)
    if found is None:
        return "该 NPC 不在当前场景。"
    intent = _opt_str(args.get("intent"))
    if intent is None:
        return "intent 为空。请给出这句话的意图（如：警告别下地下室）。"
    activity = found[1]
    line = None
    if cfg.rpg_ai_enabled:
        line = await ai_npc.generate_npc_line(
            game, cfg, npc, activity, f"KP 指示：{intent}"
        )
    if not line:
        line = npc.fallback_line or "（对方似乎不想多说。）"
    clean_line = ai_npc.sanitize_npc_line(line)
    # KP 工具只能产生公开台词；它不经过社交结算，也不修改关系。
    game.append_npc_context(npc.id, f"NPC {npc.name}：{clean_line}")
    await _announce(game, f"【{npc.name}】{clean_line}")
    return f"已以 {npc.name} 名义播报（台词由系统按其人格生成）。"


async def _tool_monster_attack(
    game: Game,
    args: dict[str, object],
) -> str:
    """monster_attack：怪物在场存活校验 + 对抗结算。"""
    if game.module is None or game.current_scene is None:
        return "当前不在场景中。"
    scene = game.module.scene(game.current_scene)
    if scene is None:
        return "当前场景缺失。"
    monster_id = str(args.get("monster_id", ""))
    monster = game.module.monster(monster_id)
    if monster is None or monster_id not in scene.monsters:
        return "该怪物不在当前场景。"
    if monster_id in game.dead_monsters:
        return "该怪物已经死了。"
    target = _resolve_player_arg(game, _opt_str(args.get("target")), None)
    if target is not None and target not in game.active_players():
        target = None
    # target 缺省传 None → 引擎随机选取，与工具 schema"缺省随机"一致
    return await do_monster_attack(game, monster, target)


async def _tool_end_session(game: Game, args: dict[str, object]) -> str:
    """end_session：结局条件复核。"""
    if game.module is None:
        return "对局未就绪。"
    ending_id = str(args.get("ending_id", ""))
    endings = list(game.module.endings)
    if game.module.generic_endings:
        endings.extend(_GENERIC_ENDINGS)
    ending = next((e for e in endings if e.id == ending_id), None)
    if ending is None:
        return "结局不存在。"
    if not evaluate_condition(ending.condition, game.condition_context()):
        return "结局条件尚未满足，不能结束。继续推进剧情。"
    await do_ending(game, ending)
    return "结局已播报，对局结束。"


async def _tool_query_story(game: Game, args: dict[str, object]) -> str:
    """query_story：结局 / 具名事件的 KP 向说明回执（不播报给玩家）。

    名称或 id 精确匹配。结局返 名称 + 来龙去脉 + 倾向（不返
    condition，守住防剧透边界——触发语境由作者在 summary 里写）；
    事件返 名称 + 来龙去脉。
    """
    module = game.module
    if module is None:
        return "对局未就绪。"
    q = _opt_str(args.get("name"))
    if q is None:
        return "请给出要查询的结局或事件名称（见【剧本概览】[结局]/[事件]）。"
    endings = list(module.endings)
    if module.generic_endings:
        endings.extend(_GENERIC_ENDINGS)
    for ending in endings:
        if q in (ending.display_name, ending.id):
            summary = ending.summary or "（作者未撰写说明）"
            return (
                f"结局 · {ending.display_name}\n"
                f"来龙去脉：{summary}\n"
                f"倾向：{ending.outcome}"
            )
    for event in module.events:
        if q in (event.name, event.id):
            summary = event.summary or "（作者未撰写说明）"
            return f"事件 · {event.name}\n来龙去脉：{summary}"
    return "未找到该名称的结局或事件，请查看【剧本概览】的 [结局]/[事件]。"


def _opt_str(value: object) -> Optional[str]:
    """参数可选字符串化（None / 空串返回 None）。"""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


# ── PLAY 主循环 ───────────────────────────────────────────


async def _world_reaction(game: Game, cfg: Config, offenses: list[str]) -> None:
    """违规世界反应：在场首个 NPC 按人格一句反应。

    镜像 AI-off 罐线路径结构：AI 开 → ai_npc 生成；AI 关 /
    生成失败 → 固定惊呼罐头（非 fallback_line——那是聊天搪塞语，
    作纵火反应是退化）；无 NPC 在场 → 通用恐慌文案。
    """
    labels = "、".join(_OFFENSE_LABELS[c] for c in offenses)
    present = game.npcs_in_scene(game.current_scene or "")
    if not present:
        await _announce(game, "你们的举动引起了周围极大的恐慌。")
        return
    npc, activity = present[0]
    line = None
    if cfg.rpg_ai_enabled:
        directive = (
            f"你刚刚目睹了调查员尝试{labels}。"
            "立即按其人格反应（阻止/呼救/逃跑/敌视），世界必须有回应。"
        )
        line = await ai_npc.generate_npc_line(game, cfg, npc, activity, directive)
    if not line:
        line = "住手！你们这是要干什么！来人啊！"
    clean_line = ai_npc.sanitize_npc_line(line)
    game.append_npc_context(npc.id, f"NPC {npc.name}：{clean_line}")
    await _announce(game, f"【{npc.name}】{clean_line}")


async def _collect_say_batch(
    game: Game,
    cfg: Config,
    first: Action,
) -> list[Action]:
    """先收集固定窗口内的原始 SAY，再并行分类并按首个 NPC 边界拆分。"""
    raw = [first]
    deadline = _loop_time() + cfg.rpg_say_settle_window
    while len(raw) < cfg.rpg_kp_max_batch_lines:
        step = deadline - _loop_time()
        if step <= 0:
            break
        action = await _get_action(game, step)
        if action is None:
            continue
        if action.kind is ActionKind.SAY:
            raw.append(action)
        else:
            game.pending = action  # 首个非 SAY 行动暂存，下轮处理
            break
    await asyncio.gather(*(_classify_say(game, cfg, action) for action in raw))
    first_npc = next(
        (index for index, action in enumerate(raw) if action.route in _NPC_ROUTE_NAMES),
        None,
    )
    if first_npc is None:
        return raw
    if first_npc == 0:
        game.mid_turn_buffer.extend(raw[1:])
        return raw[:1]
    game.mid_turn_buffer.extend(raw[first_npc:])
    return raw[:first_npc]


async def _handle_say(
    game: Game,
    cfg: Config,
    first: Action,
    batch: Optional[list[Action]] = None,
) -> bool:
    """处理自由发言：合批 → 违规扫描 → 触发提示 → KP 智能体循环。"""
    if batch is None:
        batch = await _collect_say_batch(game, cfg, first)
    if len(batch) == 1 and batch[0].route in _NPC_ROUTE_NAMES:
        first = batch[0]
        player = game.player_by_user(first.actor_user_id)
        if player is not None and first.target_id is not None:
            return await _handle_npc_interaction(
                game,
                cfg,
                player,
                first.target_id,
                first.aux or "",
                route=first.route or "npc_talk",
                social_node_id=first.social_node_id,
                social_skill=first.social_skill,
                emotion=first.emotion,
                emotion_confidence=first.emotion_confidence,
            )
        # 分类器不应产生无目标 NPC 路由；安全降级为 KP 普通发言。
        first.route = "kp_say"
    try:
        lines: list[str] = []
        hint = ""
        offenses: list[str] = []
        for action in batch:
            player = game.player_by_user(action.actor_user_id)
            if player is not None and player.sheet is not None:
                name = player.sheet.name
            else:
                name = str(action.actor_user_id)
            text = (action.aux or "").strip()
            if len(text) > cfg.rpg_speech_truncate:
                text = text[: cfg.rpg_speech_truncate] + "……"
            if not text:
                continue
            game.group_log.append(f"【{name}】{text}")
            lines.append(f"{name}：{text}")
            if not hint:
                hint = _trigger_hint(game, text)
            offense = _scan_offense(text)
            if offense is not None and offense not in offenses:
                offenses.append(offense)
        if not lines:
            return False
        # 违规标记先于 tick 记录：flag 驱动的行程变化（如 NPC 吓跑）
        # 能在本次 tick 的进出 diff 里立即体现
        for category in offenses:
            raise_flag(game, category)
        # 每批发言整体消耗一次时间（在 KP 回合之前 tick，让局面反映最新时刻）
        await _tick_time(game, _time_cost(game, "say"))
        speaker = game.player_by_user(first.actor_user_id)
        instruction = (
            "调查员的发言如下：\n"
            + "\n".join(lines)
            + "\n请即兴续写氛围或 NPC 反应。"
        )
        # 违规世界反应走 NPC 智能体（AI 关为罐头），两路统一，先于 KP 旁白
        if offenses:
            await _world_reaction(game, cfg, offenses)
        await run_kp_turn(game, cfg, speaker, instruction, hint=hint)
        return True
    finally:
        for queued in batch[1:]:
            release_action(game, queued)


async def _handle_explicit_check(game: Game, player: PlayerState, aux: str) -> bool:
    """处理 /检定：确定性路径，不经 AI。"""
    parts = aux.split()
    difficulty = CheckDifficulty.REGULAR
    diff_map = {
        "困难": CheckDifficulty.HARD,
        "极难": CheckDifficulty.EXTREME,
        "hard": CheckDifficulty.HARD,
        "extreme": CheckDifficulty.EXTREME,
    }
    if parts and parts[-1].casefold() in diff_map:
        difficulty = diff_map[parts[-1].casefold()]
        parts = parts[:-1]
    skill = resolve_skill(" ".join(parts)) if parts else None
    if skill is None:
        await _announce(game, "格式：/检定 技能名（如 /检定 侦查，可加 困难/极难）")
        return False
    if skill.key == "cthulhu_mythos":
        # 与 request_check 工具侧一致：克苏鲁神话不可主动检定
        await _announce(game, "克苏鲁神话不能主动检定~")
        return False
    success = await do_skill_check(game, player, skill.key, difficulty)
    if success is None:
        await _announce(game, f"你的角色卡上没有「{skill.name}」这项技能~")
        return False
    await _tick_time(game, _time_cost(game, "check"))
    return True


def _social_delta(
    strategy: SocialStrategy,
    node: SocialNode,
    field: str,
) -> int:
    """读取策略覆写值，否则回退到节点默认值。"""
    value = getattr(strategy, field)
    return getattr(node, field) if value is None else value


def _social_text(
    strategy: SocialStrategy,
    node: SocialNode,
    field: str,
) -> str:
    """读取策略覆写文案，否则回退到节点文案。"""
    value = getattr(strategy, field)
    return getattr(node, field) if value is None else value


async def _deliver_social_rewards(
    game: Game,
    player: PlayerState,
    npc: NPC,
    node: SocialNode,
    *,
    success: bool,
) -> None:
    """发放社交节点奖励；私人正文只进入私聊，不进入群聊或 NPC 上下文。"""
    if game.module is None:
        return
    reward_key = (npc.id, player.user_id, node.id, success)
    if reward_key in game.npc_social_rewards:
        return
    game.npc_social_rewards.add(reward_key)
    if success:
        fact_ids = game.npc_unlocked_facts.setdefault((npc.id, player.user_id), set())
        new_facts = [
            fact
            for fact in npc.facts
            if fact.id in node.unlock_facts and fact.id not in fact_ids
        ]
        fact_ids.update(fact.id for fact in new_facts)
        if new_facts:
            fact_text = "\n\n".join(
                f"〔NPC 情报〕{fact.name}\n{fact.text}" for fact in new_facts
            )
            sent = await _dm(game, player, fact_text)
            if not sent and game.bot is None:
                await _announce(
                    game,
                    MessageSegment.at(player.user_id)
                    + " 获得了 NPC 私人情报，但私聊发送失败，请加机器人为好友。",
                )
        for clue_id in node.private_clues:
            await discover_clue(game, clue_id, owner=player)
        for clue_id in node.public_clues:
            if clue_id in game.public_clues:
                continue
            if clue_id in game.discovered_clues:
                clue = game.module.clue(clue_id)
                if clue is not None:
                    game.public_clues.add(clue_id)
                    await _announce(game, f"〔线索〕{clue.name}\n{clue.text}")
            else:
                await discover_clue(game, clue_id)
        for flag in node.success_flags:
            raise_flag(game, flag)
    else:
        for flag in node.failure_flags:
            raise_flag(game, flag)


async def _resolve_social_action(
    game: Game,
    player: PlayerState,
    npc: NPC,
    node: SocialNode,
    skill_key: str,
) -> str:
    """校验并结算一次模组声明的社交节点，返回给 NPC 的公开反应指令。"""
    strategy = node.strategy(skill_key)
    if strategy is None:
        return "调查员的交涉方式不符合当前诉求；保持 NPC 的自然态度回应。"
    if _player_skill(player, strategy.skill) is None:
        return "调查员没有这项交涉技能；保持 NPC 的自然态度回应。"
    unlocked = game.npc_unlocked_facts.get((npc.id, player.user_id), set())
    unlocked = unlocked | game.npc_public_facts.get(npc.id, set())
    if not set(node.requires_facts).issubset(unlocked):
        await _announce(game, f"{npc.name} 似乎还不愿谈及这件事。")
        return "调查员尚未掌握足够背景；不要透露隐藏情报，只按人格婉拒。"
    rapport = game.npc_rapport_value(npc.id, player.user_id)
    attitude = game.npc_attitude_value(npc.id)
    if rapport < node.min_rapport or attitude < node.min_attitude:
        await _announce(game, f"{npc.name} 对这个请求仍保持距离。")
        return "调查员与 NPC 的关系尚未达到要求；不要透露隐藏情报，只按人格婉拒。"
    key = (npc.id, player.user_id, node.id)
    attempt = game.npc_social_attempts.get(key, 0)
    if attempt >= node.max_attempts:
        await _announce(game, f"{npc.name} 已经不愿再回应这件事了。")
        return "这个社交诉求已经没有新的尝试机会；按 NPC 人格冷淡收束。"
    attempt += 1
    game.npc_social_attempts[key] = attempt
    success = await do_skill_check(game, player, strategy.skill, strategy.difficulty)
    if success is None:
        return "系统无法完成这次社交检定；保持 NPC 的自然态度回应。"
    if success:
        rapport_delta = _social_delta(strategy, node, "success_rapport_delta")
        attitude_delta = _social_delta(strategy, node, "success_attitude_delta")
    else:
        rapport_delta = _social_delta(strategy, node, "failure_rapport_delta")
        attitude_delta = _social_delta(strategy, node, "failure_attitude_delta")
        rapport_delta -= node.retry_rapport_penalty * (attempt - 1)
        attitude_delta -= node.retry_attitude_penalty * (attempt - 1)
    rapport_band, attitude_band, public_changed = _apply_relation_delta(
        game,
        npc,
        player.user_id,
        rapport_delta,
        attitude_delta,
    )
    text_field = "success_text" if success else "failure_text"
    fixed_text = _social_text(strategy, node, text_field)
    if fixed_text:
        await _announce(game, fixed_text)
    await _deliver_social_rewards(game, player, npc, node, success=success)
    if public_changed:
        await _announce(game, f"{npc.name} 对调查员们的态度变为：{attitude_band}。")
    await _dm(
        game,
        player,
        f"〔社交反馈〕你与 {npc.name} 的关系：{rapport_band}。",
    )
    result = "成功" if success else "失败"
    return (
        f"调查员尝试以{strategy.name or strategy.skill}处理「{node.name}」，系统裁决：{result}。"
        f"不要透露私人情报，只根据当前关系和 NPC 人格回应。"
    )


async def _handle_npc_interaction(
    game: Game,
    cfg: Config,
    player: PlayerState,
    npc_id: str,
    text: str,
    *,
    route: str = "npc_talk",
    social_node_id: Optional[str] = None,
    social_skill: Optional[str] = None,
    emotion: Optional[str] = None,
    emotion_confidence: float = 0.0,
) -> bool:
    """统一处理自然语言 NPC 对话、社交节点和 NPC 上下文写入。"""
    found = _current_npc(game, npc_id)
    if found is None or game.module is None:
        if npc_id in game.dead_npcs and game.module is not None:
            dead = game.module.npc(npc_id)
            if dead is not None:
                await _announce(game, f"……{dead.name} 已经死了。")
                return False
        await _announce(game, "这个 NPC 不在当前场景里。")
        return False
    npc, activity = found
    text = text.strip()
    if not text:
        await _announce(game, "请直接说出你想对 NPC 说的话。")
        return False
    if len(text) > cfg.rpg_speech_truncate:
        text = text[: cfg.rpg_speech_truncate] + "……"
    pname = player.sheet.name if player.sheet else str(player.seat)
    game.npc_focus[player.user_id] = npc.id
    game.group_log.append(f"【{pname}→{npc.name}】{text}")
    game.append_npc_context(npc.id, f"玩家 {pname}：{text}")
    await _tick_time(game, _time_cost(game, "talk"))
    directive = f"调查员 {pname} 对你说：「{text}」"
    if route == "social_action" and social_node_id and social_skill:
        node = next(
            (item for item in npc.social_nodes if item.id == social_node_id),
            None,
        )
        if node is not None:
            directive += "\n" + await _resolve_social_action(
                game,
                player,
                npc,
                node,
                social_skill,
            )
    else:
        await _apply_emotion(
            game,
            cfg,
            npc,
            player.user_id,
            emotion,
            emotion_confidence,
        )
    line = None
    if cfg.rpg_ai_enabled:
        line = await ai_npc.generate_npc_line(
            game,
            cfg,
            npc,
            activity,
            directive,
            player.user_id,
        )
    if not line:
        line = npc.fallback_line or "（对方似乎不想多说。）"
    clean_line = ai_npc.sanitize_npc_line(line)
    game.append_npc_context(npc.id, f"NPC {npc.name}：{clean_line}")
    await _announce(game, f"【{npc.name}】{clean_line}")
    return True


async def _handle_talk_npc(
    game: Game,
    cfg: Config,
    player: PlayerState,
    aux: str,
) -> bool:
    """兼容内部旧 Action 的 NPC 对话处理；玩家命令入口已移除。"""
    npc_name, _, text = aux.partition(" ")
    target_id = _deterministic_npc_target(game, player.user_id, npc_name)
    if target_id is None:
        await _announce(game, f"{npc_name or '这个 NPC'} 不在这个场景里。")
        return False
    return await _handle_npc_interaction(game, cfg, player, target_id, text)


async def _handle_wait(game: Game, minutes: int) -> bool:
    """/等待：固定文案 + 时间推进。

    不走 KP 回合（等待是廉价操作，防刷屏）；时间流逝带来的
    世界反应由 _tick_time 的 NPC 进出播报承担。
    """
    minutes = max(minutes, 1)
    await _announce(game, f"（你们等待了 {minutes} 分钟……）")
    await _tick_time(game, minutes)
    return True


def _is_major_action(action: Action) -> bool:
    if action.kind is ActionKind.SAY:
        return action.route in _NPC_ROUTE_NAMES
    return action.kind in {
        ActionKind.CHECK,
        ActionKind.TALK_NPC,
        ActionKind.ATTACK,
        ActionKind.MOVE,
        ActionKind.WAIT,
        ActionKind.ASSIST,
        ActionKind.PASS_TURN,
    }


def _action_stale(game: Game, _cfg: Config, action: Action) -> bool:
    """执行前复核逻辑局面快照，不按排队秒数淘汰动作。"""
    if action.expected_phase is not None and action.expected_phase is not game.phase:
        return True
    if (
        action.expected_scene is not None
        and action.expected_scene != game.current_scene
    ):
        return True
    if action.expected_combat_round is not None:
        current_uid = (
            game.combat_order[game.combat_index]
            if game.combat_order and 0 <= game.combat_index < len(game.combat_order)
            else None
        )
        return (
            not game.combat_order
            or game.combat_round != action.expected_combat_round
            or current_uid != action.expected_combat_actor
        )
    if action.expected_explore_round is not None:
        return bool(game.combat_order) or game.explore_round != action.expected_explore_round
    return False


async def _handle_assist(game: Game, player: PlayerState, aux: str) -> bool:
    """登记一次同场景协助，供目标下一次匹配检定取奖励骰。"""
    target_text, sep, skill_text = aux.partition("|")
    skill = resolve_skill(skill_text.strip())
    if not sep or skill is None or skill.key in {"san", "cthulhu_mythos"}:
        await _announce(game, "格式：/协助 玩家 技能（如 /协助 阿明 侦查）")
        return False
    target = next(
        (
            p
            for p in game.players
            if p.sheet is not None and target_text.strip() in p.sheet.name
        ),
        None,
    )
    if target is None or target.incapped or target.user_id == player.user_id:
        await _announce(game, "协助目标必须是另一名可行动的调查员。")
        return False
    matching = [
        item
        for item in game.assists
        if item[0] == target.user_id and item[1] == skill.key
    ]
    if len(matching) >= 2:
        await _announce(game, "这次检定已经获得两名调查员协助。")
        return False
    game.assists.append(
        (
            target.user_id,
            skill.key,
            game.current_scene or "",
            game.explore_round,
            player.user_id,
        )
    )
    await _announce(
        game,
        f"{player.sheet.name if player.sheet else player.seat} 协助 {target.sheet.name if target.sheet else target.seat} 进行{skill.name}检定。",
    )
    return True


async def _handle_share_clue(game: Game, player: PlayerState, aux: str) -> bool:
    if game.module is None:
        return False
    needle = aux.strip()
    clue_id = next(
        (
            cid
            for cid, owners in game.clue_owners.items()
            if player.user_id in owners
            and (
                cid == needle
                or ((clue := game.module.clue(cid)) is not None and needle in clue.name)
            )
        ),
        None,
    )
    if clue_id is None:
        await _announce(
            game, MessageSegment.at(player.user_id) + " 你没有这条个人线索。"
        )
        return False
    clue = game.module.clue(clue_id)
    if clue is None:
        return False
    game.public_clues.add(clue_id)
    await _announce(game, f"〔线索分享〕{clue.name}\n{clue.text}")
    return True


async def _handle_share_fact(game: Game, player: PlayerState, aux: str) -> bool:
    """公开当前玩家已从 NPC 获得的个人情报。"""
    if game.module is None:
        return False
    npc_text, sep, fact_text = aux.partition("|")
    npc_needle = npc_text.strip()
    fact_needle = fact_text.strip()
    if not sep or not npc_needle or not fact_needle:
        await _announce(game, "格式：/分享情报 NPC名 情报名")
        return False
    npc = next(
        (
            item
            for item in game.module.npcs
            if item.id == npc_needle or npc_needle in item.name
        ),
        None,
    )
    if npc is None:
        await _announce(game, f"没有找到 NPC「{npc_needle}」。")
        return False
    owned = game.npc_unlocked_facts.get((npc.id, player.user_id), set())
    fact = next(
        (
            item
            for item in npc.facts
            if item.id in owned
            and (item.id == fact_needle or fact_needle in item.name)
        ),
        None,
    )
    if fact is None:
        await _announce(game, MessageSegment.at(player.user_id) + " 你没有这条 NPC 情报。")
        return False
    public = game.npc_public_facts.setdefault(npc.id, set())
    if fact.id in public:
        await _announce(game, f"这条关于 {npc.name} 的情报已经公开过了。")
        return False
    public.add(fact.id)
    # 公开后才允许正文进入该 NPC 的公开上下文。
    game.append_npc_context(npc.id, f"〔公开情报〕{fact.name}：{fact.text}")
    await _announce(game, f"〔情报分享〕{npc.name}：{fact.name}\n{fact.text}")
    return True


async def _delayed_ai_wait_notice(game: Game, cfg: Config) -> None:
    """自然语言处理超过阈值时只发一次临时等待提示。"""
    if not cfg.rpg_ai_enabled:
        return
    try:
        await asyncio.sleep(max(0.0, cfg.rpg_ai_wait_notice_delay))
        if game.phase is Phase.PLAY:
            await _announce_ephemeral(game, "KP 正在整理局面，请稍候……")
    except asyncio.CancelledError:
        return


async def _start_combat(game: Game, cfg: Config) -> bool:
    if game.combat_order:
        return False
    combatants = [p for p in game.players if not p.incapped and p.sheet is not None]
    combatants.sort(
        key=lambda p: (-(p.sheet.attributes["dex"] if p.sheet else 0), p.seat)
    )
    game.combat_order = [p.user_id for p in combatants]
    game.combat_index = 0
    game.combat_round = 1
    game.combat_deadline = _loop_time() + cfg.rpg_combat_turn_timeout
    order = [
        _player_name(player)
        for user_id in game.combat_order
        if (player := game.player_by_user(user_id)) is not None
    ]
    current = game.player_by_user(game.combat_order[game.combat_index])
    await _announce(
        game,
        f"〔战斗第 1 轮〕行动顺序：{' → '.join(order)}。"
        f"轮到 {_player_name(current) if current is not None else '调查员'}；"
        "可 /攻击 目标 或 /跳过。",
    )
    return True


async def _advance_combat(game: Game, cfg: Config) -> None:
    if not _combat_has_targets(game):
        game.combat_order.clear()
        game.combat_index = 0
        game.start_explore_round(cfg.rpg_explore_round_timeout)
        await _announce(game, "战斗结束，重新进入探索轮次。\n" + _explore_prompt_text(game))
        return
    current_uid = (
        game.combat_order[game.combat_index]
        if game.combat_order and 0 <= game.combat_index < len(game.combat_order)
        else None
    )
    old_index = game.combat_index
    game.combat_order = [
        uid for uid in game.combat_order if uid in game.active_user_ids()
    ]
    if not game.combat_order:
        return
    if current_uid in game.combat_order:
        game.combat_index = (game.combat_order.index(current_uid) + 1) % len(
            game.combat_order
        )
    else:
        game.combat_index = old_index % len(game.combat_order)
    if game.combat_index == 0:
        game.combat_round += 1
    game.combat_deadline = _loop_time() + cfg.rpg_combat_turn_timeout
    next_player = game.player_by_user(game.combat_order[game.combat_index])
    if next_player is not None:
        await _announce(
            game,
            f"〔战斗第 {game.combat_round} 轮〕轮到 {next_player.sheet.name if next_player.sheet else next_player.seat} 行动。",
        )


def _combat_has_targets(game: Game) -> bool:
    """当前场景仍有存活怪物或敌对 NPC 时才保持战斗状态。"""
    if game.module is None or game.current_scene is None:
        return False
    scene = game.module.scene(game.current_scene)
    if scene is None:
        return False
    if any(monster_id not in game.dead_monsters for monster_id in scene.monsters):
        return True
    return any(
        npc_id not in game.dead_npcs and game.npc_present(npc_id) is not None
        for npc_id in game.npc_hostile
    )


async def _process_action(
    game: Game,
    cfg: Config,
    action: Action,
    player: PlayerState,
    *,
    say_batch: Optional[list[Action]] = None,
) -> bool:
    """PLAY 阶段行动分发。"""
    if game.combat_order:
        # 群消息本身已经对全群可见；战斗中不再把自然语言交给
        # KP/NPC 工具链，避免借发言触发检定、社交或切换场景。
        if action.kind is ActionKind.SAY:
            return False
        current_uid = game.combat_order[game.combat_index]
        if action.kind not in {ActionKind.ATTACK, ActionKind.PASS_TURN}:
            await _announce(
                game,
                MessageSegment.at(player.user_id)
                + " 战斗中只能攻击或结束当前回合。",
            )
            return False
        if current_uid != player.user_id:
            await _announce(
                game,
                MessageSegment.at(player.user_id) + " 现在不是你的战斗行动时机。",
            )
            return False
    if action.kind is ActionKind.SAY:
        return await _handle_say(game, cfg, action, say_batch)
    elif action.kind is ActionKind.CHECK:
        return await _handle_explicit_check(game, player, action.aux or "")
    elif action.kind is ActionKind.TALK_NPC:
        return await _handle_talk_npc(game, cfg, player, action.aux or "")
    elif action.kind is ActionKind.ATTACK:
        target = _find_attack_target(game, action.aux or "")
        if target is None:
            await _announce(game, "这个场景里没有你说的目标。")
            return False
        started = await _start_combat(game, cfg)
        if started and isinstance(target, NPC):
            game.npc_hostile.add(target.id)
        if game.combat_order and game.combat_order[game.combat_index] != player.user_id:
            if started:
                return True
            await _announce(
                game, MessageSegment.at(player.user_id) + " 现在不是你的战斗行动时机。"
            )
            return False
        if isinstance(target, NPC):
            # 攻击 NPC 是暴力行为：assault 标记先于 tick 记录
            # （镜像 SAY 路径的"先于 tick"契约），flag 驱动的行程
            # 变化（如 NPC 吓跑）才能在本次 tick 的进出 diff 里立即
            # 播报；幸存的 NPC 立即确定性反击（镜像怪物行为）
            raise_flag(game, "assault")
            _, attitude_band, public_changed = _apply_relation_delta(
                game,
                target,
                player.user_id,
                -40,
                -30,
            )
            if public_changed:
                await _announce(
                    game,
                    f"{target.name} 对调查员们的态度变为：{attitude_band}。",
                )
            await _tick_time(game, _time_cost(game, "attack"))
            await do_player_attack_npc(game, player, target)
            if target.id not in game.dead_npcs:
                await do_npc_attack(game, target, player)
        else:
            await _tick_time(game, _time_cost(game, "attack"))
            await do_player_attack(game, player, target)
        await _advance_combat(game, cfg)
        return True
    elif action.kind is ActionKind.MOVE:
        return await _do_move(game, player, action.aux or "")
    elif action.kind is ActionKind.WAIT:
        return await _handle_wait(game, action.value or cfg.rpg_wait_default)
    elif action.kind is ActionKind.ASSIST:
        return await _handle_assist(game, player, action.aux or "")
    elif action.kind is ActionKind.SHARE_CLUE:
        return await _handle_share_clue(game, player, action.aux or "")
    elif action.kind is ActionKind.SHARE_FACT:
        return await _handle_share_fact(game, player, action.aux or "")
    elif action.kind is ActionKind.PASS_TURN:
        if game.combat_order:
            if game.combat_order[game.combat_index] != player.user_id:
                await _announce(
                    game,
                    MessageSegment.at(player.user_id) + " 现在不是你的战斗行动时机。",
                )
                return False
            await _announce(
                game,
                f"{player.sheet.name if player.sheet else player.seat} 采取防御姿态。",
            )
            await _advance_combat(game, cfg)
            return True
        else:
            await _announce(
                game, MessageSegment.at(player.user_id) + " 结束了本轮行动。"
            )
            return True
    return False


def _pop_ready_action(game: Game) -> Optional[Action]:
    """按缓冲、pending、队列顺序立即取行动；不等待、不检查超时。"""
    if game.mid_turn_buffer:
        return game.mid_turn_buffer.popleft()
    if game.pending is not None:
        action = game.pending
        game.pending = None
        return action
    try:
        action = game.action_queue.get_nowait()
    except asyncio.QueueEmpty:
        return None
    game.action_queue.task_done()
    action.in_flight = True
    return action


async def _run_play(game: Game, cfg: Config) -> None:
    """PLAY 主循环：先消费已有输入，再按真正空闲时间推进期限。"""
    _enter_phase(game, Phase.PLAY)
    if game.module is None:
        return
    game.clock_start_minutes = game.module.time.start_minutes
    game.group_log.clear()
    await enter_scene(game, game.module.start_scene, opening=True)
    auto_hop_limit = len(game.module.scenes) + 1
    auto_hops = 0
    idle_deadline = 0.0
    idle_warned = False
    while game.phase is Phase.PLAY:
        ending = check_endings(game)
        if ending is not None:
            await do_ending(game, ending)
            return
        check_events(game)
        auto = _try_auto_exit(game)
        if auto is not None:
            auto_hops += 1
            if auto_hops > auto_hop_limit:
                logger.error(
                    f"跑团群 {game.group_id} 自动出口连续切换超过 "
                    f"{auto_hop_limit} 次，疑似恒真条件成环，本局结束"
                )
                await _announce(game, "场景数据异常，本局无法继续~")
                return
            if not await enter_scene(game, auto.to_scene, transition=auto.narration):
                logger.error(
                    f"跑团群 {game.group_id} 自动出口 {auto.to_scene} "
                    "切换失败（目标场景缺失），本局结束"
                )
                await _announce(game, "场景数据异常，本局无法继续~")
                return
            idle_deadline = 0.0
            idle_warned = False
            continue
        auto_hops = 0

        # 已经在缓冲区或队列中的行动优先于探索/战斗超时。
        action = _pop_ready_action(game)
        if action is None:
            now = _loop_time()
            if game.combat_order and now >= game.combat_deadline:
                uid = game.combat_order[game.combat_index]
                waiting = game.player_by_user(uid)
                if waiting is not None:
                    await _announce(
                        game,
                        f"{_player_name(waiting)} 行动超时，采取防御姿态。",
                    )
                await _advance_combat(game, cfg)
                continue
            if not game.combat_order:
                if game.explore_deadline <= 0:
                    game.explore_deadline = now + cfg.rpg_explore_round_timeout
                if now >= game.explore_deadline:
                    skipped = game.active_user_ids() - game.explore_acted
                    if skipped:
                        await _announce(game, "本轮等待结束，未行动的调查员自动跳过。")
                    game.start_explore_round(cfg.rpg_explore_round_timeout)
                    await _announce(
                        game,
                        _explore_prompt_text(game),
                    )
                    continue
            if game.combat_order:
                wait_deadline = game.combat_deadline
            else:
                if idle_deadline <= 0:
                    idle_deadline = now + cfg.rpg_idle_timeout
                    idle_warned = False
                wait_deadline = min(game.explore_deadline, idle_deadline)
                idle_remaining = idle_deadline - now
                if not idle_warned and idle_remaining <= cfg.rpg_idle_warn_remain:
                    idle_warned = True
                    await _announce(
                        game,
                        f"已经 {cfg.rpg_idle_timeout - cfg.rpg_idle_warn_remain} 秒"
                        "无人行动，再过一会儿本团将自动收尾~",
                    )
            remaining = wait_deadline - _loop_time()
            if remaining <= 0:
                continue
            waited = await _get_action(game, min(remaining, 1.0))
            if waited is None:
                continue
            action = waited

        player = game.player_by_user(action.actor_user_id)
        if player is None:
            release_action(game, action)
            continue
        wait_notice: Optional[asyncio.Task[None]] = None
        say_batch: Optional[list[Action]] = None
        try:
            if _action_stale(game, cfg, action):
                await _announce(
                    game,
                    MessageSegment.at(player.user_id)
                    + " 这条行动对应的局面已经变化，请按当前局面重新操作。",
                )
                continue
            if player.incapped:
                if action.kind is ActionKind.SAY:
                    continue
                await _announce(
                    game,
                    MessageSegment.at(player.user_id) + " 你已失去行动能力，无法行动。",
                )
                continue
            if action.kind is ActionKind.SAY and not game.combat_order:
                if cfg.rpg_ai_enabled:
                    wait_notice = asyncio.create_task(
                        _delayed_ai_wait_notice(game, cfg)
                    )
                say_batch = await _collect_say_batch(game, cfg, action)
                if say_batch:
                    action.route = say_batch[0].route
            if (
                _is_major_action(action)
                and not game.combat_order
                and player.user_id in game.explore_acted
            ):
                if say_batch is not None:
                    for queued in reversed(say_batch[1:]):
                        game.mid_turn_buffer.appendleft(queued)
                await _announce(
                    game,
                    MessageSegment.at(player.user_id)
                    + " 你本轮已经完成主要行动，请等待下一轮。",
                )
                continue
            scene_before = game.current_scene
            round_before = game.explore_round
            combat_before = bool(game.combat_order)
            if not combat_before:
                game.explore_deadline = 0.0
            executed = await _process_action(
                game,
                cfg,
                action,
                player,
                say_batch=say_batch,
            )
            major = _is_major_action(action)
            marked_same_round = False
            if major and executed and not combat_before:
                if (
                    game.phase is Phase.PLAY
                    and not game.combat_order
                    and game.current_scene == scene_before
                    and game.explore_round == round_before
                ):
                    game.explore_acted.add(player.user_id)
                    marked_same_round = True
            elif major and not executed and not game.combat_order:
                await _announce(
                    game,
                    MessageSegment.at(player.user_id) + " 本次行动无效，未消耗本轮行动。",
                )
            if game.phase is Phase.PLAY:
                idle_deadline = 0.0
                idle_warned = False
            if (
                game.phase is Phase.PLAY
                and not game.combat_order
                and game.active_user_ids() <= game.explore_acted
            ):
                game.start_explore_round(cfg.rpg_explore_round_timeout)
                await _announce(
                    game,
                    _explore_prompt_text(game),
                )
            elif game.phase is Phase.PLAY and major and executed and marked_same_round:
                await _announce(
                    game,
                    "本轮待行动："
                    + ("、".join(_waiting_names(game)) or "正在结算")
                    + "。",
                )
        finally:
            if wait_notice is not None:
                wait_notice.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await wait_notice
            release_action(game, action)


# ── 持久化 ────────────────────────────────────────────────


async def _persist_start(game: Game) -> None:
    """开局写库：对局行 + 玩家行。失败不影响游戏。"""
    module = game.module
    if module is None:
        return
    try:
        async with get_session() as session:
            row = RPGGame(
                group_id=game.group_id,
                host_user_id=game.host_user_id,
                module_id=module.id,
                module_name=module.name,
                player_count=len(game.players),
                started_at=_now_bj(),
            )
            session.add(row)
            await session.flush()
            game.game_row_id = row.id
            for p in game.players:
                session.add(
                    RPGPlayer(
                        game_id=row.id,
                        user_id=p.user_id,
                        char_name=p.sheet.name if p.sheet else "?",
                        start_hp=p.hp,
                        start_san=p.san,
                    )
                )
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.warning(
            f"跑团群 {game.group_id} 开局写库失败",
            exc_info=True,
        )


async def _persist_end(game: Game, ending: Ending) -> None:
    """终局写库：结局 + 玩家最终状态。"""
    if game.game_row_id is None:
        return
    try:
        async with get_session() as session:
            row = await session.get(RPGGame, game.game_row_id)
            if row is not None:
                row.ended_at = _now_bj()
                row.ending_id = ending.id
                row.outcome = ending.outcome
            for p in game.players:
                stmt = select(RPGPlayer).where(
                    RPGPlayer.game_id == game.game_row_id,
                    RPGPlayer.user_id == p.user_id,
                )
                prow = (await session.execute(stmt)).scalar_one_or_none()
                if prow is None:
                    continue
                prow.final_hp = p.hp
                prow.final_san = p.san
                prow.is_incapped = p.incapped
                prow.survived = p.survived
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.warning(
            f"跑团群 {game.group_id} 终局写库失败",
            exc_info=True,
        )


# ── 引擎入口 ──────────────────────────────────────────────


async def run_game(game: Game) -> None:
    """引擎主任务：报名选模组 → 建卡 → 场景循环 → 终局。"""
    cfg = config
    logger.info(f"跑团群 {game.group_id} 引擎启动（房主 {game.host_user_id}）")
    try:
        try:
            game.bot = get_bot()
        except ValueError:
            logger.error(f"跑团群 {game.group_id} 无可用机器人连接，对局取消")
            return
        await _run_signup(game, cfg)
        if game.phase is Phase.ENDED:  # 报名阶段被解散
            return
        if game.module is None:
            modules = list_modules()
            if not modules:
                await _announce(game, "当前没有可用的剧本模组，本局流局~")
                return
            game.module = modules[0]
            await _announce(game, f"未选择模组，默认使用《{game.module.name}》")
        module = game.module
        headcount = len(game.signup_user_ids)
        min_players = max(cfg.rpg_min_players, module.min_players)
        if headcount < min_players:
            await _announce(
                game,
                f"报名人数不足 {min_players} 人，本局流局~",
            )
            return
        if headcount > module.max_players:
            await _announce(
                game,
                f"报名人数（{headcount}）超过模组上限（{module.max_players}），本局流局~",
            )
            return
        await _run_char_create(game, cfg)
        await _persist_start(game)
        await _run_play(game, cfg)
    except asyncio.CancelledError:
        # 常规取消路径（空房解散 / /结束游戏）由命令层自行播报，
        # 并已把阶段置为 ENDED；仅对意外取消兜底播报
        if game.phase not in (Phase.SIGNUP, Phase.ENDED):
            with contextlib.suppress(Exception):
                await _announce(game, "对局已被强制结束")
        logger.info(f"跑团群 {game.group_id} 引擎任务被取消")
        raise
    except Exception:  # noqa: BLE001
        logger.exception(f"跑团群 {game.group_id} 引擎异常")
        with contextlib.suppress(Exception):
            await _announce(game, "游戏引擎发生异常，本局已结束")
    finally:
        game.release_unprocessed_actions()
        _enter_phase(game, Phase.ENDED)
        discard_game(game)
        logger.info(f"跑团群 {game.group_id} 引擎任务结束")
