"""add WebUI administrator audit log"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "d4a7f9c2e610"
down_revision: str | Sequence[str] | None = "b7e1c3a9f204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(name: str = "") -> None:
    if name:
        return
    op.create_table(
        "yawn_core_webadminaudit",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("actor_session", sa.String(length=16), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=255), nullable=True),
        sa.Column(
            "result", sa.String(length=24), nullable=False, server_default="success"
        ),
        sa.Column("detail", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id"),
        info={"bind_key": "yawn_core"},
    )
    op.create_index(
        "ix_yawn_core_webadminaudit_request_id",
        "yawn_core_webadminaudit",
        ["request_id"],
    )
    op.create_index(
        "ix_yawn_core_webadminaudit_actor_session",
        "yawn_core_webadminaudit",
        ["actor_session"],
    )
    op.create_index(
        "ix_yawn_core_webadminaudit_resource_type",
        "yawn_core_webadminaudit",
        ["resource_type"],
    )
    op.create_index(
        "ix_yawn_core_webadminaudit_result",
        "yawn_core_webadminaudit",
        ["result"],
    )
    op.create_index(
        "ix_yawn_core_webadminaudit_created_at",
        "yawn_core_webadminaudit",
        ["created_at"],
    )


def downgrade(name: str = "") -> None:
    if name:
        return
    for index in (
        "ix_yawn_core_webadminaudit_created_at",
        "ix_yawn_core_webadminaudit_result",
        "ix_yawn_core_webadminaudit_resource_type",
        "ix_yawn_core_webadminaudit_actor_session",
        "ix_yawn_core_webadminaudit_request_id",
    ):
        op.drop_index(index, table_name="yawn_core_webadminaudit")
    op.drop_table("yawn_core_webadminaudit")
