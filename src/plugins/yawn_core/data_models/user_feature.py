from datetime import datetime

from nonebot_plugin_orm import Model
from sqlalchemy import BigInteger, Boolean, ForeignKeyConstraint, String, func
from sqlalchemy.orm import Mapped, mapped_column


class UserFeature(Model):
    """群内特定用户的功能覆盖（优先级高于群级别开关）。"""

    __table_args__ = (
        ForeignKeyConstraint(
            ["group_id", "user_id"],
            ["yawn_core_usergroup.group_id", "yawn_core_usergroup.user_id"],
            name="fk_user_feature_user_group",
            ondelete="CASCADE",
        ),
    )

    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    feature: Mapped[str] = mapped_column(String(64), primary_key=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )
