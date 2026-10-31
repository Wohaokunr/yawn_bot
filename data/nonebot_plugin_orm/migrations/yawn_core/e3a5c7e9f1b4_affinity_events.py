"""add affinity event ledger

Revision ID: e3a5c7e9f1b4
Revises: d7f9b1c3e5a7
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence


revision: str = "e3a5c7e9f1b4"
down_revision: str | Sequence[str] | None = "d7f9b1c3e5a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "yawn_core_affinityevent"
_INDEXES: tuple[tuple[str, list[str]], ...] = (
    ("ix_yawn_core_affinityevent_user_id", ["user_id"]),
    ("ix_affinity_event_user_source_day", ["user_id", "source", "day"]),
    ("ix_affinity_event_user_created", ["user_id", "created_at"]),
)


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade(name: str = "") -> None:
    if name:
        return
    if _TABLE in _tables():
        return
    # 与模型保持一致：除时间戳外只有 Python 侧默认值，不加 server_default。
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("balance_after", sa.Integer(), nullable=False),
        sa.Column("note", sa.String(length=120), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["yawn_core_botuser.user_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        info={"bind_key": "yawn_core"},
    )
    for index_name, columns in _INDEXES:
        op.create_index(index_name, _TABLE, columns, unique=False)


def downgrade(name: str = "") -> None:
    if name:
        return
    if _TABLE not in _tables():
        return
    for index_name, _columns in reversed(_INDEXES):
        op.drop_index(index_name, table_name=_TABLE)
    op.drop_table(_TABLE)
