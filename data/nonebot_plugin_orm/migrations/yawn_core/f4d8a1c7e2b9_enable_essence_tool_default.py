"""enable essence setting for legacy default Agent allowlists

Revision ID: f4d8a1c7e2b9
Revises: f0c2d4e6a8b1
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "f4d8a1c7e2b9"
down_revision: str | Sequence[str] | None = "f0c2d4e6a8b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "yawn_core_groupagentconfig"
_LEGACY_DEFAULT = ["mute_member", "create_group_announcement"]
_NEW_DEFAULT = [*_LEGACY_DEFAULT, "set_essence_message"]


def _config_table() -> sa.TableClause:
    return sa.table(
        _TABLE,
        sa.column("group_id", sa.BigInteger()),
        sa.column("tool_allowlist", sa.JSON()),
    )


def _replace_exact_allowlist(source: list[str], target: list[str]) -> None:
    table = _config_table()
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(table.c.group_id, table.c.tool_allowlist)
    ).mappings()
    for row in rows:
        current = row["tool_allowlist"]
        if current != source:
            continue
        bind.execute(
            sa.update(table)
            .where(table.c.group_id == row["group_id"])
            .values(tool_allowlist=target)
        )


def upgrade(name: str = "") -> None:
    if name:
        return
    # Only migrate the exact historical default. Custom/empty allowlists remain
    # untouched so an administrator's explicit security policy is preserved.
    _replace_exact_allowlist(_LEGACY_DEFAULT, _NEW_DEFAULT)


def downgrade(name: str = "") -> None:
    if name:
        return
    _replace_exact_allowlist(_NEW_DEFAULT, _LEGACY_DEFAULT)
