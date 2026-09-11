"""add Agent proactive active-interjection config columns

为群聊 Agent 增加"热闹插话"双模式配置：
- proactive_active_enabled / proactive_active_probability /
  proactive_active_window_minutes

存量行通过 server_default 保持与 ORM Python 端 default 一致的取值。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "a9f3d2c81e67"
down_revision: str | Sequence[str] | None = "c3d8e5f17a92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(name: str = "") -> None:
    if name:
        return
    with op.batch_alter_table("yawn_core_groupagentconfig") as batch:
        batch.add_column(
            sa.Column(
                "proactive_active_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("1"),
            )
        )
        batch.add_column(
            sa.Column(
                "proactive_active_probability",
                sa.Float(),
                nullable=False,
                server_default="0.08",
            )
        )
        batch.add_column(
            sa.Column(
                "proactive_active_window_minutes",
                sa.Integer(),
                nullable=False,
                server_default="8",
            )
        )


def downgrade(name: str = "") -> None:
    if name:
        return
    with op.batch_alter_table("yawn_core_groupagentconfig") as batch:
        batch.drop_column("proactive_active_window_minutes")
        batch.drop_column("proactive_active_probability")
        batch.drop_column("proactive_active_enabled")
