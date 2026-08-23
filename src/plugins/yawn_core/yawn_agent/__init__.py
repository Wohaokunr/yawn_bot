# ruff: noqa: E501,F401,I001,TID252,PLR0912,PLR0913,PLR0917,C901,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100
"""QQ 群聊 Agent 子插件。

该包把 OneBot V11 的消息解析、群聊上下文、记忆和工具调用收敛为独立
子插件；所有持久化仍使用 yawn_core 的 ORM bind。
"""

from nonebot.plugin import PluginMetadata

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

# help_panel 扫描子包时只读取包级 __plugin_meta__；命令登记必须在这里，
# 而不是某个子模块内。
__plugin_meta__ = PluginMetadata(
    name="群聊 Agent",
    description="理解群聊上下文、人物关系和可控工具调用的 QQ 群聊 Agent",
    usage="/群聊Agent 开启或查看状态；/Agent设置；/Agent记忆；/Agent清理",
    extra={
        "commands": [
            {
                "name": "群聊Agent",
                "aliases": ["群AI"],
                "description": "群聊 Agent 开关与概况",
                "feature": "group_agent",
                "scope": "group",
                "admin": True,
                "superuser": False,
            },
            {
                "name": "Agent设置",
                "aliases": [],
                "description": "设置暖场概率、插话概率、冷却、媒体缓存等参数",
                "feature": "group_agent",
                "scope": "group",
                "admin": True,
                "superuser": False,
            },
            {
                "name": "Agent状态",
                "aliases": [],
                "description": "查看群聊 Agent 当前状态",
                "feature": "group_agent",
                "scope": "group",
                "admin": False,
                "superuser": False,
            },
            {
                "name": "Agent记忆",
                "aliases": [],
                "description": "查看已沉淀的群记忆",
                "feature": "group_agent",
                "scope": "group",
                "admin": False,
                "superuser": False,
            },
            {
                "name": "Agent画像",
                "aliases": [],
                "description": "查看群成员人物画像",
                "feature": "group_agent",
                "scope": "group",
                "admin": False,
                "superuser": False,
            },
            {
                "name": "Agent清理",
                "aliases": [],
                "description": "清空本群 Agent 记忆",
                "feature": "group_agent",
                "scope": "group",
                "admin": True,
                "superuser": False,
            },
            {
                "name": "Agent导出",
                "aliases": [],
                "description": "导出本群 Agent 记忆数据",
                "feature": "group_agent",
                "scope": "group",
                "admin": True,
                "superuser": False,
            },
            {
                "name": "Agent人设",
                "aliases": [],
                "description": "查看或设置群级人设",
                "feature": "group_agent",
                "scope": "group",
                "admin": True,
                "superuser": False,
            },
            {
                "name": "Agent隐私",
                "aliases": [],
                "description": "退出或恢复本群 Agent 记忆",
                "feature": "group_agent",
                "scope": "group",
                "admin": False,
                "superuser": False,
            },
        ]
    },
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
