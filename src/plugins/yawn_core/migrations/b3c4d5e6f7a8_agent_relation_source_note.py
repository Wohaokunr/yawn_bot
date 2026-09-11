"""Agent relation edge provenance and note

Revision ID: b3c4d5e6f7a8
Revises: f8a1c2d3e4b5
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence


revision: str = "b3c4d5e6f7a8"
down_revision: str | Sequence[str] | None = "f8a1c2d3e4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_map(table_name: str) -> dict[str, Any]:
    return {
        str(column["name"]): column
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _index_names(table_name: str) -> set[str]:
    return {
        str(index["name"])
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
        if index.get("name")
    }


def upgrade(name: str = "") -> None:
    if name:
        return

    relation_columns = _column_map("yawn_core_agentrelation")
    if "source_kind" not in relation_columns or "note" not in relation_columns:
        with op.batch_alter_table("yawn_core_agentrelation", schema=None) as batch_op:
            if "source_kind" not in relation_columns:
                batch_op.add_column(
                    sa.Column(
                        "source_kind",
                        sa.String(length=16),
                        nullable=False,
                        server_default="auto",
                    )
                )
            if "note" not in relation_columns:
                batch_op.add_column(
                    sa.Column(
                        "note",
                        sa.String(length=200),
                        nullable=False,
                        server_default="",
                    )
                )
    # 列刚以 server_default 'auto' 加入，此处统一口径以防空列残留；
    # 存量边全部由整理任务派生，manual/agent 上线后才可能出现。
    op.execute("UPDATE yawn_core_agentrelation SET source_kind = 'auto'")
    if "ix_yawn_core_agentrelation_source_kind" not in _index_names(
        "yawn_core_agentrelation"
    ):
        with op.batch_alter_table("yawn_core_agentrelation", schema=None) as batch_op:
            batch_op.create_index(
                "ix_yawn_core_agentrelation_source_kind", ["source_kind"], unique=False
            )
    # 默认值收敛到 ORM 层，与 yawn_core 其他表的约定一致。
    relation_columns = _column_map("yawn_core_agentrelation")
    if (
        relation_columns["source_kind"].get("default") is not None
        or relation_columns["note"].get("default") is not None
    ):
        with op.batch_alter_table("yawn_core_agentrelation", schema=None) as batch_op:
            if relation_columns["source_kind"].get("default") is not None:
                batch_op.alter_column(
                    "source_kind",
                    existing_type=sa.String(length=16),
                    server_default=None,
                    existing_nullable=False,
                )
            if relation_columns["note"].get("default") is not None:
                batch_op.alter_column(
                    "note",
                    existing_type=sa.String(length=200),
                    server_default=None,
                    existing_nullable=False,
                )


def downgrade(name: str = "") -> None:
    if name:
        return

    with op.batch_alter_table("yawn_core_agentrelation", schema=None) as batch_op:
        batch_op.drop_index("ix_yawn_core_agentrelation_source_kind")
        batch_op.drop_column("note")
        batch_op.drop_column("source_kind")
