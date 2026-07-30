"""AI 对话消息数据模型。

存储每一轮对话中的用户消息与 AI 回复。
"""

from datetime import datetime
from typing import TYPE_CHECKING

from nonebot_plugin_orm import Model
from sqlalchemy import BigInteger, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from .chat_session import ChatSession


class ChatMessage(Model):
    """对话中的单条消息。"""

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    session_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "yawn_core_chatsession.id",
            ondelete="CASCADE",
        ),
        index=True,
    )

    # 消息角色：user / assistant / system
    role: Mapped[str] = mapped_column(String(20))

    # 消息正文
    content: Mapped[str] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        server_default=func.current_timestamp(),
    )

    # 软删除标记
    is_deleted: Mapped[bool] = mapped_column(default=False)

    session: Mapped["ChatSession"] = relationship(
        "ChatSession",
        back_populates="messages",
    )
