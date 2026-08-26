"""跑团命令入口：群命令与两个监听器。

处理器只做轻量校验（在局状态、阶段、权限），把行动投入
game.action_queue 交给引擎裁决；私聊监听器仅在建卡阶段拦
截在局玩家的私聊（其余私聊放行给 ai_chat），群自由文本监
听器在 PLAY 阶段把存活玩家的发言投入 SAY 行动队列。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Optional

from nonebot import (
    get_bot,
    get_driver,
    get_plugin_config,
    logger,
    on_command,
    on_message,
)
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageEvent,
    PrivateMessageEvent,
)
from nonebot.params import CommandArg
from nonebot.rule import Rule
from nonebot_plugin_orm import get_session

from ..event_log import record_game_event  # noqa: TID252
from ..permission import (  # noqa: TID252
    check_feature_permission,
    is_group_admin,
    require_feature,
)
from . import engine
from .charsheet import SKILLS
from .config import Config
from .dsl import _DM_HINT, parse_card_action
from .module_schema import list_modules
from .state import (
    Action,
    ActionKind,
    Phase,
    SubmitResult,
    create_game,
    game_of_user,
    get_game,
    stop_game,
    submit_action,
)
from .tutorial import help_text, set_guide_state

if TYPE_CHECKING:
    from .state import Game

config = get_plugin_config(Config)


tutorial_help_cmd = on_command(
    "跑团帮助", aliases={"TRPG帮助"}, priority=5, block=True
)


@tutorial_help_cmd.handle()
async def handle_tutorial_help(
    event: MessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("rpg"),  # pyright: ignore[reportArgumentType]
) -> None:
    """按当前阶段或指定主题显示简短帮助。"""
    topic = str(arg).strip()
    if not topic:
        group_id = getattr(event, "group_id", None)
        game = (
            get_game(int(group_id))
            if group_id is not None
            else game_of_user(int(event.get_user_id()))
        )
        if game is not None:
            topic = {
                Phase.SIGNUP: "报名",
                Phase.CHAR_CREATE: "建卡",
                Phase.PLAY: "行动",
            }.get(game.phase, "")
    await tutorial_help_cmd.finish(help_text(topic))


skip_tutorial_cmd = on_command("跳过引导", priority=5, block=True)


@skip_tutorial_cmd.handle()
async def handle_skip_tutorial(
    event: MessageEvent,
    _perm: None = require_feature("rpg"),  # pyright: ignore[reportArgumentType]
) -> None:
    user_id = int(event.get_user_id())
    await set_guide_state(user_id, "skipped")
    game = game_of_user(user_id)
    if game is not None:
        player = game.player_by_user(user_id)
        record_game_event(
            game,
            "rpg",
            "tutorial_skipped",
            phase=game.phase,
            actor_seat=player.seat if player is not None else None,
            payload={"step": "profile"},
        )
    await skip_tutorial_cmd.finish("已停止自动新手提示；仍可随时使用 /跑团帮助。")


reset_tutorial_cmd = on_command("重新引导", priority=5, block=True)


@reset_tutorial_cmd.handle()
async def handle_reset_tutorial(
    event: MessageEvent,
    _perm: None = require_feature("rpg"),  # pyright: ignore[reportArgumentType]
) -> None:
    await set_guide_state(int(event.get_user_id()), "reset")
    await reset_tutorial_cmd.finish("已重置新手引导；下次参团会重新按阶段提示。")


def _player_required(game: "Game", user_id: int) -> str | None:
    """拒绝局外人把局内行动写入队列。"""
    if game.player_by_user(user_id) is None:
        return "你不是本局调查员~"
    return None


def _submit(game: "Game", action: Action) -> str | None:
    """命令层唯一入队入口；只反馈背压，业务裁决由引擎完成。

    已入队不是需要发送给用户的消息；用 ``None`` 表示静默结束，
    避免把空字符串交给 OneBot，导致 ``message must contain at least
    one sendable segment``。
    """
    result = submit_action(
        game,
        action,
        queue_max=config.rpg_action_queue_max,
        user_pending_max=config.rpg_user_pending_max,
        user_say_pending_max=config.rpg_user_say_pending_max,
    )
    messages: dict[SubmitResult, str | None] = {
        SubmitResult.ACCEPTED: None,
        SubmitResult.QUEUE_FULL: "当前行动过多，请稍后再试~",
        SubmitResult.USER_LIMIT: "你的待处理行动过多，请等待系统结算~",
        SubmitResult.DUPLICATE: "这条行动已经提交过了~",
        SubmitResult.STALE: "局面已经变化，请重新操作~",
    }
    return messages[result]


def _action(  # noqa: PLR0913
    kind: ActionKind,
    user_id: int,
    *,
    game: "Game",
    value: int | None = None,
    aux: str | None = None,
    authority: str = "player",
) -> Action:
    """构造带阶段/场景快照的动作，供引擎拒绝过期操作。"""
    scene = game.current_scene if game.phase is Phase.PLAY else None
    combat_active = bool(game.combat_order)
    combat_actor = (
        game.combat_order[game.combat_index]
        if combat_active and 0 <= game.combat_index < len(game.combat_order)
        else None
    )
    return Action(
        kind,
        user_id,
        value=value,
        aux=aux,
        expected_phase=game.phase,
        expected_scene=scene,
        expected_explore_round=game.explore_round if game.phase is Phase.PLAY else None,
        expected_combat_round=game.combat_round if combat_active else None,
        expected_combat_actor=combat_actor,
        authority=authority,
    )


def _is_su(user_id: int) -> bool:
    """是否为超级用户。"""
    return str(user_id) in get_driver().config.superusers


def _signup_cap(game_module_max: int | None) -> int:
    """报名人数上限：配置与模组上限取较小值。"""
    if game_module_max is None:
        return config.rpg_max_players
    return min(config.rpg_max_players, game_module_max)


def _rpg_game_in_group(event: MessageEvent) -> bool:
    """规则：群消息 ∧ 本群有跑团对局。

    与 priority=4 配合做注册表门控：报名 / 开始游戏 等命令
    与狼人杀子插件同名，本插件先加载的狼人杀匹配器会无条件
    finish() 把它们全部遮蔽。门控后群里有跑团局时 RPG 先接
    （优先级数值更小者先检查）；没有时规则不通过，同名狼人杀
    命令照常接管，两边 UX 都不受影响。
    """
    if not isinstance(event, GroupMessageEvent):
        return False
    return get_game(int(event.group_id)) is not None


# ── 开房与报名 ────────────────────────────────────────────

rpg_open = on_command(
    "跑团",
    aliases={"开团", "TRPG"},
    priority=5,
    block=True,
)


@rpg_open.handle()
async def handle_open(
    event: GroupMessageEvent,
    _perm: None = require_feature("rpg"),  # pyright: ignore[reportArgumentType]
) -> None:
    """开房：创建对局并启动引擎任务，房主自动报名。"""
    group_id = int(event.group_id)
    user_id = int(event.get_user_id())
    if get_game(group_id) is not None:
        await rpg_open.finish("本群已经有正在进行的跑团~")
    if game_of_user(user_id) is not None:
        await rpg_open.finish("你已经在其他对局中，无法开房~")
    if not list_modules():
        await rpg_open.finish("当前没有可用的剧本模组，无法开团~")
    try:
        get_bot()
    except ValueError:
        await rpg_open.finish("机器人连接未就绪，请稍后重试~")
    game = create_game(group_id, user_id, queue_max=config.rpg_action_queue_max)
    if game is None:
        await rpg_open.finish("开房失败，请稍后重试")
    game.worker = asyncio.create_task(engine.run_game(game))
    logger.info(f"跑团群 {group_id} 由 {user_id} 开房")
    await rpg_open.finish("跑团房间已创建，房主已自动报名~")


module_list_cmd = on_command(
    "模组列表",
    aliases={"模组"},
    priority=5,
    block=True,
)


@module_list_cmd.handle()
async def handle_module_list(
    _perm: None = require_feature("rpg"),  # pyright: ignore[reportArgumentType]
) -> None:
    """列出可选剧本模组。"""
    await module_list_cmd.finish(engine.module_list_text())


select_module_cmd = on_command(
    "选择模组",
    aliases={"选模组"},
    priority=5,
    block=True,
)


@select_module_cmd.handle()
async def handle_select_module(
    event: GroupMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("rpg"),  # pyright: ignore[reportArgumentType]
) -> None:
    """报名阶段选定模组（房主/群管/超管）。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.SIGNUP:
        await select_module_cmd.finish("本群当前没有报名中的跑团")
    user_id = int(event.get_user_id())
    if not (user_id == game.host_user_id or is_group_admin(event) or _is_su(user_id)):
        await select_module_cmd.finish("只有房主、群管理员或超管可以选择模组~")
    text = str(arg).strip()
    if not text:
        await select_module_cmd.finish("格式：/选择模组 N（发送 /模组列表 查看）")
    module = engine.find_module(text)
    if module is None:
        await select_module_cmd.finish("没有这个编号的模组，发送 /模组列表 查看")
    if (
        selection_error := engine.module_selection_error(game, module, config)
    ) is not None:
        await select_module_cmd.finish(selection_error)
    authority = (
        "superuser" if _is_su(user_id) else "admin" if is_group_admin(event) else "host"
    )
    error = _submit(
        game,
        _action(
            ActionKind.MODULE_SELECT,
            user_id,
            game=game,
            aux=text,
            authority=authority,
        ),
    )
    await select_module_cmd.finish(error)


