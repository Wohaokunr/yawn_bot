"""渐进式帮助：先选功能分类，再展示当前场景真正可用的命令。"""

from collections.abc import Iterable
from dataclasses import dataclass

from nonebot import logger
from nonebot.adapters.onebot.v11 import (
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.matcher import Matcher
from nonebot.params import Arg, CommandArg
from nonebot.plugin import PluginMetadata, on_command
from nonebot_plugin_orm import async_scoped_session

from .command_access import resolve_command_access_context
from .command_catalog import (
    CommandAccessContext,
    CommandSpec,
    HelpSectionKey,
    PluginCommandGroup,
    get_registered_command_groups,
    register_command_group,
)
from .command_definition import CommandDefinition
from .command_ux import invalid_choice
from .session_interaction import (
    SessionIntent,
    pass_through_new_command,
    resolve_session_intent,
)
from .ui.panel_renderer import (
    CommandItemView,
    CommandSectionView,
    HelpMenuCard,
    render_command_sections,
    render_help_menu,
)

logger.info("帮助面板模块已加载")

help_cmd = on_command(
    "help",
    aliases={"帮助", "命令"},
    priority=5,
    block=True,
)
operation_cmd = on_command(
    "操作",
    aliases={"当前操作", "下一步"},
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
            CommandSpec(
                name="操作",
                aliases=("当前操作", "下一步"),
                description="查看当前场景真正能执行的动作",
                display_level="entry",
            ),
        ),
    )
)

