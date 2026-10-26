"""add Agent scheduled task table

Revision ID: d7f9b1c3e5a7
Revises: c4e6a8b0d2f3
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence


revision: str = "d7f9b1c3e5a7"
down_revision: str | Sequence[str] | None = "c4e6a8b0d2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "yawn_core_agentscheduledtask"
_INDEXES: tuple[tuple[str, list[str]], ...] = (
    ("ix_yawn_core_agentscheduledtask_group_id", ["group_id"]),
    ("ix_yawn_core_agentscheduledtask_creator_user_id", ["creator_user_id"]),
    ("ix_yawn_core_agentscheduledtask_status", ["status"]),
    ("ix_yawn_core_agentscheduledtask_next_run_at", ["next_run_at"]),
    ("ix_agent_scheduled_task_due", ["status", "next_run_at"]),
    ("ix_agent_scheduled_task_group_status", ["group_id", "status"]),
)


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
        sa.Column("bot_id", sa.BigInteger(), nullable=True),
        sa.Column("creator_user_id", sa.BigInteger(), nullable=False),
        sa.Column("creator_name", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=40), nullable=False),
        sa.Column("instruction", sa.String(length=600), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("schedule", sa.JSON(), nullable=False),
        sa.Column("schedule_kind", sa.String(length=16), nullable=False),
        sa.Column("description", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("status_reason", sa.String(length=120), nullable=False),
        sa.Column("mention_creator", sa.Boolean(), nullable=False),
        sa.Column("max_runs", sa.Integer(), nullable=True),
        sa.Column("run_count", sa.Integer(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("skip_count", sa.Integer(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(), nullable=True),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("last_result", sa.String(length=24), nullable=False),
        sa.Column("last_error", sa.String(length=240), nullable=False),
        sa.Column("last_output", sa.String(length=300), nullable=False),
        sa.Column("last_trace_id", sa.String(length=64), nullable=True),
        sa.Column("history", sa.JSON(), nullable=False),
        sa.Column("source_kind", sa.String(length=16), nullable=False),
        sa.Column("lease_until", sa.DateTime(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("cancelled_by", sa.BigInteger(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
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
