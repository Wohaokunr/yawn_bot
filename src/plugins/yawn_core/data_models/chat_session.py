"""AI 对话会话数据模型。

支持私聊与群聊（预留）两种场景，
每个用户在同一场景下可拥有多个会话。
"""

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

from nonebot_plugin_orm import Model
from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from .chat_message import ChatMessage

_BJ_TZ = timezone(timedelta(hours=8))


def _now_bj() -> datetime:
    """返回当前北京时间（naive），与项目时间约定一致。"""
    return datetime.now(_BJ_TZ).replace(tzinfo=None)


class ChatSession(Model):
    """一次 AI 对话会话。"""

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    # 会话所属用户
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)

    # 群聊预留：为 None 表示私聊会话
    group_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        nullable=True,
        default=None,
        index=True,
    )

    # 会话标题（可由用户自定义或自动截取首条消息）
    title: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(default=_now_bj)
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        nullable=True,
    )

    # 软删除标记
    is_deleted: Mapped[bool] = mapped_column(default=False)

    messages: Mapped[list["ChatMessage"]] = relationship(
        "ChatMessage",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ChatMessage.id",
    )
