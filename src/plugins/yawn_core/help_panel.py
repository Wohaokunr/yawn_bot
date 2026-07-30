"""帮助面板模块：展示当前用户可用的所有命令。

基于 NoneBot2 内置 PluginMetadata 声明命令元数据，
运行时扫描包内子模块自动发现，
结合现有权限系统按用户/场景过滤展示。
"""

import importlib
import pkgutil
from typing import Optional

from nonebot import get_driver, logger
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    MessageEvent,
)
from nonebot.plugin import PluginMetadata, on_command
from nonebot_plugin_orm import async_scoped_session

from .permission import get_user_feature_status, is_group_admin

logger.info("帮助面板模块已加载")

# ── 命令匹配器 ────────────────────────────────────────────

help_cmd = on_command(
    "help",
    aliases={"帮助", "命令"},
    priority=5,
    block=True,
)


# ── 辅助函数 ──────────────────────────────────────────────


def _build_help_text(commands: list[dict]) -> str:
    """构建帮助文本。"""
    lines: list[str] = ["═══ YawnBot 帮助 ═══", ""]

    if not commands:
        lines.append("当前没有可用的命令。")
    else:
        for cmd in commands:
            name = cmd["name"]
            desc = cmd.get("description", "")
            aliases = cmd.get("aliases", [])

            # 主命令行
            line = f"/{name}"
            if desc:
                line += f" - {desc}"
            lines.append(line)

            # 别名（如果有）
            if aliases:
                alias_str = "、".join(f"/{a}" for a in aliases)
                lines.append(f"  别名: {alias_str}")

    lines.append("")
    lines.append("──────────────")
    lines.append("发送 /帮助 查看此列表")
    return "\n".join(lines)


def _is_group_admin_or_su(
    event: MessageEvent,
    *,
    is_su: bool,
) -> bool:
    """检查是否为群管理员或超级用户。"""
    if is_su:
        return True
    if isinstance(event, GroupMessageEvent):
        return is_group_admin(event)
    return False


def _collect_plugin_metadata() -> list[PluginMetadata]:
    """扫描 yawn_core 包内子模块，收集 PluginMetadata。

    由于 yawn_core 是插件包（package），子模块不会被
    get_loaded_plugins() 作为独立插件返回，因此需要
    手动扫描各子模块的 __plugin_meta__ 变量。
    """
    from . import __name__ as pkg_name
    from . import __path__ as pkg_path

    result: list[PluginMetadata] = []
    for _importer, modname, ispkg in pkgutil.iter_modules(pkg_path):
        if ispkg and modname == "data_models":
            continue  # 跳过内部子包；业务子包（如 yawn_werewolf）参与收集
        try:
            mod = importlib.import_module(f".{modname}", pkg_name)
        except Exception:  # noqa: BLE001
            continue
        meta = getattr(mod, "__plugin_meta__", None)
        if isinstance(meta, PluginMetadata):
            result.append(meta)
    return result


# ── 事件处理 ──────────────────────────────────────────────


@help_cmd.handle()
async def handle_help(
    event: MessageEvent,
    session: async_scoped_session,
) -> None:
    """处理 /help 命令，展示当前用户可用的命令列表。"""
    user_id = int(event.get_user_id())
    group_id: Optional[int] = getattr(event, "group_id", None)
    if group_id is not None:
        group_id = int(group_id)

    superusers = get_driver().config.superusers
    is_su = str(user_id) in superusers

    # 1. 获取当前用户的功能权限状态
    statuses = await get_user_feature_status(user_id, group_id, session)
    enabled_features = {key for key, _, enabled, _ in statuses if enabled}

    # 2. 扫描包内子模块的 PluginMetadata，收集命令
    commands: list[dict] = []
    is_admin = _is_group_admin_or_su(event, is_su=is_su)

    for meta in _collect_plugin_metadata():
        extra_cmds = meta.extra.get("commands", [])
        for cmd in extra_cmds:
            # 过滤：superuser 命令
            if cmd.get("superuser") and not is_su:
                continue
            # 过滤：需要群管权限的命令
            if cmd.get("admin") and not is_admin:
                continue
            # 过滤：scope
            scope = cmd.get("scope", "all")
            if scope == "group" and group_id is None:
                continue
            if scope == "private" and group_id is not None:
                continue
            # 过滤：feature 权限
            feature = cmd.get("feature")
            if feature and feature not in enabled_features:
                continue
            commands.append(cmd)

    # 3. 构建帮助文本并发送
    help_text = _build_help_text(commands)
    await help_cmd.finish(help_text)
