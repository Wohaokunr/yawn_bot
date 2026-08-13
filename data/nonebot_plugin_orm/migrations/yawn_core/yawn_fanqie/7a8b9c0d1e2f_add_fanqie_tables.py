"""add fanqie public novel download tables

迁移 ID: 7a8b9c0d1e2f
父迁移：

本迁移只提交到 canonical data/nonebot_plugin_orm/migrations/；部署时由维护者
手动执行 ``uv run nb orm upgrade heads``。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "7a8b9c0d1e2f"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = ("yawn_fanqie",)
depends_on: str | Sequence[str] | None = None


def upgrade(name: str = "") -> None:
    if name:
        return
    op.create_table(
        "yawn_fanqie_fanqiebook",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("book_id", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("author", sa.String(length=128), nullable=True),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("url", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_yawn_fanqie_fanqiebook")),
        sa.UniqueConstraint("book_id", name=op.f("uq_yawn_fanqie_fanqiebook_book_id")),
        info={"bind_key": "yawn_fanqie"},
    )
    with op.batch_alter_table("yawn_fanqie_fanqiebook") as batch_op:
        batch_op.create_index(
            batch_op.f("ix_yawn_fanqie_fanqiebook_book_id"),
            ["book_id"],
            unique=False,
        )

    op.create_table(
        "yawn_fanqie_fanqiejob",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("book_record_id", sa.Integer(), nullable=False),
        sa.Column("requester_user_id", sa.BigInteger(), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=True),
        sa.Column("start_chapter", sa.Integer(), nullable=False),
        sa.Column("end_chapter", sa.Integer(), nullable=False),
        sa.Column("total_chapters", sa.Integer(), nullable=False),
        sa.Column("completed_chapters", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("output_path", sa.String(length=1024), nullable=True),
        sa.Column("output_name", sa.String(length=256), nullable=True),
        sa.Column("last_error", sa.String(length=1000), nullable=True),
        sa.Column("send_status", sa.String(length=16), nullable=False),
        sa.Column("send_error", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["book_record_id"],
            ["yawn_fanqie_fanqiebook.id"],
            name=op.f(
                "fk_yawn_fanqie_fanqiejob_book_record_id_yawn_fanqie_fanqiebook"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_yawn_fanqie_fanqiejob")),
        info={"bind_key": "yawn_fanqie"},
    )
    with op.batch_alter_table("yawn_fanqie_fanqiejob") as batch_op:
        batch_op.create_index(
            batch_op.f("ix_yawn_fanqie_fanqiejob_book_record_id"),
            ["book_record_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_yawn_fanqie_fanqiejob_requester_user_id"),
            ["requester_user_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_yawn_fanqie_fanqiejob_group_id"),
            ["group_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_yawn_fanqie_fanqiejob_status"),
            ["status"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_yawn_fanqie_fanqiejob_created_at"),
            ["created_at"],
            unique=False,
        )

    op.create_table(
        "yawn_fanqie_fanqiejobchapter",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("chapter_index", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("is_locked", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("temp_path", sa.String(length=1024), nullable=True),
        sa.Column("last_error", sa.String(length=1000), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["yawn_fanqie_fanqiejob.id"],
            name=op.f("fk_yawn_fanqie_fanqiejobchapter_job_id_yawn_fanqie_fanqiejob"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_yawn_fanqie_fanqiejobchapter")),
        sa.UniqueConstraint(
            "job_id",
            "chapter_index",
            name=op.f("uq_fanqie_job_chapter_index"),
        ),
        info={"bind_key": "yawn_fanqie"},
    )
    with op.batch_alter_table("yawn_fanqie_fanqiejobchapter") as batch_op:
        batch_op.create_index(
            batch_op.f("ix_yawn_fanqie_fanqiejobchapter_job_id"),
            ["job_id"],
            unique=False,
        )


def downgrade(name: str = "") -> None:
    if name:
        return
    with op.batch_alter_table("yawn_fanqie_fanqiejobchapter") as batch_op:
        batch_op.drop_index(batch_op.f("ix_yawn_fanqie_fanqiejobchapter_job_id"))
    op.drop_table("yawn_fanqie_fanqiejobchapter")
    with op.batch_alter_table("yawn_fanqie_fanqiejob") as batch_op:
        batch_op.drop_index(batch_op.f("ix_yawn_fanqie_fanqiejob_created_at"))
        batch_op.drop_index(batch_op.f("ix_yawn_fanqie_fanqiejob_status"))
        batch_op.drop_index(batch_op.f("ix_yawn_fanqie_fanqiejob_group_id"))
        batch_op.drop_index(batch_op.f("ix_yawn_fanqie_fanqiejob_requester_user_id"))
        batch_op.drop_index(batch_op.f("ix_yawn_fanqie_fanqiejob_book_record_id"))
    op.drop_table("yawn_fanqie_fanqiejob")
    with op.batch_alter_table("yawn_fanqie_fanqiebook") as batch_op:
        batch_op.drop_index(batch_op.f("ix_yawn_fanqie_fanqiebook_book_id"))
    op.drop_table("yawn_fanqie_fanqiebook")
