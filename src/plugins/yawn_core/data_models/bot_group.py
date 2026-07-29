from datetime import datetime
from typing import TYPE_CHECKING, Optional

from nonebot_plugin_orm import Model
from sqlalchemy import BigInteger, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from .user_group import UserGroup


class BotGroup(Model):
    """Bot 认识的 QQ 群。"""

    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    group_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    first_seen_at: Mapped[datetime] = mapped_column(
        server_default=func.current_timestamp(),
    )
    last_active_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    members: Mapped[list["UserGroup"]] = relationship(
        "UserGroup",
        back_populates="group",
        cascade="all, delete-orphan",
    )
