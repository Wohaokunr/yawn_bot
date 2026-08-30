"""split Agent trigger mode into orthogonal participation capabilities

Revision ID: b4d6e8f0a2c4
Revises: a2b4c6d8e0f1
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "b4d6e8f0a2c4"
down_revision: str | Sequence[str] | None = "a2b4c6d8e0f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "yawn_core_groupagentconfig"


def upgrade(name: str = "") -> None:
    if name:
        return
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(
            sa.Column(
                "reply_trigger_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
        batch_op.add_column(
            sa.Column(
                "explicit_wakeup_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
        batch_op.add_column(
            sa.Column(
                "proactive_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )

    # Preserve the exact behavior of every legacy trigger_mode.
    op.execute(
        sa.text(
            f"""
            UPDATE {_TABLE}
            SET
                reply_trigger_enabled = CASE
                    WHEN trigger_mode IN (
                        'mention_or_reply', 'mention_or_proactive'
                    ) THEN 1
                    ELSE 0
                END,
                explicit_wakeup_enabled = CASE
                    WHEN trigger_mode IN (
                        'explicit_wakeup', 'mention_or_proactive'
                    ) THEN 1
                    ELSE 0
                END,
                proactive_enabled = CASE
                    WHEN trigger_mode = 'mention_or_proactive' THEN 1
                    ELSE 0
                END
            """
        )
    )


    with op.batch_alter_table(_TABLE) as batch_op:
        for column in (
            "reply_trigger_enabled",
            "explicit_wakeup_enabled",
            "proactive_enabled",
        ):
            batch_op.alter_column(
                column,
                existing_type=sa.Boolean(),
                existing_nullable=False,
                server_default=None,
            )


def downgrade(name: str = "") -> None:
    if name:
        return
    # Reconstruct the closest legacy mode before removing the new capability columns.
    op.execute(
        sa.text(
            f"""
            UPDATE {_TABLE}
            SET trigger_mode = CASE
                WHEN proactive_enabled = 1 THEN 'mention_or_proactive'
                WHEN explicit_wakeup_enabled = 1 THEN 'explicit_wakeup'
                WHEN reply_trigger_enabled = 1 THEN 'mention_or_reply'
                ELSE 'mention_only'
            END
            """
        )
    )
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_column("proactive_enabled")
        batch_op.drop_column("explicit_wakeup_enabled")
        batch_op.drop_column("reply_trigger_enabled")