signup_cmd = on_command(
    "报名",
    aliases={"上车", "加一"},
    rule=Rule(_rpg_game_in_group),
    priority=4,  # 先于狼人杀同名命令；规则不通过时放行
    block=True,
)


@signup_cmd.handle()
async def handle_signup(
    event: GroupMessageEvent,
    _perm: None = require_feature("rpg"),  # pyright: ignore[reportArgumentType]
) -> None:
    """报名加入当前房间。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.SIGNUP:
        await signup_cmd.finish("本群当前没有报名中的跑团（发送 /跑团 开房）")
    error = _submit(
        game,
        _action(ActionKind.JOIN_GAME, int(event.get_user_id()), game=game),
    )
    await signup_cmd.finish(error or "报名申请已提交，系统将按顺序处理~")


leave_cmd = on_command(
    "退报名",
    aliases={"下车"},
    rule=Rule(_rpg_game_in_group),
    priority=4,  # 先于狼人杀同名命令；规则不通过时放行
    block=True,
)


@leave_cmd.handle()
async def handle_leave(
    event: GroupMessageEvent,
    _perm: None = require_feature("rpg"),  # pyright: ignore[reportArgumentType]
) -> None:
    """退出报名；房主退出自动移交，空房自动解散。"""
    game = get_game(int(event.group_id))
    user_id = int(event.get_user_id())
    if game is None or game.phase is not Phase.SIGNUP:
        await leave_cmd.finish("本群当前没有报名中的跑团")
    error = _submit(game, _action(ActionKind.LEAVE_GAME, user_id, game=game))
    await leave_cmd.finish(error or "退报名申请已提交，系统将按顺序处理~")


view_cmd = on_command(
    "查看报名",
    aliases={"报名情况"},
    rule=Rule(_rpg_game_in_group),
    priority=4,  # 先于狼人杀同名命令；规则不通过时放行
    block=True,
)


@view_cmd.handle()
async def handle_view(
    event: GroupMessageEvent,
    _perm: None = require_feature("rpg"),  # pyright: ignore[reportArgumentType]
) -> None:
    """查看报名名单。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.SIGNUP:
        await view_cmd.finish("本群当前没有报名中的跑团")
    cap = _signup_cap(game.module.max_players if game.module else None)
    module_name = game.module.name if game.module is not None else "（未选择）"
    lines = [
        "═══ 跑团 · 报名名单 ═══",
        f"模组：{module_name}",
    ]
    for idx, uid in enumerate(game.signup_user_ids, start=1):
        mark = "（房主）" if uid == game.host_user_id else ""
        lines.append(f"{idx}. {uid}{mark}")
    lines.append("──────────────")
    lines.append(f"当前 {len(game.signup_user_ids)}/{cap} 人")
    await view_cmd.finish("\n".join(lines))


situation_cmd = on_command(
    "局面",
    aliases={"当前局面", "跑团状态"},
    rule=Rule(_rpg_game_in_group),
    priority=4,
    block=True,
)


@situation_cmd.handle()
async def handle_situation(
    event: GroupMessageEvent,
    _perm: None = require_feature("rpg"),  # pyright: ignore[reportArgumentType]
) -> None:
    """群内发送公开局面，并把请求者的私人摘要单独发到私聊。"""
    game = get_game(int(event.group_id))
    if game is None:
        await situation_cmd.finish("本群当前没有跑团对局")
    public_text = engine.public_situation_text(game)
    player = game.player_by_user(int(event.get_user_id()))
    if player is not None and game.phase in {Phase.CHAR_CREATE, Phase.PLAY}:
        private_text = engine.private_situation_text(game, player)
        if not await engine.send_private_text(game, player, private_text):
            public_text += "\n私聊局面未送达，请加机器人好友后重试 /局面。"
    await situation_cmd.finish(public_text)


start_cmd = on_command(
    "开始游戏",
    aliases={"发车"},
    rule=Rule(_rpg_game_in_group),
    priority=4,  # 先于狼人杀同名命令；规则不通过时放行
    block=True,
)


@start_cmd.handle()
async def handle_start(
    event: GroupMessageEvent,
    _perm: None = require_feature("rpg"),  # pyright: ignore[reportArgumentType]
) -> None:
    """手动开局：房主/群管/超管可发起，人数由引擎校验。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.SIGNUP:
        await start_cmd.finish("本群当前没有报名中的跑团")
    user_id = int(event.get_user_id())
    if not (user_id == game.host_user_id or is_group_admin(event) or _is_su(user_id)):
        await start_cmd.finish("只有房主、群管理员或超管可以开始游戏~")
    if (start_error := engine.signup_start_error(game, config)) is not None:
        await start_cmd.finish(start_error)
    authority = (
        "superuser" if _is_su(user_id) else "admin" if is_group_admin(event) else "host"
    )
    error = _submit(
        game,
        _action(ActionKind.START_GAME, user_id, game=game, authority=authority),
    )
    if error:
        await start_cmd.finish(error)
    logger.info(
        f"跑团群 {int(event.group_id)} {user_id} 请求开始游戏"
        f"（{len(game.signup_user_ids)} 人）"
    )
    await start_cmd.finish("已请求开始游戏~")


end_cmd = on_command(
    "结束游戏",
    aliases={"解散团"},
    rule=Rule(_rpg_game_in_group),
    priority=4,  # 先于狼人杀同名命令（其无局分支会全员解禁）；规则不通过时放行
    block=True,
)


@end_cmd.handle()
async def handle_end(
    event: GroupMessageEvent,
    _perm: None = require_feature("rpg"),  # pyright: ignore[reportArgumentType]
) -> None:
    """强制结束对局。"""
    game = get_game(int(event.group_id))
    if game is None:
        await end_cmd.finish("本群当前没有跑团对局")
    user_id = int(event.get_user_id())
    if not (user_id == game.host_user_id or is_group_admin(event) or _is_su(user_id)):
        await end_cmd.finish("只有房主、群管理员或超管可以结束游戏~")
    logger.info(
        f"跑团群 {int(event.group_id)} {user_id} 强制结束对局（{game.phase.value}）"
    )
    await stop_game(game)
    await end_cmd.finish("对局已结束")


# ── 局内群命令 ────────────────────────────────────────────

check_cmd = on_command("检定", aliases={"rc"}, priority=5, block=True)


@check_cmd.handle()
async def handle_check(
    event: GroupMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("rpg"),  # pyright: ignore[reportArgumentType]
) -> None:
    """显式技能检定（确定性路径）。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.PLAY:
        await check_cmd.finish("现在不在跑团进行中")
    user_id = int(event.get_user_id())
    if (error := _player_required(game, user_id)) is not None:
        await check_cmd.finish(error)
    error = _submit(
        game,
        _action(
            ActionKind.CHECK, user_id, game=game, aux=str(arg).strip()
        ),
    )
    await check_cmd.finish(error)


attack_cmd = on_command("攻击", aliases={"打"}, priority=5, block=True)


@attack_cmd.handle()
async def handle_attack(
    event: GroupMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("rpg"),  # pyright: ignore[reportArgumentType]
) -> None:
    """攻击场景中的怪物。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.PLAY:
        await attack_cmd.finish("现在不在跑团进行中")
    user_id = int(event.get_user_id())
    if (error := _player_required(game, user_id)) is not None:
        await attack_cmd.finish(error)
    target = str(arg).strip()
    if not target:
        await attack_cmd.finish("格式：/攻击 目标名")
    error = _submit(
        game,
        _action(ActionKind.ATTACK, user_id, game=game, aux=target),
    )
    await attack_cmd.finish(error)


move_cmd = on_command("前往", aliases={"去"}, priority=5, block=True)


@move_cmd.handle()
async def handle_move(
    event: GroupMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("rpg"),  # pyright: ignore[reportArgumentType]
) -> None:
    """经出口切换场景。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.PLAY:
        await move_cmd.finish("现在不在跑团进行中")
    user_id = int(event.get_user_id())
    if (error := _player_required(game, user_id)) is not None:
        await move_cmd.finish(error)
    target = str(arg).strip()
    if not target:
        await move_cmd.finish("格式：/前往 地点名")
    error = _submit(
        game, _action(ActionKind.MOVE, user_id, game=game, aux=target)
    )
    await move_cmd.finish(error)


time_cmd = on_command("时间", aliases={"时辰"}, priority=5, block=True)


@time_cmd.handle()
async def handle_time(
    event: GroupMessageEvent,
    _perm: None = require_feature("rpg"),  # pyright: ignore[reportArgumentType]
) -> None:
    """查看游戏内时钟（只读直答，不消耗时间）。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.PLAY:
        await time_cmd.finish("现在不在跑团进行中")
    await time_cmd.finish(f"现在是 {engine.format_clock(game)}")


wait_cmd = on_command("等待", aliases={"休息"}, priority=5, block=True)


@wait_cmd.handle()
async def handle_wait(
    event: GroupMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("rpg"),  # pyright: ignore[reportArgumentType]
) -> None:
    """原地等待 N 分钟（缺省 rpg_wait_default，钳制 rpg_wait_max）。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.PLAY:
        await wait_cmd.finish("现在不在跑团进行中")
    user_id = int(event.get_user_id())
    if (error := _player_required(game, user_id)) is not None:
        await wait_cmd.finish(error)
    text = str(arg).strip()
    if text:
        if not (text.isascii() and text.isdigit()):
            await wait_cmd.finish("格式：/等待 分钟数（如 /等待 90）")
        minutes = max(1, min(int(text), config.rpg_wait_max))
    else:
        # 缺省值同样受上限钳制（引擎侧只钳下限），防配置越界
        minutes = max(1, min(config.rpg_wait_default, config.rpg_wait_max))
    error = _submit(
        game,
        _action(ActionKind.WAIT, user_id, game=game, value=minutes),
    )
    await wait_cmd.finish(error)


status_cmd = on_command("状态", aliases={"我的状态"}, priority=5, block=True)


@status_cmd.handle()
async def handle_status(
    event: GroupMessageEvent,
    _perm: None = require_feature("rpg"),  # pyright: ignore[reportArgumentType]
) -> None:
    """查看自己的 HP/SAN/属性。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.PLAY:
        await status_cmd.finish("现在不在跑团进行中")
    player = game.player_by_user(int(event.get_user_id()))
    if player is None or player.sheet is None:
        await status_cmd.finish("你不是本局调查员~")
    a = player.sheet.attributes
    state_desc = "失去行动能力" if player.incapped else "正常"
    lines = [
        f"═══ {player.sheet.name} · 状态 ═══",
        f"HP {player.hp}/{player.sheet.max_hp}  "
        f"SAN {player.san}/{player.sheet.max_san}  状态：{state_desc}",
        f"力量 {a['str']}  体质 {a['con']}  体型 {a['siz']}",
        f"敏捷 {a['dex']}  外貌 {a['app']}  智力 {a['int']}",
        f"意志 {a['pow']}  教育 {a['edu']}  幸运 {a['luck']}",
    ]
    await status_cmd.finish("\n".join(lines))


skill_cmd = on_command("技能", aliases={"技能列表"}, priority=5, block=True)


@skill_cmd.handle()
async def handle_skills(
    event: GroupMessageEvent,
    _perm: None = require_feature("rpg"),  # pyright: ignore[reportArgumentType]
) -> None:
    """查看自己的技能值。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.PLAY:
        await skill_cmd.finish("现在不在跑团进行中")
    player = game.player_by_user(int(event.get_user_id()))
    if player is None or player.sheet is None:
        await skill_cmd.finish("你不是本局调查员~")
    values = player.sheet.skill_values()
    lines = [f"═══ {player.sheet.name} · 技能 ═══"]
    parts = [f"{s.name} {values[s.key]}" for s in SKILLS if s.key != "cthulhu_mythos"]
    lines.extend("  ".join(parts[i : i + 4]) for i in range(0, len(parts), 4))
    await skill_cmd.finish("\n".join(lines))


