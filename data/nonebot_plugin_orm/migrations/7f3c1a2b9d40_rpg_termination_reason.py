"""add RPG termination reason"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "7f3c1a2b9d40"
down_revision = "4d4511a9af13"
branch_labels = None
depends_on = None


def upgrade(name: str = "") -> None:
    if name:
        return
    with op.batch_alter_table("yawn_rpg_rpggame") as batch_op:
        batch_op.add_column(sa.Column("termination_reason", sa.String(32), nullable=True))
    op.create_table(
        "yawn_rpg_rpgplayerguide",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("tutorial_version", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("skipped_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_yawn_rpg_rpgplayerguide")),
        info={"bind_key": "yawn_rpg"},
    )


def downgrade(name: str = "") -> None:
    if name:
        return
    op.drop_table("yawn_rpg_rpgplayerguide")
    with op.batch_alter_table("yawn_rpg_rpggame") as batch_op:
        batch_op.drop_column("termination_reason")
