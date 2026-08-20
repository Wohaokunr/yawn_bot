from datetime import datetime
from typing import Optional

from nonebot_plugin_orm import Model
from sqlalchemy import (
    JSON,
    BigInteger,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column


class AgentMemory(Model):
    """群摘要、人物画像、关系和回忆的统一存储。"""

    __table_args__ = (
        UniqueConstraint(
            "scope",
            "group_id",
            "subject_user_id",
            "memory_key",
            name="uq_agent_memory_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(16), default="group", index=True)
    group_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("yawn_core_botgroup.group_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    subject_user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True, index=True
    )
    memory_type: Mapped[str] = mapped_column(String(24), default="summary", index=True)
    memory_key: Mapped[str] = mapped_column(String(128))
    content: Mapped[str] = mapped_column(Text)
    evidence_message_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    salience: Mapped[float] = mapped_column(Float, default=0.5)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    visibility: Mapped[str] = mapped_column(String(24), default="group")
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.current_timestamp(), onupdate=func.current_timestamp()
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(nullable=True, index=True)


class AgentRelation(Model):
    """群内人物关系边。"""

    __table_args__ = (
        UniqueConstraint(
            "group_id",
            "subject_user_id",
            "object_user_id",
            "relation_type",
            name="uq_agent_relation_edge",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("yawn_core_botgroup.group_id", ondelete="CASCADE"),
        index=True,
    )
    subject_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    object_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    relation_type: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    evidence_count: Mapped[int] = mapped_column(Integer, default=1)
    last_seen_at: Mapped[datetime] = mapped_column(
        server_default=func.current_timestamp()
    )


class AgentPrivacy(Model):
    """成员级 Agent 退出状态。"""

    group_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("yawn_core_botgroup.group_id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    opted_out: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.current_timestamp(), onupdate=func.current_timestamp()
    )
