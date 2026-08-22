from datetime import datetime
from typing import Optional

from nonebot_plugin_orm import Model
from sqlalchemy import JSON, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column


class WebAdminAudit(Model):
    """WebUI 管理写操作的脱敏审计记录。"""

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    actor_session: Mapped[str] = mapped_column(String(16), index=True)
    action: Mapped[str] = mapped_column(String(16))
    resource_type: Mapped[str] = mapped_column(String(64), index=True)
    resource_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    result: Mapped[str] = mapped_column(String(24), default="success", index=True)
    detail: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.current_timestamp(), index=True
    )
