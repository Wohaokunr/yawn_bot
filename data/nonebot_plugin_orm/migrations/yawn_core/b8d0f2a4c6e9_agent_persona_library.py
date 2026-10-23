"""add Agent persona library (global default persona)

Revision ID: b8d0f2a4c6e9
Revises: a7c3e9f1b5d2
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence


revision: str = "b8d0f2a4c6e9"
down_revision: str | Sequence[str] | None = "a7c3e9f1b5d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "yawn_core_agentpersonatemplate"
_GLOBAL_INDEX = "ix_yawn_core_agentpersonatemplate_is_global"


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade(name: str = "") -> None:
    if name:
        return
    if _TABLE in _tables():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("card", sa.JSON(), nullable=False),
        # 与模型保持一致：is_global/revision 只有 Python 侧默认值，不加
        # server_default，否则启动时的 orm check 会判定 schema 漂移。
        sa.Column("is_global", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_agent_persona_template_name"),
        info={"bind_key": "yawn_core"},
    )
    op.create_index(_GLOBAL_INDEX, _TABLE, ["is_global"], unique=False)


def downgrade(name: str = "") -> None:
    if name:
        return
    if _TABLE not in _tables():
        return
    op.drop_index(_GLOBAL_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
