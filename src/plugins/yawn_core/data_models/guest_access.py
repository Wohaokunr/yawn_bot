from datetime import datetime
from typing import Optional

from nonebot_plugin_orm import Model
from sqlalchemy import BigInteger, Boolean, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column


class GuestAccessConfig(Model):
    """WebUI 访客访问的全局策略。固定使用 id=1 的单例行。"""

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    credential_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    credential_version: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class GuestGroupAccess(Model):
    """允许访客查看的群聊白名单。"""

    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )
