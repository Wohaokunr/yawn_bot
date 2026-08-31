"""add independent Agent critical-tool quota

Revision ID: f0c2d4e6a8b1
Revises: e4a1c6d8b9f2
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "f0c2d4e6a8b1"
down_revision: str | Sequence[str] | None = "e4a1c6d8b9f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "yawn_core_groupagentconfig"


def _columns() -> set[str]:
    return {str(item["name"]) for item in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def upgrade(name: str = "") -> None:
    if name:
        return
    columns = _columns()
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        if "critical_tool_count" not in columns:
            batch_op.add_column(
                sa.Column(
                    "critical_tool_count",
                    sa.Integer(),
                    nullable=False,
                    server_default=sa.text("0"),
                )
            )
        if "critical_tool_daily_limit" not in columns:
            batch_op.add_column(
                sa.Column(
                    "critical_tool_daily_limit",
                    sa.Integer(),
                    nullable=False,
                    server_default=sa.text("5"),
                )
            )

    # The temporary defaults above are only for backfilling existing rows.
    # Runtime defaults live in the ORM model, so leave no database default;
    # otherwise nonebot_plugin_orm autogenerate reports a schema drift.
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        for column in ("critical_tool_count", "critical_tool_daily_limit"):
            batch_op.alter_column(
                column,
                existing_type=sa.Integer(),
                existing_nullable=False,
                server_default=None,
            )


def downgrade(name: str = "") -> None:
    if name:
        return
    columns = _columns()
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        if "critical_tool_daily_limit" in columns:
            batch_op.drop_column("critical_tool_daily_limit")
        if "critical_tool_count" in columns:
            batch_op.drop_column("critical_tool_count")
