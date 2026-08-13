"""YawnBot 跑团子插件：CoC 7版群聊 TRPG，AI 主持人(KP)主持。

作为 yawn_core 的可选子插件加载（父插件不硬依赖本包）：
群内 `/跑团` 开房选模组，人物卡系统生成、私聊微调，局内
自由发言由路由器分发给 KP 或 NPC 社交系统；KP 通过 tool_call 驱动系统判定与
剧情分支，引擎验证并执行每一次工具调用（骰子与数值始终
由系统掌控）。命令元数据由父插件 help_panel 扫描。
"""

from nonebot import get_plugin_config, logger
from nonebot.plugin import PluginMetadata

from . import commands, models  # noqa: F401
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="跑团",
    description="CoC 7版群聊跑团：AI 主持人主持，按模组推进剧情",
    usage=(
        "发送 /跑团 开房，/选择模组 N 选定剧本，/报名 加入；"
        "开局后可用 /局面 查看状态，直接用自然语言行动或与 NPC 交谈，"
        "个人线索可用 /分享线索 公开"
    ),
    config=Config,
    extra={
        "commands": [
            {
                "name": "跑团",
                "aliases": ["开团", "TRPG"],
                "description": "开设跑团房间（房主自动报名）",
                "feature": "rpg",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "模组列表",
                "aliases": ["模组"],
                "description": "列出可选剧本模组",
                "feature": "rpg",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "选择模组",
                "aliases": ["选模组"],
                "description": "报名阶段选定剧本（选择模组 N，房主/群管/超管）",
                "feature": "rpg",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "报名",
                "aliases": ["上车", "加一"],
                "description": "报名加入跑团",
                "feature": "rpg",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "退报名",
                "aliases": ["下车"],
                "description": "退出跑团报名",
                "feature": "rpg",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "查看报名",
                "aliases": ["报名情况"],
                "description": "查看报名名单",
                "feature": "rpg",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "开始游戏",
                "aliases": ["发车"],
                "description": "开始游戏（房主/群管/超管）",
                "feature": "rpg",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "结束游戏",
                "aliases": ["解散团"],
                "description": "结束跑团（房主/群管/超管）",
                "feature": "rpg",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "检定",
                "aliases": ["rc"],
                "description": "局内显式技能检定（检定 技能名，可带难度）",
                "feature": "rpg",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "攻击",
                "aliases": ["打"],
                "description": "攻击场景中的怪物或 NPC（攻击 目标名）",
                "feature": "rpg",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "前往",
                "aliases": ["去"],
                "description": "经出口切换场景（前往 地点名）",
                "feature": "rpg",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "时间",
                "aliases": ["时辰"],
                "description": "查看游戏内时钟",
                "feature": "rpg",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "等待",
                "aliases": ["休息"],
                "description": "原地等待 N 分钟（等待 N，缺省 30）",
                "feature": "rpg",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "状态",
                "aliases": ["我的状态"],
                "description": "查看自己的 HP/SAN/属性",
                "feature": "rpg",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "技能",
                "aliases": ["技能列表"],
                "description": "查看自己的技能值",
                "feature": "rpg",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "线索",
                "aliases": ["已发现线索"],
                "description": "群内查看公共线索，私聊查看自己的完整调查手记",
                "feature": "rpg",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "局面",
                "aliases": ["当前局面", "跑团状态"],
                "description": "查看公开局面，并私聊接收自己的状态与私人信息",
                "feature": "rpg",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "协助",
                "aliases": ["帮忙"],
                "description": "协助同场景调查员的下一次技能检定",
                "feature": "rpg",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "分享线索",
                "aliases": ["公开线索"],
                "description": "把自己的个人线索公开给队伍",
                "feature": "rpg",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "跳过",
                "aliases": ["结束行动"],
                "description": "结束本探索轮或当前战斗行动",
                "feature": "rpg",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "分享情报",
                "aliases": ["公开情报"],
                "description": "公开自己从 NPC 获得的个人情报",
                "feature": "rpg",
                "scope": "group",
                "superuser": False,
            },
        ],
    },
)

config = get_plugin_config(Config)

logger.info("跑团子插件已加载")
