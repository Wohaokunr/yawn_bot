# ruff: noqa: E501,F401,I001,TID252,PLR0912,PLR0913,PLR0917,C901,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100
"""QQ 群聊 Agent 子插件。

该包把 OneBot V11 的消息解析、群聊上下文、记忆和工具调用收敛为独立
子插件；所有持久化仍使用 yawn_core 的 ORM bind。
"""

from nonebot.plugin import PluginMetadata

from ..command_catalog import CommandSpec, PluginCommandGroup, register_command_group
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
        commands=(
            CommandSpec(
                name="群聊Agent",
                aliases=("群AI",),
                description="群聊 Agent 开关与概况",
                feature="group_agent",
                scope="group",
                permission="group_admin",
            ),
            CommandSpec(
                name="Agent状态",
                description="查看群聊 Agent 当前状态",
                feature="group_agent",
                scope="group",
            ),
            CommandSpec(
                name="Agent记忆",
                description="查看已沉淀的群记忆",
                feature="group_agent",
                scope="group",
                display_level="advanced",
            ),
            CommandSpec(
                name="Agent画像",
                description="查看群成员人物画像",
                feature="group_agent",
                scope="group",
                display_level="advanced",
            ),
            CommandSpec(
                name="Agent隐私",
                description="退出或恢复本群 Agent 记忆",
                feature="group_agent",
                scope="group",
                display_level="advanced",
            ),
            CommandSpec(
                name="Agent设置",
                description="设置暖场概率、插话概率、冷却、媒体缓存等参数",
                feature="group_agent",
                scope="group",
                permission="group_admin",
                display_level="advanced",
            ),
            CommandSpec(
                name="Agent清理",
                description="清空本群 Agent 记忆",
                feature="group_agent",
                scope="group",
                permission="group_admin",
                display_level="advanced",
            ),
            CommandSpec(
                name="Agent导出",
                description="导出本群 Agent 记忆数据",
                feature="group_agent",
                scope="group",
                permission="group_admin",
                display_level="advanced",
            ),
            CommandSpec(
                name="Agent人设",
                description="查看或设置群级人设",
                feature="group_agent",
                scope="group",
                permission="group_admin",
                display_level="advanced",
            ),
        ),
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