clue_cmd = on_command("线索", aliases={"已发现线索"}, priority=5, block=True)


@clue_cmd.handle()
async def handle_clues(
    event: GroupMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("rpg"),  # pyright: ignore[reportArgumentType]
) -> None:
    """公共线索群内回看，个人线索只私聊发送给请求者。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.PLAY:
        await clue_cmd.finish("现在不在跑团进行中")
    user_id = int(event.get_user_id())
    player = game.player_by_user(user_id)
    needle = str(arg).strip()
    if not needle:
        public_text = engine.public_clue_list_text(game)
        if player is None:
            await clue_cmd.finish(public_text)
        sent = await engine.send_private_text(
            game,
            player,
            engine.private_journal_text(game, player),
        )
        if not sent:
            public_text += "\n完整调查手记未送达，请加机器人好友后重试 /线索。"
        await clue_cmd.finish(public_text)
    clue, visibility = engine.lookup_visible_clue(game, player, needle)
    if visibility == "ambiguous":
        await clue_cmd.finish("匹配到多条线索，请输入更完整的名称。")
    if clue is None or visibility is None:
        await clue_cmd.finish("没有找到你可查看的这条线索。")
    if visibility == "public":
        await clue_cmd.finish(f"〔公共线索〕{clue.name}\n{clue.text}")
    if player is None:
        await clue_cmd.finish("这条线索只对发现者可见。")
    sent = await engine.send_private_text(
        game,
        player,
        f"〔个人线索〕{clue.name}\n{clue.text}",
    )
    message = (
        f"已将个人线索「{clue.name}」发送到你的私聊。"
        if sent
        else "这条个人线索未能私聊送达，请加机器人好友后重试。"
    )
    await clue_cmd.finish(message)


assist_cmd = on_command("协助", aliases={"帮忙"}, priority=5, block=True)


@assist_cmd.handle()
async def handle_assist(
    event: GroupMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("rpg"),  # pyright: ignore[reportArgumentType]
) -> None:
    """协助一名同场景调查员的下一次技能检定。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.PLAY:
        await assist_cmd.finish("现在不在跑团进行中")
    user_id = int(event.get_user_id())
    if (error := _player_required(game, user_id)) is not None:
        await assist_cmd.finish(error)
    target, sep, skill = str(arg).strip().partition(" ")
    if not sep or not target or not skill.strip():
        await assist_cmd.finish("格式：/协助 玩家 技能（如 /协助 阿明 侦查）")
    error = _submit(
        game,
        _action(
            ActionKind.ASSIST,
            user_id,
            game=game,
            aux=f"{target}|{skill.strip()}",
        ),
    )
    await assist_cmd.finish(error)


share_clue_cmd = on_command("分享线索", aliases={"公开线索"}, priority=5, block=True)

clue_board_cmd = on_command("线索板", aliases={"证据板"}, priority=5, block=True)


@clue_board_cmd.handle()
async def handle_clue_board(
    event: GroupMessageEvent,
    _perm: None = require_feature("rpg"),  # pyright: ignore[reportArgumentType]
) -> None:
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.PLAY:
        await clue_board_cmd.finish("现在不在跑团进行中；发送 /跑团帮助 查看入门流程。")
    await clue_board_cmd.finish(engine.clue_board_text(game))


