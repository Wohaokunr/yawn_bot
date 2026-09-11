"""agent proactive speech tuning

主动发言优化（非常活跃档）：
- 新列 last_proactive_at：主动发言专用冷却基准，与被动回复写入的
  last_agent_at 解耦，被@答话不再封锁主动发言一个完整冷却期。
- 存量默认值升级：只更新仍等于旧默认值的行，用户自定义的群不受影响。

迁移 ID: c7d9e1f3a5b7
父迁移: b3c4d5e6f7a8
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "c7d9e1f3a5b7"
down_revision: str | Sequence[str] | None = "b3c4d5e6f7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "yawn_core_groupagentconfig"

# (列名, 旧默认, 新默认)；浮点比较用容差，避免二进制表示差异漏匹配。
_DEFAULT_BUMPS: tuple[tuple[str, float, float], ...] = (
    ("proactive_probability", 0.15, 0.35),
    ("idle_threshold_minutes", 30, 15),
    ("cooldown_minutes", 20, 8),
    ("proactive_active_probability", 0.08, 0.25),
    ("proactive_active_window_minutes", 8, 12),
    ("daily_limit", 12, 30),
)


def _bump_defaults(old_values: tuple[tuple[str, float, float], ...]) -> None:
    for column, old, new in old_values:
        if float(old) == int(old) and float(new) == int(new):
            match = f"{column} = {int(old)}"
        else:
            match = f"ABS({column} - {old}) < 0.000001"
        op.execute(
            sa.text(f"UPDATE {_TABLE} SET {column} = {new} WHERE {match}")
        )


def upgrade(name: str = "") -> None:
    if name:
        return
    # 业务列默认值由 ORM Python 端 default= 提供，可空列无需 server_default
    # （见 e5b8a0f4d3c2 的约定）。
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(sa.Column("last_proactive_at", sa.DateTime(), nullable=True))
    _bump_defaults(_DEFAULT_BUMPS)


def downgrade(name: str = "") -> None:
    if name:
        return
    _bump_defaults(
        tuple((column, new, old) for column, old, new in _DEFAULT_BUMPS)
    )
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column("last_proactive_at")
