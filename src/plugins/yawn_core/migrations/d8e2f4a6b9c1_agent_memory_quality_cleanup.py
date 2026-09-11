"""normalize legacy dynamic core memories

动态兴趣、偏好、技能和常聊话题不再作为永不过期核心事实。管理员手工画像
优先；其余旧 auto core 降回 90 天画像。迁移只修复数据，不改变表结构。

迁移 ID: d8e2f4a6b9c1
父迁移: c7d9e1f3a5b7
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence


revision: str = "d8e2f4a6b9c1"
down_revision: str | Sequence[str] | None = "c7d9e1f3a5b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "yawn_core_agentmemory"
_DYNAMIC_KEYS = "'hobby','preference','skill','recurring_topic'"


def upgrade(name: str = "") -> None:
    if name:
        return
    # 同键 manual 画像是管理员结论，直接丢弃冲突的 auto core。
    op.execute(
        sa.text(
            f"""
            DELETE FROM {_TABLE} AS core
            WHERE core.memory_type = 'core'
              AND core.source_kind = 'auto'
              AND core.memory_key IN ({_DYNAMIC_KEYS})
              AND EXISTS (
                  SELECT 1 FROM {_TABLE} AS profile
                  WHERE profile.scope = core.scope
                    AND profile.group_id = core.group_id
                    AND profile.subject_user_id = core.subject_user_id
                    AND profile.memory_type = 'profile'
                    AND profile.memory_key = core.memory_key
                    AND profile.source_kind = 'manual'
              )
            """
        )
    )
    # 正常晋升链不会同时存在 auto profile；若历史数据发生冲突，保留证据
    # 更完整的 core 行再执行降级，避免唯一约束阻断升级。
    op.execute(
        sa.text(
            f"""
            DELETE FROM {_TABLE} AS profile
            WHERE profile.memory_type = 'profile'
              AND profile.source_kind != 'manual'
              AND EXISTS (
                  SELECT 1 FROM {_TABLE} AS core
                  WHERE core.scope = profile.scope
                    AND core.group_id = profile.group_id
                    AND core.subject_user_id = profile.subject_user_id
                    AND core.memory_type = 'core'
                    AND core.source_kind = 'auto'
                    AND core.memory_key = profile.memory_key
                    AND core.memory_key IN ({_DYNAMIC_KEYS})
              )
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            UPDATE {_TABLE}
            SET memory_type = 'profile',
                expires_at = DATETIME(CURRENT_TIMESTAMP, '+90 days')
            WHERE memory_type = 'core'
              AND source_kind = 'auto'
              AND memory_key IN ({_DYNAMIC_KEYS})
            """
        )
    )


def downgrade(name: str = "") -> None:
    # 数据降级不重新制造已确认有害的永不过期动态核心记忆。
    _ = name
