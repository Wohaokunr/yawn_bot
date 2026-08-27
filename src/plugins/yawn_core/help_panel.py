"""渐进式帮助：先选功能分类，再展示当前场景真正可用的命令。"""

from dataclasses import dataclass
from typing import Optional

from nonebot import get_driver, logger
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.matcher import Matcher
from nonebot.params import Arg, CommandArg
from nonebot.plugin import PluginMetadata, on_command
from nonebot_plugin_orm import async_scoped_session

from .command_catalog import (
    CommandContext,
    CommandSpec,
    HelpSectionKey,
    PluginCommandGroup,
    get_registered_command_groups,
    register_command_group,
)
from .command_ux import invalid_choice
from .permission import get_user_feature_status, is_group_admin
from .session_interaction import is_cancel
from .ui.panel_renderer import HelpMenuCard, render_help_menu

logger.info("帮助面板模块已加载")

help_cmd = on_command(
    "help",
    aliases={"帮助", "命令"},
    priority=5,
    block=True,
)

COMMAND_GROUP = register_command_group(
    PluginCommandGroup(
        plugin_id="yawn_core.help",
        display_name="帮助",
        entrypoint="help",
        commands=(
            CommandSpec(
                name="help",
                aliases=("帮助", "命令"),
                description="按功能分类查看当前可用命令",
                display_level="entry",
            ),
        ),
    )
)

__plugin_meta__ = PluginMetadata(
    name="帮助",
    description="先选择功能分类，再按权限和当前场景展示可用命令",
    usage="发送 /help 后回复分类；也可发送 /help 狼人杀 直接进入",
    extra={"command_group": COMMAND_GROUP},
)


@dataclass(frozen=True, slots=True)
class HelpSection:
    """第一层帮助菜单中的稳定分类。"""

    key: HelpSectionKey
    display_name: str
    summary: str
    entrypoint: str
    aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HelpSectionView:
    """一个分类在当前用户、会话和游戏状态下的可见内容。"""

    section: HelpSection
    groups: tuple[
        tuple[PluginCommandGroup, tuple[CommandSpec, ...], str | None],
        ...,
    ]


HELP_SECTIONS = (
    HelpSection(
        key="basic",
        display_name="个人与基础功能",
        summary="签到、个人面板等日常功能",
        entrypoint="面板",
        aliases=("个人", "基础", "基础功能", "个人功能", "常用"),
    ),
    HelpSection(
        key="agent",
        display_name="群聊 Agent",
        summary="查看或管理群聊智能助手",
        entrypoint="Agent状态",
        aliases=("Agent", "群聊Agent", "群AI", "AI"),
    ),
    HelpSection(
        key="rpg",
        display_name="跑团",
        summary="创建或参与 CoC 跑团",
        entrypoint="跑团",
        aliases=("TRPG", "COC"),
    ),
    HelpSection(
        key="werewolf",
        display_name="狼人杀",
        summary="创建或参与狼人杀对局",
        entrypoint="狼人杀",
        aliases=("狼人", "开狼"),
    ),
    HelpSection(
        key="fanqie",
        display_name="番茄小说",
        summary="搜索和下载公开免费章节",
        entrypoint="番茄小说",
        aliases=("番茄", "小说", "番茄下载"),
    ),
    HelpSection(
        key="admin",
        display_name="管理功能",
        summary="管理群功能、定时提醒和审批",
        entrypoint="群管理",
        aliases=("管理", "管理员", "群管理"),
    ),
)


def _is_group_admin_or_su(event: MessageEvent, *, is_su: bool) -> bool:
    """检查是否为群管理员或超级用户。"""

    if is_su:
        return True
    return isinstance(event, GroupMessageEvent) and is_group_admin(event)


def _command_is_visible(
    command: CommandSpec,
    *,
    group_id: int | None,
    enabled_features: set[str],
    context: CommandContext,
) -> bool:
    """处理跨插件通用的权限、作用域和功能开关。"""

    return not (
        (command.scope == "group" and group_id is None)
        or (command.scope == "private" and group_id is not None)
        or (bool(command.feature) and command.feature not in enabled_features)
        or (command.permission == "superuser" and not context.is_superuser)
        or (command.permission == "group_admin" and not context.is_group_admin)
    )


def _collect_visible_sections(
    *,
    context: CommandContext,
    enabled_features: set[str],
) -> tuple[HelpSectionView, ...]:
    """从注册表按帮助分类收集当前真正可用的命令。"""

    registered_groups = get_registered_command_groups()
    views: list[HelpSectionView] = []
    for section in HELP_SECTIONS:
        section_groups: list[
            tuple[PluginCommandGroup, tuple[CommandSpec, ...], str | None]
        ] = []
        for group in registered_groups:
            commands = tuple(
                command
                for command in group.available_commands(context)
                if (command.help_section or group.help_section) == section.key
                and _command_is_visible(
                    command,
                    group_id=context.group_id,
                    enabled_features=enabled_features,
                    context=context,
                )
            )
            if commands:
                section_groups.append((group, commands, group.help_hint(context)))
        if section_groups:
            views.append(HelpSectionView(section, tuple(section_groups)))
    return tuple(views)


