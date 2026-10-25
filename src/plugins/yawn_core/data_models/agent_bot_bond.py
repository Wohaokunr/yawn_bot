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
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .timeutil import beijing_now


class AgentBotBond(Model):
    """Bot 自己与某位群成员之间的关系（Bot 视角的一条有向边）。

    成员之间的关系在 ``AgentRelation``；这里只记 Bot 作为关系一方的那部分：
    熟悉度由结构化互动统计（@Bot、回复 Bot、Bot 回复他）确定性推导，
    好感/信任、称呼、梗、边界由记忆整理模型在有界步长内调整。
    共同经历（“我们一起做过的事”）是 ``AgentMemory`` 的 ``bond`` 类型行，
    以便复用检索、召回强化与隐私删除。
    """

    __table_args__ = (
        UniqueConstraint("group_id", "bot_id", "user_id", name="uq_agent_bot_bond"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("yawn_core_botgroup.group_id", ondelete="CASCADE"),
        index=True,
    )
    bot_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    # stranger / acquaintance / familiar / friend / close，由熟悉度和好感推导，
    # 冗余落库只为了让列表按阶段筛选时不必全表重算。
    stage: Mapped[str] = mapped_column(String(16), default="stranger")
    familiarity: Mapped[float] = mapped_column(Float, default=0.0)
    # -1（反感）~ 1（亲近）；0 是中性，未观察到情绪证据时保持不动。
    affinity: Mapped[float] = mapped_column(Float, default=0.0)
    trust: Mapped[float] = mapped_column(Float, default=0.3)
    # 一句话关系标签（“爱逗我的老朋友”“常来问代码的人”）。
    label: Mapped[str] = mapped_column(String(32), default="")
    # 成员怎么称呼 Bot / Bot 习惯怎么称呼他。
    member_calls_bot: Mapped[str] = mapped_column(String(24), default="")
    bot_calls_member: Mapped[str] = mapped_column(String(24), default="")
    # 和他说话的分寸（“可以互相吐槽”“说话要正经些”）。
    tone: Mapped[str] = mapped_column(String(60), default="")
    note: Mapped[str] = mapped_column(String(200), default="")
    inside_jokes: Mapped[list[str]] = mapped_column(JSON, default=list)
    # 他明确不希望 Bot 做的事（“别叫我外号”“别在群里提我考试”）。
    boundaries: Mapped[list[str]] = mapped_column(JSON, default=list)
    # 被管理员或成员本人锁定的字段，自动整理不再覆盖它们。
    locked_fields: Mapped[list[str]] = mapped_column(JSON, default=list)
    # auto 由整理任务维护；agent 表示成员本人在对话里改过；manual 由管理员维护。
    source_kind: Mapped[str] = mapped_column(String(16), default="auto", index=True)
    # 互动计数：成员 @/回复 Bot、Bot 回复/@ 他、二者一来一回的轮次。
    member_to_bot_count: Mapped[int] = mapped_column(Integer, default=0)
    bot_to_member_count: Mapped[int] = mapped_column(Integer, default=0)
    exchange_count: Mapped[int] = mapped_column(Integer, default=0)
    # 模型判定的正/负向互动次数（感谢、夸奖 / 吐槽、不满），不做关键词匹配。
    positive_count: Mapped[int] = mapped_column(Integer, default=0)
    negative_count: Mapped[int] = mapped_column(Integer, default=0)
    # 记录互动发生的不同自然日数，比消息条数更能区分“认识很久”和“一天聊爆”。
    active_days: Mapped[int] = mapped_column(Integer, default=0)
    last_active_date: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    evidence_message_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    first_interaction_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    last_interaction_at: Mapped[Optional[datetime]] = mapped_column(
        nullable=True, index=True
    )
    last_model_update_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        default=beijing_now, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        default=beijing_now,
        server_default=func.current_timestamp(),
        onupdate=beijing_now,
    )


Index(
    "ix_agent_bot_bond_group_bot_familiarity",
    AgentBotBond.group_id,
    AgentBotBond.bot_id,
    AgentBotBond.familiarity.desc(),
)
