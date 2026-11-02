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

新人引导（私聊 Agent 的 ``webui_onboarding`` 工具）：
- Agent 调 ``ask_password`` 时只是“开一次收密码的窗口”（``arm_password_capture``），
  提示由 Bot 直接私发；他接下来发的那一条私聊由本模块 ``capture_listener``
  （优先级 -3，先于一切私聊监听）直接收走：校验、scrypt 哈希、回结果和登录链接。
- 这条消息不进 NoneBot 事件日志、不进对话历史、不给模型看；Agent 只从工具结果和
  记进历史的“已设好”通知里知道结果，永远拿不到密码本身。
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
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
from nonebot.plugin import PluginMetadata, on_command, on_message
from nonebot.rule import Rule

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


def _capture_hides(event: Any) -> bool:
    """他正处在“把密码发给我”的窗口里：这条私聊整条不进日志。"""

    if not isinstance(event, PrivateMessageEvent):
        return False
    try:
        return capture_pending(int(event.user_id))
    except (TypeError, ValueError):
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
            if is_password_command_text(text) or _capture_hides(self):
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


def login_url() -> str:
    """QQ 账号登录页的完整地址；没配 WEBUI_PUBLIC_URL 时返回空串。"""

    url = _login_url()
    return f"{url}/?login=qq" if url else ""


def login_hint() -> str:
    url = login_url()
    where = f"打开 {url}" if url else "打开 WebUI 网页"
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


# ── 新人引导：Agent 开窗口，下一条私聊直接当密码收走 ─────────────────────

CAPTURE_TTL_SECONDS = 300
CAPTURE_MAX_FAILURES = 3
CANCEL_WORDS = frozenset({"取消", "算了", "不设了", "cancel", "退出"})


@dataclass(slots=True)
class _Capture:
    expires_at: float
    failures: int = 0


_CAPTURES: dict[int, _Capture] = {}
_CAPTURE_LOCK = threading.Lock()


def capture_pending(user_id: int, *, now: float | None = None) -> bool:
    current = time.time() if now is None else now
    with _CAPTURE_LOCK:
        item = _CAPTURES.get(int(user_id))
        if item is None:
            return False
        if item.expires_at <= current:
            _CAPTURES.pop(int(user_id), None)
            return False
        return True


def capture_seconds_left(user_id: int, *, now: float | None = None) -> int:
    current = time.time() if now is None else now
    with _CAPTURE_LOCK:
        item = _CAPTURES.get(int(user_id))
    return max(int(item.expires_at - current), 0) if item else 0


def arm_password_capture(user_id: int, *, now: float | None = None) -> int:
    """开（或续）一次收密码的窗口，返回有效秒数。"""

    current = time.time() if now is None else now
    with _CAPTURE_LOCK:
        _CAPTURES[int(user_id)] = _Capture(expires_at=current + CAPTURE_TTL_SECONDS)
    return CAPTURE_TTL_SECONDS


def cancel_password_capture(user_id: int) -> bool:
    with _CAPTURE_LOCK:
        return _CAPTURES.pop(int(user_id), None) is not None


def reset_captures_for_tests() -> None:
    with _CAPTURE_LOCK:
        _CAPTURES.clear()


def password_rules_text() -> str:
    low, high = webui_accounts.PASSWORD_MIN_LENGTH, webui_accounts.PASSWORD_MAX_LENGTH
    return (
        f"{low} 到 {high} 位，"
        "字母、数字、符号里至少混用两种，不能有空格，别带自己的 QQ 号"
    )


def capture_prompt_text() -> str:
    minutes = CAPTURE_TTL_SECONDS // 60
    return (
        "【设置网页登录密码】\n"
        "请直接把你想用的密码发给我，只发密码本身就行。\n"
        f"要求：{password_rules_text()}。\n"
        "这条消息由系统直接收走加密保存，不会给 AI 看、不进聊天记录。\n"
        f"{minutes} 分钟内有效；不想设了发「取消」。"
    )


def capture_success_text(user_id: int) -> str:
    url = login_url()
    lines = ["网页登录密码设好了 ✅ 之前登录过的设备都已退出。"]
    how = f"登录页选「QQ 账号」，账号填 QQ 号 {user_id}，再输入刚才的密码。"
    if url:
        lines.append(f"登录地址：{url}")
        lines.append(f"打开后{how}")
    else:
        lines.append(f"打开 WebUI 网页，{how}")
    lines.append("为了安全，建议现在长按撤回刚才那条密码消息。")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class CaptureOutcome:
    """收到一条窗口内消息后的结果；``reply`` 里绝不含密码。"""

    reply: str
    done: bool
    success: bool = False


async def consume_capture_message(  # noqa: C901
    user_id: int, text: str, *, has_media: bool = False
) -> CaptureOutcome:
    """处理窗口里收到的一条私聊：取消 / 不合格（留窗口）/ 设好。"""

    stripped = text.strip()
    if stripped.lower() in CANCEL_WORDS:
        cancel_password_capture(user_id)
        return CaptureOutcome("好，先不设了。想设的时候再跟我说就行。", done=True)
    blocked = feature_blocked_reason()
    if blocked:
        cancel_password_capture(user_id)
        return CaptureOutcome(blocked, done=True)
    problem: str | None
    if not stripped or has_media:
        problem = "只发密码文字就好，别带图片或表情"
    else:
        problem = webui_accounts.password_problem(stripped, user_id)
    if problem is None:
        try:
            await asyncio.to_thread(
                webui_accounts.set_password,
                user_id,
                stripped,
                source="agent_onboarding",
            )
        except webui_accounts.AccountError as exc:
            problem = str(exc)
            if exc.code == "disabled":
                cancel_password_capture(user_id)
                return CaptureOutcome(f"没设置成功：{exc}", done=True)
        else:
            cancel_password_capture(user_id)
            return CaptureOutcome(
                capture_success_text(user_id), done=True, success=True
            )
    with _CAPTURE_LOCK:
        item = _CAPTURES.get(int(user_id))
        failures = (item.failures + 1) if item else CAPTURE_MAX_FAILURES
        if item is not None:
            item.failures = failures
        if failures >= CAPTURE_MAX_FAILURES:
            _CAPTURES.pop(int(user_id), None)
    if failures >= CAPTURE_MAX_FAILURES:
        return CaptureOutcome(
            f"没设置成功：{problem}。试了几次都不行，先停一下；"
            "想继续的话跟我说一声，或者自己发 /网页密码 拿设置码去网页上设。",
            done=True,
        )
    return CaptureOutcome(
        f"没设置成功：{problem}。换一个直接发给我（发「取消」放弃）。", done=False
    )


async def _is_capture_msg(event: MessageEvent) -> bool:
    if not isinstance(event, PrivateMessageEvent):
        return False
    if not capture_pending(int(event.user_id)):
        return False
    text = event.get_plaintext().strip()
    # 命令照常走命令（如 /网页密码 自己设），并顺手关掉窗口。
    for start in _command_starts():
        if start and text.startswith(start):
            cancel_password_capture(int(event.user_id))
            return False
    return True


# 优先级 -3：先于网页账号命令（-2）、对局私聊监听（-1）、对话模式（1）和自动接管（9），
# 这条消息绝不会进对话队列、历史或 Agent 上下文。
capture_listener = on_message(rule=Rule(_is_capture_msg), priority=-3, block=True)


async def _record_capture_result(user_id: int, text: str) -> None:
    """把“设好了 + 登录地址”记进私聊历史，Agent 下一轮知道结果（不含密码）。"""

    try:
        from datetime import datetime, timedelta, timezone

        from .yawn_agent.private_scheduler import record_outgoing

        now = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
        await record_outgoing(user_id, text, now)
    except Exception:  # noqa: BLE001
        logger.opt(exception=True).debug("网页账号：记录引导结果失败(忽略)")


@capture_listener.handle()
async def _handle_capture(event: MessageEvent) -> None:
    user_id = int(event.user_id)
    has_media = any(seg.type != "text" for seg in event.get_message())
    outcome = await consume_capture_message(
        user_id, event.get_plaintext(), has_media=has_media
    )
    # 日志只记结果，不记内容
    logger.info(
        f"网页账号引导：用户 {user_id} 设密码"
        + (
            "成功"
            if outcome.success
            else ("结束" if outcome.done else "未通过，继续等")
        )
    )
    if outcome.success:
        await _record_capture_result(user_id, outcome.reply)
    await capture_listener.finish(outcome.reply)


__all__ = [
    "CAPTURE_TTL_SECONDS",
    "CaptureOutcome",
    "account_status_text",
    "arm_password_capture",
    "cancel_password_capture",
    "capture_pending",
    "capture_prompt_text",
    "consume_capture_message",
    "is_password_command_text",
    "login_url",
    "setup_code_text",
]
