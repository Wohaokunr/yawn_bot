"""harden Agent message identity for cross-group OneBot ids

Revision ID: f6a7b8c9d0e1
Revises: e1f2a3b4c5d6
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "f6a7b8c9d0e1"
down_revision: str | Sequence[str] | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "yawn_core_groupagentmessage"
_OLD = "uq_agent_message_bot_message"
_NEW = "uq_agent_message_bot_group_message"


def _unique_names() -> set[str]:
    return {
        str(item["name"])
        for item in sa.inspect(op.get_bind()).get_unique_constraints(_TABLE)
        if item.get("name")
    }


def upgrade(name: str = "") -> None:
    if name:
        return
    names = _unique_names()
    if _NEW in names:
        return
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        if _OLD in names:
            batch_op.drop_constraint(_OLD, type_="unique")
        batch_op.create_unique_constraint(
            _NEW,
            ["bot_id", "group_id", "message_id"],
        )


def downgrade(name: str = "") -> None:
    if name:
        return
    names = _unique_names()
    if _OLD in names:
        return
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        if _NEW in names:
            batch_op.drop_constraint(_NEW, type_="unique")
        batch_op.create_unique_constraint(_OLD, ["bot_id", "message_id"])
