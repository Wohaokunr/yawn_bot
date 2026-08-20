from datetime import datetime
from typing import Optional

from nonebot_plugin_orm import Model
from sqlalchemy import JSON, BigInteger, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column


class AgentAudit(Model):
    """Agent 工具调用审计记录。"""

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("yawn_core_botgroup.group_id", ondelete="CASCADE"),
        index=True,
    )
    actor_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    tool_name: Mapped[str] = mapped_column(String(64))
    arguments: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    result: Mapped[str] = mapped_column(String(24), default="success")
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.current_timestamp(), index=True
    )
