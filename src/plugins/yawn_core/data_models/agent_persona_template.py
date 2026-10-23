from datetime import datetime

from nonebot_plugin_orm import Model
from sqlalchemy import JSON, Boolean, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from .timeutil import beijing_now


class AgentPersonaTemplate(Model):
    """Agent 人设库：可复用的人设卡；``is_global`` 的那张是全局默认人设。

    群人设应用人设库时复制一份卡到 ``GroupAgentConfig.persona_profile``，
    之后两边互不影响；同一时刻最多一张 ``is_global=True``（由写入路径保证）。
    """

    __table_args__ = (UniqueConstraint("name", name="uq_agent_persona_template_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(32))
    card: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    is_global: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(
        default=beijing_now, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        default=beijing_now,
        onupdate=beijing_now,
        server_default=func.current_timestamp(),
    )
