"""first revision

迁移 ID: 8dbbbcdab3a6
父迁移:
创建时间: 2026-08-22 22:46:22.533781

保留已写入 alembic_version 的 weather 分支基线。该基线没有结构变更，
但迁移文件必须存在，否则 Alembic 无法解析数据库当前版本。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


revision: str = "8dbbbcdab3a6"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = ("weather",)
depends_on: str | Sequence[str] | None = None


def upgrade(name: str = "") -> None:
    if name:
        return


def downgrade(name: str = "") -> None:
    if name:
        return
