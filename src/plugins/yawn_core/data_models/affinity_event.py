"""好感度流水：每一次加减都留一条，供每日上限、面板和 WebUI 解释“为什么变了”。"""

from datetime import date, datetime
from typing import Optional

from nonebot_plugin_orm import Model
from sqlalchemy import BigInteger, Date, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .timeutil import beijing_now


class AffinityEvent(Model):
    """一次好感度变化。

    ``BotUser.affinity`` 是全局余额，``UserGroup.group_affinity`` 是在某个群里
    攒下的部分；两者都只由 ``yawn_core.affinity`` 写，这张表是它们的流水。
    ``day`` 冗余存北京时间日期，按来源统计当日累计时不用再算时区。
    """

    __table_args__ = (
        Index("ix_affinity_event_user_source_day", "user_id", "source", "day"),
        Index("ix_affinity_event_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("yawn_core_botuser.user_id", ondelete="CASCADE"),
        index=True,
    )
    # 不挂外键：群被删除后流水仍要能解释全局余额。
    group_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    # checkin / milestone / private_chat / group_chat / manual
    source: Mapped[str] = mapped_column(String(24))
    delta: Mapped[int] = mapped_column(Integer)
    balance_after: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(String(120), default="")
    day: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(
        default=beijing_now,
        server_default=func.current_timestamp(),
    )
