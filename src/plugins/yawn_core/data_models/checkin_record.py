from datetime import date, datetime
from typing import TYPE_CHECKING

from nonebot_plugin_orm import Model
from sqlalchemy import BigInteger, Date, ForeignKeyConstraint, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from .user_group import UserGroup


class CheckinRecord(Model):
    """每一次签到的历史记录。"""

    __table_args__ = (
        # 数据库层面保证每人在每个群每天只能签到一次
        UniqueConstraint(
            "group_id",
            "user_id",
            "checkin_date",
            name="uq_checkin_record_once_per_day",
        ),
        ForeignKeyConstraint(
            ["group_id", "user_id"],
            ["yawn_core_usergroup.group_id", "yawn_core_usergroup.user_id"],
            name="fk_checkin_record_user_group",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    group_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)

    checkin_date: Mapped[date] = mapped_column(Date)

    # 本次签到获得的积分
    reward: Mapped[int]

    # 记录创建时间
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.current_timestamp(),
    )

    user_group: Mapped["UserGroup"] = relationship(
        "UserGroup",
        back_populates="checkin_records",
    )