@share_clue_cmd.handle()
async def handle_share_clue(
    event: GroupMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("rpg"),  # pyright: ignore[reportArgumentType]
) -> None:
    """将自己的个人线索公开给队伍。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.PLAY:
        await share_clue_cmd.finish("现在不在跑团进行中")
    user_id = int(event.get_user_id())
    if (error := _player_required(game, user_id)) is not None:
        await share_clue_cmd.finish(error)
    clue = str(arg).strip()
    if not clue:
        await share_clue_cmd.finish("格式：/分享线索 线索名")
    error = _submit(
        game,
        _action(ActionKind.SHARE_CLUE, user_id, game=game, aux=clue),
    )
    await share_clue_cmd.finish(error)


deduction_cmd = on_command("推理", aliases={"联合推理"}, priority=5, block=True)


@deduction_cmd.handle()
async def handle_deduction(
    event: GroupMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("rpg"),  # pyright: ignore[reportArgumentType]
) -> None:
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.PLAY:
        await deduction_cmd.finish("现在不在跑团进行中；可发送 /局面 查看当前阶段。")
    user_id = int(event.get_user_id())
    if (error := _player_required(game, user_id)) is not None:
        await deduction_cmd.finish(error)
    raw = str(arg).strip()
    if not raw:
        await deduction_cmd.finish("格式：/推理 线索A + 线索B：结论")
    error = _submit(
        game,
        _action(ActionKind.PROPOSE_DEDUCTION, user_id, game=game, aux=raw),
    )
    await deduction_cmd.finish(error)


confirm_deduction_cmd = on_command("赞成推理", priority=5, block=True)


@confirm_deduction_cmd.handle()
async def handle_confirm_deduction(
    event: GroupMessageEvent,
    _perm: None = require_feature("rpg"),  # pyright: ignore[reportArgumentType]
) -> None:
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.PLAY:
        await confirm_deduction_cmd.finish("现在没有进行中的联合推理。")
    user_id = int(event.get_user_id())
    if (error := _player_required(game, user_id)) is not None:
        await confirm_deduction_cmd.finish(error)
    error = _submit(
        game, _action(ActionKind.CONFIRM_DEDUCTION, user_id, game=game)
    )
    await confirm_deduction_cmd.finish(error)


withdraw_deduction_cmd = on_command("撤回推理", priority=5, block=True)


@withdraw_deduction_cmd.handle()
async def handle_withdraw_deduction(
    event: GroupMessageEvent,
    _perm: None = require_feature("rpg"),  # pyright: ignore[reportArgumentType]
) -> None:
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.PLAY:
        await withdraw_deduction_cmd.finish("现在没有进行中的联合推理。")
    user_id = int(event.get_user_id())
    if (error := _player_required(game, user_id)) is not None:
        await withdraw_deduction_cmd.finish(error)
    error = _submit(
        game, _action(ActionKind.WITHDRAW_DEDUCTION, user_id, game=game)
    )
    await withdraw_deduction_cmd.finish(error)


share_fact_cmd = on_command("分享情报", aliases={"公开情报"}, priority=5, block=True)


@share_fact_cmd.handle()
async def handle_share_fact(
    event: GroupMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("rpg"),  # pyright: ignore[reportArgumentType]
) -> None:
    """将自己从 NPC 获得的个人情报公开给队伍。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.PLAY:
        await share_fact_cmd.finish("现在不在跑团进行中")
    user_id = int(event.get_user_id())
    if (error := _player_required(game, user_id)) is not None:
        await share_fact_cmd.finish(error)
    npc, sep, fact = str(arg).strip().partition(" ")
    if not sep or not npc or not fact.strip():
        await share_fact_cmd.finish("格式：/分享情报 NPC名 情报名")
    error = _submit(
        game,
        _action(
            ActionKind.SHARE_FACT,
            user_id,
            game=game,
            aux=f"{npc}|{fact.strip()}",
        ),
    )
    await share_fact_cmd.finish(error)


pass_turn_cmd = on_command("跳过", aliases={"结束行动"}, priority=5, block=True)


