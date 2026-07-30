"""YawnBot 狼人杀子插件：经典预女猎白群聊小游戏。

作为 yawn_core 的可选子插件加载（父插件不硬依赖本包）：
群内报名匹配，夜间私聊下达行动，群禁言控制发言秩序，
全部阶段带超时托管。命令元数据由父插件 help_panel 扫描。
"""

from nonebot import get_plugin_config, logger
from nonebot.plugin import PluginMetadata

from . import commands, models  # noqa: F401
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="狼人杀",
    description="经典预女猎白群聊狼人杀：群内报名、私聊行动、禁言控场",
    usage="发送 /狼人杀 开房，/报名 加入，夜间按私聊提示行动",
    config=Config,
    extra={
        "commands": [
            {
                "name": "狼人杀",
                "aliases": ["开狼", "来把狼人杀"],
                "description": "开设狼人杀房间（房主自动报名）",
                "feature": "werewolf",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "报名",
                "aliases": ["上车", "加一"],
                "description": "报名加入狼人杀",
                "feature": "werewolf",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "退报名",
                "aliases": ["下车"],
                "description": "退出狼人杀报名",
                "feature": "werewolf",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "查看报名",
                "aliases": ["报名情况"],
                "description": "查看报名名单",
                "feature": "werewolf",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "开始游戏",
                "aliases": ["发车"],
                "description": "开始游戏（房主/群管/超管）",
                "feature": "werewolf",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "结束游戏",
                "aliases": ["解散狼局"],
                "description": "结束对局；无对局时恢复群禁言（房主/群管/超管）",
                "feature": "werewolf",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "战绩",
                "aliases": ["狼人战绩"],
                "description": "查看狼人杀胜率（群管/超管可 @ 他人）",
                "feature": "werewolf",
                "scope": "all",
                "superuser": False,
            },
            {
                "name": "刀",
                "aliases": ["狼刀"],
                "description": "夜晚狼人击杀（私聊，刀N）",
                "feature": "werewolf",
                "scope": "private",
                "superuser": False,
            },
            {
                "name": "查验",
                "aliases": ["验"],
                "description": "夜晚预言家查验（私聊，查验N）",
                "feature": "werewolf",
                "scope": "private",
                "superuser": False,
            },
            {
                "name": "救",
                "aliases": [],
                "description": "夜晚女巫救人（私聊）",
                "feature": "werewolf",
                "scope": "private",
                "superuser": False,
            },
            {
                "name": "毒",
                "aliases": [],
                "description": "夜晚女巫毒人（私聊，毒N）",
                "feature": "werewolf",
                "scope": "private",
                "superuser": False,
            },
            {
                "name": "开枪",
                "aliases": ["带"],
                "description": "猎人开枪带走玩家（私聊，开枪N）",
                "feature": "werewolf",
                "scope": "private",
                "superuser": False,
            },
            {
                "name": "不开枪",
                "aliases": ["压枪"],
                "description": "猎人放弃开枪（私聊）",
                "feature": "werewolf",
                "scope": "private",
                "superuser": False,
            },
            {
                "name": "上警",
                "aliases": ["竞选"],
                "description": "报名竞选警长",
                "feature": "werewolf",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "退水",
                "aliases": [],
                "description": "退出警长竞选",
                "feature": "werewolf",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "排序",
                "aliases": [],
                "description": "警长决定发言顺序（排序 N 顺|逆）",
                "feature": "werewolf",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "移交警徽",
                "aliases": [],
                "description": "死亡警长移交警徽（移交警徽 N）",
                "feature": "werewolf",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "撕警徽",
                "aliases": [],
                "description": "死亡警长撕掉警徽",
                "feature": "werewolf",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "自爆",
                "aliases": [],
                "description": "狼人白天自爆，立即进入夜晚",
                "feature": "werewolf",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "投票",
                "aliases": ["票"],
                "description": "投票（警长/放逐/PK，投票 N）",
                "feature": "werewolf",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "弃票",
                "aliases": [],
                "description": "放弃投票",
                "feature": "werewolf",
                "scope": "group",
                "superuser": False,
            },
            {
                "name": "过",
                "aliases": ["跳过"],
                "description": "提前结束自己的发言/遗言",
                "feature": "werewolf",
                "scope": "group",
                "superuser": False,
            },
        ],
    },
)

config = get_plugin_config(Config)

logger.info("狼人杀子插件已加载")
