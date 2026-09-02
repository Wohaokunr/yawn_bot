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


class AgentMediaAsset(Model):
    """Agent 媒体资产索引。

    ``GroupAgentMessage.media_refs`` 仍然是消息侧唯一的媒体引用列表；本表只保存
    已物化资产、Provider 远端文件以及 caption 的生命周期信息。
    """

    __table_args__ = (
        UniqueConstraint(
            "group_id",
            "content_hash",
            "provider",
            "provider_scope",
            name="uq_agent_media_asset_provider_scope",
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
    mime_type: Mapped[str] = mapped_column(
        String(128), default="application/octet-stream"
    )
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)

    source_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    source_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    source_file: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    source_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    cache_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    provider: Mapped[str] = mapped_column(String(32), default="local")
    provider_scope: Mapped[str] = mapped_column(String(96), default="local")
    remote_file_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True
    )
    remote_created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    remote_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, index=True
    )

    caption: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    caption_model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), index=True
    )
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(24), default="ready", index=True)
