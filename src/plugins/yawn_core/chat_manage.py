"""聊天管理模块：查看历史对话、删除对话/消息。

普通用户：管理自己的对话记录
超级管理员：可查看、删除、修改任意用户的聊天记录
"""

from typing import Optional

from nonebot import logger
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageEvent,
)
from nonebot.dependencies import Dependent
from nonebot.matcher import Matcher
from nonebot.params import ArgPlainText, CommandArg
from nonebot.permission import SUPERUSER
from nonebot.plugin import on_command
from nonebot_plugin_orm import async_scoped_session
from sqlalchemy import select

from .data_models.chat_message import ChatMessage
from .data_models.chat_session import ChatSession
from .permission import require_feature

logger.info("聊天管理模块已加载")

# ── 常量 ────────────────────────────────────────────────

_PREVIEW_CHAR_LIMIT = 80
_MIN_DELETE_PARTS = 2
_MIN_ADMIN_DELETE_PARTS = 3

# ── 命令匹配器 ────────────────────────────────────────────

# 普通用户：对话管理面板
chat_manage_cmd = on_command(
    "聊天管理",
    aliases={"对话管理", "对话记录"},
    priority=5,
    block=True,
)

# 超级管理员：查看指定用户对话
admin_chat_view_cmd = on_command(
    "查看用户对话",
    permission=SUPERUSER,
    priority=2,
    block=True,
)

# 超级管理员：删除指定用户对话
admin_chat_delete_cmd = on_command(
    "删除用户对话",
    permission=SUPERUSER,
    priority=2,
    block=True,
)


# ── 辅助函数 ──────────────────────────────────────────────


def _fmt_session_list(
    sessions: list[ChatSession],
) -> str:
    """格式化会话列表。"""
    if not sessions:
        return "暂无对话记录。"

    lines: list[str] = []
    for i, s in enumerate(sessions, 1):
        title = s.title or "未命名对话"
        time_str = (
            f"{s.updated_at:%m-%d %H:%M}"
            if s.updated_at
            else f"{s.created_at:%m-%d %H:%M}"
        )
        lines.append(f"  {i}. [{s.id}] {title} ({time_str})")
    return "\n".join(lines)


def _fmt_message_list(
    messages: list[ChatMessage],
) -> str:
    """格式化消息列表。"""
    if not messages:
        return "该对话暂无消息。"

    lines: list[str] = []
    for msg in messages:
        role_label = (
            "我" if msg.role == "user" else "AI"
        )
        # 截取前 N 字符作为预览，替换换行避免破坏列表排版
        preview = (
            msg.content[:_PREVIEW_CHAR_LIMIT]
            .replace("\n", " ")
        )
        if len(msg.content) > _PREVIEW_CHAR_LIMIT:
            preview += "..."
        time_str = f"{msg.created_at:%m-%d %H:%M}"
        lines.append(
            f"  [{msg.id}] {role_label} ({time_str}): "
            f"{preview}"
        )
    return "\n".join(lines)


