"""私聊里开通 / 管理 WebUI 个人账号。

- ``/网页登录``：看账号状态、能管理哪些群、登录地址；``/网页登录 下线`` 退出所有设备，
  ``/网页登录 注销`` 删掉账号。
- ``/网页密码``（不带参数）：发一次性设置码，去登录页“用设置码设密码”，密码不经过聊天。
- ``/网页密码 新密码``：直接在私聊里设密码。

密码安全：
- 只在私聊里生效；在群里带着密码发会被拒绝（尽量撤回），并提醒换一个密码。
- 带密码的这条消息不进 NoneBot 事件日志（``get_log_string`` 抛 ``NoLogException``），
  命令优先级高于对话监听并阻断传播，不会进对话历史、Agent 上下文或记忆。
- 回复里从不复述密码。
"""

from __future__ import annotations

import asyncio
from typing import Any

from nonebot import get_driver, logger
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    PrivateMessageEvent,
)
from nonebot.exception import NoLogException
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata, on_command

from . import webui_accounts
from .command_catalog import CommandSpec, PluginCommandGroup, register_command_group

LOGIN_COMMAND = "网页登录"
LOGIN_ALIASES = ("webui", "WebUI", "WEBUI", "网页账号")
PASSWORD_COMMAND = "网页密码"
_GROUPS_SHOWN = 6
PASSWORD_ALIASES = ("webui密码", "WebUI密码", "WEBUI密码", "设置网页密码")

COMMAND_GROUP = register_command_group(
    PluginCommandGroup(
        plugin_id="yawn_core.webui_account",
        display_name="网页个人账号",
        entrypoint=LOGIN_COMMAND,
        commands=(
            CommandSpec(
                name=LOGIN_COMMAND,
                aliases=LOGIN_ALIASES,
                description="查看网页账号、能管理的群和登录地址（含下线、注销）",
                scope="private",
                display_level="advanced",
            ),
            CommandSpec(
                name=PASSWORD_COMMAND,
                aliases=PASSWORD_ALIASES,
                description="设置网页登录密码；不带参数会发一次性设置码，密码不经过聊天",
                scope="private",
                display_level="advanced",
            ),
        ),
    )
)

__plugin_meta__ = PluginMetadata(
    name="网页个人账号",
    description="QQ 用户在私聊里设密码，登录 WebUI 管理自己的群和私聊人设",
    usage="私聊发 /网页登录 查看，/网页密码 设置密码",
    extra={"command_group": COMMAND_GROUP},
)

# 优先级 -2：排在对局私聊监听（-1）和对话模式监听（1）之前，带密码的消息绝不会被
# 它们拿去当聊天内容。
login_cmd = on_command(
    LOGIN_COMMAND, aliases=set(LOGIN_ALIASES), priority=-2, block=True
)
password_cmd = on_command(
    PASSWORD_COMMAND, aliases=set(PASSWORD_ALIASES), priority=-2, block=True
)


# ── 日志脱敏 ─────────────────────────────────────────────────────────────


def _command_starts() -> tuple[str, ...]:
    try:
        starts = tuple(get_driver().config.command_start) or ("/",)
    except ValueError:
        starts = ("/",)
    return tuple(sorted(starts, key=len, reverse=True))


def is_password_command_text(text: str) -> bool:
    """这条文本是不是“网页密码”命令（带不带参数都算，宁可多藏）。"""

    stripped = text.strip()
    for start in _command_starts():
        if start and not stripped.startswith(start):
            continue
        body = stripped[len(start) :].lstrip()
        for name in (PASSWORD_COMMAND, *PASSWORD_ALIASES):
            if body.startswith(name):
                return True
    return False


def _install_log_redaction() -> None:
    """带密码命令的消息事件不进 NoneBot 的“收到事件”日志。"""

    for cls in (PrivateMessageEvent, GroupMessageEvent):
        if getattr(cls, "_yawn_password_redaction", False):
            continue
        original = cls.get_log_string

        def get_log_string(self: Any, _original: Any = original) -> str:
            try:
                text = self.get_plaintext()
            except Exception:  # noqa: BLE001
                text = ""
            if is_password_command_text(text):
                raise NoLogException("OneBot V11")  # noqa: TRY003
            return _original(self)

        cls.get_log_string = get_log_string  # type: ignore[method-assign]
        cls._yawn_password_redaction = True  # type: ignore[attr-defined]


_install_log_redaction()


# ── 回复文案 ─────────────────────────────────────────────────────────────


def _webui_config() -> Any:
    from .webui.config import config

    return config


def _login_url() -> str:
    base = str(_webui_config().webui_public_url or "").strip().rstrip("/")
    return base or ""


def login_hint() -> str:
    url = _login_url()
    where = f"打开 {url}/?login=qq" if url else "打开 WebUI 网页"
    return f"{where}，登录页选「QQ 账号」"


def feature_blocked_reason() -> str | None:
    config = _webui_config()
    if not config.webui_enabled:
        return "网页管理端还没开，暂时用不了网页登录。"
    if not config.webui_user_login_enabled:
        return "管理员暂时关闭了网页个人账号登录。"
    return None


