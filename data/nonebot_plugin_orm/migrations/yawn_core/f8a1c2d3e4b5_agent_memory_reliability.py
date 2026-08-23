"""repair Agent memory identity, provenance, privacy, and rebuild state

Revision ID: f8a1c2d3e4b5
Revises: e5b8a0f4d3c2
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence


revision: str = "f8a1c2d3e4b5"
down_revision: str | Sequence[str] | None = "e5b8a0f4d3c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_map(table_name: str) -> dict[str, dict[str, object]]:
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


def upgrade(name: str = "") -> None:  # noqa: C901,PLR0912,PLR0915
    if name:
        return

    memory_columns = _column_map("yawn_core_agentmemory")
    if "source_kind" not in memory_columns or "related_user_ids" not in memory_columns:
        with op.batch_alter_table("yawn_core_agentmemory", schema=None) as batch_op:
            if "source_kind" not in memory_columns:
                batch_op.add_column(
                    sa.Column(
                        "source_kind",
                        sa.String(length=16),
                        nullable=False,
                        server_default="auto",
                    )
                )
            if "related_user_ids" not in memory_columns:
                batch_op.add_column(
                    sa.Column(
                        "related_user_ids",
                        sa.JSON(),
                        nullable=False,
                        server_default="[]",
                    )
                )
    if (
        "ix_yawn_core_agentmemory_source_kind"
        not in _index_names("yawn_core_agentmemory")
    ):
        with op.batch_alter_table("yawn_core_agentmemory", schema=None) as batch_op:
            batch_op.create_index(
                "ix_yawn_core_agentmemory_source_kind", ["source_kind"], unique=False
            )

    op.execute(
        "UPDATE yawn_core_agentmemory SET source_kind = 'manual' "
        "WHERE memory_type = 'manual'"
    )
    op.execute(
        "UPDATE yawn_core_agentmemory "
        "SET related_user_ids = json_array(subject_user_id) "
        "WHERE source_kind = 'manual' AND subject_user_id IS NOT NULL"
    )
    # 旧自动派生数据可能由漏批或宽松提取产生；保留手工记忆，启动后重建。
    op.execute("DELETE FROM yawn_core_agentmemory WHERE source_kind = 'auto'")
    op.execute("DELETE FROM yawn_core_agentrelation")

    memory_columns = _column_map("yawn_core_agentmemory")
    if bool(memory_columns["subject_user_id"].get("nullable", True)):
        with op.batch_alter_table("yawn_core_agentmemory", schema=None) as batch_op:
            batch_op.drop_constraint("uq_agent_memory_key", type_="unique")
        op.execute(
            "UPDATE yawn_core_agentmemory SET subject_user_id = 0 "
            "WHERE subject_user_id IS NULL"
        )
        with op.batch_alter_table("yawn_core_agentmemory", schema=None) as batch_op:
            batch_op.alter_column(
                "subject_user_id",
                existing_type=sa.BigInteger(),
                nullable=False,
            )
            batch_op.create_unique_constraint(
                "uq_agent_memory_key",
                [
                    "scope",
                    "group_id",
                    "subject_user_id",
                    "memory_type",
                    "memory_key",
                ],
            )

    memory_columns = _column_map("yawn_core_agentmemory")
    if (
        memory_columns["source_kind"].get("default") is not None
        or memory_columns["related_user_ids"].get("default") is not None
    ):
        with op.batch_alter_table("yawn_core_agentmemory", schema=None) as batch_op:
            if memory_columns["source_kind"].get("default") is not None:
                batch_op.alter_column(
                    "source_kind",
                    existing_type=sa.String(length=16),
                    server_default=None,
                    existing_nullable=False,
                )
            if memory_columns["related_user_ids"].get("default") is not None:
                batch_op.alter_column(
                    "related_user_ids",
                    existing_type=sa.JSON(),
                    server_default=None,
                    existing_nullable=False,
                )

    config_columns = _column_map("yawn_core_groupagentconfig")
    missing_config_columns = {
        "memory_rebuild_required",
        "memory_last_attempt_at",
        "memory_last_success_at",
        "memory_last_error",
        "memory_consecutive_failures",
    } - set(config_columns)
    if missing_config_columns:
        with op.batch_alter_table(
            "yawn_core_groupagentconfig", schema=None
        ) as batch_op:
            if "memory_rebuild_required" in missing_config_columns:
                batch_op.add_column(
                    sa.Column(
                        "memory_rebuild_required",
                        sa.Boolean(),
                        nullable=False,
                        server_default=sa.text("1"),
                    )
                )
            if "memory_last_attempt_at" in missing_config_columns:
                batch_op.add_column(sa.Column("memory_last_attempt_at", sa.DateTime()))
            if "memory_last_success_at" in missing_config_columns:
                batch_op.add_column(sa.Column("memory_last_success_at", sa.DateTime()))
            if "memory_last_error" in missing_config_columns:
                batch_op.add_column(sa.Column("memory_last_error", sa.Text()))
            if "memory_consecutive_failures" in missing_config_columns:
                batch_op.add_column(
                    sa.Column(
                        "memory_consecutive_failures",
                        sa.Integer(),
                        nullable=False,
                        server_default="0",
                    )
                )

    # 存量行已由临时默认值回填；另起 batch 再移除默认，避免 SQLite
    # 在同一次表重建的 INSERT...SELECT 中看不到刚加入的列默认值。
    config_columns = _column_map("yawn_core_groupagentconfig")
    if (
        config_columns["memory_rebuild_required"].get("default") is not None
        or config_columns["memory_consecutive_failures"].get("default") is not None
    ):
        with op.batch_alter_table(
            "yawn_core_groupagentconfig", schema=None
        ) as batch_op:
            if config_columns["memory_rebuild_required"].get("default") is not None:
                batch_op.alter_column(
                    "memory_rebuild_required",
                    existing_type=sa.Boolean(),
                    server_default=None,
                    existing_nullable=False,
                )
            if config_columns["memory_consecutive_failures"].get("default") is not None:
                batch_op.alter_column(
                    "memory_consecutive_failures",
                    existing_type=sa.Integer(),
                    server_default=None,
                    existing_nullable=False,
                )


def downgrade(name: str = "") -> None:
    if name:
        return

    with op.batch_alter_table("yawn_core_groupagentconfig", schema=None) as batch_op:
        batch_op.drop_column("memory_consecutive_failures")
        batch_op.drop_column("memory_last_error")
        batch_op.drop_column("memory_last_success_at")
        batch_op.drop_column("memory_last_attempt_at")
        batch_op.drop_column("memory_rebuild_required")

    with op.batch_alter_table("yawn_core_agentmemory", schema=None) as batch_op:
        batch_op.drop_constraint("uq_agent_memory_key", type_="unique")
        batch_op.alter_column(
            "subject_user_id",
            existing_type=sa.BigInteger(),
            nullable=True,
        )
    op.execute(
        "UPDATE yawn_core_agentmemory SET subject_user_id = NULL "
        "WHERE subject_user_id = 0"
    )
    with op.batch_alter_table("yawn_core_agentmemory", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_agent_memory_key",
            ["scope", "group_id", "subject_user_id", "memory_type", "memory_key"],
        )
        batch_op.drop_index("ix_yawn_core_agentmemory_source_kind")
        batch_op.drop_column("related_user_ids")
        batch_op.drop_column("source_kind")
