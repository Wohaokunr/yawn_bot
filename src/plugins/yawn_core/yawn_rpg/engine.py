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
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional, Union

from nonebot import get_bot, get_plugin_config, logger
from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot_plugin_orm import get_session
from sqlalchemy import select

from ..llm import complete, complete_with_tools  # noqa: TID252
from . import ai_kp, api
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
    Ending,
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
)

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import Message
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

# NPC 台词最大长度（speak_as_npc 工具截断用）
_NPC_LINE_MAX = 150

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
    return action


def _enter_phase(game: Game, phase: Phase) -> None:
    """切换阶段并记日志；同阶段重复赋值不重复记录。"""
    if game.phase is phase:
        return
    logger.info(f"跑团群 {game.group_id} 进入阶段 {phase.value}")
    game.phase = phase


async def _announce(game: Game, text: Union[str, "Message"]) -> None:
    """群播报并记入群聊记录（KP 上下文来源）。"""
    game.group_log.append(f"〔系统〕{text}")
    if game.bot is None:
        return
    await api.safe_group_msg(game.bot, game.group_id, text)


async def _dm(game: Game, player: PlayerState, text: str) -> bool:
    """私聊玩家；首次失败时群内 @ 提示并标记 dm_ok=False。"""
    if game.bot is None:
        return False
    ok = await api.send_dm(game.bot, player.user_id, text)
    if not ok and player.dm_ok:
        player.dm_ok = False
        await _announce(
            game,
            MessageSegment.at(player.user_id) + " 你的私聊发送失败：请加机器人为好友。"
            "建卡将为你自动确认。",
        )
    return ok


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


async def _run_signup(game: Game, cfg: Config) -> None:
    """报名阶段：等待 START_GAME；期间可 MODULE_SELECT。"""
    _enter_phase(game, Phase.SIGNUP)
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
    deadline = _loop_time() + cfg.rpg_signup_timeout
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
        if action.kind is ActionKind.MODULE_SELECT and action.aux:
            module = _find_module(action.aux)
            if module is None:
                await _announce(game, "没有这个编号的模组，发送 /模组列表 查看")
            else:
                game.module = module
                logger.info(f"跑团群 {game.group_id} 选定模组：{module.name}")
                await _announce(
                    game,
                    f"已选定模组《{module.name}》（{module.min_players}"
                    f"-{module.max_players} 人）",
                )
        elif action.kind is ActionKind.START_GAME:
            break


def _find_module(text: str) -> Optional["ModuleDef"]:
    """按序号或 id 查找模组。"""
    modules = list_modules()
    s = text.strip()
    # isdigit 对 Unicode 数字（如 ²）为真而 int() 拒绝，会抛 ValueError
    if s.isascii() and s.isdigit():
        idx = int(s)
        if 1 <= idx <= len(modules):
            return modules[idx - 1]
        return None
    for m in modules:
        if s in (m.id, m.name):
            return m
    return None


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
        rerolls_left=player.rerolls_left,
        confirmed=player.confirmed,
    )


