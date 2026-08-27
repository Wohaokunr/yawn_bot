"""命令权限与帮助可见性的共享上下文。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from nonebot import get_driver
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent

from .game_registry import GameKind, resolve_game_access
from .permission import get_user_feature_status, is_group_admin

if TYPE_CHECKING:
    from nonebot_plugin_orm import async_scoped_session

    from .command_catalog import CommandPermission, CommandScope


ChatScope = Literal["group", "private"]


@dataclass(frozen=True, slots=True)
class CommandAccessContext:
    """一次命令请求中可复用的作用域、功能、身份和玩法快照。"""

    user_id: int
    group_id: int | None
    enabled_features: frozenset[str] = frozenset()
    is_superuser: bool = False
    is_group_admin: bool = False
    is_room_host: bool = False
    is_player: bool = False
    current_game: GameKind | None = None

    @property
    def chat_scope(self) -> ChatScope:
        return "group" if self.group_id is not None else "private"

    @property
    def can_manage_room(self) -> bool:
        return self.is_room_host or self.is_group_admin or self.is_superuser

    def feature_enabled(self, feature: str | None) -> bool:
        return feature is None or feature in self.enabled_features

    def allows(
        self,
        *,
        scope: CommandScope,
        feature: str | None,
        permission: CommandPermission,
    ) -> bool:
        """只解析通用可见性；业务 handler 仍需执行最终鉴权。"""

        if scope == "group" and self.group_id is None:
            return False
        if scope == "private" and self.group_id is not None:
            return False
        if not self.feature_enabled(feature):
            return False
        permission_allowed = {
            "everyone": True,
            "superuser": self.is_superuser,
            "group_admin": self.is_group_admin or self.is_superuser,
            "room_host_or_admin": self.can_manage_room,
            "player": self.is_player,
        }
        return permission_allowed[permission]


async def resolve_command_access_context(
    event: MessageEvent,
    session: async_scoped_session,
) -> CommandAccessContext:
    """从真实事件、功能设置和玩法登记生成统一访问上下文。"""

    user_id = int(event.get_user_id())
    group_id = int(event.group_id) if isinstance(event, GroupMessageEvent) else None
    is_superuser = str(user_id) in get_driver().config.superusers
    feature_status = await get_user_feature_status(user_id, group_id, session)
    game_access = resolve_game_access(group_id, user_id)
    return CommandAccessContext(
        user_id=user_id,
        group_id=group_id,
        enabled_features=frozenset(
            key for key, _display, enabled, _source in feature_status if enabled
        ),
        is_superuser=is_superuser,
        is_group_admin=is_superuser
        or (isinstance(event, GroupMessageEvent) and is_group_admin(event)),
        is_room_host=bool(game_access and game_access.is_room_host),
        is_player=bool(game_access and game_access.is_player),
        current_game=game_access.kind if game_access else None,
    )


__all__ = ["ChatScope", "CommandAccessContext", "resolve_command_access_context"]
