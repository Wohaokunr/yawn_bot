"""YawnBot 跑团子插件：CoC 7版群聊 TRPG。"""

from nonebot import get_driver, get_plugin_config, logger
from nonebot.plugin import PluginMetadata

from ..command_catalog import (  # noqa: TID252
    CommandSpec,
    PluginCommandGroup,
    register_command_group,
)
from . import commands, models  # noqa: F401
from .command_context import get_available_commands, get_help_hint
from .config import Config
from .state import stop_all_games

config = get_plugin_config(Config)


@get_driver().on_shutdown
async def _stop_rpg_games() -> None:
    """服务关闭时立即终止所有内存中的 RPG 对局，不做恢复。"""

    await stop_all_games("server_shutdown")
    logger.info("跑团活跃对局已因服务关闭终止")


COMMAND_GROUP = register_command_group(
    PluginCommandGroup(
        plugin_id="yawn_rpg",
        display_name="跑团",
        entrypoint="跑团",
        help_section="rpg",
        get_available_commands=get_available_commands,
        get_help_hint=get_help_hint,
        commands=(
            CommandSpec(
                name="跑团",
                aliases=("开团", "TRPG"),
                description="开设跑团房间（房主自动报名）",
                feature="rpg",
                scope="group",
                display_level="entry",
            ),
            CommandSpec(
                name="模组列表",
                aliases=("模组",),
                description="列出可选剧本模组",
                feature="rpg",
                scope="group",
                display_level="entry",
            ),
            CommandSpec(
                name="跑团帮助",
                aliases=("TRPG帮助",),
                description="按当前阶段查看新手玩法引导",
                feature="rpg",
                display_level="entry",
            ),
            CommandSpec(
                name="选择模组",
                aliases=("选模组",),
                description="选定剧本（房主/群管/超管）",
                feature="rpg",
                scope="group",
                permission="room_host_or_admin",
                display_level="lobby",
            ),
            CommandSpec(
                name="报名",
                aliases=("上车", "加一"),
                description="报名加入跑团",
                feature="rpg",
                scope="group",
                display_level="lobby",
            ),
            CommandSpec(
                name="退报名",
                aliases=("下车",),
                description="退出跑团报名",
                feature="rpg",
                scope="group",
                permission="player",
                display_level="lobby",
            ),
            CommandSpec(
                name="查看报名",
                aliases=("报名情况",),
                description="查看报名名单",
                feature="rpg",
                scope="group",
                display_level="lobby",
            ),
            CommandSpec(
                name="开始游戏",
                aliases=("发车",),
                description="开始游戏（房主/群管/超管）",
                feature="rpg",
                scope="group",
                permission="room_host_or_admin",
                display_level="lobby",
            ),
            CommandSpec(
                name="局面",
                aliases=("当前局面", "跑团状态"),
                description="查看公开局面并私聊接收个人状态",
                feature="rpg",
                scope="group",
                display_level="active",
            ),
            CommandSpec(
                name="状态",
                aliases=("我的状态",),
                description="查看自己的 HP/SAN/属性",
                feature="rpg",
                scope="group",
                permission="player",
                display_level="active",
            ),
            CommandSpec(
                name="技能",
                aliases=("技能列表",),
                description="查看自己的技能值",
                feature="rpg",
                scope="group",
                permission="player",
                display_level="active",
            ),
            CommandSpec(
                name="线索",
                aliases=("已发现线索",),
                description="查看公共线索和自己的调查手记",
                feature="rpg",
                scope="group",
                permission="player",
                display_level="active",
            ),
            CommandSpec(
                name="线索板",
                aliases=("证据板",),
                description="查看团队公开线索和联合推理进度",
                feature="rpg",
                scope="group",
                display_level="active",
            ),
            CommandSpec(
                name="时间",
                aliases=("时辰",),
                description="查看游戏内时钟",
                feature="rpg",
                scope="group",
                display_level="active",
            ),
            CommandSpec(
                name="结束游戏",
                aliases=("解散团",),
                description="结束跑团（房主/群管/超管）",
                feature="rpg",
                scope="group",
                permission="room_host_or_admin",
                display_level="contextual",
            ),
            CommandSpec(
                name="检定",
                aliases=("rc",),
                description="执行显式技能检定",
                feature="rpg",
                scope="group",
                permission="player",
                display_level="contextual",
            ),
            CommandSpec(
                name="攻击",
                aliases=("打",),
                description="攻击场景中的怪物或 NPC",
                feature="rpg",
                scope="group",
                permission="player",
                display_level="contextual",
            ),
            CommandSpec(
                name="前往",
                aliases=("去",),
                description="经出口切换场景",
                feature="rpg",
                scope="group",
                permission="player",
                display_level="contextual",
            ),
            CommandSpec(
                name="等待",
                aliases=("休息",),
                description="原地等待指定分钟数",
                feature="rpg",
                scope="group",
                permission="player",
                display_level="contextual",
            ),
            CommandSpec(
                name="协助",
                aliases=("帮忙",),
                description="协助同场景调查员的下一次检定",
                feature="rpg",
                scope="group",
                permission="player",
                display_level="contextual",
            ),
            CommandSpec(
                name="分享线索",
                aliases=("公开线索",),
                description="把自己的个人线索公开给队伍",
                feature="rpg",
                scope="group",
                permission="player",
                display_level="contextual",
            ),
            CommandSpec(
                name="推理",
                aliases=("联合推理",),
                description="组合线索发起团队推理",
                feature="rpg",
                scope="group",
                permission="player",
                display_level="contextual",
            ),
            CommandSpec(
                name="赞成推理",
                description="确认当前团队推理提案",
                feature="rpg",
                scope="group",
                permission="player",
                display_level="contextual",
            ),
            CommandSpec(
                name="撤回推理",
                description="撤回自己发起的团队推理提案",
                feature="rpg",
                scope="group",
                permission="player",
                display_level="contextual",
            ),
            CommandSpec(
                name="跳过",
                aliases=("结束行动",),
                description="结束本探索轮或当前战斗行动",
                feature="rpg",
                scope="group",
                permission="player",
                display_level="contextual",
            ),
            CommandSpec(
                name="分享情报",
                aliases=("公开情报",),
                description="公开自己从 NPC 获得的个人情报",
                feature="rpg",
                scope="group",
                permission="player",
                display_level="contextual",
            ),
            CommandSpec(
                name="跳过引导",
                description="停止自动新手提示",
                feature="rpg",
                display_level="advanced",
            ),
            CommandSpec(
                name="重新引导",
                description="重置当前版本的新手引导状态",
                feature="rpg",
                display_level="advanced",
            ),
        ),
    )
)

__plugin_meta__ = PluginMetadata(
    name="跑团",
    description="CoC 7版群聊跑团：AI 主持人按模组推进剧情",
    usage=(
        "发送 /跑团 开房，/选择模组 N 选定剧本，/报名 加入；"
        "开局后可用 /局面 查看状态，直接用自然语言行动"
    ),
    config=Config,
    extra={"command_group": COMMAND_GROUP},
)

logger.info("跑团子插件已加载")
