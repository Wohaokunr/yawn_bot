"""add Agent bot-member bond table

Revision ID: c4e6a8b0d2f3
Revises: b8d0f2a4c6e9
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence


revision: str = "c4e6a8b0d2f3"
down_revision: str | Sequence[str] | None = "b8d0f2a4c6e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "yawn_core_agentbotbond"
_INDEXES: tuple[tuple[str, list[str]], ...] = (
    ("ix_yawn_core_agentbotbond_group_id", ["group_id"]),
    ("ix_yawn_core_agentbotbond_bot_id", ["bot_id"]),
    ("ix_yawn_core_agentbotbond_user_id", ["user_id"]),
    ("ix_yawn_core_agentbotbond_source_kind", ["source_kind"]),
    ("ix_yawn_core_agentbotbond_last_interaction_at", ["last_interaction_at"]),
)
_FAMILIARITY_INDEX = "ix_agent_bot_bond_group_bot_familiarity"


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade(name: str = "") -> None:
    if name:
        return
    if _TABLE in _tables():
        return
    # 与模型保持一致：除时间戳外只有 Python 侧默认值，不加 server_default，
    # 否则启动时的 orm check 会判定 schema 漂移。
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("stage", sa.String(length=16), nullable=False),
        sa.Column("familiarity", sa.Float(), nullable=False),
        sa.Column("affinity", sa.Float(), nullable=False),
        sa.Column("trust", sa.Float(), nullable=False),
        sa.Column("label", sa.String(length=32), nullable=False),
        sa.Column("member_calls_bot", sa.String(length=24), nullable=False),
        sa.Column("bot_calls_member", sa.String(length=24), nullable=False),
        sa.Column("tone", sa.String(length=60), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=False),
        sa.Column("inside_jokes", sa.JSON(), nullable=False),
        sa.Column("boundaries", sa.JSON(), nullable=False),
        sa.Column("locked_fields", sa.JSON(), nullable=False),
        sa.Column("source_kind", sa.String(length=16), nullable=False),
        sa.Column("member_to_bot_count", sa.Integer(), nullable=False),
        sa.Column("bot_to_member_count", sa.Integer(), nullable=False),
        sa.Column("exchange_count", sa.Integer(), nullable=False),
        sa.Column("positive_count", sa.Integer(), nullable=False),
        sa.Column("negative_count", sa.Integer(), nullable=False),
        sa.Column("active_days", sa.Integer(), nullable=False),
        sa.Column("last_active_date", sa.String(length=10), nullable=True),
        sa.Column("evidence_message_ids", sa.JSON(), nullable=False),
        sa.Column("first_interaction_at", sa.DateTime(), nullable=True),
        sa.Column("last_interaction_at", sa.DateTime(), nullable=True),
        sa.Column("last_model_update_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["yawn_core_botgroup.group_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "bot_id", "user_id", name="uq_agent_bot_bond"),
        info={"bind_key": "yawn_core"},
    )
    for index_name, columns in _INDEXES:
        op.create_index(index_name, _TABLE, columns, unique=False)
    op.create_index(
        _FAMILIARITY_INDEX,
        _TABLE,
        ["group_id", "bot_id", sa.text("familiarity DESC")],
        unique=False,
    )


def downgrade(name: str = "") -> None:
    if name:
        return
    if _TABLE not in _tables():
        return
    op.drop_index(_FAMILIARITY_INDEX, table_name=_TABLE)
    for index_name, _columns in reversed(_INDEXES):
        op.drop_index(index_name, table_name=_TABLE)
    op.drop_table(_TABLE)
