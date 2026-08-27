"""共享玩法短命令路由：按群当前活跃玩法分发同名命令。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nonebot import on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent
from nonebot.rule import Rule

from . import game_registry
from .command_ux import condition_unmet

if TYPE_CHECKING:
    from collections.abc import Collection

    from .game_registry import GameKind

NO_ACTIVE_GAME_MESSAGES: dict[str, str] = {
    "报名": condition_unmet(
        "当前没有正在报名的玩法", "可使用 /狼人杀 或 /跑团 创建房间。"
    ),
    "退报名": condition_unmet(
        "当前没有正在报名的玩法", "可使用 /狼人杀 或 /跑团 创建房间。"
    ),
    "查看报名": condition_unmet(
        "当前没有正在报名的玩法", "可使用 /狼人杀 或 /跑团 创建房间。"
    ),
    "开始游戏": condition_unmet(
        "当前没有可以开始的玩法", "可使用 /狼人杀 或 /跑团 创建房间。"
    ),
    "结束游戏": condition_unmet(
        "当前没有正在进行的玩法", "可使用 /狼人杀 或 /跑团 创建房间。"
    ),
}

_REGISTERED = False


def game_context_matches(event: MessageEvent, kind: GameKind) -> bool:
    """群消息仅在当前活跃玩法与 ``kind`` 一致时通过。"""

    if not isinstance(event, GroupMessageEvent):
        return False
    return game_registry.group_has_game(kind, int(event.group_id))


def game_context_rule(kind: GameKind) -> Rule:
    """构造按群当前玩法互斥匹配的 NoneBot Rule。"""

    def _matches(event: MessageEvent) -> bool:
        return game_context_matches(event, kind)

    return Rule(_matches)


def no_active_game_matches(event: MessageEvent) -> bool:
    """只匹配当前没有任何已登记玩法的群消息。"""

    if not isinstance(event, GroupMessageEvent):
        return False
    return game_registry.active_game_kind(int(event.group_id)) is None


def no_active_game_rule() -> Rule:
    """构造无活跃玩法时的兜底 Rule。"""

    return Rule(no_active_game_matches)


def no_active_game_message(command: str) -> str:
    """返回同名短命令在无玩法上下文时的中立提示。"""

    return NO_ACTIVE_GAME_MESSAGES.get(
        command,
        condition_unmet(
            "当前没有活跃玩法", "可使用 /狼人杀 或 /跑团 创建房间。"
        ),
    )


def _register_fallback(
    command: str,
    *,
    aliases: Collection[str] = (),
) -> None:
    matcher = on_command(
        command,
        aliases=set(aliases),
        rule=no_active_game_rule(),
        priority=5,
        block=True,
    )

    @matcher.handle()
    async def _handle_no_active_game() -> None:
        await matcher.finish(no_active_game_message(command))


def register_no_active_game_matchers() -> None:
    """注册同名短命令的无上下文兜底；重复调用保持幂等。"""

    global _REGISTERED  # noqa: PLW0603
    if _REGISTERED:
        return
    _REGISTERED = True

    _register_fallback("报名", aliases=("上车", "加一"))
    _register_fallback("退报名", aliases=("下车",))
    _register_fallback("查看报名", aliases=("报名情况",))
    _register_fallback("开始游戏", aliases=("发车",))
    _register_fallback("结束游戏")
