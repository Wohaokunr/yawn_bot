"""add independent Agent short-conversation toggle

短会话续聊从 trigger_mode 中解耦，成为群级独立开关。存量配置按旧行为回填：
只有 mention_or_proactive 模式此前会开启短会话，因此该模式迁移为开启，
其余模式迁移为关闭。

Revision ID: e1f2a3b4c5d6
Revises: d8e2f4a6b9c1
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "e1f2a3b4c5d6"
down_revision: str | Sequence[str] | None = "d8e2f4a6b9c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "yawn_core_groupagentconfig"


def upgrade(name: str = "") -> None:
    if name:
        return
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(
            sa.Column("short_conversation_enabled", sa.Boolean(), nullable=True)
        )

    op.execute(
        sa.text(
            f"""
            UPDATE {_TABLE}
            SET short_conversation_enabled = CASE
                WHEN trigger_mode = 'mention_or_proactive' THEN 1
                ELSE 0
            END
            WHERE short_conversation_enabled IS NULL
            """
        )
    )

    with op.batch_alter_table(_TABLE) as batch:
        batch.alter_column(
            "short_conversation_enabled",
            existing_type=sa.Boolean(),
            nullable=False,
            existing_nullable=True,
        )


def downgrade(name: str = "") -> None:
    if name:
        return
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column("short_conversation_enabled")
