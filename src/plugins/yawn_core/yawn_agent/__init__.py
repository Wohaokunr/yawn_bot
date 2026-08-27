# ruff: noqa: E501,F401,I001,TID252,PLR0912,PLR0913,PLR0917,C901,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100
"""QQ 群聊 Agent 子插件。

该包把 OneBot V11 的消息解析、群聊上下文、记忆和工具调用收敛为独立
子插件；所有持久化仍使用 yawn_core 的 ORM bind。
"""

from nonebot.plugin import PluginMetadata

from ..command_catalog import PluginCommandGroup, register_command_group
from .command_definitions import COMMAND_DEFINITIONS
from . import (
    agent,
    capabilities,
    collector,
    commands,
    config_store,
    context,
    conversation,
    dialogue,
    media,
    memory,
    message_parser,
    persona,
    proactive,
    prompt,
    tools,
)

COMMAND_GROUP = register_command_group(
    PluginCommandGroup(
        plugin_id="yawn_agent",
        display_name="群聊 Agent",
        entrypoint="群聊Agent",
        help_section="agent",
        commands=COMMAND_DEFINITIONS,
    )
)

__plugin_meta__ = PluginMetadata(
    name="群聊 Agent",
    description="理解群聊上下文、人物关系和可控工具调用的 QQ 群聊 Agent",
    usage="/群聊Agent 开启或查看状态；/Agent设置；/Agent记忆；/Agent清理",
    extra={"command_group": COMMAND_GROUP},
)

__all__ = [
    "agent",
    "capabilities",
    "collector",
    "commands",
    "config_store",
    "context",
    "conversation",
    "dialogue",
    "media",
    "memory",
    "message_parser",
    "persona",
    "proactive",
    "prompt",
    "tools",
]
