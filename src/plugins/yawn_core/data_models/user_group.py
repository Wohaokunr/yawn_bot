from datetime import datetime
from typing import TYPE_CHECKING, Optional

from nonebot_plugin_orm import Model
from sqlalchemy import BigInteger, Boolean, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from .bot_group import BotGroup
    from .bot_user import BotUser
    from .checkin_record import CheckinRecord
    from .checkin_user import CheckinUser


class UserGroup(Model):
    """用户与 QQ 群之间的关系。"""

    group_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("yawn_core_botgroup.group_id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("yawn_core_botuser.user_id", ondelete="CASCADE"),
        primary_key=True,
    )

    first_seen_at: Mapped[datetime] = mapped_column(
        server_default=func.current_timestamp(),
    )
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    group_nickname: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    role: Mapped[str] = mapped_column(String(16), default="member")
    title: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_role_sync_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    # 群内扩展字段
    group_affinity: Mapped[int] = mapped_column(default=0)
    exp: Mapped[int] = mapped_column(default=0)
    coins: Mapped[int] = mapped_column(default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    user: Mapped["BotUser"] = relationship(
        "BotUser",
        back_populates="groups",
    )
    group: Mapped["BotGroup"] = relationship(
        "BotGroup",
        back_populates="members",
    )
    checkin_user: Mapped[Optional["CheckinUser"]] = relationship(
        "CheckinUser",
        back_populates="user_group",
        cascade="all, delete-orphan",
        uselist=False,
    )
    checkin_records: Mapped[list["CheckinRecord"]] = relationship(
        "CheckinRecord",
        back_populates="user_group",
        cascade="all, delete-orphan",
    )
