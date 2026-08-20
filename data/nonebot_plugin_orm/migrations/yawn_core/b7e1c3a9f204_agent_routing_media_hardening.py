"""add Agent model routing, media cache and compaction state"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "b7e1c3a9f204"
down_revision: str | Sequence[str] | None = "8a6f1c2d9e40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(name: str = "") -> None:
    if name:
        return
    # The first Agent migration omitted this nullable model column even though
    # the ORM class and context query already use it.
    with op.batch_alter_table("yawn_core_agentmemory") as batch:
        batch.add_column(sa.Column("expires_at", sa.DateTime(), nullable=True))
        batch.create_index("ix_yawn_core_agentmemory_expires_at", ["expires_at"])

    with op.batch_alter_table("yawn_core_groupagentconfig") as batch:
        batch.add_column(sa.Column("last_response_input_fingerprint", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("last_response_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("recent_response_fingerprints", sa.JSON(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("context_epoch", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("last_compacted_message_id", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("tool_day", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("admin_tool_count", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("admin_tool_daily_limit", sa.Integer(), nullable=False, server_default="30"))
        batch.add_column(sa.Column("tool_allowlist", sa.JSON(), nullable=False, server_default="[]"))

    op.create_table(
        "yawn_core_agentmediacache",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("media_type", sa.String(length=24), nullable=False, server_default="image"),
        sa.Column("cache_path", sa.String(length=512), nullable=True),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("model_name", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="ready"),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("last_access_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["yawn_core_botgroup.group_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "content_hash", "media_type", "model_name", name="uq_agent_media_cache_key"),
        info={"bind_key": "yawn_core"},
    )
    op.create_index("ix_yawn_core_agentmediacache_group_id", "yawn_core_agentmediacache", ["group_id"])
    op.create_index("ix_yawn_core_agentmediacache_content_hash", "yawn_core_agentmediacache", ["content_hash"])
    op.create_index("ix_yawn_core_agentmediacache_created_at", "yawn_core_agentmediacache", ["created_at"])
    op.create_index("ix_yawn_core_agentmediacache_expires_at", "yawn_core_agentmediacache", ["expires_at"])


def downgrade(name: str = "") -> None:
    if name:
        return
    for index in (
        "ix_yawn_core_agentmediacache_expires_at",
        "ix_yawn_core_agentmediacache_created_at",
        "ix_yawn_core_agentmediacache_content_hash",
        "ix_yawn_core_agentmediacache_group_id",
    ):
        op.drop_index(index, table_name="yawn_core_agentmediacache")
    op.drop_table("yawn_core_agentmediacache")
    with op.batch_alter_table("yawn_core_groupagentconfig") as batch:
        for column in (
            "tool_allowlist",
            "admin_tool_daily_limit",
            "admin_tool_count",
            "tool_day",
            "last_compacted_message_id",
            "context_epoch",
            "recent_response_fingerprints",
            "last_response_at",
            "last_response_input_fingerprint",
        ):
            batch.drop_column(column)
    with op.batch_alter_table("yawn_core_agentmemory") as batch:
        batch.drop_index("ix_yawn_core_agentmemory_expires_at")
        batch.drop_column("expires_at")

