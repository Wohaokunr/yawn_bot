"""YawnBot 狼人杀子插件。"""

from nonebot import get_plugin_config, logger
from nonebot.plugin import PluginMetadata

from ..command_catalog import (  # noqa: TID252
    PluginCommandGroup,
    register_command_group,
)
from . import commands, models  # noqa: F401
from .command_context import get_available_commands, get_help_hint
from .command_definitions import COMMAND_DEFINITIONS
from .config import Config

config = get_plugin_config(Config)

COMMAND_GROUP = register_command_group(
    PluginCommandGroup(
        plugin_id="yawn_werewolf",
        display_name="狼人杀",
        entrypoint="狼人杀",
        help_section="werewolf",
        get_available_commands=get_available_commands,
        get_help_hint=get_help_hint,
        commands=COMMAND_DEFINITIONS,
    )
)

__plugin_meta__ = PluginMetadata(
    name="狼人杀",
    description="群聊狼人杀：群内报名、私聊行动、禁言控场",
    usage="发送 /狼人杀 开房，/报名 加入，夜间按私聊提示行动",
    config=Config,
    extra={"command_group": COMMAND_GROUP},
)

logger.info("狼人杀子插件已加载")
