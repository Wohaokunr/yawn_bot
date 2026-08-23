"""rpg game event log id

迁移 ID: b2f7d4e91a63
父迁移: 7f3c1a2b9d40
创建时间: 2026-08-22 22:10:00.000000

RPGGame 增加 event_log_id（事件日志稳定 id），赛后据此定位 JSONL 回放。
业务列不设 server_default；旧行保持 NULL，回放端点据此优雅降级。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence


revision: str = "b2f7d4e91a63"
down_revision: str | Sequence[str] | None = "7f3c1a2b9d40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(name: str = "") -> None:
    if name:
        return
    with op.batch_alter_table("yawn_rpg_rpggame", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("event_log_id", sa.String(length=64), nullable=True)
        )
        batch_op.create_index(
            op.f("ix_yawn_rpg_rpggame_event_log_id"),
            ["event_log_id"],
            unique=False,
        )


def downgrade(name: str = "") -> None:
    if name:
        return
    with op.batch_alter_table("yawn_rpg_rpggame", schema=None) as batch_op:
        batch_op.drop_index(op.f("ix_yawn_rpg_rpggame_event_log_id"))
        batch_op.drop_column("event_log_id")
