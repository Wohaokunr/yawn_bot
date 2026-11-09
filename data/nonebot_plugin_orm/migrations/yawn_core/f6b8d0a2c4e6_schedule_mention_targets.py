"""add mention targets to agent scheduled tasks

群定时任务到点可以 @全体成员 或 @群里的其他成员：新增可空 JSON 列
``mention_targets``（``["all", 123456, ...]``）。旧行保持 NULL，读取时按空列表处理，
所以不需要回填。

Revision ID: f6b8d0a2c4e6
Revises: e3a5c7e9f1b4
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "f6b8d0a2c4e6"
down_revision: str | Sequence[str] | None = "e3a5c7e9f1b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "yawn_core_agentscheduledtask"
_COLUMN = "mention_targets"


def _columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return set()
    return {item["name"] for item in inspector.get_columns(_TABLE)}


def upgrade(name: str = "") -> None:
    if name:
        return
    columns = _columns()
    if not columns or _COLUMN in columns:
        return
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(sa.Column(_COLUMN, sa.JSON(), nullable=True))


def downgrade(name: str = "") -> None:
    if name:
        return
    if _COLUMN not in _columns():
        return
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_COLUMN)