async def _get_user_sessions(
    session: async_scoped_session,
    user_id: int,
    group_id: Optional[int] = None,
) -> list[ChatSession]:
    """获取用户的活跃会话列表。"""
    stmt = (
        select(ChatSession)
        .where(
            ChatSession.user_id == user_id,
            ChatSession.group_id == group_id,
            ChatSession.is_deleted == False,  # noqa: E712
        )
        .order_by(ChatSession.updated_at.desc().nullslast())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _get_session_messages(
    session: async_scoped_session,
    session_id: int,
) -> list[ChatMessage]:
    """获取会话中的活跃消息。"""
    stmt = (
        select(ChatMessage)
        .where(
            ChatMessage.session_id == session_id,
            ChatMessage.is_deleted == False,  # noqa: E712
        )
        .order_by(ChatMessage.id.asc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ── 普通用户：交互式管理面板 ──────────────────────────────


@chat_manage_cmd.handle()
async def handle_chat_manage(
    event: MessageEvent,
    matcher: Matcher,
    session: async_scoped_session,
    args: Message = CommandArg(),
    _perm: Dependent = require_feature("ai_chat"),
) -> None:
    """展示对话管理主菜单。"""
    if isinstance(event, GroupMessageEvent):
        await chat_manage_cmd.finish(
            "请在私聊中使用聊天管理功能~"
        )

    user_id = int(event.get_user_id())
    sessions = await _get_user_sessions(session, user_id)

    if not sessions:
        await chat_manage_cmd.finish(
            "你还没有任何对话记录。\n"
            "发送 /对话 <内容> 开始聊天吧！"
        )

    lines = ["═══ 对话管理 ═══"]
    lines.append(_fmt_session_list(sessions))
    lines.append("")
    lines.append("操作说明：")
    lines.append("  发送序号 → 查看对话详情")
    lines.append("  发送「删除 <序号>」→ 删除该对话")
    lines.append("  发送「取消」→ 退出")

    matcher.state["sessions"] = sessions
    matcher.state["user_id"] = user_id

    await chat_manage_cmd.send("\n".join(lines))

    # 若命令自带参数，直接处理
    arg_text = args.extract_plain_text().strip()
    if arg_text:
        matcher.set_arg("manage_choice", args)


@chat_manage_cmd.got(
    "manage_choice",
    prompt="请输入操作（序号/删除 <序号>/取消）",
)
async def handle_manage_choice(
    matcher: Matcher,
    session: async_scoped_session,
    choice: str = ArgPlainText("manage_choice"),
) -> None:
    """统一管理面板交互（单层 reject_arg 循环）。

    通过 matcher.state["view"] 区分当前视图层级：
    - "list"：会话列表视图
    - "detail"：对话详情视图
    所有反馈均通过 reject_arg 发送，保证用户只收到一条消息。
    """
    text = choice.strip()

    if text in ("取消", "退出", "q"):
        await chat_manage_cmd.finish("已退出对话管理")

    view: str = matcher.state.get("view", "list")

    if view == "list":
        await _handle_list_view(
            matcher, session, text
        )
    else:
        await _handle_detail_view(
            matcher, session, text
        )


async def _handle_list_view(
    matcher: Matcher,
    session: async_scoped_session,
    text: str,
) -> None:
    """列表视图：删除会话 / 进入详情。"""
    sessions: list[ChatSession] = matcher.state["sessions"]

    # 删除会话（排除 "删除消息" 前缀，避免误导报错）
    if (
        text.startswith("删除")
        and not text.startswith("删除消息")
    ):
        parts = text.split()
        if (
            len(parts) < _MIN_DELETE_PARTS
            or not parts[1].isdigit()
        ):
            await chat_manage_cmd.reject_arg(
                "manage_choice",
                "格式：删除 <序号>\n例如：删除 1",
            )
        idx = int(parts[1])
        if idx < 1 or idx > len(sessions):
            await chat_manage_cmd.reject_arg(
                "manage_choice",
                f"序号超出范围（1-{len(sessions)}）",
            )

        target = sessions[idx - 1]
        # commit 前缓存字段，避免惰性加载触发 MissingGreenlet
        target_id = target.id
        target_title = target.title or "未命名"
        target.is_deleted = True
        await session.commit()

        logger.info(
            f"用户删除了对话 #{target_id}「{target_title}」"
        )
        await chat_manage_cmd.finish(
            f"已删除对话「{target_title}」"
        )

    # 进入详情
    if text.isdigit():
        idx = int(text)
        if idx < 1 or idx > len(sessions):
            await chat_manage_cmd.reject_arg(
                "manage_choice",
                f"序号超出范围（1-{len(sessions)}）",
            )

        target = sessions[idx - 1]
        messages = await _get_session_messages(
            session, target.id
        )

        lines = [
            f"═══ 对话 #{target.id}："
            f"{target.title or '未命名'} ═══",
            f"消息数：{len(messages)}",
            "",
            _fmt_message_list(messages),
            "",
            "操作：",
            "  删除消息 <ID> → 删除指定消息",
            "  返回 → 回到列表",
            "  取消 → 退出管理",
        ]

        matcher.state["view"] = "detail"
        matcher.state["current_session"] = target
        await chat_manage_cmd.reject_arg(
            "manage_choice",
            "\n".join(lines),
        )

    # 无效输入
    await chat_manage_cmd.reject_arg(
        "manage_choice",
        "请输入有效序号，或「删除 <序号>」，"
        "或「取消」退出\n"
        "（「删除消息 <ID>」需先进入对话详情）",
    )


async def _handle_detail_view(
    matcher: Matcher,
    session: async_scoped_session,
    text: str,
) -> None:
    """详情视图：删除消息 / 返回列表。"""
    # 返回会话列表
    if text == "返回":
        user_id: int = matcher.state["user_id"]
        sessions = await _get_user_sessions(
            session, user_id
        )
        matcher.state["sessions"] = sessions
        matcher.state["view"] = "list"
        matcher.state.pop("current_session", None)

        lines = [
            "═══ 对话管理 ═══",
            _fmt_session_list(sessions),
            "",
            "操作说明：",
            "  发送序号 → 查看对话详情",
            "  发送「删除 <序号>」→ 删除该对话",
            "  发送「取消」→ 退出",
        ]
        await chat_manage_cmd.reject_arg(
            "manage_choice",
            "\n".join(lines),
        )

    # 删除消息
    if text.startswith("删除消息"):
        parts = text.split()
        if (
            len(parts) < _MIN_DELETE_PARTS
            or not parts[1].isdigit()
        ):
            await chat_manage_cmd.reject_arg(
                "manage_choice",
                "格式：删除消息 <消息ID>",
            )
        msg_id = int(parts[1])
        msg = await session.get(ChatMessage, msg_id)

        if msg is None or msg.is_deleted:
            await chat_manage_cmd.reject_arg(
                "manage_choice",
                "未找到该消息，请检查 ID 是否正确",
            )

        # 验证消息属于当前会话
        current: ChatSession = matcher.state[
            "current_session"
        ]
        if msg.session_id != current.id:
            await chat_manage_cmd.reject_arg(
                "manage_choice",
                "该消息不属于当前对话",
            )

        msg.is_deleted = True
        await session.commit()

        logger.info(f"用户删除了消息 #{msg_id}")
        await chat_manage_cmd.finish(
            f"已删除消息 #{msg_id}"
        )

    # 无效操作
    await chat_manage_cmd.reject_arg(
        "manage_choice",
        "无效操作。可用：删除消息 <ID> / 返回 / 取消",
    )


# ── 超级管理员命令 ────────────────────────────────────────


@admin_chat_view_cmd.handle()
async def handle_admin_view(
    session: async_scoped_session,
    args: Message = CommandArg(),
) -> None:
    """超管查看指定用户的对话记录。

    格式：/查看用户对话 <QQ号> [会话ID]
    """
    text = args.extract_plain_text().strip()
    parts = text.split()

    if not parts or not parts[0].isdigit():
        await admin_chat_view_cmd.finish(
            "格式：/查看用户对话 <QQ号> [会话ID]"
        )

    target_uid = int(parts[0])

    # 查看指定会话的消息
    if len(parts) > 1 and parts[1].isdigit():
        sess_id = int(parts[1])
        chat_sess = await session.get(
            ChatSession, sess_id
        )
        if (
            chat_sess is None
            or chat_sess.user_id != target_uid
        ):
            await admin_chat_view_cmd.finish(
                f"未找到用户 {target_uid} 的会话 #{sess_id}"
            )

        messages = await _get_session_messages(
            session, sess_id
        )
        lines = [
            f"═══ 用户 {target_uid} 的对话 "
            f"#{sess_id} ═══",
            f"标题：{chat_sess.title or '未命名'}",
            f"消息数：{len(messages)}",
            "",
            _fmt_message_list(messages),
        ]
        await admin_chat_view_cmd.finish("\n".join(lines))

    # 列出用户所有会话
    sessions = await _get_user_sessions(
        session, target_uid
    )
    lines = [
        f"═══ 用户 {target_uid} 的对话列表 ═══",
        _fmt_session_list(sessions),
        "",
        "使用 /查看用户对话 "
        f"{target_uid} <会话ID> 查看详情",
    ]
    await admin_chat_view_cmd.finish("\n".join(lines))


@admin_chat_delete_cmd.handle()
async def handle_admin_delete(
    session: async_scoped_session,
    args: Message = CommandArg(),
) -> None:
    """超管删除指定用户的对话或消息。

    格式：
      /删除用户对话 <QQ号> 会话 <会话ID>
      /删除用户对话 <QQ号> 消息 <消息ID>
    """
    text = args.extract_plain_text().strip()
    parts = text.split()

    if (
        len(parts) < _MIN_ADMIN_DELETE_PARTS
        or not parts[0].isdigit()
    ):
        await admin_chat_delete_cmd.finish(
            "格式：\n"
            "  /删除用户对话 <QQ号> 会话 <会话ID>\n"
            "  /删除用户对话 <QQ号> 消息 <消息ID>"
        )

    target_uid = int(parts[0])
    target_type = parts[1]
    if not parts[2].isdigit():
        await admin_chat_delete_cmd.finish(
            "ID 必须为数字"
        )
    target_id = int(parts[2])

    if target_type == "会话":
        chat_sess = await session.get(
            ChatSession, target_id
        )
        if (
            chat_sess is None
            or chat_sess.user_id != target_uid
        ):
            await admin_chat_delete_cmd.finish(
                f"未找到用户 {target_uid} 的会话 "
                f"#{target_id}"
            )
        chat_sess.is_deleted = True
        await session.commit()
        logger.info(
            f"超管删除了用户 {target_uid} 的会话 "
            f"#{target_id}"
        )
        await admin_chat_delete_cmd.finish(
            f"已删除用户 {target_uid} 的会话 #{target_id}"
        )

    elif target_type == "消息":
        msg = await session.get(ChatMessage, target_id)
        if msg is None:
            await admin_chat_delete_cmd.finish(
                f"未找到消息 #{target_id}"
            )
        # 验证消息属于该用户
        chat_sess = await session.get(
            ChatSession, msg.session_id
        )
        if (
            chat_sess is None
            or chat_sess.user_id != target_uid
        ):
            await admin_chat_delete_cmd.finish(
                f"消息 #{target_id} 不属于用户 "
                f"{target_uid}"
            )
        msg.is_deleted = True
        await session.commit()
        logger.info(
            f"超管删除了用户 {target_uid} 的消息 "
            f"#{target_id}"
        )
        await admin_chat_delete_cmd.finish(
            f"已删除用户 {target_uid} 的消息 #{target_id}"
        )

    else:
        await admin_chat_delete_cmd.finish(
            "类型必须为「会话」或「消息」"
        )
