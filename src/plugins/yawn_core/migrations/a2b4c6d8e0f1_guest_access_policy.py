"""add persistent WebUI guest access policy

Revision ID: a2b4c6d8e0f1
Revises: f6a7b8c9d0e1
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "a2b4c6d8e0f1"
down_revision: str | Sequence[str] | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(name: str = "") -> None:
    if name:
        return
    op.create_table(
        "yawn_core_guestaccessconfig",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("credential_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "credential_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
        ),
        sa.PrimaryKeyConstraint("id"),
        info={"bind_key": "yawn_core"},
    )
    op.create_table(
        "yawn_core_guestgroupaccess",
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
        ),
        sa.PrimaryKeyConstraint("group_id"),
        info={"bind_key": "yawn_core"},
    )


def downgrade(name: str = "") -> None:
    if name:
        return
    op.drop_table("yawn_core_guestgroupaccess")
    op.drop_table("yawn_core_guestaccessconfig")