def _build_section_menu(sections: tuple[HelpSectionView, ...]) -> str:
    """构建只含分类、单句说明和主入口的第一层菜单。"""

    lines = ["═══ YawnBot 帮助 ═══", ""]
    for index, view in enumerate(sections, start=1):
        section = view.section
        lines.append(
            f"{index}. {section.display_name} — {section.summary}"
            f"（入口：/{section.entrypoint}）"
        )
    lines.extend(
        (
            "",
            "回复序号或分类名称继续；也可直接发送 /help 分类名称。",
            "发送“0 / 取消 / 退出”结束。",
        )
    )
    return "\n".join(lines)


def _build_section_text(view: HelpSectionView) -> str:
    """构建单个分类的第二层命令帮助。"""

    lines = [f"═══ {view.section.display_name} ═══"]
    for group_index, (group, commands, hint) in enumerate(view.groups):
        if group_index or len(view.groups) > 1:
            lines.extend(("", f"【{group.display_name}】"))
        for command in commands:
            aliases = ""
            if command.aliases:
                alias_text = "、".join(f"/{alias}" for alias in command.aliases)
                aliases = f"（别名：{alias_text}）"
            description = f" — {command.description}" if command.description else ""
            lines.append(f"/{command.name}{aliases}{description}")
        if hint:
            lines.append(f"提示：{hint}")
    lines.extend(("", "发送 /help 返回分类菜单。"))
    return "\n".join(lines)


def _normalize_topic(text: str) -> str:
    value = text.strip()
    for prefix in ("/help", "/帮助"):
        if value.lower().startswith(f"{prefix.lower()} "):
            value = value[len(prefix) :].strip()
            break
    return "".join(value.lower().split())


def _resolve_section(
    text: str,
    sections: tuple[HelpSectionView, ...],
) -> HelpSectionView | None:
    """按当前菜单序号、名称或别名解析分类。"""

    normalized = _normalize_topic(text)
    if normalized.isdigit():
        index = int(normalized)
        return sections[index - 1] if 1 <= index <= len(sections) else None
    for view in sections:
        names = (view.section.key, view.section.display_name, *view.section.aliases)
        if normalized in {_normalize_topic(name) for name in names}:
            return view
    return None


async def _current_help_sections(
    event: MessageEvent,
    session: async_scoped_session,
) -> tuple[HelpSectionView, ...]:
    user_id = int(event.get_user_id())
    group_id: Optional[int] = getattr(event, "group_id", None)
    if group_id is not None:
        group_id = int(group_id)

    is_su = str(user_id) in get_driver().config.superusers
    statuses = await get_user_feature_status(user_id, group_id, session)
    enabled_features = {key for key, _, enabled, _ in statuses if enabled}
    context = CommandContext(
        user_id=user_id,
        group_id=group_id,
        is_superuser=is_su,
        is_group_admin=_is_group_admin_or_su(event, is_su=is_su),
    )
    return _collect_visible_sections(
        context=context,
        enabled_features=enabled_features,
    )


@help_cmd.handle()
async def handle_help_entry(
    event: MessageEvent,
    matcher: Matcher,
    session: async_scoped_session,
    arg: Message = CommandArg(),
) -> None:
    """直接参数一步进入；无参数时发送第一层分类菜单。"""

    topic = arg.extract_plain_text().strip()
    if topic:
        matcher.set_arg("help_topic", Message(topic))
        return

    sections = await _current_help_sections(event, session)
    if not sections:
        await help_cmd.finish("当前没有可用的帮助分类。")
    cards = tuple(
        HelpMenuCard(
            index=index,
            title=view.section.display_name,
            summary=view.section.summary,
            entrypoint=view.section.entrypoint,
        )
        for index, view in enumerate(sections, start=1)
    )
    help_image = await render_help_menu(cards)
    if help_image is None:
        await help_cmd.send(_build_section_menu(sections))
    else:
        await help_cmd.send(MessageSegment.image(help_image))


@help_cmd.got("help_topic")
async def handle_help_topic(
    event: MessageEvent,
    session: async_scoped_session,
    topic: Message = Arg("help_topic"),
) -> None:
    """接收首层菜单的序号/名称，并结束本次帮助会话。"""

    topic_text = topic.extract_plain_text().strip()
    if is_cancel(topic_text):
        await help_cmd.finish("已退出帮助。")

    sections = await _current_help_sections(event, session)
    view = _resolve_section(topic_text, sections)
    if view is None:
        await help_cmd.reject_arg(
            "help_topic",
            invalid_choice(valid=f"1-{len(sections)} 或分类名称"),
        )
    await help_cmd.finish(_build_section_text(view))


__all__ = [
    "HELP_SECTIONS",
    "handle_help_entry",
    "handle_help_topic",
    "help_cmd",
]
