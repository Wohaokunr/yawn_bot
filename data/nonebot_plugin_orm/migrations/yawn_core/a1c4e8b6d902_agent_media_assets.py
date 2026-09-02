"""add provider-aware Agent media assets

Revision ID: a1c4e8b6d902
Revises: f4d8a1c7e2b9
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "a1c4e8b6d902"
down_revision: str | Sequence[str] | None = "f4d8a1c7e2b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "yawn_core_agentmediaasset"


def upgrade(name: str = "") -> None:
    if name:
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "media_type", sa.String(length=24), nullable=False, server_default="image"
        ),
        sa.Column(
            "mime_type",
            sa.String(length=128),
            nullable=False,
            server_default="application/octet-stream",
        ),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_type", sa.String(length=32), nullable=True),
        sa.Column("source_message_id", sa.BigInteger(), nullable=True),
        sa.Column("source_file", sa.String(length=512), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("cache_path", sa.String(length=512), nullable=True),
        sa.Column(
            "provider", sa.String(length=32), nullable=False, server_default="local"
        ),
        sa.Column(
            "provider_scope",
            sa.String(length=96),
            nullable=False,
            server_default="local",
        ),
        sa.Column("remote_file_id", sa.String(length=255), nullable=True),
        sa.Column("remote_created_at", sa.DateTime(), nullable=True),
        sa.Column("remote_expires_at", sa.DateTime(), nullable=True),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("caption_model", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "last_used_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column(
            "status", sa.String(length=24), nullable=False, server_default="ready"
        ),
        sa.ForeignKeyConstraint(
            ["group_id"], ["yawn_core_botgroup.group_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "group_id",
            "content_hash",
            "provider",
            "provider_scope",
            name="uq_agent_media_asset_provider_scope",
        ),
        info={"bind_key": "yawn_core"},
    )
    for column in (
        "group_id",
        "content_hash",
        "remote_file_id",
        "remote_expires_at",
        "created_at",
        "last_used_at",
        "expires_at",
        "status",
    ):
        op.create_index(f"ix_{_TABLE}_{column}", _TABLE, [column], unique=False)


def downgrade(name: str = "") -> None:
    if name:
        return
    for column in reversed(
        (
            "group_id",
            "content_hash",
            "remote_file_id",
            "remote_expires_at",
            "created_at",
            "last_used_at",
            "expires_at",
            "status",
        )
    ):
        op.drop_index(f"ix_{_TABLE}_{column}", table_name=_TABLE)
    op.drop_table(_TABLE)
