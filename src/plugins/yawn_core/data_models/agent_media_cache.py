from datetime import datetime
from typing import Optional

from nonebot_plugin_orm import Model
from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column


class AgentMediaCache(Model):
    """短期 Agent 媒体缓存；按群隔离，避免跨群泄露识别结果。"""

    __table_args__ = (
        UniqueConstraint(
            "group_id",
            "content_hash",
            "media_type",
            "model_name",
            name="uq_agent_media_cache_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("yawn_core_botgroup.group_id", ondelete="CASCADE"),
        index=True,
    )
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    media_type: Mapped[str] = mapped_column(String(24), default="image")
    cache_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    caption: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    model_name: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(24), default="ready")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), index=True
    )
    last_access_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