async def _run_char_create(game: Game, cfg: Config) -> None:
    """建卡阶段：系统掷卡私聊下发，私聊限时调整，超时自动确认。"""
    _enter_phase(game, Phase.CHAR_CREATE)
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
        if not await _dm(game, p, _card_text(p, cfg)):
            p.confirmed = True  # 私聊失败：自动确认
    timer = _Timer(
        _loop_time() + cfg.rpg_char_create_timeout,
        warn_remain=None,
    )
    while timer.remaining() > 0 and not all(p.confirmed for p in game.players):
        action = await timer.next_action(game)
        if action is None:
            continue
        player = game.player_by_user(action.actor_user_id)
        if player is None or player.confirmed or player.sheet is None:
            continue
        await _handle_card_action(game, cfg, player, action)
    unconfirmed = [p for p in game.players if not p.confirmed]
    for p in unconfirmed:
        p.confirmed = True
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
        await _dm(game, player, "角色卡已锁定，等待其他调查员……")
    elif action.kind is ActionKind.REROLL:
        if player.rerolls_left <= 0:
            await _dm(game, player, "重掷次数已用完~")
            return
        reroll_sheet(sheet)
        player.rerolls_left -= 1
        logger.info(f"跑团群 {game.group_id} {sheet.name} 重掷角色卡")
        await _dm(game, player, "已重掷整张角色卡：\n" + _card_text(player, cfg))
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
            await _dm(game, player, f"调整失败：{error}")
            return
        sheet.adjustments[skill_key] = sheet.adjustments.get(skill_key, 0) + delta
        await _dm(game, player, "调整成功：\n" + _card_text(player, cfg))
    elif action.kind is ActionKind.RESET_SKILLS:
        sheet.adjustments.clear()
        await _dm(game, player, "已清空加点：\n" + _card_text(player, cfg))
    elif action.kind is ActionKind.SHOW_CARD:
        await _dm(game, player, _card_text(player, cfg))


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
    result = skill_check(value, difficulty)
    skill = resolve_skill(skill_key)
    skill_name = skill.name if skill is not None else skill_key
    await _announce(game, result.describe(skill_name))
    logger.info(
        f"跑团群 {game.group_id} {skill_name} 检定："
        f"d100={result.roll}/{value} {result.tier.value}"
    )
    return result.success


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
    else:
        success = await do_skill_check(game, player, cp.skill, cp.difficulty)
        if success is None:
            success = False
    text = cp.success_text if success else cp.failure_text
    if text:
        await _announce(game, text)
    if success and cp.clue is not None:
        await discover_clue(game, cp.clue)
    if not success and cp.damage_on_fail:
        amount = roll_dice(cp.damage_on_fail)
        await apply_damage(game, player, amount, source=cp.id)
    if cp.once:
        game.fired_checks.add(cp.id)
    # 检定结算消耗时间（检定点 time_cost 覆写 > 引擎 check 默认；
    # 降级模式的关键词自动检定走这里，时间照常流动）
    cost = cp.time_cost if cp.time_cost is not None else _time_cost(game, "check")
    await _tick_time(game, cost)


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


async def discover_clue(game: Game, clue_id: str) -> bool:
    """发现线索并播报；重复发现返回 False。"""
    if game.module is None or clue_id in game.discovered_clues:
        return False
    clue = game.module.clue(clue_id)
    if clue is None:
        return False
    game.discovered_clues.add(clue_id)
    logger.info(f"跑团群 {game.group_id} 发现线索：{clue.name}")
    await _announce(game, f"〔线索〕{clue.name}\n{clue.text}")
    return True


# ── 场景与移动 ────────────────────────────────────────────


async def enter_scene(  # noqa: C901
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
    game.drain_actions()
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


async def _do_move(game: Game, player: PlayerState, target_text: str) -> None:
    """/前往 的确定性移动（与 transition_scene 工具共用出口校验）。"""
    if game.module is None or game.current_scene is None:
        return
    scene = game.module.scene(game.current_scene)
    if scene is None:
        return
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
        return
    await _transition_exit(game, player, chosen)


async def _transition_exit(game: Game, player: PlayerState, ex: "Exit") -> bool:
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
        name = player.sheet.name if player.sheet else str(player.seat)
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
        if monster is not None and (needle in monster.name or needle == mid):
            return monster
    for npc, _ in game.npcs_in_scene(game.current_scene):
        if needle in npc.name or needle == npc.id:
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


async def do_ending(game: Game, ending: Ending) -> None:
    """播报告终、写库、切 ENDED。"""
    logger.info(f"跑团群 {game.group_id} 达成结局 {ending.id}（{ending.outcome}）")
    for p in game.players:
        p.survived = not p.incapped
    await _announce(game, ending.text)
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


async def _fallback_narrate(game: Game, text: str) -> None:
    """AI 失败时的确定性兜底叙述。"""
    if game.phase is not Phase.PLAY:
        return
    cp = match_trigger(game, text)
    if cp is not None:
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


async def run_kp_turn(  # noqa: C901, PLR0912, PLR0915
    game: Game,
    cfg: Config,
    actor: Optional[PlayerState],
    instruction: str,
    hint: str = "",
) -> None:
    """KP 智能体循环：提示词 → 工具调用 → 验证执行 → 最终旁白。

    任何失败都落到确定性兜底（关键词自动检定 / 罐头文案），
    绝不卡局。所有状态写入发生在 execute_tool 内的引擎函数里。
    """
    if not cfg.rpg_ai_enabled:
        await _fallback_narrate(game, instruction)
        return
    # 防刷屏：宁可等待也不叠加调用
    wait = game.last_kp_at + cfg.rpg_kp_min_interval - _loop_time()
    if wait > 0:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.sleep(wait), timeout=wait + 1)
        if game.phase is not Phase.PLAY:
            return
    situation = ai_kp.build_situation(game)
    user_content = f"{situation}\n\n【当前任务】{instruction}{hint}"
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": ai_kp.build_system_prompt(cfg)},
        {"role": "user", "content": user_content},
    ]
    logger.debug(f"跑团群 {game.group_id} KP 提示词：{user_content}")
    turn_deadline = _loop_time() + cfg.rpg_ai_turn_timeout
    final_text: Optional[str] = None
    try:
        for _ in range(cfg.rpg_ai_max_tool_rounds):
            if game.phase is not Phase.PLAY:
                return
            remain = turn_deadline - _loop_time()
            if remain <= 1:
                break
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
                # 工具 schema 全静态：整局惰性构建一次后复用，
                # 使 wire 前缀（tools + 系统提示词）逐字节稳定
                player_names = [
                    p.sheet.name for p in game.players if p.sheet is not None
                ]
                game.tools_cache = ai_kp.build_tools(game.module, player_names)
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
            # 工具轮数用尽：强制收尾
            messages.append({"role": "user", "content": ai_kp.FINAL_NUDGE})
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
            return
    await _fallback_narrate(game, instruction)