@pass_turn_cmd.handle()
async def handle_pass_turn(
    event: GroupMessageEvent,
    _perm: None = require_feature("rpg"),  # pyright: ignore[reportArgumentType]
) -> None:
    """结束本探索轮或当前战斗行动。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.PLAY:
        await pass_turn_cmd.finish("现在不在跑团进行中")
    user_id = int(event.get_user_id())
    if (error := _player_required(game, user_id)) is not None:
        await pass_turn_cmd.finish(error)
    error = _submit(
        game, _action(ActionKind.PASS_TURN, user_id, game=game)
    )
    await pass_turn_cmd.finish(error)


# ── 私聊建卡监听 ──────────────────────────────────────────

# 监听规则特性开关判定的缓存秒数：开关局内基本不变，逐条消息
# 开库查 1-2 次主键 SELECT 纯属浪费；开关变更后最多 TTL 秒生效
_FEATURE_CACHE_TTL = 300.0


async def _rpg_feature_ok(game: "Game", user_id: int, group_id: Optional[int]) -> bool:
    """查 rpg 特性开关（按 (用户, 群) 带 TTL 缓存，随对局销毁回收）。"""
    now = asyncio.get_running_loop().time()
    key = (user_id, group_id)
    hit = game.feature_ok_cache.get(key)
    if hit is not None and hit[1] > now:
        return hit[0]
    # 与命令处理器的 require_feature 一致：私聊走全局用户解析链
    # （group_id=None），群聊走 用户→群 解析链
    async with get_session() as session:
        verdict = await check_feature_permission(
            user_id,
            group_id,
            "rpg",
            session,  # pyright: ignore[reportArgumentType]
        )
    game.feature_ok_cache[key] = (verdict, now + _FEATURE_CACHE_TTL)
    return verdict


async def _is_char_create_dm(event: MessageEvent) -> bool:
    """规则：私聊 ∧ 用户在建卡阶段的对局中 ∧ 未被禁用跑团功能。"""
    if isinstance(event, GroupMessageEvent):
        return False
    user_id = int(event.get_user_id())
    game = game_of_user(user_id)
    if game is None or game.phase is not Phase.CHAR_CREATE:
        return False
    return await _rpg_feature_ok(game, user_id, None)


private_listener = on_message(
    rule=Rule(_is_char_create_dm),
    # 必须抢先于 ai_chat 的对话模式监听器（同 block=True、priority=0
    # 但注册更早）：否则建卡期玩家的私聊指令会被当成
    # 闲聊吃掉，角色卡只能等超时自动确认。负优先级保证本监听器在
    # CHAR_CREATE 期先于一切私聊拦截器运行（规则已把范围收窄到
    # 建卡期在局玩家，不影响其余私聊）。
    priority=-1,
    block=True,
)


@private_listener.handle()
async def handle_char_create_dm(event: PrivateMessageEvent) -> None:
    """建卡期私聊解析入口（优先于 ai_chat）。"""
    user_id = int(event.get_user_id())
    game = game_of_user(user_id)
    if game is None:
        return
    text = event.get_plaintext()
    action = parse_card_action(text, user_id)
    if action is None:
        logger.info(f"跑团群 {game.group_id} {user_id} 建卡私聊无法解析：{text!r}")
        await private_listener.finish(_DM_HINT)
    player = game.player_by_user(user_id)
    name = player.sheet.name if player is not None and player.sheet else str(user_id)
    logger.info(
        f"跑团群 {game.group_id} {name} 建卡行动：{action.kind.value}（原文 {text!r}）"
    )
    action.expected_phase = game.phase
    error = _submit(game, action)
    await private_listener.finish(error)


# ── 群自由文本监听（SAY）─────────────────────────────────


async def _is_game_speech(event: MessageEvent) -> bool:
    """规则：群消息 ∧ PLAY 阶段 ∧ 发送者是存活调查员 ∧ 功能开启。"""
    if not isinstance(event, GroupMessageEvent):
        return False
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.PLAY:
        return False
    player = game.player_by_user(int(event.get_user_id()))
    if player is None or player.incapped:
        return False
    return await _rpg_feature_ok(game, int(event.get_user_id()), int(event.group_id))


speech_listener = on_message(
    rule=Rule(_is_game_speech),
    priority=0,
    block=False,  # 不阻断命令匹配；仅旁路投递 SAY
)


@speech_listener.handle()
async def handle_game_speech(event: GroupMessageEvent) -> None:
    """在局玩家的群自由发言 → SAY 行动（交给 AI 路由器与引擎）。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.PLAY:
        return
    text = event.get_plaintext().strip()
    # 空消息与命令（/检定 等）不算自由发言
    if not text or text.startswith("/"):
        return
    error = _submit(
        game, _action(ActionKind.SAY, int(event.get_user_id()), game=game, aux=text)
    )
    if error:
        logger.info(f"跑团群 {game.group_id} 自由发言未入队：{error}")
