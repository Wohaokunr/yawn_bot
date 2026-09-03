from datetime import datetime
from typing import Optional

from nonebot_plugin_orm import Model
from sqlalchemy import (
    JSON,
    BigInteger,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column


class AgentMemory(Model):
    """群摘要、人物画像、关系和回忆的统一存储。"""

    # 去重查询按 memory_type 过滤，唯一约束必须包含它，
    # 否则同键不同类型的行会在提交时 IntegrityError。
    __table_args__ = (
        UniqueConstraint(
            "scope",
            "group_id",
            "subject_user_id",
            "memory_type",
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
    # 0 表示无特定成员主体。SQLite 的 UNIQUE 约束会把多个 NULL
    # 视为互不相同，使用稳定哨兵才能真正保证每日摘要幂等。
    subject_user_id: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False, index=True
    )
    memory_type: Mapped[str] = mapped_column(String(24), default="summary", index=True)
    memory_key: Mapped[str] = mapped_column(String(128))
    content: Mapped[str] = mapped_column(Text)
    evidence_message_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    # auto 由整理任务维护；manual 由管理员维护且自动重建不会覆盖。
    source_kind: Mapped[str] = mapped_column(String(16), default="auto", index=True)
    # 冗余涉及成员，保证原始证据过期后仍可完成隐私删除。
    related_user_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
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


Index(
    "ix_agent_memory_group_type_updated_desc",
    AgentMemory.group_id,
    AgentMemory.memory_type,
    AgentMemory.updated_at.desc(),
)


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
    # auto 由整理任务维护；mention 来自 @提及正则；agent 来自对话工具；
    # manual 由管理员维护且自动重建不会覆盖。
    source_kind: Mapped[str] = mapped_column(String(16), default="auto", index=True)
    note: Mapped[str] = mapped_column(String(200), default="")
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
