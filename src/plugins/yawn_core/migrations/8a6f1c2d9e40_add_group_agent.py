"""add group agent tables and member role snapshot fields"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "8a6f1c2d9e40"
down_revision: str | Sequence[str] | None = "c9d1a7163db4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(name: str = "") -> None:
    if name:
        return
    with op.batch_alter_table("yawn_core_usergroup") as batch:
        batch.add_column(sa.Column("role", sa.String(length=16), nullable=False, server_default="member"))
        batch.add_column(sa.Column("title", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("last_role_sync_at", sa.DateTime(), nullable=True))

    op.create_table(
        "yawn_core_groupagentconfig",
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("persona", sa.Text(), nullable=False, server_default="友好、自然、简洁的群友"),
        sa.Column("persona_override", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("persona_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("persona_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("trigger_mode", sa.String(length=24), nullable=False, server_default="mention_or_proactive"),
        sa.Column("proactive_probability", sa.Float(), nullable=False, server_default="0.15"),
        sa.Column("idle_threshold_minutes", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("cooldown_minutes", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("daily_limit", sa.Integer(), nullable=False, server_default="12"),
        sa.Column("raw_retention_days", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("cross_group_visibility", sa.String(length=24), nullable=False, server_default="public_summary"),
        sa.Column("media_cache_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.Column("last_agent_at", sa.DateTime(), nullable=True),
        sa.Column("proactive_day", sa.String(length=16), nullable=True),
        sa.Column("proactive_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active_topic", sa.Text(), nullable=True),
        sa.Column("emotion_state", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("last_response_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["group_id"], ["yawn_core_botgroup.group_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("group_id"),
        info={"bind_key": "yawn_core"},
    )
    op.create_table(
        "yawn_core_groupagentmessage",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("sender_name", sa.String(length=255), nullable=True),
        sa.Column("role", sa.String(length=16), nullable=False, server_default="member"),
        sa.Column("title", sa.String(length=64), nullable=True),
        sa.Column("normalized_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("segments", sa.JSON(), nullable=False),
        sa.Column("reply_chain", sa.JSON(), nullable=False),
        sa.Column("forward_tree", sa.JSON(), nullable=False),
        sa.Column("media_refs", sa.JSON(), nullable=False),
        sa.Column("received_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["group_id"], ["yawn_core_botgroup.group_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bot_id", "message_id", name="uq_agent_message_bot_message"),
        info={"bind_key": "yawn_core"},
    )
    op.create_table(
        "yawn_core_agentmemory",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False, server_default="group"),
        sa.Column("group_id", sa.BigInteger(), nullable=True),
        sa.Column("subject_user_id", sa.BigInteger(), nullable=True),
        sa.Column("memory_type", sa.String(length=24), nullable=False, server_default="summary"),
        sa.Column("memory_key", sa.String(length=128), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("evidence_message_ids", sa.JSON(), nullable=False),
        sa.Column("salience", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("visibility", sa.String(length=24), nullable=False, server_default="group"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["yawn_core_botgroup.group_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope", "group_id", "subject_user_id", "memory_key", name="uq_agent_memory_key"),
        info={"bind_key": "yawn_core"},
    )
    op.create_table(
        "yawn_core_agentrelation",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("subject_user_id", sa.BigInteger(), nullable=False),
        sa.Column("object_user_id", sa.BigInteger(), nullable=False),
        sa.Column("relation_type", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("evidence_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_seen_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["yawn_core_botgroup.group_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "subject_user_id", "object_user_id", "relation_type", name="uq_agent_relation_edge"),
        info={"bind_key": "yawn_core"},
    )
    op.create_table(
        "yawn_core_agentaudit",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=True),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=False),
        sa.Column("result", sa.String(length=24), nullable=False, server_default="success"),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["yawn_core_botgroup.group_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        info={"bind_key": "yawn_core"},
    )
    op.create_table(
        "yawn_core_agentprivacy",
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("opted_out", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["yawn_core_botgroup.group_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("group_id", "user_id"),
        info={"bind_key": "yawn_core"},
    )


def downgrade(name: str = "") -> None:
    if name:
        return
    for table in ("yawn_core_agentprivacy", "yawn_core_agentaudit", "yawn_core_agentrelation", "yawn_core_agentmemory", "yawn_core_groupagentmessage", "yawn_core_groupagentconfig"):
        op.drop_table(table)
    with op.batch_alter_table("yawn_core_usergroup") as batch:
        batch.drop_column("last_role_sync_at")
        batch.drop_column("title")
        batch.drop_column("role")
