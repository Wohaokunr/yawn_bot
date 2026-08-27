"""YawnBot 跑团子插件：CoC 7版群聊 TRPG。"""

from nonebot import get_driver, get_plugin_config, logger
from nonebot.plugin import PluginMetadata

from ..command_catalog import (  # noqa: TID252
    PluginCommandGroup,
    register_command_group,
)
from . import commands, models  # noqa: F401
from .command_context import get_available_commands, get_help_hint
from .command_definitions import COMMAND_DEFINITIONS
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
        commands=COMMAND_DEFINITIONS,
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
