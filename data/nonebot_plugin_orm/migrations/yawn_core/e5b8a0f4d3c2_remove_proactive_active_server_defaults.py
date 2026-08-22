"""remove server_default from proactive active config columns

a9f3d2c81e67 加列时为了给存量行赋值带了 server_default，但本插件
约定业务列默认值一律由 ORM Python 端 default= 提供（见
c3d8e5f17a92）；compare_server_default 开启时启动检查会把残留的
server_default 判为模型与库的差异并拒绝启动。此迁移在存量行已经
拿到默认值之后把 server_default 摘掉。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "e5b8a0f4d3c2"
down_revision: str | Sequence[str] | None = "a9f3d2c81e67"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(name: str = "") -> None:
    if name:
        return
    with op.batch_alter_table("yawn_core_groupagentconfig", schema=None) as batch_op:
        batch_op.alter_column(
            "proactive_active_enabled",
            existing_type=sa.Boolean(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "proactive_active_probability",
            existing_type=sa.Float(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "proactive_active_window_minutes",
            existing_type=sa.Integer(),
            server_default=None,
            existing_nullable=False,
        )


def downgrade(name: str = "") -> None:
    if name:
        return
    with op.batch_alter_table("yawn_core_groupagentconfig", schema=None) as batch_op:
        batch_op.alter_column(
            "proactive_active_window_minutes",
            existing_type=sa.Integer(),
            server_default="8",
            existing_nullable=False,
        )
        batch_op.alter_column(
            "proactive_active_probability",
            existing_type=sa.Float(),
            server_default="0.08",
            existing_nullable=False,
        )
        batch_op.alter_column(
            "proactive_active_enabled",
            existing_type=sa.Boolean(),
            server_default=sa.text("1"),
            existing_nullable=False,
        )
