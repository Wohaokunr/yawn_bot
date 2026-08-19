"""局后回放命令入口。

回放命令只读取事件日志，不触碰正在运行的游戏状态。公开回放可在群内查看；
个人回放要求在私聊中明确座位号，投影层仍只加入该座位自己的结构化行动。
"""

from __future__ import annotations

import re

from nonebot import on_command
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageEvent,
)
from nonebot.params import CommandArg

from .event_log import GameKind, flush_events
from .replay import (
    ReplayView,
    load_replay,
    render_replay,
    replay_viewer_seat,
)

_GAME_KIND_ALIASES: dict[str, GameKind] = {
    "rpg": "rpg",
    "跑团": "rpg",
    "werewolf": "werewolf",
    "狼人杀": "werewolf",
}
_PUBLIC_ALIASES = frozenset({"public", "公开", "公示"})
_PERSONAL_ALIASES = frozenset({"personal", "private", "个人", "私密"})


def _parse_replay_args(
    raw: str,
) -> tuple[str, GameKind | None, ReplayView, int | None] | str:
    tokens = [token for token in re.split(r"\s+", raw.strip()) if token]
    if not tokens:
        return "格式：/回放 GAME_ID [公开|个人 [座位号]]"
    game_id = tokens.pop(0)
    game_kind: GameKind | None = None
    if tokens and tokens[0].lower() in _GAME_KIND_ALIASES:
        game_kind = _GAME_KIND_ALIASES[tokens.pop(0).lower()]
    view: ReplayView = "public"
    viewer_seat: int | None = None
    if tokens:
        mode = tokens.pop(0).lower()
        if mode in _PUBLIC_ALIASES:
            view = "public"
        elif mode in _PERSONAL_ALIASES:
            view = "personal"
            if not tokens:
                viewer_seat = None
            elif len(tokens) == 1 and re.fullmatch(r"\d+号?", tokens[0]):
                viewer_seat = int(tokens.pop(0).rstrip("号"))
            else:
                return "个人回放格式：/回放 GAME_ID 个人 [座位号]"
        else:
            return "视角只能填写 公开 或 个人"
    if tokens:
        return "格式：/回放 GAME_ID [公开|个人 [座位号]]"
    return game_id, game_kind, view, viewer_seat


replay_cmd = on_command(
    "回放",
    aliases={"局后回放", "对局回放", "replay"},
    priority=5,
    block=True,
)


@replay_cmd.handle()
async def handle_replay(
    event: MessageEvent,
    arg: "Message" = CommandArg(),
) -> None:
    """加载事件并渲染公开/个人视角回放。"""

    parsed = _parse_replay_args(str(arg))
    if isinstance(parsed, str):
        await replay_cmd.finish(parsed)
    game_id, game_kind, view, viewer_seat = parsed
    if view == "personal" and isinstance(event, GroupMessageEvent):
        await replay_cmd.finish("个人回放请私聊发送：/回放 GAME_ID 个人 [座位号]")
    if view == "personal":
        authorized_seat = replay_viewer_seat(
            game_id,
            int(event.get_user_id()),
            game_kind=game_kind,
        )
        if authorized_seat is None:
            await replay_cmd.finish("当前账号没有该局个人回放权限")
        if viewer_seat is not None and viewer_seat != authorized_seat:
            await replay_cmd.finish("个人回放座位与当前账号不匹配")
        viewer_seat = authorized_seat
    await flush_events()
    projection = load_replay(
        game_id,
        game_kind=game_kind,
        view=view,
        viewer_seat=viewer_seat,
    )
    await replay_cmd.finish(render_replay(projection))


__all__ = ["handle_replay", "replay_cmd"]
