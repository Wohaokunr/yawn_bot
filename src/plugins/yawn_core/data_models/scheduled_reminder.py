from datetime import datetime
from typing import Optional

from nonebot_plugin_orm import Model
from sqlalchemy import JSON, BigInteger, Boolean, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column


class ScheduledReminder(Model):
    """群聊定时提醒配置。"""

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    group_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("yawn_core_botgroup.group_id", ondelete="CASCADE"),
        index=True,
    )

    creator_user_id: Mapped[int] = mapped_column(BigInteger, index=True)

    name: Mapped[str] = mapped_column(String(64))
    cron_expression: Mapped[str] = mapped_column(String(128))

    # group：发送到所属群；private：发送给所属群内的指定用户
    target_type: Mapped[str] = mapped_column(String(16))
    target_user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        nullable=True,
    )

    # [{"type": "text", "data": {"text": "..." }}, ...]
    message_segments: Mapped[list[dict[str, object]]] = mapped_column(JSON)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        server_default=func.current_timestamp(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )
    last_run_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    last_success_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(
        String(2000),
        nullable=True,
    )
