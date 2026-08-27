"""本子插件的命令声明；matcher 与帮助目录共享同一数据源。"""

from ..command_definition import CommandDefinition, command_map  # noqa: TID252

COMMAND_DEFINITIONS = (
    CommandDefinition(
        name="群聊Agent",
        aliases=("群AI",),
        description="群聊 Agent 开关与概况",
        feature="group_agent",
        scope="group",
        permission="group_admin",
        namespace_path=("Agent",),
    ),
    CommandDefinition(
        name="Agent状态",
        description="查看群聊 Agent 当前状态",
        feature="group_agent",
        scope="group",
        namespace_path=("Agent", "状态"),
    ),
    CommandDefinition(
        name="Agent记忆",
        description="查看已沉淀的群记忆",
        feature="group_agent",
        scope="group",
        display_level="advanced",
        namespace_path=("Agent", "记忆"),
    ),
    CommandDefinition(
        name="Agent画像",
        description="查看群成员人物画像",
        feature="group_agent",
        scope="group",
        display_level="advanced",
        namespace_path=("Agent", "画像"),
    ),
    CommandDefinition(
        name="Agent隐私",
        description="退出或恢复本群 Agent 记忆",
        feature="group_agent",
        scope="group",
        display_level="advanced",
        namespace_path=("Agent", "隐私"),
    ),
    CommandDefinition(
        name="Agent设置",
        description="设置暖场概率、插话概率、冷却、媒体缓存等参数",
        feature="group_agent",
        scope="group",
        permission="group_admin",
        display_level="advanced",
        namespace_path=("Agent", "设置"),
    ),
    CommandDefinition(
        name="Agent清理",
        description="清空本群 Agent 记忆",
        feature="group_agent",
        scope="group",
        permission="group_admin",
        display_level="advanced",
        namespace_path=("Agent", "清理"),
    ),
    CommandDefinition(
        name="Agent导出",
        description="导出本群 Agent 记忆数据",
        feature="group_agent",
        scope="group",
        permission="group_admin",
        display_level="advanced",
        namespace_path=("Agent", "导出"),
    ),
    CommandDefinition(
        name="Agent人设",
        description="查看或设置群级人设",
        feature="group_agent",
        scope="group",
        permission="group_admin",
        display_level="advanced",
        namespace_path=("Agent", "人设"),
    ),
)

COMMAND_BY_NAME = command_map(COMMAND_DEFINITIONS)
