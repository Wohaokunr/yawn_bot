"""类型化指令目录与已加载插件注册表。"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Literal

CommandScope = Literal["all", "group", "private"]
CommandDisplayLevel = Literal[
    "entry",
    "lobby",
    "active",
    "contextual",
    "advanced",
]
HelpSectionKey = Literal[
    "basic",
    "agent",
    "rpg",
    "werewolf",
    "fanqie",
    "admin",
]
CommandPermission = Literal[
    "everyone",
    "group_admin",
    "superuser",
    "room_host_or_admin",
    "player",
]


@dataclass(frozen=True, slots=True)
class CommandContext:
    """帮助请求的只读上下文；游戏插件据此查询自己的当前状态。"""

    user_id: int
    group_id: int | None
    is_superuser: bool = False
    is_group_admin: bool = False


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """一条命令的稳定展示元数据。"""

    name: str
    description: str
    aliases: tuple[str, ...] = ()
    scope: CommandScope = "all"
    feature: str | None = None
    permission: CommandPermission = "everyone"
    display_level: CommandDisplayLevel = "entry"
    help_section: HelpSectionKey | None = None


CommandAvailability = Callable[[CommandContext], Collection[str]]
CommandHelpHint = Callable[[CommandContext], str | None]


@dataclass(frozen=True, slots=True)
class PluginCommandGroup:
    """一个已加载插件提供的类型化命令组。"""

    plugin_id: str
    display_name: str
    entrypoint: str
    commands: tuple[CommandSpec, ...]
    get_available_commands: CommandAvailability | None = None
    get_help_hint: CommandHelpHint | None = None
    help_section: HelpSectionKey = "basic"

    def __post_init__(self) -> None:
        names = [command.name for command in self.commands]
        if len(names) != len(set(names)):
            raise ValueError("命令组存在重复命令名")
        if self.entrypoint not in names:
            raise ValueError("命令组主入口未登记为命令")

    def available_commands(self, context: CommandContext) -> tuple[CommandSpec, ...]:
        """按插件自己的状态判断保留当前真正可用的命令。"""

        if self.get_available_commands is None:
            return self.commands
        available = frozenset(self.get_available_commands(context))
        return tuple(command for command in self.commands if command.name in available)

    def help_hint(self, context: CommandContext) -> str | None:
        """返回插件根据当前状态提供的一句操作提示。"""

        if self.get_help_hint is None:
            return None
        return self.get_help_hint(context)


_COMMAND_GROUPS: dict[str, PluginCommandGroup] = {}


def register_command_group(group: PluginCommandGroup) -> PluginCommandGroup:
    """登记一个已成功导入插件的命令组；重复导入时原位替换。"""

    _COMMAND_GROUPS[group.plugin_id] = group
    return group


def unregister_command_group(plugin_id: str) -> None:
    """移除加载失败插件可能留下的不完整登记。"""

    _COMMAND_GROUPS.pop(plugin_id, None)


def get_registered_command_groups() -> tuple[PluginCommandGroup, ...]:
    """返回当前进程已成功注册的命令组快照。"""

    return tuple(_COMMAND_GROUPS.values())
