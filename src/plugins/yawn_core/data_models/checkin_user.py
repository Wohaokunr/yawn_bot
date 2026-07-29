from datetime import date
from typing import TYPE_CHECKING, Optional

from nonebot_plugin_orm import Model
from sqlalchemy import BigInteger, Date, ForeignKeyConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from .user_group import UserGroup


class CheckinUser(Model):
    """群内用户的签到汇总数据。"""

    __table_args__ = (
        ForeignKeyConstraint(
            ["group_id", "user_id"],
            ["yawn_core_usergroup.group_id", "yawn_core_usergroup.user_id"],
            name="fk_checkin_user_user_group",
            ondelete="CASCADE",
        ),
    )

    # 联合主键：同一个 QQ 在不同群里分别统计
    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # 累计签到天数
    total_days: Mapped[int] = mapped_column(default=0)

    # 当前连续签到天数
    streak_days: Mapped[int] = mapped_column(default=0)

    # 总积分
    points: Mapped[int] = mapped_column(default=0)

    # 最后一次签到日期
    last_checkin_date: Mapped[Optional[date]] = mapped_column(
        Date,
        nullable=True,
    )

    user_group: Mapped["UserGroup"] = relationship(
        "UserGroup",
        back_populates="checkin_user",
    )
