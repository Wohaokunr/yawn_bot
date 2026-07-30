from datetime import datetime

from nonebot_plugin_orm import Model
from sqlalchemy import BigInteger, Boolean, ForeignKeyConstraint, String, func
from sqlalchemy.orm import Mapped, mapped_column


class GlobalUserFeature(Model):
    """全局用户级别的功能开关（适用于私聊及跨群全局控制）。"""

    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id"],
            ["yawn_core_botuser.user_id"],
            name="fk_global_user_feature_user",
            ondelete="CASCADE",
        ),
    )

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    feature: Mapped[str] = mapped_column(String(64), primary_key=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )
