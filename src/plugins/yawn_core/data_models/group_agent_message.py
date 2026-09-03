from datetime import datetime
from typing import Optional

from nonebot_plugin_orm import Model
from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column


class GroupAgentMessage(Model):
    """短期群消息索引；媒体只保存可重建引用。"""

    __table_args__ = (
        UniqueConstraint(
            "bot_id",
            "group_id",
            "message_id",
            name="uq_agent_message_bot_group_message",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger, index=True)
    group_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("yawn_core_botgroup.group_id", ondelete="CASCADE"),
        index=True,
    )
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    sender_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(16), default="member")
    title: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    normalized_text: Mapped[str] = mapped_column(Text, default="")
    segments: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    reply_chain: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    forward_tree: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    media_refs: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    received_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), index=True
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, index=True
    )


Index(
    "ix_agent_message_group_bot_id_desc",
    GroupAgentMessage.group_id,
    GroupAgentMessage.bot_id,
    GroupAgentMessage.id.desc(),
)
