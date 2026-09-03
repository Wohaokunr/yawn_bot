"""add Agent hot-path composite indexes

Revision ID: f3d5a7c9e1b2
Revises: f0c2d4e6a8b1
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "f3d5a7c9e1b2"
down_revision: str | Sequence[str] | None = "f0c2d4e6a8b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MESSAGE_TABLE = "yawn_core_groupagentmessage"
_MEMORY_TABLE = "yawn_core_agentmemory"
_MESSAGE_INDEX = "ix_agent_message_group_bot_id_desc"
_MEMORY_SUMMARY_INDEX = "ix_agent_memory_group_type_updated_desc"


def _index_names(table: str) -> set[str]:
    return {
        str(item["name"])
        for item in sa.inspect(op.get_bind()).get_indexes(table)
        if item.get("name")
    }


def upgrade(name: str = "") -> None:
    if name:
        return
    if _MESSAGE_INDEX not in _index_names(_MESSAGE_TABLE):
        op.create_index(
            _MESSAGE_INDEX,
            _MESSAGE_TABLE,
            ["group_id", "bot_id", sa.text("id DESC")],
            unique=False,
        )
    if _MEMORY_SUMMARY_INDEX not in _index_names(_MEMORY_TABLE):
        op.create_index(
            _MEMORY_SUMMARY_INDEX,
            _MEMORY_TABLE,
            ["group_id", "memory_type", sa.text("updated_at DESC")],
            unique=False,
        )


def downgrade(name: str = "") -> None:
    if name:
        return
    if _MEMORY_SUMMARY_INDEX in _index_names(_MEMORY_TABLE):
        op.drop_index(_MEMORY_SUMMARY_INDEX, table_name=_MEMORY_TABLE)
    if _MESSAGE_INDEX in _index_names(_MESSAGE_TABLE):
        op.drop_index(_MESSAGE_INDEX, table_name=_MESSAGE_TABLE)
