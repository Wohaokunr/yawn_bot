"""track Agent memory recall for reinforcement and add subject lookup index

Revision ID: a7c3e9f1b5d2
Revises: f3d5a7c9e1b2
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence


revision: str = "a7c3e9f1b5d2"
down_revision: str | Sequence[str] | None = "f3d5a7c9e1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "yawn_core_agentmemory"
_INDEX = "ix_agent_memory_group_subject_type"


def _columns() -> set[str]:
    return {
        str(column["name"]) for column in sa.inspect(op.get_bind()).get_columns(_TABLE)
    }


def _indexes() -> set[str]:
    return {
        str(index["name"])
        for index in sa.inspect(op.get_bind()).get_indexes(_TABLE)
        if index.get("name")
    }


def upgrade(name: str = "") -> None:
    if name:
        return
    columns = _columns()
    if "access_count" not in columns or "last_accessed_at" not in columns:
        with op.batch_alter_table(_TABLE, schema=None) as batch_op:
            if "access_count" not in columns:
                batch_op.add_column(
                    sa.Column(
                        "access_count",
                        sa.Integer(),
                        nullable=False,
                        server_default="0",
                    )
                )
            if "last_accessed_at" not in columns:
                batch_op.add_column(
                    sa.Column("last_accessed_at", sa.DateTime(), nullable=True)
                )
    if _INDEX not in _indexes():
        op.create_index(
            _INDEX,
            _TABLE,
            ["group_id", "subject_user_id", "memory_type"],
            unique=False,
        )


def downgrade(name: str = "") -> None:
    if name:
        return
    if _INDEX in _indexes():
        op.drop_index(_INDEX, table_name=_TABLE)
    columns = _columns()
    if "access_count" in columns or "last_accessed_at" in columns:
        with op.batch_alter_table(_TABLE, schema=None) as batch_op:
            if "last_accessed_at" in columns:
                batch_op.drop_column("last_accessed_at")
            if "access_count" in columns:
                batch_op.drop_column("access_count")