_ADMIN_PERMISSIONS = frozenset({"group_admin", "superuser", "room_host_or_admin"})
_COMMAND_SECTION_ORDER = (
    ("recommended", "推荐操作"),
    ("common", "常用"),
    ("admin", "管理操作"),
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


def _command_is_visible(
    command: CommandSpec,
    *,
    context: CommandAccessContext,
) -> bool:
    """处理跨插件通用的权限、作用域和功能开关。"""

    return context.allows(
        scope=command.scope,
        feature=command.feature,
        permission=command.permission,
    )


def _collect_visible_sections(
    *,
    context: CommandAccessContext,
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


def _command_name(command: CommandSpec) -> str:
    if isinstance(command, CommandDefinition):
        return command.qualified_name
    return command.name


def _command_aliases(command: CommandSpec) -> tuple[str, ...]:
    if isinstance(command, CommandDefinition):
        return command.help_aliases
    return command.aliases


def _command_section_key(command: CommandSpec) -> str:
    if command.permission in _ADMIN_PERMISSIONS:
        return "admin"
    if command.display_level in {"entry", "lobby", "contextual"}:
        return "recommended"
    return "common"


def _build_command_sections(
    commands: Iterable[CommandSpec],
) -> tuple[CommandSectionView, ...]:
    """只按命令元数据分组；可用性仍完全由命令组负责。"""

    grouped: dict[str, list[CommandItemView]] = {
        key: [] for key, _title in _COMMAND_SECTION_ORDER
    }
    for command in commands:
        aliases = "、".join(f"/{alias}" for alias in _command_aliases(command))
        grouped[_command_section_key(command)].append(
            CommandItemView(
                name=_command_name(command),
                description=command.description,
                aliases=aliases,
            )
        )
    return tuple(
        CommandSectionView(title, tuple(grouped[key]))
        for key, title in _COMMAND_SECTION_ORDER
        if grouped[key]
    )


def _view_commands(view: HelpSectionView) -> tuple[CommandSpec, ...]:
    return tuple(
        command for _group, commands, _hint in view.groups for command in commands
    )


def _view_hints(view: HelpSectionView) -> tuple[str, ...]:
    return tuple(dict.fromkeys(hint for _group, _commands, hint in view.groups if hint))


def _build_grouped_command_text(
    *,
    title: str,
    commands: Iterable[CommandSpec],
    hints: Iterable[str] = (),
    footer: str,
) -> str:
    lines = [f"═══ {title} ═══"]
    for section in _build_command_sections(commands):
        lines.extend(("", f"【{section.title}】"))
        for command in section.commands:
            aliases = f"（别名：{command.aliases}）" if command.aliases else ""
            description = f" — {command.description}" if command.description else ""
            lines.append(f"/{command.name}{aliases}{description}")
    lines.extend(f"提示：{hint}" for hint in hints)
    lines.extend(("", footer))
    return "\n".join(lines)


def _build_section_text(view: HelpSectionView) -> str:
    """构建单个分类的第二层纯文本降级帮助。"""

    return _build_grouped_command_text(
        title=view.section.display_name,
        commands=_view_commands(view),
        hints=_view_hints(view),
        footer="发送 /操作 查看当前动作；发送 /help 返回分类菜单。",
    )


def _select_operation_commands(
    sections: tuple[HelpSectionView, ...],
) -> tuple[CommandSpec, ...]:
    """从已经过 ``available_commands`` 过滤的结果中挑选最短动作集。

    这里仅依据稳定展示元数据决定层级，不读取任何游戏状态，避免形成新的
    阶段判断路径。
    """

    commands = tuple(
        command
        for view in sections
        for command in _view_commands(view)
        if command.name != "操作"
    )
    contextual = tuple(
        command
        for command in commands
        if command.display_level == "contextual"
        and command.permission not in _ADMIN_PERMISSIONS
    )
    if contextual:
        group_support = tuple(
            command
            for command in commands
            if command.operation_support and command.scope != "private"
        )
        return (*contextual, *group_support)

    lobby = tuple(command for command in commands if command.display_level == "lobby")
    if lobby:
        return lobby

    support = tuple(command for command in commands if command.operation_support)
    if support:
        return support

    return tuple(command for command in commands if command.display_level == "entry")


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
    context = await resolve_command_access_context(event, session)
    return _collect_visible_sections(context=context)


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
    matcher: Matcher,
    session: async_scoped_session,
    topic: Message = Arg("help_topic"),
) -> None:
    """接收首层菜单的序号/名称，并结束本次帮助会话。"""

    topic_text = topic.extract_plain_text().strip()
    intent = resolve_session_intent(topic_text)
    if intent is SessionIntent.NEW_COMMAND:
        await pass_through_new_command(matcher, topic_text)
    if intent in {SessionIntent.EXIT, SessionIntent.BACK}:
        await help_cmd.finish("已退出帮助。")

    sections = await _current_help_sections(event, session)
    if intent is SessionIntent.MENU:
        await help_cmd.reject_arg("help_topic", _build_section_menu(sections))
    view = _resolve_section(topic_text, sections)
    if view is None:
        await help_cmd.reject_arg(
            "help_topic",
            invalid_choice(valid=f"1-{len(sections)} 或分类名称"),
        )
    command_sections = _build_command_sections(_view_commands(view))
    help_image = await render_command_sections(
        title=view.section.display_name,
        subtitle="只展示你在当前场景真正能使用的命令。",
        sections=command_sections,
        note="；".join(_view_hints(view)) or None,
    )
    if help_image is None:
        await help_cmd.finish(_build_section_text(view))
    await help_cmd.finish(
        MessageSegment.image(help_image) + "\n发送 /操作 查看当前动作"
    )


@operation_cmd.handle()
async def handle_operation(
    event: MessageEvent,
    session: async_scoped_session,
) -> None:
    """显示当前上下文最值得执行的动作，不另做游戏状态判断。"""

    sections = await _current_help_sections(event, session)
    commands = _select_operation_commands(sections)
    if not commands:
        await operation_cmd.finish("当前没有可执行的操作。发送 /help 查看帮助。")

    operation_image = await render_command_sections(
        title="当前可做什么",
        subtitle="已按当前会话、身份、阶段和权限筛选。",
        sections=_build_command_sections(commands),
    )
    if operation_image is None:
        await operation_cmd.finish(
            _build_grouped_command_text(
                title="当前可做什么",
                commands=commands,
                footer="发送 /help 查看完整帮助。",
            )
        )
    await operation_cmd.finish(
        MessageSegment.image(operation_image) + "\n发送 /help 查看完整帮助"
    )


__all__ = [
    "HELP_SECTIONS",
    "handle_help_entry",
    "handle_help_topic",
    "handle_operation",
    "help_cmd",
    "operation_cmd",
]
