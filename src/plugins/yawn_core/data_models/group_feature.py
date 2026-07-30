from datetime import datetime

from nonebot_plugin_orm import Model
from sqlalchemy import BigInteger, Boolean, ForeignKeyConstraint, String, func
from sqlalchemy.orm import Mapped, mapped_column


class GroupFeature(Model):
    """群级别的功能开关。"""

    __table_args__ = (
        ForeignKeyConstraint(
            ["group_id"],
            ["yawn_core_botgroup.group_id"],
            name="fk_group_feature_group",
            ondelete="CASCADE",
        ),
    )

    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    feature: Mapped[str] = mapped_column(String(64), primary_key=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )
