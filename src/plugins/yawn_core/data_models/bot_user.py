from datetime import datetime
from typing import TYPE_CHECKING, Optional

from nonebot_plugin_orm import Model
from sqlalchemy import BigInteger, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from .user_group import UserGroup


class BotUser(Model):
    """Bot 认识的全局 QQ 用户。"""

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    nickname: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    first_interaction_at: Mapped[datetime] = mapped_column(
        server_default=func.current_timestamp(),
    )
    last_interaction_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    # 全局好感度
    affinity: Mapped[int] = mapped_column(default=0)

    groups: Mapped[list["UserGroup"]] = relationship(
        "UserGroup",
        back_populates="user",
        cascade="all, delete-orphan",
    )
