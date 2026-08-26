"""番茄免费小说公开内容下载子插件。

本插件只处理无需登录、无需验证码且公开可访问的页面。下载正文只在本地
临时文件中保存，任务记录与正文内容分离，方便任务恢复与权限审计。
"""

from nonebot import get_plugin_config, logger
from nonebot.plugin import PluginMetadata

from ..command_catalog import (  # noqa: TID252
    CommandSpec,
    PluginCommandGroup,
    register_command_group,
)
from . import commands, models  # noqa: F401
from .config import Config

COMMAND_GROUP = register_command_group(
    PluginCommandGroup(
        plugin_id="yawn_fanqie",
        display_name="番茄小说",
        entrypoint="番茄小说",
        help_section="fanqie",
        commands=(
            CommandSpec(
                name="番茄小说",
                aliases=("番茄下载", "下载小说"),
                description="模糊搜索、浏览榜单并下载番茄免费小说公开章节",
                feature="fanqie",
            ),
            CommandSpec(
                name="番茄任务",
                aliases=("小说任务",),
                description="查看或管理番茄小说下载任务",
                feature="fanqie",
                display_level="advanced",
            ),
        ),
    )
)

__plugin_meta__ = PluginMetadata(
    name="番茄小说",
    description="模糊搜索、浏览番茄公开榜单并下载公开章节为 TXT",
    usage=(
        "发送 /番茄小说，按提示搜索/选择榜单和章节；"
        "发送 /番茄任务 查看进度或管理已有任务"
    ),
    config=Config,
    extra={"command_group": COMMAND_GROUP},
)

config = get_plugin_config(Config)

logger.info("番茄小说子插件已加载")
