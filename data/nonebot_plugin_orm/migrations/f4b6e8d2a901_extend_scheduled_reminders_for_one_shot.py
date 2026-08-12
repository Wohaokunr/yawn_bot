"""extend scheduled reminders for recurring and one-shot schedules

Revision ID: f4b6e8d2a901
Revises: ea3af2a76220
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence


revision: str = "f4b6e8d2a901"
down_revision: str | Sequence[str] | None = "ea3af2a76220"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(name: str = "") -> None:
    if name:
        return
    with op.batch_alter_table("yawn_core_scheduledreminder", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "schedule_type",
                sa.String(length=16),
                server_default="recurring",
                nullable=False,
            )
        )
        batch_op.add_column(sa.Column("run_at", sa.DateTime(), nullable=True))
        batch_op.alter_column(
            "cron_expression",
            existing_type=sa.String(length=128),
            nullable=True,
        )
        batch_op.alter_column(
            "schedule_type",
            existing_type=sa.String(length=16),
            server_default=None,
        )


def downgrade(name: str = "") -> None:
    if name:
        return
    with op.batch_alter_table("yawn_core_scheduledreminder", schema=None) as batch_op:
        batch_op.drop_column("run_at")
        batch_op.drop_column("schedule_type")
        batch_op.alter_column(
            "cron_expression",
            existing_type=sa.String(length=128),
            nullable=False,
        )