async def execute_tool(  # noqa: C901, PLR0911, PLR0912
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
        if name == "san_check":
            return await _tool_san_check(game, cfg, args, actor)
        if name == "deal_damage":
            return await _tool_damage(game, cfg, args, actor, heal=False)
        if name == "heal":
            return await _tool_damage(game, cfg, args, actor, heal=True)
        if name == "transition_scene":
            return await _tool_transition(game, args)
        if name == "grant_clue":
            return await _tool_grant_clue(game, args)
        if name == "speak_as_npc":
            return await _tool_speak_as_npc(game, args)
        if name == "monster_attack":
            return await _tool_monster_attack(game, args)
        if name == "end_session":
            return await _tool_end_session(game, args)
        if name == "get_situation":
            return ai_kp.build_situation(game)
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


async def _tool_damage(  # noqa: PLR0911
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


async def _tool_transition(game: Game, args: dict[str, object]) -> str:
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
        if not await enter_scene(game, target_id, transition=chosen.narration):
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
    grantable = {cp.clue for cp in scene.checks if cp.clue}
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


async def _tool_speak_as_npc(  # noqa: PLR0911
    game: Game, args: dict[str, object]
) -> str:
    """speak_as_npc：NPC 在场校验 + 格式化播报。"""
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
    if game.npc_present(npc_id) is None:
        return "该 NPC 不在当前场景。"
    text = str(args.get("text", "")).strip().lstrip("/").strip()
    if not text:
        return "台词为空。"
    if len(text) > _NPC_LINE_MAX:
        text = text[:_NPC_LINE_MAX].rstrip() + "……"
    # _announce 已记入群聊记录，无需重复 append
    await _announce(game, f"【{npc.name}】{text}")
    return f"已以 {npc.name} 名义播报。"


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
    ending = next(
        (e for e in game.module.endings if e.id == ending_id),
        None,
    )
    if ending is None:
        return "结局不存在。"
    if not evaluate_condition(ending.condition, game.condition_context()):
        return "结局条件尚未满足，不能结束。继续推进剧情。"
    await do_ending(game, ending)
    return "结局已播报，对局结束。"


def _opt_str(value: object) -> Optional[str]:
    """参数可选字符串化（None / 空串返回 None）。"""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


# ── PLAY 主循环 ───────────────────────────────────────────


async def _collect_say_batch(
    game: Game,
    cfg: Config,
    first: Action,
) -> list[Action]:
    """合批连续 SAY：合批窗口内的发言合并为一次 KP 调用。"""
    batch = [first]
    deadline = _loop_time() + cfg.rpg_say_settle_window
    while len(batch) < cfg.rpg_kp_max_batch_lines:
        step = deadline - _loop_time()
        if step <= 0:
            break
        action = await _get_action(game, step)
        if action is None:
            continue
        if action.kind is ActionKind.SAY and action.aux:
            batch.append(action)
        else:
            game.pending = action  # 首个非 SAY 行动暂存，下轮处理
            break
    return batch


async def _handle_say(  # noqa: C901, PLR0912
    game: Game, cfg: Config, first: Action
) -> None:
    """处理自由发言：合批 → 违规扫描 → 触发提示 → KP 智能体循环。"""
    batch = await _collect_say_batch(game, cfg, first)
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
        return
    # 违规标记先于 tick 记录：flag 驱动的行程变化（如 NPC 吓跑）
    # 能在本次 tick 的进出 diff 里立即体现
    for category in offenses:
        raise_flag(game, category)
    # 每批发言整体消耗一次时间（在 KP 回合之前 tick，让局面反映最新时刻）
    await _tick_time(game, _time_cost(game, "say"))
    speaker = game.player_by_user(first.actor_user_id)
    instruction = (
        "调查员的发言如下：\n" + "\n".join(lines) + "\n请即兴续写氛围或 NPC 反应。"
    )
    if offenses:
        labels = "、".join(_OFFENSE_LABELS[c] for c in offenses)
        if cfg.rpg_ai_enabled:
            instruction += (
                f"\n【世界反应】调查员刚才尝试{labels}。在场 NPC 必须按其人格"
                "立即反应（阻止/呼救/逃跑/敌视），世界必须有回应，不得无视。"
            )
        else:
            # 降级模式：NPC 反应同样不依赖 AI（确定性罐头台词）
            present = game.npcs_in_scene(game.current_scene or "")
            if present:
                npc = present[0][0]
                await _announce(
                    game,
                    f"【{npc.name}】住手！你们这是要干什么！来人啊！",
                )
            else:
                await _announce(game, "你们的举动引起了周围极大的恐慌。")
    await run_kp_turn(game, cfg, speaker, instruction, hint=hint)


async def _handle_explicit_check(game: Game, player: PlayerState, aux: str) -> None:
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
        return
    if skill.key == "san":
        await _roll_san(game, player, "1/1d6", source="显式检定")
        await _tick_time(game, _time_cost(game, "check"))
        return
    success = await do_skill_check(game, player, skill.key, difficulty)
    if success is None:
        await _announce(game, f"你的角色卡上没有「{skill.name}」这项技能~")
        return
    await _tick_time(game, _time_cost(game, "check"))


async def _handle_talk_npc(
    game: Game,
    cfg: Config,
    player: PlayerState,
    aux: str,
) -> None:
    """处理 /对话 NPC名 内容。"""
    module = game.module
    if module is None or game.current_scene is None:
        return
    scene = module.scene(game.current_scene)
    if scene is None:
        return
    npc_name, _, text = aux.partition(" ")
    npc_name = npc_name.strip()
    text = text.strip()
    if not npc_name or not text:
        await _announce(game, "格式：/对话 NPC名 要说的话")
        return
    npc = None
    for cand, _ in game.npcs_in_scene(game.current_scene):
        if npc_name in cand.name:
            npc = cand
            break
    if npc is None:
        # 区分"死了"与"不在场"：对尸体搭话也给明确回应
        dead_match = next(
            (
                cand
                for cand in (module.npc(nid) for nid in game.dead_npcs)
                if cand is not None and npc_name in cand.name
            ),
            None,
        )
        if dead_match is not None:
            await _announce(game, f"……{dead_match.name} 已经死了。")
        else:
            await _announce(game, f"{npc_name} 不在这个场景里。")
        return
    await _tick_time(game, _time_cost(game, "talk"))
    pname = player.sheet.name if player.sheet else str(player.seat)
    game.group_log.append(f"【{pname}→{npc.name}】{text}")
    if not cfg.rpg_ai_enabled:
        line = npc.fallback_line or "（对方似乎不想多说。）"
        await _announce(game, f"【{npc.name}】{line}")
        return
    # 扮演规则系统提示词与工具 description 已说明，指令只给事实
    instruction = (
        f"调查员 {pname} 对 {npc.name} 说：「{text}」\n"
        f"请以 {npc.name} 的身份回应，可附少量环境描写。"
    )
    await run_kp_turn(game, cfg, player, instruction)


async def _handle_wait(game: Game, minutes: int) -> None:
    """/等待：固定文案 + 时间推进。

    不走 KP 回合（等待是廉价操作，防刷屏）；时间流逝带来的
    世界反应由 _tick_time 的 NPC 进出播报承担。
    """
    minutes = max(minutes, 1)
    await _announce(game, f"（你们等待了 {minutes} 分钟……）")
    await _tick_time(game, minutes)


async def _process_action(
    game: Game,
    cfg: Config,
    action: Action,
    player: PlayerState,
) -> None:
    """PLAY 阶段行动分发。"""
    if action.kind is ActionKind.SAY:
        await _handle_say(game, cfg, action)
    elif action.kind is ActionKind.CHECK:
        await _handle_explicit_check(game, player, action.aux or "")
    elif action.kind is ActionKind.TALK_NPC:
        await _handle_talk_npc(game, cfg, player, action.aux or "")
    elif action.kind is ActionKind.ATTACK:
        target = _find_attack_target(game, action.aux or "")
        if target is None:
            await _announce(game, "这个场景里没有你说的目标。")
        elif isinstance(target, NPC):
            await _tick_time(game, _time_cost(game, "attack"))
            # 攻击 NPC 是暴力行为：记 assault 标记（累计触发通用
            # 结局），幸存的 NPC 立即确定性反击（镜像怪物行为）
            raise_flag(game, "assault")
            game.npc_hostile.add(target.id)
            await do_player_attack_npc(game, player, target)
            if target.id not in game.dead_npcs:
                await do_npc_attack(game, target, player)
        else:
            await _tick_time(game, _time_cost(game, "attack"))
            await do_player_attack(game, player, target)
    elif action.kind is ActionKind.MOVE:
        await _do_move(game, player, action.aux or "")
    elif action.kind is ActionKind.WAIT:
        await _handle_wait(game, action.value or cfg.rpg_wait_default)


async def _run_play(game: Game, cfg: Config) -> None:  # noqa: C901
    """PLAY 主循环：结局安全网 → 自动出口 → 取行动 → 处理。"""
    _enter_phase(game, Phase.PLAY)
    if game.module is None:
        return
    # 游戏内时钟按模组起始时刻初始化（默认 20:00）
    game.clock_start_minutes = game.module.time.start_minutes
    await enter_scene(game, game.module.start_scene, opening=True)
    while game.phase is Phase.PLAY:
        ending = check_endings(game)
        if ending is not None:
            await do_ending(game, ending)
            return
        auto = _try_auto_exit(game)
        if auto is not None:
            if not await enter_scene(game, auto.to_scene, transition=auto.narration):
                # 条件恒真的自动出口 + 切换失败会构成无 await 忙环，
                # 冻死整个事件循环；正常模组经加载校验不可达，此处兜底
                logger.error(
                    f"跑团群 {game.group_id} 自动出口 {auto.to_scene} "
                    "切换失败（目标场景缺失），本局结束"
                )
                await _announce(game, "场景数据异常，本局无法继续~")
                return
            continue
        action = game.pending
        game.pending = None
        if action is None:
            action = await _get_action_idle(game, cfg)
        if action is None:
            await _announce(game, "长时间无人行动，本团暂告一段落~")
            return
        player = game.player_by_user(action.actor_user_id)
        if player is None:
            continue
        if player.incapped:
            if action.kind is ActionKind.SAY:
                continue  # 倒地玩家的发言静默忽略
            # 命令层不校验在局状态与是否倒地，由引擎统一拦截
            await _announce(
                game,
                MessageSegment.at(player.user_id) + " 你已失去行动能力，无法行动。",
            )
            continue
        await _process_action(game, cfg, action, player)


async def _get_action_idle(game: Game, cfg: Config) -> Optional[Action]:
    """空闲等待：rpg_idle_timeout 超时解散，剩余提醒一次。"""
    deadline = _loop_time() + cfg.rpg_idle_timeout
    warned = False
    while True:
        remaining = deadline - _loop_time()
        if remaining <= 0:
            return None
        if not warned and remaining <= cfg.rpg_idle_warn_remain:
            warned = True
            await _announce(
                game,
                f"已经 {cfg.rpg_idle_timeout - cfg.rpg_idle_warn_remain} 秒"
                "无人行动，再过一会儿本团将自动收尾~",
            )
        step = min(remaining, 1.0)
        if not warned:
            step = min(
                step,
                max(remaining - cfg.rpg_idle_warn_remain, 0.5),
            )
        action = await _get_action(game, step)
        if action is None:
            continue
        if game.player_by_user(action.actor_user_id) is None:
            continue  # 局外人的命令不重置空闲计时（否则可无限续命）
        return action


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
        _enter_phase(game, Phase.ENDED)
        discard_game(game)
        logger.info(f"跑团群 {game.group_id} 引擎任务结束")
