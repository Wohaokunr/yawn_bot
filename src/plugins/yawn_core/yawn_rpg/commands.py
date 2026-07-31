"""跑团命令入口：群命令与两个监听器。

处理器只做轻量校验（在局状态、阶段、权限），把行动投入
game.action_queue 交给引擎裁决；私聊监听器仅在建卡阶段拦
截在局玩家的私聊（其余私聊放行给 ai_chat），群自由文本监
听器在 PLAY 阶段把存活玩家的发言投入 SAY 行动队列。
"""

from __future__ import annotations

import asyncio

from nonebot import (
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
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.params import CommandArg
from nonebot.rule import Rule
from nonebot_plugin_orm import get_session

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
    create_game,
    game_of_user,
    get_game,
    join_signup,
    leave_signup,
    stop_game,
)

config = get_plugin_config(Config)


def _is_su(user_id: int) -> bool:
    """是否为超级用户。"""
    return str(user_id) in get_driver().config.superusers


def _signup_cap(game_module_max: int | None) -> int:
    """报名人数上限：配置与模组上限取较小值。"""
    if game_module_max is None:
        return config.rpg_max_players
    return min(config.rpg_max_players, game_module_max)


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
    game = create_game(group_id, user_id)
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
    game.action_queue.put_nowait(Action(ActionKind.MODULE_SELECT, user_id, aux=text))
    await select_module_cmd.finish()


signup_cmd = on_command(
    "报名",
    aliases={"上车", "加一"},
    priority=5,
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
    cap = _signup_cap(game.module.max_players if game.module else None)
    if len(game.signup_user_ids) >= cap:
        await signup_cmd.finish("报名已满员，等待开局~")
    if not join_signup(game, int(event.get_user_id())):
        await signup_cmd.finish("你已在局中，无需重复报名~")
    logger.info(
        f"跑团群 {int(event.group_id)} {int(event.get_user_id())} 报名"
        f"（{len(game.signup_user_ids)}/{cap}）"
    )
    await signup_cmd.finish(
        MessageSegment.at(event.user_id)
        + f"报名成功！当前 {len(game.signup_user_ids)}/{cap} 人"
    )


leave_cmd = on_command(
    "退报名",
    aliases={"下车"},
    priority=5,
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
    if not leave_signup(game, user_id):
        await leave_cmd.finish("你还没有报名~")
    logger.info(f"跑团群 {int(event.group_id)} {user_id} 退报名")
    if not game.signup_user_ids:
        # 空房：直接取消引擎任务。阶段置 ENDED 后命令层自播"房间已解散"，
        # 引擎取消分支见到 ENDED 不再重复播报"对局已被强制结束"
        game.phase = Phase.ENDED
        task = game.worker
        game.worker = None
        if task is not None and not task.done():
            task.cancel()
        logger.info(f"跑团群 {int(event.group_id)} 空房解散")
        await leave_cmd.finish("房间已解散")
    if game.host_user_id == user_id:
        game.host_user_id = game.signup_user_ids[0]
        await leave_cmd.finish(f"已退报名，房主移交给 {game.host_user_id}")
    await leave_cmd.finish("已退出报名~")


view_cmd = on_command(
    "查看报名",
    aliases={"报名情况"},
    priority=5,
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


start_cmd = on_command(
    "开始游戏",
    aliases={"发车"},
    priority=5,
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
    game.action_queue.put_nowait(Action(ActionKind.START_GAME, user_id))
    logger.info(
        f"跑团群 {int(event.group_id)} {user_id} 请求开始游戏"
        f"（{len(game.signup_user_ids)} 人）"
    )
    await start_cmd.finish("已请求开始游戏~")


end_cmd = on_command(
    "结束游戏",
    aliases={"解散团"},
    priority=5,
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
    game.action_queue.put_nowait(
        Action(ActionKind.CHECK, int(event.get_user_id()), aux=str(arg).strip())
    )
    await check_cmd.finish()


talk_cmd = on_command("对话", aliases={"询问"}, priority=5, block=True)


@talk_cmd.handle()
async def handle_talk(
    event: GroupMessageEvent,
    arg: Message = CommandArg(),
    _perm: None = require_feature("rpg"),  # pyright: ignore[reportArgumentType]
) -> None:
    """与在场 NPC 交谈：/对话 NPC名 要说的话。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.PLAY:
        await talk_cmd.finish("现在不在跑团进行中")
    text = str(arg).strip()
    if not text:
        await talk_cmd.finish("格式：/对话 NPC名 要说的话")
    game.action_queue.put_nowait(
        Action(ActionKind.TALK_NPC, int(event.get_user_id()), aux=text)
    )
    await talk_cmd.finish()


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
    target = str(arg).strip()
    if not target:
        await attack_cmd.finish("格式：/攻击 目标名")
    game.action_queue.put_nowait(
        Action(ActionKind.ATTACK, int(event.get_user_id()), aux=target)
    )
    await attack_cmd.finish()


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
    target = str(arg).strip()
    if not target:
        await move_cmd.finish("格式：/前往 地点名")
    game.action_queue.put_nowait(
        Action(ActionKind.MOVE, int(event.get_user_id()), aux=target)
    )
    await move_cmd.finish()


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
    _perm: None = require_feature("rpg"),  # pyright: ignore[reportArgumentType]
) -> None:
    """列出已发现的线索。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.PLAY:
        await clue_cmd.finish("现在不在跑团进行中")
    module = game.module
    if not game.discovered_clues or module is None:
        await clue_cmd.finish("还没有发现任何线索~")
    names = [
        clue.name
        for cid in sorted(game.discovered_clues)
        if (clue := module.clue(cid)) is not None
    ]
    await clue_cmd.finish("已发现线索：" + ("、".join(names) if names else "无"))


# ── 私聊建卡监听 ──────────────────────────────────────────


async def _is_char_create_dm(event: MessageEvent) -> bool:
    """规则：私聊 ∧ 用户在建卡阶段的对局中 ∧ 未被禁用跑团功能。"""
    if isinstance(event, GroupMessageEvent):
        return False
    user_id = int(event.get_user_id())
    game = game_of_user(user_id)
    if game is None or game.phase is not Phase.CHAR_CREATE:
        return False
    # 与命令处理器的 require_feature 一致：按私聊解析链查全局用户开关
    async with get_session() as session:
        return await check_feature_permission(
            user_id,
            None,
            "rpg",
            session,  # pyright: ignore[reportArgumentType]
        )


private_listener = on_message(
    rule=Rule(_is_char_create_dm),
    priority=0,
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
    game.action_queue.put_nowait(action)
    await private_listener.finish()


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
    async with get_session() as session:
        return await check_feature_permission(
            int(event.get_user_id()),
            int(event.group_id),
            "rpg",
            session,  # pyright: ignore[reportArgumentType]
        )


speech_listener = on_message(
    rule=Rule(_is_game_speech),
    priority=0,
    block=False,  # 不阻断命令匹配；仅旁路投递 SAY
)


@speech_listener.handle()
async def handle_game_speech(event: GroupMessageEvent) -> None:
    """在局玩家的群自由发言 → SAY 行动（交给 KP 智能体循环）。"""
    game = get_game(int(event.group_id))
    if game is None or game.phase is not Phase.PLAY:
        return
    text = event.get_plaintext().strip()
    # 空消息与命令（/检定 等）不算自由发言
    if not text or text.startswith("/"):
        return
    game.action_queue.put_nowait(
        Action(ActionKind.SAY, int(event.get_user_id()), aux=text)
    )
