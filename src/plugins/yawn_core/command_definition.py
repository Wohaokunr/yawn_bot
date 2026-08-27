"""命令单一真相源：从声明同时生成 NoneBot matcher 与帮助元数据。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    MessageEvent,
    PrivateMessageEvent,
)
from nonebot.params import Command
from nonebot.plugin import on_command
from nonebot.rule import Rule

from . import game_registry
from .command_catalog import CommandSpec

_DUPLICATE_COMMAND_ERROR = "CommandDefinition 存在重复命令名"

if TYPE_CHECKING:
    from nonebot.matcher import Matcher

    from .game_registry import GameKind


@dataclass(frozen=True, slots=True)
class CommandDefinition(CommandSpec):
    """命令完整声明。

    ``name`` 与 ``aliases`` 是兼容短命令；``namespace_path`` 是稳定、无歧义的
    完整命令路径，例如 ``("跑团", "报名")``。大型子插件迁移后，matcher 与
    help catalog 都必须直接使用本对象，避免两处重复声明。
    """

    namespace_path: tuple[str, ...] | None = None
    context_game: GameKind | None = None
    no_active_game_aliases: tuple[str, ...] = ()
    priority: int = 5
    block: bool = True

    @property
    def qualified_name(self) -> str:
        if self.namespace_path:
            return " ".join(self.namespace_path)
        return self.name

    @property
    def help_aliases(self) -> tuple[str, ...]:
        """帮助中展示兼容入口；完整命令为主入口时把短命令也列为别名。"""
        if self.namespace_path and self.namespace_path != (self.name,):
            return (self.name, *self.aliases)
        return self.aliases

    @property
    def namespace_commands(self) -> frozenset[str]:
        """返回实际注册给 NoneBot 的空格形式完整命令文本。

        NoneBot 的 tuple command 会使用全局 ``command_sep``（项目默认是 ``.``），
        因而 ``("跑团", "报名")`` 实际对应 ``/跑团.报名``。P7 需要的是
        ``/跑团 报名``，所以命名空间入口必须注册成包含空格的单个命令字符串。
        """

        if not self.namespace_path:
            return frozenset()
        prefix = self.namespace_path[:-1]
        commands = {" ".join(self.namespace_path)}
        if prefix:
            commands.update(" ".join((*prefix, alias)) for alias in self.aliases)
        return frozenset(commands)

    def matcher_aliases(self) -> set[str | tuple[str, ...]]:
        aliases: set[str | tuple[str, ...]] = set(self.aliases)
        aliases.update(self.namespace_commands)
        aliases.discard(self.name)
        return aliases


def _scope_rule(definition: CommandDefinition) -> Rule:
    def _matches(event: MessageEvent) -> bool:
        if definition.scope == "group":
            return isinstance(event, GroupMessageEvent)
        if definition.scope == "private":
            return isinstance(event, PrivateMessageEvent)
        return True

    return Rule(_matches)


def command_context_matches(
    definition: CommandDefinition,
    *,
    group_id: int | None,
    cmd: tuple[str, ...] | None,
) -> bool:
    """纯函数版上下文策略，供 matcher 与回归测试共用。"""

    if definition.context_game is None:
        return True
    if cmd and len(cmd) == 1 and cmd[0] in definition.namespace_commands:
        return True
    if group_id is None:
        return False
    active_kind = game_registry.active_game_kind(group_id)
    if active_kind == definition.context_game:
        return True
    return (
        active_kind is None
        and cmd is not None
        and len(cmd) == 1
        and cmd[0] in definition.no_active_game_aliases
    )


def _context_rule(definition: CommandDefinition) -> Rule:
    """短命令按活跃玩法分流；显式命名空间始终按指定插件路由。"""

    def _matches(
        event: MessageEvent,
        cmd: tuple[str, ...] | None = Command(),
    ) -> bool:
        group_id = int(event.group_id) if isinstance(event, GroupMessageEvent) else None
        return command_context_matches(definition, group_id=group_id, cmd=cmd)

    return Rule(_matches)


def build_matcher(definition: CommandDefinition) -> type[Matcher]:
    """由声明生成 NoneBot matcher；handler 仍由业务模块原样注册。"""

    rule = _scope_rule(definition) & _context_rule(definition)
    return on_command(
        definition.name,
        aliases=definition.matcher_aliases(),
        rule=rule,
        priority=definition.priority,
        block=definition.block,
        state={"command_definition": definition},
    )


def command_map(
    definitions: tuple[CommandDefinition, ...],
) -> dict[str, CommandDefinition]:
    """按兼容短命令名构建只读式索引，并在导入期拒绝重复。"""

    result = {definition.name: definition for definition in definitions}
    if len(result) != len(definitions):
        raise ValueError(_DUPLICATE_COMMAND_ERROR)
    return result