async def managed_group_names(user_id: int) -> list[str]:
    from nonebot_plugin_orm import get_session

    from .webui.user_scope import managed_group_rows

    try:
        async with get_session() as db:
            rows = await managed_group_rows(db, user_id)
    except Exception:  # noqa: BLE001
        logger.opt(exception=True).debug("网页账号：读取管理的群失败")
        return []
    return [group.group_name or str(group.group_id) for _membership, group in rows]


def account_status_text(user_id: int, groups: list[str]) -> str:
    account = webui_accounts.get_account(user_id)
    lines = ["【网页个人账号】"]
    if account is None or not account.has_password:
        lines.append("还没设置密码。")
        lines.append(
            "· 发 /网页密码 拿一次性设置码，去网页上设密码（推荐，密码不经过聊天）"
        )
        lines.append("· 或者直接发 /网页密码 你的新密码")
    elif account.disabled:
        lines.append("你的网页账号被管理员停用了。")
    else:
        lines.append(f"已开通，密码设置于 {account.password_changed_at or '未知'}。")
        if account.last_login_at:
            lines.append(f"上次登录：{account.last_login_at}")
        lines.append(f"登录：{login_hint()}，用 QQ 号 {user_id} + 密码。")
    if groups:
        shown = "、".join(groups[:_GROUPS_SHOWN]) + (
            f" 等 {len(groups)} 个群" if len(groups) > _GROUPS_SHOWN else ""
        )
        lines.append(
            f"你是这些群的群主/管理员，网页上可以看群关系、开关群功能：{shown}"
        )
    else:
        lines.append("网页上可以调整我和你私聊时的人设；当了群管理员还能管理群。")
    lines.append("其他：/网页登录 下线（退出所有设备）、/网页登录 注销（删除账号）")
    return "\n".join(lines)


def setup_code_text(code: str) -> str:
    minutes = webui_accounts.SETUP_CODE_TTL_SECONDS // 60
    return (
        f"你的一次性设置码：{code}\n"
        f"{minutes} 分钟内有效，只能用一次。{login_hint()}，点「用设置码设密码」，"
        "输入 QQ 号、设置码和新密码即可。\n"
        "设置码别发给任何人，我们也不会找你要。"
    )


# ── 命令 ─────────────────────────────────────────────────────────────────


@login_cmd.handle()
async def _handle_login(event: MessageEvent, args: Message = CommandArg()) -> None:
    if not isinstance(event, PrivateMessageEvent):
        await login_cmd.finish("网页账号的事请私聊我发 /网页登录")
    blocked = feature_blocked_reason()
    if blocked:
        await login_cmd.finish(blocked)
    user_id = int(event.user_id)
    action = args.extract_plain_text().strip()
    if action in {"下线", "退出", "退出所有设备", "logout"}:
        done = webui_accounts.revoke_sessions(user_id)
        await login_cmd.finish(
            "已让你的网页账号在所有设备上退出登录。" if done else "你还没有网页账号。"
        )
    if action in {"注销", "删除", "删除账号"}:
        done = webui_accounts.delete_account(user_id)
        await login_cmd.finish(
            "网页账号已注销，所有设备都已退出；之后想用再发 /网页密码 重新设置。"
            if done
            else "你还没有网页账号。"
        )
    await login_cmd.finish(
        account_status_text(user_id, await managed_group_names(user_id))
    )


async def _try_recall(bot: Bot, event: MessageEvent) -> bool:
    try:
        await bot.delete_msg(message_id=event.message_id)
    except Exception:  # noqa: BLE001
        return False
    return True


@password_cmd.handle()
async def _handle_password(
    bot: Bot, event: MessageEvent, args: Message = CommandArg()
) -> None:
    supplied = args.extract_plain_text().strip()
    if not isinstance(event, PrivateMessageEvent):
        if supplied:
            recalled = await _try_recall(bot, event)
            await password_cmd.finish(
                ("已撤回那条消息。" if recalled else "")
                + "密码不能在群里发！这个密码已经被别人看到了，别再用它。"
                "请私聊我发 /网页密码 重新设置。"
            )
        await password_cmd.finish("请私聊我发 /网页密码 设置网页登录密码")
    blocked = feature_blocked_reason()
    if blocked:
        await password_cmd.finish(blocked)
    user_id = int(event.user_id)
    if not supplied:
        try:
            code = webui_accounts.issue_setup_code(user_id)
        except webui_accounts.AccountError as exc:
            await password_cmd.finish(str(exc))
        await password_cmd.finish(setup_code_text(code))
    try:
        await asyncio.to_thread(
            webui_accounts.set_password, user_id, supplied, source="private_command"
        )
    except webui_accounts.AccountError as exc:
        # 原因里不含密码本身
        await password_cmd.finish(f"没设置成功：{exc}")
    await password_cmd.finish(
        "网页登录密码已设置 ✅ 之前登录的设备都已退出。\n"
        f"{login_hint()}，用 QQ 号 {user_id} + 新密码登录。\n"
        "为了安全，建议现在长按撤回刚才那条带密码的消息。"
    )


__all__ = [
    "account_status_text",
    "is_password_command_text",
    "setup_code_text",
]
